#!/usr/bin/env bash
set -euo pipefail

OPTIONS_FILE=/data/options.json
CONFIG_FILE=/data/zenproxy.yaml

virtual_sn=$(jq -r '.virtual_sn' "$OPTIONS_FILE")
virtual_product=$(jq -r '.virtual_product' "$OPTIONS_FILE")
port=$(jq -r '.port' "$OPTIONS_FILE")

# Home Assistant Supervisor does not inject MQTT service credentials as
# environment variables for an addon declaring `services: [mqtt:want]`. The
# addon must instead ask the Supervisor's Services API directly. SUPERVISOR_TOKEN
# is auto-injected by Supervisor for authenticating to that API. If no MQTT
# broker is configured in HA (or the call fails for any other reason), fall
# back to rendering a config with no `mqtt:` block, just like before.
mqtt_json=""
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    mqtt_json=$(curl -fsS \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
        http://supervisor/services/mqtt || true)
fi

mqtt_host=""
if [ -n "$mqtt_json" ]; then
    mqtt_host=$(echo "$mqtt_json" | jq -r '.data.host // empty')
fi

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
    if [ -n "$mqtt_host" ]; then
        mqtt_port=$(echo "$mqtt_json" | jq -r '.data.port // 1883')
        mqtt_username=$(echo "$mqtt_json" | jq -r '.data.username // empty')
        mqtt_password=$(echo "$mqtt_json" | jq -r '.data.password // empty')

        echo
        echo "mqtt:"
        # host/username/password come from the Supervisor, not the user, and
        # can contain YAML-significant characters -- quote them safely via jq
        # rather than interpolating raw. port is already numeric.
        echo "  host: $(jq -Rn --arg v "$mqtt_host" '$v')"
        echo "  port: ${mqtt_port}"
        if [ -n "$mqtt_username" ]; then
            echo "  username: $(jq -Rn --arg v "$mqtt_username" '$v')"
        fi
        if [ -n "$mqtt_password" ]; then
            echo "  password: $(jq -Rn --arg v "$mqtt_password" '$v')"
        fi
    fi
} > "$CONFIG_FILE"

exec /app/.venv/bin/zenproxy --config "$CONFIG_FILE"
