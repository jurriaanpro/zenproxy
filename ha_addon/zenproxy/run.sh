#!/usr/bin/env bash
set -euo pipefail

OPTIONS_FILE=/data/options.json
CONFIG_FILE=/data/zenproxy.yaml

virtual_sn=$(jq -r '.virtual_sn' "$OPTIONS_FILE")
virtual_product=$(jq -r '.virtual_product' "$OPTIONS_FILE")
port=$(jq -r '.port' "$OPTIONS_FILE")
leader_rotation_enabled=$(jq -r '.leader_rotation_enabled' "$OPTIONS_FILE")
leader_rotation_soc_delta_percent=$(jq -r '.leader_rotation_soc_delta_percent' "$OPTIONS_FILE")

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
    echo
    echo "leader_rotation:"
    echo "  enabled: ${leader_rotation_enabled}"
    echo "  soc_delta_percent: ${leader_rotation_soc_delta_percent}"
} > "$CONFIG_FILE"

exec /app/.venv/bin/zenproxy --config "$CONFIG_FILE"
