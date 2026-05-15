#!/usr/bin/env python3
"""
Scan the local network for hosts that expose RTSP (typically TCP 554 / 8554).

Uses parallel TCP connect checks, then verifies RTSP with an OPTIONS request.
Optionally runs ONVIF WS-Discovery (UDP multicast) for cameras that advertise there.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import re
import socket
import struct
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

DEFAULT_RTSP_PORTS = (554, 8554)
# Common RTSP / IP-camera ports (CP Plus, Dahua, Hikvision, generic)
KNOWN_RTSP_PORTS = (
    554, 5544, 5554,
    8554, 9554, 10554,
    1935, 3000, 3454, 5000, 5001, 5002, 5004,
    6036, 6037, 7070, 7447,
    8000, 8001, 8008, 8080, 8081, 8082, 8086,
    8888, 8899, 9000, 9100,
    10000, 10001, 10002, 10080, 10081, 10500,
    34567, 37777,
)
# Non-RTSP ports used only for camera fingerprinting / HTTP config
CP_PLUS_EXTRA_PORTS = (37777, 80, 8080, 8000)
# Default wide scan on hosts that look like cameras (when --discover-ports)
DEFAULT_PORT_RANGE = (5000, 10000)
CP_PLUS_RTSP_PATHS = (
    "/cam/realmonitor?channel=1&subtype=0",
    "/cam/realmonitor?channel=1&subtype=1",
    "/VideoInput/1/mpeg4/1",
    "/VideoInput/1/h264/1",
    "/live",
    "/stream1",
)
CP_PLUS_HTTP_MARKERS = ("cp plus", "cp-plus", "dahua", "realmonitor", "aditya")
ONVIF_MULTICAST = ("239.255.255.250", 3702)
CONNECT_TIMEOUT = 0.35
RTSP_TIMEOUT = 1.5
ONVIF_TIMEOUT = 3.0
MAX_WORKERS = 128

RTSP_REQUEST = (
    "OPTIONS * RTSP/1.0\r\n"
    "CSeq: 1\r\n"
    "User-Agent: VscCam-RTSP-Discovery/1.0\r\n"
    "\r\n"
).encode("ascii")


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


@dataclass(frozen=True)
class RtspCamera:
    host: str
    port: int
    rtsp_url: str
    server: str | None = None
    public: str | None = None
    source: str = "port-scan"
    suggested_urls: tuple[str, ...] = ()
    http_hint: str | None = None


def load_dotenv(env_path: Path | None = None) -> dict[str, str]:
    if env_path is None:
        script_dir = Path(__file__).resolve().parent
        for candidate in (script_dir / ".env", Path.cwd() / ".env"):
            if candidate.is_file():
                env_path = candidate
                break
    if env_path is None or not env_path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def credentials_from_env(env: dict[str, str]) -> Credentials:
    username = env.get("USERNAME") or env.get("RTSP_USERNAME") or "admin"
    password = env.get("PASSWORD") or env.get("RTSP_PASSWORD") or ""
    return Credentials(username=username, password=password)


def get_default_interface_ip() -> str:
    """Return the IPv4 address used for outbound traffic."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]


def guess_local_network(ip: str, prefix_len: int = 24) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network(f"{ip}/{prefix_len}", strict=False)


def get_windows_lan_network() -> ipaddress.IPv4Network | None:
    """Best-effort: read Windows host IPv4 on a physical LAN (WSL2)."""
    import subprocess

    try:
        out = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-NetIPAddress -AddressFamily IPv4 | "
                "Where-Object { $_.InterfaceAlias -notmatch 'vEthernet|Loopback|Tailscale|WSL' "
                "-and $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' "
                "-and $_.IPAddress -match '^192\\.168\\.|^10\\.' } | "
                "Select-Object -First 1 -ExpandProperty IPAddress",
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    ip = (out.stdout or "").strip()
    if not ip:
        return None
    try:
        return guess_local_network(ip)
    except ValueError:
        return None


def iter_hosts(network: ipaddress.IPv4Network) -> Iterable[str]:
    for host in network.hosts():
        yield str(host)


