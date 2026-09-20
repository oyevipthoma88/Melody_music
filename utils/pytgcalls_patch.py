"""
🛠 py-tgcalls ffprobe hardening patch (ROOT-CAUSE FIX).

REPORTED BUG
------------
    _stream_track failed in -100...
      File ".../pytgcalls/ffmpeg.py", line 56, in check_stream
        result = loads(stdout.decode('utf-8')) or {}
    json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)

    During handling of the above exception, another exception occurred:
      File ".../pytgcalls/ffmpeg.py", line 62, in check_stream
        ffprobe.terminate()
    ProcessLookupError

WHY IT HAPPENS
--------------
Before playing anything, py-tgcalls runs `ffprobe` on the source. When the
source is a direct googlevideo CDN URL that ffprobe cannot open (expired
link, 403, region block, throttled/hung range request, ffprobe timeout,
DASH-only manifest), ffprobe prints NOTHING on stdout, so `json.loads("")`
raises `JSONDecodeError`. py-tgcalls' own `except` branch then calls
`ffprobe.terminate()` on a process that has already exited — on uvloop that
raises `ProcessLookupError`, which REPLACES the original error. The real
reason is destroyed and the caller sees an unactionable traceback.

WHAT THIS PATCH DOES
--------------------
1. `ffprobe.terminate()` is never allowed to raise, so the original failure
   is preserved instead of being masked by `ProcessLookupError`.
2. A probe failure is retried once (most googlevideo hiccups are transient).
3. If the source is a LOCAL FILE that exists on disk, a probe failure is
   ignored — we produced that file ourselves, and refusing to play a
   perfectly good download because ffprobe hiccuped is strictly worse.
4. Otherwise it raises `StreamProbeUnavailable` — one clear, catchable error
   which `melody/core/call.py` treats as "this CDN URL is unusable" and
   falls back to the download path instead of failing the /play.

Call `apply_pytgcalls_probe_patch()` once at import time of call.py.
"""
from __future__ import annotations

import logging
import os
import time

from melody.logging import redact_sensitive_text

log = logging.getLogger(__name__)

_applied = False

# ─── LATENCY / TimeoutError ROOT CAUSE ───────────────────────────────────────
#
# Reported traceback:
#     media_stream.py, line 286, in check_stream -> cleanup_commands(...)
#     ffmpeg.py, line 134, in cleanup_commands   -> asyncio.wait_for(..., 20)
#     TimeoutError
#
# py-tgcalls asks the binary for its FULL help page (`ffmpeg -h full`,
# `ffprobe -h full`) on EVERY single play, just to learn which flags it
# supports, and gives it a 20-second budget. That help page is huge; on a
# memory-constrained dyno the child process is slow or gets stuck, so each
# /play paid several extra seconds — and when it exceeded the budget the whole
# play died with a bare TimeoutError.
#
# The supported-flag list of a binary NEVER changes while the process runs, so
# it is computed once, cached, and reused for the lifetime of the worker. That
# removes the repeated cost entirely (instant VC join + instant playback) and
# makes a slow/failed help probe non-fatal: we fall back to using the command
# unchanged, which ffmpeg itself built.
PROBE_TIMEOUT_LOCAL = float(os.getenv("FFPROBE_LOCAL_TIMEOUT", "4"))
PROBE_TIMEOUT_REMOTE = float(os.getenv("FFPROBE_REMOTE_TIMEOUT", "4"))
HELP_PROBE_TIMEOUT = float(os.getenv("FFPROBE_HELP_TIMEOUT", "6"))

_supported_flags_cache: "dict[str, set[str] | None]" = {}


async def _supported_flags(binary: str) -> "set[str] | None":
    """Flags accepted by `binary`, probed once and cached. None => unknown."""
    if binary in _supported_flags_cache:
        return _supported_flags_cache[binary]

    import asyncio
    import re

    flags: "set[str] | None" = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "-h", "full",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=HELP_PROBE_TIMEOUT
            )
            text = stdout.decode("utf-8", "ignore")
            found = set(re.findall(r"(?m)^ *(-\w+).*?\s+", text))
            if found:
                found.add("-i")
                flags = found
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    except FileNotFoundError:
        raise
    except Exception as exc:
        log.debug("capability probe for %s failed (%s)", binary, exc)

    if flags is None:
        log.warning(
            "⚠️ could not read %s capabilities — using its command as built "
            "(no flag filtering). Playback continues.", binary,
        )
    _supported_flags_cache[binary] = flags
    return flags


