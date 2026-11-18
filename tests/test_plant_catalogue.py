"""Unit checks for plant catalogue resolution + transitional EC scaling."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config import (  # noqa: E402
    TRANSITIONAL_STRENGTH_FRACTION,
    scale_tds_band_for_stage,
    suggested_grow_stage,
)
from plant_catalogue import (  # noqa: E402
    format_ph_target,
    load_plants,
    mqtt_setpoints_payload,
    resolve_setpoints_by_ids,
    suggest_compatible,
    plants_by_id,
)


def main() -> None:
    plants = load_plants()
    by_id = plants_by_id(plants)

    duo = resolve_setpoints_by_ids(["basil", "lettuce"], plants)
    assert duo.ec_mscm.confidence == "overlap", duo.ec_mscm
    assert abs(duo.ec_mscm.min_value - 1.2) < 1e-9
    assert abs(duo.ec_mscm.max_value - 1.6) < 1e-9
    assert duo.ph.confidence == "overlap", duo.ph
    assert abs(duo.ph.min_value - 6.0) < 1e-9
    assert abs(duo.ph.max_value - 6.0) < 1e-9
    assert duo.ph.zero_width is True
    assert abs(duo.ppm500.min_value - 600) < 1e-9
    assert abs(duo.ppm500.max_value - 800) < 1e-9

    # The single-point optimum overlap is the setpoint; the controller's dead
    # limits are the overlap of the tolerance ranges (basil 5.0-6.5, lettuce
    # 5.5-7.5), not setpoint +/- 0.1.
    assert duo.ph_setpoint == 6.0
    assert (duo.ph_dead_low, duo.ph_dead_high) == (5.5, 6.5)
    assert duo.ph_dead_widened is False
    assert (duo.ec_dead_low, duo.ec_dead_high) == (550.0, 850.0)
    payload = mqtt_setpoints_payload(duo)
    assert payload["ph_setpoint"] == 6.0
    assert (payload["ph_dead_low"], payload["ph_dead_high"]) == (5.5, 6.5)
    assert (payload["ec_dead_low"], payload["ec_dead_high"]) == (550.0, 850.0)
    assert format_ph_target(duo) == "pH 6.0 (5.5–6.5)"

    # A narrow tolerance overlap is widened to setpoint +/- 0.3.
    pak = resolve_setpoints_by_ids(["pak_choi"], plants)
    assert pak.ph_dead_low <= pak.ph_setpoint - 0.3 + 1e-9
    assert pak.ph_dead_high >= pak.ph_setpoint + 0.3 - 1e-9

    tr_lo, tr_hi = scale_tds_band_for_stage("transitional", 650.0, 800.0)
    assert abs(tr_lo - 325.0) < 1e-9 and abs(tr_hi - 400.0) < 1e-9
    assert TRANSITIONAL_STRENGTH_FRACTION == 0.5

    t_lo, t_hi = scale_tds_band_for_stage(
        "transitional", duo.ppm500.min_value, duo.ppm500.max_value
    )
    assert abs(t_lo - 300.0) < 1e-9 and abs(t_hi - 400.0) < 1e-9
    assert duo.ph.min_value == duo.ph.max_value == 6.0

    basil = resolve_setpoints_by_ids(["basil"], plants)
    b_lo, b_hi = scale_tds_band_for_stage(
        "transitional", basil.ppm500.min_value, basil.ppm500.max_value
    )
    assert (b_lo, b_hi) != (t_lo, t_hi)
    assert abs(b_lo - 250.0) < 1e-9 and abs(b_hi - 400.0) < 1e-9

    suggestions = suggest_compatible(by_id["basil"], plants, limit=3)
    assert suggestions, "expected at least one suggestion for basil"
    assert suggestions[0].plant.id == "sage", [s.plant.id for s in suggestions]

    assert suggested_grow_stage(1) == "seedling"
    assert suggested_grow_stage(2) == "transitional"
    assert suggested_grow_stage(3) == "transitional"
    assert suggested_grow_stage(4) == "vegetative"

    print("OK basil+lettuce EC [1.2, 1.6] overlap; pH 6.0 zero-width overlap")
    print("OK transitional 650-800 -> 325-400; basil/lettuce veg 600-800 -> 300-400")
    print("OK selection change updates transitional band")
    print("OK basil suggestions:", [(s.plant.id, round(s.distance, 4)) for s in suggestions])
    print("OK suggested stages week 1/2/3/4")


if __name__ == "__main__":
    main()
