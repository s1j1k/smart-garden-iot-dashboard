import random
from datetime import timedelta

from config import GROW_PRESETS, WATER_LEVEL_MAX_PERCENT, apply_grow_preset, normalize_stage_name, normalize_stage_name
from sensors_fake import generate_environment, clamp
from control import run_control_logic


def sync_stage_targets(state, config):
    """Keep simulation state targets aligned with the selected grow stage."""
    stage = normalize_stage_name(config.get("grow_stage", state.get("grow_stage", "seedling")))
    preset = apply_grow_preset(config, stage)

    state["grow_stage"] = stage
    state["water_target_pct"] = preset["water_level_target_pct"]
    state["water_max_pct"] = config.get("water_level_max_pct", WATER_LEVEL_MAX_PERCENT)
    state["nutrient_tds_min"] = preset["nutrient_tds_min"]
    state["nutrient_tds_max"] = preset["nutrient_tds_max"]
    state["ph_target_min"] = preset["ph_min"]
    state["ph_target_max"] = preset["ph_max"]
    return state


def update_physics(state, config):
    """
    Update simulated hydroponic environment state for the ESP32 sensor set:
    DHT11 ambient, DS18B20 water temp, LDR, water level, TDS, pH, CO2.
    """

    sunlight, ext_temp, ext_humidity = generate_environment(state["sim_time"])

    led_contribution = 900 if state.get("led_on") else 0
    light_adc = round(clamp(sunlight * 3.5 + led_contribution + random.uniform(-40, 40), 0, 4095), 0)
    state["light_adc"] = light_adc
    state["light_intensity"] = light_adc

    ambient_temp = ext_temp + random.uniform(-0.3, 0.3)
    if state.get("led_on"):
        ambient_temp += 1.0
    if state.get("fan_on"):
        ambient_temp -= 1.5
    state["ambient_temp_c"] = round(ambient_temp, 2)
    state["temperature"] = state["ambient_temp_c"]

    humidity = ext_humidity
    if state.get("water_pump_on") or state.get("pump_on") or state.get("drain_pump_on"):
        humidity += 1.0
    if state.get("fan_on"):
        humidity -= 3
    state["ambient_humidity_pct"] = round(clamp(humidity, 30, 95), 2)
    state["humidity"] = state["ambient_humidity_pct"]

    water_temp = state.get("water_temp_c", 22.0)
    water_temp += (ambient_temp - water_temp) * 0.05 + random.uniform(-0.1, 0.1)
    state["water_temp_c"] = round(clamp(water_temp, 10, 40), 2)

    water_level = state.get("water_level_pct", state.get("water_level", 35.0))
    water_level -= random.uniform(0.05, 0.2)
    if state.get("water_pump_on") or state.get("pump_on"):
        water_level += random.uniform(1.5, 3.0)
    if state.get("nutrient_pump_on"):
        water_level += random.uniform(0.05, 0.15)
    if state.get("drain_pump_on"):
        water_level -= random.uniform(1.0, 2.0)
    state["water_level_pct"] = round(clamp(water_level, 0, 100), 2)
    state["water_level"] = state["water_level_pct"]

    tds = state.get("tds_ppm", 320.0)
    tds -= random.uniform(1.0, 4.0)
    if state.get("nutrient_pump_on"):
        tds += random.uniform(40, 80)
    if state.get("water_pump_on") or state.get("pump_on"):
        tds -= random.uniform(10, 25)
    state["tds_ppm"] = round(clamp(tds, 0, 2500), 0)
    state["ec"] = round(state["tds_ppm"] / 500.0, 2)

    ph = state.get("ph", 5.7)
    ph += random.uniform(-0.03, 0.03)
    if state.get("ph_up_pump_on"):
        ph += random.uniform(0.08, 0.16)
    if state.get("ph_down_pump_on"):
        ph -= random.uniform(0.08, 0.16)
    state["ph"] = round(clamp(ph, 4.5, 8.0), 2)

    co2 = state.get("co2_ppm", 450.0)
    co2 += random.uniform(-12, 12)
    if state.get("fan_on"):
        co2 -= random.uniform(20, 40)
    state["co2_ppm"] = round(clamp(co2, 350, 2000), 0)

    if state.get("ph_up_pump_on"):
        state["ph_dose"] = "up"
    elif state.get("ph_down_pump_on"):
        state["ph_dose"] = "down"
    elif state.get("nutrient_dose") == "dilute" and (
        state.get("water_pump_on") or state.get("pump_on")
    ):
        state["ph_dose"] = "none"
    else:
        state["ph_dose"] = state.get("ph_dose", "none")
        if not state.get("ph_up_pump_on") and not state.get("ph_down_pump_on"):
            if state.get("ph_dose") in {"up", "down"}:
                state["ph_dose"] = "none"

    if state.get("nutrient_pump_on"):
        state["nutrient_dose"] = "dose"
    elif state.get("nutrient_dose") == "dilute":
        state["nutrient_dose"] = "dilute"
    else:
        state["nutrient_dose"] = "none"

    return state


def step_system(state, config, actuators):
    """
    One full closed-loop step:
    sensor state → control logic → actuator update → physical update
    """

    state["sim_time"] += timedelta(minutes=config["step_minutes"])
    state = sync_stage_targets(state, config)

    desired_actions, alerts = run_control_logic(state, config)

    actuators.set_water_pump(desired_actions["water_pump_on"])
    actuators.set_drain_pump(desired_actions.get("drain_pump_on", False))
    actuators.set_nutrient_pump(desired_actions["nutrient_pump_on"])
    actuators.set_ph_up_pump(desired_actions.get("ph_up_pump_on", False))
    actuators.set_ph_down_pump(desired_actions.get("ph_down_pump_on", False))
    actuators.set_led(desired_actions.get("led_on"))
    actuators.set_fan(desired_actions.get("fan_on"))

    actuator_states = actuators.get_states()
    state.update(actuator_states)
    state["ph_dose"] = desired_actions.get("ph_dose", "none")
    state["nutrient_dose"] = desired_actions.get("nutrient_dose", "none")

    state = update_physics(state, config)

    return state, alerts