def _patch_cleanup_commands(_ffmpeg_mod, _media_stream_mod) -> None:
    original_cleanup = getattr(_ffmpeg_mod, "cleanup_commands", None)
    if original_cleanup is None or getattr(original_cleanup, "_melody_patched", False):
        return

    async def cleanup_commands(commands, process_name=None, blacklist=None):
        if not commands:
            return list(commands or [])
        binary = process_name or commands[0]
        try:
            supported = await _supported_flags(binary)
        except FileNotFoundError:
            # Preserve py-tgcalls' own "not installed" error semantics.
            return await original_cleanup(commands, process_name, blacklist)

        if supported is None:
            # Unknown capabilities: never drop the caller's command. Only the
            # explicit blacklist is honoured.
            if not blacklist:
                return list(commands)
            supported = None

        new_commands: list = []
        ignore_next = False
        for value in commands:
            if not value:
                continue
            if value[0] == "-":
                ignore_next = (
                    (supported is not None and value not in supported)
                    or (blacklist is not None and value in blacklist)
                )
            if not ignore_next:
                new_commands.append(value)
            elif value[0] != "-":
                ignore_next = False
        return new_commands

    cleanup_commands._melody_patched = True  # type: ignore[attr-defined]
    _ffmpeg_mod.cleanup_commands = cleanup_commands
    # media_stream.py imported the symbol directly — rebind its reference too,
    # otherwise it keeps calling the slow original (this is exactly where the
    # reported TimeoutError came from).
    if hasattr(_media_stream_mod, "cleanup_commands"):
        _media_stream_mod.cleanup_commands = cleanup_commands
    log.info("⚡ py-tgcalls capability probe cached (instant playback start).")


class StreamProbeUnavailable(Exception):
    """ffprobe could not read the media source (dead/blocked CDN URL)."""


class NoAudioTrack(StreamProbeUnavailable):
    """The source was READ successfully and genuinely carries no audio.

    Distinguishing this from "ffprobe could not read the URL at all" is the
    whole point: py-tgcalls raises ``NoAudioSourceFound`` for BOTH cases
    (see ffmpeg.py: an unreadable URL makes ffprobe print ``{}``, so the
    stream list is empty and the audio check fails). Treating an unreadable
    URL as "this itag has no audio" poisoned the picker's blacklist for 15
    minutes and forced a full download for every later /play of that video.
    """


