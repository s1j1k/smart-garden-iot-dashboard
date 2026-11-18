"""Fast paths for a large store: id-bounded windows, incremental stage changes,
and skipping InfluxDB when SQLite already has the history."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import history_loader  # noqa: E402
import sqlite_store  # noqa: E402

START = datetime(2026, 10, 10, tzinfo=timezone.utc)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fill(db: Path, stages: list[str], every_min: int = 10) -> None:
    for i, stage in enumerate(stages):
        when = START + timedelta(minutes=i * every_min)
        sqlite_store.insert_sensor_reading(db, {
            "received_at": _iso(when), "source": "mqtt", "device_id": "esp32",
            "timestamp": _iso(when) if i != 3 else "millis:1234", "reading_key": f"k{i}",
            "grow_stage": stage, "tds_ppm": 700, "tank_volume_l": 7.0})


def test_first_id_since_finds_the_window_start(tmp_path):
    db = tmp_path / "g.sqlite"
    _fill(db, ["vegetative"] * 50)
    conn = sqlite_store._connect(db)
    try:
        assert sqlite_store.first_id_since(conn, _iso(START)) == 1
        assert sqlite_store.first_id_since(conn, _iso(START + timedelta(minutes=200))) == 21
        assert sqlite_store.first_id_since(conn, _iso(START + timedelta(minutes=195))) == 21
        assert sqlite_store.first_id_since(conn, _iso(START + timedelta(days=9))) == 51
    finally:
        conn.close()


def test_stage_changes_are_read_incrementally(tmp_path):
    db = tmp_path / "g.sqlite"
    _fill(db, ["seedling"] * 5 + ["vegetative"] * 5)
    first = sqlite_store.load_stage_changes(db)
    assert [s for _, s in first] == ["seedling", "vegetative"]
    # New rows: only they are read, and the earlier changes are kept.
    for i in range(10, 14):
        when = START + timedelta(minutes=i * 10)
        sqlite_store.insert_sensor_reading(db, {
            "received_at": _iso(when), "source": "mqtt", "device_id": "esp32",
            "timestamp": _iso(when), "reading_key": f"n{i}", "grow_stage": "harvest"})
    again = sqlite_store.load_stage_changes(db)
    assert [s for _, s in again] == ["seedling", "vegetative", "harvest"]
    assert sqlite_store._stage_cache[str(db.resolve())][0] == 14


def test_window_queries_still_return_the_window(tmp_path):
    db = tmp_path / "g.sqlite"
    _fill(db, ["vegetative"] * 30)
    rows = sqlite_store.load_hourly_conditions(db, _iso(START + timedelta(hours=2)))
    assert [r["hour"] for r in rows] == ["2026-10-10T02", "2026-10-10T03", "2026-10-10T04"]
    uptake = sqlite_store.load_uptake_readings(db, _iso(START + timedelta(hours=4)))
    assert len(uptake) == 6 and uptake[0]["timestamp"].startswith("2026-10-10T04:00")


def test_influx_skipped_when_sqlite_covers_the_window():
    since = _iso(START)
    rows = [{"timestamp": _iso(START + timedelta(minutes=20 + i))} for i in range(10)]
    assert history_loader.sqlite_covers(rows, since, limit=4000)
    late = [{"timestamp": _iso(START + timedelta(hours=5))}]
    assert not history_loader.sqlite_covers(late, since, limit=4000)
    assert history_loader.sqlite_covers(late * 3, since, limit=3)         # filled the limit
    assert not history_loader.sqlite_covers([], since, limit=4000)
    assert not history_loader.sqlite_covers([{"timestamp": "millis:5"}], since, limit=4000)
