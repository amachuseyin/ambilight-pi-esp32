#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
  source "$SCRIPT_DIR/venv/bin/activate"
fi

iwconfig wlan0 power off 2>/dev/null || true

python3 -u server.py --ws-port 8765 --http-port 8080 --dir "$SCRIPT_DIR" &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sleep 3

exec python3 -u capture.py --config "$SCRIPT_DIR/config.json"