# ─── ROOT CAUSE: ffmpeg/ffprobe received only the LAST HTTP header ───────────
#
# py-tgcalls' build_command() emits one `-headers "K: V"` pair PER header:
#
#     ffprobe ... -headers 'User-Agent: ...' -headers 'Referer: ...' -i URL
#
# `-headers` is a single AVOption, so the second occurrence OVERWRITES the
# first: the User-Agent never reaches the socket and ffmpeg sends its default
# `Lavf/<version>`. googlevideo answers that with 403, ffprobe prints `{}`,
# py-tgcalls sees zero streams and raises NoAudioSourceFound — the exact
# Heroku line "Direct CDN stream unusable ... NoAudioSourceFound ... falling
# back to download" followed by a 13-20s full download.
#
# Fix: collapse every `-headers` pair into ONE CRLF-joined value (verified
# against a local HTTP server: both headers then arrive intact) and make sure
# a googlevideo URL always carries a User-Agent matching the client that
# minted it.
_CLIENT_USER_AGENTS = {
    "ANDROID": "com.google.android.youtube/19.09.37 (Linux; U; Android 14) gzip",
    "ANDROID_MUSIC": (
        "com.google.android.apps.youtube.music/6.42.52 "
        "(Linux; U; Android 14) gzip"
    ),
    "ANDROID_VR": "com.google.android.apps.youtube.vr.oculus/1.56.21 (Linux; U; Android 12)",
    "IOS": (
        "com.google.ios.youtube/19.09.3 (iPhone16,2; U; CPU iOS 17_4 like Mac OS X)"
    ),
    "IOS_MUSIC": (
        "com.google.ios.youtubemusic/6.42.52 "
        "(iPhone16,2; U; CPU iOS 17_4 like Mac OS X)"
    ),
    "TVHTML5": "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/Version",
    "TVHTML5_SIMPLY_EMBEDDED_PLAYER": "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/Version",
    "VISIONOS": (
        "com.google.ios.youtube/19.09.3 (RealityDevice14,1; U; CPU visionOS 1_1)"
    ),
    "MWEB": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
    ),
}
_DEFAULT_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def client_user_agent(url: str) -> str:
    """User-Agent matching the InnerTube client a googlevideo URL was signed for.

    googlevideo ties a signed URL to the client in its ``c=`` query parameter;
    a mismatched UA is answered with 403 even though the link is perfectly
    valid.
    """
    text = str(url or "")
    _, _, query = text.partition("?")
    for part in query.split("&"):
        name, _, value = part.partition("=")
        if name == "c" and value:
            return _CLIENT_USER_AGENTS.get(value.upper(), _DEFAULT_WEB_UA)
    return _DEFAULT_WEB_UA


def _is_googlevideo(url: str) -> bool:
    return "googlevideo.com" in str(url or "")


def merge_header_options(command: list) -> list:
    """Collapse repeated ``-headers`` options into one CRLF-joined value."""
    if not command:
        return list(command or [])

    collected: "dict[str, str]" = {}
    merged: list = []
    header_slot = -1
    index = 0
    while index < len(command):
        value = command[index]
        if value == "-headers" and index + 1 < len(command):
            for line in str(command[index + 1]).split("\r\n"):
                line = line.strip()
                if not line:
                    continue
                name, sep, header_value = line.partition(":")
                if sep and name.strip():
                    collected[name.strip()] = header_value.strip()
            if header_slot < 0:
                header_slot = len(merged)
            index += 2
            continue
        merged.append(value)
        index += 1

    url = ""
    if "-i" in merged:
        position = merged.index("-i")
        if position + 1 < len(merged):
            url = str(merged[position + 1])

    if _is_googlevideo(url) and not any(
        key.lower() == "user-agent" for key in collected
    ):
        collected["User-Agent"] = client_user_agent(url)

    if not collected:
        return merged

    if header_slot < 0:
        header_slot = merged.index("-i") if "-i" in merged else len(merged)
    blob = "".join(f"{name}: {value}\r\n" for name, value in collected.items())
    merged[header_slot:header_slot] = ["-headers", blob]
    return merged


def _patch_build_command(_ffmpeg_mod, _media_stream_mod) -> None:
    original = getattr(_ffmpeg_mod, "build_command", None)
    if original is None or getattr(original, "_melody_patched", False):
        return

    def build_command(*args, **kwargs):
        return merge_header_options(original(*args, **kwargs))

    build_command._melody_patched = True  # type: ignore[attr-defined]
    _ffmpeg_mod.build_command = build_command
    if hasattr(_media_stream_mod, "build_command"):
        _media_stream_mod.build_command = build_command
    log.info("🔧 ffmpeg/ffprobe HTTP headers merged (User-Agent reaches the CDN).")


