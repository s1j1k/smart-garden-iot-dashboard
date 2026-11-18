"""Paths for Pi camera captures. Separate from MQTT telemetry files."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CAMERA_DATA_DIR = REPO_ROOT / "data" / "camera"
IMAGE_DIR = CAMERA_DATA_DIR / "images"
DEBUG_DIR = CAMERA_DATA_DIR / "debug"
LATEST_IMAGE = CAMERA_DATA_DIR / "latest.jpg"
GROWTH_CSV = CAMERA_DATA_DIR / "growth.csv"
PLANTS_LOG = CAMERA_DATA_DIR / "plants.jsonl"
LATEST_PLANTS_IMAGE = CAMERA_DATA_DIR / "latest_plants.jpg"   # latest frame, plants boxed
LAYOUT_JSON = CAMERA_DATA_DIR / "layout.json"    # net pots / plants from the pre-trained model
LAYOUT_IMAGE = CAMERA_DATA_DIR / "layout.jpg"    # the frame it ran on, with its boxes

# One still every 5 minutes. Each JPEG in images/ is one timelapse frame.
# Override on the Pi: CAMERA_CAPTURE_INTERVAL_S=600 python src/camera_capture.py
_DEFAULT_CAPTURE_INTERVAL_S = 5 * 60


def capture_interval_s() -> int:
    raw = os.getenv("CAMERA_CAPTURE_INTERVAL_S", str(_DEFAULT_CAPTURE_INTERVAL_S)).strip()
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_CAPTURE_INTERVAL_S
    return max(1, value)


_DEFAULT_NIGHT_INTERVAL_S = 1800


def night_capture_interval_s() -> int:
    """Gap between timelapse photos while the grow light is off (default 30 min).

    Each photo needs a flash of white light, and lettuce is a long-day plant that a
    night break can push towards bolting, so the dark period is photographed sparsely.
    Never shorter than the daytime gap.
    """
    raw = os.getenv("CAMERA_NIGHT_INTERVAL_S", str(_DEFAULT_NIGHT_INTERVAL_S)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = _DEFAULT_NIGHT_INTERVAL_S
    return max(capture_interval_s(), value)


def format_capture_interval(seconds: float) -> str:
    """Human label for a capture gap, e.g. 3600 -> '1 hour'."""
    if seconds >= 3600:
        hours = seconds / 3600
        if abs(hours - round(hours)) < 0.05:
            hours_i = int(round(hours))
            return "1 hour" if hours_i == 1 else f"{hours_i} hours"
        return f"{hours:.1f} hours"
    if seconds >= 60:
        minutes = seconds / 60
        if abs(minutes - round(minutes)) < 0.05:
            minutes_i = int(round(minutes))
            return "1 minute" if minutes_i == 1 else f"{minutes_i} minutes"
        return f"{minutes:.1f} minutes"
    seconds_i = int(round(seconds))
    return "1 second" if seconds_i == 1 else f"{seconds_i} seconds"
