"""Smart Garden dashboard: side navigation over the garden's pages.

Run through ``server.py`` to get the installable app (manifest + service worker):
``streamlit run src/server.py``. ``streamlit run src/app.py`` still works, without the
app install and phone notifications.
"""

import streamlit as st

import explain
from admin_auth import is_admin_site
from config import archive_now
from alerts import counts
from browser_notify import show_notifications
from camera_view import render_camera_tab
from core import CAMERA_REFRESH_S, LIVE_REFRESH_S, init_session, live_context, run_live
from pwa import MOBILE_CSS
from version import version_line
from views import alerts_page, home, learn, settings, stage, trends
from views.nav import PAGES

st.set_page_config(page_title="Smart Garden", page_icon="🌱", layout="wide",
                   initial_sidebar_state="auto")
init_session()
st.html(MOBILE_CSS)
explain.style()


def camera_page():
    explain.heading("📷 Camera", "camera", "title")
    run_live(render_camera_tab, CAMERA_REFRESH_S)


PAGES.update({
    "home": st.Page(home.page, title="Home", icon=":material/home:", default=True),
    "trends": st.Page(trends.page, title="Trends", icon=":material/monitoring:",
                      url_path="trends"),
    "stage": st.Page(stage.page, title="Grow stage", icon=":material/potted_plant:",
                     url_path="stage"),
    "camera": st.Page(camera_page, title="Camera", icon=":material/photo_camera:",
                      url_path="camera"),
    "alerts": st.Page(alerts_page.page, title="Alerts", icon=":material/notifications:",
                      url_path="alerts"),
    "learn": st.Page(learn.page, title="Learn", icon=":material/school:", url_path="learn"),
    "settings": st.Page(settings.page, title="Settings", icon=":material/settings:",
                        url_path="settings"),
})

# Settings (calibration, connection, raw data, device health) is admin-only.
ADMIN = is_admin_site()
if not ADMIN:
    PAGES.pop("settings")
help_pages = [PAGES["alerts"], PAGES["learn"]] + ([PAGES["settings"]] if ADMIN else [])
nav = st.navigation(
    {
        "Garden": [PAGES["home"], PAGES["trends"], PAGES["stage"], PAGES["camera"]],
        "Help": help_pages,
    },
    position="sidebar",
)


def _sidebar_status():
    ctx = live_context()
    n = counts(ctx["alerts"])
    parts = ["🟢 Controller live" if ctx["node_live"] else "⚪ Controller offline"]
    if n["fault"]:
        parts.append(f"🔴 {n['fault']} fault{'s' * (n['fault'] != 1)}")
    if n["warning"]:
        parts.append(f"🟠 {n['warning']} warning{'s' * (n['warning'] != 1)}")
    if n["info"]:
        parts.append(f"🔵 {n['info']} reminder{'s' * (n['info'] != 1)}")
    st.caption("  \n".join(parts))
    st.caption(version_line(ctx["state"]))
    # Device notifications for new alerts, on whichever page is open.
    show_notifications(ctx["alerts"])


with st.sidebar:
    run_live(_sidebar_status, LIVE_REFRESH_S)
    if ADMIN:
        st.link_button("🔐 Log out of admin", "/logout", width="stretch")

if archive_now():
    st.info("Archive of the trial, which ended on 21 October 2026. Readings are shown as at "
            f"{archive_now().astimezone():%d %b %Y %H:%M}; the controller is no longer running.")
nav.run()
