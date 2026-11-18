"""Grow-light "flash" for camera captures.

Before a photo the ESP32 is asked to switch the grow light to white
(``{"cmd": "light_flash", "action": "on", "ms": …}``). It answers on the ack
topic once the strip is lit, the camera waits for auto-exposure to settle, the
photo is taken, then ``"off"`` returns the light to whatever it was doing.
The firmware also ends the flash by itself after ``ms``, so a lost "off" (or a
crashed camera script) never leaves the light white. The viewfinder holds the
light white the same way, re-sending "on" before that timeout runs out.

If the ESP32 does not answer (offline, older firmware) the photo is still
taken, just without the flash.

Uses paho-mqtt when it is importable, else the ``mosquitto_pub`` /
``mosquitto_sub`` command-line tools (the camera service runs on the Pi's
system Python, where picamera2 lives).

    CAMERA_FLASH=0        disable the flash
    MQTT_HOST / MQTT_PORT broker (default localhost:1883)
    GARDEN_DEVICE_ID      node id (default garden-node-01)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from contextlib import ExitStack, contextmanager

FLASH_MS = 15000        # firmware safety timeout; the light is normally white ~1.5 s
ACK_TIMEOUT_S = 3.0
SETTLE_S = 0.75         # let auto-exposure / white balance adapt to the white light
REFRESH_S = 5.0         # viewfinder re-sends "on" this often, well inside FLASH_MS

# The last flash ack. The "off" ack carries the grow light's photoperiod state
# ("on", "off", "fade_in", ...), which tells the camera whether it is night.
_last_ack: dict = {}


def light_mode_after_flash():
    """Photoperiod state the grow light went back to after the last flash, or None."""
    return _last_ack.get("mode") if _last_ack.get("flash") is False else None


def _topics():
    device = os.getenv("GARDEN_DEVICE_ID", "garden-node-01")
    return f"smart-garden/{device}/cmd", f"smart-garden/{device}/ack"


def _broker():
    return os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883"))


def enabled():
    return os.getenv("CAMERA_FLASH", "1").strip().lower() not in {"0", "false", "no", "off"}


def _is_flash_ack(raw, want_on):
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if msg.get("cmd") == "light_flash" and bool(msg.get("flash")) == want_on:
        _last_ack.clear()
        _last_ack.update(msg, flash=bool(msg.get("flash")))
        return True
    return False


@contextmanager
def _paho_sender():
    """One MQTT connection for both "on" and "off", so "off" goes out at once."""
    import paho.mqtt.client as mqtt

    cmd_topic, ack_topic = _topics()
    host, port = _broker()
    acks = []
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"smart-garden-camera-{os.getpid()}")
    client.on_message = lambda _c, _u, m: acks.append(m.payload.decode("utf-8", "replace"))
    client.connect(host, port, keepalive=30)
    client.loop_start()

    def send(payload, want_ack):
        acks.clear()
        client.publish(cmd_topic, json.dumps(payload)).wait_for_publish(2)
        deadline = time.monotonic() + ACK_TIMEOUT_S
        while time.monotonic() < deadline:
            if any(_is_flash_ack(raw, want_ack) for raw in list(acks)):
                return True
            time.sleep(0.05)
        return False

    try:
        client.subscribe(ack_topic)
        time.sleep(0.2)
        yield send
    finally:
        client.loop_stop()
        client.disconnect()


def _send_cli(payload, want_ack):
    cmd_topic, ack_topic = _topics()
    host, port = _broker()
    sub = None
    if shutil.which("mosquitto_sub"):
        sub = subprocess.Popen(
            ["mosquitto_sub", "-h", host, "-p", str(port), "-t", ack_topic, "-C", "1",
             "-W", str(int(ACK_TIMEOUT_S) + 1)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        time.sleep(0.3)
    subprocess.run(["mosquitto_pub", "-h", host, "-p", str(port), "-t", cmd_topic,
                    "-m", json.dumps(payload)], check=True, timeout=5)
    if sub is None:
        return False
    try:
        out, _ = sub.communicate(timeout=ACK_TIMEOUT_S + 2)
    except subprocess.TimeoutExpired:
        sub.kill()
        return False
    return _is_flash_ack(out.strip(), want_ack)


@contextmanager
def _sender():
    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError:
        if not shutil.which("mosquitto_pub"):
            raise RuntimeError("no paho-mqtt and no mosquitto_pub for the flash")
        yield _send_cli
        return
    with _paho_sender() as send:
        yield send


@contextmanager
def _white_light(log):
    """Ask for white for the ``with`` block. Yields ``send`` (None if there is no
    connection) and whether the light confirmed; sends "off" at the end if it did."""
    if not enabled():
        yield None, False
        return
    with ExitStack() as stack:
        send, lit = None, False
        _last_ack.clear()
        try:
            send = stack.enter_context(_sender())
            lit = send({"cmd": "light_flash", "action": "on", "ms": FLASH_MS}, True)
        except Exception as exc:  # noqa: BLE001 - never block a photo on the light
            log(f"Flash request failed ({exc}); taking the photo without it")
        if not lit:
            log("Grow light did not confirm the flash; taking the photo without it")
        try:
            yield send, lit
        finally:
            if lit:
                try:
                    send({"cmd": "light_flash", "action": "off"}, False)
                except Exception as exc:  # noqa: BLE001 - firmware times the flash out anyway
                    log(f"Flash off failed ({exc}); the light returns by itself in "
                        f"{FLASH_MS // 1000} s")


@contextmanager
def grow_light_flash(log=print, settle_s=SETTLE_S):
    """Light the grow light white for the duration of the ``with`` block.

    ``settle_s`` is the wait for auto-exposure after the light confirms; a caller
    that fixes the exposure itself passes 0 and keeps the flash short.
    """
    with _white_light(log) as (_send, lit):
        if lit and settle_s > 0:
            time.sleep(settle_s)
        yield lit


@contextmanager
def grow_light_held(log=print):
    """Keep the grow light white while the viewfinder is open.

    Yields ``keep_lit()``. Call it every frame: it re-sends "on" every REFRESH_S
    so the firmware's FLASH_MS timeout does not end the flash. If this process
    dies, the light goes back to its photoperiod within FLASH_MS.
    """
    with _white_light(log) as (send, lit):
        last = time.monotonic()

        def keep_lit():
            nonlocal last
            if not lit or time.monotonic() - last < REFRESH_S:
                return
            last = time.monotonic()
            try:
                send({"cmd": "light_flash", "action": "on", "ms": FLASH_MS}, True)
            except Exception as exc:  # noqa: BLE001 - the viewfinder carries on regardless
                log(f"Keeping the light white failed ({exc})")

        yield keep_lit
