def run_control_logic(state, config):
    """
    Threshold-based closed-loop control for offline simulation.

    Mirrors the ESP32 firmware paths:
      - water refill toward stage target (hard max WATER_LEVEL_MAX)
      - reverse drain when above the ceiling
      - nutrient dose / fresh-water dilution from TDS bounds
      - pH up / pH down dosing

    Grow light (led_on) and fan (fan_on) are placeholders for upcoming
    hardware. Simulation can drive them; live MQTT leaves them unset until
    the ESP32 publishes those fields.
    """

    alerts = []
    simulate_placeholders = config.get("simulate_placeholder_actuators", True)

    water_level = state.get("water_level_pct", state.get("water_level", 0.0))
    tds_ppm = state.get("tds_ppm", state.get("ec", 0.0) * 500.0)
    light_adc = state.get("light_adc", state.get("light_intensity", 0.0))
    ambient_temp = state.get("ambient_temp_c", state.get("temperature", 0.0))
    ph = state.get("ph", 0.0)
    co2_ppm = state.get("co2_ppm", 0.0)
    water_target = state.get("water_target_pct", config.get("water_level_target_pct", 30.0))
    water_max = state.get("water_max_pct", config.get("water_level_max_pct", 50.0))
    # Act only outside the controller's dead limits, as the ESP32 does.
    tds_min = state.get("ec_dead_low", config.get("tds_low_threshold", 550.0))
    tds_max = state.get("ec_dead_high", config.get("tds_high_threshold", 850.0))
    ph_min = state.get("ph_dead_low", config.get("ph_low_threshold", 5.5))
    ph_max = state.get("ph_dead_high", config.get("ph_high_threshold", 6.5))

    desired = {
        "water_pump_on": False,
        "drain_pump_on": False,
        "nutrient_pump_on": False,
        "ph_up_pump_on": False,
        "ph_down_pump_on": False,
        "pump_on": False,
        "led_on": None,
        "fan_on": None,
        "ph_dose": "none",
        "nutrient_dose": "none",
    }

    if config["mode"] == "MANUAL":
        desired["water_pump_on"] = config.get("manual_water_pump", config.get("manual_pump", False))
        desired["drain_pump_on"] = config.get("manual_drain_pump", False)
        desired["nutrient_pump_on"] = config.get("manual_nutrient_pump", False)
        desired["ph_up_pump_on"] = config.get("manual_ph_up_pump", False)
        desired["ph_down_pump_on"] = config.get("manual_ph_down_pump", False)
        desired["pump_on"] = desired["water_pump_on"]
        if simulate_placeholders:
            desired["led_on"] = config.get("manual_led", False)
            desired["fan_on"] = config.get("manual_fan", False)
        desired["ph_dose"] = (
            "up" if desired["ph_up_pump_on"] else
            "down" if desired["ph_down_pump_on"] else
            "none"
        )
        desired["nutrient_dose"] = "dose" if desired["nutrient_pump_on"] else "none"
        return desired, alerts

    water_control = config.get("water_control_enabled", True)
    nutrient_dosing = config.get("nutrient_dosing_enabled", True)
    nutrient_dilution = config.get("nutrient_dilution_enabled", True)
    ph_control = config.get("ph_control_enabled", True)

    # Reverse drain takes priority when the reservoir is over the hard ceiling.
    if water_control and water_level > water_max:
        desired["drain_pump_on"] = True
        alerts.append(f"Water level above {water_max:.0f}% max: reverse drain.")
        _apply_placeholder_actuators(
            desired,
            state,
            config,
            light_adc,
            ambient_temp,
            alerts,
            simulate_placeholders,
        )
        if co2_ppm > config.get("co2_high_threshold", 1200.0):
            alerts.append("CO2 above comfort threshold.")
        return desired, alerts

    if water_control and water_level <= config.get("water_level_low_threshold", water_target):
        alerts.append("Water reservoir at/below stage target.")

    # Water refill — never add above the hard ceiling.
    if water_control and water_level < water_max:
        if state.get("pump_timer", 0) > 0:
            desired["water_pump_on"] = True
            state["pump_timer"] -= 1
        elif water_level <= water_target:
            desired["water_pump_on"] = True
            state["pump_timer"] = config["pump_duration_steps"] - 1
            alerts.append("Water level below stage target: refill activated.")

    desired["pump_on"] = desired["water_pump_on"]

    # Nutrient dose / dilute (mutually exclusive with water refill in firmware;
    # simulation approximates with priority: refill > dose > dilute).
    if desired["water_pump_on"]:
        state["nutrient_timer"] = 0
        state["dilute_timer"] = 0
    elif state.get("nutrient_timer", 0) > 0:
        desired["nutrient_pump_on"] = True
        desired["nutrient_dose"] = "dose"
        state["nutrient_timer"] -= 1
    elif state.get("dilute_timer", 0) > 0:
        if water_level < water_max:
            desired["water_pump_on"] = True
            desired["pump_on"] = True
            desired["nutrient_dose"] = "dilute"
            state["dilute_timer"] -= 1
        else:
            state["dilute_timer"] = 0
            alerts.append("Nutrient dilution blocked — water at max ceiling.")
    elif nutrient_dosing and tds_ppm < tds_min:
        desired["nutrient_pump_on"] = True
        desired["nutrient_dose"] = "dose"
        state["nutrient_timer"] = config["nutrient_duration_steps"] - 1
        alerts.append("TDS low: nutrient pump activated.")
    elif nutrient_dilution and tds_ppm > tds_max:
        if water_level < water_max:
            desired["water_pump_on"] = True
            desired["pump_on"] = True
            desired["nutrient_dose"] = "dilute"
            state["dilute_timer"] = config.get("dilute_duration_steps", 1) - 1
            alerts.append("TDS high: diluting with fresh water.")
        else:
            alerts.append("TDS high, but dilution blocked — water at max ceiling.")

    # pH dosing (firmware pauses while water refill / drain / nutrient active).
    if (
        ph_control
        and not desired["water_pump_on"]
        and not desired["drain_pump_on"]
        and not desired["nutrient_pump_on"]
    ):
        if state.get("ph_timer", 0) > 0:
            direction = state.get("ph_dose", "none")
            if direction == "up":
                desired["ph_up_pump_on"] = True
                desired["ph_dose"] = "up"
            elif direction == "down":
                desired["ph_down_pump_on"] = True
                desired["ph_dose"] = "down"
            state["ph_timer"] -= 1
        elif ph < ph_min:
            desired["ph_up_pump_on"] = True
            desired["ph_dose"] = "up"
            state["ph_dose"] = "up"
            state["ph_timer"] = config.get("ph_dose_duration_steps", 1) - 1
            alerts.append("pH below target: pH-up dosing.")
        elif ph > ph_max:
            desired["ph_down_pump_on"] = True
            desired["ph_dose"] = "down"
            state["ph_dose"] = "down"
            state["ph_timer"] = config.get("ph_dose_duration_steps", 1) - 1
            alerts.append("pH above target: pH-down dosing.")

    _apply_placeholder_actuators(
        desired,
        state,
        config,
        light_adc,
        ambient_temp,
        alerts,
        simulate_placeholders,
    )

    if co2_ppm > config.get("co2_high_threshold", 1200.0):
        alerts.append("CO2 above comfort threshold.")

    return desired, alerts


