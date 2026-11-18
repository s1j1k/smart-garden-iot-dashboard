"""Local SQLite persistence for MQTT telemetry.

Stores:
  - mqtt_messages: every raw broker payload (full JSON)
  - sensor_readings: normalized rows (same columns as CSV / LOG_FIELDS)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

_lock = threading.Lock()
_initialized: set[str] = set()

# Keep in sync with sensor_ingest.LOG_FIELDS (minus auto id).
SENSOR_READING_COLUMNS = [
    "received_at",
    "source",
    "device_id",
    "timestamp",
    "kind",
    "boot_id",
    "seq",
    "uptime_ms",
    "reading_key",
    "firmware_version",
    "replay",
    "grow_stage",
    "system_mode",
    "stage_persisted",
    "control_idle",
    "hydroponic_mode",
    "ph_settle_s",
    "control_law",
    "pumps_calibrated",
    "water_target_pct",
    "water_max_pct",
    "target_drawdown_mm",
    "current_drawdown_mm",
    "tank_volume_l",
    "drawdown_tolerance_mm",
    "fill_0_pct",
    "level_check_s",
    "ph_check_s",
    "ec_check_s",
    "ec_control_enabled",
    "ph_control_enabled",
    "level_control_enabled",
    "nutrient_tds_min",
    "nutrient_tds_max",
    "ec_dead_low",
    "ec_dead_high",
    "ec_min_dose_ml",
    "ec_max_dose_ml",
    "ec_settle_s",
    "ph_target_min",
    "ph_target_max",
    "ph_dead_low",
    "ph_dead_high",
    "ph_center",
    "ph_min_dose_ml",
    "ph_max_dose_ml",
    "ph_session_cap_ml",
    "ph_baseline_recheck_s",
    "ph_dose",
    "nutrient_dose",
    "ph_voltage",
    "ph_raw",
    "ph_filtered",
    "ph_slope",
    "ph_offset",
    "ph_probe_calibrated",
    "ph_cal_active",
    "ph_cal_state",
    "ph_cal_message",
    "ph_cal_ok",
    "ph_cal_acid_v",
    "ph_cal_alkali_v",
    "ph_cal_stored_acid_v",
    "ph_cal_stored_alkali_v",
    "water_calibrated",
    "nutrient_calibrated",
    "ph_up_calibrated",
    "ph_down_calibrated",
    "water_ml_per_s",
    "nutrient_ml_per_burst",
    "ph_up_ml_per_burst",
    "ph_down_ml_per_burst",
    "pump_cal_active",
    "pump_cal_state",
    "pump_cal_pump",
    "pump_cal_message",
    "pump_cal_ok",
    "pump_cal_mode",
    "pump_cal_run",
    "pump_cal_runs_needed",
    "pump_cal_bursts",
    "pump_cal_pulse_ms",
    "pump_cal_target_bursts",
    "pump_cal_target_ms",
    "pump_cal_last_sample",
    "pump_cal_unit",
    "water_cap_ml",
    "water_rem_ml",
    "water_empty",
    "nutrient_cap_ml",
    "nutrient_rem_ml",
    "nutrient_empty",
    "ph_up_cap_ml",
    "ph_up_rem_ml",
    "ph_up_empty",
    "ph_down_cap_ml",
    "ph_down_rem_ml",
    "ph_down_empty",
    "stock_alert",
    "ambient_temp_c",
    "ambient_humidity_pct",
    "water_temp_c",
    "light_adc",
    "water_level_pct",
    "tds_ppm",
    "ph",
    "co2_ppm",
    "water_pump_on",
    "drain_pump_on",
    "nutrient_pump_on",
    "ph_up_pump_on",
    "ph_down_pump_on",
    "led_on",
    "fan_on",
]

BOOLEANISH = {
    "replay",
    "stage_persisted",
    "control_idle",
    "ec_control_enabled",
    "ph_control_enabled",
    "level_control_enabled",
    "ph_probe_calibrated",
    "ph_cal_active",
    "ph_cal_ok",
    "water_calibrated",
    "nutrient_calibrated",
    "ph_up_calibrated",
    "ph_down_calibrated",
    "pump_cal_active",
    "pump_cal_ok",
    "water_empty",
    "nutrient_empty",
    "ph_up_empty",
    "ph_down_empty",
    "water_pump_on",
    "drain_pump_on",
    "nutrient_pump_on",
    "ph_up_pump_on",
    "ph_down_pump_on",
    "led_on",
    "fan_on",
}


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _column_sql_type(name: str) -> str:
    if name in BOOLEANISH:
        return "INTEGER"
    if name in {"received_at", "source", "device_id", "timestamp", "grow_stage",
                "system_mode", "hydroponic_mode", "control_law", "ph_dose",
                "nutrient_dose", "ph_cal_state", "ph_cal_message", "pump_cal_state",
                "pump_cal_pump", "pump_cal_message", "pump_cal_mode", "pump_cal_unit",
                "stock_alert", "firmware_version", "kind", "reading_key"}:
        return "TEXT"
    if name in {"boot_id", "seq", "uptime_ms"}:
        return "INTEGER"
    return "REAL"


def ensure_schema(sqlite_path: str | Path) -> Path:
    path = Path(sqlite_path)
    key = str(path.resolve())
    with _lock:
        if key in _initialized and path.exists():
            return path
        conn = _connect(path)
        try:
            cols = ",\n  ".join(
                f"{name} {_column_sql_type(name)}" for name in SENSOR_READING_COLUMNS
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS sensor_readings (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  {cols}
                );
                """
            )
            # Migrate: add any new columns from SENSOR_READING_COLUMNS.
            existing = {
                row[1]
                for row in conn.execute("PRAGMA table_info(sensor_readings)").fetchall()
            }
            for name in SENSOR_READING_COLUMNS:
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE sensor_readings ADD COLUMN {name} {_column_sql_type(name)}"
                    )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mqtt_messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  received_at TEXT NOT NULL,
                  topic TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  device_id TEXT,
                  parse_ok INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            mqtt_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(mqtt_messages)").fetchall()
            }
            if "msg_key" not in mqtt_cols:
                conn.execute("ALTER TABLE mqtt_messages ADD COLUMN msg_key TEXT")
            # One row per ESP32 reading / event (firmware 1.8.0+). Rows from
            # older firmware have no key and are not constrained.
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_sensor_readings_reading_key
                  ON sensor_readings (reading_key) WHERE reading_key IS NOT NULL;
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_mqtt_messages_msg_key
                  ON mqtt_messages (msg_key) WHERE msg_key IS NOT NULL;
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sensor_readings_device_time
                  ON sensor_readings (device_id, timestamp);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sensor_readings_received_at
                  ON sensor_readings (received_at);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mqtt_messages_received_at
                  ON mqtt_messages (received_at);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mqtt_messages_topic
                  ON mqtt_messages (topic);
                """
            )
            conn.commit()
        finally:
            conn.close()
        _initialized.add(key)
    return path


def _sql_value(column: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if column in BOOLEANISH:
        if isinstance(value, str):
            return 1 if value.strip().lower() in {"1", "true", "on", "yes"} else 0
        return 1 if bool(value) else 0
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def insert_sensor_reading(sqlite_path: str | Path, reading: Mapping[str, Any]) -> bool:
    """Insert one normalized row. False when its reading_key is already stored."""
    path = ensure_schema(sqlite_path)
    values = [_sql_value(col, reading.get(col)) for col in SENSOR_READING_COLUMNS]
    placeholders = ", ".join("?" for _ in SENSOR_READING_COLUMNS)
    cols = ", ".join(SENSOR_READING_COLUMNS)
    with _lock:
        conn = _connect(path)
        try:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO sensor_readings ({cols}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()


def insert_mqtt_message(
    sqlite_path: str | Path,
    *,
    received_at: str,
    topic: str,
    payload_json: str,
    device_id: Optional[str] = None,
    parse_ok: bool = False,
    msg_key: Optional[str] = None,
) -> bool:
    """Store one raw payload. False when msg_key is already stored (a repeat)."""
    path = ensure_schema(sqlite_path)
    with _lock:
        conn = _connect(path)
        try:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO mqtt_messages
                  (received_at, topic, payload_json, device_id, parse_ok, msg_key)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (received_at, topic, payload_json, device_id, 1 if parse_ok else 0, msg_key),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()


def load_recent_sensor_readings(
    sqlite_path: str | Path,
    *,
    since_iso: Optional[str] = None,
    limit: int = 4000,
) -> list[dict[str, Any]]:
    """Newest normalized rows, oldest first. Empty if the file does not exist yet."""
    path = Path(sqlite_path)
    if not path.exists():
        return []
    path = ensure_schema(path)
    limit = max(1, int(limit))
    with _lock:
        conn = _connect(path)
        conn.row_factory = sqlite3.Row
        try:
            if since_iso:
                cursor = conn.execute(
                    """
                    SELECT * FROM (
                      SELECT * FROM sensor_readings
                      WHERE COALESCE(NULLIF(timestamp, ''), received_at) >= ?
                      ORDER BY id DESC
                      LIMIT ?
                    )
                    ORDER BY id ASC
                    """,
                    (since_iso, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM (
                      SELECT * FROM sensor_readings
                      ORDER BY id DESC
                      LIMIT ?
                    )
                    ORDER BY id ASC
                    """,
                    (limit,),
                )
            rows = []
            for row in cursor.fetchall():
                item = {key: row[key] for key in row.keys() if key != "id"}
                for column in BOOLEANISH:
                    if item.get(column) is not None:
                        item[column] = bool(item[column])
                if not item.get("timestamp"):
                    item["timestamp"] = item.get("received_at")
                rows.append(item)
            return rows
        finally:
            conn.close()


