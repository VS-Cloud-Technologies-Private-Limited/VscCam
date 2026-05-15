#!/usr/bin/env bash
set -euo pipefail
# Bind on all interfaces so the player is reachable at http://<HOST_IP>:8765 on your LAN.
HOST="${UVICORN_HOST:-0.0.0.0}"
PORT="${UVICORN_PORT:-8765}"
exec uvicorn server:app --host "${HOST}" --port "${PORT}"
