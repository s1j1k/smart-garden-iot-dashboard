#!/usr/bin/env python3
"""Re-measure every timelapse photo with the current plant measurement.

Photos taken before the pot-rim scale existed have only frame shares in
data/camera/plants.jsonl, so their sizes jump whenever the camera moved. This
re-runs growth_detection.find_plants on every JPEG in data/camera/images/ (named
by capture time, e.g. 2026-10-15_13-06-00.jpg) and writes a new plants.jsonl
with area_cm2 / height_cm / width_cm. The old log is kept as plants.jsonl.bak,
and each frame keeps its "light" (day / night) from the old log.

Runs at low priority; on a Pi 4 about 0.3 s a photo.

    python scripts/remeasure_plants.py            # all photos
    python scripts/remeasure_plants.py --every 3  # every third photo (quicker)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402

import growth_detection  # noqa: E402
from camera_paths import IMAGE_DIR, PLANTS_LOG  # noqa: E402


def capture_time(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def old_light(log: Path) -> dict[str, str]:
    light = {}
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                light[entry["image"]] = entry.get("light", "day")
            except (ValueError, KeyError, TypeError):
                continue
    return light


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--every", type=int, default=1, help="use every Nth photo")
    parser.add_argument("--images", type=Path, default=IMAGE_DIR)
    parser.add_argument("--log", type=Path, default=PLANTS_LOG)
    args = parser.parse_args()
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass

    light = old_light(args.log)
    photos = sorted(p for p in args.images.glob("*.jpg") if capture_time(p))[::max(1, args.every)]
    pots = growth_detection.load_pots()
    fresh = args.log.with_suffix(".jsonl.new")
    scaled = unlit = 0
    with fresh.open("w", encoding="utf-8") as out:
        for i, photo in enumerate(photos, start=1):
            image = cv2.imread(str(photo))
            if image is None:
                continue
            white, _ = growth_detection.white_light(image)
            plants = growth_detection.find_plants(image, pots=pots) if white else []
            unlit += not white
            scaled += any(p.get("area_cm2") is not None for p in plants)
            out.write(json.dumps({"timestamp": f"{capture_time(photo):%Y-%m-%d %H:%M:%S}",
                                  "image": photo.name, "light": light.get(photo.name, "day"),
                                  "flash": white, "plants": plants}) + "\n")
            if i % 200 == 0:
                print(f"{i}/{len(photos)} photos, {scaled} with a scale", flush=True)
    # The camera keeps logging while this runs: carry over any frame it wrote
    # that was not among the photos re-measured.
    done = {p.name for p in photos}
    if args.log.exists():
        with fresh.open("a", encoding="utf-8") as out:
            for line in args.log.read_text(encoding="utf-8").splitlines():
                try:
                    image = json.loads(line).get("image")
                except ValueError:
                    continue
                if image and image not in done and (args.every == 1 or image > photos[-1].name):
                    out.write(line + "\n")
        args.log.replace(args.log.with_suffix(".jsonl.bak"))
    fresh.replace(args.log)
    print(f"Re-measured {len(photos)} photos ({scaled} scaled in cm, {unlit} not lit white). Old log: "
          f"{args.log.with_suffix('.jsonl.bak')}")


if __name__ == "__main__":
    main()
