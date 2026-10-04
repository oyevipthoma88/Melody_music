"""
Free egress proxy for YouTube (Cloudflare WARP via wireproxy).

ROOT CAUSE of the never-ending "Sign in to confirm you're not a bot" /
"Requested format is not available" loop on Heroku:
    YouTube flags Heroku's datacenter IP ranges. Every cookie-less client is
    bot-checked from those IPs, and a cookie jar exported from a home browser
    gets invalidated/flagged once it is replayed from a datacenter IP — so
    "cookies added hai phir bhi" fails again after a few hours or days.
    No amount of client/format juggling fixes an IP reputation problem.

FIX (free, no account, no card):
    Cloudflare WARP is a free WireGuard VPN. `wgcf` registers a free WARP
    identity, `wireproxy` runs it in USERSPACE (no root / no TUN device,
    works on Heroku, Railway, Render, Koyeb, Docker) and exposes it as a local
    HTTP proxy. yt-dlp then talks to YouTube from a Cloudflare IP instead of
    the flagged dyno IP.

Precedence:
    1. YT_PROXY      — any proxy URL you provide (http://, socks5://). Used as-is.
    2. USE_WARP      — default ON for cloud hosts, OFF elsewhere. Set
                       USE_WARP=false to disable.
    3. no proxy      — plain dyno IP (old behaviour).

Everything here is best-effort: if WARP cannot start, the bot keeps running
exactly as before and just logs a warning.
"""
from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import tarfile
import threading
import time
import urllib.request

from melody.logging import LOGGER

_WGCF_VERSION = os.getenv("WGCF_VERSION", "2.3.0")
_WGCF_URL = (
    f"https://github.com/ViRb3/wgcf/releases/download/v{_WGCF_VERSION}/"
    f"wgcf_{_WGCF_VERSION}_linux_amd64"
)
_WIREPROXY_URLS = (
    "https://github.com/windtf/wireproxy/releases/latest/download/wireproxy_linux_amd64.tar.gz",
    "https://github.com/whyvl/wireproxy/releases/latest/download/wireproxy_linux_amd64.tar.gz",
)

_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_VENDOR_DIR = os.path.join(_APP_ROOT, "vendor", "warp")   # filled by bin/post_compile
_RUNTIME_DIR = "/tmp/melody_warp"                          # runtime fallback
_PORT = int(os.getenv("WARP_PROXY_PORT", "40001") or 40001)

_proxy_url: str | None = None
_proc: subprocess.Popen | None = None
_started = False
_lock = threading.Lock()
_ready = threading.Event()


def _on_cloud_host() -> bool:
    return bool(
        os.environ.get("DYNO")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RENDER_SERVICE_ID")
        or os.environ.get("FLY_APP_NAME")
        or os.environ.get("K_SERVICE")
        or os.environ.get("KOYEB_APP_NAME")
        or os.environ.get("WEBSITE_INSTANCE_ID")
    )


def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().strip('"').strip("'").lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "melody-bot"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest + ".part", "wb") as f:
        shutil.copyfileobj(resp, f)
    os.replace(dest + ".part", dest)


def _make_exec(path: str) -> None:
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _binary(name: str) -> str | None:
    for d in (_VENDOR_DIR, _RUNTIME_DIR):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which(name)


def _ensure_binaries() -> tuple[str, str] | None:
    os.makedirs(_RUNTIME_DIR, exist_ok=True)
    wgcf = _binary("wgcf")
    if not wgcf:
        wgcf = os.path.join(_RUNTIME_DIR, "wgcf")
        _download(_WGCF_URL, wgcf)
        _make_exec(wgcf)
    wireproxy = _binary("wireproxy")
    if not wireproxy:
        archive = os.path.join(_RUNTIME_DIR, "wireproxy.tar.gz")
        last_exc: Exception | None = None
        for url in _WIREPROXY_URLS:
            try:
                _download(url, archive)
                with tarfile.open(archive) as tf:
                    member = next(m for m in tf.getmembers() if m.name.endswith("wireproxy"))
                    member.name = "wireproxy"
                    tf.extract(member, _RUNTIME_DIR)
                wireproxy = os.path.join(_RUNTIME_DIR, "wireproxy")
                _make_exec(wireproxy)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if not wireproxy:
            raise RuntimeError(f"wireproxy download failed: {last_exc}")
    return wgcf, wireproxy


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _verify(proxy: str) -> str:
    """Return the trace text fetched THROUGH the proxy (raises on failure)."""
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    with opener.open("https://www.cloudflare.com/cdn-cgi/trace", timeout=8) as r:
        return r.read().decode("utf-8", "replace")


