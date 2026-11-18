"""Home-page reading tiles: value, target band and an in-range status per measurement."""

from __future__ import annotations

import html

from growth_targets import climate_band, climate_stage, ph_band, tds_band
from fmt_range import fmt_range

STATUS = {
    "ok": ("#2da44e", "✓", "In range"),
    "low": ("#bf8700", "↓", "Low"),
    "high": ("#bf8700", "↑", "High"),
    "fault": ("#d1242f", "⚠", "Fault"),
    "none": ("#8c959f", "·", "No reading"),
    "info": ("#8c959f", "", ""),
}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status(value, band, faulty=False):
    if faulty:
        return "fault"
    if value is None:
        return "none"
    if not band:
        return "info"
    lo, hi = band
    if value < lo:
        return "low"
    if value > hi:
        return "high"
    return "ok"


def _target(band, digits, unit, word="Target"):
    if not band:
        return ""
    lo, hi = band
    return f"{word} {fmt_range(lo, hi, digits, unit)}"


def reading_tiles(state, actual_stage, suggested_stage):
    health = state.get("health") or {}
    stage = climate_stage(actual_stage, suggested_stage)
    growing = actual_stage not in (None, "idle", "sensor_baseline", "filling")

    def tile(key, label, field, unit, digits, band, sensor, word="Target"):
        value = _num(state.get(field))
        faulty = health.get(sensor) == "fault"
        return {"key": key, "label": label, "unit": unit,
                "value": "—" if value is None else f"{value:.{digits}f}",
                "target": _target(band, digits if digits else 0, unit.strip() and f" {unit}", word),
                "status": _status(value, band, faulty)}

    ph_value = "ph_ema" if state.get("ph_ema") is not None else "ph"
    tds_value = "tds_ppm_ema" if state.get("tds_ppm_ema") is not None else "tds_ppm"
    tds_target = tds_band(state, actual_stage) if actual_stage in (
        "transitional", "vegetative", "harvest") else None
    tiles = [
        # pH and TDS tiles show the dead limits (no dosing inside), not the target band.
        tile("ph", "🧪 pH", ph_value, "", 2, ph_band(state, actual_stage) if growing else None,
             "ph", "Acceptable"),
        tile("tds", "🌿 Nutrient (TDS)", tds_value, "ppm", 0, tds_target, "tds", "Acceptable"),
        tile("water_level", "💧 Water level", "water_level_pct", "%", 0, None, "water_level"),
        tile("water_temp", "🌡️ Water temp", "water_temp_c", "°C", 1,
             climate_band(stage, "water_temp_c"), "water_temp"),
        tile("air_temp", "🌤️ Air temp", "ambient_temp_c", "°C", 1,
             climate_band(stage, "air_temp_c"), "ambient"),
        tile("humidity", "💦 Humidity", "ambient_humidity_pct", "%", 0,
             climate_band(stage, "humidity_pct"), "ambient"),
        tile("co2", "💨 CO₂", "co2_ppm", "ppm", 0, climate_band(stage, "co2_ppm"), "co2"),
    ]
    if state.get("water_level_init_required"):
        tiles[2].update(value="Init", status="fault", target="Lift and lower the stick")
    elif _num(state.get("water_target_pct")) is not None:
        tiles[2]["target"] = f"Target about {_num(state['water_target_pct']):.0f} %"
    return tiles


TILE_CSS = """
<style>
.sg-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.sg-tile{border:1px solid rgba(128,128,128,.25);border-left:4px solid var(--c);border-radius:10px;
  padding:10px 12px;min-height:96px}
.sg-tile .l{font-size:.82rem;opacity:.8}
.sg-tile .v{font-size:1.6rem;font-weight:700;line-height:1.2;margin:2px 0}
.sg-tile .v small{font-size:.85rem;font-weight:500;opacity:.75;margin-left:3px}
.sg-tile .s{font-size:.78rem;color:var(--c);font-weight:600}
.sg-tile .t{font-size:.75rem;opacity:.7}
</style>
"""


def tiles_html(tiles):
    cells = []
    for tile in tiles:
        color, icon, label = STATUS[tile["status"]]
        unit = f"<small>{html.escape(tile['unit'])}</small>" if tile["unit"] and tile["value"] != "—" \
            else ""
        status = f'<div class="s">{icon} {label}</div>' if label else '<div class="s">&nbsp;</div>'
        cells.append(
            f'<div class="sg-tile" style="--c:{color}">'
            f'<div class="l">{html.escape(tile["label"])}</div>'
            f'<div class="v">{html.escape(tile["value"])}{unit}</div>'
            f'{status}<div class="t">{html.escape(tile["target"])}</div></div>')
    return TILE_CSS + '<div class="sg-tiles">' + "".join(cells) + "</div>"
