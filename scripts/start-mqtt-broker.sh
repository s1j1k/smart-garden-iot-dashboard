#!/usr/bin/env bash
# Start Mosquitto on this machine for local ESP32 ↔ dashboard testing.
#
# Prefer: ./scripts/run-local-dashboard.sh  (starts broker if needed + Streamlit)
#
# ChromeOS Linux: hostname -I is a container IP. ESP32 must use the Chromebook
# WiFi IP + port-forward 1883. Run: ./scripts/print-broker-ip-hint.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF="$ROOT/docker/mosquitto.conf"

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
echo "Starting Mosquitto with $CONF"
echo "Linux IP (may be a container): ${LAN_IP:-unknown}"
echo "Dashboard MQTT host (same machine): localhost"
echo "ESP32 MQTT_BROKER_HOST: Chromebook/PC WiFi IP on HOME_WIFI — see print-broker-ip-hint.sh"
echo

if command -v docker >/dev/null 2>&1; then
  cd "$ROOT"
  exec docker compose -f docker-compose.local.yml up mosquitto
fi

if ! command -v mosquitto >/dev/null 2>&1; then
  echo "Install mosquitto or Docker:"
  echo "  sudo apt-get install -y mosquitto mosquitto-clients"
  echo "  or: docker compose -f docker-compose.local.yml up -d mosquitto"
  exit 1
fi

exec mosquitto -c "$CONF"