async def probe_audio_streams(url: str, headers: "dict | None" = None) -> "bool | None":
    """True/False when ffprobe could READ the url, None when it could not.

    Used to tell a genuinely audio-less stream apart from a URL the CDN simply
    refused, so only the former is ever blacklisted.
    """
    import asyncio
    import json

    if not _ffprobe_available():
        return None
    merged = dict(headers or {})
    if _is_googlevideo(url) and not any(k.lower() == "user-agent" for k in merged):
        merged["User-Agent"] = client_user_agent(url)
    command = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
               "-of", "json"]
    if merged:
        command += [
            "-headers",
            "".join(f"{name}: {value}\r\n" for name, value in merged.items()),
        ]
    command += ["-i", url]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=PROBE_TIMEOUT_REMOTE
        )
    except Exception:  # noqa: BLE001 - probe is advisory only
        return None
    try:
        streams = (json.loads(stdout.decode("utf-8") or "{}") or {}).get("streams") or []
    except Exception:  # noqa: BLE001
        return None
    if not streams:
        # Unreadable source (403/expired/blocked): NOT evidence of "no audio".
        return None
    return any(s.get("codec_type") == "audio" for s in streams)




def _is_local_file(path) -> bool:
    try:
        return isinstance(path, str) and os.path.isfile(path)
    except Exception:
        return False


def _is_local_proxy_url(path) -> bool:
    """True for Melody-owned loopback Telegram range-proxy URLs.

    These URLs are intentionally bound to localhost and are not public CDN
    sources. Treating them as remote makes the ffprobe wrapper raise
    ``StreamProbeUnavailable`` even though the aiohttp proxy is healthy.
    """
    if not isinstance(path, str):
        return False
    value = path.lower()
    return value.startswith(("http://127.0.0.1:", "http://localhost:")) and "/tg/" in value


def _is_local_source(path) -> bool:
    return _is_local_file(path) or _is_local_proxy_url(path)


def _size(path) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def _is_local_playable(path) -> bool:
    return _is_local_file(path) and _size(path) > 8192


async def _wait_for_growth(path, seconds: float = 4.0) -> bool:
    """Give a still-downloading file a moment to become probeable.

    ROOT CAUSE of the reported "_stream_track failed ... JSONDecodeError":
    download_audio() hands the path back EARLY, while yt-dlp is still writing
    it. ffprobe on those first bytes can print nothing at all -> json.loads("")
    -> JSONDecodeError. The file is fine a second later, so waiting (instead
    of failing the whole /play) is the correct behaviour.
    """
    import asyncio

    start = _size(path)
    waited = 0.0
    while waited < seconds:
        await asyncio.sleep(0.5)
        waited += 0.5
        if _size(path) > max(start, 65536):
            return True
    return _size(path) > 65536


# ─── ROOT CAUSE: ffprobe binary missing ──────────────────────────────────────
#
# Reported error card:
#     utils.pytgcalls_patch.StreamProbeUnavailable:
#         ffprobe could not read source (FileNotFoundError: )
#
# An EMPTY FileNotFoundError message is not a bad CDN URL — it is the OS saying
# the `ffprobe` executable itself does not exist (ffmpeg installed without
# ffprobe, or PATH missing it on the host). In that state EVERY /play failed
# forever. Probing is only an optional pre-check, so when the binary is absent
# we permanently bypass it instead of failing playback.
def _resolve_ffprobe() -> "str | None":
    import shutil

    found = shutil.which("ffprobe")
    if found:
        return found
    for candidate in (
        "/usr/bin/ffprobe", "/usr/local/bin/ffprobe", "/app/vendor/ffmpeg/ffprobe",
        "/app/bin/ffprobe", "/opt/homebrew/bin/ffprobe",
    ):
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # Some hosts ship ffprobe next to the ffmpeg binary but not on PATH.
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        sibling = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
        if os.path.exists(sibling) and os.access(sibling, os.X_OK):
            return sibling
    return None


def ensure_ffprobe_on_path() -> bool:
    """True if ffprobe is usable. Adds its folder to PATH when found off-PATH."""
    import shutil

    if shutil.which("ffprobe"):
        return True
    found = _resolve_ffprobe()
    if not found:
        return False
    os.environ["PATH"] = os.path.dirname(found) + os.pathsep + os.environ.get("PATH", "")
    return bool(shutil.which("ffprobe"))


_HAS_FFPROBE = None


def _ffprobe_available() -> bool:
    global _HAS_FFPROBE
    if _HAS_FFPROBE is None:
        _HAS_FFPROBE = ensure_ffprobe_on_path()
        if not _HAS_FFPROBE:
            log.warning(
                "⚠️ ffprobe binary not found — stream pre-probe disabled "
                "(playback continues without it)."
            )
    return _HAS_FFPROBE


