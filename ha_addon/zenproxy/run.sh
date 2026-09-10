#!/usr/bin/env bash
set -euo pipefail

OPTIONS_FILE=/data/options.json
CONFIG_FILE=/data/zenproxy.yaml

virtual_sn=$(jq -r '.virtual_sn' "$OPTIONS_FILE")
virtual_product=$(jq -r '.virtual_product' "$OPTIONS_FILE")
port=$(jq -r '.port' "$OPTIONS_FILE")

{
    echo "virtual_sn: ${virtual_sn}"
    echo "virtual_product: ${virtual_product}"
    echo
    echo "devices:"
    jq -r '.devices[] | "  - host: \(.host)\n    port: \(.port)"' "$OPTIONS_FILE"
    echo
    echo "server:"
    echo "  host: 0.0.0.0"
    echo "  port: ${port}"
    if [ -n "${MQTT_HOST:-}" ]; then
        echo
        echo "mqtt:"
        echo "  host: ${MQTT_HOST}"
        echo "  port: ${MQTT_PORT:-1883}"
        if [ -n "${MQTT_USERNAME:-}" ]; then
            echo "  username: ${MQTT_USERNAME}"
        fi
        if [ -n "${MQTT_PASSWORD:-}" ]; then
            echo "  password: ${MQTT_PASSWORD}"
        fi
    fi
} > "$CONFIG_FILE"

exec /app/.venv/bin/zenproxy --config "$CONFIG_FILE"
