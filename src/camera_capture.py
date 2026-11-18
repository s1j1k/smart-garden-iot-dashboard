"""Raspberry Pi Camera Module 3 capture.

Run on the Pi (picamera2 and OpenCV come from apt, not requirements.txt):

    python src/camera_capture.py

Every photo is taken under a white grow-light "flash" (camera_flash.py): the
ESP32 lights the strip white, the photo is taken, then the light goes back to
its photoperiod. Starts paused. The dashboard Camera tab opens the viewfinder
(live frames for lining the camera up) or starts the timelapse (every 5
minutes) by writing data/camera/command.json. This process is the only one
that opens the camera.

Timelapse frames go to data/camera/images, data/camera/latest.jpg, and
data/camera/growth.csv, with each frame's plant boxes in data/camera/plants.jsonl.
Viewfinder frames (with the plants boxed) overwrite data/camera/preview/frame.jpg
and that folder is deleted when the viewfinder closes. Does not publish MQTT and
does not touch sensor logs.
"""

import json
import os
import shutil
import time
from contextlib import ExitStack
from datetime import datetime

import cv2
import numpy as np
from libcamera import controls
from picamera2 import Picamera2

from camera_control import (
    PREVIEW_IMAGE,
    STATUS_PATH,
    append_growth_row,
    clear_preview,
    decide_tick,
    load_command,
    load_status,
    next_timelapse_at,
    save_json,
    stamp,
    unique_image_path,
)
from camera_paths import (
    CAMERA_DATA_DIR,
    IMAGE_DIR,
    LATEST_IMAGE,
    LATEST_PLANTS_IMAGE,
    PLANTS_LOG,
    capture_interval_s,
    format_capture_interval,
    night_capture_interval_s,
)
from camera_flash import grow_light_flash, grow_light_held, light_mode_after_flash

# Whether the last timelapse photo was lit white (written to the camera status).
_flash_state: dict = {}
from growth_detection import analyse_image, draw_plants, find_plants, white_light

POLL_S = 1.0
PREVIEW_S = 0.5         # gap between viewfinder frames
PREVIEW_WIDTH = 960     # viewfinder frames are small; only stills are full size
HEARTBEAT_S = 5.0

# Fixed exposure under the white flash, day and night: what auto-exposure chose
# for well-lit daytime frames (8-12 Oct: ~15 ms at ISO ~112). In the dark,
# auto-exposure had ramped to ~66 ms and ISO up to ~950 and did not come back
# within the flash, so night frames were blown out and blurred.
FLASH_EXPOSURE_US = int(os.getenv("CAMERA_FLASH_EXPOSURE_US", "15000"))
FLASH_GAIN = float(os.getenv("CAMERA_FLASH_GAIN", "1.12"))
CONTROLS_TIMEOUT_S = 1.5   # wait for the sensor to report the new exposure
LIT_FRAMES = 2             # frames dropped after the light confirms, so the photo is fully lit

# Lens position from the last daytime autofocus. At night the photo reuses it,
# so the flash does not have to stay on for an autofocus sweep.
_lens_position = None


def _analyse(image_path):
    try:
        return analyse_image(image_path), None
    except Exception as error:
        return None, str(error)


def _has_autofocus(camera) -> bool:
    return "AfMode" in camera.camera_controls


def _focus(camera, night: bool) -> None:
    """Focus for one photo, under the flash.

    By day: one autofocus sweep, and remember where the lens ended up. At night,
    once a daytime position is known, set the lens there instead of sweeping, to
    keep the flash short.
    """
    global _lens_position
    if not _has_autofocus(camera):
        return
    if night and _lens_position is not None:
        camera.set_controls({"AfMode": controls.AfModeEnum.Manual,
                             "LensPosition": _lens_position})
        return
    camera.set_controls({"AfMode": controls.AfModeEnum.Auto})
    if camera.autofocus_cycle():
        _lens_position = camera.capture_metadata().get("LensPosition", _lens_position)
    else:
        print("Autofocus did not lock; taking the photo anyway")


