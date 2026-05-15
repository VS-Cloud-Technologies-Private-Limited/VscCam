from __future__ import annotations

import base64
import os
import socket
from pathlib import Path
from urllib.parse import quote

ENV_KEYS = (
    "USERNAME",
    "PASSWORD",
    "RTSP_HOST",
    "RTSP_PORT",
    "RTSP_PATH",
    "RTSP_URL",
    "ONVIF_ENABLED",
    "ONVIF_PORT",
    "ONVIF_PROFILE",
    "HTTP_PORT",
    "HOST_IP",
    "LOW_LATENCY",
    "STREAM_FPS",
    "SNAPSHOT_PATH",
    "SNAPSHOT_URL",
    "WEB_BASE_URL",
    "UVICORN_HOST",
    "UVICORN_PORT",
)

RTSP_PATH_CANDIDATES = (
    "/live/channel0",
    "/live/channel1",
    "/cam/realmonitor?channel=1&subtype=0",
    "/cam/realmonitor?channel=1&subtype=1",
    "/live/av0",
    "/live/av1",
    "/VideoInput/1/h264/1",
    "/VideoInput/1/mpeg4/1",
    "/live",
    "/stream",
    "/stream1",
    "/pusher",
    "/test.mp4",
    "/videodevice",
    "/h264/ch1/main/av_stream",
    "/Streaming/Channels/101",
    "/",
)


def load_dotenv(env_path: Path | None = None) -> dict[str, str]:
    if env_path is None:
        script_dir = Path(__file__).resolve().parent
        for candidate in (script_dir / ".env", Path.cwd() / ".env"):
            if candidate.is_file():
                env_path = candidate
                break
    values: dict[str, str] = {}
    if env_path is not None and env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ENV_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def credentials(env: dict[str, str]) -> tuple[str, str]:
    return (
        env.get("USERNAME", "admin"),
        env.get("PASSWORD", ""),
    )


def rtsp_auth_header(username: str, password: str) -> str:
    if not username:
        return ""
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Authorization: Basic {token}\r\n"


def probe_rtsp_path(host: str, port: int, path: str, username: str, password: str, timeout: float = 2.0) -> bool:
    if not path.startswith("/"):
        path = "/" + path
    request = (
        f"DESCRIBE rtsp://{host}:{port}{path} RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "Accept: application/sdp\r\n"
        f"{rtsp_auth_header(username, password)}"
        "User-Agent: VscCam/1.0\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            data = sock.recv(1024)
    except OSError:
        return False
    return b" 200 " in data.split(b"\r\n", 1)[0]


def discover_onvif_stream(env: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    """Return (rtsp_path, profile_name) via ONVIF GetStreamUri, or (None, None)."""
    env = env or load_dotenv()
    if env.get("ONVIF_ENABLED", "true").lower() in ("0", "false", "no"):
        return None, None

    host = env.get("RTSP_HOST", "192.168.1.100")
    onvif_port = int(env.get("ONVIF_PORT", env.get("HTTP_PORT", "8000")))
    username, password = credentials(env)
    profile_index = int(env.get("ONVIF_PROFILE", "0"))

    try:
        from onvif import ONVIFCamera

        cam = ONVIFCamera(host, onvif_port, username, password)
        media = cam.create_media_service()
        profiles = media.GetProfiles()
        if not profiles:
            return None, None
        profile = profiles[min(profile_index, len(profiles) - 1)]
        uri = media.GetStreamUri(
            {
                "StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
                "ProfileToken": profile.token,
            }
        )
        from urllib.parse import urlparse

        parsed = urlparse(uri.Uri)
        return parsed.path or None, getattr(profile, "Name", profile.token)
    except Exception:
        return None, None


def discover_rtsp_path(env: dict[str, str] | None = None) -> str | None:
    env = env or load_dotenv()
    host = env.get("RTSP_HOST", "192.168.1.100")
    port = int(env.get("RTSP_PORT", "5543"))
    username, password = credentials(env)

    configured = env.get("RTSP_PATH", "auto")
    if configured and configured.lower() not in ("auto", "onvif", ""):
        if probe_rtsp_path(host, port, configured, username, password):
            return configured
        return configured

    onvif_path, _ = discover_onvif_stream(env)
    if onvif_path and probe_rtsp_path(host, port, onvif_path, username, password):
        return onvif_path

    for path in RTSP_PATH_CANDIDATES:
        if probe_rtsp_path(host, port, path, username, password):
            return path
    return onvif_path


def build_rtsp_url(env: dict[str, str] | None = None, path: str | None = None) -> str:
    env = env or load_dotenv()
    if url := env.get("RTSP_URL"):
        return url

    host = env.get("RTSP_HOST", "192.168.1.100")
    port = env.get("RTSP_PORT", "5543")
    if path is None:
        configured = env.get("RTSP_PATH", "auto")
        if configured.lower() in ("auto", "onvif", ""):
            path = discover_rtsp_path(env) or "/live/channel0"
        else:
            path = configured
    if not path.startswith("/"):
        path = "/" + path

    username, password = credentials(env)
    cred = ""
    if username:
        cred = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"rtsp://{cred}{host}:{port}{path}"


def snapshot_url(env: dict[str, str] | None = None) -> str:
    env = env or load_dotenv()
    if url := env.get("SNAPSHOT_URL"):
        return url
    host = env.get("RTSP_HOST", "192.168.1.100")
    http_port = env.get("HTTP_PORT", "8000")
    path = env.get("SNAPSHOT_PATH", "/tmpfs/auto.jpg")
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{host}:{http_port}{path}"
