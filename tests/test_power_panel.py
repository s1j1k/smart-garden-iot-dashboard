"""Unit checks for the garden power card and the power ack plumbing."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from power_panel import ACK_WAIT_S, power_state, waiting_line  # noqa: E402
from sensor_ingest import ack_topic_for, parse_power_ack, power_ack_with_age  # noqa: E402


def _ack(state, age_s, ok=True, message=""):
    return {"cmd": "power", "ok": ok, "state": state, "message": message, "age_s": age_s}


def test_live_telemetry_is_on():
    kind, title, _ = power_state(True, 5.0, None)
    assert (kind, title) == ("on", "On")


def test_off_ack_newer_than_telemetry_shows_standby_while_still_live():
    kind, _, detail = power_state(True, 40.0, _ack("standby", 3.0))
    assert kind == "standby"
    assert "Pumps stopped" in detail


def test_older_standby_ack_gives_way_to_telemetry():
    kind, _, _ = power_state(True, 5.0, _ack("standby", 600.0))
    assert kind == "on"


def test_refused_on_shows_the_fault():
    ack = _ack("standby", 2.0, ok=False, message="panel button stuck or shorted at power-up")
    kind, _, detail = power_state(False, None, ack)
    assert kind == "fault"
    assert "panel button stuck" in detail


def test_on_ack_before_first_reading_is_on():
    kind, _, _ = power_state(False, None, _ack("on", 1.0))
    assert kind == "on"


def test_no_telemetry_and_no_ack_is_unknown():
    kind, title, detail = power_state(False, 300.0, None)
    assert kind == "unknown"
    assert "5 min" in title
    assert "Check" in detail


def test_waiting_until_the_ack_arrives():
    sent = ("on", 100.0)
    assert "waiting" in waiting_line(sent, None, now=105.0)
    # Ack 2 s old, command sent 5 s ago: answered.
    assert waiting_line(sent, _ack("on", 2.0), now=105.0) is None
    # Ack older than the command does not count.
    assert "waiting" in waiting_line(sent, _ack("standby", 50.0), now=105.0)
    assert "No answer" in waiting_line(sent, None, now=100.0 + ACK_WAIT_S + 1)
    assert waiting_line(None, None) is None


def test_ack_topic_follows_the_telemetry_pattern():
    assert ack_topic_for("smart-garden/+/telemetry") == "smart-garden/+/ack"
    assert ack_topic_for("smart-garden/#") is None


def test_only_power_acks_are_kept():
    assert parse_power_ack(b'{"cmd":"power","ok":true,"state":"on"}')["state"] == "on"
    assert parse_power_ack(b'{"cmd":"light_flash","flash":true}') is None
    assert parse_power_ack(b"not json") is None


def test_ack_age():
    out = power_ack_with_age({"cmd": "power", "state": "on", "at": 10.0}, now=14.5)
    assert out["age_s"] == 4.5 and "at" not in out
    assert power_ack_with_age(None) is None
