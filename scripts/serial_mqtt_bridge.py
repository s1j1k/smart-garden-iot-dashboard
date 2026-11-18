#!/usr/bin/env python3
"""Serial link to the ESP32 over the USB cable, for when its WiFi cannot reach the Pi.

The firmware (1.9.29+) sends its MQTT messages on Serial while MQTT is down and
this bridge is talking to it:

  Pi  -> ESP32   @LINK <epoch>          every 5 s; keeps the link up, sets the clock
  ESP32 -> Pi    @LS mqtt|serial        reply: which link the node is using
  ESP32 -> Pi    @MQ <r|-> <topic> <payload>   publish (r = retained)
  Pi  -> ESP32   @CMD <json>            a dashboard command from .../cmd

Commands go down the cable only while the node says "serial", so a command it
also got over MQTT does not run twice. Every other Serial line (the firmware's
log) goes to data/esp32_serial.log.

  .venv/bin/python scripts/serial_mqtt_bridge.py

Flashing: scripts/flash_esp32_from_pi.sh creates /tmp/esp32-flash.lock; while it
exists the bridge lets go of the port.

The dashboard counts the node as live from any message in the last 90 s, so
messages carried here show it live the same as over WiFi.
"""

from __future__ import annotations

import glob
import logging
import os
import sys
import termios
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
    import serial
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"paho-mqtt and pyserial required: {exc}") from exc

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
CMD_TOPIC = os.getenv("SERIAL_BRIDGE_CMD_TOPIC", "smart-garden/+/cmd")
BAUD = 115200
# On 20 Oct the node came back talking at 74880 baud (26/40 of 115200) after a
# bridge process was killed and the port closed. When the bytes stop being
# text, the bridge tries the next rate.
BAUDS = (BAUD, 74880)
BAUD_CHECK_BYTES = 2048
BAUD_MIN_PRINTABLE = 0.7
HEARTBEAT_S = 5
# The node replies to every heartbeat; older than this and its mode is unknown.
MODE_STALE_S = 3 * HEARTBEAT_S
FLASH_LOCK = Path(os.getenv("ESP32_FLASH_LOCK", "/tmp/esp32-flash.lock"))
CMD_MAX = 768  # COMMS_INBOX_PAYLOAD_MAX in the firmware

log = logging.getLogger("serial_bridge")


def find_port() -> str | None:
    explicit = os.getenv("SERIAL_PORT")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for pattern in ("/dev/serial/by-id/usb-Silicon_Labs_CP210*", "/dev/ttyUSB*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


class Bridge:
    def __init__(self) -> None:
        self.ser: serial.Serial | None = None
        self.write_lock = threading.Lock()
        self.mode = ""
        self.mode_at = 0.0
        self.published = 0
        self.esp_log = self._esp_log()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"serial-bridge-{os.getpid()}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 30)

    @staticmethod
    def _esp_log() -> logging.Logger:
        DATA_DIR.mkdir(exist_ok=True)
        handler = RotatingFileHandler(DATA_DIR / "esp32_serial.log",
                                      maxBytes=5_000_000, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        esp = logging.getLogger("esp32")
        esp.addHandler(handler)
        esp.setLevel(logging.INFO)
        esp.propagate = False
        return esp

    # --- MQTT -----------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        log.info("MQTT connected to %s:%s (%s)", MQTT_HOST, MQTT_PORT, reason_code)
        client.subscribe(CMD_TOPIC, qos=0)

    def _on_message(self, client, userdata, msg) -> None:
        payload = msg.payload.decode("utf-8", "replace").replace("\r", " ").replace("\n", " ")
        if self.mode != "serial" or time.time() - self.mode_at > MODE_STALE_S:
            return  # the node is on MQTT (or silent): it gets the command itself
        if len(payload) >= CMD_MAX:
            log.warning("command too long for the serial link (%d bytes), dropped", len(payload))
            return
        if self._write(f"@CMD {payload}\n"):
            log.info("command -> serial: %s", payload[:200])

    # --- Serial ---------------------------------------------------------
    def _write(self, text: str) -> bool:
        with self.write_lock:
            if self.ser is None:
                return False
            try:
                self.ser.write(text.encode("utf-8"))
                return True
            except (serial.SerialException, OSError) as exc:
                log.warning("serial write failed: %s", exc)
                return False

    def _handle_line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("@MQ "):
            parts = line.split(" ", 3)
            if len(parts) < 4:
                log.warning("bad @MQ line: %s", line[:120])
                return
            _, flag, topic, payload = parts
            self.client.publish(topic, payload, qos=0, retain=(flag == "r"))
            self.published += 1
            if self.published == 1 or self.published % 100 == 0:
                log.info("published %d messages from serial (last %s, %d B)",
                         self.published, topic, len(payload))
        elif line.startswith("@LS "):
            mode = line[4:].strip()
            if mode != self.mode:
                log.info("node link mode: %s", mode)
            self.mode, self.mode_at = mode, time.time()
        elif line:
            self.esp_log.info(line)

    def _heartbeat(self) -> None:
        while True:
            self._write(f"@LINK {int(time.time())}\n")
            time.sleep(HEARTBEAT_S)

    def _open(self, port: str) -> serial.Serial:
        # Leave DTR/RTS as the kernel sets them on open (both asserted): on the
        # ESP32 auto-reset circuit equal levels mean no reset. Driving them low
        # one after the other pulses EN and reboots the node.
        ser = serial.Serial(port, BAUD, timeout=0.5)
        # No hang-up on close: dropping DTR/RTS when the port closes (bridge
        # restart, a probe script) can reset the node into the ROM
        # download mode, where it stays silent until the next reset.
        attrs = termios.tcgetattr(ser.fd)
        attrs[2] &= ~termios.HUPCL
        termios.tcsetattr(ser.fd, termios.TCSANOW, attrs)
        ser.reset_input_buffer()
        return ser

    def run(self) -> None:
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        self.client.loop_start()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        while True:
            if FLASH_LOCK.exists():
                time.sleep(1)
                continue
            port = find_port()
            if port is None:
                time.sleep(5)
                continue
            try:
                ser = self._open(port)
            except (serial.SerialException, OSError) as exc:
                log.warning("cannot open %s: %s", port, exc)
                time.sleep(5)
                continue
            log.info("serial open on %s", port)
            with self.write_lock:
                self.ser = ser
            buf = b""
            baud_idx, seen, printable = 0, 0, 0
            try:
                while not FLASH_LOCK.exists():
                    chunk = ser.read(4096)
                    if not chunk:
                        continue
                    seen += len(chunk)
                    printable += sum(32 <= c < 127 or c in (9, 10, 13) for c in chunk)
                    if seen >= BAUD_CHECK_BYTES:
                        if printable / seen < BAUD_MIN_PRINTABLE:
                            baud_idx = (baud_idx + 1) % len(BAUDS)
                            ser.baudrate = BAUDS[baud_idx]
                            log.warning("serial looks garbled (%d%% text): trying %d baud",
                                        100 * printable // seen, BAUDS[baud_idx])
                            buf, chunk = b"", b""
                        seen, printable = 0, 0
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        self._handle_line(raw)
                    if len(buf) > 64 * 1024:  # no newline in 64 kB: not our framing
                        buf = b""
                log.info("flash lock present: releasing %s", port)
            except (serial.SerialException, OSError) as exc:
                log.warning("serial lost: %s", exc)
            finally:
                with self.write_lock:
                    self.ser = None
                    try:
                        ser.close()
                    except Exception:
                        pass
                self.mode = ""
            time.sleep(1)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Bridge().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
