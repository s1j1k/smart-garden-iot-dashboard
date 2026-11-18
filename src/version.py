"""The dashboard's own version: the git commit it is running, read once at start-up."""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _git_version():
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO), "log", "-1", "--format=%h %cs"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out or "unknown"


# "c0eebb4 2026-10-07": commit and commit date of the running code.
DASHBOARD_VERSION = _git_version()


def version_line(state):
    """'Dashboard c0eebb4 (2026-10-07) · Firmware 1.9.4' for the sidebar."""
    commit, _, date = DASHBOARD_VERSION.partition(" ")
    dash = f"Dashboard {commit}" + (f" ({date})" if date else "")
    firmware = (state or {}).get("firmware_version")
    return f"{dash} · Firmware {firmware}" if firmware else f"{dash} · Firmware not reported"