def device_id_from_topic(topic: str) -> Optional[str]:
    # smart-garden/<device_id>/telemetry or .../cmd
    parts = topic.split("/")
    if len(parts) >= 2 and parts[0] == "smart-garden":
        return parts[1]
    return None


def persist_mqtt_payload(
    sqlite_path: str | Path,
    *,
    topic: str,
    payload_bytes: bytes,
    reading: Optional[Mapping[str, Any]] = None,
    received_at: Optional[str] = None,
) -> bool:
    """Write raw MQTT bytes, and the normalized row when parsing succeeded.

    Returns False when the message repeats one already stored (same device,
    boot_id and seq, e.g. an outbox replay of a row that did arrive); then
    nothing is written.
    """
    from datetime import datetime, timezone

    ts = received_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        text = payload_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = json.dumps({"_raw_b64": payload_bytes.hex()}, separators=(",", ":"))

    device_id = None
    if reading is not None:
        device_id = reading.get("device_id")
    if not device_id:
        device_id = device_id_from_topic(topic)

    msg_key = reading.get("reading_key") if reading is not None else None
    if msg_key is None:
        try:
            from sensor_ingest import reading_key

            msg_key = reading_key(json.loads(text))
        except (ValueError, TypeError):
            msg_key = None
    if msg_key is not None:
        # Topic leaf keeps a telemetry key distinct from an alerts/ack one.
        msg_key = f"{topic.rsplit('/', 1)[-1]}:{msg_key}"

    is_new = insert_mqtt_message(
        sqlite_path,
        received_at=ts,
        topic=topic,
        payload_json=text,
        device_id=device_id,
        parse_ok=reading is not None,
        msg_key=msg_key,
    )
    if is_new and reading is not None:
        insert_sensor_reading(sqlite_path, reading)
    return is_new


