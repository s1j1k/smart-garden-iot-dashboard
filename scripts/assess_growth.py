#!/usr/bin/env python3
"""Assess the camera growth record, the stage classifier and the harvest forecast.

Prints, for each pot: daily median leaf area / height / width (cm) on days with
enough scaled photos, the fitted relative growth rate with a 95 % interval, and
the doubling time; then whether the stage classifier can be trained, and the
harvest forecast with its feature importances. Use after
scripts/remeasure_plants.py so the whole record is in centimetres.

    python scripts/assess_growth.py --sqlite data/sensor_readings.sqlite \\
        --break 2026-09-03T17:00 2026-09-25T12:00 "windowsill before transplant"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import development  # noqa: E402
import garden_settings  # noqa: E402
import harvest  # noqa: E402
import plant_growth  # noqa: E402
import sqlite_store  # noqa: E402

MIN_PHOTOS = 20


def growth_table(plants: pd.DataFrame) -> pd.DataFrame:
    sized = plants.dropna(subset=["area_cm2"]).copy()
    sized["day"] = pd.to_datetime(sized["timestamp"]).dt.normalize()
    return sized.groupby(["plant", "day"]).agg(
        photos=("area_cm2", "size"), area_cm2=("area_cm2", "median"),
        height_cm=("height_cm", "median"), width_cm=("width_cm", "median"),
        leaves=("leaf_count", "median")).reset_index()


def fit_rate(days: pd.Series, area: pd.Series) -> tuple[float, float]:
    """Relative growth rate (per day) and its 95 % half-width, from a
    least-squares line through ln(area) against days."""
    x = (days - days.iloc[0]).dt.total_seconds().to_numpy() / 86400
    y = np.log(area.to_numpy())
    if len(x) < 3:
        return float("nan"), float("nan")
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (intercept + slope * x)
    se = np.sqrt((resid ** 2).sum() / (len(x) - 2) / ((x - x.mean()) ** 2).sum())
    return float(slope), float(1.96 * se)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", default=str(ROOT / "data" / "sensor_readings.sqlite"))
    parser.add_argument("--planted", help="ISO planting time (default: garden settings)")
    parser.add_argument("--break", dest="breaks", nargs=3, action="append", default=[],
                        metavar=("START", "END", "REASON"))
    args = parser.parse_args()

    settings = garden_settings.load()
    planted = development._naive_local(args.planted or garden_settings.planted_at().isoformat())
    pots = [str(p).lower() for p in (settings.get("pots") or settings.get("plants") or [])]
    breaks = development.parse_breaks([{"start": s, "end": e, "reason": r} for s, e, r in args.breaks]
                                      or settings.get("breaks"))

    plants = plant_growth.load_plants()
    if plant_growth.size_features(plants) is not plant_growth.FEATURES:
        print("plants.jsonl has no cm measurements: run scripts/remeasure_plants.py first.")
        return
    table = growth_table(plants)
    print(f"Photos with a scale: {int(plants['area_cm2'].notna().sum())} plant measurements, "
          f"{plants['timestamp'].nunique()} frames.\n")
    for plant, one in table.groupby("plant"):
        crop = pots[int(plant) - 1] if int(plant) - 1 < len(pots) else "?"
        print(f"== Pot {plant} ({crop})")
        print(one.drop(columns="plant").round(2).to_string(index=False))
        solid = one[one["photos"] >= MIN_PHOTOS]
        rate, half = fit_rate(solid["day"], solid["area_cm2"])
        if rate == rate:
            print(f"   leaf area: {solid['area_cm2'].iloc[0]:.2f} -> {solid['area_cm2'].iloc[-1]:.2f} cm2 "
                  f"({solid['day'].iloc[0]:%d %b} to {solid['day'].iloc[-1]:%d %b}, {len(solid)} days "
                  f"with {MIN_PHOTOS}+ photos); RGR {rate:.3f} +- {half:.3f} /day, doubling "
                  f"{np.log(2) / rate:.1f} days" if rate > 0 else f"   RGR {rate:.3f} /day (not growing)")
        print()

    rows = sqlite_store.load_hourly_conditions(args.sqlite, planted.astimezone().isoformat())
    hourly = development.hourly_conditions(rows)
    conditions = {crop: development.daily_development(hourly, crop, planted, datetime.now(), breaks,
                                                      settings.get("grow_light_ppfd"))
                  for crop in set(pots)}

    changes = sqlite_store.load_stage_changes(args.sqlite)
    stages = sorted({s for _, s in changes if s in plant_growth.GROWTH_STAGES})
    hourly_plants = plant_growth.hourly_features(plants)
    estimate = plant_growth.estimate_stage(hourly_plants, changes, planted)
    print(f"== Stage classifier: stages the controller ran: {', '.join(stages) or 'none'}")
    print("   " + (estimate.get("reason") or
                   f"suggests {estimate['prediction']} ({estimate['confidence']:.0%} of trees); "
                   f"accuracy on unseen days {estimate['accuracy']:.0%} vs age schedule "
                   f"{estimate.get('baseline_accuracy', float('nan')):.0%}"))

    result = harvest.forecast(harvest.daily_sizes(plants), conditions, pots, datetime.now())
    print("\n== Harvest forecast (random forest on daily growth)")
    if not result["ready"]:
        print("   " + result["reason"])
        return
    print(f"   trained on {result['days']} day-to-day growth pairs")
    for number, pot in result["pots"].items():
        dates = [pot[k] for k in ("date", "earliest", "latest")]
        when = "not growing towards it" if dates[0] is None else (
            f"{dates[0]:%d %b} (range {dates[1]:%d %b} - {dates[2]:%d %b})" if None not in dates
            else f"{dates[0]:%d %b}")
        print(f"   pot {number} ({pot['crop']}): {pot['area_cm2']:.1f} of {pot['target_cm2']:.0f} cm2, "
              f"growing {pot['rgr_pct']:.1f} %/day -> harvest {when}")
    print("   importance: " + ", ".join(f"{harvest.FEATURE_LABELS[k]} {v:.0%}"
                                        for k, v in result["importance"].items()))


if __name__ == "__main__":
    main()
