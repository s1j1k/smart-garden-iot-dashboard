"""Unit checks for the health lights and control schedule panel."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sensor_ingest import normalize_reading, reading_to_state  # noqa: E402
from status_panel import (  # noqa: E402
    COLORS,
    alert_lines,
    device_status,
    health_rows,
    loop_status,
)

NOW = datetime(2026, 9, 30, 10, 0, 0, tzinfo=timezone.utc)


def _payload(**extra):
    base = {
        "device_id": "garden-node-01",
        "ambient_temp_c": 26.0,
        "ambient_humidity_pct": 54.0,
        "water_temp_c": 23.3,
        "light_adc": 2679,
        "water_level_pct": -1,
        "tds_ppm": 1115,
        "ph": None,
        "co2_ppm": 1454,
    }
    base.update(extra)
    return base


def test_device_status_colours():
    state = {"health": {"ph": "fault", "tds": "ok", "water_level": "init"},
             "health_msg": {"ph": "no valid pH"}}
    assert device_status(state, "ph", True)["color"] == COLORS["fault"]
    assert device_status(state, "ph", True)["message"] == "no valid pH"
    assert device_status(state, "tds", True)["color"] == COLORS["ok"]
    assert device_status(state, "water_level", True)["label"] == "Init required"
    # Not reported by old firmware → grey, not blue.
    assert device_status(state, "co2", True)["state"] == "unknown"


def test_no_comms_is_grey_and_keeps_last_state():
    state = {"health": {"ph": "fault"}}
    status = device_status(state, "ph", False)
    assert status["state"] == "nocomms"
    assert status["color"] == COLORS["nocomms"]
    assert "FAULT" in status["message"]


def test_health_rows_include_pump_running():
    state = {"health": {"water_pump": "ok"}, "water_pump_on": True}
    row = next(r for r in health_rows(state, True) if r["key"] == "water_pump")
    assert row["running"] == "ON"


def test_ingest_replaces_status_each_message():
    first = normalize_reading(_payload(health={"ph": "fault"}, ph_sensor_fault=True))
    state = reading_to_state(first, {})
    assert state["health"]["ph"] == "fault"
    second = normalize_reading(_payload(ph=6.1, health={"ph": "ok"}, ph_sensor_fault=False))
    state = reading_to_state(second, state)
    assert state["health"]["ph"] == "ok"
    assert state["ph_sensor_fault"] is False


def _sched_state(**extra):
    state = {
        "received_at": (NOW - timedelta(seconds=60)).isoformat(),
        "sched": {
            "read_s": 10, "read_in_s": 4, "activity": "idle", "wait_s": 0,
            "water_settle_s": 30, "ec_settle_s": 2700, "ph_settle_s": 900,
            "ema_warmup_s": 60,
            "level": {"on": True, "every_s": 7200, "in_s": 3660,
                      "topup_at_pct": 72, "refill_to_pct": 78},
            "ec": {"on": True, "every_s": 7200, "in_s": 0, "dose_below": 750,
                   "min": 800, "max": 1200, "high_above": 1250},
            "ph": {"on": True, "every_s": 7200, "in_s": 600, "up_below": 5.4,
                   "down_above": 5.9, "min": 5.5, "max": 5.8},
        },
    }
    state.update(extra)
    return state


def _live(**extra):
    state = _sched_state(ph_ema=5.6, tds_ppm_ema=800, water_level_pct=80,
                         loops_paused={"level": False, "nutrient": False, "ph": False},
                         auto_control_paused=False)
    for key in ("ph", "ec", "level"):
        state["sched"][key]["every_s"] = 0
    state.update(extra)
    return state


def test_loop_idle_shows_trigger_and_value():
    state = _live()
    assert loop_status("ph", state, True, now=NOW) == (
        "idle", "When pH leaves 5.40–5.90 · now 5.60")
    assert loop_status("nutrient", state, True, now=NOW) == (
        "idle", "When TDS < 750 ppm · now 800")
    assert loop_status("level", state, True, now=NOW)[1] == "When level ≤ 72 % · now 80 %"


def test_loop_out_of_limits_runs_next_pass_or_retries():
    state = _live(ph_ema=6.7)
    state["sched"]["ph"]["down_above"] = 6.5
    assert loop_status("ph", state, True, now=NOW) == ("due", "Runs on the next pass · now 6.70")
    state["sched"]["retry_in_s"] = 773
    kind, text = loop_status("ph", state, True, now=NOW)
    assert kind == "held" and text.startswith("Out of limits · retries in 12 min")


def test_loop_paused_held_off_and_offline():
    state = _live(loops_paused={"level": False, "nutrient": False, "ph": True})
    assert loop_status("ph", state, True, now=NOW) == ("paused", "Paused")
    assert loop_status("nutrient", _live(auto_control_paused=True), True, now=NOW)[0] == "paused"
    state = _live()
    state["sched"]["level"]["hold"] = "water level init required"
    assert loop_status("level", state, True, now=NOW) == (
        "held", "Held: water level init required")
    state["sched"]["ec"]["on"] = False
    assert loop_status("nutrient", state, True, now=NOW) == ("off", "Off in this stage")
    assert loop_status("ph", state, False, now=NOW) == ("off", "No live telemetry")
    assert loop_status("ph", {}, True, now=NOW)[0] == "off"


def test_loop_running_and_mixing():
    state = _live(ph_down_pump_on=True)
    assert loop_status("ph", state, True, now=NOW) == ("run", "Running now")
    state = _live()
    state["sched"].update(activity="wait_ph_settle", wait_s=480)
    assert loop_status("ph", state, True, now=NOW) == ("wait", "Mixing · checks again in 8 min")
    assert loop_status("nutrient", state, True, now=NOW)[1] == "Waits 8 min for a dose to mix"


def test_loop_run_request_shows_until_stale():
    state = _live()
    asked = NOW - timedelta(seconds=5)
    assert loop_status("ph", state, True, requested_at=asked, now=NOW)[1].startswith("Requested")
    stale = NOW - timedelta(seconds=60)
    assert loop_status("ph", state, True, requested_at=stale, now=NOW)[0] == "idle"
    state["sched"]["ph_after_ec"] = True
    assert loop_status("ph", state, True, now=NOW) == ("due", "Runs on the next pass")


def test_alert_lines_are_a_single_block():
    rows = health_rows({"health": {"ph": "fault", "water_level": "init"},
                        "health_msg": {"ph": "no valid pH"}}, True)
    lines = alert_lines(rows, True)
    assert len(lines) == 2 and "FAULT" in lines[0]
    assert "No faults" in alert_lines(health_rows({"health": {"ph": "ok"}}, True), True)[0]
    assert "No live telemetry" in alert_lines(rows, False)[0]


def test_ingest_keeps_firmware_influx_and_replay_flag():
    live = normalize_reading(_payload(firmware_version="1.6.2",
                                      influx={"queued": 1, "ok_ago_s": 4}))
    assert live["firmware_version"] == "1.6.2" and live["replay"] is False
    state = reading_to_state(live, {})
    assert state["firmware_version"] == "1.6.2"
    assert state["influx"]["ok_ago_s"] == 4
    replay = normalize_reading(_payload(replay=True))
    assert replay["replay"] is True and replay["firmware_version"] is None


def test_influx_upload_light():
    state = {"health": {"influx": "fault"},
             "health_msg": {"influx": "last upload 300 s ago, HTTP -1, queue 8/8, dropped 20"}}
    row = next(r for r in health_rows(state, True) if r["key"] == "influx")
    assert row["color"] == COLORS["fault"] and "300 s" in row["message"]
    # Older firmware does not report it: grey, not blue.
    row = next(r for r in health_rows({"health": {}}, True) if r["key"] == "influx")
    assert row["state"] == "unknown"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK", name)


if __name__ == "__main__":
    main()


def test_refill_progress():
    from status_panel import refill_progress
    assert refill_progress({"active": False}) is None
    frac, text = refill_progress({"active": True, "start_pct": 35, "now_pct": 40, "target_pct": 45,
                                  "delivered_ml": 300, "planned_ml": 1100, "minutes": 7})
    assert abs(frac - 0.5) < 1e-9 and "300 of about 1100 mL" in text
    frac, _ = refill_progress({"active": True, "start_pct": 35, "now_pct": 30, "target_pct": 45})
    assert frac == 0.0  # a low reading never shows negative progress
