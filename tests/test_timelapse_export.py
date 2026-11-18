"""Timelapse downloads and clearing every photo."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import camera_control  # noqa: E402
import timelapse_export  # noqa: E402


def _paths(tmp_path):
    return {
        "image_dir": tmp_path / "images",
        "growth_csv": tmp_path / "growth.csv",
        "latest_image": tmp_path / "latest.jpg",
        "debug_dir": tmp_path / "debug",
    }


def test_frames_sort_by_capture_time_and_zip_per_day(tmp_path):
    names = ("2026-10-01_10-00-00.jpg", "2026-09-30_09-15-00.jpg", "2026-09-30_09-20-00.jpg")
    for name in (*names, "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    frames = timelapse_export.frames(tmp_path)
    assert [f.name for f in frames] == sorted(names)
    days = timelapse_export.by_day(frames)
    assert list(days) == ["2026-09-30", "2026-10-01"]
    target = tmp_path / "zips" / "2026-09-30.zip"
    assert timelapse_export.build_zip(days["2026-09-30"], target) == target
    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == ["2026-09-30_09-15-00.jpg", "2026-09-30_09-20-00.jpg"]
    assert timelapse_export.is_current(days["2026-09-30"], target)
    assert not list(target.parent.glob("*.tmp.zip"))


def test_frame_stamp_from_file_name():
    assert timelapse_export.stamp(Path("2026-10-16_13-05-46.jpg")) == "16 Oct 2026  13:05"
    assert timelapse_export.stamp(Path("2026-10-06_09-00-00_1.jpg")) == "6 Oct 2026  09:00"
    assert timelapse_export.stamp(Path("odd.jpg")) == "odd"


def test_video_needs_photos(tmp_path):
    with pytest.raises(ValueError):
        timelapse_export.build_video([], tmp_path / "v.mp4")


@pytest.mark.skipif(not timelapse_export.ffmpeg_available(), reason="ffmpeg not installed")
def test_video_builds_and_is_cached(tmp_path):
    from PIL import Image

    paths = []
    for i in range(3):
        path = tmp_path / f"2026-10-01_10-0{i}-00.jpg"
        Image.new("RGB", (64, 48), (40 * i, 120, 60)).save(path)
        paths.append(path)
    video = timelapse_export.build_video(paths, tmp_path / "t.mp4", fps=4)
    assert video.stat().st_size > 0
    assert timelapse_export.video_is_current(paths, video)


def test_clear_all_photos(tmp_path):
    p = _paths(tmp_path)
    p["image_dir"].mkdir()
    (p["image_dir"] / "2026-10-01_10-00-00.jpg").write_bytes(b"x")
    p["debug_dir"].mkdir()
    (p["debug_dir"] / "debug_a.jpg").write_bytes(b"x")
    p["growth_csv"].write_text("timestamp\n")
    p["latest_image"].write_bytes(b"x")
    assert camera_control.clear_all_photos(**p) == 1
    assert not p["image_dir"].exists() and not p["debug_dir"].exists()
    assert not (tmp_path / "zips").exists()
    assert not p["growth_csv"].exists() and not p["latest_image"].exists()


def _duration_s(video):
    import re
    import subprocess
    out = subprocess.run(["ffmpeg", "-i", str(video)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


@pytest.mark.skipif(not timelapse_export.ffmpeg_available(), reason="ffmpeg not installed")
def test_video_built_by_day_and_kept_up_to_date(tmp_path):
    from PIL import Image

    images = tmp_path / "images"
    images.mkdir()

    def photo(name, shade):
        path = images / name
        Image.new("RGB", (64, 48), (shade, 120, 60)).save(path)
        return path

    for i in range(4):
        photo(f"2026-10-11_10-0{i}-00.jpg", 30 * i)
    for i in range(4):
        photo(f"2026-10-12_10-0{i}-00.jpg", 30 * i)
    video = tmp_path / "timelapse.mp4"
    timelapse_export.build_video(timelapse_export.frames(images), video, fps=4)
    yesterday = tmp_path / "timelapse_days" / "2026-10-11.mp4"
    assert yesterday.exists() and (tmp_path / "timelapse_days" / "2026-10-12.mp4").exists()
    assert abs(_duration_s(video) - 8 / 4) < 0.3
    built = yesterday.stat().st_mtime_ns

    # A photo taken after the build: the video is out of date, by name not by clock.
    photo("2026-10-12_10-05-00.jpg", 200)
    paths = timelapse_export.frames(images)
    assert not timelapse_export.video_is_current(paths, video)
    timelapse_export.build_video(paths, video, fps=4)
    assert timelapse_export.video_is_current(paths, video)
    assert yesterday.stat().st_mtime_ns == built            # finished day not re-encoded
    assert abs(_duration_s(video) - 9 / 4) < 0.3
    info = timelapse_export.video_info(video)
    assert info["frames"] == 9 and info["last"] == "2026-10-12_10-05-00.jpg"

    # Deleting a day's photos drops its segment.
    for path in images.glob("2026-10-11_*.jpg"):
        path.unlink()
    timelapse_export.build_video(timelapse_export.frames(images), video, fps=4)
    assert not yesterday.exists()
