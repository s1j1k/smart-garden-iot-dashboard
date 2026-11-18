#!/bin/sh
set -eu

# Prefer the project broker config so LAN ESP32 clients can connect
# (Mosquitto 2.x defaults to localhost-only + auth if left unconfigured).
BROKER_CONF="/etc/mosquitto/conf.d/smart-garden.conf"
if [ ! -f "$BROKER_CONF" ]; then
  BROKER_CONF="/etc/mosquitto/mosquitto.conf"
fi

echo "[smart-garden] Starting Mosquitto with $BROKER_CONF"
mosquitto -c "$BROKER_CONF" &

# Always-on alerts: pushes to ntfy even when no dashboard tab is open.
echo "[smart-garden] Starting alert notifier"
MQTT_HOST="${MQTT_HOST:-127.0.0.1}" python scripts/alert_notifier.py &

# server.py serves app.py as an installable web app (manifest + service worker).
exec streamlit run src/server.py \
  --server.address 0.0.0.0 \
  --server.port 8501