def _short_source(path) -> str:
    """CDN urls are ~1.5 KB of query string — keep logs readable."""
    text = str(path)
    if text.startswith(("http://", "https://")):
        head = text.split("?", 1)[0]
        return f"{head}?…" if "?" in text else head
    return text


def _is_missing_binary(exc: "BaseException | None") -> bool:
    if exc is None:
        return False
    if isinstance(exc, FileNotFoundError):
        return True
    text = f"{exc}".lower()
    return "no such file or directory" in text and "ffprobe" in text


try:
    _REMOTE_CHECK_TIMEOUT = max(0.25, float(os.getenv("REMOTE_CHECK_TIMEOUT", "2.5")))
except (TypeError, ValueError):
    _REMOTE_CHECK_TIMEOUT = 2.5
# SPEED FIX: on Heroku/Railway/Render/Fly a slow CDN preflight must not eat the
# playback budget — the parallel download is already racing. Local/self-hosted
# deployments keep the operator value.
_IS_CLOUD = bool(
    os.getenv("DYNO")
    or os.getenv("RAILWAY_ENVIRONMENT")
    or os.getenv("RENDER_SERVICE_ID")
    or os.getenv("FLY_APP_NAME")
)
try:
    _cloud_budget = max(0.25, float(os.getenv("REMOTE_CHECK_CLOUD_BUDGET", "1.5")))
except (TypeError, ValueError):
    _cloud_budget = 0.9
_REMOTE_CHECK_BUDGET = min(
    _REMOTE_CHECK_TIMEOUT,
    _cloud_budget if _IS_CLOUD else _REMOTE_CHECK_TIMEOUT,
)
try:
    _REMOTE_CHECK_CACHE_TTL = max(0.5, float(os.getenv("REMOTE_CHECK_CACHE_TTL", "10")))
except (TypeError, ValueError):
    _REMOTE_CHECK_CACHE_TTL = 10.0
_remote_reach_cache: dict[str, float] = {}
_REMOTE_REACH_CACHE_MAX = 256
_remote_probe_disabled = False


def _http_reachable_sync(url: str, timeout: float, headers: dict | None = None) -> bool:
    """True when the CDN answers a 1-byte ranged GET with a success status.

    ROOT CAUSE of the "10 second silence before the song starts":
    py-tgcalls pre-probes every source with `ffprobe`. On this host ffprobe
    exits INSTANTLY with empty stdout for googlevideo URLs (json.loads("") ->
    JSONDecodeError, then kill() -> ProcessLookupError), so the direct CDN
    stream was declared unusable and every /play fell back to a full
    download — the measured 10-20s gap.

    ffprobe is only a pre-check. A single ranged GET (~100-300 ms) tells us
    everything we actually need: is the URL alive? If yes, hand the URL to
    ffmpeg immediately and start playing.
    """
    from urllib.request import Request, urlopen

    # LOG FIX: googlevideo ties a URL to the client that produced it. A single
    # desktop-Chrome UA got 403'd on `c=TVHTML5` URLs, so a perfectly playable
    # stream looked "dead", fell through to ffprobe (which prints nothing on
    # those URLs -> JSONDecodeError) and produced three warnings + a needless
    # full download per song. Try the matching client UA too before giving up.
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    ]
    if "c=TVHTML5" in url:
        user_agents.insert(
            0,
            "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/Version",
        )
    elif "c=ANDROID" in url:
        user_agents.insert(0, "com.google.android.youtube/19.09.37 (Linux; U; Android 14)")

    supplied = {
        str(key): str(value)
        for key, value in (headers or {}).items()
        if value is not None
    }
    supplied_agent = supplied.get("User-Agent")
    if supplied_agent:
        user_agents.insert(0, supplied_agent)

    try:
        from curl_cffi import requests as curl_requests
    except Exception:
        curl_requests = None

    for agent in user_agents:
        request_headers = {
            "Range": "bytes=0-1",
            "User-Agent": agent,
            "Accept": "*/*",
        }
        # yt-dlp may provide a Referer and a client-specific UA with the
        # resolved URL. Preserve the Referer for YouTube CDN anti-hotlinking,
        # while trying the inferred client UA variants above as fallbacks.
        if supplied.get("Referer"):
            request_headers["Referer"] = supplied["Referer"]
        # urllib is frequently rejected by YouTube’s CDN even when the same
        # URL works with the Chrome TLS fingerprint used by yt-dlp. Prefer the
        # installed curl_cffi transport first; retain urllib as a dependency-free
        # fallback for non-YouTube hosts and minimal installations.
        if curl_requests is not None:
            try:
                resp = curl_requests.get(
                    url,
                    headers=request_headers,
                    timeout=timeout,
                    impersonate="chrome124",
                    stream=True,
                )
                try:
                    if 200 <= int(resp.status_code) < 400:
                        # A status-only check accepted URLs whose headers
                        # arrived but whose media body then stalled; ffmpeg
                        # timed out later and the whole window was wasted.
                        first_bytes = next(resp.iter_content(chunk_size=2), b"")
                        return bool(first_bytes)
                finally:
                    resp.close()
            except Exception:
                continue
        req = Request(url, method="GET", headers=request_headers)
        try:
            with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed CDN url
                if 200 <= getattr(resp, "status", 200) < 400:
                    return bool(resp.read(2))
        except Exception:
            continue
    return False


