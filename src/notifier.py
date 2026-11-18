"""Push notifications for alerts: decide what to send, then send it via ntfy.

ntfy (https://ntfy.sh, or a self-hosted server) delivers the same message to
the ntfy phone app (Android / iOS) and to desktop browsers subscribed to the
topic, so the gardener is reached whether or not the dashboard is open.

``AlertTracker`` remembers, in ``data/alert_state.json``, when each alert was
first seen and last pushed:

* a new alert is pushed once it has lasted ``notify_after_s``;
* while it stays active it is re-pushed every ``REPEAT_S[severity]``;
* when a pushed fault or warning clears, one "resolved" message is sent.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from alerts import SEVERITY_ORDER
from config import DATA_DIR

STATE_PATH = DATA_DIR / "alert_state.json"

REPEAT_S = {"fault": 2 * 3600, "warning": 12 * 3600, "info": 24 * 3600}
NTFY_PRIORITY = {"fault": "urgent", "warning": "high", "info": "default", "resolved": "low"}
NTFY_PRIORITY_LEVEL = {"urgent": 5, "high": 4, "default": 3, "low": 2, "min": 1}
NTFY_TAGS = {"fault": "rotating_light", "warning": "warning", "info": "seedling",
             "resolved": "white_check_mark"}


def _ts(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class AlertTracker:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        try:
            self.state = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.state = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def update(self, alerts, now=None, min_severity="warning", stage_changes=True):
        """Return [(kind, alert_or_record)] to send now; kind is new / repeat / resolved."""
        now = now or datetime.now(timezone.utc)
        floor = SEVERITY_ORDER.get(min_severity, 1)
        active = {a.key: a for a in alerts}
        out = []
        for key, alert in active.items():
            wanted = SEVERITY_ORDER[alert.severity] <= floor or key == "refill.done" or (
                stage_changes and key == "stage_change_due")
            rec = self.state.setdefault(key, {"first_seen": now.isoformat()})
            rec.update({"severity": alert.severity, "title": alert.title})
            if not wanted:
                continue
            first = _ts(rec["first_seen"]) or now
            if (now - first).total_seconds() < alert.notify_after_s:
                continue
            last = _ts(rec.get("last_sent"))
            if last is None:
                out.append(("new", alert))
            elif (now - last).total_seconds() >= REPEAT_S[alert.severity]:
                out.append(("repeat", alert))
            else:
                continue
            rec["last_sent"] = now.isoformat()
        for key in list(self.state):
            if key in active:
                continue
            rec = self.state.pop(key)
            if rec.get("last_sent") and rec.get("severity") in ("fault", "warning"):
                out.append(("resolved", rec))
        return out


def format_message(kind, item):
    """(title, body, priority, tags) for one notification."""
    if kind == "resolved":
        return (f"Resolved: {item.get('title', 'alert')}", "This alert has cleared.",
                NTFY_PRIORITY["resolved"], NTFY_TAGS["resolved"])
    prefix = "Still active: " if kind == "repeat" else ""
    body = item.detail
    if item.action:
        body = f"{body}\n→ {item.action}" if body else f"→ {item.action}"
    return (f"{prefix}{item.title}", body or item.title,
            NTFY_PRIORITY[item.severity], NTFY_TAGS[item.severity])


def send_ntfy(settings, title, body, priority="default", tags="", timeout_s=10):
    """Publish one message with ntfy's JSON API (keeps °C / CO₂ intact). Returns (ok, error)."""
    topic = (settings.get("ntfy_topic") or "").strip()
    if not topic:
        return False, "No ntfy topic set."
    server = (settings.get("ntfy_url") or "https://ntfy.sh").rstrip("/")
    message = {
        "topic": topic,
        "title": title,
        "message": body,
        "priority": NTFY_PRIORITY_LEVEL.get(priority, 3),
        "tags": [tags] if tags else [],
    }
    if settings.get("dashboard_url"):
        message["click"] = settings["dashboard_url"]
    headers = {"Content-Type": "application/json"}
    if settings.get("ntfy_token"):
        headers["Authorization"] = f"Bearer {settings['ntfy_token']}"
    request = urllib.request.Request(server, data=json.dumps(message).encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return 200 <= response.status < 300, ""
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def dispatch(tracker, alerts, settings, now=None, sender=send_ntfy):
    """Update the tracker and push what is due. Returns a list of (title, ok, error)."""
    results = []
    due = tracker.update(alerts, now, settings.get("notify_min_severity", "warning"),
                         settings.get("notify_stage_changes", True))
    for kind, item in due:
        title, body, priority, tags = format_message(kind, item)
        ok, err = sender(settings, title, body, priority, tags)
        results.append((title, ok, err))
        if not ok and kind != "resolved":
            # Try again on the next pass instead of waiting for the repeat interval.
            tracker.state.get(item.key, {}).pop("last_sent", None)
    tracker.save()
    return results
