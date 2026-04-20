#!/bin/sh
set -e

CONFIG=/data/options.json

if [ ! -f "$CONFIG" ]; then
    echo "[ERROR] No config file found at $CONFIG"
    exit 1
fi

export SB_TOKEN=$(python3 -c "import json; print(json.load(open('$CONFIG'))['token'])")
export SB_SECRET=$(python3 -c "import json; print(json.load(open('$CONFIG'))['secret'])")
export API_PORT=$(python3 -c "import json; print(json.load(open('$CONFIG'))['api_port'])")
export POLL_INTERVAL=$(python3 -c "import json; print(json.load(open('$CONFIG'))['poll_interval'])")

echo "[INFO] SwitchBot Direct API Bridge v0.1.0"
echo "[INFO] API port: ${API_PORT}"
echo "[INFO] Poll interval: ${POLL_INTERVAL}s"

exec python3 /app/server.py
