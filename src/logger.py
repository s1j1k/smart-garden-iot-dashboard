import pandas as pd


def _as_flag(value):
    if value is None:
        return None
    return int(bool(value))


def log_state(history, state, config):
    record = {
        "time": state["sim_time"],
        "mode": config.get("mode", "MQTT"),
        "grow_stage": state.get("grow_stage") or "",
        "hydroponic_mode": state.get("hydroponic_mode") or "",
        "ph_settle_s": state.get("ph_settle_s"),

        "ambient_temp_c": state.get("ambient_temp_c", state.get("temperature")),
        "ambient_humidity_pct": state.get("ambient_humidity_pct", state.get("humidity")),
        "water_temp_c": state.get("water_temp_c"),
        "light_adc": state.get("light_adc", state.get("light_intensity")),
        "water_level_pct": state.get("water_level_pct", state.get("water_level")),
        "water_target_pct": state.get("water_target_pct"),
        "tds_ppm": state.get("tds_ppm"),
        "ph": state.get("ph"),
        "ph_raw": state.get("ph_raw"),
        "ph_filtered": state.get("ph_filtered"),
        "ph_voltage": state.get("ph_voltage"),
        "co2_ppm": state.get("co2_ppm"),

        # 0/1/None so in-memory history matches the SQLite-ready CSV export.
        "water_pump_on": _as_flag(state.get("water_pump_on", state.get("pump_on"))),
        "drain_pump_on": _as_flag(state.get("drain_pump_on")),
        "nutrient_pump_on": _as_flag(state.get("nutrient_pump_on")),
        "ph_up_pump_on": _as_flag(state.get("ph_up_pump_on")),
        "ph_down_pump_on": _as_flag(state.get("ph_down_pump_on")),
        "led_on": _as_flag(state.get("led_on")),
        "fan_on": _as_flag(state.get("fan_on")),
        "ph_dose": state.get("ph_dose"),
        "nutrient_dose": state.get("nutrient_dose"),
        "water_rem_ml": state.get("water_rem_ml"),
        "nutrient_rem_ml": state.get("nutrient_rem_ml"),
        "ph_up_rem_ml": state.get("ph_up_rem_ml"),
        "ph_down_rem_ml": state.get("ph_down_rem_ml"),
        "stock_alert": state.get("stock_alert"),
    }

    history.append(record)
    return history


def history_to_dataframe(history):
    if not history:
        return pd.DataFrame()

    return pd.DataFrame(history)


def add_event(event_log, sim_time, message):
    event_log.insert(
        0,
        {
            "time": sim_time,
            "event": message,
        },
    )

    return event_log[:50]
