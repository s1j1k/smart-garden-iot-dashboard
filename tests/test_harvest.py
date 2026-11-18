"""Harvest-date regression: growth rate from conditions, projected to harvest size."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import harvest  # noqa: E402

START = pd.Timestamp("2026-10-10")


def _conditions(days: int, warm_from: int | None = None) -> pd.DataFrame:
    """daily_development-shaped table: ideal, or a warm spell (f_temp 0.5) from a day."""
    index = pd.date_range(START, periods=days, freq="D")
    f_temp = np.where(np.arange(days) >= (warm_from if warm_from is not None else days), 0.5, 1.0)
    return pd.DataFrame({"status": "measured", "f_temp": f_temp, "temp_c": 20 + 6 * (f_temp < 1),
                         "light_hours": 16.0, "tds_in_band": 1.0, "ph_in_band": 1.0}, index=index)


def _sizes(conditions: pd.DataFrame, area0: float = 2.0, base_rgr: float = 0.2) -> pd.DataFrame:
    """Two pots whose leaf area grows by base_rgr x f_temp per day."""
    rows = []
    for plant in (1, 2):
        area = area0
        for day, row in conditions.iterrows():
            rows.append({"day": day, "plant": plant, "area_cm2": area})
            area *= np.exp(base_rgr * row["f_temp"])
    return pd.DataFrame(rows)


def test_daily_sizes_use_only_scaled_frames():
    plants = pd.DataFrame({
        "timestamp": [START + timedelta(hours=h) for h in (1, 2, 3, 25)],
        "plant": [1, 1, 1, 1], "area_cm2": [2.0, np.nan, 4.0, 5.0]})
    sizes = harvest.daily_sizes(plants)
    assert list(sizes["area_cm2"]) == [3.0, 5.0]
    assert harvest.daily_sizes(pd.DataFrame({"timestamp": [], "plant": []})).empty


def test_training_pairs_skip_unmeasured_days():
    cond = _conditions(6)
    cond.loc[cond.index[2], "status"] = "no data"
    table = harvest.training_table(_sizes(cond), {"basil": cond, "lettuce": cond}, ["basil", "lettuce"])
    assert len(table) == 2 * 4                        # 5 pairs per pot, one day unmeasured
    assert set(table["crop"]) == {"basil", "lettuce"}
    assert np.allclose(table["rgr"], 0.2)


def test_not_ready_without_enough_days():
    cond = _conditions(3)
    out = harvest.forecast(_sizes(cond), {"basil": cond}, ["basil"], datetime(2026, 10, 13))
    assert not out["ready"] and "5" in out["reason"]


def test_forecast_reaches_harvest_size_and_warmth_delays_it():
    pytest.importorskip("sklearn")
    cool = _conditions(14)
    out = harvest.forecast(_sizes(cool), {"basil": cool, "lettuce": cool}, ["basil", "lettuce"],
                           datetime(2026, 10, 24))
    assert out["ready"]
    pot = out["pots"][1]
    assert pot["crop"] == "basil" and pot["target_cm2"] == harvest.HARVEST_AREA_CM2["basil"]
    assert abs(pot["rgr_pct"] - 100 * (np.exp(0.2) - 1)) < 3
    # 2 cm2 x e^(0.2 x 13) = 27 cm2 today; to 90 cm2 at 0.2/day is ~6 more days.
    days = (pot["date"] - (START + timedelta(days=13)).to_pydatetime()).days
    assert 5 <= days <= 7
    assert pot["earliest"] <= pot["date"] <= pot["latest"]

    warm = _conditions(14, warm_from=7)               # the last week at half speed
    later = harvest.forecast(_sizes(warm), {"basil": warm, "lettuce": warm}, ["basil", "lettuce"],
                             datetime(2026, 10, 24))
    assert later["pots"][1]["date"] > pot["date"]
    assert set(later["importance"]) == set(harvest.FEATURES)


def test_a_plant_that_stopped_growing_gets_no_date():
    pytest.importorskip("sklearn")
    cond = _conditions(10)
    sizes = _sizes(cond, base_rgr=0.0)
    out = harvest.forecast(sizes, {"basil": cond}, ["basil"], datetime(2026, 10, 20))
    assert out["ready"] and out["pots"][1]["date"] is None
