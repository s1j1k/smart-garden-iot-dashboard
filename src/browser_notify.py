"""System notifications on the device that has the dashboard open (phone or computer).

Uses the service worker registered by the PWA shell (``pwa.py``) so it works on
Android and on an installed iOS app, and falls back to ``new Notification`` on
desktop. Each device remembers in localStorage what it has already shown, with
the same delays and repeat intervals as the ntfy notifier.

Browsers only allow this on HTTPS or http://localhost — see README "Alerts".
"""

from __future__ import annotations

import json

import streamlit as st

from alerts import SEVERITY_ICON

_SHOW = """
<script>
(() => {
  const alerts = %(alerts)s;
  const w = window;
  if (!("Notification" in w) || Notification.permission !== "granted") return;
  const repeatMs = {fault: 2*3600e3, warning: 12*3600e3, info: 24*3600e3};
  let seen = {};
  try { seen = JSON.parse(localStorage.getItem("sg-notified") || "{}"); } catch (e) {}
  const now = Date.now();
  const show = (a) => {
    const opts = {body: a.body, tag: a.key, icon: "/pwa/icon-192.png", badge: "/pwa/badge.png",
                  data: {url: "/alerts"}};
    const fallback = () => { try { new Notification(a.title, opts); } catch (e) {} };
    if (navigator.serviceWorker) {
      navigator.serviceWorker.getRegistration("/")
        .then((reg) => reg ? reg.showNotification(a.title, opts) : fallback())
        .catch(fallback);
    } else { fallback(); }
  };
  for (const a of alerts) {
    const rec = seen[a.key] || {first: now};
    seen[a.key] = rec;
    if (now - rec.first < a.after * 1000) continue;
    if (!rec.sent || now - rec.sent > repeatMs[a.severity]) { show(a); rec.sent = now; }
  }
  for (const key of Object.keys(seen)) {
    if (!alerts.some((a) => a.key === key)) delete seen[key];
  }
  try { localStorage.setItem("sg-notified", JSON.stringify(seen)); } catch (e) {}
})();
</script>
"""

_ENABLE = """
<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
  <button id="sg-notify-btn" style="padding:8px 14px;border-radius:8px;border:1px solid #8b5cf6;
    background:#8b5cf6;color:#fff;font-weight:600;cursor:pointer">
    🔔 Enable notifications on this device</button>
  <span id="sg-notify-state" style="font-size:.9rem;opacity:.8"></span>
</div>
<script>
(() => {
  const btn = document.getElementById("sg-notify-btn");
  const label = document.getElementById("sg-notify-state");
  const secure = window.isSecureContext;
  const render = () => {
    if (!secure) {
      label.textContent = "This page is not on HTTPS or localhost, so the browser blocks "
        + "notifications. Use ntfy below, or open the dashboard over HTTPS.";
      btn.disabled = true; btn.style.opacity = .5; return;
    }
    if (!("Notification" in window)) {
      label.textContent = "This browser has no notifications. On iPhone, first add the "
        + "dashboard to the Home Screen (Share → Add to Home Screen) and open it from there.";
      btn.disabled = true; btn.style.opacity = .5; return;
    }
    const p = Notification.permission;
    label.textContent = p === "granted" ? "✓ Notifications are on for this device."
      : p === "denied" ? "Blocked — allow notifications for this site in the browser settings."
      : "Not enabled yet.";
    btn.style.display = p === "granted" ? "none" : "";
  };
  btn.onclick = () => Notification.requestPermission().then((p) => {
    render();
    if (p === "granted" && navigator.serviceWorker) {
      navigator.serviceWorker.getRegistration("/").then((reg) => reg && reg.showNotification(
        "Smart garden", {body: "Notifications are on for this device.",
                         icon: "/pwa/icon-192.png"}));
    }
  });
  render();
})();
</script>
"""


def payload(alerts, include_info=True):
    """The alerts a device should notify about, as plain dicts."""
    out = []
    for alert in alerts:
        if alert.severity == "info" and not include_info:
            continue
        body = alert.detail + (f"\n→ {alert.action}" if alert.action else "")
        out.append({"key": alert.key, "severity": alert.severity, "after": alert.notify_after_s,
                    "title": f"{SEVERITY_ICON[alert.severity]} {alert.title}", "body": body})
    return out


def show_notifications(alerts):
    """Notify on this device for new alerts (no-op until the user enables notifications)."""
    data = json.dumps(payload(alerts)).replace("</", "<\\/")
    st.html(_SHOW % {"alerts": data}, unsafe_allow_javascript=True)


def enable_button():
    st.html(_ENABLE, unsafe_allow_javascript=True)
