#!/usr/bin/env python3
"""Always-on alert notifier: MQTT telemetry → alert engine → ntfy push.

Runs next to the dashboard (the Docker image starts it automatically) so
alerts reach the phone and computer even when no dashboard tab is open.

  .venv/Scripts/python.exe scripts/alert_notifier.py          # run forever
  .venv/Scripts/python.exe scripts/alert_notifier.py --test   # send one test message

Set the ntfy topic on the dashboard (Alerts → Phone & computer notifications)
or with NTFY_TOPIC. Status is written to data/notifier_status.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import alerts  # noqa: E402
import garden_settings  # noqa: E402
from config import DATA_DIR, DEFAULT_CONFIG, INITIAL_STATE, seedling_age, suggested_grow_stage  # noqa: E402
from notifier import AlertTracker, dispatch, send_ntfy  # noqa: E402
from sensor_ingest import normalize_reading, reading_to_state  # noqa: E402

STATUS_PATH = DATA_DIR / "notifier_status.json"
CHECK_EVERY_S = 30

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"paho-mqtt required: {exc}") from exc


def write_status(**fields):
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(fields, indent=2), encoding="utf-8")
    tmp.replace(STATUS_PATH)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="send one test notification and exit")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # log lines show up under Docker / nohup

    if args.test:
        ok, err = send_ntfy(garden_settings.load(), "Smart garden test",
                            "Notifications from the garden reach this device.", "default",
                            "seedling")
        print("sent" if ok else f"failed: {err}")
        sys.exit(0 if ok else 1)

    host = os.getenv("MQTT_HOST", DEFAULT_CONFIG["mqtt_host"])
    port = int(os.getenv("MQTT_PORT", DEFAULT_CONFIG["mqtt_port"]))
    stale_s = DEFAULT_CONFIG["mqtt_stale_after_seconds"]
    lock = threading.Lock()
    shared = {"state": dict(INITIAL_STATE), "last_seen": None}

    def on_connect(client, _userdata, _flags, rc, properties=None):
        print(f"[notifier] connected to {host}:{port} rc={rc}")
        client.subscribe(DEFAULT_CONFIG["mqtt_topic"])

    def on_message(_client, _userdata, msg):
        try:
            reading = normalize_reading(json.loads(msg.payload.decode("utf-8")), topic=msg.topic)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not stop alerts
            print(f"[notifier] skipped message: {exc}")
            return
        if reading.get("replay"):
            return
        with lock:
            shared["state"] = reading_to_state(reading, shared["state"])
            shared["last_seen"] = time.monotonic()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"smart-garden-notifier-{os.getpid()}")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(host, port, keepalive=60)
    client.loop_start()
    print(f"[notifier] watching {DEFAULT_CONFIG['mqtt_topic']} on {host}:{port}")

    tracker = AlertTracker()
    last_sent = []
    while True:
        settings = garden_settings.load()
        _, week, _ = seedling_age(planted_at=garden_settings.planted_at(settings))
        with lock:
            state = dict(shared["state"])
            seen = shared["last_seen"]
        age = None if seen is None else time.monotonic() - seen
        active = alerts.evaluate(state, node_live=age is not None and age < stale_s,
                                 suggested_stage=suggested_grow_stage(week), plant_week=week,
                                 last_seen_s=age)
        if settings.get("ntfy_topic"):
            results = dispatch(tracker, active, settings)
            for title, ok, err in results:
                print(f"[notifier] {'sent' if ok else 'FAILED'}: {title} {err}")
            if results:
                last_sent = [{"title": t, "ok": ok, "error": e,
                              "at": datetime.now(timezone.utc).isoformat()}
                             for t, ok, e in results]
        write_status(
            broker=f"{host}:{port}",
            mqtt_connected=client.is_connected(),
            ntfy_configured=bool(settings.get("ntfy_topic")),
            active=[a.as_dict() for a in active],
            last_sent=last_sent,
        )
        time.sleep(CHECK_EVERY_S)


if __name__ == "__main__":
    main()