def parse_ports_spec(spec: str) -> tuple[int, ...]:
    """Parse '554,8554,5000-5010,8000-8005' into a sorted unique port tuple."""
    ports: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s.strip()), int(end_s.strip())
            if start > end:
                start, end = end, start
            if end - start > 50_000:
                raise ValueError(f"Port range too large: {part}")
            ports.update(range(start, end + 1))
        else:
            port = int(part)
            if not 1 <= port <= 65535:
                raise ValueError(f"Invalid port: {port}")
            ports.add(port)
    return tuple(sorted(ports))


def tcp_port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_open_ports(
    host: str, ports: tuple[int, ...], timeout: float, workers: int = 32
) -> tuple[int, ...]:
    open_ports: list[int] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(ports) or 1)) as pool:
        futures = {pool.submit(tcp_port_open, host, port, timeout): port for port in ports}
        for future in as_completed(futures):
            if future.result():
                open_ports.append(futures[future])
    return tuple(sorted(open_ports))


def host_responsive(host: str, timeout: float) -> bool:
    import subprocess

    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "1", host],
            capture_output=True,
            timeout=timeout + 1,
        )
        if result.returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    for port in (80, 443, 554, 8000, 8080, 37777):
        if tcp_port_open(host, port, timeout):
            return True
    return False


def rtsp_auth_header(creds: Credentials) -> str:
    if not creds.username:
        return ""
    token = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode("ascii")
    return f"Authorization: Basic {token}\r\n"


def cp_plus_urls(host: str, port: int, creds: Credentials) -> tuple[str, ...]:
    user = quote(creds.username, safe="")
    password = quote(creds.password, safe="")
    cred = f"{user}:{password}@" if creds.username else ""
    base = f"rtsp://{cred}{host}:{port}"
    return tuple(f"{base}{path}" for path in CP_PLUS_RTSP_PATHS)


def probe_rtsp(host: str, port: int, timeout: float, creds: Credentials) -> RtspCamera | None:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(RTSP_REQUEST)
            data = sock.recv(4096)
    except OSError:
        return None

    if not data or not data.upper().startswith(b"RTSP/"):
        return None

    text = data.decode("utf-8", errors="replace")
    headers: dict[str, str] = {}
    for line in text.split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()

    server = headers.get("server")
    public = headers.get("public")
    url = f"rtsp://{host}:{port}/"
    return RtspCamera(
        host=host,
        port=port,
        rtsp_url=url,
        server=server,
        public=public,
        source="port-scan",
        suggested_urls=cp_plus_urls(host, port, creds),
    )


