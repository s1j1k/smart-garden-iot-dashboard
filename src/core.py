"""Session state, MQTT access and formatting helpers shared by every page."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import streamlit as st

import alerts
import garden_settings
from config import DEFAULT_CONFIG, INITIAL_STATE, normalize_stage_name, seedling_age, \
    suggested_grow_stage
from history_loader import HistoryRestore, restore_history
from logger import log_state
from sensor_ingest import reading_to_state, shared_mqtt_client

# Auto Refresh cadence per section. Telemetry arrives about every 10 s;
# 2 s keeps the countdowns and pump lights current without redrawing charts.
LIVE_REFRESH_S = 2
CHART_REFRESH_S = 30
CAMERA_REFRESH_S = 15

# Unique per process, so two dashboard processes on one broker do not kick each other.
_PROCESS_CLIENT_ID = f"smart-garden-dashboard-{uuid.uuid4().hex[:8]}"


def init_session():
    if "config" not in st.session_state:
        config = DEFAULT_CONFIG.copy()
        # One ID per dashboard process, shared by every browser session: the
        # client is process-wide (shared_mqtt_client), and a per-session ID
        # made each session's rerun stop the other's client, so with two tabs
        # open neither ever saw a message and the node showed offline.
        config["mqtt_client_id"] = _PROCESS_CLIENT_ID
        st.session_state.config = config
    config = st.session_state.config
    config["data_source"] = "MQTT"
    config.setdefault("mqtt_cmd_topic", DEFAULT_CONFIG.get("mqtt_cmd_topic"))
    config.setdefault("mqtt_device_id", DEFAULT_CONFIG.get("mqtt_device_id"))

    st.session_state.setdefault("state", INITIAL_STATE.copy())
    st.session_state.setdefault("history", [])
    # Position in the shared client's reading log already shown in this session.
    st.session_state.setdefault("mqtt_cursor", None)
    st.session_state.setdefault("mqtt_readings_received", 0)
    st.session_state.setdefault("last_cmd_status", "")

    if not st.session_state.get("history_restored"):
        try:
            restored = shared_history(config)
        except Exception as exc:  # noqa: BLE001 - a broken store must not stop the page
            restored = HistoryRestore(records=[], latest=None,
                                      note=f"Stored history could not be loaded ({exc}).")
        # Stored history is shared by every viewer (shared_history); a session only
        # keeps the live readings it receives itself, so 100 viewers do not hold
        # 100 copies of three days of readings.
        st.session_state.history = []
        st.session_state.history_note = restored.note
        st.session_state.history_count = len(restored.records)
        st.session_state.history_latest_at = (
            (restored.latest or {}).get("timestamp") if restored.latest else None
        )
        st.session_state.history_restored = True
        if restored.latest:
            st.session_state.state = reading_to_state(restored.latest, st.session_state.state)


HISTORY_SHARE_S = 30
PUBLIC_MIN_REFRESH_S = 10   # live sections on the public site refresh at most this often
SESSION_ROWS = 1080        # live readings a tab keeps (3 h at 10 s); older ones are in the shared history


def shared_history(config) -> HistoryRestore:
    """The stored history, read once per HISTORY_SHARE_S for all viewers."""
    keys = ("sqlite_path", "history_lookback_hours", "history_max_rows", "history_from_influx")
    return _shared_history(tuple((k, config.get(k)) for k in keys),
                           int(time.time() // HISTORY_SHARE_S))


@st.cache_resource(show_spinner=False, max_entries=2)
def _shared_history(config_items: tuple, bucket: int) -> HistoryRestore:
    """One object for every session (cache_resource: not copied per viewer).
    Read-only: sessions keep their own live readings in session_state.history."""
    return restore_history(dict(config_items))


def trend_history(config) -> list[dict]:
    """Shared stored history plus this session's live readings, for the charts."""
    return list(shared_history(config).records) + list(st.session_state.get("history") or [])


