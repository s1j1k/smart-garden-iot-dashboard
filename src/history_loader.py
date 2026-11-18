"""Load dashboard charts from stored history, without a live ESP32.

Local SQLite is the MQTT log. InfluxDB Cloud is the ESP32 backup and is used
for the same lookback window when credentials are available. A missing node
does not block either load.
"""

from __future__ import annotations

import csv
import io
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from sensor_ingest import parse_sim_time
from sqlite_store import load_recent_sensor_readings

@dataclass
class HistoryRestore:
    records: list[dict[str, Any]]
    latest: Optional[dict[str, Any]]
    note: str


def _since_iso(hours: int) -> str:
    from config import now_utc

    moment = now_utc() - timedelta(hours=max(1, hours))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _flag(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "on", "yes"} else 0
    return 1 if bool(value) else 0


def reading_to_history_record(reading: Mapping[str, Any]) -> dict[str, Any]:
    ph = reading.get("ph_filtered")
    if ph is None:
        ph = reading.get("ph")
    stamp = reading.get("timestamp") or reading.get("received_at")
    return {
        "time": parse_sim_time(stamp),
        "mode": "stored",
        "grow_stage": reading.get("grow_stage") or "",
        "hydroponic_mode": reading.get("hydroponic_mode") or "",
        "ph_settle_s": reading.get("ph_settle_s"),
        "ambient_temp_c": reading.get("ambient_temp_c"),
        "ambient_humidity_pct": reading.get("ambient_humidity_pct"),
        "water_temp_c": reading.get("water_temp_c"),
        "light_adc": reading.get("light_adc"),
        "water_level_pct": reading.get("water_level_pct"),
        "water_target_pct": reading.get("water_target_pct"),
        "tds_ppm": reading.get("tds_ppm"),
        "ph": ph,
        "ph_raw": reading.get("ph_raw"),
        "ph_filtered": reading.get("ph_filtered"),
        "ph_voltage": reading.get("ph_voltage"),
        "co2_ppm": reading.get("co2_ppm"),
        "water_pump_on": _flag(reading.get("water_pump_on")),
        "drain_pump_on": _flag(reading.get("drain_pump_on")),
        "nutrient_pump_on": _flag(reading.get("nutrient_pump_on")),
        "ph_up_pump_on": _flag(reading.get("ph_up_pump_on")),
        "ph_down_pump_on": _flag(reading.get("ph_down_pump_on")),
        "led_on": _flag(reading.get("led_on")),
        "fan_on": _flag(reading.get("fan_on")),
        "ph_dose": reading.get("ph_dose"),
        "nutrient_dose": reading.get("nutrient_dose"),
        "water_rem_ml": reading.get("water_rem_ml"),
        "nutrient_rem_ml": reading.get("nutrient_rem_ml"),
        "ph_up_rem_ml": reading.get("ph_up_rem_ml"),
        "ph_down_rem_ml": reading.get("ph_down_rem_ml"),
        "stock_alert": reading.get("stock_alert"),
    }


def _influx_secrets() -> Optional[dict[str, str]]:
    keys = ("INFLUX_URL", "INFLUX_ORG", "INFLUX_BUCKET", "INFLUX_TOKEN")
    from_env = {key: (os.getenv(key) or "").strip() for key in keys}
    if all(from_env.values()):
        return from_env

    candidates = [
        Path(__file__).resolve().parents[2] / "embedded-smart-garden" / "secrets.h",
        Path(__file__).resolve().parents[1] / "secrets.h",
    ]
    text = ""
    for path in candidates:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            break
    if not text:
        return None

    found: dict[str, str] = {}
    for key in keys:
        match = re.search(rf'{key}\s*=\s*"([^"]*)"', text)
        if match and match.group(1) and not match.group(1).startswith("YOUR_"):
            found[key] = match.group(1)
    if len(found) != len(keys):
        return None
    return found


