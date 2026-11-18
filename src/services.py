"""State of the Pi's systemd services (dashboard, notifier, camera), for the UI.

Empty on machines without systemd (e.g. a Windows dev PC), so callers can skip
the panel there.
"""

from __future__ import annotations

import shutil
import subprocess

SERVICES = {
    "smart-garden-ingest": "Data ingest (SQLite)",
    "smart-garden-dashboard": "Public dashboard",
    "smart-garden-admin": "Admin dashboard",
    "smart-garden-notifier": "Alert notifier",
    "smart-garden-camera": "Camera capture",
}


def service_states() -> dict[str, str]:
    """{unit: "active" | "inactive" | "failed" | ...}, or {} without systemd."""
    if shutil.which("systemctl") is None:
        return {}
    try:
        out = subprocess.run(["systemctl", "is-active", *SERVICES], capture_output=True,
                             text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(zip(SERVICES, out)) if len(out) == len(SERVICES) else {}


def restart_hint(unit: str) -> str:
    return f"sudo systemctl restart {unit}"
