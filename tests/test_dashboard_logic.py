"""Growth targets, chart helpers, reading tiles and the PWA shell."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

import pwa  # noqa: E402
from charts import local_frame, metric_chart, on_periods, stage_chart  # noqa: E402
from config import suggested_grow_stage  # noqa: E402
from growth_targets import (  # noqa: E402
    actual_timeline,
    climate_stage,
    hint_for,
    predicted_timeline,
    stage_change_due,
)
from readings import reading_tiles, tiles_html  # noqa: E402

PLANTED = datetime(2026, 9, 3, 17, 0)


def test_stage_schedule_includes_harvest():
    assert [suggested_grow_stage(w) for w in (1, 2, 4, 6, 7)] == [
        "seedling", "transitional", "vegetative", "vegetative", "harvest"]


def test_predicted_timeline_change_points():
    points = predicted_timeline(PLANTED, PLANTED + timedelta(days=60))
    assert [stage for _, stage in points] == ["seedling", "transitional", "vegetative", "harvest"]
    assert points[1][0] == PLANTED + timedelta(days=7)
    assert points[3][0] == PLANTED + timedelta(days=42)


def test_actual_timeline_segments_end_at_next_change():
    t0, t1, now = PLANTED, PLANTED + timedelta(days=9), PLANTED + timedelta(days=20)
    segments = actual_timeline([(t0, "seedling"), (t1, "transitional")], now)
    assert segments == [(t0, t1, "seedling"), (t1, now, "transitional")]


def test_stage_change_due_only_when_behind():
    assert stage_change_due("seedling", "transitional")
    assert stage_change_due("idle", "seedling")
    assert not stage_change_due("vegetative", "transitional")
    assert not stage_change_due("vegetative", "vegetative")


def test_hints_point_to_the_next_stage():
    assert hint_for("seedling").next_stage == "transitional"
    assert hint_for("filling").next_stage == "seedling"
    assert hint_for("vegetative").next_stage == "harvest"


def test_climate_stage_falls_back_to_plant_age():
    assert climate_stage("idle", "seedling") == "seedling"
    assert climate_stage("vegetative", "seedling") == "vegetative"
    assert climate_stage(None, None) is None


def _history():
    start = datetime(2026, 10, 1, 0, 0)
    rows = []
    for i in range(12):
        rows.append({"time": start + timedelta(minutes=10 * i), "ph": 5.6 + i * 0.01,
                     "water_pump_on": 1 if i in (3, 4) else 0, "led_on": 1})
    rows.append({"time": start + timedelta(hours=8), "ph": -9.8, "water_pump_on": 0,
                 "led_on": 0})
    return rows


def test_local_frame_and_on_periods():
    df = local_frame(_history())
    assert "t" in df and len(df) == 13
    spans = on_periods(df, "water_pump_on")
    assert len(spans) == 1
    assert spans[0][1] - spans[0][0] == timedelta(minutes=20)


def test_metric_chart_drops_sentinels_and_builds():
    df = local_frame(_history())
    chart = metric_chart(df, [("ph", "pH", "#2a78d6")], "pH", band=(5.5, 5.8), valid=(0, 14))
    spec = chart.to_dict()
    values = [row["v"] for ds in spec["datasets"].values() for row in ds if "v" in row]
    assert values and min(values) >= 0
    assert metric_chart(df, [("missing", "x", "#000")], "x") is None


def test_stage_chart_builds():
    now = PLANTED + timedelta(days=28)
    chart = stage_chart(predicted_timeline(PLANTED, now), [(PLANTED, now, "seedling")], now)
    assert chart is not None and chart.to_dict()


def test_reading_tiles_status():
    now = datetime.now(timezone.utc).isoformat()
    state = {"received_at": now, "ph_ema": 6.7, "ph_target_min": 5.8, "ph_target_max": 6.2,
             "ph_dead_low": 5.5, "ph_dead_high": 6.5,
             "tds_ppm_ema": 700, "nutrient_tds_min": 600, "nutrient_tds_max": 800,
             "ec_dead_low": 550, "ec_dead_high": 850,
             "ambient_temp_c": 22, "health": {"co2": "fault"}, "co2_ppm": 900}
    tiles = {t["key"]: t for t in reading_tiles(state, "vegetative", "vegetative")}
    assert tiles["ph"]["status"] == "high"
    assert tiles["tds"]["status"] == "ok"
    assert tiles["co2"]["status"] == "fault"
    assert tiles["humidity"]["status"] == "none"
    assert "sg-tile" in tiles_html(list(tiles.values()))


def test_pwa_head_injection_and_manifest():
    page = b"<html><head><title>x</title></head><body></body></html>"
    out = pwa.inject_head(page)
    assert b'rel="manifest"' in out and b"/sw.js" in out
    assert pwa.inject_head(out) == out  # idempotent
    assert pwa.MANIFEST["display"] == "standalone"
    for icon in pwa.MANIFEST["icons"]:
        assert (pwa.ASSETS / Path(icon["src"]).name).exists()
    assert (pwa.ASSETS / "sw.js").exists() and (pwa.ASSETS / "offline.html").exists()


def test_frame_handles_empty_history():
    assert local_frame([]).empty
    assert on_periods(pd.DataFrame(), "x") == []
