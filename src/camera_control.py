"""Dashboard ↔ camera-capture commands. The capture script owns the Pi camera.

The dashboard writes ``command.json``. ``camera_capture.py`` writes ``status.json``
and is the only process that opens the camera. While the viewfinder is open the
capture script keeps overwriting one small frame in ``preview/``; that folder is
deleted when the viewfinder closes, so viewfinder frames never reach the reel.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from camera_paths import (
    CAMERA_DATA_DIR,
    DEBUG_DIR,
    GROWTH_CSV,
    IMAGE_DIR,
    LATEST_IMAGE,
    PLANTS_LOG,
    REPO_ROOT,
)

COMMAND_PATH = CAMERA_DATA_DIR / "command.json"
STATUS_PATH = CAMERA_DATA_DIR / "status.json"
PREVIEW_DIR = CAMERA_DATA_DIR / "preview"
PREVIEW_IMAGE = PREVIEW_DIR / "frame.jpg"

MODE_PAUSED = "paused"
MODE_PREVIEW = "preview"
MODE_TIMELAPSE = "timelapse"
MODES = (MODE_PAUSED, MODE_PREVIEW, MODE_TIMELAPSE)
HEARTBEAT_STALE_S = 30
# A viewfinder left open (tab closed) shuts itself, so the grow light does not
# stay white and the camera does not keep writing frames.
PREVIEW_TIMEOUT_S = 10 * 60

_DEFAULT_COMMAND = {
    "mode": MODE_PAUSED,
    "preview_until": None,
}


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S")


def parse_stamp(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def repo_relative(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        relative = path
    return str(relative).replace("\\", "/")


def resolve_repo_path(value: str | None) -> Path | None:
    if not value or not isinstance(value, str):
        return None
    path = Path(value)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(default)
    if not isinstance(loaded, dict):
        return dict(default)
    return loaded


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_command(path: Path = COMMAND_PATH) -> dict:
    data = _load_json(path, _DEFAULT_COMMAND)
    mode = data.get("mode")
    data["mode"] = mode if mode in MODES else MODE_PAUSED
    data.setdefault("preview_until", None)
    if not path.exists():
        save_json(path, data)
    return data


def load_status(path: Path = STATUS_PATH) -> dict:
    data = _load_json(path, {})
    data.setdefault("heartbeat", None)
    data.setdefault("busy", False)
    data.setdefault("last_error", None)
    data.setdefault("timelapse_next_at", None)
    data.setdefault("last_timelapse_at", None)
    return data


def camera_is_alive(status: dict, now: datetime, stale_s: int = HEARTBEAT_STALE_S) -> bool:
    heartbeat = parse_stamp(status.get("heartbeat"))
    if heartbeat is None:
        return False
    return 0 <= (now - heartbeat).total_seconds() <= stale_s


def preview_open(command: dict, now: datetime) -> bool:
    """True while the viewfinder is asked for and has not timed out."""
    if command.get("mode") != MODE_PREVIEW:
        return False
    until = parse_stamp(command.get("preview_until"))
    return until is not None and now < until


def decide_tick(command: dict, status: dict, now: datetime) -> str:
    """Return ``preview``, ``timelapse``, or ``wait``."""
    if command.get("mode") == MODE_TIMELAPSE:
        next_at = parse_stamp(status.get("timelapse_next_at"))
        if next_at is None or now >= next_at:
            return "timelapse"
        return "wait"
    if preview_open(command, now):
        return "preview"
    return "wait"


def set_mode(mode: str, path: Path = COMMAND_PATH, now: datetime | None = None) -> dict:
    """Switch mode. Opening the viewfinder (re)starts its PREVIEW_TIMEOUT_S timer;
    going from the viewfinder to the timelapse records the new camera position."""
    if mode not in MODES:
        raise ValueError(f"Unknown camera mode: {mode}")
    command = load_command(path)
    if command["mode"] == MODE_PREVIEW and mode == MODE_TIMELAPSE:
        # "Use this position": the old pot layout no longer matches the view.
        command["position_confirmed_at"] = stamp(now or datetime.now())
        (path.parent / "layout.json").unlink(missing_ok=True)
    command["mode"] = mode
    command["preview_until"] = (
        stamp((now or datetime.now()) + timedelta(seconds=PREVIEW_TIMEOUT_S))
        if mode == MODE_PREVIEW
        else None
    )
    save_json(path, command)
    if mode != MODE_PREVIEW:
        clear_preview(path.parent / PREVIEW_DIR.name)
    return command


def clear_preview(preview_dir: Path = PREVIEW_DIR) -> None:
    """Delete every viewfinder frame. They are never kept."""
    if preview_dir.exists():
        shutil.rmtree(preview_dir, ignore_errors=True)


def unique_image_path(image_dir: Path, when: datetime) -> Path:
    image_dir.mkdir(parents=True, exist_ok=True)
    candidate = image_dir / when.strftime("%Y-%m-%d_%H-%M-%S.jpg")
    suffix = 1
    while candidate.exists():
        candidate = image_dir / f"{when.strftime('%Y-%m-%d_%H-%M-%S')}_{suffix}.jpg"
        suffix += 1
    return candidate


def ensure_growth_csv(csv_path: Path = GROWTH_CSV) -> None:
    if csv_path.exists():
        return
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "timestamp,image_path,green_ratio,green_percentage,plant_area,sprouted\n",
        encoding="utf-8",
    )


def append_growth_row(
    when: datetime,
    image_path: Path,
    result: dict,
    csv_path: Path = GROWTH_CSV,
) -> None:
    import csv

    ensure_growth_csv(csv_path)
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                stamp(when),
                repo_relative(image_path),
                result.get("green_ratio"),
                result.get("green_percentage"),
                result.get("plant_area"),
                result.get("sprouted"),
            ]
        )


def clear_all_photos(
    image_dir: Path = IMAGE_DIR,
    growth_csv: Path = GROWTH_CSV,
    latest_image: Path = LATEST_IMAGE,
    debug_dir: Path = DEBUG_DIR,
    plants_log: Path = PLANTS_LOG,
) -> int:
    """Delete every kept photo, debug images, growth rows, plant boxes and the
    built video and zips. The camera mode is left as it was. Returns photos removed."""
    removed = len(list(image_dir.glob("*.jpg"))) if image_dir.exists() else 0
    for folder in (image_dir, debug_dir, latest_image.parent / "zips",
                   latest_image.parent / "timelapse_days"):
        if folder.exists():
            shutil.rmtree(folder)
    for path in (growth_csv, plants_log, latest_image,
                 latest_image.with_name("latest_plants.jpg"),
                 latest_image.with_name("layout.json"), latest_image.with_name("layout.jpg"),
                 latest_image.parent / "timelapse.mp4", latest_image.parent / "timelapse.json"):
        path.unlink(missing_ok=True)
    return removed


def next_timelapse_at(finished: datetime, interval_s: int) -> str:
    return stamp(finished + timedelta(seconds=interval_s))
