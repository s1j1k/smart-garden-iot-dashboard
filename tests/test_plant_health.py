"""Plant health checks on synthetic photo histories."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import plant_health  # noqa: E402

START = pd.Timestamp("2026-10-10 06:00")


def _history(days=4, every_h=1, light="day", **series):
    """One plant photographed every ``every_h`` hours; each series is f(day) -> value."""
    rows = []
    for i in range(int(days * 24 / every_h) + 1):
        day = i * every_h / 24
        row = {"timestamp": START + pd.Timedelta(hours=i * every_h), "plant": 1, "light": light,
               "area_pct": 1.0, "height_pct": 20.0, "stretch": 1.8,
               "leaf_l": 60.0, "leaf_a": -20.0, "leaf_b": 20.0}
        row.update({k: f(day) for k, f in series.items()})
        rows.append(row)
    return pd.DataFrame(rows)


def _checks(frame):
    return [f.check for f in plant_health.check_plant(frame)]


def test_healthy_growth_raises_nothing():
    grow = _history(area_pct=lambda d: 1.0 * 1.3 ** d, height_pct=lambda d: 20 * 1.1 ** d)
    assert _checks(grow) == []


def test_stretching_plant_is_leggy():
    assert "leggy" in _checks(_history(area_pct=lambda d: 1.0 * 1.2 ** d,
                                       stretch=lambda d: 1.8 * 1.15 ** d))
    # Already tall for its leaf area (the lettuce on 13 Oct: stretch 3.3).
    assert "leggy" in _checks(_history(area_pct=lambda d: 1.2 ** d, stretch=lambda d: 3.3))


def test_leaf_area_dropping_at_same_height_is_curling():
    def area(d):
        return 1.5 if d < 3 else 1.1      # leaves cupped overnight
    assert "curling" in _checks(_history(area_pct=area))
    # Smaller and shorter (e.g. a leaf removed): not curling.
    assert "curling" not in _checks(_history(area_pct=area,
                                             height_pct=lambda d: 20 if d < 3 else 15))


def test_paler_only_compares_photos_under_the_same_light():
    pale = _history(area_pct=lambda d: 1.2 ** d, leaf_a=lambda d: -20 + 1.5 * d)
    assert "paler" in _checks(pale)
    # Night photos (flash only) look less green, but must not count against day ones.
    mixed = pd.concat([_history(area_pct=lambda d: 1.2 ** d),
                       _history(every_h=2, light="night", area_pct=lambda d: 1.2 ** d,
                                leaf_a=lambda d: -10.0)])
    assert "paler" not in _checks(mixed.sort_values("timestamp"))


def test_no_growth_is_stalled():
    assert "stalled" in _checks(_history())


def test_too_little_history_says_nothing():
    assert plant_health.check_plant(_history(days=0.5)) == []


def test_causes_come_from_the_live_state():
    state = {"ph_ema": 8.2, "ph_dead_high": 6.5, "ph_dead_low": 5.5, "tds_ppm_ema": 1100,
             "ec_dead_high": 850, "ec_dead_low": 550, "ambient_temp_c": 22,
             "solution": {"status": "due"}}
    causes = " ".join(plant_health.likely_causes("paler", state))
    assert "pH 8.2" in causes and "iron" in causes and "solution" in causes
    assert any("TDS 1100" in c for c in plant_health.likely_causes("curling", state))
    assert any("light" in c for c in plant_health.likely_causes("leggy", state))
    assert plant_health.likely_causes("paler", {"ph_ema": 6.0, "ph_dead_high": 6.5}) == []
