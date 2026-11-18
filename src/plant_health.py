"""Plant health from the timelapse: is a plant stretching, curling, paling or stalled,
and which readings could explain it.

Reads ``data/camera/plants.jsonl`` (camera_capture.py, one line per photo) and the
latest sensor state; pandas only, so the dashboard does not need OpenCV.

Each check compares a plant with its own earlier photos, so it works for any species and for a camera that has moved a little. Medians over a few
hours of photos keep one bad frame from raising a finding.

    leggy     stretch (box height / sqrt(leaf area)) up 25 % in 3 days, or above
              2.8 now: the plant is taller than its leaf area supports, i.e. long
              stems or petioles with leaves held apart (etiolation)
    curling   projected leaf area down 15 % in a day while the plant's height held:
              leaves cupping, rolling or drooping show less of their surface
    paler     leaves less green (Lab a* up 3) or yellower (b* up 4) than 3 days
              ago, compared only between photos under the same light
    stalled   leaf area grew less than 5 % in 3 days

The likely causes listed with a finding are the readings known to produce that
symptom, checked against the live state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from camera_paths import PLANTS_LOG

RECENT = pd.Timedelta(hours=4)          # "now" = median of the last 4 h of photos
LEGGY_DAYS, LEGGY_RATIO = 3, 1.25
# A compact plant whose leaves fill half of a square box has stretch ~1.9 in a 16:9
# frame; at 2.8 the plant is half again as tall as its leaf area would make it.
LEGGY_ABS = 2.8
CURL_DAYS, CURL_AREA_RATIO, CURL_HEIGHT_RATIO = 1, 0.85, 0.95
PALE_DAYS, PALE_A_RISE, PALE_B_RISE = 3, 3.0, 4.0
STALL_DAYS, STALL_GROWTH = 3, 0.05
MIN_PHOTOS = 3                          # in each window

COLUMNS = ["timestamp", "plant", "light", "area_pct", "height_pct", "stretch",
           "leaf_l", "leaf_a", "leaf_b"]


@dataclass
class Finding:
    check: str          # leggy | curling | paler | stalled
    title: str
    detail: str
    causes: list[str] = field(default_factory=list)


def load(path: Path = PLANTS_LOG) -> pd.DataFrame:
    """One row per plant per photo, with the health inputs."""
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            when = datetime.strptime(entry["timestamp"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError, TypeError):
            continue
        if entry.get("flash") is False:
            continue   # not lit white: nothing was measured
        for number, p in enumerate(entry.get("plants") or [], start=1):
            area = float(p.get("area_frac", np.nan))
            height = float(p.get("height_frac", np.nan))
            rows.append({
                "timestamp": when,
                "plant": int(p.get("number", number)),
                "light": entry.get("light"),     # None before the camera logged it
                "area_pct": 100 * area,
                "height_pct": 100 * height,
                # Older lines have no "stretch"; it is computable from what they do have.
                "stretch": float(p.get("stretch", height / area ** 0.5 if area > 0 else np.nan)),
                "leaf_l": float(p.get("leaf_l", np.nan)),
                "leaf_a": float(p.get("leaf_a", np.nan)),
                "leaf_b": float(p.get("leaf_b", np.nan)),
            })
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame(rows).sort_values(["timestamp", "plant"]).reset_index(drop=True)


def _window(one: pd.DataFrame, end: pd.Timestamp, column: str, light=None):
    """Median of ``column`` over the RECENT window ending at ``end``, or None."""
    rows = one[(one["timestamp"] > end - RECENT) & (one["timestamp"] <= end)]
    if light is not None:
        rows = rows[rows["light"] == light]
    values = rows[column].dropna()
    return float(values.median()) if len(values) >= MIN_PHOTOS else None


def check_plant(one: pd.DataFrame) -> list[Finding]:
    """Health findings for one plant's rows (one ``plant`` number)."""
    if one.empty:
        return []
    now = one["timestamp"].max()
    then = {days: now - pd.Timedelta(days=days) for days in {LEGGY_DAYS, CURL_DAYS, PALE_DAYS}}
    found = []

    s_now, s_then = _window(one, now, "stretch"), _window(one, then[LEGGY_DAYS], "stretch")
    if s_now and s_then and s_now / s_then >= LEGGY_RATIO:
        found.append(Finding("leggy", "Getting leggy",
                             f"Height is outgrowing leaf area: stretch up "
                             f"{s_now / s_then - 1:.0%} in {LEGGY_DAYS} days."))
    elif s_now and s_now >= LEGGY_ABS:
        found.append(Finding("leggy", "Leggy",
                             f"The plant is tall for its leaf area (stretch {s_now:.1f}; a "
                             "compact plant is about 2): long stems or petioles holding the "
                             "leaves apart."))

    a_now, a_then = _window(one, now, "area_pct"), _window(one, then[CURL_DAYS], "area_pct")
    h_now, h_then = _window(one, now, "height_pct"), _window(one, then[CURL_DAYS], "height_pct")
    if (a_now and a_then and h_now and h_then and a_now / a_then <= CURL_AREA_RATIO
            and h_now / h_then >= CURL_HEIGHT_RATIO):
        found.append(Finding("curling", "Leaves curling or drooping",
                             f"Visible leaf area fell {1 - a_now / a_then:.0%} in a day while "
                             "the plant kept its height, so leaves are cupping, rolling or "
                             "hanging."))

    light = one["light"].dropna().iloc[-1] if one["light"].notna().any() else None
    if light is not None:
        da = _diff(one, now, then[PALE_DAYS], "leaf_a", light)
        db = _diff(one, now, then[PALE_DAYS], "leaf_b", light)
        if (da is not None and da >= PALE_A_RISE) or (db is not None and db >= PALE_B_RISE):
            found.append(Finding("paler", "Leaves turning paler",
                                 "Leaf colour has moved away from green "
                                 f"(a* {da:+.1f}, b* {db:+.1f} in {PALE_DAYS} days) "
                                 "compared under the same light." if da is not None
                                 and db is not None else
                                 "Leaf colour has moved away from green."))

    g_then = _window(one, now - pd.Timedelta(days=STALL_DAYS), "area_pct")
    if a_now and g_then and a_now / g_then - 1 < STALL_GROWTH:
        found.append(Finding("stalled", "Growth has stalled",
                             f"Leaf area changed {a_now / g_then - 1:+.0%} in {STALL_DAYS} days."))
    return found