def _fixed_exposure(camera) -> None:
    """Fix exposure and gain for the flash, and wait until the frames use them."""
    camera.set_controls({"AeEnable": False, "ExposureTime": FLASH_EXPOSURE_US,
                         "AnalogueGain": FLASH_GAIN})
    deadline = time.monotonic() + CONTROLS_TIMEOUT_S
    while time.monotonic() < deadline:
        meta = camera.capture_metadata()
        if (abs(meta.get("ExposureTime", 0) - FLASH_EXPOSURE_US) <= 0.1 * FLASH_EXPOSURE_US
                and abs(meta.get("AnalogueGain", 0) - FLASH_GAIN) <= 0.1 * FLASH_GAIN):
            return
    print("Camera did not confirm the fixed exposure; taking the photo anyway")


def _auto_exposure(camera) -> None:
    camera.set_controls({"AeEnable": True})


class Viewfinder:
    """Live frames for lining the camera up before a timelapse.

    Keeps the light white and focus continuous while open, and boxes the
    plants it finds. Each frame overwrites the last; close() deletes them, so
    none are ever kept.
    """

    def __init__(self, camera):
        self.camera = camera
        self._open = None
        self._keep_lit = None

    def show(self) -> None:
        if self._open is None:
            self._open = ExitStack()
            self._keep_lit = self._open.enter_context(grow_light_held())
            if _has_autofocus(self.camera):
                self.camera.set_controls({"AfMode": controls.AfModeEnum.Continuous})
            print("Viewfinder open")
        self._keep_lit()
        image = self.camera.capture_image("main").convert("RGB")
        image.thumbnail((PREVIEW_WIDTH, PREVIEW_WIDTH))
        frame = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        draw_plants(frame, find_plants(frame))
        PREVIEW_IMAGE.parent.mkdir(parents=True, exist_ok=True)
        temporary = PREVIEW_IMAGE.parent / "frame.tmp.jpg"
        cv2.imwrite(str(temporary), frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        temporary.replace(PREVIEW_IMAGE)

    def close(self) -> None:
        if self._open is not None:
            if _has_autofocus(self.camera):
                self.camera.set_controls({"AfMode": controls.AfModeEnum.Auto})
            self._open.close()
            self._open = None
            print("Viewfinder closed")
        clear_preview()


def _log_plants(when: datetime, image_path, plants: list[dict], night: bool,
                flash: bool = True) -> None:
    """One JSON line per frame: where each plant is, how big, its shape and colour.
    ``light`` ("day": grow light + flash, "night": flash only) keeps colour
    comparisons to photos taken under the same light (plant_health.py).
    ``flash`` False: the photo was not lit white, so no plants were measured;
    growth curves, health checks and the forests skip it."""
    with PLANTS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": stamp(when), "image": image_path.name,
                                 "light": "night" if night else "day", "flash": flash,
                                 "plants": plants if flash else []}) + "\n")


