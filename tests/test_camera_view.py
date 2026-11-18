"""Growth CSV loading for the camera tab."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from camera_paths import capture_interval_s, format_capture_interval  # noqa: E402
from camera_view import load_growth_frame, observed_capture_interval_s  # noqa: E402


def main() -> None:
    missing = load_growth_frame(ROOT / "data" / "camera" / "does-not-exist.csv")
    assert missing.empty

    sample = ROOT / "tests" / "_growth_sample.csv"
    sample.write_text(
        "timestamp,image_path,green_ratio,green_percentage,plant_area,sprouted\n"
        "2026-09-26 14:52:00,data/camera/images/a.jpg,0.03,3.0,1200,true\n"
        "bad,data/camera/images/b.jpg,nope,x,y,false\n",
        encoding="utf-8",
    )
    try:
        frame = load_growth_frame(sample)
    finally:
        sample.unlink(missing_ok=True)
    assert len(frame) == 1
    assert abs(float(frame.iloc[0]["green_percentage"]) - 3.0) < 1e-9
    assert str(frame.iloc[0]["sprouted"]).lower() == "true"
    assert observed_capture_interval_s(frame) is None

    series = ROOT / "tests" / "_growth_series.csv"
    series.write_text(
        "timestamp,image_path,green_ratio,green_percentage,plant_area,sprouted\n"
        "2026-09-26 14:52:00,data/camera/images/a.jpg,0.03,3.0,1200,true\n"
        "2026-09-26 14:52:10,data/camera/images/b.jpg,0.04,4.0,1300,true\n"
        "2026-09-26 14:52:20,data/camera/images/c.jpg,0.05,5.0,1400,true\n",
        encoding="utf-8",
    )
    try:
        series_frame = load_growth_frame(series)
    finally:
        series.unlink(missing_ok=True)
    assert abs(observed_capture_interval_s(series_frame) - 10.0) < 1e-6
    assert capture_interval_s() >= 1
    assert format_capture_interval(3600) == "1 hour"
    assert format_capture_interval(10) == "10 seconds"
    print("camera view ok")


if __name__ == "__main__":
    main()
