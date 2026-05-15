# VscCam

[![VS Cloud Technologies](https://img.shields.io/badge/org-VS%20Cloud%20Technologies-181717?logo=github)](https://github.com/VS-Cloud-Technologies-Private-Limited)

Discover and play **CP Plus** (and other) Wi‑Fi camera streams on your local network. Includes RTSP/ONVIF discovery, a low‑latency web player, pop‑out window, and always‑on‑top Picture‑in‑Picture.

## Features

- **Network discovery** — scan the LAN for RTSP cameras (CP Plus mode, ONVIF, port-range probe)
- **ONVIF** — resolve the correct stream path (e.g. `/live/channel0` on CP Plus)
- **Web player** — MJPEG live view (low latency) with HLS fallback
- **Player controls** — play/pause, restart, fullscreen, pop‑out, always on top (PiP)

## Requirements

- Python 3.10+
- Camera on the same network as the machine running VscCam
- Credentials in `.env` (see [Configuration](#configuration))

FFmpeg is bundled via `imageio-ffmpeg`; you do not need a system install.

## Quick start (local)

```bash
git clone https://github.com/VS-Cloud-Technologies-Private-Limited/VscCam.git
cd VscCam
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your camera IP, username, and password

uvicorn server:app --host 0.0.0.0 --port 8765
```

Open **http://localhost:8765**

## Discover your camera

If you do not know the camera IP or RTSP port:

```bash
python discover_rtsp_cameras.py --cp-plus --use-windows-lan
```

| Flag | Purpose |
|------|---------|
| `--cp-plus` | CP Plus / Dahua ports and RTSP paths |
| `--use-windows-lan` | Under WSL2, scan the Windows host LAN |
| `--network 192.168.1.0/24` | Scan a specific subnet |
| `--port-range 5000-10000` | Find non‑standard RTSP ports (e.g. **5543**) |

Copy the reported host, port, and path into `.env`.

## Configuration

Create `.env` from `.env.example`:

| Variable | Description |
|----------|-------------|
| `USERNAME` / `PASSWORD` | Camera login |
| `RTSP_HOST` | Camera IP |
| `RTSP_PORT` | RTSP port (often `5543` on CP Plus, not `554`) |
| `RTSP_PATH` | Stream path (`/live/channel0` main, `/live/channel1` sub) |
| `ONVIF_PORT` | ONVIF HTTP port (often `8000`) |
| `ONVIF_ENABLED` | `true` to discover path via ONVIF |
| `LOW_LATENCY` | `true` = MJPEG `/api/live` (default); `false` = HLS only |
| `STREAM_FPS` | MJPEG frame rate cap (default `20`) |

**CP Plus notes**

- ONVIF is usually on port **8000** (same as the Android ONVIF app).
- Correct path is often `/live/channel0`, not `/cam/realmonitor?...`.
- Only **one** RTSP client at a time — close the CP Plus app or VLC before using the web player.

Test in VLC:

```text
rtsp://USER:PASS@HOST:PORT/live/channel0
```

## Docker

Build and run with Docker Compose:

```bash
cp .env.example .env
# Edit .env

docker compose up --build -d
```

Open **http://localhost:8765**

### LAN access from the container

The container must reach your camera on the local network.

- **Linux:** uncomment `network_mode: host` in `docker-compose.yml` and remove the `ports` section, then `docker compose up --build -d`. Use **http://localhost:8765**.
- **Bridge mode (default):** works if Docker can route to the camera subnet (typical on the same LAN).

Run discovery inside the container (host network on Linux):

```bash
docker compose run --rm vscam python discover_rtsp_cameras.py --cp-plus
```

Plain Docker:

```bash
docker build -t vscam .
docker run --rm -p 8765:8765 --env-file .env -v "$(pwd)/hls:/app/hls" vscam
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /` | Web player |
| `GET /popout` | Pop‑out player |
| `GET /api/status` | Stream state, VLC URL, help |
| `GET /api/live` | Low‑latency MJPEG stream |
| `GET /hls/playlist.m3u8` | HLS playlist (when enabled) |
| `POST /api/restart` | Restart stream (`?hls=true` for HLS mode) |

## Project layout

```text
VscCam/
├── server.py                 # FastAPI web server
├── config.py                 # .env loading, ONVIF, RTSP URL helpers
├── discover_rtsp_cameras.py  # LAN / RTSP discovery CLI
├── static/                   # Web UI (index.html, popout.html)
├── hls/                      # Generated HLS segments (gitignored)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| Black screen | Hard refresh; use Chrome/Edge; check `/api/status` |
| “Waiting for camera” | Open live view in the CP Plus app once, then **Restart** |
| High delay | Keep `LOW_LATENCY=true`; close other RTSP clients |
| Discovery finds nothing (WSL) | Use `--use-windows-lan` or set `--network` manually |
| Pop‑out blocked | Allow pop‑ups for localhost |
| Always on top missing | Chrome/Edge; Document PiP for live canvas mode |

## License

Use and modify as needed for your own setup.
