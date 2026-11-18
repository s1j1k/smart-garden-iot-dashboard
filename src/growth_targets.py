"""What "good" looks like for each grow stage, and when to move to the next one.

Ranges are for the current crop (basil + lettuce, leafy greens in deep water
culture). They are the bands the dashboard shades on charts and the bounds the
alert engine checks; outside them a warning is raised. pH and TDS bands come
from the ESP32 stage itself (``ph_target_*``, ``nutrient_tds_*``), not from
here, because the node is what actually doses.

Sources: Johnny's hydroponic basil guide, OSU hydroponic EC/pH guide, and the
usual leafy-green DWC references (see README "Why these numbers").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from config import GROW_PRESETS, STAGE_ORDER, normalize_stage_name, suggested_grow_stage

GROWTH_STAGES = ["seedling", "transitional", "vegetative", "harvest"]

# (low, high) acceptable range per stage. Alerts fire outside these.
CLIMATE_TARGETS = {
    "seedling": {
        "air_temp_c": (20.0, 26.0),
        "humidity_pct": (60.0, 80.0),
        "water_temp_c": (18.0, 24.0),
        "co2_ppm": (400.0, 1500.0),
    },
    "transitional": {
        "air_temp_c": (18.0, 27.0),
        "humidity_pct": (50.0, 75.0),
        "water_temp_c": (18.0, 24.0),
        "co2_ppm": (400.0, 1500.0),
    },
    "vegetative": {
        "air_temp_c": (18.0, 27.0),
        "humidity_pct": (45.0, 70.0),
        "water_temp_c": (18.0, 24.0),
        "co2_ppm": (400.0, 1500.0),
    },
    "harvest": {
        "air_temp_c": (16.0, 26.0),
        "humidity_pct": (40.0, 70.0),
        "water_temp_c": (16.0, 24.0),
        "co2_ppm": (400.0, 1500.0),
    },
}

CLIMATE_LABELS = {
    "air_temp_c": ("Air temperature", "°C"),
    "humidity_pct": ("Humidity", "%"),
    "water_temp_c": ("Water temperature", "°C"),
    "co2_ppm": ("CO₂", "ppm"),
}


def climate_stage(actual_stage, suggested_stage):
    """Stage whose climate bands apply: the node's growth stage, else the plant-age guess."""
    actual = normalize_stage_name(actual_stage) if actual_stage else None
    if actual in CLIMATE_TARGETS:
        return actual
    if suggested_stage in CLIMATE_TARGETS:
        return suggested_stage
    return None


def climate_band(stage, metric):
    return CLIMATE_TARGETS.get(stage or "", {}).get(metric)


def ph_band(state, stage):
    """(min, max) acceptable pH: the controller's dead limits, live from the
    node, else the stage preset.

    This is the range the plants grow normally in, so the dashboard calls a
    reading inside it fine, exactly when the controller leaves it alone. The
    narrower target band only says where the setpoint sits.
    """
    lo, hi = state.get("ph_dead_low"), state.get("ph_dead_high")
    if lo is not None and hi is not None:
        return float(lo), float(hi)
    preset = GROW_PRESETS.get(normalize_stage_name(stage) if stage else "idle")
    return (preset["ph_dead_low"], preset["ph_dead_high"]) if preset else None


def tds_band(state, stage):
    """(min, max) acceptable TDS in ppm (the controller's dead limits), or None
    when the stage does not feed nutrient."""
    target_hi = state.get("nutrient_tds_max")
    lo, hi = state.get("ec_dead_low"), state.get("ec_dead_high")
    if lo is not None and hi is not None and target_hi is not None and float(target_hi) > 0:
        return float(lo), float(hi)
    preset = GROW_PRESETS.get(normalize_stage_name(stage) if stage else "idle")
    if preset and preset["nutrient_tds_max"] > 0:
        return preset["ec_dead_low"], preset["ec_dead_high"]
    return None


@dataclass(frozen=True)
class StageHint:
    next_stage: str
    when: str
    look_for: list[str]
    then: str