def _diff(one, now, then, column, light):
    a, b = _window(one, now, column, light), _window(one, then, column, light)
    return None if a is None or b is None else a - b


def _num(state, key):
    try:
        value = float(state.get(key))
    except (TypeError, ValueError):
        return None
    return None if np.isnan(value) else value


def likely_causes(check: str, state: dict) -> list[str]:
    """Readings that are known to produce ``check``, if the live state shows them."""
    state = state or {}
    ph, tds = _num(state, "ph_ema") or _num(state, "ph"), _num(state, "tds_ppm_ema") or _num(state, "tds_ppm")
    ph_hi, ph_lo = _num(state, "ph_dead_high"), _num(state, "ph_dead_low")
    ec_hi, ec_lo = _num(state, "ec_dead_high"), _num(state, "ec_dead_low")
    air, water = _num(state, "ambient_temp_c"), _num(state, "water_temp_c")
    rh = _num(state, "ambient_humidity_pct")
    solution = (state.get("solution") or {}).get("status")
    high_ph = ph is not None and ph_hi is not None and ph > ph_hi
    low_ph = ph is not None and ph_lo is not None and ph < ph_lo
    out = []
    if check == "leggy":
        out.append("Too little light at the leaves: lower the light or raise its intensity "
                   "(stretching is the usual response to weak light).")
        if air is not None and air > 26:
            out.append(f"Warm air ({air:.0f} °C) speeds stem elongation.")
    if check in ("curling", "paler", "stalled"):
        if high_ph:
            out.append(f"pH {ph:.1f} is above {ph_hi:.1f}: iron and manganese become "
                       "unavailable, which pales new leaves and slows growth.")
        if low_ph:
            out.append(f"pH {ph:.1f} is below {ph_lo:.1f}: calcium and magnesium uptake "
                       "suffers and roots can be damaged.")
    if check in ("curling", "stalled") and tds is not None and ec_hi is not None and tds > ec_hi:
        out.append(f"TDS {tds:.0f} ppm is above {ec_hi:.0f}: salt stress curls leaves and "
                   "slows growth.")
    if check in ("paler", "stalled") and tds is not None and ec_lo is not None and tds < ec_lo:
        out.append(f"TDS {tds:.0f} ppm is below {ec_lo:.0f}: too little nutrient (nitrogen "
                   "shortage pales older leaves first).")
    if check == "curling":
        if air is not None and air > 28:
            out.append(f"Hot air ({air:.0f} °C): leaves cup to shed heat.")
        if rh is not None and rh < 40:
            out.append(f"Dry air ({rh:.0f} % RH) makes leaves lose water faster than roots "
                       "supply it.")
        if water is not None and water > 25:
            out.append(f"Warm solution ({water:.0f} °C) holds little oxygen, so roots "
                       "struggle and leaves droop.")
    if check in ("curling", "paler", "stalled") and solution == "due":
        out.append("The nutrient solution is due for a change (Settings → Nutrient solution).")
    return out


def assess(plants: pd.DataFrame, state: dict) -> dict[int, list[Finding]]:
    """{plant number: findings, each with its likely causes} for every plant seen."""
    out = {}
    for number, one in plants.groupby("plant", sort=True):
        findings = check_plant(one.sort_values("timestamp"))
        for f in findings:
            f.causes = likely_causes(f.check, state)
        out[int(number)] = findings
    return out