async def _remote_reachable(url: str, headers: dict | None = None) -> bool:
    import asyncio

    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return False
    now = time.monotonic()
    if _remote_reach_cache.get(url, 0.0) > now:
        return True
    loop = asyncio.get_running_loop()
    try:
        from melody.core.pools import IO_POOL
        reachable = await asyncio.wait_for(
            loop.run_in_executor(
                IO_POOL, _http_reachable_sync, url, _REMOTE_CHECK_BUDGET, headers
            ),
            timeout=_REMOTE_CHECK_BUDGET + 0.25,
        )
        if reachable:
            _remote_reach_cache[url] = time.monotonic() + _REMOTE_CHECK_CACHE_TTL
            if len(_remote_reach_cache) > _REMOTE_REACH_CACHE_MAX:
                ordered = sorted(_remote_reach_cache, key=_remote_reach_cache.get)
                for old_url in ordered[: len(_remote_reach_cache) - _REMOTE_REACH_CACHE_MAX]:
                    _remote_reach_cache.pop(old_url, None)
        return bool(reachable)
    except Exception:
        return False


def apply_pytgcalls_probe_patch() -> None:
    global _applied
    if _applied:
        return

    try:
        from pytgcalls import ffmpeg as _ffmpeg_mod
        from pytgcalls.types.stream import media_stream as _media_stream_mod
    except Exception as exc:  # pragma: no cover - py-tgcalls layout changed
        log.warning("pytgcalls probe patch skipped (%s)", exc)
        return

    original = getattr(_ffmpeg_mod, "check_stream", None)
    if original is None or getattr(original, "_melody_patched", False):
        _applied = True
        return

    async def check_stream(ffmpeg_parameters, path, stream_parameters, *args, **kwargs):
        import asyncio as _asyncio

        last_exc: BaseException | None = None
        local = _is_local_source(path)
        stream_headers = None
        if len(args) > 1 and isinstance(args[1], dict):
            stream_headers = args[1]
        elif isinstance(kwargs.get("headers"), dict):
            stream_headers = kwargs["headers"]
        # Melody's Telegram range proxy is already an HTTP byte-range source
        # backed by the media object. Running ffprobe before Py-TgCalls opens
        # it causes an avoidable full probe/tail-range timeout on multi-GB MKV
        # files; Py-TgCalls/ffmpeg can consume the same source directly.
        if _is_local_proxy_url(path):
            log.debug("local Telegram proxy — skipping pre-probe for %s", _short_source(path))
            return None
        # PERMANENT FIX: no ffprobe binary on this host => nothing to probe
        # with. Skip the pre-check instead of killing every single /play.
        if not _ffprobe_available():
            return None
        # SPEED FIX: an early-handoff source is a *growing* file. ffprobe waits
        # for EOF/an index on it and blocks the first audio frame for seconds
        # (and can raise TimeoutError). WebM/Opus prefixes are self-describing,
        # so hand the file straight to ffmpeg.
        _path_text = str(path)
        if (
            _path_text.endswith((".part", ".ytdl", ".temp", ".tmp"))
            or ".part-" in _path_text
            or ".ytdl" in _path_text
        ):
            log.info("⚡ early-handoff partial file — skipping ffprobe pre-check.")
            return None
        # LOG FIX: retrying a probe that already failed on a LOCAL file just
        # burns another ffprobe timeout (the log shows every local track probed
        # twice before playing anyway). Retry only remote URLs, where a hiccup
        # is genuinely transient.
        if not local:
            # ⚡ PERMANENT LATENCY FIX — never let ffprobe gate a CDN stream.
            # A live URL is played straight away (no probe, no download
            # fallback); only a genuinely dead URL falls through to ffprobe
            # and the download path below.
            if ".m3u8" in str(path).lower():
                log.info("⚡ HLS source — skipping remote preflight, playing directly.")
                return None
            if await _remote_reachable(path, stream_headers):
                log.info(
                    "⚡ remote source reachable — skipping ffprobe pre-check, "
                    "playing directly."
                )
                return None
            # Expected, recoverable fallback (the download path handles it),
            # so this must not look like an error in the logs.
            log.debug(
                "remote source did not answer a ranged GET — verifying with ffprobe."
            )
        attempts = (1,) if local else (1, 2)
        for attempt in attempts:
            try:
                # LATENCY FIX: py-tgcalls' own probe timeout is 20s and it is
                # paid BEFORE the first audio frame, so a slow/hanging probe
                # turned every /play into a 20-40s wait (and finally the
                # reported TimeoutError). A local file we produced ourselves
                # needs at most a couple of seconds to answer.
                return await _asyncio.wait_for(
                    original(
                        ffmpeg_parameters, path, stream_parameters, *args, **kwargs
                    ),
                    timeout=PROBE_TIMEOUT_LOCAL if local else PROBE_TIMEOUT_REMOTE,
                )
            except Exception as exc:
                name = type(exc).__name__
                # Real, meaningful stream verdicts must keep bubbling up:
                # py-tgcalls uses them for control flow (image sources, live
                # streams, proportion adjustment).
                if name in (
                    "ImageSourceFound",
                    "LiveStreamFound",
                    "InvalidVideoProportion",
                ):
                    raise
                if name == "NoVideoSourceFound" and local:
                    # Audio-only file played as audio: expected, not an error.
                    return None
                if name == "NoAudioSourceFound" and not local:
                    # ROOT-CAUSE FIX: py-tgcalls raises this both for a real video-only
                    # stream AND for a URL the CDN refused (ffprobe prints "{}",
                    # so its stream list is empty). Re-probe ourselves with the
                    # correct client User-Agent before believing it.
                    verdict = await probe_audio_streams(path, stream_headers)
                    if verdict is True:
                        log.info(
                            "⚡ CDN source carries audio (header-corrected probe) "
                            "— playing directly."
                        )
                        return None
                    if verdict is False:
                        raise NoAudioTrack(
                            f"source has no audio track ({_short_source(path)})"
                        ) from exc
                    # verdict is None: unreadable, fall through to the retry /
                    # StreamProbeUnavailable path — never blacklist this URL.
                last_exc = exc

                if _is_missing_binary(exc):
                    # ffprobe vanished mid-run (or was never really there):
                    # remember it and stop probing for good.
                    global _HAS_FFPROBE
                    _HAS_FFPROBE = False
                    log.warning(
                        "ffprobe binary missing — pre-probe disabled, playing %s directly.",
                        path,
                    )
                    return None
                # Only the final attempt is worth a warning — the earlier ones
                # are retried straight away (and the direct-CDN path falls back
                # to a download), so they were pure log noise.
                # A failed probe on a REMOTE url is a routine fallback to the
                # download path — only a local-file probe failure is worth a
                # warning. Log the url short (query strings are 1.5 KB long).
                log.log(
                    logging.WARNING if (local and attempt == attempts[-1]) else logging.DEBUG,
                    "ffprobe check_stream failed (%s: %s) attempt %d for %s",
                    name, exc, attempt, _short_source(path),
                )

        if local:
            # The file may simply be too fresh (early hand-off while yt-dlp is
            # still writing). Wait for it to grow and probe one last time.
            if _is_local_file(path) and not _is_local_playable(path) and await _wait_for_growth(path):
                try:
                    return await original(
                        ffmpeg_parameters, path, stream_parameters, *args, **kwargs
                    )
                except Exception as exc:
                    name = type(exc).__name__
                    if name in (
                        "ImageSourceFound", "LiveStreamFound", "InvalidVideoProportion",
                    ):
                        raise
                    last_exc = exc
            # A local file we produced ourselves — play it anyway rather than
            # failing the user's /play because ffprobe hiccuped.
            log.warning(
                "ffprobe unusable for local file %s (%s) — playing without probe.",
                path, type(last_exc).__name__ if last_exc else "?",
            )
            return None

        safe_detail = redact_sensitive_text(last_exc or "unknown probe failure")
        raise StreamProbeUnavailable(
            f"ffprobe could not read source ({type(last_exc).__name__}: {safe_detail})"
        ) from last_exc


    check_stream._melody_patched = True  # type: ignore[attr-defined]
    _ffmpeg_mod.check_stream = check_stream
    # media_stream.py did `from ...ffmpeg import check_stream`, so the module
    # attribute above is NOT enough — rebind its own reference too.
    if hasattr(_media_stream_mod, "check_stream"):
        _media_stream_mod.check_stream = check_stream

    _patch_cleanup_commands(_ffmpeg_mod, _media_stream_mod)
    _patch_build_command(_ffmpeg_mod, _media_stream_mod)
    _patch_terminate(_ffmpeg_mod)
    _applied = True
    log.info(
        "🛠 py-tgcalls patch active — probe failures non-fatal, binary "
        "capability probe cached (help=%.0fs, local=%.0fs, remote=%.0fs).",
        HELP_PROBE_TIMEOUT, PROBE_TIMEOUT_LOCAL, PROBE_TIMEOUT_REMOTE,
    )


