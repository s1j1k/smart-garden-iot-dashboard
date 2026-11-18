#!/usr/bin/env bash
# Print the MQTT broker host the ESP32 should use when the dashboard runs
# on this PC (ChromeOS Linux / laptop), not the Raspberry Pi.

set -euo pipefail

IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
echo "Linux hostname -I → ${IP:-unknown}"
echo
if [[ "${IP:-}" == 100.115.* ]]; then
  echo "That is the Crostini/penguin container IP — ESP32 cannot use it."
  echo
  echo "Do this instead:"
  echo "  1. ChromeOS Settings → Network → HOME_WIFI → note the IPv4 address"
  echo "     (usually 192.168.x.x or 10.x.x.x)."
  echo "  2. ChromeOS Settings → Developers → Linux → Port forwarding:"
  echo "       Port 1883, TCP, Linux (penguin)."
  echo "  3. In embedded-smart-garden/secrets.h set:"
  echo "       const char* MQTT_BROKER_HOST = \"<chromebook-wifi-ip>\";"
  echo "  4. On this machine start the broker + dashboard:"
  echo "       cd ~/dev/smart-garden-iot-dashboard"
  echo "       ./scripts/run-local-dashboard.sh"
else
  echo "Likely LAN IP for ESP32 MQTT_BROKER_HOST:"
  echo "  const char* MQTT_BROKER_HOST = \"${IP}\";"
fi
echo
echo "Dashboard (same PC) uses mqtt_host=localhost in the Streamlit sidebar."
