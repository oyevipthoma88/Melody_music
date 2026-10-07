"""ShrutiBots YouTube API source (https://shrutibots.site).

ROOT-CAUSE FIX: YouTube blocks Heroku IPs and burns cookies, so yt-dlp hits
the "Sign in to confirm you're not a bot" wall. This API downloads the
track on ITS servers and returns the raw audio/video file, so no cookies,
no PO-token and no Heroku IP ever touches YouTube for the media itself.

Env vars:
  SHRUTI_API_KEY          one key, or several comma-separated (auto-rotation on
                          quota / expiry / block / network error)
  SHRUTI_API_URL          optional, defaults to https://shrutibots.site
  SHRUTI_TIMEOUT          optional total seconds per attempt (default 60)
  SHRUTI_HEALTH_INTERVAL  seconds between "API ACTIVE?" checks (default 1800)

Endpoint used: GET /stream/{video_id}?type=audio|video&api_key=KEY
Response 200 = the file itself; errors = JSON {"detail": "..."}.
Key check uses GET /autoplay (does NOT spend daily quota).
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Optional

import httpx

from melody.logging import LOGGER

_CT_EXT = {
    "audio/webm": "webm",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/mpeg": "mp3",
    "video/mp4": "mp4",
    "video/x-matroska": "mkv",
    "video/webm": "webm",
}

# key -> unix time until which it must not be used
_key_cooldown: dict[str, float] = {}
_client: Optional[httpx.AsyncClient] = None

# Live status - shown in startup log + periodic "API active?" log lines.
STATUS: dict = {
    "checked_at": 0.0, "api_up": None, "valid_keys": 0, "total_keys": 0,
    "detail": "not checked yet", "ok": 0, "fail": 0, "last_ok": 0.0,
    "last_error": "",
}
_monitor_task: "asyncio.Task | None" = None


def _keys() -> list[str]:
    raw = os.environ.get("SHRUTI_API_KEY", "") or os.environ.get("SHRUTI_API_KEYS", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def enabled() -> bool:
    return bool(_keys())


def _base() -> str:
    return (os.environ.get("SHRUTI_API_URL") or "https://shrutibots.site").rstrip("/")


def _timeout() -> float:
    try:
        return float(os.environ.get("SHRUTI_TIMEOUT", "60"))
    except ValueError:
        return 60.0


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(_timeout(), connect=8.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


def _live_keys() -> list[str]:
    now = time.time()
    return [k for k in _keys() if _key_cooldown.get(k, 0) <= now]


def _cool(key: str, seconds: float, why: str) -> None:
    _key_cooldown[key] = time.time() + seconds
    STATUS["last_error"] = f"key ...{key[-4:]}: {why}"[:200]
    LOGGER.warning("ShrutiAPI key ...%s paused %ss: %s", key[-4:], int(seconds), why)
    if not _live_keys():
        LOGGER.error("🔴 ShrutiAPI INACTIVE - all keys paused/expired. Get a new key from @SHRUTIAPIBOT")


async def health_check() -> dict:
    """Check /health (no key) + validate every key via /autoplay, which does
    NOT spend daily quota. Logs a clear ACTIVE / INACTIVE line."""
    keys = _keys()
    STATUS.update(total_keys=len(keys), checked_at=time.time())
    if not keys:
        STATUS.update(api_up=None, valid_keys=0, detail="SHRUTI_API_KEY not set")
        LOGGER.warning("⚪ ShrutiAPI DISABLED - SHRUTI_API_KEY env var is empty (YouTube via Heroku IP only)")
        return STATUS
    client = _get_client()
    try:
        r = await client.get(f"{_base()}/health", timeout=10.0)
        up = r.status_code == 200 and "healthy" in r.text
    except Exception as exc:  # noqa: BLE001
        up = False
        STATUS["detail"] = f"health error: {exc!r}"[:200]
    STATUS["api_up"] = up
    valid = 0
    reasons = []
    for key in keys:
        try:
            kr = await client.get(f"{_base()}/autoplay",
                                  params={"video_id": "dQw4w9WgXcQ", "api_key": key},
                                  timeout=15.0)
            if kr.status_code == 200:
                valid += 1
                _key_cooldown.pop(key, None)
            else:
                d = kr.text[:120]
                reasons.append(f"...{key[-4:]}={kr.status_code} {d}")
                if kr.status_code in (401, 403) and "IP" not in d:
                    _key_cooldown[key] = time.time() + 6 * 3600
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"...{key[-4:]}={exc!r}"[:120])
    STATUS["valid_keys"] = valid
    if up and valid:
        STATUS["detail"] = f"{valid}/{len(keys)} key(s) valid"
        LOGGER.info("🟢 ShrutiAPI ACTIVE - server healthy, %d/%d key(s) valid. /play + /vplay use API (no YouTube cookies/IP needed)", valid, len(keys))
    else:
        STATUS["detail"] = ("server down" if not up else "no valid key") + (
            ": " + "; ".join(reasons) if reasons else "")
        LOGGER.error("🔴 ShrutiAPI INACTIVE - %s. Falling back to yt-dlp/JioSaavn", STATUS["detail"])
    return STATUS


def is_active() -> bool:
    return bool(STATUS.get("api_up")) and bool(_live_keys()) and STATUS.get("valid_keys", 0) > 0


def status_html() -> str:
    if not _keys():
        return "⚪ <b>YT API:</b> disabled (SHRUTI_API_KEY not set)"
    icon = "🟢 ACTIVE" if is_active() else "🔴 INACTIVE"
    return (f"🎧 <b>YT API (ShrutiBots):</b> {icon}\n"
            f"   └ {STATUS['detail']} · served {STATUS['ok']} ok / {STATUS['fail']} fail")


async def send_status(bot, assistant) -> None:
    """Post the API status to LOG_GROUP_ID (assistant first, then bot)."""
    try:
        from melody.config import Config
        chat = getattr(Config, "LOG_GROUP_ID", None) or getattr(Config, "OWNER_ID", None)
    except Exception:  # noqa: BLE001
        chat = None
    if not chat:
        return
    text = status_html()
    if STATUS.get("last_error") and not is_active():
        text += f"\n   └ last error: <code>{STATUS['last_error'][:150]}</code>"
    for c in (assistant, bot):
        if c is None:
            continue
        try:
            await c.send_message(chat, text)
            return
        except Exception:  # noqa: BLE001
            continue


def start_monitor() -> None:
    """Re-check every SHRUTI_HEALTH_INTERVAL seconds (default 1800) and log."""
    global _monitor_task
    if _monitor_task is not None and not _monitor_task.done():
        return
    try:
        every = max(300.0, float(os.environ.get("SHRUTI_HEALTH_INTERVAL", "1800")))
    except ValueError:
        every = 1800.0

    async def _loop():
        while True:
            await asyncio.sleep(every)
            try:
                await health_check()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("ShrutiAPI monitor error: %r", exc)

    _monitor_task = asyncio.get_running_loop().create_task(_loop())


async def download(
    video_id: str, tag: str, audio_only: bool = True,
    cancel_event: "threading.Event | None" = None,
) -> Optional[str]:
    """Download into /tmp/melody_<video_id>_<tag>.<ext>; return path or None."""
    keys = _live_keys()
    if not keys:
        return None
    kind = "audio" if audio_only else "video"
    url = f"{_base()}/stream/{video_id}"
    client = _get_client()
    t0 = time.monotonic()

    for key in keys:
        tmp = f"/tmp/melody_{video_id}_{tag}.shruti.part"
        try:
            async with client.stream("GET", url, params={"type": kind, "api_key": key}) as r:
                if r.status_code != 200:
                    body = (await r.aread())[:300].decode("utf-8", "ignore")
                    s = r.status_code
                    if s in (401, 403) and "IP" not in body:
                        _cool(key, 6 * 3600, f"{s} {body}")
                        continue
                    if s == 429 and "quota" in body.lower():
                        _cool(key, 3 * 3600, body)
                        continue
                    if s == 429:
                        await asyncio.sleep(1.5)
                        continue
                    LOGGER.info("ShrutiAPI %s for %s: %s", s, video_id, body)
                    STATUS["fail"] += 1
                    STATUS["last_error"] = f"{s} {body}"[:200]
                    return None  # 400/500: video-side problem, other keys won't help
                ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ct.startswith("application/json") or ct.startswith("text/"):
                    LOGGER.info("ShrutiAPI non-media reply for %s: %s", video_id, ct)
                    return None
                ext = _CT_EXT.get(ct) or ("m4a" if audio_only else "mp4")
                size = 0
                with open(tmp, "wb") as fh:
                    async for chunk in r.aiter_bytes(256 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            raise asyncio.CancelledError()
                        fh.write(chunk)
                        size += len(chunk)
            if size < 10 * 1024:
                LOGGER.info("ShrutiAPI tiny file (%sB) for %s", size, video_id)
                _rm(tmp)
                return None
            final = f"/tmp/melody_{video_id}_{tag}.{ext}"
            os.replace(tmp, final)
            STATUS["ok"] += 1
            STATUS["last_ok"] = time.time()
            LOGGER.info("⚡ ShrutiAPI %s %s ready in %.1fs (%d KB)",
                        kind, video_id, time.monotonic() - t0, size // 1024)
            return final
        except asyncio.CancelledError:
            _rm(tmp)
            raise
        except Exception as exc:  # noqa: BLE001
            _rm(tmp)
            LOGGER.warning("ShrutiAPI error for %s with key ...%s: %r - trying next key", video_id, key[-4:], exc)
            STATUS["last_error"] = repr(exc)[:200]
            continue
    STATUS["fail"] += 1
    return None


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
