#!/usr/bin/env bash
# Configure and start Mosquitto on the Raspberry Pi 4 (MQTT broker for ESP32).
# Run this ON the Pi, from the dashboard repo root:
#   ./scripts/setup-pi-mqtt-broker.sh
#
# Then on the ESP32 secrets.h set:
#   MQTT_BROKER_HOST = "<Pi LAN IP from hostname -I>"
# Dashboard (on the Pi) MQTT mode:
#   Host localhost   Port 1883   Topic smart-garden/+/telemetry

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_CONF="$ROOT/docker/mosquitto-pi.conf"
DEST_CONF="/etc/mosquitto/conf.d/smart-garden.conf"

if [ "$(uname -m)" = "Darwin" ]; then
  echo "This script is for the Raspberry Pi, not macOS."
  echo "Copy the dashboard repo to the Pi and run it there."
  exit 1
fi

if [ ! -f "$SRC_CONF" ]; then
  echo "Missing $SRC_CONF"
  exit 1
fi

if ! command -v mosquitto >/dev/null 2>&1; then
  echo "Installing mosquitto..."
  sudo apt-get update
  sudo apt-get install -y mosquitto mosquitto-clients
fi

echo "Installing broker config -> $DEST_CONF"
sudo mkdir -p /etc/mosquitto/conf.d
sudo cp "$SRC_CONF" "$DEST_CONF"

# Ensure main config includes conf.d (Debian/Raspberry Pi OS default usually does).
if [ -f /etc/mosquitto/mosquitto.conf ] && ! grep -q 'include_dir /etc/mosquitto/conf.d' /etc/mosquitto/mosquitto.conf; then
  echo "include_dir /etc/mosquitto/conf.d" | sudo tee -a /etc/mosquitto/mosquitto.conf >/dev/null
fi

sudo systemctl enable mosquitto
sudo systemctl restart mosquitto
sudo systemctl --no-pager --full status mosquitto || true

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Mosquitto should now listen on 0.0.0.0:1883 (anonymous)."
echo "ESP32 MQTT_BROKER_HOST: smart-garden.local"
echo "  (fallback LAN IP: ${LAN_IP:-<pi-lan-ip>} from hostname -I)"
echo "On the Pi dashboard use MQTT host: localhost"
echo "Topic: smart-garden/+/telemetry"
echo
echo "Quick test on the Pi:"
echo "  mosquitto_sub -h localhost -t 'smart-garden/+/telemetry' -v"
echo "From another device: ping smart-garden.local"
