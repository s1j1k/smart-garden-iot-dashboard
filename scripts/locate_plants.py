#!/usr/bin/env python3
"""Find the net pots and plants with a pre-trained model (src/plant_locator.py).

Runs as the smart-garden-detector service on the Pi, at low CPU priority:
whenever the latest timelapse frame is newer than the last layout and at least
LOCATE_EVERY_S old news (default 3 h), or the camera position was confirmed
since, it runs Grounding DINO on data/camera/latest.jpg and writes
data/camera/layout.json (pots and plants, as fractions of the frame) and
data/camera/layout.jpg (the photo with them drawn on).

  .venv/bin/python scripts/locate_plants.py                 # run forever
  .venv/bin/python scripts/locate_plants.py --once PHOTO    # one photo, print the layout
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from camera_control import COMMAND_PATH, load_command, parse_stamp, save_json  # noqa: E402
from camera_paths import LATEST_IMAGE, LAYOUT_IMAGE, LAYOUT_JSON  # noqa: E402

POLL_S = 60


def locate_every_s() -> int:
    try:
        return max(600, int(os.getenv("LOCATE_EVERY_S", str(3 * 3600))))
    except ValueError:
        return 3 * 3600


def _due(layout: dict) -> bool:
    """A newer frame exists, and the last layout is old or predates the camera
    position being confirmed in the viewfinder."""
    if not LATEST_IMAGE.exists():
        return False
    frame_mtime = LATEST_IMAGE.stat().st_mtime
    last = float(layout.get("source_mtime") or 0)
    if frame_mtime <= last:
        return False
    confirmed = parse_stamp(load_command(COMMAND_PATH).get("position_confirmed_at"))
    if confirmed is not None and confirmed.timestamp() > last:
        return True
    return frame_mtime - last >= locate_every_s()


def run_once(image: Path) -> dict:
    import plant_locator

    layout = plant_locator.locate(image)
    if image.resolve() == LATEST_IMAGE.resolve():
        plant_locator.draw_layout(image, layout, LAYOUT_IMAGE)
        save_json(LAYOUT_JSON, layout)
    return layout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", type=Path, help="locate pots and plants in one photo and exit")
    args = parser.parse_args()
    if args.once:
        print(json.dumps(run_once(args.once), indent=2))
        return

    print(f"Plant locator started; re-checks every {locate_every_s() // 60} min of new frames")
    while True:
        layout = json.loads(LAYOUT_JSON.read_text()) if LAYOUT_JSON.exists() else {}
        if _due(layout):
            try:
                layout = run_once(LATEST_IMAGE)
                print(f"[{layout['located_at']}] {len(layout['pots'])} pot(s), "
                      f"{len(layout['plants'])} plant(s) in {layout['seconds']} s")
            except Exception as exc:  # noqa: BLE001 - keep the service up; retry next frame
                print(f"Locating failed: {exc}")
                save_json(LAYOUT_JSON, dict(layout, source_mtime=LATEST_IMAGE.stat().st_mtime,
                                            error=str(exc)))
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
