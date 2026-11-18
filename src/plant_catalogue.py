"""Plant catalogue loader + setpoint resolution + compatibility suggestions.

Loader is the only I/O boundary: swap `load_plants_from_json` for a SQLite
loader with the same Plant dataclass fields without touching resolve/suggest.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from config import DATA_DIR
from fmt_range import fmt_range

DEFAULT_CATALOGUE_PATH = DATA_DIR / "plant_catalogue.json"

# Controller design (report Section 3.6, firmware shared/control_design.h).
# EC dead limits sit this far outside the target band.
EC_DEAD_MARGIN_PPM = 50.0
# pH dead limits are never narrower than setpoint +/- this, so the loop does
# not chase sensor noise or the drift the plants cause between checks.
PH_DEAD_MIN_HALF_WIDTH = 0.3
# Fallback when a plant has no tolerance range: optimum +/- this.
PH_TOLERANCE_MARGIN = 0.5


@dataclass(frozen=True)
class Plant:
    id: str
    common_name: str
    scientific_name: str
    ec_min_mscm: float
    ec_max_mscm: float
    ppm500_min: float
    ppm500_max: float
    ph_min: float
    ph_max: float
    note: str | None = None
    # Range the plant grows normally in (controller dead limits); wider
    # than the optimum ph_min-ph_max.
    ph_tolerance_min: float | None = None
    ph_tolerance_max: float | None = None

    @property
    def ph_tol_min(self) -> float:
        if self.ph_tolerance_min is not None:
            return self.ph_tolerance_min
        return self.ph_min - PH_TOLERANCE_MARGIN

    @property
    def ph_tol_max(self) -> float:
        if self.ph_tolerance_max is not None:
            return self.ph_tolerance_max
        return self.ph_max + PH_TOLERANCE_MARGIN

    @property
    def ec_mid_mscm(self) -> float:
        return 0.5 * (self.ec_min_mscm + self.ec_max_mscm)

    @property
    def ph_mid(self) -> float:
        return 0.5 * (self.ph_min + self.ph_max)

    @property
    def ppm500_mid(self) -> float:
        return 0.5 * (self.ppm500_min + self.ppm500_max)


@dataclass(frozen=True)
class BandResult:
    """Resolved numeric band for EC (mS/cm) or pH."""

    min_value: float
    max_value: float
    confidence: str  # "overlap" | "fallback_average"
    zero_width: bool = False
    unit: str = ""

    @property
    def mid(self) -> float:
        return 0.5 * (self.min_value + self.max_value)


@dataclass(frozen=True)
class ResolvedSetpoints:
    ec_mscm: BandResult
    # Optimum pH overlap. Often a single point (basil 5.5-6.0 with lettuce
    # 6.0-7.0 meet only at 6.0): that is the setpoint, not the control band.
    ph: BandResult
    # Firmware uses 500-scale ppm; derived from catalogue ppm500 fields
    # with the same two-tier logic (not mS/cm × 500).
    ppm500: BandResult
    plant_ids: tuple[str, ...]
    # pH the controller aims for (middle of the optimum overlap).
    ph_setpoint: float = 0.0
    # pH dead limits: the overlap of every plant's tolerance range. The
    # controller leaves pH alone inside it.
    ph_dead_low: float = 0.0
    ph_dead_high: float = 0.0
    # True when the tolerance overlap was narrower than the design minimum
    # (setpoint +/- PH_DEAD_MIN_HALF_WIDTH) and was widened to it.
    ph_dead_widened: bool = False

    @property
    def ec_dead_low(self) -> float:
        return max(0.0, self.ppm500.min_value - EC_DEAD_MARGIN_PPM)

    @property
    def ec_dead_high(self) -> float:
        return self.ppm500.max_value + EC_DEAD_MARGIN_PPM


@dataclass(frozen=True)
class Suggestion:
    plant: Plant
    distance: float


def _plant_from_dict(row: dict) -> Plant:
    return Plant(
        id=str(row["id"]),
        common_name=str(row["common_name"]),
        scientific_name=str(row.get("scientific_name") or ""),
        ec_min_mscm=float(row["ec_min_mscm"]),
        ec_max_mscm=float(row["ec_max_mscm"]),
        ppm500_min=float(row["ppm500_min"]),
        ppm500_max=float(row["ppm500_max"]),
        ph_min=float(row["ph_min"]),
        ph_max=float(row["ph_max"]),
        note=row.get("note"),
        ph_tolerance_min=(
            float(row["ph_tolerance_min"]) if row.get("ph_tolerance_min") is not None else None
        ),
        ph_tolerance_max=(
            float(row["ph_tolerance_max"]) if row.get("ph_tolerance_max") is not None else None
        ),
    )


def load_plants_from_json(path: Path | str | None = None) -> list[Plant]:
    """Read-only JSON catalogue loader (swap-point for a future SQLite table)."""
    catalogue_path = Path(path) if path else DEFAULT_CATALOGUE_PATH
    with catalogue_path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = payload["plants"] if isinstance(payload, dict) else payload
    return [_plant_from_dict(row) for row in rows]


def load_plants(path: Path | str | None = None) -> list[Plant]:
    """Public loader entry used by the dashboard. Change only this to use SQLite."""
    return load_plants_from_json(path)


def plants_by_id(plants: Sequence[Plant]) -> dict[str, Plant]:
    return {p.id: p for p in plants}


def _resolve_axis(
    plants: Sequence[Plant],
    min_attr: str,
    max_attr: str,
    unit: str,
) -> BandResult:
    if not plants:
        raise ValueError("at least one plant is required")

    lows = [getattr(p, min_attr) for p in plants]
    highs = [getattr(p, max_attr) for p in plants]
    lo = max(lows)
    hi = min(highs)
    if lo <= hi:
        return BandResult(
            min_value=lo,
            max_value=hi,
            confidence="overlap",
            zero_width=(lo == hi),
            unit=unit,
        )

    mids = [0.5 * (a + b) for a, b in zip(lows, highs)]
    mean_mid = sum(mids) / len(mids)
    return BandResult(
        min_value=mean_mid,
        max_value=mean_mid,
        confidence="fallback_average",
        zero_width=True,
        unit=unit,
    )


def _resolve_ph_dead_limits(plants: Sequence[Plant], setpoint: float) -> tuple[float, float, bool]:
    """Overlap of the plants' pH tolerance ranges, widened if needed so it
    holds the setpoint with at least PH_DEAD_MIN_HALF_WIDTH either side."""
    lo = max(p.ph_tol_min for p in plants)
    hi = min(p.ph_tol_max for p in plants)
    widened = False
    if lo > setpoint - PH_DEAD_MIN_HALF_WIDTH:
        lo = setpoint - PH_DEAD_MIN_HALF_WIDTH
        widened = True
    if hi < setpoint + PH_DEAD_MIN_HALF_WIDTH:
        hi = setpoint + PH_DEAD_MIN_HALF_WIDTH
        widened = True
    return round(lo, 3), round(hi, 3), widened


def resolve_setpoints(plants: Sequence[Plant]) -> ResolvedSetpoints:
    """Two-tier EC / pH / ppm500 bands for a multi-plant reservoir, plus the
    pH setpoint and dead limits the controller uses."""
    selected = list(plants)
    ph = _resolve_axis(selected, "ph_min", "ph_max", "pH")
    setpoint = round(ph.mid, 3)
    dead_lo, dead_hi, widened = _resolve_ph_dead_limits(selected, setpoint)
    return ResolvedSetpoints(
        ec_mscm=_resolve_axis(selected, "ec_min_mscm", "ec_max_mscm", "mS/cm"),
        ph=ph,
        ppm500=_resolve_axis(selected, "ppm500_min", "ppm500_max", "ppm500"),
        plant_ids=tuple(p.id for p in selected),
        ph_setpoint=setpoint,
        ph_dead_low=dead_lo,
        ph_dead_high=dead_hi,
        ph_dead_widened=widened,
    )


def resolve_setpoints_by_ids(
    selected_ids: Sequence[str],
    catalogue: Sequence[Plant],
) -> ResolvedSetpoints:
    index = plants_by_id(catalogue)
    missing = [pid for pid in selected_ids if pid not in index]
    if missing:
        raise KeyError(f"unknown plant id(s): {', '.join(missing)}")
    if not selected_ids:
        raise ValueError("select at least one plant")
    return resolve_setpoints([index[pid] for pid in selected_ids])


def _ranges_overlap(a_min: float, a_max: float, b_min: float, b_max: float) -> bool:
    return max(a_min, b_min) <= min(a_max, b_max)


def suggest_compatible(
    selected: Plant,
    catalogue: Sequence[Plant],
    limit: int = 3,
) -> list[Suggestion]:
    """Top-N catalogue plants compatible with `selected` (EC and pH overlap)."""
    ranked: list[Suggestion] = []
    for candidate in catalogue:
        if candidate.id == selected.id:
            continue
        if not _ranges_overlap(
            selected.ec_min_mscm,
            selected.ec_max_mscm,
            candidate.ec_min_mscm,
            candidate.ec_max_mscm,
        ):
            continue
        if not _ranges_overlap(
            selected.ph_min,
            selected.ph_max,
            candidate.ph_min,
            candidate.ph_max,
        ):
            continue
        dist = math.sqrt(
            (candidate.ec_mid_mscm - selected.ec_mid_mscm) ** 2
            + (candidate.ph_mid - selected.ph_mid) ** 2
        )
        ranked.append(Suggestion(plant=candidate, distance=dist))
    ranked.sort(key=lambda s: (s.distance, s.plant.common_name.lower()))
    return ranked[:limit]


def suggest_compatible_for_selection(
    selected_ids: Sequence[str],
    catalogue: Sequence[Plant],
    limit: int = 3,
) -> tuple[Plant | None, list[Suggestion]]:
    """Suggestions anchored on the first selected plant (multi-select UI)."""
    if not selected_ids:
        return None, []
    index = plants_by_id(catalogue)
    anchor = index.get(selected_ids[0])
    if anchor is None:
        return None, []
    return anchor, suggest_compatible(anchor, catalogue, limit=limit)


def mqtt_setpoints_payload(resolved: ResolvedSetpoints, stage: str = "vegetative") -> dict:
    """Build the ESP32 cmd payload (ppm500 + pH only — no catalogue ids).

    Firmware 1.9+ reads the dead limits and pH setpoint; older firmware
    ignores them and derives its own from the target bands.
    """
    return {
        "cmd": "setpoints",
        "stage": stage,
        "ec_min": round(resolved.ppm500.min_value, 1),
        "ec_max": round(resolved.ppm500.max_value, 1),
        "ph_min": round(resolved.ph.min_value, 3),
        "ph_max": round(resolved.ph.max_value, 3),
        "ec_dead_low": round(resolved.ec_dead_low, 1),
        "ec_dead_high": round(resolved.ec_dead_high, 1),
        "ph_setpoint": resolved.ph_setpoint,
        "ph_dead_low": resolved.ph_dead_low,
        "ph_dead_high": resolved.ph_dead_high,
    }


def format_ph_target(resolved: ResolvedSetpoints) -> str:
    """'pH 6.0 (5.5–6.5)': setpoint, then the range the controller leaves alone."""
    return (
        f"pH {resolved.ph_setpoint:.1f} "
        f"({fmt_range(resolved.ph_dead_low, resolved.ph_dead_high, 1)})"
    )
