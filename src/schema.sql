-- Local SQLite store for the MQTT dashboard.
-- Runtime schema is created/migrated by src/sqlite_store.py (authoritative).
-- This file documents the tables for humans / one-shot imports.

CREATE TABLE IF NOT EXISTS mqtt_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,          -- ISO-8601 UTC
  topic TEXT NOT NULL,
  payload_json TEXT NOT NULL,         -- full MQTT payload as received
  device_id TEXT,
  parse_ok INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sensor_readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  source TEXT NOT NULL,
  device_id TEXT NOT NULL,
  timestamp TEXT,
  grow_stage TEXT,
  hydroponic_mode TEXT,
  ph_settle_s REAL,
  control_law TEXT,
  pumps_calibrated REAL,
  water_target_pct REAL,
  water_max_pct REAL,
  nutrient_tds_min REAL,
  nutrient_tds_max REAL,
  ph_target_min REAL,
  ph_target_max REAL,
  ph_dose TEXT,
  nutrient_dose TEXT,
  ph_voltage REAL,
  ph_raw REAL,
  ph_filtered REAL,
  water_cap_ml REAL,
  water_rem_ml REAL,
  water_empty INTEGER,
  nutrient_cap_ml REAL,
  nutrient_rem_ml REAL,
  nutrient_empty INTEGER,
  ph_up_cap_ml REAL,
  ph_up_rem_ml REAL,
  ph_up_empty INTEGER,
  ph_down_cap_ml REAL,
  ph_down_rem_ml REAL,
  ph_down_empty INTEGER,
  stock_alert TEXT,
  ambient_temp_c REAL,
  ambient_humidity_pct REAL,
  water_temp_c REAL,
  light_adc REAL,
  water_level_pct REAL,
  tds_ppm REAL,
  ph REAL,
  co2_ppm REAL,
  water_pump_on INTEGER,
  drain_pump_on INTEGER,
  nutrient_pump_on INTEGER,
  ph_up_pump_on INTEGER,
  ph_down_pump_on INTEGER,
  led_on INTEGER,
  fan_on INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sensor_readings_device_time
  ON sensor_readings (device_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_sensor_readings_received_at
  ON sensor_readings (received_at);

CREATE INDEX IF NOT EXISTS idx_mqtt_messages_received_at
  ON mqtt_messages (received_at);

CREATE INDEX IF NOT EXISTS idx_mqtt_messages_topic
  ON mqtt_messages (topic);