def probe_rtsp_describe(
    host: str, port: int, path: str, timeout: float, creds: Credentials
) -> bool:
    """Return True if DESCRIBE gets 200, or 401 when no credentials were provided."""
    request = (
        f"DESCRIBE rtsp://{host}:{port}{path} RTSP/1.0\r\n"
        "CSeq: 2\r\n"
        "Accept: application/sdp\r\n"
        "User-Agent: VscCam-RTSP-Discovery/1.0\r\n"
        f"{rtsp_auth_header(creds)}"
        "\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            data = sock.recv(512)
    except OSError:
        return False
    if not data.upper().startswith(b"RTSP/"):
        return False
    status = data.split(b"\r\n", 1)[0]
    if b" 200 " in status:
        return True
    return b" 401 " in status and not creds.password


def refine_cp_plus_camera(cam: RtspCamera, rtsp_timeout: float, creds: Credentials) -> RtspCamera:
    working: list[str] = []
    for path in CP_PLUS_RTSP_PATHS:
        if probe_rtsp_describe(cam.host, cam.port, path, rtsp_timeout, creds):
            user = quote(creds.username, safe="")
            password = quote(creds.password, safe="")
            cred = f"{user}:{password}@" if creds.username else ""
            working.append(f"rtsp://{cred}{cam.host}:{cam.port}{path}")
    if not working:
        return RtspCamera(
            host=cam.host,
            port=cam.port,
            rtsp_url=cam.rtsp_url,
            server=cam.server,
            public=cam.public,
            source=cam.source,
            suggested_urls=cp_plus_urls(cam.host, cam.port, creds),
            http_hint=cam.http_hint,
        )
    best = working[0]
    return RtspCamera(
        host=cam.host,
        port=cam.port,
        rtsp_url=best,
        server=cam.server,
        public=cam.public,
        source=cam.source,
        suggested_urls=tuple(dict.fromkeys(working + list(cp_plus_urls(cam.host, cam.port, creds)))),
        http_hint=cam.http_hint,
    )


def probe_http_rtsp_port(host: str, timeout: float, creds: Credentials) -> int | None:
    """Ask Dahua/CP Plus HTTP API for configured RTSP port."""
    auth = rtsp_auth_header(creds)
    paths = (
        "/cgi-bin/configManager.cgi?action=getConfig&name=RTSP",
        "/cgi-bin/magicBox.cgi?action=getSerialNo",
    )
    for path in paths:
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
            f"{auth}Connection: close\r\nUser-Agent: VscCam-RTSP-Discovery/1.0\r\n\r\n"
        ).encode("ascii")
        for http_port in (80, 8080, 8000):
            try:
                with socket.create_connection((host, http_port), timeout=timeout) as sock:
                    sock.settimeout(timeout)
                    sock.sendall(req)
                    data = sock.recv(16384).decode("utf-8", errors="replace")
            except OSError:
                continue
            for pattern in (
                r"table\.RTSP\.Port=(\d+)",
                r"RtspPort[\"']?\s*[:=]\s*[\"']?(\d+)",
                r"rtspport[\"']?\s*[:=]\s*[\"']?(\d+)",
                r"RTSP.*?Port.*?(\d{2,5})",
            ):
                match = re.search(pattern, data, flags=re.IGNORECASE)
                if match:
                    port = int(match.group(1))
                    if 1 <= port <= 65535:
                        return port
    return None