def _apply_placeholder_actuators(
    desired,
    state,
    config,
    light_adc,
    ambient_temp,
    alerts,
    simulate_placeholders,
):
    """Drive grow-light / fan placeholders in simulation only."""
    if not simulate_placeholders:
        desired["led_on"] = None
        desired["fan_on"] = None
        if light_adc < config.get("light_on_threshold", 800.0):
            alerts.append("Light low — grow light not installed (no telemetry).")
        if ambient_temp > config.get("temp_fan_on_threshold", 29.0):
            alerts.append("Ambient temperature high — fan not installed (no telemetry).")
        return

    # Hysteresis for grow light placeholder.
    led_on = bool(state.get("led_on") or False)
    if light_adc < config.get("light_on_threshold", 800.0):
        led_on = True
        alerts.append("Light low: grow light placeholder ON (simulation only).")
    elif light_adc > config.get("light_off_threshold", 1600.0):
        led_on = False
    desired["led_on"] = led_on

    # Hysteresis for fan placeholder.
    fan_on = bool(state.get("fan_on") or False)
    if ambient_temp > config.get("temp_fan_on_threshold", 29.0):
        fan_on = True
        alerts.append("Ambient temperature high: fan placeholder ON (simulation only).")
    elif ambient_temp < config.get("temp_fan_off_threshold", 26.0):
        fan_on = False
    desired["fan_on"] = fan_on
