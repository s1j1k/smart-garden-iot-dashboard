"""One way to print a range, so a band whose ends match never shows as "6.0–6.0"."""

from __future__ import annotations


def fmt_range(lo, hi, digits=1, unit="", sep="–"):
    """'5.5–6.5' (or '6.0' when both ends round the same); '—' if either end is missing.

    digits=None uses the general format (:g). unit is appended as given, so pass
    ' ppm' with its leading space.
    """
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return "—"
    if digits is None:
        a, b = f"{lo:g}", f"{hi:g}"
    else:
        a, b = f"{lo:.{digits}f}", f"{hi:.{digits}f}"
    if a == b:
        return f"{a}{unit}"
    return f"{a}{sep}{b}{unit}"
