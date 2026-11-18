"""Home: is the garden OK right now, and what needs doing."""

from __future__ import annotations

import streamlit as st

import explain
import plant_health
from admin_auth import is_admin_site
from alerts import SEVERITY_ICON, counts
from camera_paths import LATEST_IMAGE
from camera_view import load_growth_frame
from core import LIVE_REFRESH_S, fmt_number, live_context, metric_value, run_live, \
    set_light_makeup, set_power
from grow_light_panel import render_grow_light_panel
from power_panel import render_power_panel
from readings import reading_tiles, tiles_html
from views.nav import link
from views.stage import stage_name

BOTTLES = [
    ("water", "💧 Water jug"),
    ("nutrient", "🧴 Nutrient"),
    ("ph_up", "⬆️ pH-up"),
    ("ph_down", "⬇️ pH-down"),
]


def _status_line(ctx):
    status = ctx["mqtt_status"] or {}
    if ctx["node_live"]:
        age = status.get("last_message_age_s")
        live = f"🟢 Live · updated {age:.0f} s ago" if isinstance(age, (int, float)) else "🟢 Live"
    elif ctx["show_stored"]:
        live = f"⚪ Offline · showing the last reading ({st.session_state.get('history_latest_at')})"
    else:
        live = "⚪ Offline · no readings yet"
    plants = " & ".join(p.replace("_", " ").title() for p in ctx["settings"].get("plants") or [])
    st.caption(f"{live} · {plants or 'Plants'} · {ctx['age_label']} · "
               f"Stage: **{stage_name(ctx['stage'])}**")


def _alert_strip(ctx):
    active = ctx["alerts"]
    if not active:
        st.success("✓ All good — nothing needs your attention.")
        return
    n = counts(active)
    parts = [f"{n['fault']} fault{'s' * (n['fault'] != 1)}" if n["fault"] else "",
             f"{n['warning']} warning{'s' * (n['warning'] != 1)}" if n["warning"] else "",
             f"{n['info']} reminder{'s' * (n['info'] != 1)}" if n["info"] else ""]
    summary = " · ".join(p for p in parts if p)
    box = st.error if n["fault"] else st.warning if n["warning"] else st.info
    lines = [f"{SEVERITY_ICON[a.severity]} **{a.title}** — {a.action or a.detail}"
             for a in active[:3]]
    more = f"  \n…and {len(active) - 3} more." if len(active) > 3 else ""
    box(f"**{summary}**  \n" + "  \n".join(lines) + more)
    link("alerts", "See all alerts", ":material/notifications:")


def _readings(ctx):
    if explain.enabled():
        explain.heading("**📊 Live readings**", "readings")
    st.markdown(tiles_html(reading_tiles(ctx["state"], ctx["stage"], ctx["suggested_stage"])),
                unsafe_allow_html=True)


def _bottles(ctx):
    state = ctx["state"]
    explain.heading("**🧴 Dosing bottles**", "bottles")
    for key, label in BOTTLES:
        rem, cap = metric_value(state, f"{key}_rem_ml"), metric_value(state, f"{key}_cap_ml")
        if rem is None or not cap:
            st.caption(f"{label}: —")
            continue
        frac = max(0.0, min(1.0, float(rem) / float(cap)))
        empty = state.get(f"{key}_empty")
        text = f"{label}: {fmt_number(rem, 0)} of {fmt_number(cap, 0)} mL"
        st.progress(frac, text=f"{text} — EMPTY, refill" if empty else text)


def _photo():
    explain.heading("**📷 Latest photo**", "photo")
    if LATEST_IMAGE.exists():
        frame = load_growth_frame()
        caption = None
        if not frame.empty:
            latest = frame.iloc[-1]
            caption = f"{latest['timestamp']:%d %b %H:%M} · green {latest['green_percentage']:.1f} %"
        st.image(str(LATEST_IMAGE), caption=caption, width="stretch")
    else:
        st.caption("No photo yet — start the timelapse on the Camera page.")


@st.cache_data(ttl=120, show_spinner=False)
def _plant_rows():
    return plant_health.load()


def _health(ctx):
    """Per plant: what the photos show (leggy, curling, paler, stalled) and why it may be."""
    explain.heading("**🩺 Plant health ✨**", "plant_health")
    rows = _plant_rows()
    if rows.empty:
        st.caption("No plant measurements yet — they come with the timelapse photos.")
        return
    names = [p.replace("_", " ").title() for p in ctx["settings"].get("plants") or []]
    for number, findings in plant_health.assess(rows, ctx["state"]).items():
        name = names[number - 1] if number <= len(names) else f"Plant {number}"
        if not findings:
            st.markdown(f"✅ **{name}** — nothing unusual in the recent photos.")
            continue
        for f in findings:
            st.markdown(f"⚠️ **{name}: {f.title}.** {f.detail}")
            if f.causes:
                st.caption("Possible causes: " + " ".join(f.causes))
    st.caption("Each plant is compared with its own timelapse photos from the last few days.")


def _live():
    ctx = live_context()
    _status_line(ctx)
    _alert_strip(ctx)
    _readings(ctx)


def _power():
    ctx = live_context()
    render_power_panel(ctx["mqtt_status"], set_power,
                       send_error=st.session_state.get("power_send_error"),
                       sent=st.session_state.get("power_sent"))


def _light():
    ctx = live_context()
    # Make-up light is an operator control: admin site only.
    render_grow_light_panel(ctx["state"], ctx["node_live"],
                            set_light_makeup if is_admin_site() else None,
                            send_error=st.session_state.pop("light_makeup_send_error", None))


def _side():
    ctx = live_context()
    photo_col, bottle_col = st.columns([1.3, 1])
    with photo_col:
        _photo()
        _health(ctx)
    with bottle_col:
        _bottles(ctx)


def page():
    explain.heading("🌱 Smart Garden", "home", "title")
    run_live(_live, LIVE_REFRESH_S)
    # The panel ON/OFF switch, for when the garden is out of reach: admin site only.
    if is_admin_site():
        run_live(_power, LIVE_REFRESH_S)
    run_live(_light, LIVE_REFRESH_S)
    run_live(_side, 5)
