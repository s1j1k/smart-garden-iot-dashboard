"""Alerts: what needs attention, and where notifications go."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import explain
import garden_settings
from alerts import CATALOGUE, SEVERITY_ICON, counts
from browser_notify import enable_button
from config import DATA_DIR
from core import LIVE_REFRESH_S, live_context, run_live
from notifier import send_ntfy
from admin_auth import is_admin_site

NOTIFIER_STATUS = DATA_DIR / "notifier_status.json"
SEVERITY_LABEL = {"fault": "Fault", "warning": "Warning", "info": "Reminder"}


def _active():
    ctx = live_context()
    active = ctx["alerts"]
    n = counts(active)
    c1, c2, c3 = st.columns(3)
    c1.metric("🔴 Faults", n["fault"])
    c2.metric("🟠 Warnings", n["warning"])
    c3.metric("🔵 Reminders", n["info"])
    if not active:
        st.success("✓ No active alerts.")
        return
    for alert in active:
        with st.container(border=True):
            st.markdown(f"{SEVERITY_ICON[alert.severity]} **{alert.title}**  \n"
                        f"<small>{SEVERITY_LABEL[alert.severity]} · {alert.category}</small>",
                        unsafe_allow_html=True)
            if alert.detail:
                st.write(alert.detail)
            if alert.action:
                st.markdown(f"**What to do:** {alert.action}")


def _notifier_status():
    try:
        status = json.loads(NOTIFIER_STATUS.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(status["updated_at"])
    except (FileNotFoundError, ValueError, KeyError):
        return None, None
    return status, (datetime.now(timezone.utc) - updated).total_seconds()


def _ntfy_setup():
    settings = garden_settings.load()
    status, age = _notifier_status()
    if status is None or age is None or age > 120:
        st.warning("The background notifier is **not running**, so phone and computer "
                   "notifications are not being sent. It starts with the Docker image; "
                   "otherwise run `python scripts/alert_notifier.py` on the dashboard computer.")
    elif not status.get("ntfy_configured"):
        st.info("The background notifier is running. Add a topic below to start pushing.")
    else:
        st.success(f"✓ Background notifier running (checked {age:.0f} s ago) and pushing to "
                   f"**{settings['ntfy_topic']}**.")

    st.markdown(
        "1. Install **ntfy** — [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) · "
        "[iPhone](https://apps.apple.com/app/ntfy/id1625396347) · on a computer open "
        "[ntfy.sh/app](https://ntfy.sh/app) and allow notifications.\n"
        "2. In the app, **subscribe** to the topic below (same server).\n"
        "3. Press **Send test notification**.")
    st.caption("Anyone who knows the topic name can read it on the public server, so keep the "
               "random one, or use your own ntfy server with a token.")
    with st.form("ntfy_form"):
        suggested = st.session_state.setdefault("ntfy_suggested_topic",
                                                f"smart-garden-{secrets.token_hex(4)}")
        topic = st.text_input("Topic", value=settings["ntfy_topic"] or suggested)
        server = st.text_input("Server", value=settings["ntfy_url"])
        min_sev = st.selectbox(
            "Send", options=["fault", "warning", "info"],
            index=["fault", "warning", "info"].index(settings["notify_min_severity"]),
            format_func={"fault": "Faults only", "warning": "Faults and warnings",
                         "info": "Everything, including reminders"}.get)
        stage = st.toggle("Remind me when it is time to change the grow stage",
                          value=bool(settings["notify_stage_changes"]))
        with st.expander("More options"):
            token = st.text_input("Access token (protected topics)", value=settings["ntfy_token"],
                                  type="password")
            url = st.text_input("Dashboard address to open when a notification is tapped",
                                value=settings["dashboard_url"],
                                placeholder="https://garden.example.com")
        saved = st.form_submit_button("Save", type="primary")
    if saved:
        garden_settings.save({"ntfy_topic": topic.strip(), "ntfy_url": server.strip(),
                              "notify_min_severity": min_sev, "notify_stage_changes": stage,
                              "ntfy_token": token.strip(), "dashboard_url": url.strip()})
        st.success("Saved. The notifier picks this up within 30 s.")
    if st.button("Send test notification", disabled=not garden_settings.load()["ntfy_topic"]):
        ok, err = send_ntfy(garden_settings.load(), "Smart garden test",
                            "Garden alerts will arrive on this device.", "default", "seedling")
        (st.success if ok else st.error)("Sent — check your phone / computer." if ok else
                                         f"Could not send: {err}")


def _reference():
    rows = [{"Alert": e.key, "Type": SEVERITY_LABEL[e.severity], "Area": e.category,
             "When": e.trigger, "What to do": e.action, "Push after": e.notify_after}
            for e in CATALOGUE]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def page():
    explain.heading("🔔 Alerts", "alerts", "title")
    run_live(_active, LIVE_REFRESH_S)
    st.divider()
    explain.heading("Notifications", "notifications", "subheader")
    # The ntfy set-up holds the topic and access token: admin-only.
    admin = is_admin_site()
    tabs = st.tabs(["This device"] + (["Phone & computer (always on)"] if admin else []))
    with tabs[0]:
        st.caption("While the dashboard is open — in a browser tab or installed as an app — "
                   "this device shows a system notification for each new alert.")
        enable_button()
    if admin:
        with tabs[1]:
            _ntfy_setup()
    with st.expander("📖 Every alert, when it fires and what to do"):
        st.caption("Faults: something is broken or a reading cannot be trusted. Warnings: the "
                   "garden needs attention. Reminders: a nudge, such as changing the grow stage. "
                   "“Push after” is how long it must last before a notification is sent.")
        _reference()
