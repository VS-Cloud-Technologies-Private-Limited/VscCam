#!/usr/bin/env bash
# Rebuild and restart VscCam (use after code fixes). Run from project dir or /opt/vscam.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
  COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.wsl.yml)
  echo "WSL2: using bridge + port 8765 (open http://\${HOST_IP}:8765 from Windows LAN)"
else
  COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.lan.yml)
  echo "Linux LAN: using host network"
fi

docker compose "${COMPOSE_FILES[@]}" up -d --build --remove-orphans
echo ""
curl -sf http://127.0.0.1:8765/api/status | python3 -m json.tool 2>/dev/null || echo "Waiting for http://127.0.0.1:8765 …"
