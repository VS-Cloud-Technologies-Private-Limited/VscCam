#!/usr/bin/env bash
# Build the VscCam Docker image locally.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IMAGE="${IMAGE:-ghcr.io/vs-cloud-technologies-private-limited/vscam}"
TAG="${TAG:-latest}"

echo "Building ${IMAGE}:${TAG} ..."
docker build -t "${IMAGE}:${TAG}" .

echo ""
echo "Built: ${IMAGE}:${TAG}"
echo "Run:   docker compose up -d"
echo "Or:    docker run --rm --network host --env-file .env -v \"\$(pwd)/hls:/app/hls\" ${IMAGE}:${TAG}"