def first_id_since(conn: sqlite3.Connection, since_iso: str) -> int:
    """Smallest sensor_readings id received at or after ``since_iso`` (UTC ISO).

    Ids grow with arrival time, so a binary search over a few rowid lookups finds
    the start of a time window without scanning the table (received_at has an
    index on new stores, but not on the Pi's 3 GB one).
    """
    low, high = conn.execute("SELECT MIN(id), MAX(id) FROM sensor_readings").fetchone()
    if low is None:
        return 0
    high += 1                      # past the end: every row is older than the window
    since = str(since_iso)[:19]
    while low < high:
        mid = (low + high) // 2
        row = conn.execute("SELECT id, received_at FROM sensor_readings WHERE id >= ? "
                           "ORDER BY id LIMIT 1", (mid,)).fetchone()
        if row is None or str(row[1])[:19] >= since:
            high = mid
        else:
            low = row[0] + 1
    return int(low)


# path -> (last id scanned, last stage seen, changes so far): a page refresh only
# reads rows that arrived since, instead of the whole table.
_stage_cache: dict[str, tuple[int, str | None, list[tuple[str, str]]]] = {}


def load_stage_changes(sqlite_path: str | Path) -> list[tuple[str, str]]:
    """[(timestamp, grow_stage)] for each reading where the node's stage changed."""
    path = Path(sqlite_path)
    if not path.exists():
        return []
    key = str(path.resolve())
    last_id, last_stage, changes = _stage_cache.get(key, (0, None, []))
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT id, COALESCE(NULLIF(timestamp, ''), received_at), LOWER(TRIM(grow_stage))
            FROM sensor_readings
            WHERE id > ? AND grow_stage IS NOT NULL AND TRIM(grow_stage) <> ''
            ORDER BY id
            """,
            (last_id,),
        ).fetchall()
    except sqlite3.Error:
        return list(changes)
    finally:
        conn.close()
    changes = list(changes)
    for row_id, ts, stage in rows:
        last_id = row_id
        if str(ts).startswith("millis:"):
            continue
        if stage != last_stage and ts:
            changes.append((str(ts), str(stage)))
            last_stage = stage
    _stage_cache[key] = (last_id, last_stage, changes)
    return list(changes)


def load_uptake_readings(sqlite_path: str | Path, since_iso: str) -> list[dict[str, Any]]:
    """Per-minute tank volume, TDS, top-up stock and pump flags since ``since_iso``,
    oldest first: the inputs to plant_growth.uptake_features. Averaged per minute in
    SQL so weeks of 10 s readings stay small. Read-only on its own connection (WAL),
    so it does not hold up the rest of the dashboard."""
    path = Path(sqlite_path)
    if not path.exists():
        return []
    conn = _connect(path)
    try:
        start = first_id_since(conn, since_iso)
        rows = conn.execute(
            """
            SELECT MIN(ts), AVG(tank_volume_l), AVG(tds_ppm), AVG(water_rem_ml),
                   MAX(nutrient_pump_on), MAX(drain_pump_on)
            FROM (
                SELECT COALESCE(NULLIF(timestamp, ''), received_at) AS ts, tank_volume_l,
                       tds_ppm, water_rem_ml, nutrient_pump_on, drain_pump_on
                FROM sensor_readings
                WHERE id >= ? AND COALESCE(NULLIF(timestamp, ''), received_at) >= ?
                  AND COALESCE(NULLIF(timestamp, ''), received_at) NOT LIKE 'millis:%'
            )
            GROUP BY SUBSTR(ts, 1, 16)
            ORDER BY 1
            """,
            (start, since_iso),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    keys = ("timestamp", "tank_volume_l", "tds_ppm", "water_rem_ml",
            "nutrient_pump_on", "drain_pump_on")
    return [dict(zip(keys, row)) for row in rows if row[0]]


def load_hourly_conditions(sqlite_path: str | Path, since_iso: str) -> list[dict[str, Any]]:
    """Per-hour growing conditions since ``since_iso``, oldest first: the inputs to
    development.hourly_conditions. ``hour`` is the UTC hour "YYYY-MM-DDTHH". Sensor
    faults (DHT11 -127, unplugged probes) are left out of the averages. Light comes
    two ways: ``led_on`` (what the firmware commanded) and ``light_seen`` (the LDR
    over LIGHT_SEEN_ADC: 0 in the dark; under the grow light 300 to 3000, depending
    on where the light and the ambient board sit), with ``light_adc_max`` so a dead
    LDR (near 0 all day) can be told from dark.
    Read-only on its own connection (WAL)."""
    path = Path(sqlite_path)
    if not path.exists():
        return []
    conn = _connect(path)
    try:
        start = first_id_since(conn, since_iso)
        rows = conn.execute(
            """
            SELECT SUBSTR(ts, 1, 13), COUNT(*),
                   AVG(CASE WHEN ambient_temp_c BETWEEN -5 AND 50 THEN ambient_temp_c END),
                   AVG(CASE WHEN water_temp_c BETWEEN 0 AND 40 THEN water_temp_c END),
                   AVG(CASE WHEN ambient_humidity_pct BETWEEN 1 AND 100 THEN ambient_humidity_pct END),
                   AVG(led_on), AVG(CASE WHEN light_adc IS NOT NULL THEN (light_adc > 200) * 1.0 END),
                   MAX(light_adc),
                   AVG(CASE WHEN tds_ppm IS NULL OR ec_dead_low IS NULL THEN NULL
                            WHEN tds_ppm BETWEEN ec_dead_low AND ec_dead_high THEN 1.0 ELSE 0.0 END),
                   AVG(CASE WHEN ph IS NULL OR ph_dead_low IS NULL THEN NULL
                            WHEN ph BETWEEN ph_dead_low AND ph_dead_high THEN 1.0 ELSE 0.0 END)
            FROM (
                SELECT COALESCE(NULLIF(timestamp, ''), received_at) AS ts, ambient_temp_c,
                       water_temp_c, ambient_humidity_pct, led_on, light_adc, tds_ppm, ph,
                       ec_dead_low, ec_dead_high, ph_dead_low, ph_dead_high
                FROM sensor_readings
                WHERE id >= ? AND COALESCE(NULLIF(timestamp, ''), received_at) >= ?
                  AND COALESCE(NULLIF(timestamp, ''), received_at) NOT LIKE 'millis:%'
            )
            GROUP BY SUBSTR(ts, 1, 13)
            ORDER BY 1
            """,
            (start, since_iso),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    keys = ("hour", "readings", "air_temp_c", "water_temp_c", "humidity_pct", "led_on",
            "light_seen", "light_adc_max", "tds_in_band", "ph_in_band")
    return [dict(zip(keys, row)) for row in rows if row[0]]


def load_control_events(sqlite_path: str | Path, since_iso: str) -> list[dict[str, Any]]:
    """ESP32 control events (kind control_event) received since ``since_iso``, oldest first.

    Events live only in mqtt_messages; their msg_key starts with "events:", so the
    key index finds them without scanning the raw payload table.
    """
    path = Path(sqlite_path)
    if not path.exists():
        return []
    with _lock:
        conn = _connect(path)
        try:
            rows = conn.execute(
                """
                SELECT received_at, payload_json FROM mqtt_messages
                WHERE msg_key >= 'events:' AND msg_key < 'events;' AND received_at >= ?
                ORDER BY received_at, id
                """,
                (since_iso,),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
    events = []
    for received_at, payload in rows:
        try:
            item = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict) and item.get("kind") == "control_event":
            item.setdefault("timestamp", received_at)
            if not item.get("timestamp") or str(item["timestamp"]).startswith("millis:"):
                item["timestamp"] = received_at
            events.append(item)
    return events
