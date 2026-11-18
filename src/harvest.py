"""Harvest date from the camera and the conditions: a random-forest regression.

Target: each pot's daily relative growth rate of leaf area,
    rgr = ln(area on day d+1 / area on day d)        (per day)
learned from that day's conditions (development.daily_development: the crop's
temperature response f_temp, mean temperature, grow-light hours, the shares of
the day TDS and pH sat in the controller's limits) and the plant's size (log
leaf area: big plants grow more slowly in relative terms) and crop.

Forecast: from today's leaf area, each future day takes the forest's predicted
rgr under the recent conditions (the mean of the last RECENT_DAYS measured
days, i.e. "if the room stays as it has been") until the area reaches the
crop's harvest size. Run once with the median over the trees and once each with
their 20th and 80th percentiles, which gives the date and its range.

This is trained on the trial's own days, so it learns how this garden's plants
respond to this garden's conditions. Until a crop has been harvested the
harvest size is an estimate (HARVEST_AREA_CM2: a side-on silhouette of a
harvest-ready plant), and the date cannot be checked; the development model's
date (development.predict_stages) is the independent second opinion.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# Side-on leaf area (cm2) of a harvest-ready plant, provisional: basil about
# 15 cm tall and 12 cm wide, lettuce about 10 cm tall and 20 cm wide, each with
# about half of its silhouette in leaf. Replace with the area measured on the
# day of the first harvest.
HARVEST_AREA_CM2 = {"basil": 90.0, "lettuce": 100.0}
DEFAULT_HARVEST_AREA_CM2 = 90.0
CONDITIONS = ["f_temp", "temp_c", "light_hours", "tds_in_band", "ph_in_band"]
FEATURES = ["log_area", "is_lettuce", *CONDITIONS]
FEATURE_LABELS = {
    "log_area": "Plant size", "is_lettuce": "Crop", "f_temp": "Temperature fit for the crop",
    "temp_c": "Temperature", "light_hours": "Grow-light hours",
    "tds_in_band": "Nutrient in range", "ph_in_band": "pH in range",
}
MIN_DAYS = 5          # day-to-day growth pairs needed before a forecast is shown
RECENT_DAYS = 3
TREES = 200
HORIZON_DAYS = 120
BAND = (20, 80)       # percentiles of the trees for the date range


def daily_sizes(plants: pd.DataFrame) -> pd.DataFrame:
    """Median leaf area (cm2) per pot per day, from plant_growth.load_plants
    (frames without a scale are already blank). Columns: day, plant, area_cm2."""
    if plants.empty or "area_cm2" not in plants or plants["area_cm2"].isna().all():
        return pd.DataFrame(columns=["day", "plant", "area_cm2"])
    sized = plants.dropna(subset=["area_cm2"])
    sized = sized.assign(day=pd.to_datetime(sized["timestamp"]).dt.normalize())
    return sized.groupby(["day", "plant"], as_index=False)["area_cm2"].median()


def training_table(sizes: pd.DataFrame, conditions: dict[str, pd.DataFrame],
                   pots: list[str]) -> pd.DataFrame:
    """One row per pot per pair of consecutive days with both sizes and the first
    day's conditions measured. ``conditions`` maps crop -> daily_development table;
    ``pots`` gives each pot's crop, left to right (garden settings)."""
    rows = []
    for plant, one in sizes.groupby("plant"):
        crop = _crop(pots, plant)
        daily = conditions.get(crop)
        if daily is None:
            continue
        one = one.set_index("day")["area_cm2"].sort_index()
        for day, area in one.items():
            nxt = one.get(day + pd.Timedelta(days=1))
            if nxt is None or area <= 0 or nxt <= 0 or day not in daily.index:
                continue
            cond = daily.loc[day]
            if cond["status"] != "measured":
                continue
            rows.append({"day": day, "plant": plant, "crop": crop, "log_area": np.log(area),
                         "is_lettuce": float(crop == "lettuce"),
                         **{c: cond.get(c, np.nan) for c in CONDITIONS},
                         "rgr": np.log(nxt / area)})
    return pd.DataFrame(rows, columns=["day", "plant", "crop", *FEATURES, "rgr"])


def _crop(pots: list[str], plant) -> str:
    index = int(plant) - 1
    return str(pots[index]).lower() if 0 <= index < len(pots) and pots[index] else "basil"


