"""Download the timelapse so far: every kept photo as a zip, or stitched into an MP4.

The reel is every JPEG in ``data/camera/images/``: timelapse frames (every 5 minutes),
named by capture time so they sort in order.
The video (each frame stamped with its capture time) is built with ffmpeg a day at a time (``data/camera/timelapse_days/``)
and the days joined into ``data/camera/timelapse.mp4``; scripts/build_timelapse.py
(the smart-garden-timelapse service) brings it up to date every hour, and a
download click brings it fully up to date. Photos are zipped one day at a time into ``data/camera/zips/``
on disk: a download is held in memory while it is sent, and a whole trial
at one photo every 5 minutes is several GB, more than a Pi has.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from camera_paths import CAMERA_DATA_DIR, IMAGE_DIR

VIDEO_PATH = CAMERA_DATA_DIR / "timelapse.mp4"   # with timelapse.json: which photos it holds
SEGMENT_DIR_NAME = "timelapse_days"                # one MP4 per day, joined into VIDEO_PATH
ZIP_DIR = CAMERA_DATA_DIR / "zips"
FPS = 24
VIDEO_WIDTH = 1280
# Each frame's capture time, burnt in along the bottom. Bump STYLE when the
# look of the video changes so day segments built the old way are re-encoded.
STYLE = "stamped-1"
STAMP_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def frames(image_dir: Path = IMAGE_DIR) -> list[Path]:
    """Kept photos, oldest first (file names are capture timestamps)."""
    if not image_dir.exists():
        return []
    return sorted(p for p in image_dir.glob("*.jpg") if p.is_file())


def by_day(paths: list[Path]) -> dict[str, list[Path]]:
    """Photos grouped by capture date (``YYYY-MM-DD``, the start of each file name)."""
    days: dict[str, list[Path]] = {}
    for path in paths:
        days.setdefault(path.name[:10], []).append(path)
    return days


def build_zip(paths: list[Path], target: Path) -> Path:
    """Write ``paths`` into the zip ``target`` on disk, unless it is already current."""
    if not paths:
        raise ValueError("No photos to zip.")
    if is_current(paths, target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.stem + ".tmp.zip")
    try:
        # JPEGs are already compressed; storing them keeps this fast on a Pi.
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as archive:
            for path in paths:
                archive.write(path, arcname=path.name)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def day_zip_bytes(day: str, zip_dir: Path = ZIP_DIR) -> bytes:
    return build_zip(by_day(frames()).get(day, []), zip_dir / f"{day}.zip").read_bytes()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def is_current(paths: list[Path], built: Path) -> bool:
    """``built`` exists and is newer than every photo in ``paths``."""
    if not built.exists() or not paths:
        return False
    return built.stat().st_mtime >= max(p.stat().st_mtime for p in paths)


def _contents(paths: list[Path]) -> dict:
    """What a video built from ``paths`` holds: compared by name, so a photo
    taken while a build runs is never mistaken for being in it."""
    return {"frames": len(paths), "first": paths[0].name, "last": paths[-1].name,
            "style": STYLE}


def _read(meta: Path) -> dict:
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _meta(video: Path) -> Path:
    return video.with_suffix(".json")


def video_info(video: Path = VIDEO_PATH) -> dict:
    """``frames``, ``first``, ``last`` (photo file names) and ``built_at`` of the
    built video; {} if there is none."""
    return _read(_meta(video)) if video.exists() else {}


def video_is_current(paths: list[Path], video: Path = VIDEO_PATH) -> bool:
    """The built video holds exactly ``paths``."""
    if not paths or not video.exists():
        return False
    info = video_info(video)
    return {k: info.get(k) for k in ("frames", "first", "last", "style")} == _contents(paths)


def stamp(path: Path) -> str:
    """The capture time shown on a frame, e.g. ``16 Oct 2026  13:05``, from its file name."""
    try:
        when = datetime.strptime(path.name[:16], "%Y-%m-%d_%H-%M")
    except ValueError:
        return path.stem
    return f"{when.day} {when:%b %Y  %H:%M}"


def _stamp_filter() -> str:
    """drawtext reading each frame's ``ts`` metadata (set per file in the concat listing)."""
    font = (f"fontfile={STAMP_FONT.as_posix()}:" if STAMP_FONT.exists() else "")
    return (f"drawtext={font}text='%{{metadata\\:ts}}':fontsize=34:fontcolor=white:"
            "box=1:boxcolor=black@0.55:boxborderw=12:x=(w-text_w)/2:y=h-text_h-28")


def _entry(path: Path, fps: int | None = None) -> str:
    label = stamp(path).replace("'", "")
    entry = f"file '{path.resolve().as_posix()}'\nfile_packet_metadata 'ts={label}'\n"
    return entry + (f"duration {1 / fps:.4f}\n" if fps else "")


def _encode(paths: list[Path], target: Path, fps: int) -> None:
    """Stitch ``paths`` into an H.264 MP4, one photo per frame, each stamped with its capture time."""
    listing = target.with_suffix(".txt")
    listing.write_text("".join(_entry(p, fps) for p in paths) + _entry(paths[-1]),
                       encoding="utf-8")
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing),
             "-vf", f"scale={VIDEO_WIDTH}:-2,{_stamp_filter()},fps={fps}",
             "-c:v", "libx264", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart"], target, listing)


def _join(segments: list[Path], target: Path) -> None:
    """Join same-format MP4s end to end without re-encoding (seconds, not minutes)."""
    listing = target.with_suffix(".txt")
    listing.write_text("".join(f"file '{s.resolve().as_posix()}'\n" for s in segments),
                       encoding="utf-8")
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
             "-movflags", "+faststart"], target, listing)


def _ffmpeg(args: list[str], target: Path, listing: Path) -> None:
    tmp = target.with_name(target.stem + ".tmp.mp4")
    try:
        result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args, str(tmp)],
                                capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[-300:]}")
        tmp.replace(target)
    finally:
        listing.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


def build_video(paths: list[Path], video: Path = VIDEO_PATH, fps: int = FPS,
                segment_dir: Path | None = None) -> Path:
    """The timelapse of ``paths`` (``fps`` photos per second). Raises on failure.

    Built one day at a time: a day's segment is only re-encoded when its photos
    change (in practice just today's), and the segments are then joined without
    re-encoding, so keeping the video up to date stays quick however long the
    trial runs.
    """
    if not paths:
        raise ValueError("No photos to put in the video yet.")
    if video_is_current(paths, video):
        return video
    segment_dir = segment_dir or video.parent / SEGMENT_DIR_NAME
    segment_dir.mkdir(parents=True, exist_ok=True)
    segments = []
    days = by_day(paths)
    for day, day_paths in days.items():
        segment = segment_dir / f"{day}.mp4"
        if not (segment.exists() and _read(_meta(segment)) == _contents(day_paths)):
            _encode(day_paths, segment, fps)
            _meta(segment).write_text(json.dumps(_contents(day_paths)), encoding="utf-8")
        segments.append(segment)
    for old in segment_dir.glob("*.mp4"):          # days whose photos were deleted
        if old.stem not in days:
            old.unlink(missing_ok=True)
            _meta(old).unlink(missing_ok=True)
    video.parent.mkdir(parents=True, exist_ok=True)
    _join(segments, video)
    info = dict(_contents(paths), built_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _meta(video).write_text(json.dumps(info), encoding="utf-8")
    return video


def video_bytes() -> bytes:
    return build_video(frames()).read_bytes()