def probe_http_cp_plus(host: str, timeout: float) -> str | None:
    req = (
        f"GET / HTTP/1.1\r\nHost: {host}\r\n"
        "Connection: close\r\nUser-Agent: VscCam-RTSP-Discovery/1.0\r\n\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection((host, 80), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(req)
            data = sock.recv(8192)
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace").lower()
    for marker in CP_PLUS_HTTP_MARKERS:
        if marker in text:
            return f"HTTP UI looks like CP Plus/Dahua ({marker!r})"
    if "ipcam" in text or "network camera" in text:
        return "HTTP UI looks like an IP camera"
    return None


def _attach_hint(camera: RtspCamera, http_hint: str | None) -> RtspCamera:
    if not http_hint:
        return camera
    return RtspCamera(
        host=camera.host,
        port=camera.port,
        rtsp_url=camera.rtsp_url,
        server=camera.server,
        public=camera.public,
        source=camera.source,
        suggested_urls=camera.suggested_urls,
        http_hint=http_hint,
    )


def _try_rtsp_on_ports(
    host: str,
    ports: tuple[int, ...],
    rtsp_timeout: float,
    cp_plus: bool,
    creds: Credentials,
    http_hint: str | None,
    source_label: str,
) -> list[RtspCamera]:
    found: list[RtspCamera] = []
    seen_ports: set[int] = set()
    for port in ports:
        if port in seen_ports:
            continue
        seen_ports.add(port)
        camera = probe_rtsp(host, port, rtsp_timeout, creds)
        if not camera:
            continue
        camera = RtspCamera(
            host=camera.host,
            port=camera.port,
            rtsp_url=camera.rtsp_url,
            server=camera.server,
            public=camera.public,
            source=f"{source_label} (port {port})",
            suggested_urls=camera.suggested_urls,
            http_hint=http_hint,
        )
        if cp_plus:
            camera = refine_cp_plus_camera(camera, rtsp_timeout, creds)
        found.append(_attach_hint(camera, http_hint))
    return found


def scan_host(
    host: str,
    ports: tuple[int, ...],
    connect_timeout: float,
    rtsp_timeout: float,
    cp_plus: bool,
    creds: Credentials,
    discover_ports: bool,
    port_range: tuple[int, int] | None,
) -> list[RtspCamera]:
    found: list[RtspCamera] = []
    http_hint: str | None = None
    if cp_plus:
        http_hint = probe_http_cp_plus(host, connect_timeout + 1.0)

    # Ports to TCP-scan (skip plain HTTP service ports; RTSP-probe open ports only)
    scan_ports = tuple(p for p in ports if p not in (80, 8080, 8000) or not cp_plus)
    open_ports = find_open_ports(host, scan_ports, connect_timeout)
    found.extend(
        _try_rtsp_on_ports(host, open_ports, rtsp_timeout, cp_plus, creds, http_hint, "port-scan")
    )

    # HTTP config may report a custom RTSP port
    if cp_plus:
        configured = probe_http_rtsp_port(host, connect_timeout + 1.0, creds)
        if configured and configured not in {c.port for c in found}:
            found.extend(
                _try_rtsp_on_ports(
                    host,
                    (configured,),
                    rtsp_timeout,
                    cp_plus,
                    creds,
                    http_hint,
                    "http-config",
                )
            )

    # Wide port-range scan on responsive / camera-like hosts
    if discover_ports and port_range and not found:
        if host_responsive(host, connect_timeout) or http_hint:
            lo, hi = port_range
            range_ports = tuple(range(lo, hi + 1))
            range_open = find_open_ports(host, range_ports, min(connect_timeout, 0.2), workers=64)
            found.extend(
                _try_rtsp_on_ports(
                    host,
                    range_open,
                    rtsp_timeout,
                    cp_plus,
                    creds,
                    http_hint,
                    f"range {lo}-{hi}",
                )
            )

    if cp_plus and tcp_port_open(host, 37777, connect_timeout) and not found:
        urls = cp_plus_urls(host, 554, creds)
        found.append(
            RtspCamera(
                host=host,
                port=554,
                rtsp_url=urls[0],
                source="dahua-port-37777 (RTSP not verified)",
                suggested_urls=urls,
                http_hint=http_hint,
            )
        )
    if cp_plus and http_hint and not found:
        urls = cp_plus_urls(host, 554, creds)
        found.append(
            RtspCamera(
                host=host,
                port=554,
                rtsp_url=urls[0],
                source="http-hint (RTSP port not found — enable RTSP on camera)",
                suggested_urls=urls,
                http_hint=http_hint,
            )
        )
    return found


def scan_network(
    network: ipaddress.IPv4Network,
    ports: tuple[int, ...],
    workers: int,
    connect_timeout: float,
    rtsp_timeout: float,
    cp_plus: bool,
    creds: Credentials,
    discover_ports: bool,
    port_range: tuple[int, int] | None,
) -> list[RtspCamera]:
    hosts = list(iter_hosts(network))
    results: list[RtspCamera] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(hosts) or 1)) as pool:
        futures = {
            pool.submit(
                scan_host,
                host,
                ports,
                connect_timeout,
                rtsp_timeout,
                cp_plus,
                creds,
                discover_ports,
                port_range,
            ): host
            for host in hosts
        }
        for future in as_completed(futures):
            results.extend(future.result())
    results.sort(key=lambda c: (ipaddress.IPv4Address(c.host), c.port))
    return results


def _local_tag() -> str:
    return f"uuid:{uuid.uuid4()}"


