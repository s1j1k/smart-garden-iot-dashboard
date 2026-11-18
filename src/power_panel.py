"""Garden power card (admin): the front-panel ON/OFF switch, over MQTT.

The ESP32 (firmware 1.9.26+) takes ``{"cmd": "power", "action": "on" | "off" |
"status"}`` on its cmd topic and answers on the ack topic with
``{"cmd": "power", "ok": …, "state": "on" | "standby", "message": …}``. In
standby it sends no telemetry, so the ack is the only way to tell a garden that
was switched off from one that is unreachable.
"""

from __future__ import annotations

import time

from status_panel import fmt_duration

# How long to wait for the ack before saying the controller did not answer.
ACK_WAIT_S = 30


def power_state(node_live, telemetry_age_s, ack):
    """(kind, title, detail). kind: on / standby / fault / unknown.

    ``ack`` is the newest power ack with its ``age_s``. It counts when it is newer
    than the newest telemetry, so OFF shows at once although the last reading is
    still under the 90 s live window.
    """
    ack_newer = ack is not None and (telemetry_age_s is None or ack["age_s"] < telemetry_age_s)
    if ack_newer and not ack.get("ok") and ack.get("state") == "standby":
        return "fault", "Standby — ON refused", (
            f"Critical fault: {ack.get('message') or 'not given'}. Fix it at the garden.")
    if ack_newer and ack.get("state") == "standby":
        return "standby", "Standby", (
            "Pumps stopped, grow light off, no sensor readings. "
            f"Last answer {fmt_duration(ack['age_s'])} ago.")
    if node_live or (ack_newer and ack.get("state") == "on"):
        return "on", "On", "The garden is running."
    since = f" for {fmt_duration(telemetry_age_s)}" if telemetry_age_s is not None else ""
    return "unknown", "No telemetry" + since, (
        "The garden is in standby or unreachable. Check asks the controller which.")


def waiting_line(sent, ack, now=None):
    """Line for a command still waiting for its ack, or None once answered.

    ``sent`` is (action, time.monotonic() when sent) from ``core.set_power``.
    """
    if not sent:
        return None
    action, sent_at = sent
    now = time.monotonic() if now is None else now
    waited = now - sent_at
    if ack is not None and ack["age_s"] < waited:
        return None
    if waited < ACK_WAIT_S:
        return f"Sent {action}, waiting for the controller…"
    return ("No answer from the controller. It may be unpowered, off WiFi, or on "
            "firmware older than 1.9.26 (no MQTT power switch).")


def render_power_panel(status, on_send, send_error=None, sent=None):
    """``status`` is the MQTT client status; ``on_send(action)`` publishes the command."""
    import streamlit as st

    status = status or {}
    ack = status.get("power_ack")
    kind, title, detail = power_state(bool(status.get("node_live")),
                                      status.get("last_message_age_s"), ack)
    icon = {"on": "🟢", "standby": "⚪", "fault": "🔴", "unknown": "❔"}[kind]
    st.markdown(f"**⏻ Garden power** · {icon} **{title}**")
    st.caption(detail)
    if send_error:
        st.error(send_error)
    waiting = waiting_line(sent, ack)
    if waiting:
        st.caption(waiting)

    col_main, col_check = st.columns([2, 1])
    with col_main:
        if kind == "on":
            with st.popover("Switch off", width="stretch"):
                st.caption("Stops every pump (including a dose or refill under way), turns "
                           "the grow light off and stops sensor readings. Switch on again "
                           "from here or the green button.")
                st.button("Switch the garden off", key="power_off", type="primary",
                          on_click=on_send, args=("off",),
                          help='Sends {"cmd": "power", "action": "off"}, the same as the '
                          "red OFF button.")
        else:
            st.button("Switch on", key="power_on", type="primary", width="stretch",
                      on_click=on_send, args=("on",),
                      help='Sends {"cmd": "power", "action": "on"}, the same as the green '
                      "ON button. Refused while there is a critical fault.")
    with col_check:
        st.button("Check", key="power_status", width="stretch",
                  on_click=on_send, args=("status",),
                  help='Sends {"cmd": "power", "action": "status"}: the controller says '
                  "whether it is on or in standby.")