def _force_ipv4(profile: str) -> None:
    """Keep only IPv4 inside the tunnel.

    Oct 2 2026 log: WARP came up with an IPv6 egress (2a09:bac5:...) and
    YouTube still bot-checked it. WARP's IPv6 /48s are shared by millions of
    users and are flagged far more than its IPv4 pool. Dropping the v6
    address/route makes every connection leave over IPv4.
    WARP_IPV6=true restores dual-stack.
    """
    if _flag("WARP_IPV6", False):
        return
    out = []
    for line in open(profile, encoding="utf-8").read().splitlines():
        key = line.split("=", 1)[0].strip().lower()
        if key in {"address", "allowedips", "dns"} and "=" in line:
            vals = [v.strip() for v in line.split("=", 1)[1].split(",")]
            vals = [v for v in vals if ":" not in v]
            if not vals:
                continue
            line = f"{line.split('=', 1)[0].strip()} = {', '.join(vals)}"
        out.append(line)
    with open(profile, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def _start_warp() -> str | None:
    global _proc
    wgcf, wireproxy = _ensure_binaries()
    work = _RUNTIME_DIR
    account = os.path.join(work, "wgcf-account.toml")
    profile = os.path.join(work, "wgcf-profile.conf")

    if not os.path.isfile(account):
        subprocess.run(
            [wgcf, "register", "--accept-tos", "--config", account],
            cwd=work, check=True, timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    subprocess.run(
        [wgcf, "generate", "--config", account, "--profile", profile],
        cwd=work, check=True, timeout=60,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    _force_ipv4(profile)

    conf = os.path.join(work, "wireproxy.conf")
    with open(conf, "w", encoding="utf-8") as f:
        f.write(f"WGConfig = {profile}\n\n[http]\nBindAddress = 127.0.0.1:{_PORT}\n")

    _proc = subprocess.Popen(
        [wireproxy, "-s", "-c", conf],
        cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    proxy = f"http://127.0.0.1:{_PORT}"
    deadline = time.monotonic() + 25
    last = ""
    while time.monotonic() < deadline:
        if _proc.poll() is not None:
            raise RuntimeError(f"wireproxy exited with code {_proc.returncode}")
        if _port_open(_PORT):
            try:
                trace = _verify(proxy)
                if "warp=on" in trace or "warp=plus" in trace:
                    ip = next((l[3:] for l in trace.splitlines() if l.startswith("ip=")), "?")
                    LOGGER.info("🛡 Cloudflare WARP proxy ACTIVE (egress %s) — YouTube sees a clean IP", ip)
                    return proxy
                last = "warp=off"
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
        time.sleep(1)
    raise RuntimeError(f"WARP tunnel did not come up ({last or 'timeout'})")


def _boot() -> None:
    global _proxy_url
    try:
        manual = (os.getenv("YT_PROXY") or "").strip().strip('"').strip("'")
        if manual:
            _proxy_url = manual
            LOGGER.info("🛡 YT_PROXY set — all YouTube downloads go through your proxy")
            return
        if not _flag("USE_WARP", _on_cloud_host()):
            return
        for attempt in (1, 2):
            try:
                _proxy_url = _start_warp()
                return
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("WARP start attempt %d failed: %s", attempt, exc)
                # A stale/blocked identity is the usual cause — re-register.
                try:
                    os.remove(os.path.join(_RUNTIME_DIR, "wgcf-account.toml"))
                except OSError:
                    pass
                if _proc and _proc.poll() is None:
                    _proc.kill()
        LOGGER.warning("⚠️ WARP proxy unavailable — continuing on the dyno IP")
    finally:
        if _proxy_url and not _flag("PROXY_DIRECT_STREAM", False):
            # googlevideo URLs are locked to the IP that resolved them. When
            # yt-dlp resolves through the proxy, ffmpeg (which connects from
            # the dyno IP) would get HTTP 403 — so play the downloaded file.
            os.environ["DIRECT_STREAM"] = "false"
            os.environ["DISABLE_DIRECT_STREAM"] = "1"
        _ready.set()


def start_in_background() -> None:
    """Idempotent: kick off proxy setup without blocking the event loop."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_boot, name="melody-warp", daemon=True).start()


def get_proxy(wait: float = 0.0) -> str | None:
    """Proxy URL for yt-dlp, or None. Optionally wait for boot to finish."""
    if wait > 0 and not _ready.is_set():
        _ready.wait(wait)
    if _proxy_url and _proc is not None and _proc.poll() is not None:
        # wireproxy died — never hand yt-dlp a dead proxy.
        return None
    return _proxy_url


def force_refresh() -> None:
    """Delete wgcf identity + restart WARP — gets a NEW Cloudflare IP."""
    global _proxy_url, _proc, _started
    with _lock:
        try:
            if _proc and _proc.poll() is None:
                _proc.kill()
        except Exception:
            pass
        try:
            os.remove(os.path.join(_RUNTIME_DIR, "wgcf-account.toml"))
        except OSError:
            pass
        try:
            os.remove(os.path.join(_RUNTIME_DIR, "wgcf-profile.conf"))
        except OSError:
            pass
        _proxy_url = None
        _ready.clear()
        _started = False
    LOGGER.warning("🔄 WARP identity reset — will re-register on next start")
    start_in_background()


def is_ready() -> bool:
    return _ready.is_set()
