"""Settings: everyday options, plus the Advanced area for set-up and diagnostics."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import garden_settings
from config import GROW_PRESETS, STAGE_ORDER
from core import (
    LIVE_REFRESH_S,
    fmt_bool,
    fmt_number,
    fmt_on_off,
    fmt_seconds,
    live_context,
    metric_value,
    request_control_cycle,
    mark_water_changed,
    reset_dosing_session,
    run_live,
    proceed_refill,
    set_auto_control,
    set_light_on,
    set_loop_paused,
)
from grow_light_panel import last_on_window, missed_light_s, render_light_on
from plant_catalogue import load_plants
from sqlite_store import load_recent_sensor_readings
from services import SERVICES, restart_hint, service_states
from status_panel import render_control, render_refill, render_session_limits, \
    render_solution, render_status_panel
from views.calibration import render_calibration_tabs
from views.stage import STAGE_EMOJI, stage_name
from fmt_range import fmt_range


def stage_threshold_rows(preset, live_state=None):
    """Build advanced threshold table rows from preset (+ optional live MQTT)."""
    live = live_state or {}

    def live_or(key, preset_key=None):
        preset_key = preset_key or key
        value = live.get(key)
        if value is not None:
            return value
        return preset.get(preset_key)

    rows = [
        ("Air-gap target (mm)", fmt_number(live_or("target_drawdown_mm"), 0)),
        ("Drawdown tolerance (mm)", fmt_number(live_or("drawdown_tolerance_mm"), 0)),
        ("Level control", fmt_bool(live_or("level_control_enabled", "water_control_enabled"))),
        ("Level check interval", fmt_seconds(live_or("level_check_s"))),
        ("pH control", fmt_bool(live_or("ph_control_enabled"))),
        ("pH setpoint", fmt_number(live_or("ph_center"), 2)),
        ("pH acceptable (no dosing inside)", fmt_range(live_or('ph_dead_low'), live_or('ph_dead_high'), 2, sep=' – ')),
        ("pH target band", fmt_range(live_or('ph_target_min', 'ph_min'), live_or('ph_target_max', 'ph_max'), 2, sep=' – ')),
        ("pH dose (min – hard max mL)", fmt_range(live_or('ph_min_dose_ml'), live_or('ph_max_dose_ml'), 2, sep=' – ')),
        ("pH settle", fmt_seconds(live_or("ph_settle_s"))),
        ("pH session cap (mL)", fmt_number(live_or("ph_session_cap_ml"), 1)),
        ("pH check interval", fmt_seconds(live_or("ph_check_s"))),
        ("EC / nutrient control", fmt_bool(live_or("ec_control_enabled", "ec_control_enabled"))),
        ("TDS target (ppm)", fmt_range(live_or('nutrient_tds_min'), live_or('nutrient_tds_max'), 0, sep=' – ')),
        ("TDS acceptable (no dosing inside, ppm)", fmt_range(live_or('ec_dead_low'), live_or('ec_dead_high'), 0, sep=' – ')),
        ("Nutrient dose (min – hard max mL)", fmt_range(live_or('ec_min_dose_ml'), live_or('ec_max_dose_ml'), 2, sep=' – ')),
        ("EC settle", fmt_seconds(live_or("ec_settle_s"))),
        ("EC check interval", fmt_seconds(live_or("ec_check_s"))),
        ("Nutrient dosing enabled", fmt_bool(preset.get("nutrient_dosing_enabled"))),
        ("Nutrient dilution enabled", fmt_bool(preset.get("nutrient_dilution_enabled"))),
    ]
    return rows


def _general():
    settings = garden_settings.load()
    planted = garden_settings.planted_at(settings)
    st.markdown("#### 🌱 My plants")
    with st.form("garden_form"):
        c1, c2 = st.columns(2)
        day = c1.date_input("Planted on", value=planted.date())
        clock = c2.time_input("at", value=planted.time().replace(tzinfo=None))
        try:
            options = {p.id: p.common_name for p in load_plants()}
        except Exception:  # noqa: BLE001 - the catalogue is optional here
            options = {pid: pid.title() for pid in settings["plants"]}
        plants = st.multiselect("Growing", options=list(options),
                                default=[p for p in settings["plants"] if p in options],
                                format_func=options.get)
        if st.form_submit_button("Save", type="primary"):
            when = datetime.combine(day, clock).replace(tzinfo=planted.tzinfo)
            garden_settings.save({"planted_at": when.isoformat(), "plants": plants})
            st.success("Saved. Plant age and stage suggestions now count from this date.")
    st.caption("Plant age drives the suggested grow stage and the stage-change reminders.")

    st.markdown("#### 🔄 Display")
    config = st.session_state.config
    config["auto_refresh"] = st.toggle("Update live readings automatically",
                                       value=config["auto_refresh"])

    st.markdown("#### 📱 Install as an app")
    st.markdown(
        "- **Android (Chrome):** menu ⋮ → *Install app* / *Add to Home screen*.\n"
        "- **iPhone / iPad (Safari):** Share → *Add to Home Screen*, then open it from the "
        "icon (needed for notifications on iOS).\n"
        "- **Computer (Chrome / Edge):** the install icon at the right of the address bar.\n\n"
        "Installing needs the dashboard on **HTTPS** (or `localhost`) — see the README.")


def _thresholds(ctx):
    state, live_stage = ctx["state"], ctx["stage"]
    labels = {key: GROW_PRESETS[key]["label"] for key in STAGE_ORDER}
    view_stage = st.selectbox(
        "Show thresholds for", options=STAGE_ORDER,
        index=STAGE_ORDER.index(live_stage) if live_stage in STAGE_ORDER else 0,
        format_func=lambda key: f"{stage_name(key)} — {labels[key]}",
        key="stage_threshold_view")
    preset = GROW_PRESETS[view_stage]
    use_live = (ctx["has_live_data"] and live_stage == view_stage
                and metric_value(state, "ph_target_min") is not None)
    st.caption("Live values from the controller" if use_live
               else "Preset table (matches `shared/grow_stage.h`).")
    st.dataframe(pd.DataFrame(stage_threshold_rows(preset, state if use_live else None),
                              columns=["Parameter", "Value"]), hide_index=True, width="stretch")
    st.markdown("**All stages**")
    compare = [{
        "Stage": f"{STAGE_EMOJI.get(key, '🪴')} {key}",
        "Drawdown mm": p["target_drawdown_mm"],
        "pH (acceptable)": f"{p['ph_center']:.1f} ({fmt_range(p['ph_dead_low'], p['ph_dead_high'], 1)})",
        "TDS ppm": fmt_range(p['nutrient_tds_min'], p['nutrient_tds_max'], 0),
        "pH ctrl": "yes" if p["ph_control_enabled"] else "no",
        "EC ctrl": "yes" if p["ec_control_enabled"] else "no",
        "pH dose mL": fmt_range(p['ph_min_dose_ml'], p['ph_max_dose_ml'], 2),
        "Session cap mL": p["ph_session_cap_ml"],
    } for key, p in ((k, GROW_PRESETS[k]) for k in STAGE_ORDER)]
    st.dataframe(pd.DataFrame(compare), hide_index=True, width="stretch")


def _services():
    states = service_states()
    if not states:
        return
    st.markdown("**🖥️ Services on this computer**")
    for unit, label in SERVICES.items():
        state = states.get(unit, "unknown")
        icon = "🟢" if state == "active" else "🔴"
        hint = "" if state == "active" else f" — restart: `{restart_hint(unit)}`"
        st.markdown(f"{icon} {label} (`{unit}`): **{state}**{hint}")


def _device_status():
    ctx = live_context()
    _services()
    render_status_panel(ctx["state"], ctx["node_live"])
    render_control(ctx["state"], ctx["node_live"], set_auto_control, set_loop_paused,
                   request_control_cycle, requested=st.session_state.get("run_requested_at"),
                   send_error=st.session_state.pop("control_send_error", None))
    render_refill(ctx["state"], ctx["node_live"], proceed_refill,
                  send_error=st.session_state.pop("refill_send_error", None))
    render_session_limits(ctx["state"], ctx["node_live"], reset_dosing_session,
                          send_error=st.session_state.pop("session_reset_error", None))
    render_solution(ctx["state"], ctx["node_live"], mark_water_changed,
                    send_error=st.session_state.pop("water_changed_error", None))
    render_light_on(ctx["state"], ctx["node_live"], set_light_on,
                    missed=_missed_light(ctx["state"]),
                    send_error=st.session_state.pop("light_on_send_error", None))


@st.cache_data(ttl=60, show_spinner=False)
def _message_times(sqlite_path, since_iso):
    rows = load_recent_sensor_readings(sqlite_path, since_iso=since_iso, limit=20000)
    times = pd.to_datetime([r.get("timestamp") for r in rows], utc=True, errors="coerce",
                           format="ISO8601")
    return [t.to_pydatetime() for t in times if not pd.isna(t)]


def _missed_light(state):
    """(seconds, "current" | "last") of light missed while the controller was off,
    from gaps in the stored telemetry; None if it cannot be worked out."""
    window = last_on_window(state.get("grow_light") or {}, state.get("received_at"))
    sqlite_path = st.session_state.config.get("sqlite_path")
    if window is None or not sqlite_path:
        return None
    start, end, which = window
    since = (start - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return missed_light_s(_message_times(sqlite_path, since), start, end), which


def _calibration():
    ctx = live_context()
    render_calibration_tabs(ctx["state"], ctx["has_live_data"])


def _connection():
    ctx = live_context()
    config = st.session_state.config
    status = ctx["mqtt_status"] or {}
    firmware = ctx["state"].get("firmware_version")
    if ctx["node_live"]:
        st.success(f"Controller live (firmware {firmware or '?'}) via "
                   f"{config['mqtt_host']}:{config['mqtt_port']}.")
    elif ctx["broker_up"]:
        st.warning("Broker is up but the controller is not sending — type `MQTT CONNECT` on "
                   "its serial console or power-cycle it.")
    else:
        st.warning("MQTT broker is down — live data unavailable; stored history still loads.")
    for err in (status.get("errors") or [])[-3:]:
        st.error(err)
    if st.session_state.get("history_note"):
        st.caption(st.session_state.history_note)
    config["mqtt_host"] = st.text_input("Broker host", value=config["mqtt_host"],
                                        help="localhost on the Pi or this PC.")
    config["mqtt_port"] = int(st.number_input("Port", min_value=1, max_value=65535,
                                              value=int(config["mqtt_port"]), step=1))
    config["mqtt_topic"] = st.text_input("Telemetry topic", value=config["mqtt_topic"])
    config["mqtt_cmd_topic"] = st.text_input("Command topic", value=config["mqtt_cmd_topic"])
    config["mqtt_client_id"] = st.text_input("Client ID", value=config["mqtt_client_id"],
                                             help="Unique per browser tab.")
    config["mqtt_tls"] = st.toggle("Use TLS", value=config["mqtt_tls"])
    st.caption("The ESP32's `MQTT_BROKER_HOST` must be this computer's WiFi LAN address "
               "(`./scripts/print-broker-ip-hint.sh`).")


def _data():
    ctx = live_context()
    state, config = ctx["state"], st.session_state.config
    log_path = Path(config["sensor_log_path"])
    st.caption(f"Readings are stored in `{config.get('sqlite_path')}` and `{log_path}`.")
    if log_path.exists():
        st.download_button("Download readings (CSV)", data=log_path.read_bytes(),
                           file_name=log_path.name, mime="text/csv")
    if st.button("Reload stored history"):
        st.session_state.history_restored = False
        st.session_state.history = []
        st.rerun()
    sensors = {
        "Firmware": state.get("firmware_version") or "—",
        "pH filtered / raw": f"{fmt_number(metric_value(state, 'ph_filtered', 'ph'), 3)} / "
                             f"{fmt_number(state.get('ph_raw'), 3)}",
        "pH control (3-min average)": fmt_number(state.get("ph_ema"), 3),
        "pH board voltage (V)": fmt_number(state.get("ph_voltage"), 4),
        "pH slope / offset": f"{fmt_number(state.get('ph_slope'), 4)} / "
                             f"{fmt_number(state.get('ph_offset'), 4)}",
        "TDS burst / control (ppm)": f"{fmt_number(state.get('tds_ppm'), 0)} / "
                                     f"{fmt_number(state.get('tds_ppm_ema'), 0)}",
        "Water level (%)": fmt_number(state.get("water_level_pct"), 1),
        "Light sensor (ADC)": fmt_number(state.get("light_adc"), 0),
        "Hydroponic mode": state.get("hydroponic_mode") or "—",
    }
    actuators = {
        "Water top-up pump": fmt_on_off(metric_value(state, "water_pump_on", "pump_on")),
        "Reverse drain": fmt_on_off(state.get("drain_pump_on")),
        "Nutrient pump": fmt_on_off(state.get("nutrient_pump_on")),
        "pH-up pump": fmt_on_off(state.get("ph_up_pump_on")),
        "pH-down pump": fmt_on_off(state.get("ph_down_pump_on")),
        "pH dose state": state.get("ph_dose") or "—",
        "Nutrient dose state": state.get("nutrient_dose") or "—",
    }
    c1, c2 = st.columns(2)
    c1.dataframe(pd.DataFrame(sensors.items(), columns=["Reading", "Value"]),
                 hide_index=True, width="stretch")
    c2.dataframe(pd.DataFrame(actuators.items(), columns=["Actuator", "State"]),
                 hide_index=True, width="stretch")


def page():
    st.title("⚙️ Settings")
    general, advanced = st.tabs(["General", "Advanced"])
    with general:
        _general()
    with advanced:
        st.caption("Set-up and diagnostics: calibration, device health, connection and raw data.")
        status_tab, cal_tab, thr_tab, conn_tab, data_tab = st.tabs(
            ["Device status", "Calibration", "Stage thresholds", "Connection", "Data"])
        with status_tab:
            run_live(_device_status, LIVE_REFRESH_S)
        with cal_tab:
            run_live(_calibration, LIVE_REFRESH_S)
        with thr_tab:
            _thresholds(live_context())
        with conn_tab:
            _connection()
        with data_tab:
            run_live(_data, LIVE_REFRESH_S)
