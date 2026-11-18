"""Turning pre-trained detector output into the pot / plant layout (no model needed)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import plant_locator  # noqa: E402


def test_layout_keeps_distinct_pots_left_to_right():
    detections = [
        {"label": "net pot", "score": 0.62, "box": [600, 300, 760, 350]},
        {"label": "net pot", "score": 0.55, "box": [100, 300, 260, 350]},
        {"label": "net pot", "score": 0.40, "box": [105, 302, 262, 352]},   # same pot again
        {"label": "net pot", "score": 0.70, "box": [20, 200, 940, 530]},    # the whole reservoir
        {"label": "net pot", "score": 0.20, "box": [400, 300, 520, 350]},   # too unsure
        {"label": "plant", "score": 0.45, "box": [120, 150, 240, 300]},
        {"label": "net pot plant", "score": 0.80, "box": [300, 150, 380, 500]},  # pot + plant
    ]
    layout = plant_locator.layout_from_detections(detections, (960, 540))
    assert [round(p["box"][0] * 960) for p in layout["pots"]] == [100, 600]
    assert layout["pots"][0]["score"] == 0.55
    assert len(layout["plants"]) == 2                  # "net pot plant" is a plant box, not a pot
    assert all(0 <= v <= 1 for p in layout["pots"] for v in p["box"])
    assert layout["model"] == plant_locator.MODEL_ID and plant_locator.MODEL_LICENCE == "Apache-2.0"


def test_no_detections():
    layout = plant_locator.layout_from_detections([], (960, 540))
    assert layout["pots"] == [] and layout["plants"] == []
