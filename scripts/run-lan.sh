#!/usr/bin/env bash
# Run VscCam on the LAN (host network). Open http://192.168.60.51:8765 from other devices.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Create .env first: cp .env.example .env"
  exit 1
fi

# shellcheck disable=SC1091
source .env 2>/dev/null || true
HOST_IP="${HOST_IP:-192.168.60.51}"

echo "LAN mode — player URL: http://${HOST_IP}:8765"
echo "Ensure HOST_IP in .env matches this machine's Wi‑Fi/Ethernet IP."
echo ""

docker compose -f docker-compose.yml -f docker-compose.lan.yml up -d --build "$@"

echo ""
echo "Open from this PC:    http://127.0.0.1:8765"
echo "Open from phone/TV:  http://${HOST_IP}:8765"
