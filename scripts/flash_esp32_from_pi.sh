#!/usr/bin/env bash
# Flash the ESP32 app over the USB cable from the Pi.
#
#   ./scripts/flash_esp32_from_pi.sh embedded-smart-garden.ino.bin
#
# The .bin is the app image from arduino-cli compile (build/<sketch>.ino.bin);
# only the app at 0x10000 is written, so the partition table must be unchanged.
# The garden goes to standby over MQTT first, as flash.ps1 does, and the serial
# bridge lets go of the port while /tmp/esp32-flash.lock exists.
set -euo pipefail

APP_BIN="${1:?usage: $0 <app.bin>}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-$REPO/.venv/bin/python}"
LOCK="${ESP32_FLASH_LOCK:-/tmp/esp32-flash.lock}"
PORT="${SERIAL_PORT:-$(ls /dev/serial/by-id/usb-Silicon_Labs_CP210* 2>/dev/null | head -1)}"
PORT="${PORT:-/dev/ttyUSB0}"

[[ -s "$APP_BIN" ]] || { echo "[flash] no such file: $APP_BIN" >&2; exit 1; }
"$PY" -m esptool version >/dev/null 2>&1 || { echo "[flash] esptool missing: $PY -m pip install esptool" >&2; exit 1; }

if command -v mosquitto_pub >/dev/null; then
  mosquitto_pub -h localhost -t smart-garden/garden-node-01/cmd \
    -m '{"cmd":"power","action":"off"}' && echo "[flash] standby requested" && sleep 3
fi

trap 'rm -f "$LOCK"' EXIT
touch "$LOCK"
sleep 2  # the bridge checks the lock between reads
echo "[flash] writing $APP_BIN to $PORT"
"$PY" -m esptool --chip esp32 --port "$PORT" --baud 460800 write-flash 0x10000 "$APP_BIN"
echo "[flash] done; the node reboots into standby (press ON or send power on)"
