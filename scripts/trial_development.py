#!/usr/bin/env python3
"""Why is the trial taking so long? Condition-weighted development for each crop.

Reads the garden's readings (the dashboard SQLite, or telemetry CSV exports from
the firmware repo), counts each day since planting for what it was worth to each
crop (src/development.py), and prints the days lost by cause, the unmeasured days
and the predicted stage dates. With --out it also writes the per-day table.

    python scripts/trial_development.py --sqlite data/sensor_readings.sqlite
    python scripts/trial_development.py --csv a.csv.gz b.csv.gz --planted 2026-09-03T17:00+10:00 \\
        --break 2026-09-03T17:00 2026-09-25T12:00 "windowsill, before transplant"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import development  # noqa: E402


def rows_from_csv(paths: list[str]) -> list[dict]:
    """Hourly rows (as sqlite_store.load_hourly_conditions) from telemetry exports."""
    frames = []
    for path in paths:
        d = pd.read_csv(path, low_memory=False)
        time = d["time_utc"] if "time_utc" in d else d["timestamp"]
        d["when"] = pd.to_datetime(time, utc=True, errors="coerce", format="ISO8601")
        frames.append(d.dropna(subset=["when"]))
    d = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["when"])
    d["hour"] = d["when"].dt.strftime("%Y-%m-%dT%H")

    def col(name):
        return pd.to_numeric(d[name], errors="coerce") if name in d else pd.Series(float("nan"), index=d.index)

    air, water, light = col("ambient_temp_c"), col("water_temp_c"), col("light_adc")
    ph, tds = col("ph"), col("tds_ppm")
    d = d.assign(
        air=air.where(air.between(-5, 50)), water=water.where(water.between(0, 40)),
        hum=col("ambient_humidity_pct").where(col("ambient_humidity_pct").between(1, 100)),
        led=col("led_on"), seen=(light > 200).astype(float).where(light.notna()), adc=light,
        tds_ok=((tds >= col("ec_dead_low")) & (tds <= col("ec_dead_high"))).astype(float).where(tds.notna()),
        ph_ok=((ph >= col("ph_dead_low")) & (ph <= col("ph_dead_high"))).astype(float).where(ph.notna()))
    g = d.groupby("hour")
    out = pd.DataFrame({
        "readings": g.size(), "air_temp_c": g["air"].mean(), "water_temp_c": g["water"].mean(),
        "humidity_pct": g["hum"].mean(), "led_on": g["led"].mean(), "light_seen": g["seen"].mean(),
        "light_adc_max": g["adc"].max(), "tds_in_band": g["tds_ok"].mean(),
        "ph_in_band": g["ph_ok"].mean()}).reset_index()
    return out.to_dict("records")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sqlite", help="dashboard data/sensor_readings.sqlite")
    source.add_argument("--csv", nargs="+", help="telemetry CSV exports")
    parser.add_argument("--planted", help="planting time, ISO (default: garden settings)")
    parser.add_argument("--now", help="end of the analysis, ISO (default: now)")
    parser.add_argument("--break", dest="breaks", nargs=3, action="append", default=[],
                        metavar=("START", "END", "REASON"), help="plants out of the system")
    parser.add_argument("--crops", nargs="+", default=["basil", "lettuce"])
    parser.add_argument("--out", help="write the per-day table for each crop to this CSV")
    args = parser.parse_args()

    import garden_settings

    planted = development._naive_local(args.planted) if args.planted else \
        development._naive_local(garden_settings.planted_at().isoformat())
    now = development._naive_local(args.now) if args.now else datetime.now()
    breaks = development.parse_breaks(
        [{"start": s, "end": e, "reason": r} for s, e, r in args.breaks]
        or garden_settings.load().get("breaks"))
    if args.sqlite:
        import sqlite_store
        rows = sqlite_store.load_hourly_conditions(args.sqlite, planted.astimezone().isoformat())
    else:
        rows = rows_from_csv(args.csv)
    hourly = development.hourly_conditions(rows)
    print(f"Planted {planted:%d %b %Y %H:%M}; {len(hourly)} hours of readings; "
          f"light from {hourly['light_source'].value_counts().to_dict() if len(hourly) else {}}")
    tables = []
    for name in args.crops:
        daily = development.daily_development(hourly, name, planted, now, breaks)
        summary = development.summarise(daily)
        print(f"\n== {name}: {development.explain(summary)}")
        print(f"   mean light {summary['light_hours_mean']:.1f} h/day on measured days; "
              f"recent rate {summary['recent_rate']:.2f} ideal days per day")
        for item in development.predict_stages(daily, summary, now):
            print(f"   {item['stage']:<13} day {item['ideal_day']:>2}: {item['due']:%d %b}"
                  f"  (range {item['earliest']:%d %b} - {item['latest']:%d %b})")
        tables.append(daily.assign(crop=name))
    if args.out:
        pd.concat(tables).to_csv(args.out)
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
