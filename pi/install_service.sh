#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="ambilight-led.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

cat <<UNIT | sudo tee "$SERVICE_PATH" >/dev/null
[Unit]
Description=Ambilight LED capture and local dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=${SCRIPT_DIR}
ExecStart=/bin/bash ${SCRIPT_DIR}/run_ambilight.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "Installed and started ${SERVICE_NAME}"
echo "Status: sudo systemctl status ${SERVICE_NAME}"
echo "Logs:   journalctl -u ${SERVICE_NAME} -f"
