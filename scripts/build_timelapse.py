#!/usr/bin/env python3
"""Keep the timelapse video up to date (smart-garden-timelapse service).

Every TIMELAPSE_EVERY_S (default 1 h) it brings data/camera/timelapse.mp4 up to
the latest photo. Only today's part is re-encoded (src/timelapse_export.py builds
the video one day at a time), so each update is quick even late in a trial.

  .venv/bin/python scripts/build_timelapse.py          # run forever
  .venv/bin/python scripts/build_timelapse.py --once   # one update, then exit
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import timelapse_export  # noqa: E402


def every_s() -> int:
    try:
        return max(300, int(os.getenv("TIMELAPSE_EVERY_S", "3600")))
    except ValueError:
        return 3600


def update() -> None:
    photos = timelapse_export.frames()
    if not photos or timelapse_export.video_is_current(photos):
        return
    started = time.monotonic()
    timelapse_export.build_video(photos)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] timelapse: {len(photos)} photos up to "
          f"{photos[-1].stem} in {time.monotonic() - started:.0f} s", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="update once and exit")
    args = parser.parse_args()
    while True:
        if not timelapse_export.ffmpeg_available():
            print("ffmpeg not found (sudo apt install ffmpeg); checking again in an hour", flush=True)
        else:
            try:
                update()
            except Exception as exc:  # noqa: BLE001 - keep the service up; retry next round
                print(f"Timelapse update failed: {exc}", flush=True)
        if args.once:
            return
        time.sleep(every_s())


if __name__ == "__main__":
    main()
