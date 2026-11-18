"""Alert engine: turns the latest ESP32 state into faults, warnings and reminders.

One function, ``evaluate(state, ...)``, is used both by the dashboard (Alerts
page, Home strip, browser notifications) and by the always-on notifier
(``scripts/alert_notifier.py``), so phone and computer see the same alerts.

Severities
    fault    something is broken or a reading cannot be trusted; act now
    warning  the garden needs attention (calibration, climate, levels)
    info     a reminder, e.g. time to change the grow stage

``notify_after_s`` is how long a condition must last before a push
notification goes out, so a single noisy reading does not page anyone. The
dashboard shows every alert straight away.

Every alert key is listed in ``CATALOGUE`` (documented in README "Alerts").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from config import normalize_stage_name
from fmt_range import fmt_range
from growth_targets import (
    CLIMATE_LABELS,
    climate_band,
    climate_stage,
    hint_for,
    ph_band,
    stage_change_due,
    tds_band,
)

SEVERITY_ORDER = {"fault": 0, "warning": 1, "info": 2}
SEVERITY_ICON = {"fault": "🔴", "warning": "🟠", "info": "🔵"}

# Settle and warm-up allowances, in seconds.
MIN = 60
HOUR = 3600


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str
    category: str
    title: str
    detail: str = ""
    action: str = ""
    notify_after_s: int = 0
    # Where in the dashboard to fix it (page title).
    fix_in: str = ""

    def as_dict(self):
        return {
            "key": self.key, "severity": self.severity, "category": self.category,
            "title": self.title, "detail": self.detail, "action": self.action,
        }


@dataclass(frozen=True)
class CatalogueEntry:
    key: str
    severity: str
    category: str
    trigger: str
    action: str
    notify_after: str


# Every alert the engine can raise. Keys ending in ``.*`` are per-device families.
CATALOGUE = [
    CatalogueEntry("node_offline", "fault", "System",
                   "No telemetry from the ESP32 for 90 s (the node reports every ~10 s).",
                   "Check the node's power and WiFi; type `MQTT CONNECT` on its serial console.",
                   "5 min"),
    CatalogueEntry("sensor_fault.*", "fault", "Sensors",
                   "The ESP32 marks a sensor as FAULT (pH, TDS, water level, water temp, "
                   "air temp/humidity, CO₂, light) — e.g. no valid pH, a stuck level stick, "
                   "DS18B20 reading -127 °C.",
                   "Follow the message (reseat the probe, power-cycle, lift the level stick dry).",
                   "2 min"),
    CatalogueEntry("implausible.*", "fault", "Sensors",
                   "A reading is physically impossible (pH outside 0–14, water outside 0–45 °C, "
                   "TDS outside 0–3000 ppm, humidity outside 0–100 %).",
                   "Treat the probe as faulty: check wiring and calibration before trusting dosing.",
                   "5 min"),
    CatalogueEntry("bottle_empty.*", "fault", "Nutrients",
                   "A dosing bottle is estimated empty; the ESP32 has stopped that pump.",
                   "Refill the bottle, then type `FILL` on the node serial console.", "now"),
    CatalogueEntry("actuator_fault.*", "fault", "System",
                   "The ESP32 marks a pump, the grow light or auto control as FAULT.",
                   "Read the message; check the pump tubing / wiring.", "2 min"),
    CatalogueEntry("grow_light_mismatch", "warning", "Light",
                   "The grow light is on when its schedule says off, or off when it should be on.",
                   "Check the LED supply and the GPIO 27 data line.", "10 min"),
    CatalogueEntry("uncalibrated.ph_probe", "warning", "Calibration",
                   "The pH probe is running on default calibration.",
                   "Settings → Advanced → Calibration → pH probe (buffers 4.00 and 9.18).",
                   "30 min"),
    CatalogueEntry("uncalibrated.<pump>", "warning", "Calibration",
                   "A dosing pump (water, nutrient, pH-up, pH-down) has no saved flow rate, "
                   "so doses are guesses.",
                   "Settings → Advanced → Calibration → Peristaltic pumps.", "30 min"),
    CatalogueEntry("water_level_init", "warning", "Water",
                   "The water-level stick needs initialising; top-ups are paused.",
                   "Lift the stick out of the water and lower it slowly back in.", "10 min"),
    CatalogueEntry("session_near_cap.<loop>", "warning", "Dosing",
                   "pH or nutrient dosing has used 80 % of its per-session limit.",
                   "Check the reading with a strip and that the pump is delivering.", "now"),
    CatalogueEntry("session_cap.<loop>", "warning", "Dosing",
                   "pH or nutrient dosing reached its per-session limit and has stopped.",
                   "Find out why (reading, dosing line); an admin can allow more dosing in Settings.",
                   "now"),
    CatalogueEntry("bottle_low", "warning", "Nutrients",
                   "A dosing bottle is running low.", "Refill soon, then type `FILL`.", "now"),
    CatalogueEntry("control_held", "warning", "System",
                   "Auto control is held (calibration running, bottle empty, sensor not ready).",
                   "Read the reason; it clears by itself once the cause is fixed.", "30 min"),
    CatalogueEntry("node_idle", "warning", "Growth stage",
                   "Plants are growing but the node is idle, so nothing is auto-controlled.",
                   "Grow stage → choose Seedling (or the suggested stage) → Apply.", "1 h"),
    CatalogueEntry("ph_out_of_range", "warning", "Water",
                   "Control pH (filtered) is outside the stage's target band.",
                   "Usually self-corrects on the next pH check; if it persists check the "
                   "pH bottles and calibration.", "4 h (30 min below 5.0 or above 7.0)"),
    CatalogueEntry("tds_low", "warning", "Nutrients",
                   "Control TDS is below the stage's target band.",
                   "Normally the next EC check doses nutrient, unless this fill's nutrient limit is used up "
                   "(then top up or refill); check the nutrient bottle.", "6 h"),
    CatalogueEntry("tds_high", "warning", "Nutrients",
                   "Control TDS is above the stage's band. The ESP32 never dilutes.",
                   "Top the reservoir up with plain water by hand.", "2 h"),
    CatalogueEntry("water_low", "warning", "Water",
                   "Water level is at or below 10 % — roots may be drying out.",
                   "Refill the water jug / reservoir.", "15 min"),
    CatalogueEntry("climate.air_temp_c", "warning", "Climate",
                   "Air temperature is outside the stage's range (e.g. 18–27 °C vegetative).",
                   "Move the garden, ventilate, or adjust room heating/cooling.", "1 h"),
    CatalogueEntry("climate.humidity_pct", "warning", "Climate",
                   "Humidity is outside the stage's range (e.g. 45–70 % vegetative).",
                   "Add a humidity dome for seedlings; ventilate when it is too humid.", "1 h"),
    CatalogueEntry("climate.water_temp_c", "warning", "Climate",
                   "Water temperature is outside 18–24 °C (16–24 °C at harvest). Warm water "
                   "holds less oxygen and invites root rot.",
                   "Shade the reservoir, keep it off warm floors, or add a frozen bottle.", "1 h"),
    CatalogueEntry("climate.co2_ppm", "warning", "Climate",
                   "CO₂ is below 400 ppm (sensor fault likely) or above 1500 ppm "
                   "(room is not ventilated).",
                   "Ventilate the room.", "1 h"),
    CatalogueEntry("stage_change_due", "info", "Growth stage",
                   "Plant age says the next grow stage is due (week 2 transitional, week 4 "
                   "vegetative, week 7 harvest) but the node is still on an earlier stage.",
                   "Grow stage → check the hints and photo → Apply the new stage.", "now"),
    CatalogueEntry("upload_fault", "info", "System",
                   "Cloud backup (InfluxDB) uploads are failing.",
                   "Readings are still stored locally; check WiFi / Influx token.", "1 h"),
]

# Device keys the ESP32 reports in ``health`` and how to name them.
SENSOR_KEYS = {
    "ph": "pH probe", "tds": "TDS probe", "water_level": "Water-level stick",
    "water_temp": "Water temperature probe", "ambient": "Air temp / humidity sensor",
    "co2": "CO₂ sensor", "light": "Light sensor",
}
ACTUATOR_KEYS = {
    "water_pump": "Water top-up pump", "nutrient_pump": "Nutrient pump",
    "ph_up_pump": "pH-up pump", "ph_down_pump": "pH-down pump",
    "grow_light": "Grow light", "auto_control": "Auto control",
}
PUMPS = {
    "water": ("water_calibrated", "Water top-up pump"),
    "nutrient": ("nutrient_calibrated", "Nutrient pump"),
    "ph_up": ("ph_up_calibrated", "pH-up pump"),
    "ph_down": ("ph_down_calibrated", "pH-down pump"),
}
BOTTLES = {
    "water": "Water jug", "nutrient": "Nutrient bottle",
    "ph_up": "pH-up bottle", "ph_down": "pH-down bottle",
}
IMPLAUSIBLE = {
    "ph": (0.0, 14.0, "pH", ""),
    "water_temp_c": (0.0, 45.0, "Water temperature", " °C"),
    "tds_ppm": (0.0, 3000.0, "TDS", " ppm"),
    "ambient_humidity_pct": (0.0, 100.0, "Humidity", " %"),
}


# Sensor health key behind each reading (an already-faulted sensor is not re-flagged).
IMPLAUSIBLE_SENSOR = {"ph": "ph", "water_temp_c": "water_temp", "tds_ppm": "tds",
                      "ambient_humidity_pct": "ambient"}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _offline_alerts(node_live, last_seen_s):
    if node_live:
        return []
    ago = "" if last_seen_s is None else f" Last message {int(last_seen_s // 60)} min ago."
    return [Alert("node_offline", "fault", "System", "Garden controller is offline",
                  "No telemetry from the ESP32, so nothing on this page is current." + ago,
                  "Check the node's power and WiFi; on its serial console type `MQTT CONNECT`.",
                  notify_after_s=5 * MIN, fix_in="Settings")]


def _health_alerts(state):
    out = []
    health = state.get("health") or {}
    msgs = state.get("health_msg") or {}
    for key, name in SENSOR_KEYS.items():
        if health.get(key) == "fault":
            out.append(Alert(f"sensor_fault.{key}", "fault", "Sensors", f"{name} fault",
                             msgs.get(key, "The ESP32 cannot get a valid reading."),
                             "Reseat or power-cycle the sensor; readings are not trusted until "
                             "it clears.", notify_after_s=2 * MIN, fix_in="Settings"))
    for key, name in ACTUATOR_KEYS.items():
        if key in NO_RESPONSE_PUMP_KEYS and state.get(NO_RESPONSE_PUMP_KEYS[key]):
            continue  # _no_response_alerts says what to do
        if health.get(key) == "fault":
            out.append(Alert(f"actuator_fault.{key}", "fault", "System", f"{name} fault",
                             msgs.get(key, ""), "Check wiring and tubing.",
                             notify_after_s=2 * MIN, fix_in="Settings"))
    if health.get("auto_control") == "hold":
        out.append(Alert("control_held", "warning", "System", "Auto control is paused",
                         msgs.get("auto_control", "Dosing is on hold."),
                         "It resumes by itself once the reason clears.",
                         notify_after_s=30 * MIN, fix_in="Settings"))
    if health.get("influx") == "fault":
        out.append(Alert("upload_fault", "info", "System", "Cloud backup is failing",
                         msgs.get("influx", ""), "Readings are still stored on this dashboard.",
                         notify_after_s=HOUR, fix_in="Settings"))
    # Firmware before 1.6 has no health block, only flags.
    if not health:
        if state.get("ph_sensor_fault"):
            out.append(Alert("sensor_fault.ph", "fault", "Sensors", "pH probe fault",
                             "No valid pH reading.", "Reseat the probe and check its 5 V supply.",
                             notify_after_s=2 * MIN, fix_in="Settings"))
        if state.get("water_level_sensor_fault"):
            out.append(Alert("sensor_fault.water_level", "fault", "Sensors",
                             "Water-level stick fault", "Stuck reading after a fill.",
                             "Lift it out, dry it, power-cycle the node.",
                             notify_after_s=2 * MIN, fix_in="Settings"))
    return out


def _implausible_alerts(state):
    out = []
    health = state.get("health") or {}
    for field, (lo, hi, name, unit) in IMPLAUSIBLE.items():
        value = _num(state.get(field))
        if value is None or lo <= value <= hi or health.get(IMPLAUSIBLE_SENSOR[field]) == "fault":
            continue
        out.append(Alert(f"implausible.{field}", "fault", "Sensors",
                         f"{name} reading looks wrong",
                         f"{name} reads {value:g}{unit}, outside the possible {lo:g}–{hi:g}{unit}.",
                         "Check the probe wiring and calibration before trusting any dosing.",
                         notify_after_s=5 * MIN, fix_in="Settings"))
    return out


def _calibration_alerts(state):
    out = []
    if state.get("ph_probe_calibrated") is False:
        out.append(Alert("uncalibrated.ph_probe", "warning", "Calibration",
                         "pH probe is not calibrated",
                         "Readings use default values, so pH may be off by a few tenths.",
                         "Settings → Advanced → Calibration → pH probe.",
                         notify_after_s=30 * MIN, fix_in="Settings"))
    for pump, (flag, name) in PUMPS.items():
        if state.get(flag) is False:
            out.append(Alert(f"uncalibrated.{pump}", "warning", "Calibration",
                             f"{name} is not calibrated",
                             "Without a measured flow rate the dose sizes are guesses.",
                             "Settings → Advanced → Calibration → Peristaltic pumps.",
                             notify_after_s=30 * MIN, fix_in="Settings"))
    return out


def _stock_alerts(state):
    out = []
    for bottle, name in BOTTLES.items():
        if state.get(f"{bottle}_empty"):
            out.append(Alert(f"bottle_empty.{bottle}", "fault", "Nutrients", f"{name} is empty",
                             "The ESP32 has stopped this pump so it does not run dry.",
                             "Refill it, then type `FILL` on the node serial console."))
    stock = str(state.get("stock_alert") or "").strip()
    if stock and "empty" not in stock:
        out.append(Alert("bottle_low", "warning", "Nutrients", "A dosing bottle is running low",
                         stock.replace("_", " "), "Refill soon, then type `FILL`."))
    return out


def _water_alerts(state, stage):
    out = []
    if state.get("water_level_init_required"):
        out.append(Alert("water_level_init", "warning", "Water",
                         "Water-level stick needs initialising",
                         "Top-ups are paused until it has a reference level.",
                         "Lift the stick out of the water and lower it slowly back in.",
                         notify_after_s=10 * MIN))
        return out
    level = _num(state.get("water_level_pct"))
    health = state.get("health") or {}
    if level is not None and 0 <= level <= 10 and health.get("water_level") != "fault" \
            and stage not in (None, "idle", "filling", "sensor_baseline"):
        out.append(Alert("water_low", "warning", "Water", "Reservoir water is low",
                         f"Water level {level:.0f} %.", "Refill the water jug / reservoir.",
                         notify_after_s=15 * MIN))
    return out


def _chemistry_alerts(state, stage):
    out = []
    health = state.get("health") or {}
    ph = _num(state.get("ph_ema"))
    band = ph_band(state, stage)
    if ph is not None and band and health.get("ph") != "fault" and not state.get("ph_sensor_fault") \
            and stage not in (None, "idle", "sensor_baseline", "filling"):
        lo, hi = band
        if not lo <= ph <= hi:
            extreme = ph < 5.0 or ph > 7.0
            out.append(Alert("ph_out_of_range", "warning", "Water",
                             f"pH is {'low' if ph < lo else 'high'} ({ph:.2f})",
                             f"Acceptable {fmt_range(lo, hi, 1)}. Outside this range roots cannot take up "
                             "iron, calcium and other nutrients properly.",
                             "The next pH check doses to correct it; if it persists check the pH "
                             "bottles and calibration.",
                             notify_after_s=30 * MIN if extreme else 4 * HOUR))
    tds = _num(state.get("tds_ppm_ema"))
    band = tds_band(state, stage)
    if tds is not None and band and health.get("tds") != "fault" \
            and stage in ("transitional", "vegetative", "harvest"):
        lo, hi = band
        high_above = _num(((state.get("sched") or {}).get("ec") or {}).get("high_above")) or hi
        if tds < lo:
            out.append(Alert("tds_low", "warning", "Nutrients", f"Nutrient is low ({tds:.0f} ppm)",
                             f"Acceptable {fmt_range(lo, hi, 0, ' ppm')}. Low feed shows as pale, slow growth.",
                             "The next EC check doses nutrient; make sure the bottle is not empty.",
                             notify_after_s=6 * HOUR))
        elif tds > high_above:
            out.append(Alert("tds_high", "warning", "Nutrients",
                             f"Nutrient is too strong ({tds:.0f} ppm)",
                             f"Acceptable {fmt_range(lo, hi, 0, ' ppm')}. Strong feed burns leaf tips and the "
                             "ESP32 never dilutes by itself.",
                             "Top the reservoir up with plain water by hand.",
                             notify_after_s=2 * HOUR))
    return out


def _climate_alerts(state, stage):
    out = []
    fields = {"air_temp_c": "ambient_temp_c", "humidity_pct": "ambient_humidity_pct",
              "water_temp_c": "water_temp_c", "co2_ppm": "co2_ppm"}
    health = state.get("health") or {}
    faulty = {"air_temp_c": "ambient", "humidity_pct": "ambient",
              "water_temp_c": "water_temp", "co2_ppm": "co2"}
    for metric, field in fields.items():
        band = climate_band(stage, metric)
        value = _num(state.get(field))
        if band is None or value is None or health.get(faulty[metric]) == "fault":
            continue
        lo, hi = band
        if lo <= value <= hi:
            continue
        name, unit = CLIMATE_LABELS[metric]
        word = "low" if value < lo else "high"
        out.append(Alert(f"climate.{metric}", "warning", "Climate",
                         f"{name} is {word} ({value:.0f} {unit})",
                         f"{stage.title()} plants do best at {fmt_range(lo, hi, None, ' ' + unit)}.",
                         CLIMATE_ACTIONS[metric][word], notify_after_s=HOUR))
    return out


CLIMATE_ACTIONS = {
    "air_temp_c": {"low": "Move the garden somewhere warmer or raise the room heating.",
                   "high": "Ventilate or move it out of direct sun; the light adds heat too."},
    "humidity_pct": {"low": "Use a humidity dome (seedlings) or a tray of water nearby.",
                     "high": "Improve airflow — still, damp air invites mould and mildew."},
    "water_temp_c": {"low": "Keep the reservoir off cold floors; an aquarium heater helps.",
                     "high": "Shade the reservoir and keep it off warm floors; warm water holds "
                             "less oxygen."},
    "co2_ppm": {"low": "Below outdoor air (~420 ppm) usually means the CO₂ sensor needs a reset.",
                "high": "Open a window — the room is stuffy for plants and people."},
}


def _light_alerts(state, now):
    from grow_light_panel import photoperiod

    gl = state.get("grow_light") or {}
    if not gl or gl.get("mode") in ("animating", "fade_in", "fade_out", "flash"):
        return []
    sched = photoperiod(gl, state.get("received_at"), now)
    if sched is None or gl.get("on") is None or bool(gl["on"]) == sched["want_on"]:
        return []
    expected = "on" if sched["want_on"] else "off"
    return [Alert("grow_light_mismatch", "warning", "Light",
                  f"Grow light is {'on' if gl['on'] else 'off'} but should be {expected}",
                  "The photoperiod gives 16 h light and 8 h dark each day.",
                  "Check the LED supply and the GPIO 27 data line.", notify_after_s=10 * MIN)]


def _stage_alerts(actual, suggested, week, planted):
    out = []
    if not planted:
        return out
    if actual in (None, "idle"):
        out.append(Alert("node_idle", "warning", "Growth stage",
                         "Auto control is off (node is idle)",
                         "Plants are growing but nothing is dosing pH, nutrient or water.",
                         f"Grow stage → Apply {suggested or 'seedling'}.",
                         notify_after_s=HOUR, fix_in="Grow stage"))
        return out
    if stage_change_due(actual, suggested):
        hint = hint_for(actual)
        out.append(Alert("stage_change_due", "info", "Growth stage",
                         f"Time to check for {suggested.title()}?",
                         f"Plants are in week {week}; they are usually ready to move from "
                         f"{actual} to {suggested} about now. {hint.when}",
                         "Grow stage → compare the photo with the hints → Apply the new stage.",
                         fix_in="Grow stage"))
    return out


# (key, label, telemetry flag, prime command, reading, loop to reset)
NO_RESPONSE = (("ph_down", "pH-down", "ph_down_no_response", "PRIME PH_DOWN", "the pH", "pH"),
               ("ph_up", "pH-up", "ph_up_no_response", "PRIME PH_UP", "the pH", "pH"),
               ("nutrient", "Nutrient", "nutrient_no_response", "PRIME NUTRIENT", "the TDS",
                "nutrient"))
NO_RESPONSE_PUMP_KEYS = {f"{key}_pump": flag for key, _, flag, *_ in NO_RESPONSE}


def _no_response_alerts(state):
    """Doses that settled without moving their reading: the controller has stopped
    dosing that pump (a pump, line or probe fault) until an admin clears it. Admin
    site only: it is not something for visitors to the public dashboard.
    """
    from admin_auth import is_admin_site

    if not is_admin_site():
        return []
    out = []
    for key, label, flag, prime, reading, loop in NO_RESPONSE:
        if not state.get(flag):
            continue
        out.append(Alert(
            f"no_response.{key}", "fault", "Dosing",
            f"{label} dosing stopped",
            f"Its last few doses left {reading} where it was, so the controller has stopped "
            "dosing it.",
            f"Check the {label.lower()} pump runs and its line reaches the tank (`{prime}` on "
            f"the serial console, then `STOP`), and that the probe reads correctly. A heavily "
            f"buffered tank can also do this. Then clear the fault under Settings.",
            fix_in="Settings"))
    return out


def _refill_alerts(state):
    """Level loop (firmware 1.9.24+): a refill on hold, a refill just finished, a paused loop."""
    out = []
    refill = state.get("refill") if isinstance(state.get("refill"), dict) else {}
    if refill.get("hold"):
        why = {"noisy_level": "the level stick's readings jumped by two or more steps",
               "no_level_rise": "two steps' worth of water went in and the level did not rise"}.get(
            refill.get("hold_reason"), "the level reading looked wrong")
        out.append(Alert(
            "refill.hold", "warning", "Water",
            "Refill waiting for your OK",
            f"The tank needs water, but {why}, so the refill stopped. Nutrient and pH keep "
            "running meanwhile.",
            "Check the level stick is seated and the jerry can has water, then press "
            "Proceed with refill.", fix_in="Settings"))
    last = refill.get("last_done_min_ago")
    if last is not None and last <= REFILL_DONE_NOTICE_MIN:
        out.append(Alert(
            "refill.done", "info", "Water", "Refill done",
            f"{refill.get('last_delivered_ml', 0):.0f} mL topped up in "
            f"{refill.get('last_minutes', 0)} min; the level is back at its target."))
    paused = state.get("loops_paused") if isinstance(state.get("loops_paused"), dict) else {}
    for loop, label in (("level", "Water level"), ("nutrient", "Nutrient"), ("ph", "pH")):
        if paused.get(loop):
            out.append(Alert(
                f"loop_paused.{loop}", "info", "Control", f"{label} loop paused",
                "The other loops keep running in order.",
                "Resume it under Settings when ready.", fix_in="Settings"))
    return out


REFILL_DONE_NOTICE_MIN = 30


# Firmware 1.9.20+: why the nutrient solution is due for a change (shared/solution_monitor.h).
SOLUTION_REASONS = {
    "age": ("Old solution", "it is {age} days old; ion balance drifts within 3–4 weeks"),
    "turnover": ("Much top-up water", "{turnover:.1f} tank volumes of top-up water have gone in, "
                 "so ions the plants leave behind have concentrated"),
    "reagent": ("Much pH reagent", "{reagent:.2f} mL/L of pH reagent has gone in, adding salt and "
                "buffer the plants do not take up"),
    "buffer": ("Buffered", "pH doses now reach only {buffer:.0%} of the change fresh solution gives"),
    "ph_band": ("pH will not hold", "pH was outside its limits for {ph_out} of the last 24 h"),
    "tds_high": ("TDS too high", "TDS has been above its limit for {tds_high:.0f} h, and the "
                 "controller never dilutes"),
}


def solution_reasons(solution, key):
    """[(title, sentence)] for the comma-separated reasons in solution[key]."""
    values = {"age": round(_num(solution.get("age_d")) or 0),
              "turnover": _num(solution.get("turnover")) or 0.0,
              "reagent": _num(solution.get("reagent_ml_per_l")) or 0.0,
              "buffer": _num(solution.get("buffer_ratio")) or 0.0,
              "ph_out": int(_num(solution.get("ph_out_h")) or 0),
              "tds_high": _num(solution.get("tds_high_h")) or 0.0}
    out = []
    for name in filter(None, str(solution.get(key) or "").split(",")):
        if name in SOLUTION_REASONS:
            title, text = SOLUTION_REASONS[name]
            out.append((title, text.format(**values)))
    return out


def _solution_alerts(state):
    solution = state.get("solution") or {}
    status = solution.get("status")
    if status not in ("due", "watch"):
        return []
    reasons = solution_reasons(solution, "due" if status == "due" else "watch")
    detail = "; ".join(text for _, text in reasons) or "Several indicators have drifted."
    if status == "due":
        return [Alert("water_change_due", "warning", "Water", "Time to change the nutrient solution",
                      detail[0].upper() + detail[1:] + ".",
                      "Drain and refill with fresh solution, then press Water changed in Settings "
                      "(or type WATER CHANGED on the serial console).",
                      notify_after_s=HOUR, fix_in="Settings")]
    return [Alert("water_change_soon", "info", "Water", "Nutrient solution change coming up",
                  detail[0].upper() + detail[1:] + ".",
                  "Plan a water change in the next few days.", fix_in="Settings")]


SESSION_LOOPS = (("ph", "pH", "ph_session_ml", "ph_session_cap_ml"),
                 ("ec", "Nutrient", "ec_session_ml", "ec_session_cap_ml"))
NEAR_CAP = 0.8


def session_usage(state):
    """[(loop, label, used_ml, cap_ml)] for loops with a cap; used/cap may be None."""
    out = []
    for loop, label, used_key, cap_key in SESSION_LOOPS:
        cap = _num(state.get(cap_key))
        if cap and cap > 0:
            out.append((loop, label, _num(state.get(used_key)), cap))
    return out


def _session_alerts(state):
    """A warning when a loop nears its per-session dosing limit, and when it reaches it.

    The limit stops a wrong reading or a dosing line that is not delivering from
    pouring reagent into the tank; a person must check before more is allowed.
    """
    out = []
    for loop, label, used, cap in session_usage(state):
        if used is None:
            continue
        if used >= cap:
            out.append(Alert(
                f"session_cap.{loop}", "warning", "Dosing",
                f"{label} dosing stopped: session limit reached ({used:.1f} of {cap:.0f} mL)",
                f"The controller gives at most {cap:.0f} mL of {label.lower()} reagent per session, "
                "so a wrong reading or a dosing line that is not delivering cannot keep "
                "pouring it in.",
                "Check the reading with a test strip and that the pump line delivers. An admin "
                "can then allow more dosing in Settings.",
                notify_after_s=0, fix_in="Settings"))
        elif used >= NEAR_CAP * cap:
            out.append(Alert(
                f"session_near_cap.{loop}", "warning", "Dosing",
                f"{label} dosing near its session limit ({used:.1f} of {cap:.0f} mL)",
                "Dosing stops automatically at the limit.",
                "Check the reading with a test strip and that the pump line delivers.",
                notify_after_s=0, fix_in="Settings"))
    return out


def evaluate(state, *, node_live, suggested_stage=None, plant_week=0,
             last_seen_s=None, now=None):
    """All active alerts for this state, most severe first."""
    now = now or datetime.now(timezone.utc)
    alerts = _offline_alerts(node_live, last_seen_s)
    if not state or not state.get("received_at"):
        return alerts
    raw_stage = state.get("grow_stage") or state.get("system_mode")
    actual = normalize_stage_name(raw_stage) if raw_stage else None
    stage = climate_stage(actual, suggested_stage)
    if not node_live:
        # Old readings are not re-alerted; only the plant-age reminder still applies.
        alerts += [a for a in _stage_alerts(actual, suggested_stage, plant_week, plant_week > 0)
                   if a.key == "stage_change_due"]
        return alerts
    alerts += _health_alerts(state)
    alerts += _implausible_alerts(state)
    alerts += _stock_alerts(state)
    alerts += _calibration_alerts(state)
    alerts += _water_alerts(state, actual)
    alerts += _chemistry_alerts(state, actual)
    alerts += _session_alerts(state)
    alerts += _no_response_alerts(state)
    alerts += _refill_alerts(state)
    alerts += _solution_alerts(state)
    alerts += _climate_alerts(state, stage)
    alerts += _light_alerts(state, now)
    alerts += _stage_alerts(actual, suggested_stage, plant_week, plant_week > 0)

    seen, unique = set(), []
    for alert in alerts:
        if alert.key not in seen:
            seen.add(alert.key)
            unique.append(alert)
    return sorted(unique, key=lambda a: (SEVERITY_ORDER[a.severity], a.category, a.title))


def counts(alerts):
    out = {"fault": 0, "warning": 0, "info": 0}
    for alert in alerts:
        out[alert.severity] += 1
    return out
