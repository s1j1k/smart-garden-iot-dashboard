#!/usr/bin/env bash
# Run the Streamlit dashboard on this PC (not the Raspberry Pi).
#
# Prerequisites:
#   1. MQTT broker listening on localhost:1883
#        ./scripts/start-mqtt-broker.sh
#      or: docker compose -f docker-compose.local.yml up -d mosquitto
#   2. ESP32 secrets.h MQTT_BROKER_HOST = this PC's WiFi LAN IP on HOME_WIFI
#      (ChromeOS: Settings → Network → HOME_WIFI → IP). Forward TCP 1883
#      into Linux if you are in a Crostini/penguin container.
#
# Usage (from dashboard repo root):
#   ./scripts/run-local-dashboard.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CONTAINER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"

echo "=== Smart Garden dashboard (local PC) ==="
echo "Dashboard MQTT host: localhost:1883"
echo "Subscribe topic:     smart-garden/+/telemetry"
echo "Command topic:       smart-garden/garden-node-01/cmd"
echo
echo "This Linux environment IP is: ${CONTAINER_IP:-unknown}"
if [[ "${CONTAINER_IP:-}" == 100.115.* ]]; then
  echo
  echo "WARNING: ${CONTAINER_IP} is a ChromeOS Linux *container* address."
  echo "The ESP32 on HOME_WIFI WiFi cannot reach it."
  echo "Set ESP32 MQTT_BROKER_HOST to the Chromebook WiFi IP instead,"
  echo "and port-forward 1883 in ChromeOS → Linux settings."
  echo
fi

if ! ss -ltn 2>/dev/null | grep -q ':1883' && ! netstat -ltn 2>/dev/null | grep -q ':1883'; then
  echo "No listener on :1883 yet."
  if command -v docker >/dev/null 2>&1; then
    echo "Starting Mosquitto via docker compose.local.yml …"
    docker compose -f docker-compose.local.yml up -d mosquitto
  elif command -v mosquitto >/dev/null 2>&1; then
    echo "Start broker in another terminal: ./scripts/start-mqtt-broker.sh"
  else
    echo "Install mosquitto or Docker, then re-run."
    echo "  sudo apt-get install -y mosquitto mosquitto-clients"
    exit 1
  fi
fi

if [[ ! -d .venv ]]; then
  echo "Creating .venv …"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

mkdir -p data
export STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

echo
echo "Opening Streamlit on http://localhost:8501"
echo "Calibration tab → pH probe + peristaltic pump cup wizard over MQTT."
echo
exec streamlit run src/server.py --server.address 0.0.0.0 --server.port 8501
