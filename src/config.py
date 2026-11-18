from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import socket

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Seedlings planted 3 Sep 2026 17:00 AEST (UTC+10).
PLANT_DATETIME = datetime(2026, 9, 3, 17, 0, 0, tzinfo=timezone(timedelta(hours=10)))


def archive_now():
    """The moment an archive copy of the record is shown as at, from
    SMART_GARDEN_ARCHIVE_NOW (ISO, e.g. 2026-10-18T13:00:00Z); None when live."""
    raw = os.getenv("SMART_GARDEN_ARCHIVE_NOW", "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def now_utc() -> datetime:
    """Now, or the archive moment when the dashboard is showing an archive."""
    return archive_now() or datetime.now(timezone.utc)


def now_local() -> datetime:
    """now_utc() as naive local time."""
    return now_utc().astimezone().replace(tzinfo=None)


def seedling_age(now=None, planted_at=None):
    """Return (day_number, week_number, label) since plant date.

    Day 1 is the calendar day of planting; week 1 is days 1–7.
    """
    planted_at = planted_at or PLANT_DATETIME
    if now is None:
        now = now_utc().astimezone(planted_at.tzinfo)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=planted_at.tzinfo)
    else:
        now = now.astimezone(planted_at.tzinfo)
    if now < planted_at:
        return 0, 0, "Not planted yet"
    elapsed = now - planted_at
    day = int(elapsed.total_seconds() // 86400) + 1
    week = (day - 1) // 7 + 1
    return day, week, f"Day {day} / Week {week}"


# Reagent week schedule → suggested grow stage (informational only; never auto-applied).
# Week 1 → seedling; weeks 2–3 → transitional; weeks 4–6 → vegetative;
# week 7+ → harvest (basil / lettuce are usually cut from about day 42).
TRANSITIONAL_STRENGTH_FRACTION = 0.5
HARVEST_FROM_WEEK = 7


def suggested_grow_stage(week_number: int) -> str | None:
    if week_number <= 0:
        return None
    if week_number <= 1:
        return "seedling"
    if week_number <= 3:
        return "transitional"
    if week_number < HARVEST_FROM_WEEK:
        return "vegetative"
    return "harvest"


def _default_mqtt_host():
    """Prefer explicit env; else this machine's LAN IP so we share the ESP32 broker.

    On Windows the winget Mosquitto service often binds localhost-only on :1883
    while a LAN mosquitto also listens on 0.0.0.0:1883. Dashboard clients that
    use 127.0.0.1 never see ESP32 publishes — use the WiFi LAN IP instead.
    """
    env = os.getenv("MQTT_HOST", "").strip()
    if env:
        return env
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        probe.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"

# Matches shared/grow_stage.h STAGE_CONFIGS on the ESP32.
# water_level_target_pct is derived for display when fill_0 is unknown;
# live control uses target_drawdown_mm relative to fill_0.
GROW_PRESETS = {
    "idle": {
        "label": "Idle — sensors only (no auto control until you set a stage)",
        "target_drawdown_mm": 0,
        "drawdown_tolerance_mm": 10,
        "level_check_s": 0,
        "ph_check_s": 0,
        "ec_check_s": 0,
        "water_control_enabled": False,
        "ph_control_enabled": False,
        "ec_control_enabled": False,
        "ph_min": 5.8,
        "ph_max": 6.2,
        "ph_dead_low": 5.5,
        "ph_dead_high": 6.5,
        "ph_center": 6.0,
        "ph_min_dose_ml": 0.05,
        "ph_max_dose_ml": 0.5,
        "ph_settle_s": 20 * 60,
        "ph_session_cap_ml": 0.0,
        "nutrient_tds_min": 0.0,
        "nutrient_tds_max": 0.0,
        "ec_dead_low": 0.0,
        "ec_dead_high": 0.0,
        "ec_min_dose_ml": 0.0,
        "ec_max_dose_ml": 0.0,
        "ec_settle_s": 0,
        "nutrient_dosing_enabled": False,
        "nutrient_dilution_enabled": False,
        "water_level_target_pct": 50.0,
    },
    "filling": {
        "label": "Filling — manual fill to net pots",
        "target_drawdown_mm": 0,
        "drawdown_tolerance_mm": 10,
        "level_check_s": 30 * 60,
        "ph_check_s": 0,
        "ec_check_s": 0,
        "water_control_enabled": False,
        "ph_control_enabled": False,
        "ec_control_enabled": False,
        "ph_min": 5.8,
        "ph_max": 6.2,
        "ph_dead_low": 5.5,
        "ph_dead_high": 6.5,
        "ph_center": 6.0,
        "ph_min_dose_ml": 0.05,
        "ph_max_dose_ml": 0.5,
        "ph_settle_s": 20 * 60,
        "ph_session_cap_ml": 0.0,
        "nutrient_tds_min": 0.0,
        "nutrient_tds_max": 0.0,
        "ec_dead_low": 0.0,
        "ec_dead_high": 0.0,
        "ec_min_dose_ml": 0.0,
        "ec_max_dose_ml": 0.0,
        "ec_settle_s": 0,
        "nutrient_dosing_enabled": False,
        "nutrient_dilution_enabled": False,
        "water_level_target_pct": 50.0,
    },
    "ph_baseline": {
        "label": "pH baseline — plain water pH only",
        "target_drawdown_mm": 0,
        "drawdown_tolerance_mm": 10,
        "level_check_s": 40 * 60,
        "ph_check_s": 40 * 60,
        "ec_check_s": 0,
        "water_control_enabled": False,
        "ph_control_enabled": True,
        "ec_control_enabled": False,
        "ph_min": 5.8,
        "ph_max": 6.2,
        "ph_dead_low": 5.5,
        "ph_dead_high": 6.5,
        "ph_center": 6.0,
        "ph_min_dose_ml": 0.05,
        "ph_max_dose_ml": 1.0,
        "ph_settle_s": 20 * 60,
        "ph_session_cap_ml": 5.0,
        "nutrient_tds_min": 0.0,
        "nutrient_tds_max": 0.0,
        "ec_dead_low": 0.0,
        "ec_dead_high": 0.0,
        "ec_min_dose_ml": 0.0,
        "ec_max_dose_ml": 0.0,
        "ec_settle_s": 0,
        "nutrient_dosing_enabled": False,
        "nutrient_dilution_enabled": False,
        "water_level_target_pct": 50.0,
    },
    "seedling": {
        "label": "Seedling — pH only, hold fill_0 (0 mm)",
        "target_drawdown_mm": 0,
        "drawdown_tolerance_mm": 10,
        "level_check_s": 40 * 60,
        "ph_check_s": 40 * 60,
        "ec_check_s": 0,
        "water_control_enabled": True,
        "ph_control_enabled": True,
        "ec_control_enabled": False,
        "ph_min": 5.8,
        "ph_max": 6.2,
        "ph_dead_low": 5.5,
        "ph_dead_high": 6.5,
        "ph_center": 6.0,
        "ph_min_dose_ml": 0.05,
        "ph_max_dose_ml": 0.5,
        "ph_settle_s": 20 * 60,
        "ph_session_cap_ml": 5.0,
        "nutrient_tds_min": 0.0,
        "nutrient_tds_max": 0.0,
        "ec_dead_low": 0.0,
        "ec_dead_high": 0.0,
        "ec_min_dose_ml": 0.0,
        "ec_max_dose_ml": 0.0,
        "ec_settle_s": 0,
        "nutrient_dosing_enabled": False,
        "nutrient_dilution_enabled": False,
        "water_level_target_pct": 50.0,
    },
    "transitional": {
        "label": "Transitional — half-strength EC (×0.5 of vegetative), same pH/control",
        "target_drawdown_mm": 20,
        "drawdown_tolerance_mm": 5,
        "level_check_s": 40 * 60,
        "ph_check_s": 40 * 60,
        "ec_check_s": 40 * 60,
        "water_control_enabled": True,
        "ph_control_enabled": True,
        "ec_control_enabled": True,
        "ph_min": 5.8,
        "ph_max": 6.2,
        "ph_dead_low": 5.5,
        "ph_dead_high": 6.5,
        "ph_center": 6.0,
        "ph_min_dose_ml": 0.05,
        "ph_max_dose_ml": 1.0,
        "ph_settle_s": 20 * 60,
        "ph_session_cap_ml": 10.0,
        # Placeholder defaults (= vegetative × 0.5); live UI scales catalogue veg band.
        "nutrient_tds_min": 300.0,
        "nutrient_tds_max": 400.0,
        "ec_dead_low": 250.0,
        "ec_dead_high": 450.0,
        "ec_min_dose_ml": 0.1,
        "ec_max_dose_ml": 30.0,
        "ec_settle_s": 20 * 60,
        "nutrient_dosing_enabled": True,
        "nutrient_dilution_enabled": True,
        "water_level_target_pct": 40.0,
    },
    "vegetative": {
        "label": "Vegetative — EC then pH, 20 mm air gap",
        "target_drawdown_mm": 20,
        "drawdown_tolerance_mm": 5,
        "level_check_s": 40 * 60,
        "ph_check_s": 40 * 60,
        "ec_check_s": 40 * 60,
        "water_control_enabled": True,
        "ph_control_enabled": True,
        "ec_control_enabled": True,
        "ph_min": 5.8,
        "ph_max": 6.2,
        "ph_dead_low": 5.5,
        "ph_dead_high": 6.5,
        "ph_center": 6.0,
        "ph_min_dose_ml": 0.05,
        "ph_max_dose_ml": 1.0,
        "ph_settle_s": 20 * 60,
        "ph_session_cap_ml": 10.0,
        "nutrient_tds_min": 600.0,
        "nutrient_tds_max": 800.0,
        "ec_dead_low": 550.0,
        "ec_dead_high": 850.0,
        "ec_min_dose_ml": 0.1,
        "ec_max_dose_ml": 30.0,
        "ec_settle_s": 20 * 60,
        "nutrient_dosing_enabled": True,
        "nutrient_dilution_enabled": True,
        "water_level_target_pct": 40.0,
    },
    "harvest": {
        "label": "Harvest — gentler EC/pH, 30 mm air gap",
        "target_drawdown_mm": 30,
        "drawdown_tolerance_mm": 5,
        "level_check_s": 40 * 60,
        "ph_check_s": 40 * 60,
        "ec_check_s": 40 * 60,
        "water_control_enabled": True,
        "ph_control_enabled": True,
        "ec_control_enabled": True,
        "ph_min": 5.8,
        "ph_max": 6.2,
        "ph_dead_low": 5.5,
        "ph_dead_high": 6.5,
        "ph_center": 6.0,
        "ph_min_dose_ml": 0.05,
        "ph_max_dose_ml": 0.5,
        "ph_settle_s": 20 * 60,
        "ph_session_cap_ml": 5.0,
        "nutrient_tds_min": 0.0,
        "nutrient_tds_max": 200.0,
        "ec_dead_low": 0.0,
        "ec_dead_high": 250.0,
        "ec_min_dose_ml": 0.1,
        "ec_max_dose_ml": 10.0,
        "ec_settle_s": 20 * 60,
        "nutrient_dosing_enabled": False,
        "nutrient_dilution_enabled": True,
        "water_level_target_pct": 30.0,
    },
    "sensor_baseline": {
        "label": "Sensor baseline — logging only (no pumps)",
        "target_drawdown_mm": 0,
        "drawdown_tolerance_mm": 10,
        "level_check_s": 0,
        "ph_check_s": 0,
        "ec_check_s": 0,
        "water_control_enabled": False,
        "ph_control_enabled": False,
        "ec_control_enabled": False,
        "ph_min": 5.8,
        "ph_max": 6.2,
        "ph_dead_low": 5.5,
        "ph_dead_high": 6.5,
        "ph_center": 6.0,
        "ph_min_dose_ml": 0.05,
        "ph_max_dose_ml": 0.5,
        "ph_settle_s": 20 * 60,
        "ph_session_cap_ml": 0.0,
        "nutrient_tds_min": 0.0,
        "nutrient_tds_max": 0.0,
        "ec_dead_low": 0.0,
        "ec_dead_high": 0.0,
        "ec_min_dose_ml": 0.0,
        "ec_max_dose_ml": 0.0,
        "ec_settle_s": 0,
        "nutrient_dosing_enabled": False,
        "nutrient_dilution_enabled": False,
        "water_level_target_pct": 50.0,
    },
}

# Older telemetry / docs used "flowering" for the mid stage.
STAGE_ALIASES = {
    "flowering": "vegetative",
    "flower": "vegetative",
    "veg": "vegetative",
    "transition": "transitional",
    "half": "transitional",
    "half_strength": "transitional",
    "priming": "ph_baseline",
    "baseline": "sensor_baseline",
    "fill": "filling",
    "unset": "idle",
    "none": "idle",
    "off": "idle",
}

# Ordered for the stage selector UI.
STAGE_ORDER = [
    "idle",
    "sensor_baseline",
    "filling",
    "ph_baseline",
    "seedling",
    "transitional",
    "vegetative",
    "harvest",
]

# Hard ceiling for every fresh-water path on the ESP32.
WATER_LEVEL_MAX_PERCENT = 50.0

DEFAULT_GROW_STAGE = "idle"


def normalize_stage_name(stage_name):
    if not stage_name:
        return DEFAULT_GROW_STAGE
    key = str(stage_name).strip().lower().replace(" ", "_").replace("-", "_")
    key = STAGE_ALIASES.get(key, key)
    if key in GROW_PRESETS:
        return key
    return DEFAULT_GROW_STAGE


def scale_tds_band_for_stage(stage_name, tds_min, tds_max):
    """Map a vegetative TDS band onto the active stage (transitional = × fraction)."""
    stage = normalize_stage_name(stage_name)
    if stage == "transitional":
        return (
            float(tds_min) * TRANSITIONAL_STRENGTH_FRACTION,
            float(tds_max) * TRANSITIONAL_STRENGTH_FRACTION,
        )
    return float(tds_min), float(tds_max)


def apply_grow_preset(config, stage_name=None):
    """Copy ESP32 grow-stage thresholds into config / return stage metadata."""
    stage = normalize_stage_name(stage_name or config.get("grow_stage", DEFAULT_GROW_STAGE))
    preset = GROW_PRESETS[stage]
    config["grow_stage"] = stage
    config["water_level_target_pct"] = preset["water_level_target_pct"]
    config["water_level_low_threshold"] = preset["water_level_target_pct"]
    config["ph_low_threshold"] = preset["ph_min"]
    config["ph_high_threshold"] = preset["ph_max"]
    config["tds_low_threshold"] = preset["nutrient_tds_min"]
    config["tds_high_threshold"] = preset["nutrient_tds_max"]
    config["nutrient_dosing_enabled"] = preset["nutrient_dosing_enabled"]
    config["nutrient_dilution_enabled"] = preset["nutrient_dilution_enabled"]
    config["ph_control_enabled"] = preset["ph_control_enabled"]
    config["water_control_enabled"] = preset["water_control_enabled"]
    return preset


DEFAULT_CONFIG = {
    # Production dashboard is MQTT-only (SAMPLE/SIMULATION live on the demo branch).
    "data_source": "MQTT",
    "grow_stage": DEFAULT_GROW_STAGE,

    "water_level_low_threshold": 30.0,
    "water_level_target_pct": 30.0,
    "water_level_max_pct": WATER_LEVEL_MAX_PERCENT,
    "water_control_enabled": True,

    "tds_low_threshold": 550.0,
    "tds_high_threshold": 850.0,
    "ph_low_threshold": 5.5,
    "ph_high_threshold": 6.5,
    "co2_high_threshold": 1200.0,

    "mqtt_host": _default_mqtt_host(),
    "mqtt_port": 1883,
    "mqtt_topic": "smart-garden/+/telemetry",
    "mqtt_cmd_topic": "smart-garden/garden-node-01/cmd",
    "mqtt_device_id": "garden-node-01",
    "mqtt_keepalive": 60,
    "mqtt_client_id": "smart-garden-dashboard",
    "mqtt_username": "",
    "mqtt_password": "",
    "mqtt_tls": False,
    # Sensor cycles are ~20 s and a cloud write may use the 15 s budget,
    # so "live" means a telemetry message inside this window.
    "mqtt_stale_after_seconds": 90,
    # History loaded at startup. Independent of whether the ESP32 is on MQTT.
    "history_lookback_hours": 72,
    "history_max_rows": int(os.getenv("SMART_GARDEN_HISTORY_ROWS", "4000")),
    "auto_refresh": True,
    "sensor_log_path": str(DATA_DIR / "sensor_readings.csv"),
    # Dual-written with CSV: raw mqtt_messages + normalized sensor_readings.
    "sqlite_path": os.getenv("SMART_GARDEN_SQLITE") or str(DATA_DIR / "sensor_readings.sqlite"),
}

apply_grow_preset(DEFAULT_CONFIG)

# Empty until the first MQTT reading arrives — never show fabricated sample values.
INITIAL_STATE = {
    "grow_stage": None,
    "system_mode": None,
    "stage_persisted": None,
    "control_idle": None,
    "hydroponic_mode": None,
    "ph_settle_s": None,
    "control_law": None,
    "pumps_calibrated": None,
    "water_target_pct": None,
    "water_max_pct": WATER_LEVEL_MAX_PERCENT,
    "target_drawdown_mm": None,
    "current_drawdown_mm": None,
    "tank_volume_l": None,
    "drawdown_tolerance_mm": None,
    "fill_0_pct": None,
    "level_check_s": None,
    "ph_check_s": None,
    "ec_check_s": None,
    "ec_control_enabled": None,
    "ph_control_enabled": None,
    "level_control_enabled": None,
    "nutrient_tds_min": None,
    "nutrient_tds_max": None,
    "ec_dead_low": None,
    "ec_dead_high": None,
    "ec_min_dose_ml": None,
    "ec_max_dose_ml": None,
    "ec_settle_s": None,
    "ph_target_min": None,
    "ph_target_max": None,
    "ph_dead_low": None,
    "ph_dead_high": None,
    "ph_center": None,
    "ph_min_dose_ml": None,
    "ph_max_dose_ml": None,
    "ph_session_cap_ml": None,
    "ph_baseline_recheck_s": None,
    "ph_dose": None,
    "nutrient_dose": None,
    "ph_voltage": None,
    "ph_raw": None,
    "ph_filtered": None,
    "ph_slope": None,
    "ph_offset": None,
    "ph_probe_calibrated": None,
    "ph_cal_active": None,
    "ph_cal_state": None,
    "ph_cal_message": None,
    "ph_cal_ok": None,
    "ph_cal_acid_v": None,
    "ph_cal_alkali_v": None,
    "ph_cal_stored_acid_v": None,
    "ph_cal_stored_alkali_v": None,
    "water_calibrated": None,
    "nutrient_calibrated": None,
    "ph_up_calibrated": None,
    "ph_down_calibrated": None,
    "water_ml_per_s": None,
    "nutrient_ml_per_burst": None,
    "ph_up_ml_per_burst": None,
    "ph_down_ml_per_burst": None,
    "pump_cal_active": None,
    "pump_cal_state": None,
    "pump_cal_pump": None,
    "pump_cal_message": None,
    "pump_cal_ok": None,
    "pump_cal_mode": None,
    "pump_cal_run": None,
    "pump_cal_runs_needed": None,
    "pump_cal_bursts": None,
    "pump_cal_pulse_ms": None,
    "pump_cal_target_bursts": None,
    "pump_cal_target_ms": None,
    "pump_cal_last_sample": None,
    "pump_cal_unit": None,
    "water_cap_ml": None,
    "water_rem_ml": None,
    "water_empty": None,
    "nutrient_cap_ml": None,
    "nutrient_rem_ml": None,
    "nutrient_empty": None,
    "ph_up_cap_ml": None,
    "ph_up_rem_ml": None,
    "ph_up_empty": None,
    "ph_down_cap_ml": None,
    "ph_down_rem_ml": None,
    "ph_down_empty": None,
    "stock_alert": None,

    "ambient_temp_c": None,
    "ambient_humidity_pct": None,
    "water_temp_c": None,
    "light_adc": None,
    "water_level_pct": None,
    "tds_ppm": None,
    "ph": None,
    "co2_ppm": None,

    "water_pump_on": None,
    "drain_pump_on": None,
    "nutrient_pump_on": None,
    "ph_up_pump_on": None,
    "ph_down_pump_on": None,
    "led_on": None,
    "fan_on": None,

    "temperature": None,
    "humidity": None,
    "light_intensity": None,
    "water_level": None,
    "ec": None,
    "pump_on": None,

    "sim_time": datetime.now(),
}