def record_reading(reading):
    if reading.get("replay"):
        # Recorded on the ESP32 while MQTT was down: add it to the charts at
        # its own time, but keep the live state on the newest reading.
        replayed = reading_to_state(reading, st.session_state.state)
        st.session_state.history = sorted(
            log_state(st.session_state.history, replayed, st.session_state.config),
            key=lambda row: row["time"],
        )
        return
    st.session_state.state = reading_to_state(reading, st.session_state.state)
    st.session_state.history = log_state(
        st.session_state.history, st.session_state.state, st.session_state.config)[-SESSION_ROWS:]
    st.session_state.mqtt_readings_received += 1


def get_mqtt_client(config):
    return shared_mqtt_client(config)


def drain_mqtt():
    """Record readings that arrived since this session last looked."""
    client = get_mqtt_client(st.session_state.config)
    cursor = st.session_state.mqtt_cursor
    if cursor is None or st.session_state.get("mqtt_client_id") != id(client):
        # New session or a new shared client: only readings from now on (the
        # history before this comes from SQLite via history_loader).
        st.session_state.mqtt_client_id = id(client)
        cursor = client.latest_cursor()
    cursor, readings = client.drain_since(cursor)
    st.session_state.mqtt_cursor = cursor
    for reading in readings:
        record_reading(reading)
    return client


def poll_mqtt():
    return drain_mqtt().status()


def metric_value(state, *keys, default=None):
    for key in keys:
        if key in state and state[key] is not None:
            return state[key]
    return default


def cmd_topic_for(config, state):
    device = metric_value(state, "device_id") or config.get("mqtt_device_id") or "garden-node-01"
    configured = config.get("mqtt_cmd_topic") or ""
    if configured and "garden-node-01" in configured and device != "garden-node-01":
        return f"smart-garden/{device}/cmd"
    return configured or f"smart-garden/{device}/cmd"


def publish_cmd(payload):
    client = get_mqtt_client(st.session_state.config)
    topic = cmd_topic_for(st.session_state.config, st.session_state.state)
    ok = client.publish_json(topic, payload)
    st.session_state.last_cmd_status = f"{'Sent' if ok else 'FAILED'} → `{topic}`: `{payload}`"
    return ok


def publish_cal_cmd(payload, wait_seconds=2.0):
    """Send a command, wait for ESP telemetry, refresh the UI."""
    ok = publish_cmd(payload)
    if ok and wait_seconds > 0:
        time.sleep(wait_seconds)
        drain_mqtt()
    st.rerun()


def live_context():
    """Drain new MQTT readings and return the values every live section uses."""
    status = poll_mqtt()
    state = st.session_state.state
    settings = garden_settings.load()
    planted = garden_settings.planted_at(settings)
    day, week, age_label = seedling_age(planted_at=planted)
    raw_stage = metric_value(state, "grow_stage", "system_mode")
    node_live = bool(status and status.get("node_live"))
    suggested = suggested_grow_stage(week)
    active = alerts.evaluate(
        state, node_live=node_live, suggested_stage=suggested, plant_week=week,
        last_seen_s=(status or {}).get("last_message_age_s"))
    return {
        "state": state,
        "mqtt_status": status,
        "node_live": node_live,
        "broker_up": bool(status and status.get("connected")),
        "has_live_data": st.session_state.mqtt_readings_received > 0,
        "show_stored": bool(st.session_state.get("history_count")),
        "settings": settings,
        "planted_at": planted,
        "plant_day": day,
        "plant_week": week,
        "age_label": age_label,
        "stage": normalize_stage_name(raw_stage) if raw_stage else None,
        "suggested_stage": suggested,
        "alerts": active,
    }


def run_live(section, every_s):
    """Rerun only ``section`` every ``every_s`` seconds while Auto Refresh is on.

    Whole-page reruns rebuilt every element each second, and sections whose
    content changed size pushed everything below them up and down.
    """
    auto = st.session_state.config.get("auto_refresh", True)
    if not _is_admin():
        # Each open page reruns its live sections on its own timer; on the
        # public site, with many visitors, a slower floor keeps the Pi's CPU free.
        every_s = max(every_s, PUBLIC_MIN_REFRESH_S)
    st.fragment(section, run_every=every_s if auto else None)()


def _is_admin() -> bool:
    from admin_auth import is_admin_site

    return is_admin_site()


def _control_sent(ok):
    st.session_state.control_send_error = (
        None if ok else "Could not send the command — is the broker up?")


