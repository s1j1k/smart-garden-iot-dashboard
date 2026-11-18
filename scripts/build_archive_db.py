#!/usr/bin/env python3
"""Build a small archive database from a Pi backup, cut off at one moment.

The archive holds every normalized reading up to --until and the controller's
event messages (doses, stage changes) up to the same moment, but not the raw
telemetry copies that make up most of a backup. With SMART_GARDEN_ARCHIVE_NOW set
to the same moment, the dashboard then shows the record as it was then.

    python scripts/build_archive_db.py --source data/pi_backup/db_backup_2026-10-24.sqlite \\
        --until 2026-10-18T13:00:00Z --out data/archive/archive_2026-10-18T1300Z.sqlite
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sqlite_store  # noqa: E402


def last_id_before(conn: sqlite3.Connection, until: str) -> int:
    """Largest sensor_readings id received at or before ``until`` (received_at has no
    index on a Pi database, so bisect on id, which follows arrival order)."""
    low, high = conn.execute("SELECT MIN(id), MAX(id) FROM sensor_readings").fetchone()
    best = low - 1
    while low <= high:
        mid = (low + high) // 2
        row = conn.execute("SELECT id, received_at FROM sensor_readings WHERE id >= ? "
                           "ORDER BY id LIMIT 1", (mid,)).fetchone()
        if row is None:
            high = mid - 1
        elif row[1] <= until:
            best, low = row[0], row[0] + 1
        else:
            high = mid - 1
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True)
    parser.add_argument("--until", required=True, help="ISO UTC, e.g. 2026-10-18T13:00:00Z")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    sqlite_store.ensure_schema(out)

    src = sqlite3.connect(f"file:{Path(args.source).as_posix()}?mode=ro", uri=True)
    cut = last_id_before(src, args.until)
    src.close()
    conn = sqlite3.connect(str(out))
    conn.execute("ATTACH DATABASE ? AS src", (str(Path(args.source)),))

    def columns(schema: str, table: str) -> list[str]:
        return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]

    shared = [c for c in columns("main", "sensor_readings") if c in columns("src", "sensor_readings")]
    cols = ", ".join(shared)
    conn.execute(f"INSERT INTO main.sensor_readings ({cols}) SELECT {cols} FROM src.sensor_readings "
                 "WHERE id <= ?", (cut,))
    shared = [c for c in columns("main", "mqtt_messages") if c in columns("src", "mqtt_messages")]
    cols = ", ".join(shared)
    conn.execute(f"INSERT INTO main.mqtt_messages ({cols}) SELECT {cols} FROM src.mqtt_messages "
                 "WHERE msg_key >= 'events:' AND msg_key < 'events;' AND received_at <= ?", (args.until,))
    conn.commit()
    n_read = conn.execute("SELECT COUNT(*) FROM main.sensor_readings").fetchone()[0]
    n_ev = conn.execute("SELECT COUNT(*) FROM main.mqtt_messages").fetchone()[0]
    conn.execute("DETACH DATABASE src")
    conn.execute("VACUUM")
    conn.close()
    print(f"{out}: {n_read:,} readings and {n_ev:,} events up to {args.until}")


if __name__ == "__main__":
    main()