def _patch_terminate(_ffmpeg_mod) -> None:
    """Make `Process.terminate()` inside py-tgcalls non-raising.

    py-tgcalls calls `ffprobe.terminate()` from inside its own `except`
    block; when the process already exited, uvloop raises ProcessLookupError
    there and destroys the original exception context. We cannot edit the
    library, but we CAN make asyncio's terminate() tolerant of an already
    dead child, which is the only reason it ever raises here.
    """
    try:
        from asyncio import subprocess as _asubprocess

        proc_cls = _asubprocess.Process
        if getattr(proc_cls.terminate, "_melody_patched", False):
            return
        original_terminate = proc_cls.terminate

        def terminate(self):
            try:
                return original_terminate(self)
            except (ProcessLookupError, OSError):
                return None

        terminate._melody_patched = True  # type: ignore[attr-defined]
        proc_cls.terminate = terminate  # type: ignore[assignment]

        # py-tgcalls' ffmpeg.py calls `ffprobe.kill()` (not terminate) inside
        # its own except branch — on uvloop that raises ProcessLookupError for
        # an already-exited child and REPLACES the real error. Same treatment.
        if not getattr(proc_cls.kill, "_melody_patched", False):
            original_kill = proc_cls.kill

            def kill(self):
                try:
                    return original_kill(self)
                except (ProcessLookupError, OSError):
                    return None

            kill._melody_patched = True  # type: ignore[attr-defined]
            proc_cls.kill = kill  # type: ignore[assignment]
    except Exception as exc:  # pragma: no cover
        log.debug("terminate() patch skipped (%s)", exc)