def request_control_cycle(loop="all"):
    """Button callback: run every loop now ("all"), or one: "level", "nutrient", "ph".

    One loop needs firmware 1.9.28+; older firmware runs every loop.
    """
    cmd = {"cmd": "cycle"} if loop == "all" else {"cmd": "cycle", "loop": loop}
    ok = publish_cmd(cmd)
    if ok:
        st.session_state.setdefault("run_requested_at", {})[loop] = datetime.now(timezone.utc)
    _control_sent(ok)


def reset_dosing_session(loop):
    """Admin button callback: allow more dosing after a session cap (loop "ph" or "ec")."""
    if publish_cmd({"cmd": "session_reset", "loop": loop}):
        st.session_state.session_reset_error = None
    else:
        st.session_state.session_reset_error = "Could not send the command — is the broker up?"


def mark_water_changed():
    """Admin button callback: the solution was replaced, so its indicators restart."""
    if publish_cmd({"cmd": "water_changed"}):
        st.session_state.water_changed_error = None
    else:
        st.session_state.water_changed_error = "Could not send the command — is the broker up?"


def set_auto_control(paused):
    """Button callback: pause or resume automatic dosing on the ESP32."""
    _control_sent(publish_cmd({"cmd": "auto_pause" if paused else "auto_resume"}))


def set_loop_paused(loop, paused):
    """Admin button callback: pause or resume one control loop ("level", "nutrient", "ph").

    The other loops keep running in order (level, nutrient, pH). Firmware 1.9.24+.
    """
    _control_sent(publish_cmd({"cmd": "loop_pause", "loop": loop, "paused": bool(paused)}))


def proceed_refill():
    """Admin button callback: go ahead with a refill the ESP32 put on hold."""
    if publish_cmd({"cmd": "refill_proceed"}):
        st.session_state.refill_send_error = None
    else:
        st.session_state.refill_send_error = "Could not send the command — is the broker up?"


def set_light_makeup(action):
    """Button callback: start ("on") or stop ("off") repaying missed grow-light hours."""
    if publish_cmd({"cmd": "light_makeup", "action": action}):
        st.session_state.light_makeup_send_error = None
    else:
        st.session_state.light_makeup_send_error = (
            "Could not send the command — is the broker up?")


def set_power(action):
    """Admin button callback: the panel ON/OFF switch over MQTT ("on", "off" or "status")."""
    if publish_cmd({"cmd": "power", "action": action}):
        st.session_state.power_send_error = None
        st.session_state.power_sent = (action, time.monotonic())
    else:
        # Kept until the next send: the card redraws every few seconds.
        st.session_state.power_send_error = "Could not send the command — is the broker up?"
        st.session_state.power_sent = None


def set_light_on(hours):
    """Button callback: grow light on now for ``hours``; None goes back to the schedule."""
    payload = ({"cmd": "light_auto"} if hours is None
               else {"cmd": "light_on", "hours": round(float(hours), 2)})
    if publish_cmd(payload):
        st.session_state.light_on_send_error = None
    else:
        st.session_state.light_on_send_error = "Could not send the command — is the broker up?"


def fmt_number(value, digits=1):
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def fmt_on_off(value):
    if value is None:
        return "—"
    return "ON" if value else "OFF"


def fmt_bool(value):
    if value is None:
        return "—"
    return "yes" if value else "no"


def fmt_seconds(seconds):
    if seconds is None:
        return "—"
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if seconds <= 0:
        return "off"
    if seconds >= 3600:
        hours = seconds / 3600.0
        return f"{hours:.0f} h" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f} h"
    if seconds >= 60:
        mins = seconds / 60.0
        return f"{mins:.0f} min" if abs(mins - round(mins)) < 0.05 else f"{mins:.1f} min"
    return f"{seconds:.0f} s"


def fmt_stock(remaining, capacity):
    if remaining is None and capacity is None:
        return "—"
    rem, cap = fmt_number(remaining, 0), fmt_number(capacity, 0)
    if remaining is not None and capacity not in (None, 0):
        try:
            return f"{rem} / {cap} mL ({100.0 * float(remaining) / float(capacity):.0f}%)"
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return f"{rem} / {cap} mL"
