"""Dose markers on the trends charts sit where the dose happened."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import charts  # noqa: E402


def test_ph_marker_goes_at_the_planning_check_not_the_late_recheck():
    # 14 Oct: pH-up planned at 02:14:18Z, recheck 40 min later (settle wait stretched).
    events = [
        {"timestamp": "2026-10-14T02:14:18Z", "event": "ph_check", "ph_before": 4.364},
        {"timestamp": "2026-10-14T02:54:24Z", "event": "ph_recheck", "ph_dose_dir": "up",
         "ph_dose_ml": 3.0, "ph_before": 4.364, "ph_after": 8.11},
        {"timestamp": "2026-10-14T02:54:24Z", "event": "ph_check", "ph_before": 8.11},
    ]
    _, ph = charts.dose_markers(events, ph_settle_s=1200)
    assert len(ph) == 1
    marked = ph.iloc[0]["t"]
    planned = charts.local_frame(
        __import__("pandas").DataFrame([{"time": "2026-10-14T02:14:18Z"}]))["t"].iloc[0]
    assert marked == planned
    assert "at pH 4.36" in ph.iloc[0]["detail"]


def test_recheck_without_its_check_falls_back_to_the_settle_wait():
    events = [{"timestamp": "2026-10-14T02:54:24Z", "event": "ph_recheck", "ph_dose_dir": "down",
               "ph_dose_ml": 1.0}]
    _, ph = charts.dose_markers(events, ph_settle_s=1200)
    assert len(ph) == 1
