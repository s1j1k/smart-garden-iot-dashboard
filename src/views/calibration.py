"""Calibration wizards: SEN0169 pH probe (two buffers) and the peristaltic pumps."""

import streamlit as st

from core import fmt_number, metric_value, publish_cal_cmd


def render_ph_calibration_tab(state, has_live_data):
    st.subheader("🧪 SEN0169 pH probe calibration")

    st.markdown(
        """
Two-point **software** calibration (datasheet Method 1) using your buffers:

1. Rinse the probe in distilled water  
2. Place in **pH 4.00** → wait until board voltage is stable → **Capture acid**  
3. Rinse again → place in **pH 9.18** → **Capture alkali**  
4. **Save** — ESP32 fits `pH = slope × V_board + offset` and stores it on LittleFS  

Auto pH dosing is paused while calibration is active.
"""
    )

    msg = str(metric_value(state, "ph_cal_message", default="") or "")
    cal_state = str(metric_value(state, "ph_cal_state", default="") or "")
    acid_v = metric_value(state, "ph_cal_acid_v")
    alkali_v = metric_value(state, "ph_cal_alkali_v")

    if not has_live_data:
        st.warning("Waiting for ESP32 telemetry before capturing points.")
    elif metric_value(state, "ph_voltage") is None and metric_value(state, "ph_cal_state") is None:
        st.error(
            "This ESP32 firmware is not publishing calibration fields "
            "(`ph_voltage`, `ph_cal_*`). Reflash the current "
            "`embedded-smart-garden` sketch, or calibrate over USB Serial: "
            "`ENTERPH` → `CALPH ACID` → `CALPH ALKALI` → `EXITPH`."
        )

    if msg == "acid_captured" or acid_v is not None:
        st.success(
            f"Acid point captured"
            + (f": **{fmt_number(acid_v, 4)} V** → pH 4.00" if acid_v is not None else ".")
            + " Next: rinse, then capture alkali 9.18."
        )
    if msg == "alkali_captured" or alkali_v is not None:
        st.success(
            f"Alkali point captured"
            + (f": **{fmt_number(alkali_v, 4)} V** → pH 9.18" if alkali_v is not None else ".")
            + " Next: **Save & exit**."
        )
    if msg == "saved":
        st.success("Calibration saved on the ESP32 (LittleFS).")
    if msg in ("not_active", "need_both_points", "fit_failed", "bad_voltage"):
        st.error(f"Calibration issue from node: `{msg}`")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Board voltage (V)", fmt_number(metric_value(state, "ph_voltage"), 4))
    c2.metric("pH raw", fmt_number(metric_value(state, "ph_raw"), 3))
    c3.metric("Cal state", cal_state if cal_state else "—")
    c4.metric("Last message", msg if msg else "—")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Pending acid V", fmt_number(acid_v, 4))
    c6.metric("Pending alkali V", fmt_number(alkali_v, 4))
    c7.metric("Slope", fmt_number(metric_value(state, "ph_slope"), 4))
    c8.metric(
        "Probe calibrated",
        "yes" if metric_value(state, "ph_probe_calibrated") else "no / defaults",
    )

    st.markdown("### Wizard (one step at a time)")
    if st.button("1 · Enter calibration", key="ph_cal_enter", width="stretch"):
        publish_cal_cmd({"cmd": "ph_cal", "action": "enter"})
    if st.button("2 · Capture acid (4.00)", key="ph_cal_acid", width="stretch"):
        publish_cal_cmd(
            {"cmd": "ph_cal", "action": "capture", "point": "acid", "nominal": 4.00}
        )
    if st.button("3 · Capture alkali (9.18)", key="ph_cal_alkali", width="stretch"):
        publish_cal_cmd(
            {
                "cmd": "ph_cal",
                "action": "capture",
                "point": "alkali",
                "nominal": 9.18,
            }
        )
    if st.button("4 · Save & exit", key="ph_cal_save", width="stretch", type="primary"):
        publish_cal_cmd({"cmd": "ph_cal", "action": "save"})
    if st.button("Abort calibration", key="ph_cal_abort", width="stretch"):
        publish_cal_cmd({"cmd": "ph_cal", "action": "abort"})

    d1, d2 = st.columns(2)
    with d1:
        if st.button("Clear saved calibration", key="ph_cal_clear", width="stretch"):
            publish_cal_cmd({"cmd": "ph_cal", "action": "clear"})
    with d2:
        if st.button("Refresh status from node", key="ph_cal_status", width="stretch"):
            publish_cal_cmd({"cmd": "ph_cal", "action": "status"})

    if st.session_state.last_cmd_status:
        st.caption(st.session_state.last_cmd_status)

    st.info(
        "USB Serial alternative: `ENTERPH` → `CALPH ACID` → `CALPH ALKALI` → `EXITPH`. "
        "Do not turn the SEN0169 gain pot after a software calibration."
    )


