"""Per-plant growth from plants.jsonl, stage labels, and the random-forest stage estimate."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import plant_growth  # noqa: E402

START = datetime(2026, 10, 1, 0, 0)


def _utc(local: datetime) -> str:
    return local.astimezone(timezone.utc).isoformat()


def _write_log(path: Path, days: int = 6, every_min: int = 30) -> None:
    """Two plants; seedlings for 3 days (small, slow), then vegetative (big, fast)."""
    lines = []
    steps = days * 24 * 60 // every_min
    for i in range(steps):
        when = START + timedelta(minutes=i * every_min)
        day = (when - START).total_seconds() / 86400
        area = 0.002 + 0.0005 * day if day < 3 else 0.004 + 0.006 * (day - 3)
        lines.append(json.dumps({
            "timestamp": when.strftime("%Y-%m-%d %H:%M:%S"),
            "image": f"{when:%Y-%m-%d_%H-%M-%S}.jpg",
            "plants": [
                {"box": [100, 100, 50, 60], "area_frac": area, "height_frac": 10 * area,
                 "width_frac": 8 * area, "leaf_count": 2 if day < 3 else 5},
                {"box": [600, 100, 50, 60], "area_frac": 1.2 * area, "height_frac": 11 * area,
                 "width_frac": 9 * area, "leaf_count": 2 if day < 3 else 6},
            ],
        }))
    lines.append("not json")                      # a torn line is skipped
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


CHANGES = [(_utc(START - timedelta(hours=1)), "seedling"),
           (_utc(START + timedelta(days=3)), "vegetative")]


def test_load_and_growth_rate(tmp_path):
    log = tmp_path / "plants.jsonl"
    _write_log(log, days=2)
    plants = plant_growth.load_plants(log)
    assert set(plants["plant"]) == {1, 2}
    assert len(plants) == 2 * 2 * 48
    rated = plant_growth.with_growth_rate(plants)
    first_hours = rated[rated["timestamp"] < START + timedelta(hours=11)]
    assert first_hours["growth_pct_day"].isna().all()       # not seen for half a day yet
    later = rated[(rated["plant"] == 1) & (rated["timestamp"] > START + timedelta(days=1, hours=1))]
    # area 0.002 + 0.0005/day: relative growth falls from ~25 % to 20 % a day by day 2
    assert later["growth_pct_day"].dropna().between(19.5, 25.5).all()


def test_stage_labels_follow_the_controller():
    times = __import__("pandas").DatetimeIndex(
        [START + timedelta(hours=h) for h in (-2, 5, 80)])
    labels = plant_growth.stage_at(times, CHANGES)
    assert labels.isna().iloc[0]
    assert list(labels.iloc[1:]) == ["seedling", "vegetative"]
    setup = plant_growth.stage_at(times, [(_utc(START - timedelta(days=1)), "filling")])
    assert setup.isna().all()                                # set-up stages are not labels


def test_needs_enough_days_and_stages(tmp_path):
    log = tmp_path / "plants.jsonl"
    _write_log(log, days=2)
    hourly = plant_growth.hourly_features(plant_growth.load_plants(log))
    estimate = plant_growth.estimate_stage(hourly, CHANGES)
    assert not estimate["ready"] and "3 days" in estimate["reason"]
    assert not plant_growth.estimate_stage(hourly.iloc[0:0], CHANGES)["ready"]


def test_forest_learns_the_stage(tmp_path):
    pytest.importorskip("sklearn")
    log = tmp_path / "plants.jsonl"
    _write_log(log, days=6)
    hourly = plant_growth.hourly_features(plant_growth.load_plants(log))
    estimate = plant_growth.estimate_stage(hourly, CHANGES)
    assert estimate["ready"]
    assert estimate["prediction"] == "vegetative"
    assert estimate["confidence"] > 0.8
    assert estimate["days"] == 6 and set(estimate["classes"]) == {"seedling", "vegetative"}
    assert estimate["accuracy"] > 0.8
    assert set(estimate["importance"]) == set(plant_growth.FRAME_FEATURES)   # old log: no cm
    assert abs(sum(estimate["importance"].values()) - 1) < 1e-6


# --- Uptake features (sensor log) and the age-schedule baseline -----------------

def _readings(days: int = 3, water_l_day: float = 0.2, salts_mg_day: float = 300.0,
              every_min: int = 1, **events) -> "pd.DataFrame":
    """A 7 L tank losing ``water_l_day`` and ``salts_mg_day``, topped up every 6 h.

    events: dose_at / drain_at / refill_at (hours from START) add a nutrient dose of
    500 mg, a 1 h drain of 2 L, or a top-up bottle refill.
    """
    import pandas as pd

    rows = []
    volume, salts, stock = 7.0, 5600.0, 5000.0
    step = every_min / (24 * 60)
    for i in range(int(days / step)):
        hour = i * step * 24
        volume -= water_l_day * step
        salts -= salts_mg_day * step
        pump = drain = 0
        if i % (6 * 60 // every_min) == 0 and i:             # top-up: bottle to tank
            volume += 0.05
            stock -= 50
        if "dose_at" in events and abs(hour - events["dose_at"]) < step * 24 / 2:
            salts += 500
            pump = 1
        if "drain_at" in events and events["drain_at"] <= hour < events["drain_at"] + 1:
            salts *= (volume - 2 * step * 24) / volume
            volume -= 2 * step * 24
            drain = 1
        if "refill_at" in events and abs(hour - events["refill_at"]) < step * 24 / 2:
            stock = 5000.0
        rows.append({"timestamp": START + timedelta(minutes=i * every_min),
                     "tank_volume_l": volume, "tds_ppm": salts / volume,
                     "water_rem_ml": stock, "nutrient_pump_on": pump, "drain_pump_on": drain})
    return pd.DataFrame(rows)


def test_water_uptake_counts_top_ups_and_ignores_refills():
    uptake = plant_growth.uptake_features(_readings(water_l_day=0.2, refill_at=31))
    later = uptake.loc[START + timedelta(days=1, hours=6):, "water_uptake_l_day"]
    assert later.notna().all()
    assert later.between(0.17, 0.23).all()


def test_nutrient_uptake_leaves_out_doses():
    uptake = plant_growth.uptake_features(_readings(salts_mg_day=300, dose_at=36.5))
    later = uptake.loc[START + timedelta(days=1, hours=6):, "nutrient_uptake_mg_day"]
    assert later.notna().all()
    assert later.between(270, 330).all()                    # the 500 mg dose is not "negative uptake"


def test_drain_is_not_uptake():
    uptake = plant_growth.uptake_features(_readings(water_l_day=0.2, drain_at=40))
    later = uptake.loc[START + timedelta(days=2, hours=2):, "water_uptake_l_day"]
    assert later.between(0.17, 0.23).all()                  # 2 L drained, not drunk


def test_readings_frame_moves_utc_to_local_clock():
    rows = [{"timestamp": _utc(START), "tank_volume_l": 7, "tds_ppm": "700",
             "water_rem_ml": None, "nutrient_pump_on": 1}]
    frame = plant_growth.readings_frame(rows)
    assert frame["timestamp"].iloc[0] == START
    assert frame["tds_ppm"].iloc[0] == 700 and frame["drain_pump_on"].isna().all()
    assert plant_growth.uptake_features(plant_growth.readings_frame([])).empty


def test_uptake_readings_from_sqlite(tmp_path):
    import sqlite_store

    db = tmp_path / "garden.sqlite"
    for i in range(12):                                     # two minutes of 10 s readings
        when = START + timedelta(seconds=10 * i)
        sqlite_store.insert_sensor_reading(db, {
            "received_at": _utc(when), "source": "mqtt", "device_id": "esp32",
            "timestamp": _utc(when).replace("+00:00", "Z"), "reading_key": f"k{i}",
            "tank_volume_l": 7.0 + i / 100, "tds_ppm": 700, "water_rem_ml": 4000,
            "nutrient_pump_on": i == 3, "drain_pump_on": False})
    rows = sqlite_store.load_uptake_readings(db, _utc(START - timedelta(hours=1)))
    assert len(rows) == 2
    assert rows[0]["nutrient_pump_on"] == 1 and rows[1]["nutrient_pump_on"] == 0
    assert abs(rows[0]["tank_volume_l"] - 7.025) < 1e-9
    assert sqlite_store.load_uptake_readings(tmp_path / "missing.sqlite", "") == []


def test_age_schedule_follows_the_weeks():
    import pandas as pd

    times = pd.DatetimeIndex([START + timedelta(days=d) for d in (-1, 2, 9, 30, 50)])
    stages = plant_growth.age_schedule(times, START)
    assert stages.iloc[0] is None
    assert list(stages.iloc[1:]) == ["seedling", "transitional", "vegetative", "harvest"]
    aware = plant_growth.age_schedule(times, START.astimezone())
    assert list(aware) == list(stages)


def test_forest_uses_uptake_and_is_scored_against_the_age_schedule(tmp_path):
    pytest.importorskip("sklearn")
    import pandas as pd

    log = tmp_path / "plants.jsonl"
    _write_log(log, days=6)
    readings = pd.concat([_readings(days=3, water_l_day=0.05, salts_mg_day=50),
                          _readings(days=3, water_l_day=0.4, salts_mg_day=400)
                          .assign(timestamp=lambda f: f["timestamp"] + timedelta(days=3))])
    uptake = plant_growth.uptake_features(readings.reset_index(drop=True))
    hourly = plant_growth.hourly_features(plant_growth.load_plants(log), uptake)
    assert set(plant_growth.UPTAKE_FEATURES) <= set(hourly.columns)

    # Planted 10 days before START: the schedule says transitional throughout,
    # so it is wrong on every seedling hour the forest gets right.
    estimate = plant_growth.estimate_stage(hourly, CHANGES, START - timedelta(days=10))
    assert estimate["ready"] and estimate["prediction"] == "vegetative"
    assert set(estimate["importance"]) == set(plant_growth.FRAME_FEATURES + plant_growth.UPTAKE_FEATURES)
    assert estimate["baseline_now"] == "transitional"
    assert estimate["baseline_accuracy"] < estimate["accuracy"]


def test_forest_without_sensor_log_uses_camera_only(tmp_path):
    pytest.importorskip("sklearn")
    log = tmp_path / "plants.jsonl"
    _write_log(log, days=6)
    empty = plant_growth.uptake_features(plant_growth.readings_frame([]))
    hourly = plant_growth.hourly_features(plant_growth.load_plants(log), empty)
    estimate = plant_growth.estimate_stage(hourly, CHANGES)
    assert set(estimate["importance"]) == set(plant_growth.FRAME_FEATURES)
    assert "baseline_accuracy" not in estimate


# --- Real size (cm) from the pot rims -----------------------------------------

def _write_cm_log(path: Path, days: int = 4, every_min: int = 60) -> None:
    """One plant whose leaf area doubles every 2 days. The camera moves back at
    day 2 (mm/px 0.24 -> 0.34: frame shares shrink, cm do not), and one frame per
    day has a misread rim (scale 2x off)."""
    lines = []
    for i in range(days * 24 * 60 // every_min):
        when = START + timedelta(minutes=i * every_min)
        day = (when - START).total_seconds() / 86400
        area_cm2 = 2.0 * 2 ** (day / 2)
        mm = 0.24 if day < 2 else 0.34
        if i % 24 == 5:
            mm *= 2                                     # a cable hides a rim
        frac = area_cm2 / ((mm / 10) ** 2) / (1280 * 720)
        lines.append(json.dumps({"timestamp": when.strftime("%Y-%m-%d %H:%M:%S"), "plants": [
            {"number": 1, "area_frac": frac, "height_frac": 0.1, "width_frac": 0.1, "leaf_count": 3,
             "area_cm2": area_cm2 * (4 if i % 24 == 5 else 1), "height_cm": 2.0, "width_cm": 3.0,
             "mm_per_px": mm}]}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_cm_features_survive_a_camera_move_and_drop_misread_frames(tmp_path):
    log = tmp_path / "plants.jsonl"
    _write_cm_log(log)
    plants = plant_growth.load_plants(log)
    assert plant_growth.size_features(plants) == plant_growth.FEATURES
    bad = plants[plants.index % 24 == 5]
    assert bad["area_cm2"].isna().all()                 # misread rim: cm blanked
    rated = plant_growth.with_growth_rate(plants)
    later = rated[rated["timestamp"] > START + timedelta(days=1, hours=2)]["growth_pct_day"].dropna()
    assert later.between(40.0, 42.5).all()              # 2x in 2 days = 41.4 %/day, move or not
    hourly = plant_growth.hourly_features(rated)
    assert "area_cm2" in hourly and "area_pct" not in hourly



def test_unlit_frames_are_left_out(tmp_path):
    log = tmp_path / "plants.jsonl"
    lines = [
        {"timestamp": "2026-10-15 12:00:00", "flash": True, "plants": [
            {"number": 1, "area_frac": 0.01, "height_frac": 0.1, "width_frac": 0.1,
             "leaf_count": 3, "area_cm2": 4.0, "height_cm": 2.0, "width_cm": 3.0, "mm_per_px": 0.13}]},
        {"timestamp": "2026-10-19 12:00:00", "flash": False, "plants": [
            {"number": 1, "area_frac": 0.001, "area_cm2": 0.2, "mm_per_px": 0.13}]},
        {"timestamp": "2026-10-14 12:00:00", "plants": [          # older log: no flash field
            {"number": 1, "area_frac": 0.01, "height_frac": 0.1, "width_frac": 0.1,
             "leaf_count": 3, "area_cm2": 3.5, "height_cm": 2.0, "width_cm": 3.0, "mm_per_px": 0.13}]},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    plants = plant_growth.load_plants(log)
    assert len(plants) == 2 and 0.2 not in set(plants["area_cm2"])
