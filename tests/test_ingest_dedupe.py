"""Reading identity, idempotent SQLite storage and the shared MQTT client."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sensor_ingest  # noqa: E402
from sensor_ingest import MqttSensorClient, normalize_reading, reading_key  # noqa: E402
from sqlite_store import persist_mqtt_payload  # noqa: E402

TOPIC = "smart-garden/garden-node-01/telemetry"


def _payload(seq, **extra):
    base = {
        "device_id": "garden-node-01",
        "timestamp": "2026-10-05T03:00:00Z",
        "kind": "reading",
        "boot_id": 7,
        "seq": seq,
        "uptime_ms": 1000 * seq,
        "ph": 6.0,
        "tds_ppm": 700,
        "water_temp_c": 22.0,
        "ambient_temp_c": 25.0,
        "ambient_humidity_pct": 40.0,
        "light_adc": 2000,
        "water_level_pct": 80,
        "co2_ppm": 900,
    }
    base.update(extra)
    return base


def test_reading_key_forms():
    assert reading_key(_payload(5)) == "garden-node-01/7/5"
    # Status snapshots (seq 0 / missing) and old firmware have no key.
    assert reading_key(_payload(0)) is None
    assert reading_key({"device_id": "garden-node-01", "seq": 3}) is None
    event = {"device_id": "garden-node-01", "kind": "control_event", "boot_id": 7, "event_seq": 2}
    assert reading_key(event) == "garden-node-01/7/e2"


def _store(db, payload, topic=TOPIC):
    raw = json.dumps(payload).encode()
    reading = normalize_reading(payload, topic=topic) if topic.endswith("/telemetry") else None
    return persist_mqtt_payload(db, topic=topic, payload_bytes=raw, reading=reading)


def test_replay_of_a_stored_reading_is_not_written_twice(tmp_path):
    db = tmp_path / "r.sqlite"
    assert _store(db, _payload(1)) is True
    # Outbox replay of the same reading: different flags, same identity.
    assert _store(db, _payload(1, replay=True)) is False
    assert _store(db, _payload(2)) is True
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM mqtt_messages").fetchone()[0] == 2
    keys = [r[0] for r in conn.execute("SELECT reading_key FROM sensor_readings ORDER BY id")]
    assert keys == ["garden-node-01/7/1", "garden-node-01/7/2"]


def test_messages_without_identity_are_all_kept(tmp_path):
    db = tmp_path / "r.sqlite"
    status = _payload(0)
    status.pop("seq")
    status["kind"] = "status"
    assert _store(db, status) is True
    assert _store(db, status) is True  # distinct snapshots, no key
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM mqtt_messages").fetchone()[0] == 2


def test_control_events_dedupe_on_event_seq(tmp_path):
    db = tmp_path / "r.sqlite"
    event = {"device_id": "garden-node-01", "kind": "control_event", "boot_id": 7,
             "event_seq": 4, "event": "ec_check"}
    topic = "smart-garden/garden-node-01/events"
    assert _store(db, event, topic) is True
    assert _store(db, event, topic) is False


class _Msg:
    def __init__(self, payload):
        self.topic = TOPIC
        self.payload = json.dumps(payload).encode()


def test_shared_client_fans_out_to_each_reader(tmp_path):
    config = {
        "mqtt_host": "localhost", "mqtt_port": 1883, "mqtt_topic": TOPIC,
        "mqtt_client_id": "t", "mqtt_keepalive": 60, "mqtt_persist": False,
        "sensor_log_path": str(tmp_path / "log.csv"), "sqlite_path": str(tmp_path / "x.sqlite"),
    }
    client = MqttSensorClient(config)
    a = b = client.latest_cursor()
    client._on_message(None, None, _Msg(_payload(1)))
    client._on_message(None, None, _Msg(_payload(2)))
    a, got_a = client.drain_since(a)
    assert [r["seq"] for r in got_a] == [1, 2]
    # A second session sees the same readings; the first sees nothing new.
    b, got_b = client.drain_since(b)
    assert [r["seq"] for r in got_b] == [1, 2]
    assert client.drain_since(a)[1] == []
    # mqtt_persist False: the dashboard did not write anything.
    assert not (tmp_path / "x.sqlite").exists()
    assert not (tmp_path / "log.csv").exists()


def test_shared_mqtt_client_is_one_per_config(monkeypatch):
    started = []
    monkeypatch.setattr(MqttSensorClient, "start", lambda self: started.append(self))
    monkeypatch.setattr(MqttSensorClient, "stop", lambda self: None)
    sensor_ingest._shared_clients.clear()
    config = {"mqtt_host": "h", "mqtt_port": 1883, "mqtt_topic": "t", "mqtt_client_id": "c",
              "mqtt_keepalive": 60}
    first = sensor_ingest.shared_mqtt_client(config)
    assert sensor_ingest.shared_mqtt_client(dict(config)) is first
    other = sensor_ingest.shared_mqtt_client({**config, "mqtt_host": "h2"})
    assert other is not first
    assert list(sensor_ingest._shared_clients.values()) == [other]
    sensor_ingest._shared_clients.clear()
