"""Gardener settings shared by the dashboard and the alert notifier.

Stored as JSON in ``data/garden_settings.json`` so the always-on notifier
(``scripts/alert_notifier.py``) and every browser see the same plant date and
notification setup. Environment variables override the file (useful in Docker):

    PLANTED_AT       ISO-8601 planting time, e.g. 2026-09-03T17:00:00+10:00
    NTFY_URL         ntfy server, default https://ntfy.sh
    NTFY_TOPIC       topic the phone / desktop ntfy app subscribes to
    NTFY_TOKEN       optional access token for a protected topic
    DASHBOARD_URL    link opened when a notification is tapped
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from config import DATA_DIR, PLANT_DATETIME

SETTINGS_PATH = DATA_DIR / "garden_settings.json"

DEFAULTS = {
    "planted_at": PLANT_DATETIME.isoformat(),
    "plants": ["basil", "lettuce"],
    # Plant in each net pot of the lid, left to right ("" = empty); "plants" follows it.
    "pots": ["basil", "lettuce"],
    "ntfy_url": "https://ntfy.sh",
    "ntfy_topic": "",
    "ntfy_token": "",
    "dashboard_url": "",
    # Lowest severity pushed to ntfy: "fault", "warning" or "info".
    "notify_min_severity": "warning",
    "notify_stage_changes": True,
    # Times the plants were out of the tank (housing rebuilds, tests), as
    # [{"start": ISO, "end": ISO, "reason": text}]. development.py counts these
    # days as unmeasured instead of reading the empty tank as growing conditions.
    "breaks": [],
    # PPFD (umol m-2 s-1) of the grow light measured at canopy height, or None.
    # With it the development model uses the daily light integral, not just hours.
    "grow_light_ppfd": None,
}

_ENV = {
    "planted_at": "PLANTED_AT",
    "ntfy_url": "NTFY_URL",
    "ntfy_topic": "NTFY_TOPIC",
    "ntfy_token": "NTFY_TOKEN",
    "dashboard_url": "DASHBOARD_URL",
}


def load(path: Path = SETTINGS_PATH) -> dict:
    settings = dict(DEFAULTS)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            settings.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (FileNotFoundError, ValueError):
        pass
    for key, env in _ENV.items():
        value = os.getenv(env, "").strip()
        if value:
            settings[key] = value
    return settings


def save(updates: dict, path: Path = SETTINGS_PATH) -> dict:
    current = {}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        pass
    current.update({k: v for k, v in updates.items() if k in DEFAULTS})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    tmp.replace(path)
    return load(path)


def planted_at(settings: dict | None = None) -> datetime:
    value = (settings or load()).get("planted_at")
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return PLANT_DATETIME
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=PLANT_DATETIME.tzinfo)
