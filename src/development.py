"""Condition-weighted plant development: when each stage is really due, and why
it is later than the calendar says.

The age schedule (config.suggested_grow_stage) assumes every day is an ideal
growing day. This module counts each day for what it was worth instead
(photothermal time, the idea behind growing-degree days):

    rate(day) = f_light x f_temp          0 = no development, 1 = an ideal day
    f_light   = the day's light integral / the crop's DLI_TARGET, at most 1, when
                the grow light's PPFD at the canopy has been measured (garden
                setting grow_light_ppfd); until then grow-light hours /
                LIGHT_HOURS_TARGET, i.e. the light is assumed bright enough
    f_temp    = mean over the day's hours of the crop's temperature response:
                0 below t_base, rising to 1 across t_base..t_opt_low, 1 up to
                t_opt_high, falling to 0 at t_max

Each stage starts when the summed rate reaches the day the schedule gives it
(STAGE_DAYS), so a cold or dark week pushes every later stage back. Missing
days are not filled with numbers. A day with too few readings (logger or Pi
down) or inside a declared break (plants out of the tank while the housing was
rebuilt) counts between 0 and 1: the central estimate gives it the plants'
median measured rate, and the bounds (0 and 1) become the earliest and latest
dates. Days lost are split by cause: short light, cold, heat, no data, breaks.

Temperature is the air temperature; hours without it (the DHT11 failed on
16 Oct) use the water temperature, which in deep water culture is the root-zone
temperature and tracks the room.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# Cardinal temperatures (deg C) for development. Provisional, from the usual
# values in growing-degree-day calendars and greenhouse guides: lettuce is a
# cool-season crop (base about 4 deg C, best below about 22, stressed and
# bolting towards 30); basil is warm-season (little growth below about 10 deg C,
# best about 22-28). Check against crop literature before relying on them.
CROPS = {
    "basil": {"t_base": 10.0, "t_opt_low": 22.0, "t_opt_high": 28.0, "t_max": 35.0},
    "lettuce": {"t_base": 4.0, "t_opt_low": 16.0, "t_opt_high": 22.0, "t_max": 30.0},
}
DEFAULT_CROP = {"t_base": 6.0, "t_opt_low": 18.0, "t_opt_high": 25.0, "t_max": 32.0}

LIGHT_HOURS_TARGET = 16.0   # the grow light's photoperiod (firmware grow_light.h)
# Daily light integral (mol m-2 d-1) for full growth, the usual horticultural
# targets: lettuce about 17 (controlled-environment lettuce guides), basil about
# 12 or more. DLI = PPFD (umol m-2 s-1) x light hours x 3600 / 1e6, so at a 16 h
# photoperiod lettuce needs about 295 umol m-2 s-1 at the canopy and basil 210.
DLI_TARGET = {"basil": 12.0, "lettuce": 17.0}
DEFAULT_DLI_TARGET = 14.0
MIN_HOURS = 12              # a day needs readings in this many hours to be measured
RECENT_DAYS = 7             # the rate used to project forward
LDR_ALIVE_ADC = 50          # an LDR whose daily maximum is no higher than this is dead
AIR_RANGE = (-5.0, 50.0)    # readings outside are sensor faults (-127 = DHT11 lost)
WATER_RANGE = (0.0, 40.0)


def stage_days() -> dict[str, int]:
    """Ideal-day count at which each growth stage starts, from the age schedule."""
    from config import suggested_grow_stage

    starts: dict[str, int] = {}
    for week in range(1, 30):
        stage = suggested_grow_stage(week)
        if stage and stage not in starts:
            starts[stage] = 7 * (week - 1)
    return starts


def crop(name: str) -> dict:
    return CROPS.get(str(name).lower(), DEFAULT_CROP)


def temperature_factor(temp, params: dict) -> np.ndarray:
    """0..1 development rate at ``temp`` (deg C) for one crop; NaN stays NaN."""
    t = np.asarray(temp, dtype=float)
    base, low, high, top = (params[k] for k in ("t_base", "t_opt_low", "t_opt_high", "t_max"))
    rising = (t - base) / (low - base)
    falling = (top - t) / (top - high)
    factor = np.where(t < low, rising, np.where(t > high, falling, 1.0))
    return np.where(np.isnan(t), np.nan, np.clip(factor, 0.0, 1.0))


def hourly_conditions(rows: list[dict]) -> pd.DataFrame:
    """Hourly conditions from sqlite_store.load_hourly_conditions rows (UTC hour
    ``hour`` "YYYY-MM-DDTHH"), on naive local time. Columns: readings, air_temp_c,
    water_temp_c, humidity_pct, light_on (share of the hour the grow light was on)
    and light_source ("LDR", "commanded" or "none"), tds_in_band and ph_in_band
    (shares inside the controller's limits), plus the raw light inputs."""
    columns = ["readings", "air_temp_c", "water_temp_c", "humidity_pct", "led_on",
               "light_seen", "light_adc_max", "tds_in_band", "ph_in_band"]
    frame = pd.DataFrame(rows or [], columns=["hour", *columns])
    if frame.empty:
        return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], name="hour"))
    when = pd.to_datetime(frame["hour"].astype(str) + ":00Z", utc=True, errors="coerce",
                          format="ISO8601")
    frame["hour"] = pd.to_datetime(
        [t.to_pydatetime().astimezone().replace(tzinfo=None) if pd.notna(t) else pd.NaT
         for t in when])
    frame[columns] = frame[columns].apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(subset=["hour"]).groupby("hour")[columns].mean()
    for column, (low, high) in (("air_temp_c", AIR_RANGE), ("water_temp_c", WATER_RANGE)):
        frame[column] = frame[column].where(frame[column].between(low, high))
    frame = frame.sort_index()
    frame["light_on"], frame["light_source"] = _light(frame)
    return frame