def _save_boxed_latest(image_path, plants: list[dict]) -> None:
    """The latest frame with each plant boxed, for the Camera page."""
    frame = cv2.imread(str(image_path))
    if frame is None:
        return
    draw_plants(frame, plants)
    temporary = LATEST_PLANTS_IMAGE.with_name("latest_plants.tmp.jpg")
    cv2.imwrite(str(temporary), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    temporary.replace(LATEST_PLANTS_IMAGE)


def _capture_timelapse(camera, when: datetime, night: bool) -> tuple[str | None, bool]:
    """Take one timelapse photo. Returns (analysis error, whether the grow light is off).

    ``night`` is what the previous photo found; it decides the focus. The return
    value is read from this flash's "off" ack and sets the gap to the next photo.
    """
    image_path = unique_image_path(IMAGE_DIR, when)
    _fixed_exposure(camera)   # before the light comes on, so the flash stays short
    try:
        with grow_light_flash(settle_s=0) as lit:
            if not lit:
                # No flash: expose for whatever light there is.
                _auto_exposure(camera)
                time.sleep(1.0)
            for _ in range(LIT_FRAMES):
                camera.capture_metadata()
            _focus(camera, night)
            camera.capture_file(str(image_path))
    finally:
        _auto_exposure(camera)
    if lit:
        night = light_mode_after_flash() == "off"
    shutil.copy2(image_path, LATEST_IMAGE)
    print(f"[{stamp(when)}] timelapse {image_path.name}")
    frame = cv2.imread(str(image_path))
    white, a_median = white_light(frame) if frame is not None else (False, float("nan"))
    if not (lit and white):
        # No white flash (controller offline, or the light did not turn white):
        # under the purple light the leaves cannot be measured, so the photo is
        # kept for the timelapse but not measured.
        why = "no flash ack from the controller" if not lit else f"photo not white (a* {a_median:.0f})"
        print(f"[{stamp(when)}] not measured: {why}")
        _log_plants(when, image_path, [], night, flash=False)
        _flash_state.update(ok=False, reason=why, at=stamp(when))
        return None, night
    _flash_state.update(ok=True, reason="", at=stamp(when))
    result, error = _analyse(image_path)
    if result:
        append_growth_row(when, image_path, result)
        _log_plants(when, image_path, result["plants"], night)
        _save_boxed_latest(image_path, result["plants"])
        print(
            f"Green {result['green_percentage']:.2f}%  "
            f"area {result['plant_area']:.0f}px  "
            f"sprouted {result['sprouted']}"
        )
    elif error:
        print(f"Image analysis failed: {error}")
    return error, night


def main() -> None:
    CAMERA_DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    # Test shots from before the viewfinder are never shown again.
    shutil.rmtree(CAMERA_DATA_DIR / "pending", ignore_errors=True)
    interval_s = capture_interval_s()
    night_interval_s = night_capture_interval_s()
    night = False

    camera = Picamera2()
    camera.configure(camera.create_still_configuration(main={"size": (2304, 1296)}))
    camera.start()
    time.sleep(2)
    viewfinder = Viewfinder(camera)
    viewfinder.close()

    status = load_status()
    status["busy"] = False
    save_json(STATUS_PATH, status)
    print("Smart Garden camera monitoring started (timelapse paused)")
    print(
        f"Timelapse interval once started: {format_capture_interval(interval_s)} "
        f"({interval_s} seconds); {format_capture_interval(night_interval_s)} "
        "while the grow light is off"
    )
    print("Use the dashboard Camera tab to open the viewfinder or start the timelapse.")
    print("Press Ctrl+C to stop.")

    last_status_write = time.monotonic()
    try:
        while True:
            now = datetime.now()
            command = load_command()
            status = load_status()
            action = decide_tick(command, status, now)
            error = None

            if action != "preview":
                viewfinder.close()

            if action == "preview":
                try:
                    viewfinder.show()
                except Exception as exc:
                    error = str(exc)
                    print(f"Viewfinder failed: {error}")
                if error != status.get("last_error"):
                    status["last_error"] = error
                    save_json(STATUS_PATH, status)
                    last_status_write = time.monotonic()
            elif action == "timelapse":
                status["busy"] = True
                status["heartbeat"] = stamp(now)
                save_json(STATUS_PATH, status)
                started = datetime.now()
                try:
                    error, night = _capture_timelapse(camera, started, night)
                    status["last_timelapse_at"] = stamp(started)
                    status["last_flash_ok"] = _flash_state.get("ok")
                    status["last_flash_problem"] = _flash_state.get("reason") or None
                    status["timelapse_next_at"] = next_timelapse_at(
                        started, night_interval_s if night else interval_s)
                except Exception as exc:
                    error = str(exc)
                    print(f"Capture failed: {error}")
                    status["timelapse_next_at"] = next_timelapse_at(datetime.now(), interval_s)
                finally:
                    status["busy"] = False
                status["last_error"] = error

            schedule_cleared = False
            if command.get("mode") != "timelapse" and status.get("timelapse_next_at"):
                status["timelapse_next_at"] = None
                schedule_cleared = True
            status["heartbeat"] = stamp(datetime.now())
            heartbeat_due = time.monotonic() - last_status_write >= HEARTBEAT_S
            if action == "timelapse" or schedule_cleared or heartbeat_due:
                save_json(STATUS_PATH, status)
                last_status_write = time.monotonic()
            time.sleep(PREVIEW_S if action == "preview" else POLL_S)
    except KeyboardInterrupt:
        print("Camera monitoring stopped.")
    finally:
        viewfinder.close()
        camera.stop()
        print("Camera closed.")


if __name__ == "__main__":
    main()
