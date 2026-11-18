"""Condition-weighted development: crop temperature response, light, gaps, breaks."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import development as dv  # noqa: E402

PLANTED = datetime(2026, 10, 10)   # clear of the 4 Oct daylight-saving change


def _rows(days: int, air: float = 20.0, light_hours: int = 16, start: datetime = PLANTED,
          skip_days: tuple = (), ldr_dead_days: tuple = (), water: float = 21.0) -> list[dict]:
    """Hourly rows as sqlite_store.load_hourly_conditions returns them (UTC hour)."""
    rows = []
    for h in range(days * 24):
        local = start + timedelta(hours=h)
        day = h // 24
        if day in skip_days:
            continue
        on = (local.hour < light_hours)
        dead = day in ldr_dead_days
        rows.append({
            "hour": local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H"), "readings": 360,
            "air_temp_c": air, "water_temp_c": water, "humidity_pct": 60, "led_on": float(on),
            "light_seen": 0.0 if dead else float(on), "light_adc_max": 0 if dead else 2700,
            "tds_in_band": 1.0, "ph_in_band": 1.0})
    return rows


def test_temperature_factor_shapes():
    lettuce = dv.crop("lettuce")
    f = dv.temperature_factor([2, 10, 18, 26, 31, np.nan], lettuce)
    assert f[0] == 0 and 0 < f[1] < 1 and f[2] == 1 and 0 < f[3] < 1 and f[4] == 0
    assert np.isnan(f[5])
    assert dv.temperature_factor([25], dv.crop("basil"))[0] == 1      # basil likes it warm


def test_ideal_conditions_follow_the_calendar():
    hourly = dv.hourly_conditions(_rows(10, air=18.0))
    daily = dv.daily_development(hourly, "lettuce", PLANTED, PLANTED + timedelta(days=9, hours=23))
    assert (daily["status"] == "measured").all()
    assert np.allclose(daily["rate"], 1.0)
    summary = dv.summarise(daily)
    assert abs(summary["ideal_days"] - 10) < 1e-6
    stages = {s["stage"]: s for s in dv.predict_stages(daily, summary, PLANTED + timedelta(days=10))}
    assert stages["transitional"]["due"].date() == (PLANTED + timedelta(days=6)).date()


def test_warm_room_slows_lettuce_not_basil_and_says_why():
    hourly = dv.hourly_conditions(_rows(10, air=26.0))
    now = PLANTED + timedelta(days=9, hours=23)
    lettuce = dv.summarise(dv.daily_development(hourly, "lettuce", PLANTED, now))
    basil = dv.summarise(dv.daily_development(hourly, "basil", PLANTED, now))
    assert basil["ideal_days"] == 10
    assert 4 < lettuce["ideal_days"] < 6                   # 26 C is half-way to lettuce's 30 C limit
    assert lettuce["lost"]["too warm"] > 4 and lettuce["lost"]["too cold"] == 0
    assert "too warm" in dv.explain(lettuce)


def test_short_light_is_counted():
    hourly = dv.hourly_conditions(_rows(4, air=18.0, light_hours=8))
    summary = dv.summarise(dv.daily_development(hourly, "lettuce", PLANTED,
                                                PLANTED + timedelta(days=3, hours=23)))
    assert abs(summary["lost"]["short light"] - 2.0) < 1e-6   # 8 of 16 h, four days
    assert abs(summary["light_hours_mean"] - 8) < 1e-6


def test_gaps_and_breaks_widen_the_range_but_do_not_count_as_lost():
    breaks = dv.parse_breaks([{"start": "2026-10-12T00:00", "end": "2026-10-14T00:00",
                               "reason": "housing rebuild"}, {"start": "bad"}])
    assert len(breaks) == 1 and breaks[0].reason == "housing rebuild"
    hourly = dv.hourly_conditions(_rows(10, air=18.0, skip_days=(6, 7)))
    daily = dv.daily_development(hourly, "lettuce", PLANTED, PLANTED + timedelta(days=9, hours=23),
                                 breaks)
    assert list(daily["status"].iloc[2:8]) == ["break", "break", "measured", "measured",
                                               "no data", "no data"]
    summary = dv.summarise(daily)
    assert summary["unmeasured"] == {"no data": 2, "out of the system": 2}
    assert sum(summary["lost"].values()) == 0
    assert summary["ideal_days_low"] == 6 and summary["ideal_days_high"] == 10
    harvest = [s for s in dv.predict_stages(daily, summary, PLANTED + timedelta(days=10))
               if s["stage"] == "harvest"][0]
    assert harvest["earliest"] < harvest["latest"]
    assert harvest["latest"] - harvest["earliest"] == timedelta(days=4)


def test_dead_ldr_falls_back_to_the_commanded_light():
    hourly = dv.hourly_conditions(_rows(3, air=18.0, ldr_dead_days=(1,)))
    by_day = hourly.groupby(hourly.index.normalize())["light_source"].agg(lambda s: set(s))
    assert by_day.iloc[1] == {"commanded"} and by_day.iloc[0] == {"LDR"}
    daily = dv.daily_development(hourly, "lettuce", PLANTED, PLANTED + timedelta(days=2, hours=23))
    assert np.allclose(daily["light_hours"], 16)


def test_air_fault_uses_water_temperature():
    rows = _rows(2, air=-127.0, water=18.0)
    hourly = dv.hourly_conditions(rows)
    assert hourly["air_temp_c"].isna().all()
    daily = dv.daily_development(hourly, "lettuce", PLANTED, PLANTED + timedelta(days=1, hours=23))
    assert np.allclose(daily["temp_c"], 18.0) and np.allclose(daily["rate"], 1.0)


def test_nothing_measured_is_said_plainly():
    daily = dv.daily_development(dv.hourly_conditions([]), "basil", PLANTED, PLANTED + timedelta(days=3))
    summary = dv.summarise(daily)
    assert summary["measured_days"] == 0
    assert "follow the calendar" in dv.explain(summary)
    assert isinstance(pd.Timestamp(dv.predict_stages(daily, summary, PLANTED)[-1]["due"]), pd.Timestamp)


def test_dli_limits_growth_once_ppfd_is_known():
    assert abs(dv.ppfd_needed("lettuce") - 295.1) < 0.5
    hourly = dv.hourly_conditions(_rows(4, air=18.0))
    now = PLANTED + timedelta(days=3, hours=23)
    dim = dv.daily_development(hourly, "lettuce", PLANTED, now, ppfd=100.0)
    assert np.allclose(dim["dli"], 5.76)                 # 100 x 16 h x 3600 / 1e6
    assert np.allclose(dim["f_light"], 5.76 / 17)
    bright = dv.daily_development(hourly, "lettuce", PLANTED, now, ppfd=400.0)
    assert np.allclose(bright["f_light"], 1.0)
    assert np.isnan(dv.daily_development(hourly, "lettuce", PLANTED, now)["dli"]).all()
