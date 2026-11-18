"""Sensor / actuator health lights and the Control section (one row per control loop).

Reads the ``health``, ``health_msg`` and ``sched`` blocks the ESP32 publishes
with every telemetry message (firmware 1.6+):

    health      {"ph": "fault", "tds": "ok", "water_level": "init", ...}
    health_msg  {"ph": "no valid pH (raw -9.83 @ 0.012 V) ...", ...}
    sched       {"read_s": 10, "read_in_s": 4, "activity": "idle", "wait_s": 0,
                 "level": {"on", "every_s", "in_s", "topup_at_pct", "refill_to_pct", "hold"},
                 "ec":    {"on", "every_s", "in_s", "dose_below", "min", "max", "high_above", "hold"},
                 "ph":    {"on", "every_s", "in_s", "up_below", "down_above", "min", "max", "hold"},
                 "cycle_requested": false, "ph_after_ec": false}    # firmware 1.6.1+

``in_s`` / ``read_in_s`` are relative to when the message was received.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from fmt_range import fmt_range

# Light colours. Blue = OK, red = fault, amber = not ready yet (initialising,
# warming up, or held), grey = no comms / disabled / not reported.
COLORS = {
    "ok": "#1f6feb",
    "fault": "#d1242f",
    "init": "#d4a72c",
    "warming": "#d4a72c",
    "hold": "#d4a72c",
    "off": "#8c959f",
    "nocomms": "#8c959f",
    "unknown": "#8c959f",
}

LABELS = {
    "ok": "OK",
    "fault": "FAULT",
    "init": "Init required",
    "warming": "Warming up",
    "hold": "Held",
    "off": "Disabled",
    "nocomms": "No comms",
    "unknown": "Not reported",
}

SENSORS = [
    ("ph", "🧪 pH probe"),
    ("tds", "🌿 TDS probe"),
    ("water_level", "💧 Water level (Grove)"),
    ("water_temp", "🌡️ Water temp (DS18B20)"),
    ("ambient", "🌤️ Air temp / humidity (DHT11)"),
    ("co2", "💨 CO2 (PAS CO2)"),
    ("light", "☀️ Light (LDR)"),
]

# (health key, label, running-state key in telemetry)
ACTUATORS = [
    ("water_pump", "🚰 Water top-up pump", "water_pump_on"),
    ("nutrient_pump", "🧴 Nutrient pump", "nutrient_pump_on"),
    ("ph_up_pump", "⬆️ pH-up pump", "ph_up_pump_on"),
    ("ph_down_pump", "⬇️ pH-down pump", "ph_down_pump_on"),
    ("grow_light", "💡 Grow light", "led_on"),
    ("auto_control", "🤖 Auto control", None),
    ("influx", "☁️ InfluxDB upload", None),   # firmware 1.6.2+
]


def _parse_time(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fmt_duration(seconds):
    if seconds is None:
        return "—"
    seconds = max(0, int(round(float(seconds))))
    if seconds >= 3600:
        hours, rem = divmod(seconds, 3600)
        mins = rem // 60
        return f"{hours} h {mins} min" if mins else f"{hours} h"
    if seconds >= 60:
        mins, secs = divmod(seconds, 60)
        return f"{mins} min {secs} s" if secs and mins < 10 else f"{mins} min"
    return f"{seconds} s"


def _num(value, digits):
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def device_status(state, key, node_live):
    """Return {"state", "color", "label", "message"} for one device light."""
    health = state.get("health") or {}
    messages = state.get("health_msg") or {}
    reported = health.get(key)
    if not node_live:
        last = LABELS.get(reported, reported) if reported else None
        message = "No telemetry from the ESP32."
        if last:
            message += f" Last reported: {last}."
        return {"state": "nocomms", "color": COLORS["nocomms"],
                "label": LABELS["nocomms"], "message": message}
    if not reported:
        return {"state": "unknown", "color": COLORS["unknown"], "label": LABELS["unknown"],
                "message": "Firmware did not report this (needs firmware 1.6+)."}
    return {
        "state": reported,
        "color": COLORS.get(reported, COLORS["unknown"]),
        "label": LABELS.get(reported, reported),
        "message": messages.get(key, ""),
    }


def health_rows(state, node_live):
    rows = []
    for key, label in SENSORS:
        rows.append({"group": "sensor", "key": key, "name": label,
                     **device_status(state, key, node_live)})
    for key, label, running_key in ACTUATORS:
        status = device_status(state, key, node_live)
        running = state.get(running_key) if running_key else None
        if node_live and running is not None:
            status["running"] = "ON" if running else "off"
        rows.append({"group": "actuator", "key": key, "name": label, **status})
    return rows


def _dot(color, title):
    return (f'<span title="{html.escape(title)}" style="display:inline-block;width:14px;'
            f'height:14px;border-radius:50%;background:{color};vertical-align:middle;'
            'margin-right:6px"></span>')


# One line per row whatever the message length: the full message is in the
# hover title. Wrapping text made the panel change height on every update.
_CELL = "padding:4px 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"


def _health_table(rows):
    body = []
    for row in rows:
        running = row.get("running")
        running_html = f" · <b>{running}</b>" if running else ""
        message = row["message"] or ""
        body.append(
            "<tr>"
            f"<td style='{_CELL};width:42%'>{_dot(row['color'], row['label'])}"
            f"{html.escape(row['name'])}</td>"
            f"<td style='{_CELL};width:22%'><b>{html.escape(row['label'])}</b>"
            f"{running_html}</td>"
            f"<td style='{_CELL}' title=\"{html.escape(message)}\">{html.escape(message)}</td>"
            "</tr>"
        )
    return ("<table style='width:100%;border-collapse:collapse;table-layout:fixed;"
            "font-size:0.9rem'>" + "".join(body) + "</table>")


def alert_lines(rows, node_live):
    """Fault / init lines for the fixed-height alert box (markdown)."""
    if not node_live:
        return ["⚪ No live telemetry — lights show the last report as *No comms*."]
    lines = []
    for row in rows:
        if row["state"] == "fault":
            lines.append(f"🔴 **{row['name']}: FAULT** — {row['message']}")
        elif row["state"] == "init":
            lines.append(f"🟠 **{row['name']}: initialisation required** — {row['message']}")
    return lines or ["🔵 No faults. Nothing needs initialising."]


# Fixed heights keep the page from shifting as text changes between updates;
# anything longer scrolls inside its box.
ALERT_BOX_PX = 88


def render_session_limits(state, node_live, on_reset, send_error=None):
    """Admin: per-session dosing limits, with an override once a limit is reached.

    ``on_reset(loop)`` publishes {"cmd": "session_reset", "loop": loop}.
    """
    import streamlit as st

    from alerts import NEAR_CAP, session_usage

    st.markdown("**🧯 Dosing session limits**")
    if send_error:
        st.error(send_error)
    stuck = [name for flag, name in (("ph_down_no_response", "pH-down"), ("ph_up_no_response", "pH-up"))
             if state.get(flag)]
    if stuck:
        st.error(f"{' and '.join(stuck)} dosing stopped: its last few doses did not move the pH. "
                 "Check the pump, its line and the pH probe, then clear the fault.")
        st.button("Clear the pH fault", key="no_response_reset_ph",
                  disabled=not node_live, on_click=on_reset, args=("ph",),
                  help='Sends {"cmd": "session_reset", "loop": "ph"}: clears the fault and '
                       "starts a fresh pH session. Logged as a control event.")
    if state.get("nutrient_no_response"):
        st.error("Nutrient dosing stopped: its last few doses did not raise the TDS. Check the "
                 "pump, its line and the TDS probe, then clear the fault.")
        st.button("Clear the nutrient fault", key="no_response_reset_ec",
                  disabled=not node_live, on_click=on_reset, args=("ec",),
                  help='Sends {"cmd": "session_reset", "loop": "ec"}: clears the fault and '
                       "starts a fresh nutrient session. Logged as a control event.")
    usage = session_usage(state)
    if not usage:
        st.caption("This firmware does not report its dosing session limits.")
        return
    for loop, label, used, cap in usage:
        used = used or 0.0
        frac = max(0.0, min(1.0, used / cap))
        reached = used >= cap
        note = " — limit reached, dosing stopped" if reached else (
            " — near the limit" if used >= NEAR_CAP * cap else "")
        st.progress(frac, text=f"{label}: {used:.1f} of {cap:.0f} mL this session{note}")
        if reached:
            sure = st.checkbox(
                f"I checked the {label.lower()} reading with a strip and the dosing line delivers",
                key=f"session_reset_ok_{loop}")
            st.button(f"Allow more {label.lower()} dosing", key=f"session_reset_{loop}",
                      type="primary", disabled=not (sure and node_live), on_click=on_reset,
                      args=(loop,),
                      help='Sends {"cmd": "session_reset"}: a fresh session, so the limit '
                           "applies again from zero. Logged as a control event.")
    st.caption("A session ends at a grow-stage change, a reboot or a fresh-water refill.")


def render_solution(state, node_live, on_changed, send_error=None):
    """Admin: is the nutrient solution due for a change, why, and a button to record one."""
    import streamlit as st

    from alerts import solution_reasons

    st.markdown("**💧 Nutrient solution**")
    if send_error:
        st.error(send_error)
    solution = state.get("solution")
    if not solution:
        st.caption("This firmware does not report the solution's condition (1.9.20+).")
        return
    status = solution.get("status", "ok")
    reasons = solution_reasons(solution, "due") + solution_reasons(solution, "watch")
    if status == "due":
        st.warning("**Time to change the water.** " + "; ".join(t for _, t in reasons) + ".")
    elif status == "watch":
        st.info("**Change coming up.** " + "; ".join(t for _, t in reasons) + ".")
    else:
        st.success("Solution OK — no indicator is near its limit.")

    def show(value, digits=1, suffix=""):
        return "—" if value is None else f"{value:.{digits}f}{suffix}"

    age = solution.get("age_d")
    ratio = solution.get("buffer_ratio")
    rows = [
        ("Age", show(age, 1, " d"), "watch 21 d, due 28 d"),
        ("Top-up water", show(solution.get("turnover"), 2, " tank vol."), "watch 1, due 2"),
        ("pH reagent added", show(solution.get("reagent_ml_per_l"), 2, " mL/L"), "watch 0.75, due 1.5"),
        ("pH dose response", "—" if ratio is None else
         f"{ratio:.0%} of fresh (n={solution.get('buffer_n', 0)})", "watch ≤60 %, due ≤35 %"),
        ("pH out of limits", f"{solution.get('ph_out_h', 0)} h of 24 h", "watch 6 h, due 12 h"),
        ("TDS above limit", show(solution.get("tds_high_h"), 0, " h"), "watch 12 h, due 24 h"),
    ]
    st.table({"Indicator": [r[0] for r in rows], "Now": [r[1] for r in rows],
              "Limits": [r[2] for r in rows]})
    sure = st.checkbox("I drained and refilled the tank with fresh solution", key="water_changed_ok")
    st.button("Water changed", key="water_changed", disabled=not (sure and node_live),
              on_click=on_changed,
              help='Sends {"cmd": "water_changed"}: the indicators start again from zero. '
                   "A refill confirmed with FILL / CONFIRM, or plain water in the tank, does "
                   "the same by itself.")


REFILL_HOLD_TEXT = {
    "noisy_level": "the level stick's readings are jumping by two or more steps",
    "no_level_rise": "two steps' worth of water went in and the level did not rise "
                     "(empty jerry can, stuck stick or a leak?)",
}


def refill_progress(refill):
    """(fraction 0-1, caption) for a refill in progress, or None."""
    if not isinstance(refill, dict) or not refill.get("active"):
        return None
    start, now, target = refill.get("start_pct"), refill.get("now_pct"), refill.get("target_pct")
    delivered, planned = refill.get("delivered_ml") or 0, refill.get("planned_ml") or 0
    if start is None or now is None or target is None or target <= start:
        fraction = min(delivered / planned, 1.0) if planned else 0.0
    else:
        fraction = min(max((now - start) / (target - start), 0.0), 1.0)
    caption = (f"Level {now} % → target {target} % · {delivered:.0f} of about {planned:.0f} mL · "
               f"{refill.get('minutes', 0)} min")
    return fraction, caption


def render_refill(state, node_live, on_proceed, send_error=None):
    """Admin: refill progress, and the go-ahead for a refill on hold."""
    import streamlit as st

    refill = state.get("refill")
    if not node_live or not isinstance(refill, dict):
        return
    st.markdown("**💧 Water refill**")
    if send_error:
        st.error(send_error)
    if refill.get("hold"):
        why = REFILL_HOLD_TEXT.get(refill.get("hold_reason"), "the level reading looks wrong")
        st.warning(f"Refill on hold: {why}. Check the stick is seated and dry above the water, "
                   "then go ahead. Nutrient and pH keep running meanwhile.")
        st.button("Proceed with refill", key="refill_proceed", type="primary",
                  on_click=on_proceed, help='Sends {"cmd": "refill_proceed"}.')
        return
    progress = refill_progress(refill)
    if progress is not None:
        st.progress(progress[0], text=progress[1])
        return
    last = refill.get("last_done_min_ago")
    if last is not None:
        st.caption(f"Last refill finished {last} min ago: {refill.get('last_delivered_ml', 0):.0f} mL "
                   f"in {refill.get('last_minutes', 0)} min.")
    else:
        st.caption("No refill since the ESP32 started. The level is "
                   f"{refill.get('now_pct', '—')} % (target {refill.get('target_pct', '—')} %).")


def render_status_panel(state, node_live):
    import streamlit as st

    st.subheader("🩺 Sensor & actuator status")
    st.markdown(
        f"{_dot(COLORS['ok'], 'OK')} OK &nbsp; {_dot(COLORS['fault'], 'Fault')} Fault &nbsp; "
        f"{_dot(COLORS['init'], 'Not ready')} Init required / warming up / held &nbsp; "
        f"{_dot(COLORS['nocomms'], 'No comms')} No comms / disabled",
        unsafe_allow_html=True,
    )
    rows = health_rows(state, node_live)
    col_s, col_a = st.columns(2)
    with col_s:
        st.markdown("**Sensors**")
        st.markdown(_health_table([r for r in rows if r["group"] == "sensor"]),
                    unsafe_allow_html=True)
    with col_a:
        st.markdown("**Actuators**")
        st.markdown(_health_table([r for r in rows if r["group"] == "actuator"]),
                    unsafe_allow_html=True)

    with st.container(height=ALERT_BOX_PX, border=True):
        st.markdown("  \n".join(alert_lines(rows, node_live)))


# key, label, "sched" block, pump-on fields. Run order on the ESP32: level, nutrient, pH.
LOOPS = (
    ("level", "💧 Water level", "level", ("water_pump_on",)),
    ("nutrient", "🧪 Nutrient", "ec", ("nutrient_pump_on",)),
    ("ph", "⚗️ pH", "ph", ("ph_up_pump_on", "ph_down_pump_on")),
)

# Settle wait -> the loop whose dose is mixing. Every loop waits for it.
SETTLE_OWNER = {"wait_water_settle": "level", "wait_ec_settle": "nutrient",
                "wait_ph_settle": "ph"}

LOOP_ICON = {"idle": "🟢", "due": "🟠", "run": "🔵", "wait": "🔵", "paused": "⏸️",
             "held": "🟡", "off": "⚪"}

# A run request shows as pending this long, until telemetry reflects it.
RUN_REQUEST_S = 30


def _trigger(key, state, block):
    """(due, rule, now) for the reading that starts a loop."""
    if key == "ph":
        value, low, high = state.get("ph_ema"), block.get("up_below"), block.get("down_above")
        if value is None or low is None or high is None:
            return False, "When pH leaves its limits", ""
        return (value < low or value > high,
                f"When pH leaves {_num(low, 2)}–{_num(high, 2)}", f"now {_num(value, 2)}")
    if key == "nutrient":
        value, low = state.get("tds_ppm_ema"), block.get("dose_below")
        if value is None or low is None:
            return False, "When TDS drops below its limit", ""
        return value < low, f"When TDS < {_num(low, 0)} ppm", f"now {_num(value, 0)}"
    value, at = state.get("water_level_pct"), block.get("topup_at_pct")
    if value is None or value < 0 or at is None:
        return False, "When the level falls below its limit", ""
    return value <= at, f"When level ≤ {_num(at, 0)} %", f"now {_num(value, 0)} %"


def loop_status(key, state, node_live, requested_at=None, now=None):
    """(kind, text): what one loop is doing and when it runs next.

    kind is idle / due / run / wait / paused / held / off.
    """
    now = now or datetime.now(timezone.utc)
    sched = state.get("sched")
    if not node_live or not isinstance(sched, dict):
        return "off", "No live telemetry"
    _, _, block_key, pumps = next(loop for loop in LOOPS if loop[0] == key)
    block = sched.get(block_key) or {}
    if (state.get("loops_paused") or {}).get(key):
        return "paused", "Paused"
    if state.get("auto_control_paused"):
        return "paused", "Paused with all loops"
    if not block.get("on", True):
        return "off", "Off in this stage"
    if any(state.get(pump) for pump in pumps):
        return "run", "Running now"
    if block.get("hold"):
        return "held", f"Held: {block['hold']}"
    wait_s = sched.get("wait_s") or 0
    owner = SETTLE_OWNER.get(sched.get("activity"))
    if owner and wait_s > 0:
        if owner == key:
            return "wait", f"Mixing · checks again in {fmt_duration(wait_s)}"
        return "wait", f"Waits {fmt_duration(wait_s)} for a dose to mix"
    if requested_at is not None and (now - requested_at).total_seconds() < RUN_REQUEST_S:
        return "due", "Requested · runs on the next pass"
    if sched.get("cycle_requested") or (key == "ph" and sched.get("ph_after_ec")):
        return "due", "Runs on the next pass"
    due, rule, value = _trigger(key, state, block)
    suffix = f" · {value}" if value else ""
    if due:
        retry_s = sched.get("retry_in_s") or 0
        if retry_s > 0:
            return "held", f"Out of limits · retries in {fmt_duration(retry_s)}{suffix}"
        return "due", f"Runs on the next pass{suffix}"
    return "idle", f"{rule}{suffix}"


def render_control(state, node_live, on_pause_all, on_pause_loop, on_run,
                   requested=None, send_error=None):
    """Admin: pause or run each control loop. Otherwise they run on their own.

    ``on_pause_all(paused)``, ``on_pause_loop(loop, paused)`` and ``on_run(loop)``
    publish the commands; ``on_run("all")`` runs every loop. ``requested`` maps a
    loop (or "all") to when a run was last requested from this page.
    """
    import streamlit as st

    requested = requested or {}
    st.subheader("🎛️ Control")
    if send_error:
        st.error(send_error)
    live = node_live and isinstance(state.get("sched"), dict)
    all_paused = state.get("auto_control_paused")
    loops_paused = state.get("loops_paused")

    col_text, col_pause, col_run = st.columns([2, 1, 1], vertical_alignment="center")
    if not live:
        col_text.markdown("⚪ No live telemetry")
    elif all_paused:
        col_text.markdown("⏸️ All loops paused")
    else:
        col_text.markdown("🟢 Automatic")
    col_pause.button("Resume all" if all_paused else "Pause all", key="control_pause_all",
                     width="stretch", disabled=not live or all_paused is None,
                     on_click=on_pause_all, args=(not all_paused,))
    col_run.button("Run all now", key="control_run_all", width="stretch",
                   disabled=not live or bool(all_paused), on_click=on_run, args=("all",))

    for col, (key, label, _, _) in zip(st.columns(len(LOOPS)), LOOPS):
        times = [t for t in (requested.get(key), requested.get("all")) if t is not None]
        kind, text = loop_status(key, state, node_live, max(times) if times else None)
        paused = bool((loops_paused or {}).get(key))
        with col, st.container(border=True):
            st.markdown(f"**{label}**  \n{LOOP_ICON[kind]} {text}")
            col_a, col_b = st.columns(2)
            col_a.button("Resume" if paused else "Pause", key=f"loop_pause_{key}",
                         width="stretch", disabled=not live or not isinstance(loops_paused, dict),
                         on_click=on_pause_loop, args=(key, not paused))
            col_b.button("Run now", key=f"loop_run_{key}", width="stretch",
                         disabled=not live or kind in ("paused", "off", "run"),
                         on_click=on_run, args=(key,))