def _light(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Share of each hour the grow light was on, and where that came from.

    The LDR shows what really happened (a light that was commanded on but had no
    power reads dark: about 10 h on 8 Oct, 9 h on 14 Oct). It reads 0 in the dark
    and 300 to 3000 under the light (lower after the light and the boards moved on
    8-9 Oct), so over 200 counts as lit. It is trusted on days when it rose above
    LDR_ALIVE_ADC; a day when it never did is a dead sensor (it read at most 18 on
    15 Oct with the light commanded on), and those hours use the commanded state.
    """
    day = frame.index.normalize()
    alive = frame["light_adc_max"].groupby(day).transform("max") > LDR_ALIVE_ADC
    seen = frame["light_seen"].where(alive & frame["light_seen"].notna())
    light = seen.fillna(frame["led_on"])
    source = pd.Series(np.where(seen.notna(), "LDR", np.where(frame["led_on"].notna(),
                                                             "commanded", "none")),
                       index=frame.index)
    return light, source


@dataclass(frozen=True)
class Break:
    start: datetime
    end: datetime
    reason: str


def parse_breaks(items) -> list[Break]:
    """Declared breaks from garden settings: [{"start", "end", "reason"}] (ISO
    times, naive = local). Bad entries are skipped."""
    out = []
    for item in items or []:
        try:
            start = _naive_local(item["start"])
            end = _naive_local(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            out.append(Break(start, end, str(item.get("reason") or "plants out of the system")))
    return sorted(out, key=lambda b: b.start)


def _naive_local(value) -> datetime:
    when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


def dli(ppfd: float, light_hours) -> np.ndarray:
    """Daily light integral (mol m-2 d-1) from PPFD at the canopy and hours on."""
    return np.asarray(light_hours, dtype=float) * float(ppfd) * 3600 / 1e6


def ppfd_needed(crop_name: str, hours: float = LIGHT_HOURS_TARGET) -> float:
    """PPFD (umol m-2 s-1) at the canopy that meets the crop's DLI in ``hours``."""
    return DLI_TARGET.get(str(crop_name).lower(), DEFAULT_DLI_TARGET) * 1e6 / (hours * 3600)


def daily_development(hourly: pd.DataFrame, crop_name: str, planted: datetime,
                      now: datetime, breaks: list[Break] | None = None,
                      ppfd: float | None = None) -> pd.DataFrame:
    """One row per day from planting to ``now`` (naive local).

    Columns: status ("measured", "no data" or "break"), hours (with readings),
    light_hours, temp_c, f_light, f_temp, rate (NaN unless measured), the
    day's shortfall from an ideal day split into lost_light, lost_cold, lost_heat,
    and the shares of the day TDS and pH sat inside the controller's limits.
    """
    params = crop(crop_name)
    days = pd.date_range(pd.Timestamp(planted).normalize(), pd.Timestamp(now).normalize(), freq="D")
    out = pd.DataFrame(index=days)
    out.index.name = "day"
    if hourly is None or hourly.empty:
        hourly = pd.DataFrame(columns=["readings", "air_temp_c", "water_temp_c", "light_on"],
                              index=pd.DatetimeIndex([]))
    temp = hourly["air_temp_c"].fillna(hourly["water_temp_c"])
    factor = pd.Series(temperature_factor(temp, params), index=hourly.index)
    cold = pd.Series(np.where(temp < params["t_opt_low"], 1 - factor, 0.0), index=hourly.index)
    heat = pd.Series(np.where(temp > params["t_opt_high"], 1 - factor, 0.0), index=hourly.index)
    has = hourly["readings"].fillna(0) > 0
    by_day = pd.DataFrame({
        "hours": has.astype(int), "light": hourly["light_on"].where(has),
        "temp": temp.where(has), "f_temp": factor.where(has),
        "cold": cold.where(has & factor.notna()), "heat": heat.where(has & factor.notna()),
        "tds_in_band": hourly.get("tds_in_band", pd.Series(np.nan, index=hourly.index)).where(has),
        "ph_in_band": hourly.get("ph_in_band", pd.Series(np.nan, index=hourly.index)).where(has),
    }).groupby(pd.Grouper(freq="D")).agg(
        hours=("hours", "sum"), light=("light", "mean"), temp=("temp", "mean"),
        f_temp=("f_temp", "mean"), cold=("cold", "mean"), heat=("heat", "mean"),
        tds_in_band=("tds_in_band", "mean"), ph_in_band=("ph_in_band", "mean"))
    out = out.join(by_day, how="left")
    out["hours"] = out["hours"].fillna(0).astype(int)
    out["light_hours"] = 24 * out["light"]
    if ppfd:
        out["dli"] = dli(ppfd, out["light_hours"])
        target = DLI_TARGET.get(str(crop_name).lower(), DEFAULT_DLI_TARGET)
        out["f_light"] = (out["dli"] / target).clip(upper=1.0)
    else:
        out["dli"] = np.nan
        out["f_light"] = (out["light_hours"] / LIGHT_HOURS_TARGET).clip(upper=1.0)
    out = out.rename(columns={"temp": "temp_c"})
    measured = (out["hours"] >= MIN_HOURS) & out["f_light"].notna() & out["f_temp"].notna()
    out["status"] = np.where(measured, "measured", "no data")
    for item in breaks or []:
        inside = (out.index + pd.Timedelta(hours=12) >= item.start) & (out.index + pd.Timedelta(hours=12) < item.end)
        out.loc[inside, "status"] = "break"
    ok = out["status"] == "measured"
    out["rate"] = (out["f_light"] * out["f_temp"]).where(ok)
    # 1 - fL*fT = (1 - fL) + fL*(1 - fT): light first, then temperature, split
    # into cold and heat by which side of the optimum the lost hours were on.
    out["lost_light"] = (1 - out["f_light"]).where(ok)
    temp_loss = (out["f_light"] * (1 - out["f_temp"])).where(ok)
    share = (out["cold"] / (out["cold"] + out["heat"])).where(out["cold"] + out["heat"] > 0, 0.5)
    out["lost_cold"] = temp_loss * share
    out["lost_heat"] = temp_loss * (1 - share)
    return out[["status", "hours", "light_hours", "dli", "temp_c", "f_light", "f_temp", "rate",
                "lost_light", "lost_cold", "lost_heat", "tds_in_band", "ph_in_band"]]


def summarise(daily: pd.DataFrame) -> dict:
    """Ideal days so far (central, lowest, highest) and the days lost by cause."""
    measured = daily["status"] == "measured"
    rates = daily.loc[measured, "rate"]
    typical = float(rates.median()) if len(rates) else 1.0
    unknown = int((~measured).sum())
    done = float(rates.sum())
    recent = rates.tail(RECENT_DAYS)
    return {
        "calendar_days": int(len(daily)),
        "measured_days": int(measured.sum()),
        "ideal_days": done + unknown * typical,
        "ideal_days_low": done,
        "ideal_days_high": done + unknown,
        "typical_rate": typical,
        "recent_rate": float(recent.mean()) if len(recent) else typical,
        "lost": {
            "short light": float(daily["lost_light"].sum()),
            "too cold": float(daily["lost_cold"].sum()),
            "too warm": float(daily["lost_heat"].sum()),
        },
        "unmeasured": {
            "no data": int((daily["status"] == "no data").sum()),
            "out of the system": int((daily["status"] == "break").sum()),
        },
        "light_hours_mean": float(daily.loc[measured, "light_hours"].mean()) if measured.any() else float("nan"),
        "dli_mean": float(daily.loc[measured, "dli"].mean()) if measured.any() else float("nan"),
    }


def predict_stages(daily: pd.DataFrame, summary: dict, now: datetime) -> list[dict]:
    """When each growth stage is (or was) due, with an earliest-latest range.

    Past stages are dated where the central count crossed their threshold;
    future ones are projected at the recent rate (central) and bounded by the
    lowest and highest counts of the unmeasured days.
    """
    measured = daily["status"] == "measured"
    per_day = daily["rate"].where(measured, summary["typical_rate"])
    central = per_day.cumsum()
    low = daily["rate"].where(measured, 0.0).cumsum()
    high = daily["rate"].where(measured, 1.0).cumsum()
    rate = max(summary["recent_rate"], 0.05)   # a stalled week would put harvest at infinity
    out = []
    for stage, threshold in stage_days().items():
        out.append({
            "stage": stage, "ideal_day": threshold,
            "due": _crossing(central, threshold, summary["ideal_days"], rate, now),
            "earliest": _crossing(high, threshold, summary["ideal_days_high"], rate, now),
            "latest": _crossing(low, threshold, summary["ideal_days_low"], rate, now),
        })
    return out


def _crossing(path: pd.Series, threshold: float, total: float, rate: float, now: datetime):
    reached = path[path >= threshold]
    if threshold == 0 and len(path):
        return path.index[0].to_pydatetime()
    if len(reached):
        return reached.index[0].to_pydatetime()
    return now + timedelta(days=(threshold - total) / rate)


def explain(summary: dict) -> str:
    """Plain sentences: how much the measured days were worth, what cost the
    rest, and how many days could not be measured."""
    measured = summary["measured_days"]
    worth = summary["ideal_days_low"]
    if measured == 0:
        return (f"None of the {summary['calendar_days']} days since planting has enough "
                "readings to judge, so the stages follow the calendar.")
    lost = {k: v for k, v in summary["lost"].items() if v >= 0.1}
    text = (f"The {measured} measured days were worth {worth:.1f} ideal growing days"
            + (": " + ", ".join(f"{v:.1f} lost to {k}" for k, v in
                                 sorted(lost.items(), key=lambda i: -i[1])) if lost else "")
            + ".")
    gaps = {k: v for k, v in summary["unmeasured"].items() if v}
    if gaps:
        text += (" " + " and ".join(f"{v} days {k}" for k, v in gaps.items())
                 + " are counted at the plants' typical rate, which sets the date range.")
    return text
