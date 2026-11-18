#!/usr/bin/env python3
"""Always-on MQTT → SQLite/CSV ingest (does not require an open Streamlit tab).

The only writer of data/sensor_readings.sqlite on the Pi (systemd unit
smart-garden-ingest). The dashboard reads; it does not store.

A message whose identity is already stored (firmware 1.8.0+: device_id,
boot_id, seq — e.g. an outbox replay of a reading that did arrive live) is
counted as a repeat and not written again.

Run from the dashboard repo root:
  .venv/Scripts/python.exe scripts/mqtt_to_sqlite.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config import DEFAULT_CONFIG  # noqa: E402
from sensor_ingest import (  # noqa: E402
    append_sensor_log,
    iso_now,
    normalize_reading,
    persist_mqtt_to_sqlite,
)

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"paho-mqtt required: {exc}") from exc


def main() -> None:
    host = os.getenv("MQTT_HOST", DEFAULT_CONFIG["mqtt_host"])
    port = int(os.getenv("MQTT_PORT", DEFAULT_CONFIG["mqtt_port"]))
    topic = DEFAULT_CONFIG["mqtt_topic"]
    # Also capture alerts / other device topics under the garden prefix.
    wild = "smart-garden/#"
    sqlite_path = DEFAULT_CONFIG["sqlite_path"]
    csv_path = DEFAULT_CONFIG["sensor_log_path"]

    print(f"[ingest] broker={host}:{port}  subscribe={wild}")
    print(f"[ingest] sqlite={sqlite_path}")
    print(f"[ingest] csv={csv_path}")

    counts = {"raw": 0, "telemetry": 0, "repeats": 0, "errors": 0}

    def on_connect(client, _userdata, _flags, rc, properties=None):
        print(f"[ingest] connected rc={rc}")
        client.subscribe(wild)

    def on_message(_client, _userdata, msg):
        counts["raw"] += 1
        reading = None
        try:
            if msg.topic.endswith("/telemetry"):
                payload = json.loads(msg.payload.decode("utf-8"))
                reading = normalize_reading(payload, source="mqtt", topic=msg.topic)
        except Exception as exc:
            counts["errors"] += 1
            print(f"[ingest] normalize failed on {msg.topic}: {exc}")
        is_new = None
        try:
            is_new = persist_mqtt_to_sqlite(
                sqlite_path,
                msg.topic,
                msg.payload,
                reading=reading,
                received_at=(reading or {}).get("received_at") if reading else iso_now(),
            )
        except Exception as exc:
            counts["errors"] += 1
            print(f"[ingest] sqlite failed: {exc}")
        if is_new is False:
            counts["repeats"] += 1
        elif reading is not None:
            # CSV mirrors SQLite: one row per reading.
            append_sensor_log(csv_path, reading, sqlite_path=None)
            counts["telemetry"] += 1
        if counts["raw"] % 50 == 0:
            print(
                f"[ingest] raw={counts['raw']} telemetry={counts['telemetry']} "
                f"repeats={counts['repeats']} errors={counts['errors']} last={msg.topic}"
            )

    kwargs = {"client_id": "smart-garden-sqlite-ingest"}
    if hasattr(mqtt, "CallbackAPIVersion"):
        kwargs["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION1
    client = mqtt.Client(**kwargs)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    # Broker may still be starting at boot: keep trying instead of exiting.
    while True:
        try:
            client.connect(host, port, keepalive=60)
            break
        except OSError as exc:
            print(f"[ingest] broker not reachable ({exc}); retry in 10 s")
            time.sleep(10)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
