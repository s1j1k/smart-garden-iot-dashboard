#!/usr/bin/env python3
"""Remove repeated MQTT messages stored before the single-writer ingest.

Until the dashboard stopped writing, every open browser session stored every
message, so data/sensor_readings.sqlite holds up to ~14 identical copies of
each reading. This keeps the first copy (lowest id) of:

  mqtt_messages    rows with the same topic and payload_json
  sensor_readings  rows with the same device_id, timestamp, source, replay,
                   ph_raw, tds_ppm and uptime_ms (the copies of one message)

Dry run by default. Stop the services first and keep the backup:

  sudo systemctl stop smart-garden-ingest smart-garden-dashboard
  .venv/bin/python scripts/dedupe_sqlite.py               # counts only
  .venv/bin/python scripts/dedupe_sqlite.py --apply       # backup, then delete
  .venv/bin/python scripts/dedupe_sqlite.py --apply --vacuum
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config import DEFAULT_CONFIG  # noqa: E402

MQTT_DUPES = """
    SELECT COUNT(*) FROM mqtt_messages
    WHERE id NOT IN (SELECT MIN(id) FROM mqtt_messages GROUP BY topic, payload_json)
"""
READING_GROUP = """
    device_id, timestamp, source, IFNULL(replay, 0), IFNULL(ph_raw, -999),
    IFNULL(tds_ppm, -999), IFNULL(uptime_ms, -1)
"""


def count(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    mqtt_total = conn.execute("SELECT COUNT(*) FROM mqtt_messages").fetchone()[0]
    mqtt_dupes = conn.execute(MQTT_DUPES).fetchone()[0]
    readings_total = conn.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0]
    readings_dupes = conn.execute(
        f"""SELECT COUNT(*) FROM sensor_readings WHERE id NOT IN
            (SELECT MIN(id) FROM sensor_readings GROUP BY {READING_GROUP})"""
    ).fetchone()[0]
    return mqtt_total, mqtt_dupes, readings_total, readings_dupes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_CONFIG["sqlite_path"])
    parser.add_argument("--apply", action="store_true", help="back up, then delete repeats")
    parser.add_argument("--vacuum", action="store_true", help="reclaim disk space afterwards")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"no database at {db}")
        return 1
    conn = sqlite3.connect(str(db))
    # Make sure the new identity columns exist before grouping on uptime_ms.
    from sqlite_store import ensure_schema

    ensure_schema(db)
    mt, md, rt, rd = count(conn)
    print(f"mqtt_messages:   {mt} rows, {md} repeats ({mt - md} kept)")
    print(f"sensor_readings: {rt} rows, {rd} repeats ({rt - rd} kept)")
    if not args.apply:
        print("dry run — pass --apply to delete (a backup is written first)")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db.with_name(f"{db.stem}_before_dedupe_{stamp}{db.suffix}")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    shutil.copy2(db, backup)
    print(f"backup: {backup}")

    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute(
            "DELETE FROM mqtt_messages WHERE id NOT IN "
            "(SELECT MIN(id) FROM mqtt_messages GROUP BY topic, payload_json)"
        )
        conn.execute(
            f"DELETE FROM sensor_readings WHERE id NOT IN "
            f"(SELECT MIN(id) FROM sensor_readings GROUP BY {READING_GROUP})"
        )
    mt, md, rt, rd = count(conn)
    print(f"after: mqtt_messages {mt} rows, sensor_readings {rt} rows")
    if args.vacuum:
        print("vacuum…")
        conn.execute("VACUUM")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
