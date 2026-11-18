import csv
import json
import math
import os
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from config import (
    GROW_PRESETS,
    WATER_LEVEL_MAX_PERCENT,
    DEFAULT_GROW_STAGE,
    normalize_stage_name,
)
from sensors_fake import clamp

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - exercised only when dependency is absent
    mqtt = None


# Hydroponic sensor fields published by the ESP32 garden node.
SENSOR_FIELDS = [
    "ambient_temp_c",
    "ambient_humidity_pct",
    "water_temp_c",
    "light_adc",
    "water_level_pct",
    "tds_ppm",
    "ph",
    "co2_ppm",
]

# Firmware 1.8.0+: every reading carries (boot_id, seq), the same identity in
# the ESP32 journal, live MQTT, outbox replay and Influx. reading_key is
# "<device_id>/<boot_id>/<seq>"; SQLite keeps one row per key.
IDENTITY_FIELDS = [
    "kind",
    "boot_id",
    "seq",
    "uptime_ms",
    "reading_key",
]


def reading_key(payload):
    """Stable identity of one ESP32 message, or None for older firmware."""
    if not isinstance(payload, dict):
        return None
    device = payload.get("device_id")
    boot_id = payload.get("boot_id")
    if not device or boot_id in (None, ""):
        return None
    if payload.get("kind") == "control_event" and payload.get("event_seq") not in (None, ""):
        return f"{device}/{int(boot_id)}/e{int(payload['event_seq'])}"
    seq = payload.get("seq")
    if seq in (None, "", 0):
        return None  # status snapshots have no seq: every one is distinct
    return f"{device}/{int(boot_id)}/{int(seq)}"


OPTIONAL_META_FIELDS = [
    "grow_stage",
    "system_mode",
    "stage_persisted",
    "control_idle",
    "hydroponic_mode",
    "ph_settle_s",
    "control_law",
    "pumps_calibrated",
    "water_target_pct",
    "water_max_pct",
    "target_drawdown_mm",
    "current_drawdown_mm",
    "tank_volume_l",
    "drawdown_tolerance_mm",
    "fill_0_pct",
    "level_check_s",
    "ph_check_s",
    "ec_check_s",
    "ec_control_enabled",
    "ph_control_enabled",
    "level_control_enabled",
    "nutrient_tds_min",
    "nutrient_tds_max",
    "ec_dead_low",
    "ec_dead_high",
    "ec_min_dose_ml",
    "ec_max_dose_ml",
    "ec_settle_s",
    "ph_target_min",
    "ph_target_max",
    "ph_dead_low",
    "ph_dead_high",
    "ph_center",
    "ph_min_dose_ml",
    "ph_max_dose_ml",
    "ph_session_cap_ml",
    "ph_baseline_recheck_s",
    "ph_dose",
    "nutrient_dose",
    "ph_voltage",
    "ph_raw",
    "ph_filtered",
    "ph_slope",
    "ph_offset",
    "ph_probe_calibrated",
    "ph_cal_active",
    "ph_cal_state",
    "ph_cal_message",
    "ph_cal_ok",
    "ph_cal_acid_v",
    "ph_cal_alkali_v",
    "ph_cal_stored_acid_v",
    "ph_cal_stored_alkali_v",
    "water_calibrated",
    "nutrient_calibrated",
    "ph_up_calibrated",
    "ph_down_calibrated",
    "water_ml_per_s",
    "nutrient_ml_per_burst",
    "ph_up_ml_per_burst",
    "ph_down_ml_per_burst",
    "pump_cal_active",
    "pump_cal_state",
    "pump_cal_pump",
    "pump_cal_message",
    "pump_cal_ok",
    "pump_cal_mode",
    "pump_cal_run",
    "pump_cal_runs_needed",
    "pump_cal_bursts",
    "pump_cal_pulse_ms",
    "pump_cal_target_bursts",
    "pump_cal_target_ms",
    "pump_cal_last_sample",
    "pump_cal_unit",
    "water_cap_ml",
    "water_rem_ml",
    "water_empty",
    "nutrient_cap_ml",
    "nutrient_rem_ml",
    "nutrient_empty",
    "ph_up_cap_ml",
    "ph_up_rem_ml",
    "ph_up_empty",
    "ph_down_cap_ml",
    "ph_down_rem_ml",
    "ph_down_empty",
    "stock_alert",
]

ACTUATOR_FIELDS = [
    "water_pump_on",
    "drain_pump_on",
    "nutrient_pump_on",
    "ph_up_pump_on",
    "ph_down_pump_on",
]

# Per-message status from firmware 1.3+ (see status_panel.py). Replaced on every
# reading so a cleared fault never lingers; not written to CSV / SQLite columns
# (the raw MQTT payload table keeps them).
STATUS_FIELDS = [
    "health",
    "health_msg",
    "sched",
    "solution",
    "alerts",
    "auto_control_paused",
    "ph_sensor_fault",
    "tds_sensor_fault",
    "ph_up_no_response",
    "ph_down_no_response",
    "nutrient_no_response",
    "nutrient_unanswered_doses",
    "ph_up_unanswered_doses",
    "ph_down_unanswered_doses",
    "ph_session_ml",
    "ec_session_ml",
    "ec_session_cap_ml",
    "water_level_sensor_fault",
    "water_level_init_required",
    "water_level_saved_pct",
    "ph_ema",
    "tds_ppm_ema",
    "received_at",
    "firmware_version",
    "influx",
    "grow_light",
]