def _parse_annotated_csv(body: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    header: Optional[list[str]] = None
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = next(csv.reader(io.StringIO(line)))
        if not parts:
            continue
        if parts[0] == "":
            parts = parts[1:]
        if "_time" in parts and "result" in parts:
            header = parts
            continue
        if header is None:
            continue
        if len(parts) < len(header):
            parts = parts + [""] * (len(header) - len(parts))
        row = dict(zip(header, parts))
        if row.get("_time"):
            rows.append(row)
    return rows


def _num(row: Mapping[str, str], key: str) -> Optional[float]:
    raw = row.get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _influx_to_reading(row: Mapping[str, str]) -> dict[str, Any]:
    def flag(key: str) -> Optional[bool]:
        value = _num(row, key)
        if value is None:
            return None
        return bool(int(value))

    ph = _num(row, "ph")
    return {
        "timestamp": row.get("_time") or "",
        "device_id": row.get("device") or "garden-node-01",
        "grow_stage": row.get("stage") or None,
        "source": "influx",
        "ph": ph,
        "ph_raw": _num(row, "ph_raw"),
        "ph_filtered": _num(row, "ph_filtered") if _num(row, "ph_filtered") is not None else ph,
        "ph_voltage": _num(row, "ph_voltage"),
        "tds_ppm": _num(row, "tds_ppm"),
        "water_temp_c": _num(row, "water_temp_c"),
        "water_level_pct": _num(row, "water_level_pct"),
        "ambient_temp_c": _num(row, "ambient_temp_c"),
        "ambient_humidity_pct": _num(row, "ambient_humidity_pct"),
        "light_adc": _num(row, "light_adc"),
        "co2_ppm": _num(row, "co2_ppm"),
        "water_pump_on": flag("water_pump_on"),
        "drain_pump_on": flag("drain_pump_on"),
        "nutrient_pump_on": flag("nutrient_pump_on"),
        "ph_up_pump_on": flag("ph_up_pump_on"),
        "ph_down_pump_on": flag("ph_down_pump_on"),
        "ph_dose": row.get("ph_dose_state") or None,
        "led_on": flag("grow_light_on"),
    }


def load_influx_readings(since_iso: str, *, timeout_s: float = 20.0) -> tuple[list[dict[str, Any]], str]:
    secrets = _influx_secrets()
    if secrets is None:
        return [], "Influx credentials not configured"
    bucket = secrets["INFLUX_BUCKET"]
    flux = f'''
from(bucket: "{bucket}")
  |> range(start: {since_iso})
  |> filter(fn: (r) => r._measurement == "sensor_reading")
  |> filter(fn: (r) => r.device == "garden-node-01")
  |> filter(fn: (r) =>
      r._field == "ph" or r._field == "ph_raw" or r._field == "ph_filtered" or
      r._field == "ph_voltage" or r._field == "tds_ppm" or r._field == "water_temp_c" or
      r._field == "water_level_pct" or r._field == "ambient_temp_c" or
      r._field == "ambient_humidity_pct" or r._field == "light_adc" or
      r._field == "co2_ppm" or r._field == "ph_dose_state" or
      r._field == "water_pump_on" or r._field == "drain_pump_on" or
      r._field == "nutrient_pump_on" or r._field == "ph_up_pump_on" or
      r._field == "ph_down_pump_on" or r._field == "grow_light_on")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
    url = secrets["INFLUX_URL"].rstrip("/")
    req = urllib.request.Request(
        f"{url}/api/v2/query?org={secrets['INFLUX_ORG']}",
        data=flux.encode("utf-8"),
        headers={
            "Authorization": f"Token {secrets['INFLUX_TOKEN']}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:180]
        return [], f"Influx query failed HTTP {exc.code}: {detail}"
    except Exception as exc:
        return [], f"Influx query failed: {exc}"
    if "error" in body[:200].lower() and "_time" not in body:
        return [], "Influx query returned an error"
    readings = [_influx_to_reading(row) for row in _parse_annotated_csv(body)]
    return readings, ""


def _span(readings: list[Mapping[str, Any]]) -> str:
    stamps = [str(item.get("timestamp") or "") for item in readings if item.get("timestamp")]
    if not stamps:
        return ""
    return f"{stamps[0]} to {stamps[-1]}"


def sqlite_covers(rows: list[Mapping[str, Any]], since_iso: str, limit: int,
                  slack: timedelta = timedelta(hours=1)) -> bool:
    """True when SQLite already holds the history window: it filled the row
    limit, or its oldest row is within ``slack`` of the window's start."""
    if not rows:
        return False
    if len(rows) >= limit:
        return True
    stamps = sorted(str(r.get("timestamp") or "") for r in rows
                    if r.get("timestamp") and not str(r.get("timestamp")).startswith("millis:"))
    if not stamps:
        return False
    try:
        oldest = datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))
        start = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return oldest <= start + slack


def restore_history(config: Mapping[str, Any]) -> HistoryRestore:
    hours = int(config.get("history_lookback_hours") or 72)
    limit = int(config.get("history_max_rows") or 4000)
    since = _since_iso(hours)
    sqlite_path = config.get("sqlite_path") or ""

    sqlite_rows = load_recent_sensor_readings(sqlite_path, since_iso=since, limit=limit)
    sqlite_note = f"{len(sqlite_rows)} SQLite rows"
    use_influx = str(config.get("history_from_influx") or os.getenv("HISTORY_FROM_INFLUX", "")
                     ).strip().lower() in {"1", "true", "yes", "on"}
    if not use_influx or sqlite_covers(sqlite_rows, since, limit):
        # The Pi's SQLite is the record. InfluxDB Cloud was the store before the
        # Pi was set up; asking it costs every new viewer an internet round trip
        # (up to 20 s), so it is only read when HISTORY_FROM_INFLUX is set and
        # SQLite is missing part of the window.
        influx_rows, influx_note = [], "InfluxDB not used"
    else:
        influx_rows, influx_error = load_influx_readings(since)
        influx_note = influx_error or f"{len(influx_rows)} Influx rows"

    if not sqlite_rows and not influx_rows:
        sqlite_rows = load_recent_sensor_readings(sqlite_path, since_iso=None, limit=min(limit, 800))
        if sqlite_rows:
            sqlite_note = f"{len(sqlite_rows)} older SQLite rows (none in the last {hours} h)"

    merged: dict[str, dict[str, Any]] = {}
    for reading in influx_rows:
        stamp = str(reading.get("timestamp") or "")
        if stamp:
            merged[stamp] = reading
    for reading in sqlite_rows:
        stamp = str(reading.get("timestamp") or "")
        if stamp:
            merged[stamp] = reading

    ordered = [merged[key] for key in sorted(merged)]
    if len(ordered) > limit:
        ordered = ordered[-limit:]

    records = [reading_to_history_record(reading) for reading in ordered]
    latest = ordered[-1] if ordered else None
    parts = [f"Stored history ({hours} h lookback): {sqlite_note}; {influx_note}."]
    span = _span(ordered)
    if span:
        parts.append(f"Span {span}.")
    if not ordered:
        parts.append("No stored readings to chart.")
    parts.append("Live ESP32 data is separate and is not required to show this history.")
    return HistoryRestore(records=records, latest=latest, note=" ".join(parts))
