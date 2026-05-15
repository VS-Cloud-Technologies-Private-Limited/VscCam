#!/usr/bin/env python3
"""Web player: RTSP→HLS when available, MJPEG snapshot fallback."""

from __future__ import annotations

import atexit
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Iterator

import imageio_ffmpeg
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import build_rtsp_url, discover_onvif_stream, discover_rtsp_path, load_dotenv, snapshot_url
from config import credentials as get_credentials

ROOT = Path(__file__).resolve().parent
HLS_DIR = ROOT / "hls"
STATIC_DIR = ROOT / "static"

ffmpeg_proc: subprocess.Popen[bytes] | None = None
active_rtsp_url: str = ""
active_rtsp_path: str | None = None
last_ffmpeg_error: str = ""
_force_hls: bool = False
_lock = threading.Lock()

env = load_dotenv()


def mask_url(url: str) -> str:
    if "@" in url:
        return "rtsp://" + url.split("@", 1)[1]
    return url


def stop_ffmpeg() -> None:
    global ffmpeg_proc
    with _lock:
        if ffmpeg_proc is None:
            return
        ffmpeg_proc.terminate()
        try:
            ffmpeg_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ffmpeg_proc.kill()
        ffmpeg_proc = None


def start_ffmpeg(rtsp_url: str) -> bool:
    global ffmpeg_proc, active_rtsp_url, last_ffmpeg_error
    stop_ffmpeg()
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in HLS_DIR.glob("*.ts"):
        stale.unlink(missing_ok=True)
    (HLS_DIR / "playlist.m3u8").unlink(missing_ok=True)

    playlist = HLS_DIR / "playlist.m3u8"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-g",
        "15",
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "3",
        "-hls_flags",
        "delete_segments+append_list+omit_endlist+split_by_time",
        str(playlist),
    ]
    with _lock:
        ffmpeg_proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stderr=subprocess.PIPE,
        )
        active_rtsp_url = rtsp_url

    time.sleep(2)
    with _lock:
        if ffmpeg_proc.poll() is not None:
            err = (ffmpeg_proc.stderr.read() if ffmpeg_proc.stderr else b"").decode(errors="replace")
            last_ffmpeg_error = err.strip()[-500:] if err else "ffmpeg exited"
            ffmpeg_proc = None
            return False
    return True


def playlist_ready() -> bool:
    playlist = HLS_DIR / "playlist.m3u8"
    if not playlist.is_file() or playlist.stat().st_size == 0:
        return False
    return (time.time() - playlist.stat().st_mtime) < 30


def low_latency_enabled() -> bool:
    if _force_hls:
        return False
    return env.get("LOW_LATENCY", "true").lower() not in ("0", "false", "no")


def try_start_stream() -> None:
    global active_rtsp_path, active_rtsp_url, last_ffmpeg_error
    onvif_path, profile = discover_onvif_stream(env)
    path = discover_rtsp_path(env)
    active_rtsp_path = path
    url = build_rtsp_url(env, path=path)
    active_rtsp_url = url

    if low_latency_enabled():
        # Camera allows one RTSP client; MJPEG /api/live uses RTSP directly.
        stop_ffmpeg()
        last_ffmpeg_error = "" if path else "No RTSP path found (try ONVIF on port 8000)"
        return

    if start_ffmpeg(url):
        last_ffmpeg_error = ""
    elif onvif_path:
        last_ffmpeg_error = f"ONVIF found {onvif_path} but ffmpeg could not start"
    elif not path:
        last_ffmpeg_error = "No RTSP path found (try ONVIF on port 8000)"
    elif profile:
        last_ffmpeg_error = f"Profile {profile} at {path} failed"


def _digest_opener() -> urllib.request.OpenerDirector:
    user, password = get_credentials(env)
    host = env.get("RTSP_HOST", "192.168.1.100")
    http_port = env.get("HTTP_PORT", "8000")
    base = f"http://{host}:{http_port}/"
    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, base, user, password)
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(manager))


def fetch_snapshot() -> bytes | None:
    try:
        opener = _digest_opener()
        with opener.open(snapshot_url(env), timeout=3) as resp:
            data = resp.read()
            return data if len(data) > 100 else None
    except OSError:
        return None


def mjpeg_generator() -> Iterator[bytes]:
    boundary = b"frame"
    while True:
        frame = fetch_snapshot()
        if frame:
            yield (
                b"--" + boundary + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                + frame + b"\r\n"
            )
        time.sleep(0.15)


