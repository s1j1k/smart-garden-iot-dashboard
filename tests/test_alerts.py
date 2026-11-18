"""Alert engine, notification tracker and the alert documentation."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import alerts  # noqa: E402
from notifier import AlertTracker, dispatch, format_message  # noqa: E402

NOW = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)


def _state(**extra):
    state = {
        "received_at": (NOW - timedelta(seconds=5)).isoformat(),
        "grow_stage": "vegetative",
        "ph_ema": 6.0, "tds_ppm_ema": 700, "water_level_pct": 45,
        "water_temp_c": 21.0, "ambient_temp_c": 22.0, "ambient_humidity_pct": 55.0,
        "co2_ppm": 800, "ph_target_min": 5.8, "ph_target_max": 6.2,
        "ph_dead_low": 5.5, "ph_dead_high": 6.5,
        "nutrient_tds_min": 600, "nutrient_tds_max": 800,
        "ec_dead_low": 550, "ec_dead_high": 850,
        "ph_probe_calibrated": True, "water_calibrated": True, "nutrient_calibrated": True,
        "ph_up_calibrated": True, "ph_down_calibrated": True,
        "health": {"ph": "ok", "tds": "ok"},
    }
    state.update(extra)
    return state


def _keys(state, **kw):
    kw.setdefault("node_live", True)
    kw.setdefault("suggested_stage", "vegetative")
    kw.setdefault("plant_week", 4)
    return [a.key for a in alerts.evaluate(state, now=NOW, **kw)]


def test_healthy_garden_has_no_alerts():
    assert _keys(_state()) == []


def test_offline_only_reports_offline_and_stage_reminder():
    keys = _keys(_state(ph_ema=4.0, grow_stage="transitional"), node_live=False,
                 last_seen_s=600)
    assert keys == ["node_offline", "stage_change_due"]


def test_sensor_fault_and_implausible_reading():
    keys = _keys(_state(health={"ph": "fault"}, health_msg={"ph": "no valid pH"},
                        water_temp_c=-127.0))
    assert "sensor_fault.ph" in keys
    assert "implausible.water_temp_c" in keys
    # A faulted pH probe does not also raise "pH out of range".
    assert "ph_out_of_range" not in _keys(_state(health={"ph": "fault"}, ph_ema=4.0))


def test_uncalibrated_probe_and_pumps():
    keys = _keys(_state(ph_probe_calibrated=False, nutrient_calibrated=False))
    assert {"uncalibrated.ph_probe", "uncalibrated.nutrient"} <= set(keys)


def test_ph_inside_acceptable_range_is_not_an_alert():
    # 6.3 is outside the 5.8-6.2 target band but inside the 5.5-6.5 range the
    # controller leaves alone, so the dashboard must not warn about it either.
    assert "ph_out_of_range" not in _keys(_state(ph_ema=6.3))
    assert "tds_low" not in _keys(_state(tds_ppm_ema=580))


def test_chemistry_and_climate_warnings():
    keys = _keys(_state(ph_ema=6.7, tds_ppm_ema=500, ambient_humidity_pct=30,
                        water_temp_c=27))
    assert {"ph_out_of_range", "tds_low", "climate.humidity_pct",
            "climate.water_temp_c"} <= set(keys)
    extreme = next(a for a in alerts.evaluate(_state(ph_ema=7.4), node_live=True, now=NOW,
                                              suggested_stage="vegetative", plant_week=4)
                   if a.key == "ph_out_of_range")
    assert extreme.notify_after_s == 30 * 60


def test_tds_high_uses_firmware_flag_level():
    state = _state(tds_ppm_ema=1300, sched={"ec": {"high_above": 1400}})
    assert "tds_high" not in _keys(state)
    assert "tds_high" in _keys(_state(tds_ppm_ema=1500, sched={"ec": {"high_above": 1400}}))


def test_bottles():
    keys = _keys(_state(nutrient_empty=True, stock_alert="low:ph_down"))
    assert "bottle_empty.nutrient" in keys and "bottle_low" in keys


def test_stage_change_and_idle():
    assert "stage_change_due" in _keys(_state(grow_stage="seedling"), suggested_stage="transitional",
                                       plant_week=2)
    assert "stage_change_due" not in _keys(_state(grow_stage="vegetative"),
                                           suggested_stage="transitional", plant_week=2)
    assert "node_idle" in _keys(_state(grow_stage="idle"))


def test_faults_sort_first():
    found = alerts.evaluate(_state(ph_probe_calibrated=False, nutrient_empty=True),
                            node_live=True, now=NOW, suggested_stage="vegetative", plant_week=4)
    assert found[0].severity == "fault"


def test_tracker_waits_repeats_and_resolves(tmp_path):
    tracker = AlertTracker(tmp_path / "state.json")
    alert = alerts.Alert("climate.air_temp_c", "warning", "Climate", "Air is hot",
                         notify_after_s=3600)
    assert tracker.update([alert], NOW) == []                          # not lasted long enough
    sent = tracker.update([alert], NOW + timedelta(hours=1))
    assert [kind for kind, _ in sent] == ["new"]
    assert tracker.update([alert], NOW + timedelta(hours=2)) == []     # inside repeat window
    assert [k for k, _ in tracker.update([alert], NOW + timedelta(hours=14))] == ["repeat"]
    resolved = tracker.update([], NOW + timedelta(hours=15))
    assert [k for k, _ in resolved] == ["resolved"]


def test_tracker_respects_minimum_severity(tmp_path):
    tracker = AlertTracker(tmp_path / "state.json")
    info = alerts.Alert("upload_fault", "info", "System", "Backup failing")
    stage = alerts.Alert("stage_change_due", "info", "Growth stage", "Time to switch")
    sent = tracker.update([info, stage], NOW, min_severity="warning", stage_changes=True)
    assert [a.key for _, a in sent] == ["stage_change_due"]


def test_failed_send_is_retried(tmp_path):
    tracker = AlertTracker(tmp_path / "state.json")
    alert = alerts.Alert("bottle_empty.water", "fault", "Nutrients", "Water jug is empty")
    calls = []

    def failing(*args):
        calls.append(args)
        return False, "offline"

    dispatch(tracker, [alert], {"ntfy_topic": "t"}, NOW, sender=failing)
    dispatch(tracker, [alert], {"ntfy_topic": "t"}, NOW + timedelta(seconds=30), sender=failing)
    assert len(calls) == 2


def test_message_includes_action():
    alert = alerts.Alert("water_low", "warning", "Water", "Reservoir water is low",
                         "Water level 8 %.", "Refill the water jug.")
    title, body, priority, tags = format_message("new", alert)
    assert title == "Reservoir water is low" and "Refill the water jug." in body
    assert priority == "high"


def test_every_alert_is_documented_in_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for entry in alerts.CATALOGUE:
        assert f"`{entry.key}`" in readme, entry.key


def test_engine_only_raises_catalogued_keys():
    patterns = [re.escape(e.key).replace(r"\.\*", r"\.\w+").replace(r"<pump>", r"\w+")
                for e in alerts.CATALOGUE]
    state = _state(health={"ph": "fault", "grow_light": "fault", "influx": "fault",
                           "auto_control": "hold"},
                   ph_probe_calibrated=False, water_calibrated=False, nutrient_empty=True,
                   stock_alert="low:ph_up", tds_ppm_ema=50, ambient_temp_c=35,
                   ambient_humidity_pct=20, water_temp_c=30, co2_ppm=2500,
                   water_level_init_required=True, ambient_humidity_pct_raw=None)
    for key in _keys(state, suggested_stage="harvest", plant_week=8):
        assert any(re.fullmatch(p, key) for p in patterns), key


def test_session_cap_warns_near_and_at_the_limit():
    caps = {"ph_session_cap_ml": 10.0, "ec_session_cap_ml": 100.0}
    assert not [k for k in _keys(_state(ph_session_ml=5.0, ec_session_ml=20.0, **caps))
                if k.startswith("session")]
    keys = _keys(_state(ph_session_ml=8.5, ec_session_ml=20.0, **caps))
    assert "session_near_cap.ph" in keys and "session_cap.ph" not in keys
    keys = _keys(_state(ph_session_ml=10.15, ec_session_ml=100.0, **caps))
    assert "session_cap.ph" in keys and "session_cap.ec" in keys
    assert "session_near_cap.ph" not in keys
    # A stage with no pH limit (cap 0) never warns.
    assert not [k for k in _keys(_state(ph_session_ml=3.0, ph_session_cap_ml=0.0))
                if k.startswith("session")]


def test_ph_no_response_replaces_the_generic_pump_fault(monkeypatch):
    monkeypatch.setenv("SMART_GARDEN_SITE", "admin")
    health = {"ph": "ok", "tds": "ok", "ph_down_pump": "fault"}
    keys = _keys(_state(ph_down_no_response=True, health=health,
                        health_msg={"ph_down_pump": "no response: 3 doses (1.4 mL)"}))
    assert "no_response.ph_down" in keys
    assert "actuator_fault.ph_down_pump" not in keys
    assert "no_response.ph_up" not in keys
    # Without the flag (older firmware, or a bottle-empty fault) the generic alert stays.
    keys = _keys(_state(health=health))
    assert "actuator_fault.ph_down_pump" in keys and "no_response.ph_down" not in keys


def test_water_change_alert_explains_why():
    due = {"status": "due", "due": "buffer,ph_band", "watch": "", "buffer_ratio": 0.3,
           "buffer_n": 4, "ph_out_h": 18, "age_d": 2.0}
    found = [a for a in alerts.evaluate(_state(solution=due), node_live=True,
                                        suggested_stage="vegetative", plant_week=4, now=NOW)
             if a.key == "water_change_due"]
    assert found and "30%" in found[0].detail and "18 of the last 24 h" in found[0].detail
    assert "water_change_due" not in _keys(_state(solution={"status": "ok"}))
    assert "water_change_soon" in _keys(_state(solution={"status": "watch", "watch": "age",
                                                         "age_d": 22}))
    assert not [k for k in _keys(_state()) if k.startswith("water_change")]  # older firmware


def test_nutrient_no_response_alert(monkeypatch):
    monkeypatch.setenv("SMART_GARDEN_SITE", "admin")
    health = {"ph": "ok", "tds": "ok", "nutrient_pump": "fault"}
    keys = _keys(_state(nutrient_no_response=True, health=health))
    assert "no_response.nutrient" in keys and "actuator_fault.nutrient_pump" not in keys
    found = [a for a in alerts.evaluate(_state(nutrient_no_response=True, health=health),
                                        node_live=True, suggested_stage="vegetative",
                                        plant_week=4, now=NOW) if a.key == "no_response.nutrient"]
    # The controller has stopped that loop until an admin clears it.
    assert found[0].severity == "fault"
    assert "Nutrient dosing stopped" == found[0].title
    assert "PRIME NUTRIENT" in found[0].action and "stopped" in found[0].detail


def test_no_response_note_is_admin_only(monkeypatch):
    monkeypatch.setenv("SMART_GARDEN_SITE", "public")
    health = {"ph": "ok", "tds": "ok", "ph_down_pump": "ok"}
    keys = _keys(_state(ph_down_no_response=True, nutrient_no_response=True, health=health))
    assert not [k for k in keys if k.startswith("no_response")]


def test_refill_hold_done_and_paused_loops(tmp_path):
    held = _state(refill={"hold": True, "hold_reason": "noisy_level"})
    found = [a for a in alerts.evaluate(held, node_live=True, suggested_stage="vegetative",
                                        plant_week=4, now=NOW) if a.key == "refill.hold"]
    assert found and found[0].severity == "warning" and "jumped" in found[0].detail
    assert "Proceed" in found[0].action
    done = _state(refill={"last_done_min_ago": 5, "last_delivered_ml": 612, "last_minutes": 14})
    assert "refill.done" in _keys(done)
    assert "refill.done" not in _keys(_state(refill={"last_done_min_ago": 45}))
    paused = _keys(_state(loops_paused={"level": True, "nutrient": False, "ph": False}))
    assert "loop_paused.level" in paused and "loop_paused.nutrient" not in paused
    # "Refill done" is pushed although it is only info; a refill on hold is pushed as a warning.
    tracker = AlertTracker(tmp_path / "state.json")
    sent = tracker.update(alerts.evaluate(done, node_live=True, suggested_stage="vegetative",
                                          plant_week=4, now=NOW), now=NOW)
    assert any(kind == "new" and a.key == "refill.done" for kind, a in sent)