def forecast(sizes: pd.DataFrame, conditions: dict[str, pd.DataFrame], pots: list[str],
             now: datetime) -> dict:
    """{"ready": False, "reason"} until MIN_DAYS growth pairs exist; otherwise
    {"ready", "pots": {plant: {...}}, "importance", "days"} where each pot has crop,
    area_cm2 (latest), target_cm2, rgr_pct (predicted growth now, % per day) and
    date / earliest / latest (None when the plant is not growing towards it)."""
    table = training_table(sizes, conditions, pots)
    if len(table) < MIN_DAYS:
        return {"ready": False, "reason": (
            f"Needs at least {MIN_DAYS} day-to-day growth measurements in centimetres with the "
            f"conditions measured; so far {len(table)}.")}
    from sklearn.ensemble import RandomForestRegressor

    fill = table[FEATURES].median()
    x = table[FEATURES].fillna(fill)
    model = RandomForestRegressor(n_estimators=TREES, min_samples_leaf=2, random_state=0,
                                  n_jobs=1).fit(x, table["rgr"])
    result = {"ready": True, "days": int(len(table)), "pots": {},
              "importance": dict(sorted(zip(FEATURES, map(float, model.feature_importances_)),
                                        key=lambda item: -item[1]))}
    latest = sizes.sort_values("day").groupby("plant").tail(1)
    for _, row in latest.iterrows():
        crop = _crop(pots, row["plant"])
        recent = _recent(conditions.get(crop))
        target = HARVEST_AREA_CM2.get(crop, DEFAULT_HARVEST_AREA_CM2)
        paths = {name: _project(model, fill, float(row["area_cm2"]), crop, recent, target, q)
                 for name, q in (("date", 50), ("earliest", BAND[1]), ("latest", BAND[0]))}
        start = row["day"].to_pydatetime()
        result["pots"][int(row["plant"])] = {
            "crop": crop, "area_cm2": float(row["area_cm2"]), "target_cm2": target,
            "rgr_pct": 100 * (np.exp(_rates(model, fill, float(row["area_cm2"]), crop, recent)[1]) - 1),
            **{name: (None if days is None else start + timedelta(days=days))
               for name, days in paths.items()},
        }
    return result


def _recent(daily: pd.DataFrame | None) -> dict:
    if daily is None:
        return {}
    measured = daily[daily["status"] == "measured"].tail(RECENT_DAYS)
    return {c: float(measured[c].mean()) if c in measured and measured[c].notna().any()
            else np.nan for c in CONDITIONS}


def _grid(model, fill, area: float, target: float, crop: str, recent: dict):
    """Per-tree predicted rgr over a grid of plant sizes from ``area`` to ``target``
    (only the size changes as a forecast runs on, so the trees are asked once).
    Returns (log-area grid, trees x grid array)."""
    sizes = np.linspace(np.log(area), np.log(max(target, area * 1.01)), 60)
    x = pd.DataFrame([{"log_area": s, "is_lettuce": float(crop == "lettuce"), **recent}
                      for s in sizes])[FEATURES].fillna(fill).to_numpy()
    return sizes, np.array([tree.predict(x) for tree in model.estimators_])


def _rates(model, fill, area: float, crop: str, recent: dict) -> tuple[np.ndarray, float]:
    """Per-tree predicted rgr for one plant state, and their median."""
    _, per_tree = _grid(model, fill, area, area * 1.01, crop, recent)
    return per_tree[:, 0], float(np.median(per_tree[:, 0]))


def _project(model, fill, area: float, crop: str, recent: dict, target: float,
             percentile: float) -> float | None:
    """Days until ``area`` reaches ``target`` growing at the trees' ``percentile``
    rate each day (re-read as the plant grows); None if it never does."""
    if area >= target:
        return 0.0
    sizes, per_tree = _grid(model, fill, area, target, crop, recent)
    curve = np.percentile(per_tree, percentile, axis=0)
    log_area = np.log(area)
    for day in range(1, HORIZON_DAYS + 1):
        rate = float(np.interp(log_area, sizes, curve))
        if rate <= 0:
            return None
        log_area += rate
        if log_area >= np.log(target):
            return float(day)
    return None
