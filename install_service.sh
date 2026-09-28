#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${BASE_DIR}/.venv"
PYTHON="${VENV_DIR}/bin/python"
SERVICE_NAME="kokoro-wyoming"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ ! -x "${PYTHON}" ]]; then
    echo "ERROR: virtual environment not found:"
    echo "  ${VENV_DIR}"
    echo
    echo "Run ./setup.sh first."
    exit 1
fi

if [[ ! -f "${BASE_DIR}/wyoming_server.py" ]]; then
    echo "ERROR: wyoming_server.py not found."
    exit 1
fi

if [[ ! -f "${BASE_DIR}/wyoming.json" ]]; then
    echo "ERROR: wyoming.json not found."
    exit 1
fi

cat > "${SERVICE_FILE}" <<UNIT
[Unit]
Description=Kokoro Russian Wyoming TTS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${BASE_DIR}
ExecStart=${PYTHON} ${BASE_DIR}/wyoming_server.py
Restart=on-failure
RestartSec=2

Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo
echo "Service installed:"
echo "  ${SERVICE_FILE}"
echo
echo "Project:"
echo "  ${BASE_DIR}"
echo
echo "Python:"
echo "  ${PYTHON}"
echo
echo "Start:"
echo "  systemctl start ${SERVICE_NAME}"
echo
echo "Status:"
echo "  systemctl status ${SERVICE_NAME}"
echo
echo "Logs:"
echo "  journalctl -u ${SERVICE_NAME} -f"