def onvif_ws_discovery(timeout: float) -> list[RtspCamera]:
    """Multicast ONVIF probe; returns cameras that advertise an RTSP/XAddr."""
    probe = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>{_local_tag()}</w:MessageID>
    <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""

    cameras: dict[str, RtspCamera] = {}
    seen_addrs: set[tuple[str, int]] = set()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ttl = struct.pack("b", 2)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        sock.settimeout(0.5)
        sock.sendto(probe.encode("utf-8"), ONVIF_MULTICAST)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            text = data.decode("utf-8", errors="replace")
            for url in _extract_rtsp_urls(text):
                parsed = _parse_rtsp_url(url)
                if not parsed:
                    continue
                host, port = parsed
                key = (host, port)
                if key in seen_addrs:
                    continue
                seen_addrs.add(key)
                cameras[url] = RtspCamera(
                    host=host,
                    port=port,
                    rtsp_url=url if url.endswith("/") else url + "/",
                    source=f"onvif ({addr[0]})",
                )

    return sorted(cameras.values(), key=lambda c: (ipaddress.IPv4Address(c.host), c.port))


def _extract_rtsp_urls(payload: str) -> list[str]:
    urls = re.findall(r"rtsp://[^\s<\"']+", payload, flags=re.IGNORECASE)
    cleaned: list[str] = []
    for url in urls:
        url = url.rstrip(".,;)")
        cleaned.append(url)
    return cleaned


def _parse_rtsp_url(url: str) -> tuple[str, int] | None:
    match = re.match(r"rtsp://([^/:]+)(?::(\d+))?", url, flags=re.IGNORECASE)
    if not match:
        return None
    host = match.group(1)
    port = int(match.group(2) or 554)
    try:
        ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        return None
    return host, port


def merge_results(*groups: list[RtspCamera]) -> list[RtspCamera]:
    by_key: dict[tuple[str, int], RtspCamera] = {}
    for group in groups:
        for cam in group:
            key = (cam.host, cam.port)
            if key not in by_key:
                by_key[key] = cam
            elif cam.source.startswith("onvif") and not by_key[key].source.startswith("onvif"):
                by_key[key] = cam
    return sorted(by_key.values(), key=lambda c: (ipaddress.IPv4Address(c.host), c.port))