# What to watch for before moving on from each stage.
STAGE_HINTS = {
    "idle": StageHint(
        "seedling",
        "As soon as seedlings are in the net pots and the reservoir is filled.",
        [
            "Seedlings transplanted into net pots, roots touching the water or wick.",
            "Reservoir filled to the net-pot line (Water level near target).",
            "pH probe and pumps calibrated (no calibration alerts).",
        ],
        "Seedling holds the water up to the pots and corrects pH only — no nutrient yet.",
    ),
    "seedling": StageHint(
        "transitional",
        "Usually 7–10 days after planting (week 2).",
        [
            "First **true leaves** have opened (jagged edges; the seed leaves are round).",
            "White roots are poking out of the bottom of the plug or net pot.",
            "Seedlings stand upright and are no longer stretching towards the light.",
            "Camera green coverage is clearly rising from day to day.",
        ],
        "Transitional starts half-strength nutrient and lowers the water 20 mm so roots chase it.",
    ),
    "transitional": StageHint(
        "vegetative",
        "Usually around day 21–28 (week 4).",
        [
            "3–4 sets of true leaves on basil; lettuce has a small rosette of 5–6 leaves.",
            "Roots reach well into the reservoir and are white (brown or slimy roots are rotting).",
            "TDS holds steady between doses (the plants are taking up nutrient as well as water).",
            "New leaves are a healthy mid-green — pale new growth means they want more feed.",
        ],
        "Vegetative raises nutrient to full strength for fast leaf growth.",
    ),
    "vegetative": StageHint(
        "harvest",
        "Usually from about day 42 (week 7), or when you start cutting regularly.",
        [
            "Basil is 15–20 cm tall with 6+ leaf pairs — pinch the tops to keep it bushy.",
            "Lettuce outer leaves are 10–15 cm long and the head looks full.",
            "**Flower buds** forming on basil (cut them off and switch to harvest).",
            "Camera plant area has levelled off for several days.",
        ],
        "Harvest stage eases off the nutrient so leaves taste sweeter and water use drops.",
    ),
    "harvest": StageHint(
        "idle",
        "After the last cut, before cleaning the system.",
        [
            "Plants are cleared or bolting (lettuce stretching tall and turning bitter).",
            "You are about to drain and clean the reservoir.",
        ],
        "Idle stops all dosing so the node does not dose an empty or fresh tank.",
    ),
}

for _stage in ("sensor_baseline", "filling", "ph_baseline"):
    STAGE_HINTS[_stage] = STAGE_HINTS["idle"]


def hint_for(stage):
    return STAGE_HINTS.get(normalize_stage_name(stage) if stage else "idle")


def stage_rank(stage):
    """Order growth stages for plotting; set-up stages share rank 0."""
    stage = normalize_stage_name(stage) if stage else "idle"
    return GROWTH_STAGES.index(stage) + 1 if stage in GROWTH_STAGES else 0


def stage_change_due(actual_stage, suggested_stage):
    """True when plant age says the next growth stage is due but the node is behind."""
    if not suggested_stage:
        return False
    return stage_rank(suggested_stage) > stage_rank(actual_stage)


def predicted_timeline(planted_at: datetime, until: datetime):
    """[(start, stage)] for the age-based stage schedule from planting to ``until``."""
    points = []
    week = 1
    last = None
    while True:
        start = planted_at + timedelta(days=7 * (week - 1))
        if start > until:
            break
        stage = suggested_grow_stage(week)
        if stage != last:
            points.append((start, stage))
            last = stage
        week += 1
    return points


def actual_timeline(changes, until: datetime):
    """Turn [(time, stage)] change points into step segments [(start, end, stage)]."""
    segments = []
    for i, (start, stage) in enumerate(changes):
        end = changes[i + 1][0] if i + 1 < len(changes) else until
        segments.append((start, end, normalize_stage_name(stage)))
    return segments


STAGE_LABELS = {key: key.replace("_", " ").title() for key in STAGE_ORDER}
STAGE_LABELS["ph_baseline"] = "pH baseline"