# Planned hardware — accepted when present, otherwise left as None (not received).
PLACEHOLDER_ACTUATOR_FIELDS = [
    "led_on",
    "fan_on",
]

# ESP32 firmware uses these sentinels when a sensor read fails.
INVALID_WATER_TEMP_C = -127.0
INVALID_SENSOR_FLAG = -1.0


def sanitize_sensor_value(field, value):
    """Map ESP32 failure sentinels to None so the UI can show N/A."""
    if value is None:
        return None
    if field == "water_temp_c" and value <= INVALID_WATER_TEMP_C + 0.01:
        return None
    if field == "ambient_humidity_pct" and value <= INVALID_SENSOR_FLAG + 0.01:
        return None
    if field == "water_level_pct" and value < 0:
        return None
    if field == "co2_ppm" and value < 0:
        return None
    if field == "ambient_temp_c" and value <= INVALID_WATER_TEMP_C + 0.01:
        return None
    return value

# Must stay aligned with src/schema.sql (minus the SQLite id column).
# Empty CSV cells are SQL NULL. Booleans are stored as 0/1.
LOG_FIELDS = [
    "received_at",
    "source",
    "device_id",
    "timestamp",
    *IDENTITY_FIELDS,
    *OPTIONAL_META_FIELDS,
    *SENSOR_FIELDS,
    *ACTUATOR_FIELDS,
    *PLACEHOLDER_ACTUATOR_FIELDS,
]

STOCK_EMPTY_FIELDS = (
    "water_empty",
    "nutrient_empty",
    "ph_up_empty",
    "ph_down_empty",
)