def _low_latency_input_args() -> list[str]:
    return [
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-rtsp_transport",
        "tcp",
    ]


def rtsp_mjpeg_generator(rtsp_url: str) -> Iterator[bytes]:
    """Stream MJPEG straight from RTSP (~1–2s latency vs ~8s+ for HLS)."""
    global last_ffmpeg_error
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    fps = env.get("STREAM_FPS", "15")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-an",
        "-c:v",
        "mjpeg",
        "-q:v",
        env.get("MJPEG_QUALITY", "8"),
        "-r",
        fps,
        "-f",
        "mpjpeg",
        "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if chunk:
                yield chunk
                continue
            if proc.poll() is not None:
                err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")
                if err:
                    last_ffmpeg_error = err.strip()[-300:]
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def background_retry() -> None:
    while True:
        time.sleep(15)
        if low_latency_enabled():
            if active_rtsp_path:
                continue
            try_start_stream()
            continue
        if playlist_ready():
            continue
        if ffmpeg_proc is not None and ffmpeg_proc.poll() is None:
            continue
        try_start_stream()


app = FastAPI(title="VscCam", version="1.0.0")


@app.on_event("startup")
def on_startup() -> None:
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    try_start_stream()
    threading.Thread(target=background_retry, daemon=True).start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_ffmpeg()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/popout")
def popout() -> FileResponse:
    return FileResponse(STATIC_DIR / "popout.html")


def rtsp_server_up() -> bool:
    import socket

    host = env.get("RTSP_HOST", "192.168.1.100")
    port = int(env.get("RTSP_PORT", "5543"))
    try:
        with socket.create_connection((host, port), timeout=2) as sock:
            sock.sendall(b"OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
            data = sock.recv(256)
        return data.upper().startswith(b"RTSP/") and b"200" in data
    except OSError:
        return False


@app.get("/api/status")
def status() -> JSONResponse:
    running = ffmpeg_proc is not None and ffmpeg_proc.poll() is None
    snap = fetch_snapshot()
    host = env.get("RTSP_HOST", "192.168.1.100")
    port = env.get("RTSP_PORT", "5543")
    path = active_rtsp_path or env.get("RTSP_PATH", "/cam/realmonitor?channel=1&subtype=0")
    vlc_url = f"rtsp://{host}:{port}{path}"
    return JSONResponse(
        {
            "ffmpeg_running": running,
            "playlist_ready": playlist_ready(),
            "rtsp_server_up": rtsp_server_up(),
            "rtsp_path": active_rtsp_path,
            "stream_url": mask_url(active_rtsp_url) if active_rtsp_url else vlc_url,
            "vlc_url": vlc_url,
            "hls_playlist": "/hls/playlist.m3u8",
            "live_url": "/api/live",
            "low_latency": low_latency_enabled(),
            "mjpeg_url": "/api/mjpeg",
            "snapshot_ok": snap is not None,
            "error": last_ffmpeg_error or None,
            "onvif_profile": discover_onvif_stream(env)[1],
            "help": [
                "This CP Plus camera uses ONVIF on port 8000 (same as your Android ONVIF app).",
                "Stream path: /live/channel0 (main) or /live/channel1 (sub).",
                f"Test in VLC: {vlc_url}",
            ],
        }
    )


@app.post("/api/restart")
def restart(hls: bool = False) -> JSONResponse:
    global _force_hls
    _force_hls = hls
    try_start_stream()
    return JSONResponse({"ok": True, "hls": not low_latency_enabled()})


@app.get("/api/mjpeg")
def mjpeg() -> StreamingResponse:
    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/live")
def live() -> StreamingResponse:
    url = active_rtsp_url or build_rtsp_url(env, path=active_rtsp_path)
    return StreamingResponse(
        rtsp_mjpeg_generator(url),
        media_type="multipart/x-mixed-replace; boundary=ffmpeg",
    )


@app.get("/api/snapshot", response_model=None)
def snapshot() -> Response:
    data = fetch_snapshot()
    if not data:
        return JSONResponse({"error": "snapshot unavailable"}, status_code=503)
    return Response(content=data, media_type="image/jpeg")


app.mount("/hls", StaticFiles(directory=HLS_DIR), name="hls")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

atexit.register(stop_ffmpeg)
signal.signal(signal.SIGTERM, lambda *_: stop_ffmpeg())