def render_pump_calibration_tab(state, has_live_data):
    st.subheader("💧 Peristaltic pump calibration")

    st.markdown(
        """
Cup wizard (same flow as Serial `CAL`):

1. Put the correct liquid on the **inlet** and a waste / **10 mL measuring cup** on the **outlet**  
2. **Enter** the pump session (garden auto-dosing pauses)  
3. Optional **Prime** until liquid (not air) reaches the outlet → **Stop**  
4. Empty the 10 mL cup → **Run** (water = continuous; chem = 500 ms bursts) → **Stop** when readable  
5. Enter the cup volume → repeat for **4 runs** total (1 discovery + 3 confirms) → rate saved to LittleFS  

**Safety:** pumps only start when you click **Run**. Keep a cup on the outlet before every run.
"""
    )

    msg = str(metric_value(state, "pump_cal_message", default="") or "")
    cal_state = str(metric_value(state, "pump_cal_state", default="") or "")
    cal_pump = str(metric_value(state, "pump_cal_pump", default="") or "")
    active = bool(metric_value(state, "pump_cal_active"))
    mode = str(metric_value(state, "pump_cal_mode", default="") or "")
    unit = str(metric_value(state, "pump_cal_unit", default="") or "mL/burst")

    if not has_live_data:
        st.warning("Waiting for ESP32 telemetry before starting the wizard.")
    elif metric_value(state, "pump_cal_state") is None and metric_value(state, "water_calibrated") is None:
        st.error(
            "This ESP32 firmware is not publishing `pump_cal_*` fields. "
            "Reflash the current `embedded-smart-garden` sketch, or use Serial: "
            "`CAL WATER` / `CAL PH_UP` / … then `YES` / `STOP` / volume."
        )

    if msg == "saved":
        st.success("Pump rate saved on the ESP32 (LittleFS).")
    elif msg == "finished":
        st.success("Calibration session finished — garden control resumed.")
    elif msg == "aborted":
        st.info("Calibration aborted — previous saved rates unchanged.")
    elif msg in (
        "unknown_pump",
        "not_active",
        "not_ready",
        "still_priming",
        "bad_volume",
        "no_runtime",
        "no_burst",
        "not_wait_volume",
        "unknown_action",
    ):
        st.error(f"Calibration issue from node: `{msg}`")
    elif msg == "wait_volume":
        st.info("Run complete — read the cup and submit the volume below.")
    elif msg == "need_confirm_run":
        st.info("Empty the cup, then **Run** again for the next confirm pass.")
    elif msg == "priming":
        st.warning("Priming — click **Stop** when liquid (not air) reaches the outlet.")
    elif msg == "running" or cal_state == "running":
        st.warning("Pump running — click **Stop** when the cup volume is readable.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cal state", cal_state if cal_state else "—")
    c2.metric("Pump", cal_pump if cal_pump else "—")
    c3.metric("Mode", mode if mode else "—")
    c4.metric("Last message", msg if msg else "—")

    run_n = metric_value(state, "pump_cal_run")
    runs_needed = metric_value(state, "pump_cal_runs_needed", default=4)
    c5, c6, c7, c8 = st.columns(4)
    c5.metric(
        "Run progress",
        f"{fmt_number(run_n, 0)} / {fmt_number(runs_needed, 0)}"
        if run_n is not None
        else "—",
    )
    c6.metric("Bursts this run", fmt_number(metric_value(state, "pump_cal_bursts"), 0))
    c7.metric("Pulse (ms)", fmt_number(metric_value(state, "pump_cal_pulse_ms"), 0))
    c8.metric(
        "Last sample",
        f"{fmt_number(metric_value(state, 'pump_cal_last_sample'), 3)} {unit}"
        if metric_value(state, "pump_cal_last_sample") is not None
        else "—",
    )

    st.markdown("### Stored rates")
    r1, r2, r3, r4 = st.columns(4)
    r1.metric(
        "Water",
        f"{fmt_number(metric_value(state, 'water_ml_per_s'), 3)} mL/s"
        if metric_value(state, "water_calibrated")
        else "not calibrated",
    )
    r2.metric(
        "Nutrient",
        f"{fmt_number(metric_value(state, 'nutrient_ml_per_burst'), 3)} mL/burst"
        if metric_value(state, "nutrient_calibrated")
        else "not calibrated",
    )
    r3.metric(
        "pH up",
        f"{fmt_number(metric_value(state, 'ph_up_ml_per_burst'), 3)} mL/burst"
        if metric_value(state, "ph_up_calibrated")
        else "not calibrated",
    )
    r4.metric(
        "pH down",
        f"{fmt_number(metric_value(state, 'ph_down_ml_per_burst'), 3)} mL/burst"
        if metric_value(state, "ph_down_calibrated")
        else "not calibrated",
    )

    st.markdown("### Wizard")
    pump = st.selectbox(
        "Pump",
        options=["water", "nutrient", "ph_up", "ph_down"],
        format_func=lambda p: {
            "water": "Water jug (continuous → mL/s)",
            "nutrient": "Nutrient (500 ms bursts → mL/burst)",
            "ph_up": "pH up (500 ms bursts → mL/burst)",
            "ph_down": "pH down (500 ms bursts → mL/burst)",
        }[p],
        key="pump_cal_select",
    )

    cup_ready = st.checkbox(
        "Cup is on the outlet (waste for prime, empty 10 mL cup for measure runs)",
        key="pump_cal_cup_ready",
    )

    if st.button(
        "1 · Enter calibration",
        key="pump_cal_enter",
        width="stretch",
        disabled=active,
    ):
        publish_cal_cmd({"cmd": "pump_cal", "action": "enter", "pump": pump})

    p1, p2 = st.columns(2)
    with p1:
        if st.button("2 · Prime", key="pump_cal_prime", width="stretch"):
            if not cup_ready:
                st.session_state.last_cmd_status = "Tick the cup checkbox before priming."
                st.rerun()
            else:
                publish_cal_cmd({"cmd": "pump_cal", "action": "prime"}, wait_seconds=0.5)
    with p2:
        if st.button(
            "Stop (prime / discovery run)",
            key="pump_cal_stop",
            width="stretch",
            type="primary",
        ):
            publish_cal_cmd({"cmd": "pump_cal", "action": "stop"}, wait_seconds=0.8)

    if st.button(
        "3 · Run cup measure (YES)",
        key="pump_cal_run",
        width="stretch",
        type="primary",
    ):
        if not cup_ready:
            st.session_state.last_cmd_status = "Tick the cup checkbox before running the pump."
            st.rerun()
        else:
            publish_cal_cmd({"cmd": "pump_cal", "action": "run"}, wait_seconds=0.5)

    vol = st.number_input(
        "4 · Cup volume (mL)",
        min_value=0.1,
        max_value=10.0,
        value=2.0,
        step=0.1,
        key="pump_cal_volume_ml",
    )
    if st.button(
        "Submit volume",
        key="pump_cal_volume",
        width="stretch",
        type="primary",
    ):
        publish_cal_cmd({"cmd": "pump_cal", "action": "volume", "ml": float(vol)})

    a1, a2, a3 = st.columns(3)
    with a1:
        if st.button("Retry run", key="pump_cal_retry", width="stretch"):
            if not cup_ready:
                st.session_state.last_cmd_status = "Tick the cup checkbox before retry."
                st.rerun()
            else:
                publish_cal_cmd({"cmd": "pump_cal", "action": "retry"}, wait_seconds=0.5)
    with a2:
        if st.button("Skip pump", key="pump_cal_skip", width="stretch"):
            publish_cal_cmd({"cmd": "pump_cal", "action": "skip"})
    with a3:
        if st.button("Abort session", key="pump_cal_abort", width="stretch"):
            publish_cal_cmd({"cmd": "pump_cal", "action": "abort"})

    b1, b2 = st.columns(2)
    with b1:
        if st.button("Refresh status", key="pump_cal_status", width="stretch"):
            publish_cal_cmd({"cmd": "pump_cal", "action": "status"})
    with b2:
        if st.button("Clear all saved rates", key="pump_cal_clear", width="stretch"):
            publish_cal_cmd({"cmd": "pump_cal", "action": "clear"})

    if st.session_state.last_cmd_status:
        st.caption(st.session_state.last_cmd_status)

    st.info(
        "USB Serial alternative: `CAL WATER` / `CAL NUTRIENT` / `CAL PH_UP` / `CAL PH_DOWN` → "
        "`PRIME` → `STOP` → `YES` → type volume → repeat. Rates store in `/pump_cal.txt`."
    )


def render_calibration_tabs(state, has_live_data):
    ph_tab, pump_tab = st.tabs(["🧪 pH probe", "💧 Peristaltic pumps"])
    with ph_tab:
        render_ph_calibration_tab(state, has_live_data)
    with pump_tab:
        render_pump_calibration_tab(state, has_live_data)