BOOLEAN_LOG_FIELDS = set(ACTUATOR_FIELDS + PLACEHOLDER_ACTUATOR_FIELDS + list(STOCK_EMPTY_FIELDS))


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    """SQLite-friendly ISO-8601 UTC (YYYY-MM-DDTHH:MM:SSZ)."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def format_iso_utc(value):
    """Normalize a datetime or epoch/ISO string to YYYY-MM-DDTHH:MM:SSZ."""
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str):
        if value.startswith("millis:"):
            return iso_now()
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return format_iso_utc(dt)
        except ValueError:
            return iso_now()
    return iso_now()


def parse_timestamp(value):
    return format_iso_utc(value)


def sqlite_csv_cell(field, value):
    """Format one CSV cell so sqlite3 / pandas can import it cleanly."""
    if value is None or value == "":
        return ""
    if field in BOOLEAN_LOG_FIELDS:
        return "1" if bool(value) else "0"
    if field in {"received_at", "timestamp"}:
        return format_iso_utc(value)
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime):
        return format_iso_utc(value)
    return value


def parse_sim_time(timestamp_value):
    """Convert a reading timestamp into a naive datetime for dashboard state."""
    if isinstance(timestamp_value, datetime):
        return timestamp_value.replace(tzinfo=None) if timestamp_value.tzinfo else timestamp_value

    if isinstance(timestamp_value, (int, float)):
        return datetime.fromtimestamp(timestamp_value, tz=timezone.utc).replace(tzinfo=None)

    if isinstance(timestamp_value, str):
        if timestamp_value.startswith("millis:"):
            return utc_now().replace(tzinfo=None)
        try:
            return datetime.fromisoformat(timestamp_value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return utc_now().replace(tzinfo=None)

    return utc_now().replace(tzinfo=None)


def coerce_float(payload, *keys, default=None):
    for key in keys:
        if key in payload and payload[key] is not None:
            try:
                return float(payload[key])
            except (TypeError, ValueError):
                continue
    return default


def coerce_bool(payload, *keys, default=False):
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "on", "yes"}
        return bool(value)
    return default


def coerce_optional_bool(payload, *keys):
    """Return bool if any key is present, otherwise None (field not received)."""
    if not any(key in payload for key in keys):
        return None
    return coerce_bool(payload, *keys)


def coerce_str(payload, *keys, default=""):
    for key in keys:
        if key in payload and payload[key] is not None:
            return str(payload[key])
    return default


def _tds_from_payload(payload):
    tds = coerce_float(payload, "tds_ppm", "tds", "tdsPPM")
    if tds is not None:
        return tds

    # Legacy EC (mS/cm) → approximate TDS ppm using the common 500 scale.
    ec = coerce_float(payload, "ec", "ec_ms_cm")
    if ec is not None:
        return ec * 500.0
    return None


def _dict_of_str(value):
    if not isinstance(value, dict):
        return None
    return {str(k): str(v) for k, v in value.items() if v is not None}


def _grow_light_from_payload(payload):
    """Photoperiod fields (firmware 1.5+; want_on / off_utc / next_change_s 1.6.3+)."""
    if "grow_light_on" not in payload and "grow_light_mode" not in payload:
        return None
    return {
        "on": coerce_optional_bool(payload, "grow_light_on"),
        "want_on": coerce_optional_bool(payload, "grow_light_want_on"),
        "mode": coerce_str(payload, "grow_light_mode", default=""),
        "brightness_pct": coerce_float(payload, "grow_light_brightness_pct"),
        "on_hours": coerce_float(payload, "grow_light_photoperiod_h"),
        "start_utc": coerce_float(payload, "grow_light_schedule_start_utc"),
        "off_utc": coerce_float(payload, "grow_light_schedule_off_utc"),
        "clock": coerce_optional_bool(payload, "grow_light_ntp_schedule"),
        "next_change_s": coerce_float(payload, "grow_light_next_change_s"),
        "flash": coerce_optional_bool(payload, "grow_light_flash"),
        # Make-up light for scheduled hours missed while the garden was off (1.9.23+).
        "makeup": coerce_optional_bool(payload, "grow_light_makeup"),
        "makeup_armed": coerce_optional_bool(payload, "grow_light_makeup_armed"),
        "makeup_owed_s": coerce_float(payload, "grow_light_makeup_owed_s"),
        "makeup_max_night_s": coerce_float(payload, "grow_light_makeup_max_night_s"),
        "manual": coerce_optional_bool(payload, "grow_light_manual"),
        "manual_left_s": coerce_float(payload, "grow_light_manual_left_s"),
    }


def normalize_reading(payload, source="mqtt", topic=None):
    if not isinstance(payload, dict):
        raise ValueError("MQTT payload must be a JSON object.")

    ambient_temp = coerce_float(
        payload,
        "ambient_temp_c",
        "temperature",
        "temperature_c",
        "temp_c",
        "ambient_temp",
    )
    ambient_humidity = coerce_float(
        payload,
        "ambient_humidity_pct",
        "humidity",
        "humidity_percent",
        "ambient_humidity",
    )
    water_temp = coerce_float(
        payload,
        "water_temp_c",
        "water_temperature",
        "water_temperature_c",
    )
    water_level = coerce_float(
        payload,
        "water_level_pct",
        "water_level",
        "water_level_percent",
    )
    co2 = coerce_float(payload, "co2_ppm", "co2", "CO2")

    reading = {
        "received_at": iso_now(),
        "source": source,
        "device_id": str(payload.get("device_id") or payload.get("device") or topic or "unknown"),
        "timestamp": parse_timestamp(payload.get("timestamp") or payload.get("ts")),
        "grow_stage": coerce_str(payload, "grow_stage", "stage", default="unknown"),
        "system_mode": coerce_str(payload, "system_mode", default=""),
        "stage_persisted": coerce_optional_bool(payload, "stage_persisted"),
        "control_idle": coerce_optional_bool(payload, "control_idle"),
        "hydroponic_mode": coerce_str(payload, "hydroponic_mode", default=""),
        "ph_settle_s": coerce_float(payload, "ph_settle_s", "ph_settle_seconds"),
        "control_law": coerce_str(payload, "control_law", default=""),
        "pumps_calibrated": coerce_float(payload, "pumps_calibrated"),
        "water_target_pct": coerce_float(payload, "water_target_pct", "water_target"),
        "water_max_pct": coerce_float(
            payload,
            "water_max_pct",
            "water_level_max_pct",
            default=WATER_LEVEL_MAX_PERCENT,
        ),
        "target_drawdown_mm": coerce_float(payload, "target_drawdown_mm"),
        "current_drawdown_mm": coerce_float(payload, "current_drawdown_mm"),
        "tank_volume_l": coerce_float(payload, "tank_volume_l"),
        "drawdown_tolerance_mm": coerce_float(payload, "drawdown_tolerance_mm"),
        "fill_0_pct": coerce_float(payload, "fill_0_pct"),
        "level_check_s": coerce_float(payload, "level_check_s"),
        "ph_check_s": coerce_float(payload, "ph_check_s"),
        "ec_check_s": coerce_float(payload, "ec_check_s"),
        "ec_control_enabled": coerce_optional_bool(payload, "ec_control_enabled"),
        "ph_control_enabled": coerce_optional_bool(payload, "ph_control_enabled"),
        "level_control_enabled": coerce_optional_bool(payload, "level_control_enabled"),
        "nutrient_tds_min": coerce_float(payload, "nutrient_tds_min", "tds_min"),
        "nutrient_tds_max": coerce_float(payload, "nutrient_tds_max", "tds_max"),
        "ec_dead_low": coerce_float(payload, "ec_dead_low"),
        "ec_dead_high": coerce_float(payload, "ec_dead_high"),
        "ec_min_dose_ml": coerce_float(payload, "ec_min_dose_ml"),
        "ec_max_dose_ml": coerce_float(payload, "ec_max_dose_ml"),
        "ec_settle_s": coerce_float(payload, "ec_settle_s"),
        "ph_target_min": coerce_float(payload, "ph_target_min", "ph_min"),
        "ph_target_max": coerce_float(payload, "ph_target_max", "ph_max"),
        "ph_dead_low": coerce_float(payload, "ph_dead_low"),
        "ph_dead_high": coerce_float(payload, "ph_dead_high"),
        "ph_center": coerce_float(payload, "ph_center"),
        "ph_min_dose_ml": coerce_float(payload, "ph_min_dose_ml"),
        "ph_max_dose_ml": coerce_float(payload, "ph_max_dose_ml"),
        "ph_session_cap_ml": coerce_float(payload, "ph_session_cap_ml"),
        "ph_baseline_recheck_s": coerce_float(payload, "ph_baseline_recheck_s"),
        "ph_dose": coerce_str(payload, "ph_dose", default="none"),
        "nutrient_dose": coerce_str(payload, "nutrient_dose", default="none"),
        "ph_voltage": coerce_float(payload, "ph_voltage", "ph_board_v"),
        "ph_raw": coerce_float(payload, "ph_raw"),
        "ph_filtered": coerce_float(payload, "ph_filtered"),
        "ph_slope": coerce_float(payload, "ph_slope"),
        "ph_offset": coerce_float(payload, "ph_offset"),
        "ph_probe_calibrated": coerce_optional_bool(payload, "ph_probe_calibrated"),
        "ph_cal_active": coerce_optional_bool(payload, "ph_cal_active"),
        "ph_cal_state": coerce_str(payload, "ph_cal_state", default=""),
        "ph_cal_message": coerce_str(payload, "ph_cal_message", default=""),
        "ph_cal_ok": coerce_optional_bool(payload, "ph_cal_ok"),
        "ph_cal_acid_v": coerce_float(payload, "ph_cal_acid_v"),
        "ph_cal_alkali_v": coerce_float(payload, "ph_cal_alkali_v"),
        "ph_cal_stored_acid_v": coerce_float(payload, "ph_cal_stored_acid_v"),
        "ph_cal_stored_alkali_v": coerce_float(payload, "ph_cal_stored_alkali_v"),
        "water_calibrated": coerce_optional_bool(payload, "water_calibrated"),
        "nutrient_calibrated": coerce_optional_bool(payload, "nutrient_calibrated"),
        "ph_up_calibrated": coerce_optional_bool(payload, "ph_up_calibrated"),
        "ph_down_calibrated": coerce_optional_bool(payload, "ph_down_calibrated"),
        "water_ml_per_s": coerce_float(payload, "water_ml_per_s"),
        "nutrient_ml_per_burst": coerce_float(payload, "nutrient_ml_per_burst"),
        "ph_up_ml_per_burst": coerce_float(payload, "ph_up_ml_per_burst"),
        "ph_down_ml_per_burst": coerce_float(payload, "ph_down_ml_per_burst"),
        "pump_cal_active": coerce_optional_bool(payload, "pump_cal_active"),
        "pump_cal_state": coerce_str(payload, "pump_cal_state", default=""),
        "pump_cal_pump": coerce_str(payload, "pump_cal_pump", default=""),
        "pump_cal_message": coerce_str(payload, "pump_cal_message", default=""),
        "pump_cal_ok": coerce_optional_bool(payload, "pump_cal_ok"),
        "pump_cal_mode": coerce_str(payload, "pump_cal_mode", default=""),
        "pump_cal_run": coerce_float(payload, "pump_cal_run"),
        "pump_cal_runs_needed": coerce_float(payload, "pump_cal_runs_needed"),
        "pump_cal_bursts": coerce_float(payload, "pump_cal_bursts"),
        "pump_cal_pulse_ms": coerce_float(payload, "pump_cal_pulse_ms"),
        "pump_cal_target_bursts": coerce_float(payload, "pump_cal_target_bursts"),
        "pump_cal_target_ms": coerce_float(payload, "pump_cal_target_ms"),
        "pump_cal_last_sample": coerce_float(payload, "pump_cal_last_sample"),
        "pump_cal_unit": coerce_str(payload, "pump_cal_unit", default=""),
        "water_cap_ml": coerce_float(payload, "water_cap_ml"),
        "water_rem_ml": coerce_float(payload, "water_rem_ml"),
        "water_empty": coerce_optional_bool(payload, "water_empty"),
        "nutrient_cap_ml": coerce_float(payload, "nutrient_cap_ml"),
        "nutrient_rem_ml": coerce_float(payload, "nutrient_rem_ml"),
        "nutrient_empty": coerce_optional_bool(payload, "nutrient_empty"),
        "ph_up_cap_ml": coerce_float(payload, "ph_up_cap_ml"),
        "ph_up_rem_ml": coerce_float(payload, "ph_up_rem_ml"),
        "ph_up_empty": coerce_optional_bool(payload, "ph_up_empty"),
        "ph_down_cap_ml": coerce_float(payload, "ph_down_cap_ml"),
        "ph_down_rem_ml": coerce_float(payload, "ph_down_rem_ml"),
        "ph_down_empty": coerce_optional_bool(payload, "ph_down_empty"),
        "stock_alert": coerce_str(payload, "stock_alert", default=""),
        "ambient_temp_c": sanitize_sensor_value("ambient_temp_c", ambient_temp),
        "ambient_humidity_pct": sanitize_sensor_value("ambient_humidity_pct", ambient_humidity),
        "water_temp_c": sanitize_sensor_value("water_temp_c", water_temp),
        "light_adc": coerce_float(
            payload,
            "light_adc",
            "ldr_adc",
            "light_intensity",
            "light_lux",
            "lux",
        ),
        "water_level_pct": sanitize_sensor_value("water_level_pct", water_level),
        "tds_ppm": _tds_from_payload(payload),
        "ph": coerce_float(payload, "ph", "pH", "ph_value"),
        "co2_ppm": sanitize_sensor_value("co2_ppm", co2),
        "water_pump_on": coerce_bool(payload, "water_pump_on", "pump_on"),
        "drain_pump_on": coerce_bool(payload, "drain_pump_on"),
        "nutrient_pump_on": coerce_bool(payload, "nutrient_pump_on"),
        "ph_up_pump_on": coerce_bool(payload, "ph_up_pump_on", "ph_up_on"),
        "ph_down_pump_on": coerce_bool(payload, "ph_down_pump_on", "ph_down_on"),
        # Placeholders for upcoming grow light / fan hardware.
        "led_on": coerce_optional_bool(
            payload,
            "led_on",
            "grow_light_on",
            "growlight_on",
        ),
        "fan_on": coerce_optional_bool(payload, "fan_on"),
        "health": _dict_of_str(payload.get("health")),
        "health_msg": _dict_of_str(payload.get("health_msg")),
        "sched": payload.get("sched") if isinstance(payload.get("sched"), dict) else None,
        "solution": payload.get("solution") if isinstance(payload.get("solution"), dict) else None,
        "alerts": [str(a) for a in payload.get("alerts") or [] if a],
        "auto_control_paused": coerce_optional_bool(payload, "auto_control_paused"),
        "ph_sensor_fault": coerce_optional_bool(payload, "ph_sensor_fault"),
        "tds_sensor_fault": coerce_optional_bool(payload, "tds_sensor_fault"),
        "ph_up_no_response": coerce_optional_bool(payload, "ph_up_no_response"),
        "ph_down_no_response": coerce_optional_bool(payload, "ph_down_no_response"),
        "nutrient_no_response": coerce_optional_bool(payload, "nutrient_no_response"),
        "nutrient_unanswered_doses": coerce_float(payload, "nutrient_unanswered_doses"),
        "ph_up_unanswered_doses": coerce_float(payload, "ph_up_unanswered_doses"),
        "ph_down_unanswered_doses": coerce_float(payload, "ph_down_unanswered_doses"),
        "ph_session_ml": coerce_float(payload, "ph_session_ml"),
        "ec_session_ml": coerce_float(payload, "ec_session_ml"),
        "ec_session_cap_ml": coerce_float(payload, "ec_session_cap_ml"),
        "water_level_sensor_fault": coerce_optional_bool(payload, "water_level_sensor_fault"),
        "water_level_init_required": coerce_optional_bool(payload, "water_level_init_required"),
        "water_level_saved_pct": coerce_float(payload, "water_level_saved_pct"),
        "ph_ema": coerce_float(payload, "ph_ema"),
        "tds_ppm_ema": coerce_float(payload, "tds_ppm_ema"),
        "firmware_version": (str(payload["firmware_version"])
                             if payload.get("firmware_version") else None),
        # Firmware 1.6.2+: uploader health ({"queued", "hold", "ok_ago_s", ...}).
        "influx": payload.get("influx") if isinstance(payload.get("influx"), dict) else None,
        "grow_light": _grow_light_from_payload(payload),
        # Firmware 1.6.2+: a reading recorded while MQTT was down, sent later.
        # History only; it must not overwrite the live state.
        "replay": bool(payload.get("replay")),
        # Firmware 1.8.0+: reading identity (see IDENTITY_FIELDS).
        "kind": coerce_str(payload, "kind", default="") or None,
        "boot_id": coerce_float(payload, "boot_id"),
        "seq": coerce_float(payload, "seq"),
        "uptime_ms": coerce_float(payload, "uptime_ms"),
        "reading_key": reading_key(payload),
    }

    # Require that each sensor key was present in the payload. ESP32 failure
    # sentinels are allowed through as None after sanitize_sensor_value().
    missing = []
    for field in SENSOR_FIELDS:
        aliases = {
            "ambient_temp_c": ("ambient_temp_c", "temperature", "temperature_c", "temp_c", "ambient_temp"),
            "ambient_humidity_pct": ("ambient_humidity_pct", "humidity", "humidity_percent", "ambient_humidity"),
            "water_temp_c": ("water_temp_c", "water_temperature", "water_temperature_c"),
            "light_adc": ("light_adc", "ldr_adc", "light_intensity", "light_lux", "lux"),
            "water_level_pct": ("water_level_pct", "water_level", "water_level_percent"),
            "tds_ppm": ("tds_ppm", "tds", "tdsPPM", "ec", "ec_ms_cm"),
            "ph": ("ph", "pH", "ph_value"),
            "co2_ppm": ("co2_ppm", "co2", "CO2"),
        }[field]
        if not any(key in payload for key in aliases):
            missing.append(field)
    if missing:
        raise ValueError(f"MQTT payload missing sensor fields: {', '.join(missing)}")

    return reading


def reading_to_state(reading, previous_state):
    state = previous_state.copy()
    state["sim_time"] = parse_sim_time(reading["timestamp"])

    cal_meta_fields = {
        "ph_cal_state",
        "ph_cal_message",
        "ph_cal_active",
        "ph_cal_ok",
        "ph_cal_acid_v",
        "ph_cal_alkali_v",
        "tds_cal_state",
        "tds_cal_message",
        "tds_cal_active",
        "tds_cal_ok",
        "pump_cal_state",
        "pump_cal_message",
        "pump_cal_active",
        "pump_cal_ok",
        "pump_cal_pump",
        "pump_cal_mode",
        "pump_cal_run",
        "pump_cal_runs_needed",
        "pump_cal_bursts",
        "pump_cal_pulse_ms",
        "pump_cal_target_bursts",
        "pump_cal_target_ms",
        "pump_cal_last_sample",
        "pump_cal_unit",
        "water_calibrated",
        "nutrient_calibrated",
        "ph_up_calibrated",
        "ph_down_calibrated",
        "water_ml_per_s",
        "nutrient_ml_per_burst",
        "ph_up_ml_per_burst",
        "ph_down_ml_per_burst",
        "system_mode",
        "stage_persisted",
        "control_idle",
        "target_drawdown_mm",
        "current_drawdown_mm",
        "tank_volume_l",
        "drawdown_tolerance_mm",
        "fill_0_pct",
        "level_check_s",
        "ph_check_s",
        "ec_check_s",
        "ec_control_enabled",
        "ph_control_enabled",
        "level_control_enabled",
        "ec_dead_low",
        "ec_dead_high",
        "ec_min_dose_ml",
        "ec_max_dose_ml",
        "ec_settle_s",
        "ph_dead_low",
        "ph_dead_high",
        "ph_center",
        "ph_min_dose_ml",
        "ph_max_dose_ml",
        "ph_session_cap_ml",
        "ph_baseline_recheck_s",
    }

    for field in SENSOR_FIELDS + ACTUATOR_FIELDS + OPTIONAL_META_FIELDS:
        if field in reading:
            # Keep previous value when a sensor briefly reports an ESP32 failure
            # sentinel (sanitized to None), so the dashboard does not flicker to 0.
            if reading[field] is None and field in SENSOR_FIELDS:
                continue
            if reading[field] is not None or field in cal_meta_fields:
                state[field] = reading[field]

    for field in STATUS_FIELDS:
        if field in reading:
            state[field] = reading[field]

    # Placeholder actuators: None means "not received" from this reading.
    for field in PLACEHOLDER_ACTUATOR_FIELDS:
        if field in reading:
            state[field] = reading[field]

    state.setdefault("water_max_pct", WATER_LEVEL_MAX_PERCENT)

    # Keep legacy keys used by older simulation/control helpers in sync.
    state["temperature"] = state.get("ambient_temp_c", state.get("temperature", 0.0))
    state["humidity"] = state.get("ambient_humidity_pct", state.get("humidity", 0.0))
    state["light_intensity"] = state.get("light_adc", state.get("light_intensity", 0.0))
    state["water_level"] = state.get("water_level_pct", state.get("water_level", 0.0))
    state["ec"] = state.get("tds_ppm", 0.0) / 500.0
    state["pump_on"] = state.get("water_pump_on", False)

    state.setdefault("pump_timer", 0)
    state.setdefault("nutrient_timer", 0)
    state.setdefault("dilute_timer", 0)
    state.setdefault("ph_timer", 0)
    return state


def ensure_log_schema(path):
    """Rotate an older CSV header so new files stay SQLite-importable."""
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("r", encoding="utf-8", newline="") as handle:
        first_line = handle.readline().strip()
    expected = ",".join(LOG_FIELDS)
    if first_line == expected:
        return
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    legacy = path.with_name(f"{path.stem}_legacy_{stamp}{path.suffix}")
    path.rename(legacy)
    # TODO(sqlite): import rotated legacy CSVs when the SQLite store is added.


def append_sensor_log(log_path, reading, sqlite_path=None):
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_log_schema(path)
    new_file = not path.exists()

    row = {field: sqlite_csv_cell(field, reading.get(field)) for field in LOG_FIELDS}

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=LOG_FIELDS,
            lineterminator="\n",
            extrasaction="ignore",
        )
        if new_file:
            writer.writeheader()
        writer.writerow(row)

    if sqlite_path:
        try:
            from sqlite_store import insert_sensor_reading

            insert_sensor_reading(sqlite_path, reading)
        except Exception as exc:  # pragma: no cover - disk / lock failures
            # CSV already written; surface via caller errors if available.
            print(f"[sqlite] sensor_readings insert failed: {exc}")


def persist_mqtt_to_sqlite(sqlite_path, topic, payload_bytes, reading=None, received_at=None):
    """Store raw MQTT payload (+ normalized row when available) in SQLite."""
    if not sqlite_path:
        return None
    try:
        from sqlite_store import persist_mqtt_payload

        return persist_mqtt_payload(
            sqlite_path,
            topic=topic,
            payload_bytes=payload_bytes,
            reading=reading,
            received_at=received_at,
        )
    except Exception as exc:  # pragma: no cover
        print(f"[sqlite] mqtt persist failed: {exc}")
        return None


def sample_reading(previous_state=None, grow_stage=None):
    now = utc_now()
    minute = now.hour * 60 + now.minute
    day_fraction = minute / (24 * 60)

    stage = normalize_stage_name(
        grow_stage or (previous_state or {}).get("grow_stage") or DEFAULT_GROW_STAGE
    )
    preset = GROW_PRESETS[stage]

    ambient_temp = 24 + 4 * math.sin(2 * math.pi * (day_fraction - 0.2)) + random.uniform(-0.4, 0.4)
    humidity = 62 - 8 * math.sin(2 * math.pi * (day_fraction - 0.2)) + random.uniform(-1.5, 1.5)
    light = max(0, math.sin(math.pi * (day_fraction - 0.25) / 0.5)) * 2800 + random.uniform(-40, 40)

    if previous_state:
        water_temp_base = previous_state.get("water_temp_c", 22.0)
        water_base = previous_state.get(
            "water_level_pct",
            previous_state.get("water_level", preset["water_level_target_pct"] + 5),
        )
        tds_base = previous_state.get(
            "tds_ppm",
            (preset["nutrient_tds_min"] + preset["nutrient_tds_max"]) / 2,
        )
        ph_base = previous_state.get(
            "ph",
            (preset["ph_min"] + preset["ph_max"]) / 2,
        )
        co2_base = previous_state.get("co2_ppm", 450)
    else:
        water_temp_base = 22.0
        water_base = preset["water_level_target_pct"] + 5
        tds_base = (preset["nutrient_tds_min"] + preset["nutrient_tds_max"]) / 2
        ph_base = (preset["ph_min"] + preset["ph_max"]) / 2
        co2_base = 450

    payload = {
        "device_id": "sample-garden-node-01",
        "timestamp": now.isoformat(timespec="seconds"),
        "grow_stage": stage,
        "hydroponic_mode": "kratky",
        "ph_settle_s": 18 * 60 * 60,
        "water_target_pct": preset["water_level_target_pct"],
        "water_max_pct": WATER_LEVEL_MAX_PERCENT,
        "nutrient_tds_min": preset["nutrient_tds_min"],
        "nutrient_tds_max": preset["nutrient_tds_max"],
        "ph_target_min": preset["ph_min"],
        "ph_target_max": preset["ph_max"],
        "ph_dose": "none",
        "nutrient_dose": "none",
        "ambient_temp_c": round(clamp(ambient_temp, 16, 38), 2),
        "ambient_humidity_pct": round(clamp(humidity, 35, 95), 2),
        "water_temp_c": round(clamp(water_temp_base + random.uniform(-0.2, 0.2), 10, 40), 2),
        "light_adc": round(clamp(light, 0, 4095), 0),
        "water_level_pct": round(clamp(water_base - random.uniform(0.0, 0.15), 0, 100), 2),
        "tds_ppm": round(clamp(tds_base + random.uniform(-8, 8), 0, 2500), 0),
        "ph": round(clamp(ph_base + random.uniform(-0.03, 0.03), 4.5, 8.0), 2),
        "co2_ppm": round(clamp(co2_base + random.uniform(-15, 15), 350, 2000), 0),
        "water_pump_on": False,
        "drain_pump_on": False,
        "nutrient_pump_on": False,
        "ph_up_pump_on": False,
        "ph_down_pump_on": False,
    }
    return normalize_reading(payload, source="sample")


def mqtt_connection_fingerprint(config):
    """Keys that require a new broker session when changed."""
    return (
        str(config.get("mqtt_host", "")),
        int(config.get("mqtt_port", 1883)),
        str(config.get("mqtt_topic", "")),
        str(config.get("mqtt_client_id", "")),
        bool(config.get("mqtt_tls", False)),
        str(config.get("mqtt_username") or os.getenv("MQTT_USERNAME") or ""),
        str(config.get("mqtt_password") or os.getenv("MQTT_PASSWORD") or ""),
        int(config.get("mqtt_keepalive", 60)),
    )


def dashboard_persists(config):
    """Whether the dashboard process itself writes SQLite/CSV.

    On the Pi the always-on ingest service (scripts/mqtt_to_sqlite.py) is the
    only writer and the dashboard unit sets SMART_GARDEN_DASHBOARD_PERSIST=0.
    A local run without that service keeps the old behaviour (persist).
    """
    if "mqtt_persist" in config:
        return bool(config["mqtt_persist"])
    return os.getenv("SMART_GARDEN_DASHBOARD_PERSIST", "1").strip() not in {"0", "false", "no"}


class MqttSensorClient:
    """One broker session. Readings fan out to every reader by cursor.

    Use shared_mqtt_client(): Streamlit reruns and browser tabs must not each
    open a subscriber (before this, every open session added a client and
    every client stored every message, so the Pi kept up to 14 copies).
    """

    def __init__(self, config):
        self.config = config.copy()
        self.fingerprint = mqtt_connection_fingerprint(self.config)
        self.persist = dashboard_persists(self.config)
        self._log_lock = threading.Lock()
        self._log = deque(maxlen=2000)  # (n, reading), n increasing
        self._log_n = 0
        self.recent = deque(maxlen=200)
        self.errors = deque(maxlen=20)
        self.connected = False
        self.started = False
        self._stop_requested = False
        self.client = None
        self.thread = None
        self.last_message_at = None  # time.monotonic() of the newest telemetry
        # Newest {"cmd": "power", …} ack, with "at" (time.monotonic()) added.
        # The controller answers power commands in standby, when it sends no telemetry.
        self.power_ack = None

    def start(self):
        if self.started:
            return
        self.started = True
        self._stop_requested = False

        if mqtt is None:
            self.errors.append("paho-mqtt is not installed. Install requirements.txt on the Pi.")
            return

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        client_kwargs = {"client_id": self.config["mqtt_client_id"]}
        # paho-mqtt 2.x requires an explicit callback API version.
        if hasattr(mqtt, "CallbackAPIVersion"):
            client_kwargs["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION1

        client = mqtt.Client(**client_kwargs)
        # Avoid reconnect wars when Streamlit recreates the client or another
        # tab uses the same broker (duplicate client_id kicks the peer).
        if hasattr(client, "reconnect_delay_set"):
            client.reconnect_delay_set(min_delay=1, max_delay=30)
        username = self.config.get("mqtt_username") or os.getenv("MQTT_USERNAME")
        password = self.config.get("mqtt_password") or os.getenv("MQTT_PASSWORD")

        if username:
            client.username_pw_set(username, password)
        if self.config.get("mqtt_tls"):
            client.tls_set()

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self.client = client

        try:
            host = self.config["mqtt_host"]
            port = int(self.config["mqtt_port"])
            keepalive = int(self.config["mqtt_keepalive"])
            self.errors.append(
                f"Connecting to MQTT {host}:{port} as {self.config['mqtt_client_id']} …"
            )
            client.connect(host, port, keepalive)
            # Manual loop so stop() can exit without leaving a reconnecting
            # loop_forever thread that fights a newer client for the same ID.
            while not self._stop_requested:
                rc = client.loop(timeout=1.0)
                if self._stop_requested:
                    break
                if rc != mqtt.MQTT_ERR_SUCCESS:
                    self.connected = False
                    time.sleep(1.0)
                    if self._stop_requested:
                        break
                    try:
                        client.reconnect()
                    except Exception as exc:
                        self.errors.append(f"MQTT reconnect failed: {exc}")
        except Exception as exc:
            self.connected = False
            if not self._stop_requested:
                self.errors.append(f"MQTT connection failed: {exc}")
        finally:
            self.connected = False
            try:
                client.disconnect()
            except Exception:
                pass

    def stop(self):
        self._stop_requested = True
        self.connected = False
        self.started = False
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
        thread = self.thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.thread = None

    def _on_connect(self, client, _userdata, _flags, rc):
        self.connected = rc == 0
        if self.connected:
            topic = self.config["mqtt_topic"]
            client.subscribe(topic)
            self.errors.append(f"Subscribed to {topic}")
            ack_topic = ack_topic_for(topic)
            if ack_topic:
                client.subscribe(ack_topic)
        else:
            self.errors.append(f"MQTT connect returned rc={rc}")

    def _on_disconnect(self, _client, _userdata, rc):
        self.connected = False
        if rc and not self._stop_requested:
            self.errors.append(f"MQTT disconnected unexpectedly rc={rc}")

    def _on_message(self, _client, _userdata, msg):
        if msg.topic.endswith("/ack"):
            ack = parse_power_ack(msg.payload)
            if ack is not None:
                self.power_ack = {**ack, "at": time.monotonic()}
            return
        reading = None
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            reading = normalize_reading(payload, source="mqtt", topic=msg.topic)
            self.last_message_at = time.monotonic()
            with self._log_lock:
                self._log_n += 1
                self._log.append((self._log_n, reading))
            self.recent.appendleft(reading)
        except Exception as exc:
            self.errors.append(f"Invalid MQTT message on {msg.topic}: {exc}")
        if not self.persist:
            return
        # Local runs without the ingest service: store raw bytes (even if the
        # JSON did not parse) and, for a new reading, the CSV row.
        is_new = persist_mqtt_to_sqlite(
            self.config.get("sqlite_path"),
            msg.topic,
            msg.payload,
            reading=reading,
            received_at=(reading or {}).get("received_at") if reading else None,
        )
        if reading is not None and is_new is not False:
            append_sensor_log(self.config["sensor_log_path"], reading, sqlite_path=None)

    def drain_since(self, cursor):
        """Readings after cursor, and the new cursor. Each reader keeps its own."""
        with self._log_lock:
            items = [(n, r) for n, r in self._log if n > cursor]
            latest = self._log_n
        return latest, [r for _, r in items]

    def latest_cursor(self):
        with self._log_lock:
            return self._log_n

    def publish_json(self, topic, payload, qos=0, retain=False):
        """Publish a JSON object to the broker (e.g. ESP32 cmd topic)."""
        if self.client is None or not self.connected:
            self.errors.append("MQTT publish skipped — not connected")
            return False
        try:
            body = json.dumps(payload)
            info = self.client.publish(topic, body, qos=qos, retain=retain)
            ok = info.rc == mqtt.MQTT_ERR_SUCCESS if mqtt is not None else False
            if not ok:
                self.errors.append(f"MQTT publish failed rc={getattr(info, 'rc', '?')} → {topic}")
            return ok
        except Exception as exc:
            self.errors.append(f"MQTT publish error: {exc}")
            return False

    def status(self):
        age_s = None
        if self.last_message_at is not None:
            age_s = max(0.0, time.monotonic() - self.last_message_at)
        stale_after = float(self.config.get("mqtt_stale_after_seconds") or 90)
        return {
            "started": self.started,
            "connected": self.connected,
            "recent_count": len(self.recent),
            "errors": list(self.errors),
            "last_message_age_s": age_s,
            # Broker up is not the same as the garden node publishing.
            "node_live": age_s is not None and age_s <= stale_after,
            "power_ack": power_ack_with_age(self.power_ack),
        }


def ack_topic_for(telemetry_topic):
    """smart-garden/+/telemetry -> smart-garden/+/ack; None for any other pattern."""
    head, sep, leaf = str(telemetry_topic).rpartition("/")
    return f"{head}/ack" if sep and leaf == "telemetry" else None


def parse_power_ack(payload_bytes):
    """The ack dict for {"cmd": "power", …} (firmware 1.9.26+), else None."""
    try:
        msg = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    if not isinstance(msg, dict) or msg.get("cmd") != "power":
        return None
    return msg


def power_ack_with_age(ack, now=None):
    if ack is None:
        return None
    now = time.monotonic() if now is None else now
    out = {k: v for k, v in ack.items() if k != "at"}
    out["age_s"] = max(0.0, now - ack["at"])
    return out


_shared_lock = threading.Lock()
_shared_clients = {}


def shared_mqtt_client(config):
    """The process-wide broker session for this connection config.

    Module state survives Streamlit reruns, so every session and rerun gets
    the same client. A config change replaces it (one session per process).
    """
    fingerprint = mqtt_connection_fingerprint(config)
    with _shared_lock:
        client = _shared_clients.get(fingerprint)
        if client is None:
            for old in list(_shared_clients.values()):
                old.stop()
            _shared_clients.clear()
            client = MqttSensorClient(config)
            _shared_clients[fingerprint] = client
        client.start()
        return client
