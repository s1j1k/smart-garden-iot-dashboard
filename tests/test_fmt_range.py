import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fmt_range import fmt_range  # noqa: E402


def test_equal_ends_print_one_value():
    assert fmt_range(6.0, 6.0, 1) == "6.0"
    assert fmt_range(5.999, 6.001, 2) == "6.00"


def test_range_and_unit():
    assert fmt_range(5.5, 6.5, 1) == "5.5–6.5"
    assert fmt_range(600, 800, 0, " ppm") == "600–800 ppm"
    assert fmt_range(1, 2, None, " %", sep=" – ") == "1 – 2 %"


def test_missing_end():
    assert fmt_range(None, 6.5) == "—"