def print_results(cameras: list[RtspCamera], network: ipaddress.IPv4Network, cp_plus: bool) -> None:
    if not cameras:
        print(f"No RTSP cameras found on {network}.")
        if cp_plus:
            print(
                "CP Plus WiFi: enable RTSP in the CP Plus / IMOU app "
                "(Device Settings → Network → RTSP), use same Wi‑Fi, default login admin/admin."
            )
        print("Tips: ensure cameras are on the same subnet, RTSP is enabled, and try --network or --ports.")
        return

    print(f"Found {len(cameras)} RTSP endpoint(s) on {network}:\n")
    for i, cam in enumerate(cameras, 1):
        print(f"{i}. {cam.rtsp_url}")
        print(f"   Host: {cam.host}  Port: {cam.port}  Source: {cam.source}")
        if cam.http_hint:
            print(f"   {cam.http_hint}")
        if cam.server:
            print(f"   Server: {cam.server}")
        if cam.public:
            print(f"   Public methods: {cam.public}")
        if cam.suggested_urls:
            print("   CP Plus URLs:")
            for url in cam.suggested_urls[:4]:
                print(f"     {url}")
        print()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover IP cameras on the local network that support RTSP.",
    )
    parser.add_argument(
        "--network",
        metavar="CIDR",
        help="Network to scan (e.g. 192.168.1.0/24). Default: local /24 from default route.",
    )
    parser.add_argument(
        "--ports",
        default="common",
        help="Ports to scan: 'common' (wide camera list), 'minimal' (554,8554), or "
        "comma-separated / ranges e.g. 554,8554,5000-5100 (default: common).",
    )
    parser.add_argument(
        "--discover-ports",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="On responsive hosts, scan a port range and RTSP-probe every open port (default: on).",
    )
    parser.add_argument(
        "--port-range",
        metavar="START-END",
        default=f"{DEFAULT_PORT_RANGE[0]}-{DEFAULT_PORT_RANGE[1]}",
        help=f"Range for --discover-ports (default: {DEFAULT_PORT_RANGE[0]}-{DEFAULT_PORT_RANGE[1]}).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"Parallel scan threads (default: {MAX_WORKERS}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=CONNECT_TIMEOUT,
        help=f"TCP connect timeout in seconds (default: {CONNECT_TIMEOUT}).",
    )
    parser.add_argument(
        "--rtsp-timeout",
        type=float,
        default=RTSP_TIMEOUT,
        help=f"RTSP OPTIONS timeout in seconds (default: {RTSP_TIMEOUT}).",
    )
    parser.add_argument(
        "--no-onvif",
        action="store_true",
        help="Skip ONVIF WS-Discovery multicast probe.",
    )
    parser.add_argument(
        "--onvif-only",
        action="store_true",
        help="Only run ONVIF discovery (no full subnet port scan).",
    )
    parser.add_argument(
        "--cp-plus",
        action="store_true",
        help="CP Plus / Dahua mode: extra ports, HTTP fingerprint, common RTSP paths.",
    )
    parser.add_argument(
        "--use-windows-lan",
        action="store_true",
        help="Under WSL2, scan the Windows host physical LAN (e.g. 192.168.x.x).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="Path to .env with USERNAME and PASSWORD (default: ./.env next to script).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    env = load_dotenv(args.env_file)
    creds = credentials_from_env(env)

    if args.network:
        network = ipaddress.IPv4Network(args.network, strict=False)
    elif args.use_windows_lan and (win_net := get_windows_lan_network()):
        network = win_net
        print(f"Using Windows LAN:  {network}")
    else:
        local_ip = get_default_interface_ip()
        network = guess_local_network(local_ip)

    try:
        if args.ports == "common":
            ports = tuple(dict.fromkeys(DEFAULT_RTSP_PORTS + KNOWN_RTSP_PORTS))
        elif args.ports == "minimal":
            ports = DEFAULT_RTSP_PORTS
        else:
            ports = parse_ports_spec(args.ports)
        if args.cp_plus:
            ports = tuple(dict.fromkeys(ports + CP_PLUS_EXTRA_PORTS))
    except ValueError as exc:
        print(f"Invalid ports: {exc}", file=sys.stderr)
        return 2

    if not ports:
        print("At least one port is required.", file=sys.stderr)
        return 2

    port_range: tuple[int, int] | None = None
    if args.discover_ports:
        try:
            if "-" in args.port_range:
                lo_s, hi_s = args.port_range.split("-", 1)
                port_range = (int(lo_s.strip()), int(hi_s.strip()))
            else:
                port_range = DEFAULT_PORT_RANGE
        except ValueError:
            print("Invalid --port-range; use START-END e.g. 5000-10000.", file=sys.stderr)
            return 2

    print(f"Local interface IP: {get_default_interface_ip()}")
    print(f"Scanning network:   {network}")
    if creds.username:
        print(f"RTSP credentials:   {creds.username} (from .env)")
    if not args.onvif_only:
        print(f"RTSP port list:     {len(ports)} ports ({ports[0]}..{ports[-1]} etc.)")
        if args.discover_ports and port_range:
            print(f"Port-range scan:    {port_range[0]}-{port_range[1]} on responsive hosts")
        if args.cp_plus:
            print("Mode:               CP Plus / Dahua")
        print(f"Workers:            {args.workers}")
        print()

    port_scan: list[RtspCamera] = []
    if not args.onvif_only:
        print("Port scan in progress...")
        port_scan = scan_network(
            network,
            ports,
            workers=args.workers,
            connect_timeout=args.timeout,
            rtsp_timeout=args.rtsp_timeout,
            cp_plus=args.cp_plus,
            creds=creds,
            discover_ports=args.discover_ports,
            port_range=port_range,
        )

    onvif: list[RtspCamera] = []
    if not args.no_onvif:
        print("ONVIF WS-Discovery...")
        onvif = onvif_ws_discovery(ONVIF_TIMEOUT)

    cameras = merge_results(port_scan, onvif)
    if args.cp_plus:
        cameras = [refine_cp_plus_camera(c, args.rtsp_timeout, creds) for c in cameras]
    print_results(cameras, network, args.cp_plus)
    return 0 if cameras else 1


if __name__ == "__main__":
    raise SystemExit(main())
