"""Camera command file: paused by default, viewfinder frames never kept, then timelapse."""

import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from camera_control import (  # noqa: E402
    PREVIEW_TIMEOUT_S,
    decide_tick,
    load_command,
    preview_open,
    set_mode,
)


def main() -> None:
    scratch = ROOT / "tests" / "_camera_control"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    command_file = scratch / "command.json"
    preview_dir = scratch / "preview"

    command = load_command(command_file)
    assert command["mode"] == "paused"
    status = {"heartbeat": "2026-09-30 14:00:00", "busy": False, "timelapse_next_at": None}
    now = datetime(2026, 9, 30, 14, 0, 1)
    assert decide_tick(command, status, now) == "wait"

    # Viewfinder: open until it times out.
    command = set_mode("preview", command_file, now=now)
    assert command["mode"] == "preview"
    assert preview_open(command, now)
    assert decide_tick(command, status, now) == "preview"
    later = now + timedelta(seconds=PREVIEW_TIMEOUT_S + 1)
    assert not preview_open(command, later)
    assert decide_tick(command, status, later) == "wait"

    # Closing it deletes its frames.
    preview_dir.mkdir()
    (preview_dir / "frame.jpg").write_bytes(b"jpeg")
    command = set_mode("paused", command_file)
    assert command["preview_until"] is None
    assert not preview_dir.exists()

    # "Use this position" goes straight to the timelapse and also drops frames.
    set_mode("preview", command_file, now=now)
    preview_dir.mkdir()
    (preview_dir / "frame.jpg").write_bytes(b"jpeg")
    (scratch / "layout.json").write_text("{}")              # pots from the old position
    set_mode("timelapse", command_file, now=now)
    command = load_command(command_file)
    assert command["mode"] == "timelapse"
    assert not preview_dir.exists()
    assert command["position_confirmed_at"] == "2026-09-30 14:00:01"
    assert not (scratch / "layout.json").exists()
    assert decide_tick(command, status, now) == "timelapse"
    due = dict(status, timelapse_next_at="2026-09-30 15:00:00")
    assert decide_tick(command, due, now) == "wait"

    set_mode("paused", command_file)
    assert load_command(command_file)["mode"] == "paused"

    shutil.rmtree(scratch, ignore_errors=True)
    print("camera control ok")


if __name__ == "__main__":
    main()
