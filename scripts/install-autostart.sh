#!/usr/bin/env bash
# Install VscCam under /opt/vscam (or INSTALL_DIR), build the image, and enable boot autostart.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/vscam}"
USE_HOST_NETWORK="${USE_HOST_NETWORK:-auto}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo ./scripts/install-autostart.sh"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker first: https://docs.docker.com/engine/install/"
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is required (docker compose)."
  exit 1
fi

echo "Installing VscCam to ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}"
rsync -a --delete \
  --exclude '.venv' \
  --exclude '.git' \
  --exclude 'hls' \
  --exclude '.env' \
  "${ROOT}/" "${INSTALL_DIR}/"

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  if [[ -f "${ROOT}/.env" ]]; then
    cp "${ROOT}/.env" "${INSTALL_DIR}/.env"
    echo "Copied .env from project directory."
  else
    cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
    echo "Created ${INSTALL_DIR}/.env from .env.example — edit it before starting."
  fi
fi

mkdir -p "${INSTALL_DIR}/hls"

cd "${INSTALL_DIR}"
chmod +x scripts/docker-build.sh
./scripts/docker-build.sh

# systemd unit
SERVICE_FILE="/etc/systemd/system/vscam.service"
is_wsl() {
  grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null
}

if [[ "${USE_HOST_NETWORK}" == "auto" ]]; then
  if is_wsl; then
    COMPOSE_ENV='Environment=COMPOSE_FILE=docker-compose.yml:docker-compose.wsl.yml'
  elif [[ "$(uname -s)" == "Linux" ]]; then
    COMPOSE_ENV='Environment=COMPOSE_FILE=docker-compose.yml:docker-compose.lan.yml'
  else
    COMPOSE_ENV='Environment=COMPOSE_FILE=docker-compose.yml'
  fi
else
  if [[ "${USE_HOST_NETWORK}" == "1" || "${USE_HOST_NETWORK}" == "true" ]]; then
    COMPOSE_ENV='Environment=COMPOSE_FILE=docker-compose.yml:docker-compose.lan.yml'
  else
    COMPOSE_ENV='Environment=COMPOSE_FILE=docker-compose.yml'
  fi
fi

sed \
  -e "s|WorkingDirectory=/opt/vscam|WorkingDirectory=${INSTALL_DIR}|" \
  -e "s|Environment=COMPOSE_FILE=docker-compose.yml:docker-compose.lan.yml|${COMPOSE_ENV}|" \
  "${INSTALL_DIR}/deploy/vscam.service" > "${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable vscam.service
systemctl restart vscam.service

echo ""
echo "VscCam installed."
echo "  Directory:  ${INSTALL_DIR}"
echo "  Web UI:       http://localhost:8765"
echo "  Status:       systemctl status vscam"
echo "  Logs:         journalctl -u vscam -f"
echo "  Docker logs:  docker compose -f ${INSTALL_DIR}/docker-compose.yml logs -f"
