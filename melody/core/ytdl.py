"""
🎵 YouTube downloader — yt-dlp wrapper

FIXES APPLIED:
  • Heroku CDN block detection — skips googlevideo.com CDN URLs that always
    fail on cloud IPs (Heroku DYNO, Railway, Render, Fly.io, etc.).
  • "Sign in to confirm you're not a bot" — uses android_music + ios + web
    player clients in order; these bypass the sign-in wall for public videos.
  • Instant streaming — download_audio() returns a FIFO named pipe on Linux
    (Heroku supports mkfifo).  yt-dlp writes audio to the pipe in a daemon
    thread; PyTgCalls starts reading before the full download finishes.
    Falls back to full file download if mkfifo is unavailable.
  • YT_COOKIES binary guard — already present; kept as-is.
  • WebM/Opus preferred format — streamable via FIFO without seeking,
    unlike AAC/M4A which requires the moov atom at EOF.
"""
import asyncio
import base64
from contextlib import contextmanager
import glob
import heapq
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile


# SPEED FIX: dedicated pools — see melody/core/pools.py for why the default
# executor was the real cause of the 10-15s wait before playback started.
from melody.core.pools import YTDL_POOL

# SPEED FIX: the direct-CDN URL race used to be allowed 25s. Nobody waits 25s
# for a song — if neither yt-dlp nor InnerTube has answered in this window the
# download fallback is already the faster route. Tunable via env.
try:
    # Keep direct resolution bounded because the local download races it. An
    # 8s resolver plus the Invidious rescue used to delay playback even when
    # the fallback file was already progressing.
    _RESOLVE_TIMEOUT = float(os.getenv("RESOLVE_TIMEOUT", "9.0"))
except ValueError:
    _RESOLVE_TIMEOUT = 6.0
_DIRECT_RESOLVE_MAX = max(1.0, float(os.getenv("DIRECT_RESOLVE_MAX", "9.0")))
_RESOLVE_TIMEOUT = min(_RESOLVE_TIMEOUT, _DIRECT_RESOLVE_MAX)

# How long InnerTube gets the CPU/network to itself before the heavy yt-dlp
# fallback is started as well (see resolve_stream_urls).
try:
    _INNERTUBE_HEADSTART = float(os.getenv("INNERTUBE_HEADSTART", "0.70"))
except Exception:  # noqa: BLE001
    _INNERTUBE_HEADSTART = 0.60

# Heroku/cloud IPs can return no usable InnerTube stream for every player
# client. Repeating that dead fan-out on every /play adds several seconds before
# the cookie-authenticated yt-dlp path even gets a chance. Mute the host-level
# InnerTube stream probe after consecutive failures and periodically re-test.
try:
    _IT_MUTE_AFTER = max(1, int(os.getenv("INNERTUBE_MUTE_AFTER", "2")))
except Exception:  # noqa: BLE001
    _IT_MUTE_AFTER = 1
try:
    _IT_MUTE_TTL = max(60.0, float(os.getenv("INNERTUBE_MUTE_TTL", "300")))
except Exception:  # noqa: BLE001
    _IT_MUTE_TTL = 300.0
_it_stream_fail_streak = 0
_it_stream_muted_until = 0.0
# Per-client mute tracking — a WEB failure should NOT mute IOS
_it_client_failures: dict = {}
_it_client_muted: dict = {}


def _innertube_stream_muted() -> bool:
    return _it_stream_muted_until > _time_mod.monotonic()


def _innertube_client_muted(client: str) -> bool:
    """Check if a SPECIFIC InnerTube client is muted (per-client, not global)."""
    until = _it_client_muted.get(client, 0.0)
    return until > _time_mod.monotonic()


def direct_stream_muted() -> bool:
    """Whether the WHOLE direct-CDN path should be skipped for this host.

    SPEED ROOT-CAUSE FIX (Heroku log: every /play logged `stream=9.8-27.3s`
    and no `#stream resolved direct ...` line at all): this used to return
    True as soon as the *InnerTube* probe had failed a few times, which also
    disabled the cookie-authenticated **yt-dlp** direct resolve — the only
    direct path that still works from a blocked datacenter IP. With both
    muted, every single song had to be downloaded in full (or to its early
    prefix) before a note was heard.

    Only the InnerTube fan-out is muted now (see `_innertube_stream_muted`);
    the yt-dlp direct resolve always gets its chance, and DIRECT_STREAM=false
    remains the explicit host-level opt-out.
    """
    return False


def _note_innertube_stream(ok: bool) -> None:
    """Track consecutive host-level InnerTube direct-stream failures.

    NOTE (Sep 20 fix): this global mute is kept for backward compatibility but
    the per-client mute (_note_innertube_client) is what actually gates which
    clients are probed. A WEB failure no longer mutes IOS.
    """
    global _it_stream_fail_streak, _it_stream_muted_until
    if ok:
        if _it_stream_muted_until or _it_stream_fail_streak:
            LOGGER.info("#stream innertube direct path healthy again — re-enabled")
        _it_stream_fail_streak = 0
        _it_stream_muted_until = 0.0
        return
    _it_stream_fail_streak += 1
    # Global mute only after many failures (not 2) — per-client mute handles individual dead clients
    if _it_stream_fail_streak >= max(_IT_MUTE_AFTER * 3, 6) and not _innertube_stream_muted():
        _it_stream_muted_until = _time_mod.monotonic() + _IT_MUTE_TTL
        LOGGER.info(
            "#stream innertube globally blocked on this host (%d failures) — "
            "skipping all InnerTube probes for %.0fs",
            _it_stream_fail_streak, _IT_MUTE_TTL,
        )


def _note_innertube_client(client: str, ok: bool) -> None:
    """Track per-client InnerTube failures — mute only the failing client."""
    if ok:
        if _it_client_muted.get(client) or _it_client_failures.get(client, 0):
            LOGGER.info("#stream innertube client %s healthy again", client)
        _it_client_failures.pop(client, None)
        _it_client_muted.pop(client, None)
        return
    fails = _it_client_failures.get(client, 0) + 1
    _it_client_failures[client] = fails
    if fails >= _IT_MUTE_AFTER and client not in _it_client_muted:
        _it_client_muted[client] = _time_mod.monotonic() + _IT_MUTE_TTL
        LOGGER.info(
            "#stream innertube client %s muted after %d failures — skipping for %.0fs",
            client, fails, _IT_MUTE_TTL,
        )

# How long the fast metadata race (YouTube Data API v3 + InnerTube) is given
# before falling back to yt-dlp. Kept short on purpose — see
# _get_video_info_once() for why sequential fallback used to cost 5-10s even
# with cookies/API keys configured.
try:
    _FAST_TIMEOUT = float(os.getenv("FAST_RESOLVE_TIMEOUT", "1.0"))
except ValueError:
    _FAST_TIMEOUT = 1.0

# ── Persistent HTTP client (connection pooling + DNS/TCP reuse) ──────────
# A single httpx.AsyncClient is reused across ALL InnerTube / Invidious /
# bgutil HTTP calls. This keeps TCP connections and DNS resolutions warm
# across requests, eliminating the per-request TCP handshake + DNS lookup
# latency that urllib.request paid on every single call.
import httpx as _httpx


class _NoStoreCookies(_httpx.Cookies):
    """Cookie jar that NEVER stores Set-Cookie from responses.

    SPEED ROOT CAUSE (Sep 10 log: `IOS -> LOGIN_REQUIRED`,
    `ANDROID_VR -> LOGIN_REQUIRED` on every /play): the pooled httpx client
    kept the cookies YouTube handed back to the *authenticated web* probes and
    then replayed them on the mobile InnerTube probes. The mobile clients are
    cookie-less by design — a logged-in session on them makes YouTube answer
    "Sign in to confirm you're not a bot", which killed the 0.3s fast path and
    forced the 5-7s yt-dlp resolve for every single song. Cookies are attached
    explicitly per request (web family only); nothing is ever remembered.
    """

    def extract_cookies(self, response) -> None:  # noqa: D102, ANN001
        return


_http_client: "_httpx.AsyncClient | None" = None
_http_client_lock = asyncio.Lock()
_http_sync_client: "_httpx.Client | None" = None
_http_sync_client_lock = threading.Lock()


def _http2_available() -> bool:
    """httpx only supports HTTP/2 when the optional `h2` package is installed;
    `httpx.Client(http2=True)` raises ImportError otherwise.

    BUG FIX ("search/autoplay kuch nahi mil raha", logs full of
    "InnerTube search: all client contexts failed"): `h2` was never added to
    requirements.txt, so BOTH pooled clients raised ImportError the moment
    they were constructed. Every InnerTube call sits inside a broad
    `except Exception: continue`, so all client contexts "failed" instantly
    for every single query — search, related-videos and AutoPlay all went
    dark while yt-dlp (which does not use these clients) kept working.
    `h2` is now in requirements.txt AND HTTP/2 degrades to HTTP/1.1 here
    instead of taking the whole HTTP layer down with it.
    """
    try:
        __import__("h2")
    except Exception:  # noqa: BLE001
        return False
    return True


def _http_client_kwargs() -> dict:
    kwargs: dict = {
        "http2": _http2_available(),
        "timeout": _httpx.Timeout(12.0, connect=5.0),
        "limits": _httpx.Limits(max_connections=20, max_keepalive_connections=10),
        "headers": {"User-Agent": "Mozilla/5.0 (compatible; MelodyBot/1.0)"},
        "follow_redirects": True,
        # See _NoStoreCookies: cross-client cookie bleed is what turned every
        # InnerTube probe into LOGIN_REQUIRED.
        "cookies": _NoStoreCookies(),
    }
    return kwargs


def get_http_sync_client() -> "_httpx.Client":
    """Return a process-wide persistent httpx.Client (sync) for use inside
    run_in_executor callbacks. Connection pooling + HTTP/2, same as async."""
    global _http_sync_client
    if _http_sync_client is not None and not _http_sync_client.is_closed:
        return _http_sync_client
    # This function is called from multiple YTDL_POOL threads. Without a
    # construction lock, two first requests both saw None and built/logged a
    # client, losing one connection pool and wasting startup work.
    with _http_sync_client_lock:
        if _http_sync_client is not None and not _http_sync_client.is_closed:
            return _http_sync_client
        _http_sync_client = _httpx.Client(**_http_client_kwargs())
        LOGGER.info("✅ Persistent sync HTTP client (connection pool) initialized")
        return _http_sync_client

# The build hook (bin/post_compile) installs the bgutil yt-dlp PO-token
# provider plugin under vendor/. Add that namespace to sys.path BEFORE
# importing yt_dlp so its plugin discovery picks it up. This is the real
# fix for YouTube's "Sign in to confirm you're not a bot" wall on Heroku —
# a Proof-of-Origin token, not a proxy, is what YouTube actually checks for
# on cloud/datacenter IPs.
_BGUTIL_PLUGIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "vendor", "bgutil-ytdlp-pot-provider", "plugin",
)
_BGUTIL_PLUGIN_DIR = os.path.realpath(os.path.normpath(_BGUTIL_PLUGIN_DIR))
if os.path.isdir(_BGUTIL_PLUGIN_DIR):
    # yt-dlp discovers namespace plugins from every sys.path entry. Heroku
    # can already expose the slug vendor directory through PYTHONPATH, while
    # this module also adds it explicitly; two equivalent entries make the
    # same bgutil modules execute twice and trigger "already registered".
    sys.path[:] = [
        entry for entry in sys.path
        if os.path.realpath(entry or os.curdir) != _BGUTIL_PLUGIN_DIR
    ]
    sys.path.insert(0, _BGUTIL_PLUGIN_DIR)

from yt_dlp import YoutubeDL
from melody.config import Config
from melody.logging import LOGGER, redact_sensitive_text, send_error_log


# yt-dlp loads namespace plugins from inside YoutubeDL.__init__. Two concurrent
# first-use constructors can both observe the global loader as incomplete and
# import bgutil twice, producing "provider already registered" tracebacks.
# Serialize only construction/close setup; extraction and downloads remain
# concurrent wherever their existing gates allow.
_YTDLP_INIT_LOCK = threading.Lock()


@contextmanager
def _locked_ytdl(opts: dict):
    with _YTDLP_INIT_LOCK:
        ydl = YoutubeDL(opts)
    # Keep yt-dlp's own console-title/cookie/request-director cleanup intact.
    with ydl as active:
        yield active


# ── Warm, reusable YoutubeDL for metadata/URL resolves ──────────────────────
# SPEED FIX (Sep 10 log: stream=3.2-3.6s on every cold /play): a brand-new
# YoutubeDL was constructed for each resolve, so its in-process caches (solved
# nsig/player functions, extractor state, the pooled TLS/HTTP connections of
# the request director) were thrown away every time and each song paid the
# handshake + player-solve cost again. One warm instance per option shape is
# reused instead; a fresh throwaway is only built when the warm one is busy
# with another track, and the instance is retired periodically so it can never
# accumulate state or stale cookies.
_WARM_YDL: dict = {}
_WARM_YDL_LOCK = threading.Lock()
try:
    _WARM_YDL_TTL = max(60.0, float(os.getenv("YTDLP_WARM_TTL", "900")))
except Exception:  # noqa: BLE001
    _WARM_YDL_TTL = 900.0


def _warm_extract(opts: dict, target: str, cache_key: str = "resolve"):
    """extract_info(download=False) on a warm YoutubeDL when one is free."""
    now = _time_mod.monotonic()
    entry = None
    with _WARM_YDL_LOCK:
        cached = _WARM_YDL.get(cache_key)
        if cached and cached["born"] + _WARM_YDL_TTL > now:
            entry = cached
        else:
            if cached:
                _WARM_YDL.pop(cache_key, None)
                try:
                    cached["ydl"].close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                with _YTDLP_INIT_LOCK:
                    entry = {
                        "ydl": YoutubeDL(opts),
                        "lock": threading.Lock(),
                        "born": now,
                    }
                _WARM_YDL[cache_key] = entry
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("warm YoutubeDL unavailable: %s", exc)
                entry = None

    if entry is not None and entry["lock"].acquire(blocking=False):
        try:
            return entry["ydl"].extract_info(target, download=False)
        except Exception:
            # A poisoned warm instance must not break later tracks.
            with _WARM_YDL_LOCK:
                if _WARM_YDL.get(cache_key) is entry:
                    _WARM_YDL.pop(cache_key, None)
            raise
        finally:
            entry["lock"].release()

    # Warm instance busy with another song — do not queue behind it.
    with _locked_ytdl(opts) as ydl:
        return ydl.extract_info(target, download=False)


# ── In-memory metadata cache (avoids re-querying YouTube for recently
# played songs — near-instant repeat playback) ─────────────────────────
import time as _time_mod
_META_CACHE_TTL = 3600  # 1 hour (in-memory / L1)
# Disk-backed (L2) TTL — deliberately much longer than the in-memory one.
# BUG FIX ("replay ek baar bhi restart ke baad slow ho jata hai"): the old
# cache was purely in-memory, so a Heroku dyno restart / redeploy wiped
# every previously-resolved song and the very next /play of an
# already-played track paid the full search cost again. utils/song_cache.py
# persists the same key -> metadata mapping to a sqlite file in /tmp, which
# survives process restarts (as long as /tmp itself isn't wiped).
_META_DISK_TTL = 259200  # 3 days
_meta_cache: dict = {}

# ── ⚡ Typo/word-order tolerant cache keys ────────────────────────────────
# REQUIREMENT ("played song ko instant play kar — thodi spelling vagera change
# ho sakti hai"): the cache used to key on the RAW query string, so
# "apna bana le", "Apna Bana Le song" and "apna banale" were three different
# keys and each one paid the full search cost again. Two extra aliases are now
# stored/looked up for every resolution:
#   q:  noise-word-stripped, token-SORTED key  -> word order + filler tolerant
#   f:  consonant-skeleton key                 -> minor spelling mistakes
# Exact key first, then q:, then f:, so a fuzzy alias can never override a
# precise hit.
_QUERY_NOISE_WORDS = {
    "song", "songs", "gana", "gaana", "gane", "play", "full", "audio", "video",
    "lyrics", "lyrical", "official", "mp3", "mp4", "hd", "4k", "new", "latest",
    "version", "remix", "original", "track", "music", "bajao", "sunao", "please",
}


def _norm_query_key(query: "str | None") -> "str | None":
    if not query:
        return None
    text = query.strip().lower()
    if text.startswith("http") or text.startswith("www."):
        return None
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    tokens = [t for t in text.split() if t and t not in _QUERY_NOISE_WORDS]
    if not tokens:
        return None
    return "q:" + " ".join(sorted(tokens))


def _fuzzy_query_key(query: "str | None") -> "str | None":
    """Consonant skeleton of the normalised key — "sanam re" / "sanamre" /
    "sannam re" all collapse to the same alias. Deliberately only used for
    queries long enough that a collision is unlikely."""
    norm = _norm_query_key(query)
    if not norm:
        return None
    body = norm[2:].replace(" ", "")
    if len(body) < 8:
        return None
    skeleton = re.sub(r"(.)\1+", r"\1", body)          # collapse doubles
    skeleton = re.sub(r"(?!^)[aeiouyh]", "", skeleton)  # drop inner vowels
    if len(skeleton) < 5:
        return None
    return "f:" + skeleton


def _meta_cache_get(key: str) -> "dict | None":
    entry = _meta_cache.get(key)
    if entry:
        if _time_mod.time() - entry["ts"] <= _META_CACHE_TTL:
            return entry["data"]
        _meta_cache.pop(key, None)
    # L2: disk-backed cache — survives restarts, so a song played before this
    # process last restarted still resolves near-instantly instead of paying
    # the full search cost again.
    try:
        from utils.song_cache import get_persistent_meta
        data = get_persistent_meta(key, ttl=_META_DISK_TTL)
        if data:
            _meta_cache[key] = {"data": data, "ts": _time_mod.time()}
            return data
    except Exception:
        pass

    # ⚡ Typo / word-order tolerant aliases (see _norm_query_key above).
    if not key.startswith(("q:", "f:", "id:")):
        for alias in (_norm_query_key(key), _fuzzy_query_key(key)):
            if not alias:
                continue
            data = _meta_cache_get(alias)
            if data:
                LOGGER.debug("⚡ meta cache alias HIT %s -> %s", key[:40], alias[:40])
                _meta_cache[key] = {"data": data, "ts": _time_mod.time()}
                return data
    return None

def _meta_cache_put(key: str, info: dict) -> None:
    _meta_cache[key] = {"data": info, "ts": _time_mod.time()}
    # LATENCY FIX: also alias the entry by its stable YouTube video id, so the
    # SAME song asked for with a different query string ("apna bana le" vs
    # "apna bana le song" vs the direct URL) still resolves from cache instead
    # of paying the full search + extraction cost again.
    aliases = [key]
    try:
        vid = (info or {}).get("id")
        if vid:
            id_key = f"id:{vid}"
            _meta_cache[id_key] = {"data": info, "ts": _time_mod.time()}
            aliases.append(id_key)
    except Exception:
        pass
    # Typo/word-order tolerant aliases for BOTH the query the user typed and
    # the resolved song title, so "apna banale" later hits "Apna Bana Le".
    try:
        title = (info or {}).get("title") or ""
        for source in (key if not key.startswith(("q:", "f:", "id:")) else "", title):
            for alias in (_norm_query_key(source), _fuzzy_query_key(source)):
                if alias and alias not in aliases:
                    _meta_cache[alias] = {"data": info, "ts": _time_mod.time()}
                    aliases.append(alias)
    except Exception:
        pass
    if len(_meta_cache) > 200:
        oldest = min(_meta_cache, key=lambda k: _meta_cache[k]["ts"])
        _meta_cache.pop(oldest, None)
    # Mirror to the disk-backed L2 cache so this resolution survives restarts.
    try:
        from utils.song_cache import put_persistent_meta
        for alias in aliases:
            put_persistent_meta(alias, info)
    except Exception:
        pass

# ── In-flight download dedup (prevents duplicate yt-dlp processes for the
# same video when play.py pre-starts a download and _stream_track also calls
# download_audio for the same track) ────────────────────────────────────
_download_futures: dict = {}
_download_jobs: set[asyncio.Task] = set()
_download_futures_lock = asyncio.Lock()
# dedup_key -> (cooperative cancel event, owner token, priority). A newer
# request may stop only low-priority work; a different chat sharing the same
# video is never canceled when its key is explicitly excluded.
_download_cancel_events: dict[str, tuple[threading.Event, object | None, int]] = {}


def cancel_download(video_id: str, audio_only: bool = True, owner=None) -> bool:
    """Request cancellation of an owned in-flight yt-dlp download.

    Cancellation is cooperative: the progress hook stops the extractor at a
    safe boundary, partial staging output is cleaned by the normal finally
    path, and the download future propagates a dedicated cancellation error.
    """
    key = f"{video_id}:{_cache_tag(audio_only)}"
    entry = _download_cancel_events.get(key)
    if entry is None:
        return False
    event, current_owner, _priority = entry
    if owner is not None and current_owner is not None and current_owner != owner:
        return False
    event.set()
    return True


def cancel_lower_priority_downloads(
    max_priority: int = 0,
    *,
    exclude_video_id: str | None = None,
) -> int:
    """Cooperatively stop running/waiting work below interactive priority.

    This is deliberately synchronous: it only sets threading events, so a
    manual /play can do it before awaiting any resolver. The excluded video is
    important because an interactive request may deduplicate onto an existing
    low-priority AutoPlay download for the same media; canceling that shared
    future would cancel the manual request too.
    """
    canceled = 0
    for key, entry in list(_download_cancel_events.items()):
        try:
            event, _owner, priority = entry
        except (TypeError, ValueError):
            continue
        video_id = key.rsplit(":", 1)[0]
        if exclude_video_id and video_id == exclude_video_id:
            continue
        if int(priority) <= int(max_priority) or event.is_set():
            continue
        event.set()
        canceled += 1
    if canceled:
        LOGGER.info("#download preempted %d low-priority job(s)", canceled)
    return canceled


class _DownloadCancelled(Exception):
    """Internal cooperative cancellation for superseded playback work."""


class _EarlyDownloadState:
    """Shared state for a safe audio-prefix handoff.

    ``future`` remains pending until the complete atomic cache file exists;
    ``ready`` is only the optional signal for the one playback caller that is
    allowed to consume a validated audio prefix while the worker continues.
    """

    def __init__(self):
        self.ready = asyncio.Event()
        self.path: str | None = None


_download_early_states: dict[str, _EarlyDownloadState] = {}


def is_download_cancelled(exc: BaseException) -> bool:
    return isinstance(exc, _DownloadCancelled)


class _PriorityDownloadGate:
    """Bounded extractor gate with interactive work ahead of prefetch work."""

    def __init__(self, capacity: int):
        self.capacity = max(1, capacity)
        self.running = 0
        self.sequence = 0
        self.waiters: list[tuple[int, int, asyncio.Future]] = []
        self.lock = asyncio.Lock()

    async def acquire(self, priority: int = 0) -> None:
        async with self.lock:
            if self.running < self.capacity and not self.waiters:
                self.running += 1
                return
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self.sequence += 1
            heapq.heappush(self.waiters, (int(priority), self.sequence, future))
        try:
            await future
        except asyncio.CancelledError:
            async with self.lock:
                for index, item in enumerate(self.waiters):
                    if item[2] is future:
                        self.waiters.pop(index)
                        heapq.heapify(self.waiters)
                        break
            raise

    async def release(self) -> None:
        async with self.lock:
            self.running = max(0, self.running - 1)
            while self.waiters:
                _, _, future = heapq.heappop(self.waiters)
                if future.cancelled():
                    continue
                self.running += 1
                future.set_result(None)
                break


def _consume_download_future(future: asyncio.Future) -> None:
    """Mark a shared download future's exception as observed.

    A caller may cancel or be superseded after the extractor has already
    completed. The shared future still receives that result for other waiters,
    so observing it here prevents asyncio's noisy un-retrieved-exception
    warning without changing the exception delivered to active callers.
    """
    if future.cancelled():
        return
    try:
        future.exception()
    except (asyncio.CancelledError, Exception):
        pass

# ROOT-CAUSE FIX for `heroku[worker.1]: Error R14 (Memory quota exceeded)`
# (log: mem=1056M(102.3%) -> mem=1613M(157.4%) in 18 seconds).
#
# `_download_audio_impl()` spawned a RAW `threading.Thread` per download with
# no global limit. Each one builds a complete YoutubeDL object: extractor
# state, the parsed watch page / InnerTube JSON, format lists and yt-dlp's own
# read buffers - tens of MB live per download, and glibc keeps those arenas.
# With several groups playing at once (the attached log shows five different
# chats active inside the same 10 seconds) RSS multiplied straight past the
# dyno quota. Nothing throttled it, which is why memguard could never win:
# memory was being allocated faster than a trim could hand it back.
#
# A global semaphore caps how many downloads RUN at once; extra /play requests
# wait a moment instead of each reserving its own copy of yt-dlp's working
# set. One concurrent extractor is the safe default for a 512 MB dyno; larger
# deployments may opt into two, but the hard cap prevents accidental R14 spikes.
def _memory_budget_mb() -> int:
    for name in ("MEMORY_LIMIT_MB", "MEMORY_AVAILABLE_MB", "DYNO_RAM_MB"):
        raw = (os.getenv(name) or "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError:
                continue
            if value > 0:
                return value
    return 512


_MEMORY_BUDGET_MB = _memory_budget_mb()
# A 1 GB Heroku dyno can still cross R14 when two yt-dlp extractors,
# ffmpeg, and the Telegram client overlap. Keep one working set by default;
# only explicitly larger dynos may use two.
_DEFAULT_CONCURRENT_DOWNLOADS = 1 if _MEMORY_BUDGET_MB <= 1024 else 2
try:
    _requested_downloads = max(
        1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "") or _DEFAULT_CONCURRENT_DOWNLOADS)
    )
except ValueError:
    _requested_downloads = _DEFAULT_CONCURRENT_DOWNLOADS
_MAX_CONCURRENT_DOWNLOADS = (
    1 if _MEMORY_BUDGET_MB <= 1024 else min(2, _requested_downloads)
)
_download_slots: "_PriorityDownloadGate | None" = None
def _download_semaphore() -> "_PriorityDownloadGate":
    """Lazy priority gate - created on the running loop, never at import time."""
    global _download_slots
    if _download_slots is None:
        _download_slots = _PriorityDownloadGate(_MAX_CONCURRENT_DOWNLOADS)
    return _download_slots


_BGUTIL_STATUS_LOGGED = False
# `_ydl_opts()` runs in multiple YTDL_POOL threads. Keep provider selection and
# the one-time status transition atomic so the first concurrent requests cannot
# all choose script mode or emit duplicate bgutil registration/configuration logs.
_BGUTIL_CONFIG_LOCK = threading.Lock()


def _bgutil_server_home() -> str:
    """Return the build-bundled bgutil PO-token provider server directory."""
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "vendor", "bgutil-ytdlp-pot-provider", "server",
    ))


_BGUTIL_HTTP_PORT = 4416
_BGUTIL_HTTP_PROC: "subprocess.Popen | None" = None
_BGUTIL_HTTP_READY = False
_BGUTIL_HTTP_LOCK = threading.Lock()


def ensure_bgutil_http_server() -> bool:
    """Start bgutil's PO-token provider as a persistent local HTTP server
    (idempotent — safe to call more than once) and return whether it's up.

    SPEED FIX ("gana bajne me 5-10 sec lag rahe hai" even after the
    early-handoff/pre-download work): every /play still has to resolve a
    fresh PO token before yt-dlp can pick a downloadable format — YouTube
    requires one for essentially all clients now. This bot got that token
    via bgutil's "script" method (`generate_once.ts`), which the upstream
    project itself documents as much slower than the HTTP server: EVERY
    single call spins up a brand-new Deno runtime + BotGuard/session state
    from scratch, solves the challenge, prints the token, and exits — there
    is nothing warm to reuse, so this cost was paid again on every track,
    stacking directly on top of the search + download time already on the
    critical path. That reliably accounts for multiple extra seconds of the
    "5-10 sec" delay on every single song, not just occasionally.

    The bundled `server/src/main.ts` is the same provider running as a
    long-lived process instead: it keeps the Deno/BotGuard session and a
    per-video token cache warm across requests, so once it's up, each
    subsequent PO-token fetch is a fast local HTTP call instead of a fresh
    process spawn. Starting it once here (in the background, at bot
    startup) means it's already warmed up long before the first /play.

    Falls back cleanly to the old script-based provider (see _ydl_opts())
    if Deno/the server files aren't available or the server never comes up
    — this is a pure addition, never a regression.
    """
    global _BGUTIL_HTTP_PROC
    global _BGUTIL_HTTP_READY

    with _BGUTIL_HTTP_LOCK:
        if _BGUTIL_HTTP_READY:
            # The old health flag was permanent: once the provider answered
            # successfully, a later Deno crash/OOM left `_BGUTIL_HTTP_READY`
            # true forever. Every yt-dlp request then kept selecting a dead
            # localhost provider and direct YouTube formats disappeared until
            # the whole dyno restarted. Treat process death as a recoverable
            # condition and let the next request restart the provider.
            if _BGUTIL_HTTP_PROC is not None and _BGUTIL_HTTP_PROC.poll() is None:
                return True
            LOGGER.warning("⚠️ bgutil PO-token server stopped — resetting health and restarting")
            _BGUTIL_HTTP_READY = False
            _BGUTIL_HTTP_PROC = None
        if _BGUTIL_HTTP_PROC is not None and _BGUTIL_HTTP_PROC.poll() is None:
            return False  # already starting; caller can retry _bgutil_http_alive() shortly

        server_home = _bgutil_server_home()
        main_ts = os.path.join(server_home, "src", "main.ts")
        deno = _deno_path()
        if not os.path.isfile(main_ts) or not (os.path.isfile(deno) or shutil.which(deno)):
            return False

        try:
            os.makedirs(os.path.join(server_home, "cache"), exist_ok=True)
            _BGUTIL_HTTP_PROC = subprocess.Popen(
                [
                    deno, "run",
                    "--allow-env", "--allow-net",
                    f"--allow-ffi={os.path.join(server_home, 'node_modules')}",
                    f"--allow-write={os.path.join(server_home, 'cache')}",
                    f"--allow-read={os.path.join(server_home, 'cache')},{os.path.join(server_home, 'node_modules')}",
                    main_ts, "--port", str(_BGUTIL_HTTP_PORT),
                ],
                cwd=server_home,
                env={**os.environ, "XDG_CACHE_HOME": os.path.join(server_home, "cache")},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            LOGGER.info("🚀 bgutil PO-token HTTP server starting on 127.0.0.1:%d", _BGUTIL_HTTP_PORT)
            return False  # not confirmed ready yet — see _bgutil_http_alive()
        except Exception as exc:
            LOGGER.warning("Could not start bgutil HTTP server, falling back to script mode: %s", exc)
            _BGUTIL_HTTP_PROC = None
            return False


def _wait_for_bgutil_http(timeout: float = 3.0) -> bool:
    """Wait a short bounded period for a server another request just started."""
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if _bgutil_http_alive():
            return True
        proc = _BGUTIL_HTTP_PROC
        if proc is None or proc.poll() is not None:
            return False
        time.sleep(0.1)
    return _bgutil_http_alive()


def _bgutil_http_alive() -> bool:
    """Best-effort /ping check against the local bgutil HTTP server."""
    global _BGUTIL_HTTP_READY
    global _BGUTIL_HTTP_PROC
    previously_ready = _BGUTIL_HTTP_READY
    if _BGUTIL_HTTP_READY:
        proc = _BGUTIL_HTTP_PROC
        if proc is None or proc.poll() is not None:
            # Do not trust a stale success flag after Deno/bgutil exits.
            _BGUTIL_HTTP_READY = False
            LOGGER.warning("⚠️ bgutil health flag was stale — provider process is not alive")
    try:
        client = get_http_sync_client()
        resp = client.get(f"http://127.0.0.1:{_BGUTIL_HTTP_PORT}/ping", timeout=1.5)
        if resp.status_code == 200:
            _BGUTIL_HTTP_READY = True
            LOGGER.info("✅ bgutil PO-token HTTP server is up — using warm HTTP provider (fast path)")
            return True
    except Exception:
        pass
    # A failed ping must be allowed to trigger a fresh start on the next
    # request instead of leaving the provider permanently marked healthy.
    _BGUTIL_HTTP_READY = False
    if previously_ready:
        # A live-but-frozen Deno process is just as bad as a dead one. Do not
        # leave it in `_BGUTIL_HTTP_PROC`, because ensure_bgutil_http_server()
        # would interpret that as "already starting" forever and every yt-dlp
        # request would silently fall back to the slow script provider.
        proc = _BGUTIL_HTTP_PROC
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=0.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            LOGGER.warning("⚠️ bgutil PO-token server became unresponsive — restarting it")
        _BGUTIL_HTTP_PROC = None
    return False


async def warm_up_bgutil_server(timeout: float = 20.0) -> None:
    """Called once at bot startup: make sure a JS runtime exists, then kick
    off the persistent bgutil HTTP server and poll until it responds (or
    times out), so it's warmed up before the first /play.
    """
    loop = asyncio.get_running_loop()
    # Deno first — bgutil literally cannot run without it, and a failed
    # build-time download must not leave playback broken until a redeploy.
    await loop.run_in_executor(YTDL_POOL, ensure_deno_runtime)
    if not ensure_bgutil_http_server() and _BGUTIL_HTTP_PROC is None:
        return  # server files not available — script-mode fallback will be used

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await loop.run_in_executor(YTDL_POOL, _bgutil_http_alive):
            return
        await asyncio.sleep(0.5)
    LOGGER.warning(
        "bgutil HTTP server did not respond within %.0fs — falling back to (slower) script mode",
        timeout,
    )


_DENO_RUNTIME_DIR = "/tmp/melody_deno"


def _ensure_deno_cache_env() -> None:
    """Point Deno at the module cache baked into the slug.

    LOG FIX: without a stable DENO_DIR, the first PO-token run re-resolved the
    bgutil npm dependency tree at runtime and printed ~400 "Download
    https://registry.npmjs.org/..." lines into the worker log (and cost several
    seconds on the first /play). The build already warms this cache — reusing
    it keeps startup silent and fast.
    """
    if os.environ.get("DENO_DIR"):
        return
    for candidate in (
        os.path.normpath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", "vendor", "deno_cache",
        )),
        "/app/vendor/deno_cache",
    ):
        if os.path.isdir(candidate):
            os.environ["DENO_DIR"] = candidate
            return
    fallback = os.path.join(_DENO_RUNTIME_DIR, "cache")
    os.makedirs(fallback, exist_ok=True)
    os.environ["DENO_DIR"] = fallback


_ensure_deno_cache_env()
_DENO_LOCK = threading.Lock()
_DENO_MIRRORS = (
    # dl.deno.land is Deno's own CDN and stays up when GitHub's release
    # download endpoint answers 503 / "Connection died" (exactly what killed
    # the Heroku build: "⚠️ Deno download failed").
    "https://dl.deno.land/release/{ver}/deno-x86_64-unknown-linux-gnu.zip",
    "https://github.com/denoland/deno/releases/download/{ver}/deno-x86_64-unknown-linux-gnu.zip",
    "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip",
)


def _deno_path() -> str:
    """Return the build-bundled (or system/runtime-installed) Deno path.

    yt-dlp's YouTube extractor needs a JS runtime to solve the player
    challenge and to run the bgutil PO-token generator script.
    """
    bundled = os.path.normpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "vendor", "deno", "bin", "deno",
    ))
    if os.path.isfile(bundled):
        return bundled
    runtime = os.path.join(_DENO_RUNTIME_DIR, "deno")
    if os.path.isfile(runtime):
        return runtime
    return shutil.which("deno") or "deno"


def _has_deno() -> bool:
    """True only when a real Deno binary exists (never a bare 'deno' guess)."""
    path = _deno_path()
    return bool(os.path.isfile(path) or shutil.which(path))


def ensure_deno_runtime() -> bool:
    """Self-heal a missing Deno binary by downloading it into /tmp at runtime.

    ROOT-CAUSE FIX ("gana nahi baja, bas skip skip ho raha" +
    "Requested format is not available" for EVERY video):
    the Heroku build log shows both vendored downloads failing —
    "Connection died, tried 5 times before giving up / ⚠️ Deno download
    failed" and four `curl: (22) ... 503` for bgutil. Without Deno there is
    no JS runtime, so yt-dlp cannot solve the player challenge and cannot
    generate a PO token; YouTube then only offers PO-gated / ciphered
    formats, yt-dlp drops all of them, and every single attempt (stream and
    download, all 8 ladder rungs) dies with "Requested format is not
    available" → the track is skipped instantly.

    A build-time-only install means one bad GitHub day bricks playback until
    the next redeploy. Fetching the binary at startup (multiple mirrors,
    /tmp is writable on a dyno) makes the bot recover by itself on restart.
    """
    with _DENO_LOCK:
        if _has_deno():
            return True
        version = "v2.9.5"
        try:
            client = get_http_sync_client()
            resp = client.get("https://dl.deno.land/release-latest.txt", timeout=6)
            if resp.status_code == 200 and resp.text.strip().startswith("v"):
                version = resp.text.strip()
        except Exception:
            pass

        os.makedirs(_DENO_RUNTIME_DIR, exist_ok=True)
        target = os.path.join(_DENO_RUNTIME_DIR, "deno")
        for mirror in _DENO_MIRRORS:
            url = mirror.format(ver=version)
            try:
                client = get_http_sync_client()
                resp = client.get(url, timeout=90, follow_redirects=True)
                if resp.status_code != 200 or len(resp.content) < 1_000_000:
                    continue
                archive = os.path.join(_DENO_RUNTIME_DIR, "deno.zip")
                with open(archive, "wb") as fh:
                    fh.write(resp.content)
                with zipfile.ZipFile(archive) as zf:
                    zf.extract("deno", _DENO_RUNTIME_DIR)
                os.chmod(target, 0o755)
                os.unlink(archive)
                LOGGER.info("✅ Deno %s installed at runtime (%s) — YouTube player challenge solvable again", version, target)
                return True
            except Exception as exc:
                LOGGER.warning("Deno mirror failed (%s): %s", url, exc)
        LOGGER.error(
            "❌ Could not install Deno — YouTube formats stay PO-token gated. "
            "Redeploy so bin/post_compile can vendor Deno."
        )
        return False



# Check whether curl_cffi is available (enables Chrome TLS impersonation,
# which bypasses YouTube's bot-detection on Heroku / cloud IPs).
try:
    __import__("curl_cffi")
    _CURL_CFFI_OK = True
    LOGGER.info("✅ curl_cffi available — Chrome TLS impersonation enabled")
except ImportError:
    _CURL_CFFI_OK = False
    LOGGER.warning(
        "curl_cffi not installed — YouTube may block requests on cloud IPs. "
        "Add 'curl_cffi>=0.7.0' to requirements.txt and redeploy."
    )


COOKIES_FILE = "/tmp/melody_yt_cookies.txt"

# ── Cloud-host detection ──────────────────────────────────────────────────────
# Cloud hosts are more likely to have a blocked/throttled googlevideo route,
# but they are not universally blocked. The old code treated DYNO=True as
# proof that direct streaming could never work and therefore forced every
# first play to wait for a complete download. Runtime evidence showed metadata
# + VC join completed in 0.79s while the user still heard 10-20s of silence.
# Normal YouTube playback races the direct CDN URL against the audio download:
# a working CDN starts quickly, while the download remains a safe fallback.
# DIRECT_STREAM=false is an explicit opt-out for hosts whose CDN route is blocked.
# Raw live HLS URLs remain direct.
_ON_CLOUD_HOST: bool = bool(
    os.environ.get("DYNO")                    # Heroku
    or os.environ.get("RAILWAY_ENVIRONMENT")  # Railway
    or os.environ.get("RENDER_SERVICE_ID")    # Render
    or os.environ.get("FLY_APP_NAME")         # Fly.io
    or os.environ.get("K_SERVICE")            # Google Cloud Run
    or os.environ.get("WEBSITE_INSTANCE_ID")  # Azure App Service
)
if _ON_CLOUD_HOST:
    LOGGER.info("☁️  Cloud host detected — direct CDN + download fallback race enabled")


def should_try_direct_stream() -> bool:
    # Direct stream is the fastest path. If a cloud CDN route rejects it,
    # call.py keeps the download fallback running in parallel.
    return os.getenv("DIRECT_STREAM", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }

# ─────────────────────────────────────────────────────────────────────────────
#  Cookie helpers (unchanged — binary-guard logic kept)
# ─────────────────────────────────────────────────────────────────────────────

def _json_cookies_to_netscape(json_text: str) -> str:
    import json
    try:
        cookies = json.loads(json_text)
    except (json.JSONDecodeError, ValueError):
        return json_text
    if not isinstance(cookies, list):
        return json_text
    lines = ["# Netscape HTTP Cookie File", "# Generated by Melody"]
    for c in cookies:
        domain = c.get("domain", "")
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure", False) else "FALSE"
        expiry = int(c.get("expirationDate") or c.get("expires") or 0)
        name = c.get("name", "")
        value = c.get("value", "")
        lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def _normalize_netscape(text: str) -> str:
    """Return a cookies.txt yt-dlp will actually accept.

    ROOT-CAUSE FIX (Heroku log: "ERROR: '/tmp/melody_yt_cookies.txt' does not
    look like a Netscape format cookies file" -> every download attempt failed
    and playback fell back to slow retries): the raw config value was written
    verbatim. yt-dlp requires the literal "# Netscape HTTP Cookie File" header
    line AND tab-separated fields, but config vars pasted through a browser or
    a shell routinely lose the tabs (turning them into runs of spaces) or drop
    the header. We rebuild both here.
    """
    out = ["# Netscape HTTP Cookie File", "# Generated by Melody"]
    for line in text.lstrip("\ufeff").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) != 7:
            parts = stripped.split()
            if len(parts) > 7:  # value itself contained spaces
                parts = parts[:6] + [" ".join(parts[6:])]
        if len(parts) == 6:  # some exporters omit the (optional) value
            parts.append("")
        if len(parts) != 7:
            continue
        out.append("\t".join(parts))
    return "\n".join(out) + "\n"


def _is_netscape_cookies(text: str) -> bool:
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line.split("\t")) == 7 or len(line.split()) >= 6:
            return True
    return False


# ── Per-run cookie copies ─────────────────────────────────────────────────────
# ROOT-CAUSE FIX for the recurring Heroku error
#   ERROR: '/tmp/melody_yt_cookies.txt' does not look like a Netscape format
#          cookies file
# even though startup logged "YT_COOKIES (plain Netscape text) written".
# yt-dlp SAVES the cookie jar back to `cookiefile` when a YoutubeDL instance is
# closed. Melody runs several extractions in parallel (download + AutoPlay
# prefetch + stream-url warm), so two writers truncate/interleave the same file
# and the surviving first line is no longer the "# Netscape HTTP Cookie File"
# header — every later download then fails on a file we wrote ourselves.
# Fix: keep the normalized text in memory as the single source of truth and
# hand every yt-dlp run its OWN throwaway copy, so a write-back can never
# corrupt the master.
_COOKIE_TEXT: str = ""
_COOKIE_DIR = "/tmp/melody_cookies"
_COOKIE_LOCK = threading.Lock()
_COOKIE_TTL_SECONDS = 900


def _write_atomic(path: str, text: str) -> None:
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _reap_cookie_copies() -> None:
    cutoff = time.time() - _COOKIE_TTL_SECONDS
    try:
        for name in os.listdir(_COOKIE_DIR):
            path = os.path.join(_COOKIE_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def cookiefile_for_run() -> "str | None":
    """Fresh private copy of the cookie jar for one yt-dlp run (or None)."""
    if not _COOKIE_TEXT:
        return None
    with _COOKIE_LOCK:
        try:
            os.makedirs(_COOKIE_DIR, exist_ok=True)
            _reap_cookie_copies()
            path = os.path.join(
                _COOKIE_DIR, f"c_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.txt"
            )
            _write_atomic(path, _COOKIE_TEXT)
            return path
        except OSError as exc:
            LOGGER.warning("cookie copy failed (%s) — falling back to master file", exc)
            return COOKIES_FILE if os.path.exists(COOKIES_FILE) else None


def _store_cookies(text: str) -> None:
    """Normalize once, keep in memory, and mirror to the master file."""
    global _COOKIE_TEXT
    _COOKIE_TEXT = _normalize_netscape(text)
    _write_atomic(COOKIES_FILE, _COOKIE_TEXT)



def _write_cookies():
    """Load YT_COOKIES into COOKIES_FILE.

    ROOT-CAUSE FIX (previous bug):
    The old code ALWAYS ran base64.b64decode() first, even when YT_COOKIES
    already contained plain-text cookies.txt content (the most common way
    people paste it into Heroku config vars). Python's base64 decoder does
    NOT error on arbitrary text — it silently mangles it into garbage bytes,
    which then fail the UTF-8 decode and get logged as "binary data" and
    thrown away. This is why cookies never worked even when pasted correctly.

    NEW ORDER:
    1. Check if the raw value is ALREADY plain Netscape text or a JSON cookie
       array (no decoding needed) — use it directly.
    2. Only if that fails, attempt strict base64 decoding (validate=True so
       non-base64 text raises immediately instead of being silently corrupted).
    """
    if not Config.YT_COOKIES:
        LOGGER.warning(
            "⚠️ YT_COOKIES is not set — bot will run WITHOUT a YouTube login. "
            "Heroku IPs are heavily bot-checked; cookies are strongly recommended."
        )
        return

    raw = Config.YT_COOKIES.strip()

    # ── Path 1: already plain text (most common case) ──────────────────────
    if raw.startswith("["):
        netscape = _json_cookies_to_netscape(raw)
        if _is_netscape_cookies(netscape):
            _store_cookies(netscape)
            LOGGER.info("✅ YT_COOKIES (plain JSON→Netscape) written to %s", COOKIES_FILE)
            return
    if raw.startswith("#") or _is_netscape_cookies(raw):
        _store_cookies(raw)
        LOGGER.info("✅ YT_COOKIES (plain Netscape text) written to %s", COOKIES_FILE)
        return

    # ── Path 2: base64-encoded (strict — fail loudly instead of corrupting) ─
    try:
        raw_bytes = base64.b64decode(raw, validate=True)
    except Exception as e:
        LOGGER.warning(
            "❌ YT_COOKIES is neither plain cookies.txt text nor valid base64 — "
            "skipping cookies: %s", e
        )
        return
    try:
        decoded = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        LOGGER.warning(
            "❌ YT_COOKIES base64 decodes to binary data (not a text cookie file). "
            "Paste the raw cookies.txt content directly into the config var — "
            "no base64 encoding needed. Skipping cookies."
        )
        return
    decoded = decoded.strip()
    if decoded.startswith("["):
        netscape = _json_cookies_to_netscape(decoded)
        if not _is_netscape_cookies(netscape):
            LOGGER.warning("❌ YT_COOKIES JSON conversion produced no valid entries — skipping.")
            return
        _store_cookies(netscape)
        LOGGER.info("✅ YT_COOKIES (base64 JSON→Netscape) written to %s", COOKIES_FILE)
        return
    if _is_netscape_cookies(decoded):
        _store_cookies(decoded)
        LOGGER.info("✅ YT_COOKIES (base64 Netscape) written to %s", COOKIES_FILE)
        return
    LOGGER.warning(
        "❌ YT_COOKIES format not recognised (not JSON array, not Netscape). "
        "Export cookies using a browser extension like 'Get cookies.txt LOCALLY' "
        "while logged into youtube.com, then paste the file content as-is. "
        "Skipping cookies — yt-dlp will run without authentication."
    )


_write_cookies()
_HAS_COOKIES = bool(_COOKIE_TEXT)
if _HAS_COOKIES:
    LOGGER.info("🍪 Cookie-authenticated YouTube session ACTIVE — using logged-in requests")
else:
    LOGGER.warning("🍪 No cookies loaded — running as anonymous guest (more likely to be blocked on Heroku)")


# ─────────────────────────────────────────────────────────────────────────────
#  yt-dlp options
# ─────────────────────────────────────────────────────────────────────────────

# Terminal fallback used whenever NO audio-only format exists (HLS-only
# extractions on blocked cloud IPs). Plain `best` there means a 1080p muxed
# file of 60-95 MB and a 10s wait; the smallest muxed rendition carries the
# same AAC audio in a few MB. Ordered smallest-usable first, `best` last so
# the selector can never fail outright.
_SMALL_MUXED_SELECTOR = (
    "best[acodec!=none][height<=144]/"
    "best[acodec!=none][height<=240]/"
    "best[acodec!=none][height<=360]/"
    "worst[acodec!=none]/worst/best"
)


def _ydl_opts(audio_only: bool = True) -> dict:
    """Return base yt-dlp options tuned for Heroku and bot-detection bypass.

    The bgutil provider is started lazily here rather than at process boot. A
    deployment that never plays music should not pay the Deno process RSS cost,
    while the first actual yt-dlp fallback still gets the provider started.

    FIXES:
    • "Sign in to confirm you're not a bot":
        android_music → ios → android → tv_embedded → mweb → web
        Android/iOS clients are served a different API path that skips the
        sign-in wall for public videos.  This is the canonical yt-dlp fix.
    • WebM/Opus format preferred — streamable via FIFO pipe without seeking.
      AAC/M4A requires the moov atom at end-of-file → breaks pipe mode.
    • geo_bypass — Heroku USA servers sometimes hit geo-restricted content;
      bypass declaration helps with most non-DRM videos.
    • concurrent_fragment_downloads=4 (SPEED FIX — see below).
    """
    fmt = (
        # SPEED FIX: prefer WebM/Opus audio because its headers are at the
        # beginning of the file, allowing safe growing-file early handoff;
        # the completed file is still atomically cached in the background.
        # directly on the critical path to "song plays". The old selector
        # ("bestaudio/best") happily grabbed the highest-bitrate stream
        # available (often 160-250kbps opus/webm), which can be 2-3x the
        # bytes of a perfectly good voice-chat-quality stream for zero
        # Prefer high-quality stereo audio. The configurable ceiling avoids
        # pulling an unnecessarily huge source on a small cloud worker while
        # still allowing 160kbps/192kbps sources when YouTube exposes them.
        # SPEED FIX (Sep 10 Heroku log: download 8.97s, stream 10.71s):
        # a 160-192kbps source is 3-4x the bytes needed for a 48kHz voice
        # chat, and HLS/m4a cannot be handed off early (moov atom at EOF).
        # WebM/Opus carries its headers at the START, so the growing .part
        # file is playable within ~1s.
        f"bestaudio[ext=webm][abr<={_env_int('YT_AUDIO_MAX_ABR', 128)}]/"
        "bestaudio[ext=webm][abr<=96]/"
        "bestaudio[ext=webm]/"
        "bestaudio[ext=opus]/bestaudio[ext=ogg]/"
        "bestaudio[acodec=opus]/bestaudio[abr<=128]/"
        # Sep 10 Heroku log fix: on hosts where YouTube hides every WebM/Opus
        # format the chain used to fall straight through to "best" — a 77MB
        # muxed mp4 that cannot be handed off early (moov at EOF). Audio-only
        # m4a (itag 140/139) is fragmented MP4 with headers FIRST, is ~5% of
        # the bytes, and is moov-verified before early handoff below.
        "bestaudio[ext=m4a]/bestaudio[format_id=140]/bestaudio[format_id=139]/"
        "bestaudio[protocol=m3u8]/bestaudio[protocol=m3u8_native]/"
        # Catch-all for audio-only formats yt-dlp exposes without an `abr`
        # or a recognised ext (bestaudio* also matches HLS audio renditions).
        "bestaudio*[vcodec=none]/"
        # Sep 10 12:45 Heroku log fix: this host got an HLS-only extraction
        # (no DASH audio at all), so the chain fell through to plain `best`
        # and pulled itag 96 — 1080p muxed, 93 MB, ~10s. When only muxed
        # formats exist, take the SMALLEST one that still has audio: itag
        # 91/92/93 are a few MB and download in ~1s. Audio is re-encoded to
        # 48kHz mono for the voice chat anyway, so low video res costs
        # nothing.
        f"{_SMALL_MUXED_SELECTOR}"

        if audio_only
        # ROOT-CAUSE FIX ("/vplay pe audio aur video mismatch"): a DASH
        # video-only + audio-only pair is fed to TWO separate ffmpeg
        # processes by PyTgCalls, which start at slightly different times
        # and drift apart for the rest of the song. A single MUXED file (or
        # an explicitly merged mp4) carries both tracks with one shared
        # timebase, so they can never drift.
        # SPEED FIX ("/vplay 20 sec le raha hai"): YouTube only ships ONE
        # progressive (muxed) format nowadays — itag 18, 360p. Everything
        # above it is DASH, which forces a bestvideo+bestaudio download AND
        # an ffmpeg merge post-processor before playback can even start
        # (that merge is also what raised the "post_process ... run_all_pps"
        # crash in the error log). Asking for the muxed format FIRST means
        # the usual /vplay is a single small file with one shared timebase:
        # no merge, no drift, no 20-second wait. DASH stays as a fallback
        # only for videos that genuinely have no progressive format.
        else (
            "best[vcodec!=none][acodec!=none][height<=720]"
            "/best[vcodec!=none][acodec!=none]"
            "/bestvideo[height<=480]+bestaudio[abr<=128]"
            "/best[height<=480]/best"
        )
    )
    # Start the warm PO-token provider only when yt-dlp is genuinely needed.
    # This is intentionally best-effort: the provider args below retain the
    # script fallback when Deno or the bundled server is unavailable.
    try:
        ensure_bgutil_http_server()
    except Exception as exc:
        LOGGER.debug("lazy bgutil startup skipped: %s", exc)

    cookiefile = cookiefile_for_run()
    has_cookies = bool(cookiefile)

    # A cookies.txt file is exported from a real desktop/mobile browser
    # session — pairing it with a matching browser User-Agent (instead of the
    # generic mobile UA) keeps the request fingerprint consistent and avoids
    # extra bot-detection flags.
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        if has_cookies
        else "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36"
    )

    # ROOT-CAUSE FIX ("Requested format is not available" on EVERY video →
    # instant skip): when no PO token can be produced (bgutil/Deno download
    # failed at build time, see ensure_deno_runtime()), yt-dlp *hides* every
    # format that YouTube gated behind a PO token. The selector then matches
    # nothing and both the direct-stream path and all 8 download rungs fail
    # with "Requested format is not available".
    #   • formats=missing_pot tells yt-dlp to keep those formats and just try
    #     them — with cookies they usually download fine.
    #   • the tv / web_safari clients still advertise plain https formats that
    #     need no PO token at all, so keep them in the client list as backup.
    extractor_args: dict = {
        # The explicit client list can yield SABR-only metadata on cloud IPs.
        # web_safari is the reliable cloud escape hatch: yt-dlp documents
        # that its HLS formats do not need a GVS PO token. Keep default/iOS
        # behind it for ordinary HTTPS formats and compatibility fallback.
        # SPEED FIX (Heroku log 17:13-17:14: every mobile client answered
        # LOGIN_REQUIRED, yet yt-dlp still probed android_vr/ios on every
        # resolve — each one a wasted round-trip on the critical path, and
        # the direct resolve cost 4.2-5.7s). Once this host is known to be
        # blocked for mobile clients, ask ONLY the cookie-authenticated
        # web/TV clients that actually answer here.
        # SPEED FIX (Sep 10 17:57 log: EVERY mobile InnerTube probe answered
        # "Sign in to confirm you're not a bot", yet yt-dlp still asked
        # android_vr / default / ios on every resolve -> three dead player
        # round-trips ahead of the one client that actually answers on this
        # host, which is why stream= stayed at 3.2-3.6s). With cookies
        # present, ask only the two cookie/PO-token clients that work here.
        "player_client": (
            # ROOT-CAUSE FIX (Sep 20 2026): visionos alone only returns muxed
            # 360p (itag 18, 10-21MB) — no audio-only formats, so the direct
            # stream URL has no clean audio track (NoAudioSourceFound) and the
            # download grabs a huge video file (13-14s). Adding ios gives
            # yt-dlp access to unciphered audio-only m4a (itag 140, ~3MB) that
            # streams directly AND downloads in ~2s.
            ["ios", "visionos", "web"]
        ),
        "formats": ["missing_pot"],
        # SPEED FIX: the watch-page "configs" request and translated-subtitle
        # listing are never used by playback but cost a round-trip each.
        # NOTE: "webpage" must NOT be skipped — the web client needs the
        # initial player response from the watch page to succeed.
        "player_skip": ["configs", "initial_data"],
        "skip": ["translated_subs"],
    }

    provider_args: dict = {"youtube": extractor_args}

    bgutil_server = _bgutil_server_home()
    bgutil_script = os.path.join(bgutil_server, "src", "generate_once.ts")
    global _BGUTIL_STATUS_LOGGED
    # SPEED FIX: prefer the warm HTTP server (see ensure_bgutil_http_server())
    # — reuses a live Deno/BotGuard session + per-video token cache, so a
    # token fetch is a fast local HTTP call instead of spawning a whole new
    # Deno process from scratch on every single track. If another thread just
    # started the server, wait only a short bounded window before choosing the
    # script fallback; this avoids paying the expensive script path merely
    # because two first-use requests arrived at the same time.
    with _BGUTIL_CONFIG_LOCK:
        http_ready = _wait_for_bgutil_http(
            timeout=_env_float("BGUTIL_HTTP_READY_WAIT", 3.0)
        )
        if http_ready:
            provider_args["youtubepot-bgutilhttp"] = {
                "base_url": [f"http://127.0.0.1:{_BGUTIL_HTTP_PORT}"],
            }
            if not _BGUTIL_STATUS_LOGGED:
                LOGGER.info("✅ bgutil PO-token provider configured (HTTP, warm): 127.0.0.1:%d", _BGUTIL_HTTP_PORT)
                _BGUTIL_STATUS_LOGGED = True
        elif os.path.isfile(bgutil_script):
            provider_args["youtubepot-bgutilscript"] = {
                "server_home": [bgutil_server],
            }
            if not _BGUTIL_STATUS_LOGGED:
                LOGGER.info("✅ bgutil PO-token provider configured (script, slower): %s", bgutil_server)
                _BGUTIL_STATUS_LOGGED = True
        elif not _BGUTIL_STATUS_LOGGED:
            LOGGER.warning(
                "⚠️ bgutil PO-token provider not installed at %s — "
                "YouTube may block cloud-host requests with 'Sign in to confirm "
                "you're not a bot'. Redeploy so bin/post_compile can install it.",
                bgutil_server,
            )
            _BGUTIL_STATUS_LOGGED = True

    opts: dict = {
        "quiet": True,
        # SPEED FIX: pin yt-dlp's cache to a stable writable dir so the solved
        # player JS / nsig functions and the downloaded EJS component are
        # reused across resolves and dyno restarts instead of being fetched
        # and re-solved on the critical path of every cold /play.
        "cachedir": os.getenv("YTDLP_CACHE_DIR", "/tmp/yt-dlp-cache"),
        # LOG FIX: yt-dlp still emits the "\r ... (frag 21/37)" progress
        # bar even with quiet=True (progress goes to stderr independently).
        # On Heroku every carriage-return chunk became its own log line.
        "noprogress": True,
        "no_color": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": fmt,
        "postprocessors": [],
        # yt-dlp may add implicit FFmpegFixup* post-processors even when the
        # explicit postprocessor list is empty.  Voice-chat playback can read
        # YouTube's original WebM/M4A/MP4 files directly, so those fixups only
        # add latency and are the source of the observed
        # "Postprocessing: Conversion failed" / *.temp.mp4 rename crashes.
        "fixup": "never",
        "geo_bypass": True,
        "geo_bypass_country": "US",
        # SPEED FIX: a stuck CDN connection used to burn 15s per socket and
        # up to 8 retries per rung before the ladder even moved on — that is
        # the "kabhi kabhi _stream_track failed" case taking 20s+ first.
        "socket_timeout": _env_int("YT_SOCKET_TIMEOUT", 5),
        "retries": _env_int("YT_RETRIES", 1),
        "fragment_retries": _env_int("YT_FRAGMENT_RETRIES", 5),
        "extractor_retries": _env_int("YT_EXTRACTOR_RETRIES", 1),
        "file_access_retries": 3,
        # ROOT-CAUSE FIX (⚠️ "prefetch_next failed" →
        #   yt_dlp/downloader/external.py real_download →
        #   "<downloader> exited with code N"):
        # yt-dlp auto-selects an EXTERNAL downloader (FFmpegFD, and aria2c/
        # curl when present on the dyno) for some protocols — mainly HLS
        # (m3u8) and DASH manifests. On Heroku that external process dies
        # with a non-zero exit code (no ffmpeg protocol whitelist / killed
        # on memory / partial CDN response), and yt-dlp turns that into a
        # hard DownloadError which killed AutoPlay's prefetch. yt-dlp's own
        # native HTTP/fragment downloader has no such dependency, retries
        # per fragment, and is what every other track already used — so we
        # force it for every protocol and never let ffmpeg download.
        "external_downloader": {"default": "native"},
        "hls_prefer_native": True,
        # ⚡ 5-SECOND RULE (Sep 10 Heroku log: format_id=91, early=False,
        # stream=10.16s): on cloud IPs YouTube often exposes ONLY HLS. Ask
        # yt-dlp for an MPEG-TS output (self-describing packets, no index at
        # EOF) so the growing file is playable long before it completes.
        "hls_use_mpegts": _env_flag("HLS_USE_MPEGTS", True),
        "check_formats": False,
        # SPEED FIX ("gaana bajne me 8-9 sec"): these three extractions sit on
        # the critical path of every cold /play. Skipping playlist expansion,
        # comments and subtitle metadata removes whole HTTP round-trips from
        # each download without changing which format is picked.
        "playlist_items": "1",
        "getcomments": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        # SPEED FIX: this used to be 1, left over from a removed FIFO-pipe
        # trick that needed sequential fragment writes to keep the pipe fed
        # continuously (see download_audio()'s docstring — FIFO streaming
        # was removed entirely). Now that every track is a plain full-file
        # download with no pipe involved, sequential fragments only slow
        # the download down for no reason. 4 parallel fragments cuts
        # download time noticeably on typical DASH-fragmented audio.
        "concurrent_fragment_downloads": _env_int("YT_CONCURRENT_FRAGMENTS", 8),  # SPEED: 4->8
        # yt-dlp's YouTube extractor needs an external JS runtime to solve
        # the player challenge and to run the bgutil PO-token script.
        # Only pin a path when a real binary exists — pointing js_runtimes at
        # a missing "deno" made yt-dlp treat the runtime as configured-but-
        # broken instead of falling back to whatever is on PATH.
        "remote_components": ["ejs:github"],

        "extractor_args": provider_args,
        # Merging only kicks in for the bestvideo+bestaudio fallback above;
        # it guarantees ONE file with both tracks instead of two siblings.
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": user_agent,
            "Referer": "https://www.youtube.com/",
        },
    }
    if _has_deno():
        opts["js_runtimes"] = {"deno": {"path": _deno_path()}}
    # NOTE: curl_cffi "impersonate" intentionally removed.
    # The impersonate target varies by installed version and crashes yt-dlp
    # with "Impersonate target not available" on older curl_cffi builds.
    if cookiefile:
        opts["cookiefile"] = cookiefile

    return opts


# ─────────────────────────────────────────────────────────────────────────────
#  Search helpers
# ─────────────────────────────────────────────────────────────────────────────

async def search_youtube(query: str, limit: int = 5) -> list[dict]:
    """Search YouTube and return a list of results."""
    def _search():
        opts = {**_ydl_opts(), "default_search": f"ytsearch{limit}", "extract_flat": True}
        with _locked_ytdl(opts) as ydl:
            info = ydl.extract_info(query, download=False)
            return info.get("entries", [])

    loop = asyncio.get_running_loop()

    if ytapi_enabled():
        try:
            api_results = await asyncio.wait_for(
                loop.run_in_executor(YTDL_POOL, _ytapi_search_sync, query, limit),
                timeout=4.0,
            )
            if api_results:
                return [
                    {
                        "id": r["id"],
                        "title": r["title"],
                        "duration": r["duration"],
                        "url": r["webpage_url"],
                        "thumbnail": r["thumbnail"],
                        "uploader": r["uploader"],
                    }
                    for r in api_results
                ]
        except Exception as exc:
            LOGGER.debug("search_youtube: API v3 failed (%s) — using yt-dlp", exc)

    try:
        results = await loop.run_in_executor(YTDL_POOL, _search)
        return [
            {
                "id": e.get("id", ""),
                "title": e.get("title", "Unknown"),
                "duration": e.get("duration", 0),
                "url": f"https://www.youtube.com/watch?v={e.get('id', '')}",
                "thumbnail": e.get("thumbnail") or f"https://i.ytimg.com/vi/{e.get('id','')}/hqdefault.jpg",
                "uploader": e.get("uploader", "Unknown"),
            }
            for e in results
            if e.get("id")
        ]
    except Exception as exc:
        await send_error_log("search_youtube failed", exc, context={"video_id": query})
        return []


async def find_playable_candidate(query: str, exclude_video_id: str = "", want_video: bool = False) -> dict | None:
    """Return the first alternate search result with a working media source.

    Search metadata is not proof that an item is playable: deleted, private,
    age-gated, or region-blocked videos can still appear in API/InnerTube
    results. Validate only alternate candidates through the cheap direct
    resolver, not a full download, so normal playback latency is unchanged.
    """
    if not query or query.strip().lower().startswith(("http://", "https://")):
        return None
    try:
        candidates = await asyncio.wait_for(search_youtube(query, limit=5), timeout=4.0)
    except Exception as exc:
        LOGGER.debug("alternate search failed for %r: %s", query[:60], exc)
        return None

    for candidate in candidates:
        video_id = candidate.get("id", "")
        if not video_id or video_id == exclude_video_id:
            continue
        try:
            resolved = await asyncio.wait_for(
                resolve_stream_urls(video_id, want_video=want_video), timeout=3.5
            )
            if not resolved:
                continue
            result = dict(candidate)
            result["stream_url"] = (
                resolved.get("audio") or resolved.get("video") or
                resolved.get("url") or resolved.get("audio_url") or
                resolved.get("video_url") or ""
            )
            result["webpage_url"] = result.get("url") or f"https://www.youtube.com/watch?v={video_id}"
            LOGGER.info(
                "♻️ Replaced unavailable result %s with playable candidate %s",
                exclude_video_id, video_id,
            )
            return result
        except Exception as exc:
            LOGGER.info("Skipping unplayable alternate %s: %s", video_id, exc)
    return None


async def get_playlist_entries(url_or_query: str, limit: int = 50) -> list[dict]:
    """Extract lightweight metadata for every entry in a YouTube playlist URL.

    Used by /playlist. Like search_youtube(), this stays on the
    extract_flat metadata path only (no streamingData / format resolution)
    so it doesn't trip YouTube's cloud-IP bot-detection — each track's real
    stream is only resolved later, at play time, by download_audio().
    """
    def _extract():
        opts = {
            **_ydl_opts(),
            "extract_flat": "in_playlist",
            "noplaylist": False,
            "playlist_items": f"1:{max(1, limit)}",
            "ignoreerrors": True,
        }
        opts.pop("format", None)
        with _locked_ytdl(opts) as ydl:
            info = ydl.extract_info(url_or_query, download=False)
            if not info:
                return []
            if "entries" in info:
                return [e for e in info["entries"] if e]
            # A single-video URL was passed instead of a real playlist.
            return [info] if info.get("id") else []

    try:
        loop = asyncio.get_running_loop()
        entries = await loop.run_in_executor(YTDL_POOL, _extract)
        results = []
        for e in entries:
            vid = e.get("id", "")
            if not vid:
                continue
            results.append({
                "id": vid,
                "title": e.get("title", "Unknown"),
                "duration": int(e.get("duration") or 0),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "thumbnail": e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "uploader": e.get("uploader", "Unknown"),
            })
        return results
    except Exception as exc:
        await send_error_log("get_playlist_entries failed", exc, context={"video_id": url_or_query})
        return []


def _extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from various URL formats."""
    import re
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None



# ─────────────────────────────────────────────────────────────────────────────
#  ⚡ YouTube Data API v3 (fastest metadata path)
#
#  SPEED FIX ("gana bajne me bohot time lagta hai"): every metadata lookup used
#  to go through yt-dlp (player-JS challenge + PO-token + cookies, 3-8s on a
#  Heroku dyno) racing InnerTube (1-3s, and frequently answering `404` for the
#  hardcoded web keys — visible all over the deploy logs). The official Data
#  API v3 is a single plain HTTPS GET to googleapis.com: no bot detection, no
#  JS runtime, no cookies, and it answers in ~200-400ms. When YOUTUBE_API_KEY
#  is set it joins (and normally wins) the race, so /play resolves a song in
#  well under a second and only the audio fetch itself remains.
# ─────────────────────────────────────────────────────────────────────────────

_YTAPI_BASE = "https://www.googleapis.com/youtube/v3"
_ytapi_dead_keys: set = set()


def _ytapi_keys() -> list:
    """All configured Data API v3 keys that have not hit their quota yet."""
    raw = (getattr(Config, "YOUTUBE_API_KEY", "") or "").replace(" ", "")
    keys = [k for k in raw.split(",") if k]
    return [k for k in keys if k not in _ytapi_dead_keys]


def ytapi_enabled() -> bool:
    return bool(_ytapi_keys())


def _ytapi_get(path: str, params: dict) -> "dict | None":
    """GET one Data API v3 endpoint, rotating keys on quota errors."""
    client = get_http_sync_client()
    for key in _ytapi_keys():
        try:
            resp = client.get(
                f"{_YTAPI_BASE}/{path}",
                params={**params, "key": key},
                timeout=6.0,
            )
        except Exception as exc:
            LOGGER.debug("ytapi %s network error: %s", path, exc)
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return None
        if resp.status_code in (400, 403):
            body = resp.text[:200]
            # quotaExceeded / keyInvalid → retire this key for the process.
            if "quota" in body.lower() or "keyInvalid" in body or "API key not valid" in body:
                _ytapi_dead_keys.add(key)
                LOGGER.warning("YOUTUBE_API_KEY retired (quota/invalid): %s", body)
                continue
        LOGGER.debug("ytapi %s -> HTTP %s", path, resp.status_code)
    return None


def extract_video_id(url: str) -> str | None:
    """Public wrapper used by the playback command for early URL warming."""
    return _extract_video_id(url or "")


def _iso8601_to_seconds(value: str) -> int:
    """PT4M13S → 253. Returns 0 when the duration is missing/live."""
    import re as _re
    m = _re.fullmatch(
        r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or ""
    )
    if not m:
        return 0
    d, h, mi, s = (int(g or 0) for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _ytapi_item_to_info(item: dict) -> "dict | None":
    """Normalize one videos.list / search.list item into yt-dlp-ish shape."""
    if not item:
        return None
    vid = item.get("id")
    if isinstance(vid, dict):
        vid = vid.get("videoId")
    if not is_valid_video_id(vid):
        return None
    snippet = item.get("snippet") or {}
    thumbs = snippet.get("thumbnails") or {}
    thumb = ""
    for size in ("maxres", "standard", "high", "medium", "default"):
        if thumbs.get(size, {}).get("url"):
            thumb = thumbs[size]["url"]
            break
    duration = _iso8601_to_seconds(
        ((item.get("contentDetails") or {}).get("duration")) or ""
    )
    return {
        "id": vid,
        "title": snippet.get("title") or "Unknown",
        "duration": duration,
        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
        "thumbnail": thumb or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "uploader": snippet.get("channelTitle") or "Unknown",
    }


def _ytapi_details_sync(video_id: str) -> "dict | None":
    """videos.list for a known id — one request, full details."""
    if not (ytapi_enabled() and is_valid_video_id(video_id)):
        return None
    data = _ytapi_get(
        "videos", {"part": "snippet,contentDetails", "id": video_id, "maxResults": 1}
    )
    items = (data or {}).get("items") or []
    return _ytapi_item_to_info(items[0]) if items else None


def _ytapi_search_sync(query: str, limit: int = 1) -> "dict | list | None":
    """search.list + videos.list (for real durations).

    Returns a single info dict when `limit == 1`, else a list of them.
    """
    if not ytapi_enabled():
        return None if limit == 1 else []
    data = _ytapi_get(
        "search",
        {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max(1, min(limit, 25)),
        },
    )
    items = [i for i in ((data or {}).get("items") or []) if i]
    ids = []
    for it in items:
        vid = (it.get("id") or {}).get("videoId")
        if is_valid_video_id(vid):
            ids.append(vid)
    if not ids:
        return None if limit == 1 else []

    # One extra call gives durations for every hit at once (1 quota unit).
    details = _ytapi_get(
        "videos", {"part": "snippet,contentDetails", "id": ",".join(ids)}
    )
    by_id = {}
    for it in ((details or {}).get("items") or []):
        info = _ytapi_item_to_info(it)
        if info:
            by_id[info["id"]] = info

    results = []
    for it in items:
        vid = (it.get("id") or {}).get("videoId")
        info = by_id.get(vid) or _ytapi_item_to_info(it)
        if info:
            results.append(info)
    if limit == 1:
        return results[0] if results else None
    return results

# ─────────────────────────────────────────────────────────────────────────────
#  YouTube InnerTube API fallback search
#  Direct POST to YouTube's internal API (same endpoint yt-dlp uses).
#  Works from Heroku because it hits youtube.com directly (not a proxy).
#  Uses the ANDROID client which bypasses bot-detection without cookies.
# ─────────────────────────────────────────────────────────────────────────────

def _innertube_search_sync(query: str) -> dict | None:
    """Search YouTube via InnerTube API — Heroku-safe, no yt-dlp needed.

    BUG FIX ("autoplay nahi ho raha / search nahi mil raha"): the old
    ANDROID client version 20.10.35 is deprecated — YouTube still returns
    HTTP 200 but the response no longer contains any videoRenderer nodes,
    so every search silently returns zero results. Now tries multiple
    client contexts (WEB, ANDROID, IOS) with current versions, returning
    the first that yields actual video results.
    """
    import json

    _SEARCH_URL = "https://www.youtube.com/youtubei/v1/search"

    # MODERNISED: dropped the legacy `?key=AIza...` query param and the plain
    # ANDROID client. Verified live: ANDROID answers 404 for /search (with or
    # without a key) — that is the 404 seen in the Heroku log — while keyless
    # WEB/IOS return full results. One dead client less per search.
    _CLIENT_CONTEXTS = [
        ("WEB", "2.20260801.00.00",
         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        ("IOS", "20.10.4",
         "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X)"),
    ]

    for client_name, client_ver, ua in _CLIENT_CONTEXTS:
        url = f"{_SEARCH_URL}?prettyPrint=false"
        client_ctx = {"clientName": client_name, "clientVersion": client_ver,
                      "hl": "en", "gl": "US", "utcOffsetMinutes": 0}
        if client_name == "ANDROID":
            client_ctx["androidSdkVersion"] = 30
        elif client_name == "IOS":
            client_ctx["deviceMake"] = "Apple"
            client_ctx["deviceModel"] = "iPhone16,2"
        payload = json.dumps({
            "context": {"client": client_ctx},
            "query": query,
            "params": "EgIQAQ==",
        }).encode("utf-8")
        try:
            client = get_http_sync_client()
            resp = client.post(
                url, content=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": ua,
                         "Accept-Language": "en-US,en;q=0.9"},
                timeout=12.0,
            )
            if resp.status_code != 200:
                LOGGER.warning(
                    "InnerTube %s search HTTP %s for %s", client_name,
                    resp.status_code, query[:40],
                )
                continue
            data = json.loads(resp.text)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("InnerTube %s search error: %s", client_name, exc)
            continue
        if data:
            break

    if not data:
        LOGGER.warning("InnerTube search: all client contexts failed for: %s", query[:50])
        return None

    # Parse InnerTube response — walk the renderer tree
    def _parse_video_renderer(vr: dict) -> "dict | None":
        vid = vr.get("videoId", "")
        if not vid:
            return None
        title = (vr.get("title", {})
                   .get("runs", [{}])[0]
                   .get("text", "") or query)
        duration_text = (
            vr.get("lengthText", {}).get("simpleText", "") or
            vr.get("lengthText", {}).get("runs", [{}])[0].get("text", "")
        )
        duration = 0
        if duration_text:
            parts = [int(p) for p in duration_text.split(":") if p.isdigit()]
            if len(parts) == 2:
                duration = parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                duration = parts[0] * 3600 + parts[1] * 60 + parts[2]
        thumbs = (vr.get("thumbnail", {}).get("thumbnails") or [])
        thumbnail = thumbs[-1].get("url", "") if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        uploader = (
            vr.get("ownerText", {}).get("runs", [{}])[0].get("text", "") or
            vr.get("longBylineText", {}).get("runs", [{}])[0].get("text", "") or
            "Unknown"
        )
        return {
            "id": vid,
            "webpage_url": f"https://www.youtube.com/watch?v={vid}",
            "title": title,
            "duration": duration,
            "thumbnail": thumbnail,
            "uploader": uploader,
        }

    # Primary path: structured sectionListRenderer walk
    try:
        contents = (
            data.get("contents", {})
                .get("sectionListRenderer", {})
                .get("contents", [])
        )
        for section in contents:
            items = (
                section.get("itemSectionRenderer", {})
                       .get("contents", [])
            )
            for item in items:
                vr = item.get("videoRenderer")
                if not vr:
                    continue
                result = _parse_video_renderer(vr)
                if result:
                    LOGGER.info("✅ InnerTube search OK: %s", result["title"][:50])
                    return result
    except Exception as exc:
        LOGGER.warning("InnerTube response parse error: %s", exc)

    # Fallback: recursive tree-walk for any videoRenderer anywhere in the
    # response (YouTube frequently changes the nesting structure between
    # client versions — a tree-walk finds the first video result regardless).
    try:
        def _walk(node, out):
            if isinstance(node, dict):
                vr = node.get("videoRenderer")
                if vr and vr.get("videoId"):
                    out.append(vr)
                for v in node.values():
                    _walk(v, out)
            elif isinstance(node, list):
                for item in node:
                    _walk(item, out)

        renderers: list = []
        _walk(data, renderers)
        for vr in renderers:
            result = _parse_video_renderer(vr)
            if result:
                LOGGER.info("✅ InnerTube search OK (tree-walk): %s", result["title"][:50])
                return result
    except Exception as exc:
        LOGGER.warning("InnerTube tree-walk parse error: %s", exc)

    LOGGER.warning("❌ InnerTube search: no video results for: %s", query[:50])
    return None


# Keep Invidious as a secondary backup behind InnerTube. Public instances are
# opportunistic only; they must never turn a bounded resolver into a serial
# 10-host timeout chain.
_INVIDIOUS_INSTANCES = [
    "https://invidious.f5.si",
    "https://invidious.privacydev.net",
    "https://invidious.nerdvpn.de",
    "https://invidious.fdn.fr",
    "https://invidious.io.lol",
    "https://invidious.einfachzocken.eu",
    "https://invidious.privacyredirect.com",
    "https://invidious.jing.rocks",
    "https://invidious.asir.dev",
    "https://invidious.drgns.space",
]
_INVIDIOUS_INSTANCES = [
    item.strip().rstrip("/")
    for item in (os.getenv("INVIDIOUS_INSTANCES") or "").split(",")
    if item.strip().startswith("https://")
] or _INVIDIOUS_INSTANCES
try:
    _INVIDIOUS_MAX_INSTANCES = max(
        1, min(3, int(os.getenv("INVIDIOUS_MAX_INSTANCES", "3")))
    )
except (TypeError, ValueError):
    _INVIDIOUS_MAX_INSTANCES = 3


def _invidious_search_sync(query: str) -> dict | None:
    """Secondary fallback: Invidious public instances."""
    import json, urllib.parse
    encoded = urllib.parse.quote_plus(query)
    for inst in _INVIDIOUS_INSTANCES:
        try:
            url = f"{inst}/api/v1/search?q={encoded}&type=video"
            client = get_http_sync_client()
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; MelodyBot/1.0)"}, timeout=5.0)
            if resp.status_code != 200:
                continue
            items = json.loads(resp.text)
            res = next((i for i in items if i.get("type") == "video" and i.get("videoId")), None)
            if not res:
                continue
            vid = res["videoId"]
            thumbs = res.get("videoThumbnails") or []
            thumb = next((t.get("url", "") for t in thumbs if t.get("quality") in {"medium", "high"}),
                         thumbs[0].get("url", "") if thumbs else "")
            LOGGER.info("✅ Invidious search OK (%s): %s", inst, res.get("title", "")[:50])
            return {
                "id": vid, "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                "title": res.get("title") or query, "duration": int(res.get("lengthSeconds") or 0),
                "thumbnail": thumb, "uploader": res.get("author") or "",
            }
        except Exception as exc:
            LOGGER.debug("Invidious (%s) failed: %s", inst, exc)
    LOGGER.warning("❌ All Invidious instances failed for: %s", query[:50])
    return None


def is_valid_video_id(vid: "str | None") -> bool:
    """True only for a real 11-char YouTube video ID."""
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or ""))


def _normalize_info(info: "dict | None") -> "dict | None":
    """Normalize raw yt-dlp / InnerTube / Invidious info into the uniform
    dict shape get_video_info() returns. Returns None if info is unusable.

    ROOT-CAUSE FIX ("ValueError: download_audio: invalid YouTube video_id
    'machayeage 4 kr$na'"): when yt-dlp runs a `ytsearch1:<query>` lookup
    with extract_flat and the search returns no usable entries, yt-dlp hands
    back the *search playlist* object itself — whose `id` is the raw search
    text ("machayeage 4 kr$na") and whose `_type` is "playlist". That got
    normalized into a Track, so `track.video_id` became the user's query and
    download_audio() blew up on every attempt. Reject anything that isn't a
    real 11-char video ID (or a Telegram synthetic `tg...` id) right here, so
    the caller falls through to the next source instead of building a
    poisoned Track.
    """
    if not info or not info.get("id"):
        return None
    if info.get("_type") in ("playlist", "multi_video", "url_transparent"):
        return None
    vid = str(info.get("id", ""))
    import re
    if not (is_valid_video_id(vid) or re.fullmatch(r"tg[A-Za-z0-9_]+", vid)):
        LOGGER.warning("Discarding metadata with non-video id %r", vid[:60])
        return None
    return {
        "id": vid,
        "title": info.get("title", "Unknown"),
        "duration": int(info.get("duration") or 0),
        "url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
        "stream_url": "",
        "thumbnail": info.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "uploader": info.get("uploader") or info.get("channel") or "Unknown",
    }


def _youtube_oembed_sync(video_id: str) -> dict | None:
    """Fetch title/author for a known ID without yt-dlp or InnerTube.

    oEmbed is metadata-only, but it is a useful emergency fast path when
    YouTube blocks player/search APIs from a cloud IP. Stream URL resolution
    remains separately bounded by the normal yt-dlp/bgutil path.
    """
    import json
    from urllib.parse import quote

    url = (
        "https://www.youtube.com/oembed?format=json&url="
        + quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
    )
    try:
        client = get_http_sync_client()
        response = client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=2.0)
        if response.status_code != 200:
            return None
        data = json.loads(response.text)
        return {
            "id": video_id,
            "title": data.get("title") or "Unknown",
            "uploader": data.get("author_name") or "Unknown",
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "duration": 0,
        }
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("YouTube oEmbed failed for %s: %s", video_id, type(exc).__name__)
        return None


def _innertube_next_sync(video_id: str) -> "dict | None":
    """Fetch single-video metadata via InnerTube /next endpoint.

    BUG FIX: old ANDROID client 20.10.35 deprecated — YouTube returns 200
    but empty results. Now tries multiple client contexts (WEB, ANDROID,
    IOS) with current versions.
    """
    import json

    _ANDROID_KEY = "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w"
    _WEB_KEY = "AIzaSyAO_FJ2SlqU8Q4STEhlGCjw-FfWAH9IrJg"
    _NEXT_URL = "https://www.youtube.com/youtubei/v1/next"

    _CLIENT_CONTEXTS = [
        ("WEB", _WEB_KEY, "2.20240807.01.00",
         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        ("ANDROID", _ANDROID_KEY, "19.29.37",
         "com.google.android.youtube/19.29.37 (Linux; U; Android 11) gzip"),
        ("IOS", _ANDROID_KEY, "19.29.1",
         "com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)"),
    ]

    data = None
    for client_name, api_key, client_ver, ua in _CLIENT_CONTEXTS:
        url = f"{_NEXT_URL}?key={api_key}&prettyPrint=false"
        client_ctx = {"clientName": client_name, "clientVersion": client_ver,
                      "hl": "en", "gl": "US", "utcOffsetMinutes": 0}
        if client_name == "ANDROID":
            client_ctx["androidSdkVersion"] = 30
        elif client_name == "IOS":
            client_ctx["deviceMake"] = "Apple"
            client_ctx["deviceModel"] = "iPhone16,2"
        payload = json.dumps({"context": {"client": client_ctx}, "videoId": video_id}).encode("utf-8")
        try:
            client = get_http_sync_client()
            resp = client.post(
                url, content=payload,
                headers={"Content-Type": "application/json", "User-Agent": ua},
                timeout=6.0,
            )
            if resp.status_code != 200:
                LOGGER.warning(
                    "InnerTube %s next HTTP %s for %s", client_name,
                    resp.status_code, video_id,
                )
                continue
            data = json.loads(resp.text)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("InnerTube %s next error: %s", client_name, exc)
            continue
        if data:
            break

    if not data:
        return None

    # Walk the response tree for the first videoRenderer that matches video_id
    def _walk(node, out):
        if isinstance(node, dict):
            renderer = node.get("compactVideoRenderer") or node.get("videoRenderer")
            if renderer and renderer.get("videoId"):
                out.append(renderer)
            for v in node.values():
                _walk(v, out)
        elif isinstance(node, list):
            for item in node:
                _walk(item, out)

    renderers: list = []
    _walk(data, renderers)
    for r in renderers:
        if r.get("videoId") == video_id:
            title = (
                r.get("title", {}).get("simpleText")
                or (r.get("title", {}).get("runs", [{}])[0].get("text"))
                or "Unknown"
            )
            length_text = (
                r.get("lengthText", {}).get("simpleText", "")
                or (r.get("lengthText", {}).get("runs", [{}])[0].get("text", "") if r.get("lengthText") else "")
            )
            duration = 0
            if length_text:
                parts = [int(p) for p in length_text.split(":") if p.isdigit()]
                if len(parts) == 2:
                    duration = parts[0] * 60 + parts[1]
                elif len(parts) == 3:
                    duration = parts[0] * 3600 + parts[1] * 60 + parts[2]
            thumbs = (r.get("thumbnail", {}).get("thumbnails") or [])
            thumb = thumbs[-1].get("url", "") if thumbs else f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            return {
                "id": video_id,
                "title": title,
                "duration": duration,
                "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
                "thumbnail": thumb,
                "uploader": "Unknown",
            }
    # Fallback: construct minimal info from the video_id itself
    return {
        "id": video_id,
        "title": "Unknown",
        "duration": 0,
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "uploader": "Unknown",
    }


async def get_video_info(url_or_query: str) -> dict | None:
    """Resolve a song, auto-correcting the user's spelling when needed.

    ROOT FIX ("users se spelling mistake hoti hai to result invalid aata
    hai"): a single failed `ytsearch1:` lookup used to end in
    "all sources exhausted". Now a failed lookup is retried with cleaned +
    YouTube-autocomplete-corrected variants of the same query before anything
    is reported (see melody/core/query_fix.py).

    TYPO FIX (requirement: "corrected query tried up front, not only after a
    failure"): the raw query is given a short window to resolve on its own
    (the fast path above almost always wins it). If that window passes
    without an answer, the spelling-corrected variants are launched
    IMMEDIATELY and race the still-running raw lookup — the user gets
    whichever answers first instead of waiting for the raw attempt to fully
    exhaust every source before correction is even considered.
    """
    cache_key = url_or_query.strip().lower()
    # A bare valid ID already identifies the exact media. Metadata enrichment
    # is useful for titles, but it must never block playback when InnerTube
    # returns a response without videoDetails (the direct stream resolver can
    # still return playable URLs in that situation).
    if is_valid_video_id(url_or_query.strip()):
        cached = _meta_cache_get(f"id:{url_or_query.strip()}")
        if cached:
            _meta_cache_put(cache_key, cached)
            return cached
        minimal = _normalize_info({
            "id": url_or_query.strip(),
            "title": "YouTube track",
            "duration": 0,
            "webpage_url": f"https://www.youtube.com/watch?v={url_or_query.strip()}",
            "thumbnail": f"https://i.ytimg.com/vi/{url_or_query.strip()}/hqdefault.jpg",
            "uploader": "YouTube",
        })
        if minimal:
            _meta_cache_put(cache_key, minimal)
            _meta_cache_put(f"id:{url_or_query.strip()}", minimal)
            return minimal

    raw_task = asyncio.ensure_future(_get_video_info_once(url_or_query))

    try:
        result = await asyncio.wait_for(asyncio.shield(raw_task), timeout=_FAST_TIMEOUT)
        if result:
            return result
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass

    from melody.core.query_fix import is_url, query_variants

    variants: list[str] = []
    if not is_url(url_or_query):
        try:
            variants = await query_variants(url_or_query)
        except Exception:
            variants = []

    if variants:
        LOGGER.info("🪄 raw query slow/empty — racing corrected variants: %r", variants)
    correction_tasks = [
        asyncio.ensure_future(_get_video_info_once(v, _quiet=True)) for v in variants
    ]

    result = None
    pending = {raw_task, *correction_tasks}
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED, timeout=8.0,
            )
            if not done:
                break
            for task in done:
                try:
                    r = task.result()
                except Exception:
                    r = None
                if r:
                    result = r
            if result:
                break
    except Exception:
        pass

    for p in pending:
        p.cancel()

    if result:
        _meta_cache_put(cache_key, result)
        return result

    # A search with no result is normal user/content input, not a bot crash.
    # Do not send a fake traceback ("NoneType: None") or label the query as a
    # video ID; play.py already shows useful suggestions to the user.
    LOGGER.info("No playable search result for %r (variants=%r)", url_or_query[:80], variants)
    return None


async def _get_video_info_once(url_or_query: str, _quiet: bool = False) -> dict | None:
    """Get metadata for a single video (or first search result).

    SPEED FIX ("bohot tym baad play hota hai"):
    Previously the search was strictly sequential — yt-dlp first (6s timeout),
    then InnerTube (12s), then Invidious (up to 50s). On Heroku without
    cookies, yt-dlp ALWAYS hits the "Sign in to confirm you're not a bot"
    wall and wastes its entire timeout before InnerTube even starts.

    Now InnerTube and yt-dlp race IN PARALLEL — whichever returns a valid
    result first wins. InnerTube is a direct HTTP POST to YouTube's own
    Android API that bypasses bot-detection and typically responds in
    1-2 seconds, so /play search latency drops from ~8s to ~2s.

    For direct YouTube URLs with a known video_id, InnerTube's /next
    endpoint is used (even faster — no search parsing needed).
    Invidious stays as a last-resort fallback only if both parallel
    lookups fail.
    METADATA CACHE: results are cached in-memory for _META_CACHE_TTL seconds
    so a recently-played song resolves near-instantly on repeat without
    re-hitting YouTube/InnerTube at all.
    """
    cache_key = url_or_query.strip().lower()
    cached = _meta_cache_get(cache_key)
    if cached:
        LOGGER.debug("⚡ get_video_info cache HIT for %s", cache_key[:50])
        return cached

    import re
    is_url = bool(re.match(r"https?://", url_or_query))
    vid_id = (
        _extract_video_id(url_or_query)
        if is_url
        else (url_or_query.strip() if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_query.strip()) else None)
    )

    # Stable video-id cache hit (any query form that maps to the same video).
    if vid_id:
        cached = _meta_cache_get(f"id:{vid_id}")
        if cached:
            LOGGER.debug("⚡ get_video_info cache HIT by video_id %s", vid_id)
            _meta_cache_put(cache_key, cached)
            return cached

    # ── Build the search query for yt-dlp ──────────────────────────────────
    if is_url:
        query = f"ytsearch1:{url_or_query}" if not vid_id else f"ytsearch1:https://www.youtube.com/watch?v={vid_id}"
    else:
        query = f"ytsearch1:{url_or_query}"

    def _ytdlp_info():
        opts = {
            **_ydl_opts(),
            "extract_flat": "in_playlist",
            "default_search": "ytsearch1",
            "noplaylist": False,
            "socket_timeout": 5,
        }
        with _locked_ytdl(opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if info and "entries" in info and info["entries"]:
                return info["entries"][0]
            return info

    # ── Choose the InnerTube call based on input type ──────────────────────
    if vid_id:
        # BUG FIX ("YouTube link se gaana Unknown title / 00:00 duration ke
        # saath bajta tha"): /next returns the *related* video renderers, not
        # the requested video's own metadata, so the tree-walk almost never
        # matched and the function fell through to its synthetic
        # {"title": "Unknown", "duration": 0} stub. That stub is a *valid*
        # dict, so the fast race declared victory and cancelled the yt-dlp
        # lookup that would have produced the real title. /player carries the
        # real videoDetails (title, author, lengthSeconds), so ask it first
        # and only fall back to /next.
        def innertube_fn():
            info = _innertube_player_sync(vid_id)
            if info and info.get("title") and info.get("title") != "Unknown":
                return info
            return _innertube_next_sync(vid_id) or info
    else:
        innertube_fn = lambda: _innertube_search_sync(url_or_query)

    loop = asyncio.get_running_loop()

    # ── ⚡⚡ TRUE PARALLEL RACE — YouTube Data API v3 + InnerTube ─────────────
    # SPEED FIX ("search 5-10 sec leta hai even though API keys + cookies
    # configured"): the old code AWAITED the Data API v3 call to completion
    # (up to 6s) before even starting InnerTube/yt-dlp, so a slow/rate-limited
    # API key added its own timeout on top of everything else instead of
    # racing it. Now every fast source starts at the exact same instant and
    # whichever answers first wins; the slower ones are cancelled. yt-dlp is
    # started in the background too (so it isn't idle if the fast sources
    # both fail) but is never blocked on inside the fast window — it is only
    # actually waited on as the last-resort fallback below.
    fast_jobs: dict = {}
    if ytapi_enabled():
        api_fn = (
            (lambda: _ytapi_details_sync(vid_id)) if vid_id
            else (lambda: _ytapi_search_sync(url_or_query, 1))
        )
        fast_jobs["ytapi"] = asyncio.ensure_future(loop.run_in_executor(YTDL_POOL, api_fn))
    if vid_id:
        # oEmbed is tiny and often remains available when player/search APIs
        # are blocked. It supplies enough metadata to keep known-ID requests
        # on the bounded direct-stream path; format resolution is separate.
        fast_jobs["oembed"] = asyncio.ensure_future(
            loop.run_in_executor(YTDL_POOL, _youtube_oembed_sync, vid_id)
        )
    fast_jobs["innertube"] = asyncio.ensure_future(loop.run_in_executor(YTDL_POOL, innertube_fn))

    # Keep yt-dlp completely lazy. It loads player JS, cookie jars, format
    # graphs, and sometimes Deno/PO-token state. Starting it before API v3 or
    # InnerTube has failed needlessly consumes CPU/RSS and the executor Future
    # cannot cancel a thread that has already entered yt-dlp.
    ytdlp_future = None

    result = None
    pending = set(fast_jobs.values())
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED, timeout=_FAST_TIMEOUT,
            )
            if not done:
                break  # fast window exhausted — fall through to yt-dlp below
            for task in done:
                try:
                    info = task.result()
                    normalized = _normalize_info(info if isinstance(info, dict) else None)
                    if normalized:
                        if result is None or (result.get("title") == "Unknown" and normalized.get("title") != "Unknown"):
                            result = normalized
                except Exception:
                    pass
            if result:
                break
    except Exception:
        pass

    for p in pending:
        p.cancel()

    if result:
        # yt-dlp is intentionally lazy and may not have been started when the
        # API/InnerTube fast path wins. Calling .cancel() on None used to raise
        # after a valid InnerTube result, which made get_video_info() report a
        # misleading "No playable search result" to the user.
        if ytdlp_future is not None:
            ytdlp_future.cancel()
        LOGGER.info("⚡ Fast-path (API/InnerTube race) resolved %s", result["id"])
        _meta_cache_put(cache_key, result)
        return result

    # ── Fallback: start yt-dlp only after the fast window has failed ────────
    # This keeps the common API-v3/InnerTube path light and avoids a second
    # extractor working set during the first playback request.
    ytdlp_future = asyncio.ensure_future(loop.run_in_executor(YTDL_POOL, _ytdlp_info))
    pending = {ytdlp_future}
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED, timeout=8.0,
            )
            if not done:
                break
            for task in done:
                try:
                    info = task.result()
                    normalized = _normalize_info(info)
                    if normalized:
                        result = normalized
                except Exception:
                    pass
            if result:
                break
    except Exception:
        pass

    for p in pending:
        p.cancel()

    if result:
        _meta_cache_put(cache_key, result)
        return result

    # ── Last-resort fallback: Invidious ────────────────────────────────────
    LOGGER.info("API v3 + InnerTube + yt-dlp all failed — trying Invidious | %s", url_or_query[:50])
    try:
        info = await loop.run_in_executor(YTDL_POOL, _invidious_search_sync, url_or_query)
        result = _normalize_info(info)
        if result:
            _meta_cache_put(cache_key, result)
            return result
    except Exception:
        pass

    if not _quiet:
        LOGGER.info("no result for %r — will retry with corrected spelling", query[:60])
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Full file download (with early growing-file handoff — see download_audio())
# ─────────────────────────────────────────────────────────────────────────────

def _cache_tag(audio_only: bool) -> str:
    """Suffix distinguishing an audio-only download from a video download of
    the same YouTube video_id — see download_audio() for why this matters."""
    return "a" if audio_only else "v"


# ROOT-CAUSE FIX ("bot VC me aata hai par gana nahi bajta" / autoplay chain
# every ~13s): the early handoff used to fire after only 8 KB had landed on
# disk. ffmpeg does NOT wait at the end of a regular file — it reaches the
# current write position, sees EOF and ends the stream. With an 8 KB buffer
# (~0.5s of audio) the reader caught up with the writer instantly, PyTgCalls
# fired StreamEnded, and the bot silently "finished" the song it never
# played — which is exactly what the logs show (AutoPlay firing again and
# again a few seconds apart, with no audible music in the VC).
#
# The buffer must therefore be large enough that the *download* stays far
# ahead of 1x realtime playback for the whole track. At <=128 kbps (~16 KB/s
# of audio) a 768 KB head start is ~48 seconds of playback buffer, while
# still landing on disk in well under a second on any normal connection —
# playback is still effectively instant, but it no longer dies at EOF.
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


# SPEED FIX ("gaana bajne me 20 sec lagta hai"): waiting for the COMPLETE
# file put the whole download on the critical path — on a Heroku dyno a
# 4-5 minute track is 4-6 MB and, together with the metadata resolve, that
# is exactly the 15-20s users reported. Playback now starts from a
# still-growing file again, but with a bounded prefix buffer rather than
# waiting for most of the file. The old ratio/60%-of-small-file rule made a
# direct-stream failure fall back to an almost-complete download, which is the
# 13-17s startup delay visible in production. The prefix is only enabled for
# probe-safe audio containers and call.py retains the premature-EOF recovery
# safety net if a slow source catches the reader up.
# SPEED FIX ("5 sec ke andar gana bajna chahiye"): 1.2 MB is ~75 seconds of
# 128 kbps audio — on a Heroku dyno that alone was 5-10 s of pure waiting
# before playback could start. 512 KB is still ~30 s of playback buffer (the
# writer stays far ahead of the 1x-realtime reader, so no premature EOF) but
# lands on disk in well under a second.
_EARLY_HANDOFF_BYTES = _env_int("EARLY_HANDOFF_BYTES", 16_000)  # SPEED: WebM/Opus header + audio fits in a small prefix
# Minimum share of the total file that must be on disk before handing off.
_EARLY_HANDOFF_RATIO = _env_float("EARLY_HANDOFF_RATIO", 0.001)
# BUG FIX ("3 ghante ki movie download hone tak wait karta hai"): the ratio
# above is only sane for small files. A percentage of a multi-GB movie is
# itself gigabytes — waiting for 35% of a 3 GB file means buffering ~1 GB
# before a single frame plays, which is functionally the same as waiting for
# the full download. Past this size, ignore the ratio entirely and hand off
# once a small FIXED prefix is on disk; a real yt-dlp fragment download runs
# far faster than 1x realtime playback, so that prefix keeps growing well
# ahead of the reader for the rest of a multi-hour file.
_EARLY_HANDOFF_LARGE_FILE_BYTES = _env_int("EARLY_HANDOFF_LARGE_FILE_BYTES", 10_000_000)
_EARLY_HANDOFF_LARGE_FILE_PREFIX = _env_int("EARLY_HANDOFF_LARGE_FILE_PREFIX", 32_000)
# ROOT-CAUSE FIX from the Aug 25 Heroku log:
#   ffprobe check_stream failed (NoAudioSourceFound: No audio source found on
#   "/tmp/melody_<id>_a.mp4.part")
#   play timings ... stream=21.00s
# Video and MP4/M4A handoff stays OFF because PyTgCalls cannot probe those
# containers reliably until the final atomic rename. Audio-only WebM/Opus/MP3
# can opt into the validated prefix path below; the shared future and download
# gate still wait for the complete file before any cache/persistence operation.
# SPEED FIX: cloud hosts are exactly where the direct CDN route is blocked
# and every play falls back to a download, so early handoff matters MOST
# there. Enable it by default everywhere (EARLY_HANDOFF=0 to opt out).
_EARLY_HANDOFF_ENABLED = _env_flag("EARLY_HANDOFF", True)
# Audio-only WebM/Opus files carry their decode headers at the beginning and
# can be consumed safely while yt-dlp keeps appending ordered clusters. Enable
# this path on cloud hosts by default; video/MP4/M4A remain completion-only.
_EARLY_AUDIO_HANDOFF_ENABLED = _env_flag("EARLY_AUDIO_HANDOFF", True)
_EARLY_AUDIO_STREAMABLE_EXTS = {
    "webm", "ogg", "oga", "opus", "mp3", "flac", "wav", "mka", "m4a",
    # MPEG-TS family (see hls_use_mpegts): prefix is immediately playable.
    "ts", "mpegts", "m2ts", "aac",
}
# Extensions that are only early-handoff-safe when the moov atom sits at the
# HEAD of the file (fragmented MP4). YouTube's audio-only m4a (itag 139/140/141)
# is always fMP4 with the init segment first, so its prefix is playable; a
# progressive mp4 keeps moov at EOF and stays excluded (mp4 is not in the set).
_EARLY_AUDIO_MOOV_CHECK_EXTS = {"m4a"}


def _early_handoff_allowed(audio_only: bool) -> bool:
    return bool(_EARLY_HANDOFF_ENABLED or (audio_only and _EARLY_AUDIO_HANDOFF_ENABLED))


def _m4a_moov_at_head(path: str) -> bool:
    """True when an m4a prefix already contains the moov atom (fragmented MP4).

    YouTube's audio-only m4a is fMP4: the init segment (ftyp+moov) is written
    first, so a growing prefix is playable. A moov found only near EOF would
    make the prefix unplayable, so sniff the head before handing it off.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(262144)
    except OSError:
        return False
    return b"moov" in head


def _mpegts_at_head(path: str) -> bool:
    """True when the file really is MPEG-TS, whatever its extension claims.

    ROOT CAUSE of "early=False" on every HLS-only play: yt-dlp names an HLS
    download ``.mp4`` even when its native downloader just concatenates MPEG-TS
    segments, so the extension check rejected a growing file ffmpeg could have
    opened right away and playback waited 8-9s for the full download.
    MPEG-TS is unambiguous: 0x47 sync byte every 188 bytes.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(188 * 4)
    except OSError:
        return False
    if len(head) < 188 * 2:
        return False
    return all(head[i] == 0x47 for i in range(0, len(head) - 187, 188))


def _early_audio_file_is_safe(path: str) -> bool:
    """Path-based container check plus a moov-at-head sniff for m4a."""
    if not _early_audio_path_is_safe(path):
        # The extension lies on HLS extractions; trust the bytes instead.
        return _mpegts_at_head(path)
    name = os.path.basename(path or "").lower()
    name = re.sub(r"\.part-frag\d+$", "", name)
    name = re.sub(r"\.frag\d+$", "", name)
    for suffix in (".part", ".ytdl", ".temp"):
        while name.endswith(suffix):
            name = name[: -len(suffix)]
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in _EARLY_AUDIO_MOOV_CHECK_EXTS:
        return _m4a_moov_at_head(path)
    return True


def _early_handoff_ready(downloaded: int, total: int = 0) -> bool:
    """Return whether a growing audio file has enough safe prefix to play."""
    downloaded = max(0, int(downloaded or 0))
    total = max(0, int(total or 0))
    if total >= _EARLY_HANDOFF_LARGE_FILE_BYTES:
        required = _EARLY_HANDOFF_LARGE_FILE_PREFIX
    elif total:
        # Do not require 60% of a normal song: that turns direct-stream
        # failures back into full-download playback. Keep a small safety floor
        # for tiny files while preserving the bounded handoff latency.
        required = min(
            _EARLY_HANDOFF_BYTES,
            max(32_000, int(total * _EARLY_HANDOFF_RATIO)),
        )
    else:
        required = _EARLY_HANDOFF_BYTES
    return downloaded >= required


def _early_audio_path_is_safe(path: str) -> bool:
    """Return True only for containers whose prefix is probeable/playable.

    yt-dlp/native fragment downloads can expose temporary names like
    ``file.webm.part-Frag159`` instead of ``file.webm.part``. Those are still
    WebM bytes, but the old suffix-only cleanup rejected them and made the
    playback caller wait for the complete download.
    """
    name = os.path.basename(path or "").lower()
    name = re.sub(r"\.part-frag\d+$", "", name)
    name = re.sub(r"\.frag\d+$", "", name)
    for suffix in (".part", ".ytdl", ".temp"):
        while name.endswith(suffix):
            name = name[: -len(suffix)]
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return ext in _EARLY_AUDIO_STREAMABLE_EXTS


def _complete_cache_files(video_id: str, tag: str) -> list:
    """Cached files for this (video_id, variant) — never partial ones.

    yt-dlp writes "<final>.part" (and "<final>.ytdl" state) while running.
    The old glob matched those too, so a second /play of the same song could
    "cache hit" onto a half-written file and play a few seconds of garbage
    (or nothing) before ending. Only real, non-empty, fully renamed files
    count as a cache hit.
    """
    out = []
    for f in glob.glob(f"/tmp/melody_{video_id}_{tag}.*"):
        # Never treat an in-progress download as a cache hit: yt-dlp writes
        # "<final>.part"/".ytdl", Pyrogram writes "<name>.temp", and tagged-media
        # staging files end in ".dl".
        if (
            f.endswith(".part")
            or f.endswith(".ytdl")
            or f.endswith(".temp")
            or f.endswith(".tmp")
            or f.endswith(".dl")
            or f.endswith(".early")
            or ".part-" in f
            or ".dl." in f
        ):
            continue
        try:
            if os.path.getsize(f) <= 0:
                continue
        except OSError:
            continue
        out.append(f)
    return out


def cached_file_path(video_id: str, audio_only: bool = True) -> "str | None":
    """Instant, network-free lookup of an already-downloaded track.

    SPEED FIX ("last played gane ko dobara play krne pe bhi same time"):
    call.py used to reach the on-disk cache only *inside* download_audio(),
    i.e. after a doomed CDN probe had already burned several seconds. This
    helper lets the player check the cache BEFORE any network work at all,
    so a replay starts in milliseconds.
    """
    try:
        found = _complete_cache_files(video_id, _cache_tag(audio_only))
    except Exception:
        return None
    return found[0] if found else None


def on_cloud_host() -> bool:
    """True on Heroku/Railway/Render/Fly/Cloud Run/Azure, where the YouTube
    CDN is IP-blocked and direct-URL streaming can never succeed."""
    return _ON_CLOUD_HOST
# Hard ceiling on how long we wait for that early-handoff threshold before
# giving up and blocking on the full download instead (pure fallback).
_EARLY_HANDOFF_TIMEOUT = _env_float("EARLY_HANDOFF_TIMEOUT", 2.0)
# SPEED FIX ("gana 20 sec baad bajta hai"): the timeout above used to be a
# HARD cutoff — miss it by a fraction of a second (very common, because yt-dlp
# spends the first seconds only resolving metadata, before a single byte is
# written) and the caller fell back to blocking on the COMPLETE download,
# turning a ~5 s start into a ~20 s one. The handoff is now awaited until it
# either fires or the download finishes, whichever comes first, so a slow
# metadata resolve costs seconds instead of the whole download.

# video_id:tag → True while a download for that variant is still running.
# call.py reads this to tell a genuine "song finished" apart from ffmpeg
# hitting EOF on a file that is still being written (premature stream end).
_downloads_in_progress: dict = {}


def is_download_in_progress(video_id: str, audio_only: bool = True) -> bool:
    """True while the given (video_id, variant) is still being downloaded."""
    return bool(_downloads_in_progress.get(f"{video_id}:{_cache_tag(audio_only)}"))


# ─────────────────────────────────────────────────────────────────────────────
#  Download retry ladder
# ─────────────────────────────────────────────────────────────────────────────
#
# ROOT-CAUSE FIX (⚠️ Melody Error Log: "prefetch_next failed" →
# `external.py real_download → '<downloader>' exited with code N`, and the
# recurring "Sign in to confirm you're not a bot" / "Requested format is not
# available" failures):
#
# One yt-dlp attempt has many independent ways to fail that are ALL fixable by
# simply asking again differently: a flaky CDN fragment, a PO-token miss on one
# client, an external downloader dying, or a format that vanished between the
# metadata resolve and the download. The old code made exactly ONE attempt and
# raised — so a single transient failure killed the whole track (and, through
# prefetch_next, spammed the owner's error log).
#
# Each rung below changes exactly one thing, cheapest first.
_DOWNLOAD_LADDER: tuple = (
    {},                                                        # as configured
    {"concurrent_fragment_downloads": 1},                      # flaky CDN / partial fragments
    {"_client": ["ios", "visionos", "web"]},                      # different API surface
    {"_client": ["ios", "ios_music", "mweb"], "concurrent_fragment_downloads": 1},
    {"_format": "bestaudio[ext=webm]/bestaudio[ext=opus]/bestaudio[ext=ogg]/"
                "bestaudio[ext=m4a]/bestaudio*[vcodec=none]/bestaudio/" + _SMALL_MUXED_SELECTOR,
     "_client": ["tv", "web"]},                                # format vanished
    # Last rung — never merge, never post-process. Fixes the recurring
    # "_stream_track failed ... YoutubeDL.post_process → run_all_pps"
    # crash, which is always an ffmpeg merge/convert failure on a DASH
    # video pair, by falling back to a single already-muxed file.
    {"_format": "bestaudio[ext=webm]/bestaudio[ext=opus]/bestaudio[ext=ogg]/"
                "bestaudio[ext=m4a]/bestaudio*[vcodec=none]/bestaudio/" + _SMALL_MUXED_SELECTOR,
     "_no_merge": True},
    # ROOT-CAUSE FIX ("ERROR: The downloaded file is empty", repeated for every
    # rung, followed by "_stream_track failed"): YouTube hands SABR-only
    # streaming URLs to the default/web clients. yt-dlp resolves them, starts
    # the download and receives 0 bytes. The `tv`/`tv_simply` and `web_safari`
    # clients still advertise plain progressive/DASH URLs, and asking for a
    # protocol-restricted (https-only, no SABR/HLS manifest) format keeps the
    # native downloader on a URL that actually returns bytes.
    {"_client": ["tv_simply", "tv"],
     "_format": "bestaudio[ext=webm][protocol^=http]/bestaudio[ext=opus][protocol^=http]/"
                "bestaudio[ext=ogg][protocol^=http]/bestaudio[ext=m4a][protocol*=dash]/"
                "bestaudio[format_id=251]/bestaudio[format_id=250]/bestaudio[format_id=249]/"
                "bestaudio[format_id=140]/bestaudio[protocol^=http]/bestaudio*[vcodec=none]/bestaudio/" + _SMALL_MUXED_SELECTOR,
     "concurrent_fragment_downloads": 1, "_no_merge": True},
    {"_client": ["ios", "visionos", "web_embedded"],
     "_format": "bestaudio[ext=webm][protocol^=http]/bestaudio[ext=opus][protocol^=http]/"
                "bestaudio[ext=ogg][protocol^=http]/bestaudio[ext=m4a][protocol*=dash]/"
                "bestaudio[format_id=251]/bestaudio[format_id=140]/"
                "bestaudio[protocol^=http]/bestaudio*[vcodec=none]/bestaudio/" + _SMALL_MUXED_SELECTOR, "_no_merge": True},
)


def _apply_ladder_step(opts: dict, step: dict, audio_only: bool) -> dict:
    """Return a copy of `opts` with one ladder step applied."""
    out = dict(opts)
    for key, value in step.items():
        if key.startswith("_"):
            continue
        out[key] = value

    clients = step.get("_client")
    if clients:
        extractor_args = {k: dict(v) for k, v in (out.get("extractor_args") or {}).items()}
        youtube = dict(extractor_args.get("youtube") or {})
        youtube["player_client"] = list(clients)
        extractor_args["youtube"] = youtube
        out["extractor_args"] = extractor_args
        # A URL minted for a mobile client can be rejected when the base
        # desktop Chrome UA is sent with it. Keep the transport fingerprint
        # aligned with the first client in each retry rung.
        client = str(clients[0]).lower()
        if client in {"ios", "ios_music"}:
            ua = "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X)"
        elif client in {"android", "android_music", "android_vr"}:
            ua = "com.google.android.youtube/20.10.38 (Linux; U; Android 14)"
        elif client == "visionos":
            ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
        elif client in {"tv", "tv_simply"}:
            ua = "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.5) AppleWebKit/537.36 TV Safari/537.36"
        else:
            ua = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/125.0 Mobile Safari/537.36"
        headers = dict(out.get("http_headers") or {})
        headers["User-Agent"] = ua
        out["http_headers"] = headers
        if client not in {"web", "web_safari", "mweb", "web_embedded", "visionos"}:
            # Browser cookies minted for desktop web are often rejected when
            # replayed with a mobile/TV client and can turn a valid media URL
            # into HTTP 403. Those client APIs are designed to work without
            # the web cookie jar, so let the client-specific identity win.
            out.pop("cookiefile", None)

    fmt = step.get("_format")
    if fmt:
        out["format"] = (
            fmt if audio_only
            else "best[vcodec!=none][acodec!=none]/best[height<=480]/best"
        )
    if step.get("_no_merge"):
        # A muxed-only selector plus an empty post-processor chain means
        # yt-dlp has nothing left that can fail inside post_process().
        out["postprocessors"] = []
        out.pop("merge_output_format", None)
    return out


def _purge_partial_outputs(outtmpl) -> None:
    """Delete half-written / single-track leftovers of a failed attempt."""
    if isinstance(outtmpl, dict):
        outtmpl = outtmpl.get("default")
    if not isinstance(outtmpl, str):
        return
    base = outtmpl.split(".%(ext)s")[0]
    for leftover in glob.glob(f"{base}.*"):
        if re.search(r"\.f\d+\.", leftover) or leftover.endswith((".part", ".ytdl")):
            try:
                os.unlink(leftover)
            except OSError:
                pass


_PERMANENT_DOWNLOAD_MARKERS = (
    # yt-dlp currently emits both "This video is unavailable" and
    # "Video unavailable. This content isn't available." depending on the
    # InnerTube client; both are terminal content failures, not retryable I/O.
    "this video is unavailable",
    "video unavailable",
    "this content isn't available",
    "this content isn’t available",
    "video has been removed",
    "private video",
    "no video formats found",
    "sign in to confirm your age",
    "members-only content",
    "not available in your country",
    "this video is drm protected",
    "drm protected",
    # Format availability is client/rung-specific; it must remain retryable
    # so tv/mobile/iOS fallback clients can provide a different format set.
    "only images are available",
    "only storyboards are available",
)


def _is_permanent_download_error(exc: BaseException) -> bool:
    """Return True for content failures that no client/ladders can repair."""
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            break
        text = str(current).lower()
        if any(marker in text for marker in _PERMANENT_DOWNLOAD_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _extract_with_retries(url: str, base_opts: dict, audio_only: bool):
    """Download `url`, walking the retry ladder until one attempt succeeds.

    Returns (info_dict, filepath). Raises the LAST error only when every rung
    failed, so callers still see a real traceback for genuinely dead videos.
    """
    last_exc: BaseException | None = None
    # SPEED FIX: 8 rungs x (retries x socket_timeout) could keep a single
    # /play busy for minutes. Bound the whole ladder by wall clock so a
    # genuinely broken video fails fast instead of holding the chat hostage.
    deadline = _time_mod.monotonic() + _env_float("DOWNLOAD_DEADLINE", 45.0)
    for index, step in enumerate(_DOWNLOAD_LADDER):
        if index and _time_mod.monotonic() > deadline:
            LOGGER.warning("download ladder deadline hit for %s after %d attempts", url, index)
            break
        opts = _apply_ladder_step(base_opts, step, audio_only)
        try:
            with _locked_ytdl(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = ydl.prepare_filename(info)
                # Some extractors/post-processors report a final filepath
                # that differs from prepare_filename(). Prefer yt-dlp's own
                # final path when present, then fall back to the completed
                # output discovered by the caller.
                requested = info.get("requested_downloads") or []
                final_path = info.get("filepath") or info.get("_filename")
                if requested and isinstance(requested[0], dict):
                    final_path = requested[0].get("filepath") or final_path
                if final_path and os.path.exists(final_path):
                    path = final_path
                if index or (info.get("vcodec") not in (None, "none")):
                    LOGGER.info(
                        "#download rung %d picked format_id=%s ext=%s vcodec=%s acodec=%s for %s",
                        index + 1, info.get("format_id"), info.get("ext"),
                        info.get("vcodec"), info.get("acodec"), url,
                    )
                # A SABR/expired-URL download can finish "successfully" with a
                # zero-byte file. Treat that as a failed rung so the ladder
                # keeps walking instead of handing an unplayable file to
                # PyTgCalls (which then dies inside _stream_track).
                if os.path.exists(path) and os.path.getsize(path) < 1024:
                    raise RuntimeError(
                        f"downloaded file is empty ({os.path.basename(path)})"
                    )
                return info, path
        except FileNotFoundError as exc:
            # A failed implicit/legacy temp rename can happen after the media
            # file has already reached its final name. Recover that completed
            # output here; if it genuinely does not exist, continue down the
            # ladder rather than turning one missing temp file into a hard
            # playback failure.
            outtmpl = opts.get("outtmpl")
            if isinstance(outtmpl, dict):
                outtmpl = outtmpl.get("default")
            base = outtmpl.split(".%(ext)s")[0] if isinstance(outtmpl, str) else ""
            completed = [
                p for p in glob.glob(f"{base}.*")
                if not p.endswith((".part", ".ytdl", ".temp", ".tmp"))
                and ".temp." not in p
                and os.path.isfile(p)
                and os.path.getsize(p) >= 1024
            ]
            if completed:
                LOGGER.info("Recovered completed download after temp rename race: %s", completed[0])
                return {}, completed[0]
            last_exc = exc
            _purge_partial_outputs(opts.get("outtmpl"))
            LOGGER.warning(
                "download attempt %d/%d hit a missing temp file for %s; trying fallback",
                index + 1, len(_DOWNLOAD_LADDER), url,
            )
            continue
        except _DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — every rung is a retry
            last_exc = exc
            if _is_permanent_download_error(exc):
                _purge_partial_outputs(opts.get("outtmpl"))
                LOGGER.info(
                    "download permanently unavailable for %s — skipping remaining fallback clients: %s",
                    url, redact_sensitive_text(exc),
                )
                raise
            # A failed merge/convert leaves single-track "*.fNNN.*" siblings
            # behind. Left on disk they are picked up later as a bogus cache
            # hit (video with no audio), so clear them before the next rung.
            _purge_partial_outputs(opts.get("outtmpl"))
            LOGGER.warning(
                "download attempt %d/%d failed for %s: %s",
                index + 1, len(_DOWNLOAD_LADDER), url, redact_sensitive_text(exc),
            )
    raise last_exc if last_exc else RuntimeError(f"download failed for {url}")


def _download_audio_sync(video_id: str, audio_only: bool = True,
                          early_event: "threading.Event | None" = None,
                          early_holder: "dict | None" = None,
                          cancel_event: "threading.Event | None" = None) -> str:
    """Synchronous full-file download.

    SPEED FIX (2-3s /play requirement): yt-dlp writes fragments straight into
    the destination file IN ORDER as they arrive (that's how its native
    fragment downloader works even with concurrent_fragment_downloads>1 — an
    early-arriving fragment is buffered until the ones before it have been
    written), so the destination file is a normal, valid, ever-growing file
    from byte 0 onward while the download is still in progress. Unlike the
    FIFO pipe this bot used to (briefly) use, a REAL file can be opened,
    probed, and read more than once — so PyTgCalls' ffprobe check_stream()
    plus its real playback read can both consume it safely without stepping
    on each other or losing data, and download speed (fragmented, parallel)
    is almost always much faster than 1x realtime playback, so the writer
    stays comfortably ahead of the reader for the whole track.
    `progress_hooks` reports `downloaded_bytes` and the live destination path
    as fragments land; once enough header+data bytes exist we signal
    `early_event` with the (still-growing) path in `early_holder['early_path']`
    so the caller can start playback immediately instead of waiting for the
    entire file. This function still runs to completion and always sets
    `early_holder['final_path']` when done, regardless of whether the early
    signal fired.
    """
    # A direct http(s) source (live/radio/m3u8) is already a URL — wrapping it
    # in a YouTube watch link produced a guaranteed-404 download that burned
    # the whole retry ladder before the caller could fall back.
    url = video_id if is_direct_url(video_id) else f"https://www.youtube.com/watch?v={video_id}"
    tag = _cache_tag(audio_only)
    # RACE FIX (Heroku log: "Unable to download video: [Errno 2] No such file
    # or directory: '/tmp/melody_<id>_a.mp4.part-Frag159'"): writing fragments
    # straight into the shared /tmp/melody_<id>_<tag>.* namespace meant any
    # concurrent cache sweep / retry purge / second download of the SAME song
    # could delete or replace files yt-dlp's fragment downloader was still
    # writing. Every attempt now writes into its own unique staging
    # sub-directory (which no glob or sweeper touches) and the finished file
    # is atomically os.replace()d into the canonical cache path on success.
    import uuid as _uuid
    attempt_dir = os.path.join(
        STAGING_DIR, f"yt_{video_id}_{tag}_{_uuid.uuid4().hex[:8]}"
    )
    os.makedirs(attempt_dir, exist_ok=True)
    outtmpl = os.path.join(attempt_dir, "file.%(ext)s")
    _downloads_in_progress[f"{video_id}:{tag}"] = True

    def _cancel_hook(_d):
        if cancel_event is not None and cancel_event.is_set():
            raise _DownloadCancelled("superseded by a newer playback request")

    def _hook(d):
        if early_event is None or early_event.is_set():
            return
        if d.get("status") != "downloading":
            return
        fp = d.get("filename") or d.get("tmpfilename")
        if fp and not os.path.exists(fp):
            # yt-dlp writes into "<final>.part" first — that's the file that
            # actually exists while the download runs.
            alt = d.get("tmpfilename") or f"{fp}.part"
            if alt and os.path.exists(alt):
                fp = alt
        # Never hand a growing MP4/M4A (moov atom at EOF) or a video file to
        # PyTgCalls. Audio-only WebM/Opus/MP3-style prefixes are the only safe
        # early sources; all other formats wait for the atomic final rename.
        if not audio_only or not _early_audio_file_is_safe(fp or ""):
            return
        # Native fragment downloads do not consistently populate
        # `downloaded_bytes`; on Heroku this left the safe WebM prefix unseen
        # until the full file completed, producing the 13–17s fallback seen in
        # the deployment log. The filesystem is authoritative for the writer
        # path, so use its current size whenever the progress field is missing.
        downloaded = d.get("downloaded_bytes") or 0
        if fp:
            try:
                downloaded = max(downloaded, os.path.getsize(fp))
            except OSError:
                pass
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        ready = _early_handoff_ready(downloaded, total)
        if fp and ready and os.path.exists(fp):
            if early_holder is not None:
                stable = f"/tmp/melody_{video_id}_{tag}.early"
                try:
                    link_tmp = f"{stable}.{os.getpid()}.tmp"
                    if os.path.lexists(link_tmp):
                        os.unlink(link_tmp)
                    os.symlink(fp, link_tmp)
                    os.replace(link_tmp, stable)
                    early_holder["early_path"] = stable
                except OSError:
                    # If symlink creation is unavailable, the real staging path
                    # is still usable until the atomic completion rename.
                    early_holder["early_path"] = fp
            LOGGER.info(
                "⚡ #download early audio handoff %s variant=%s bytes=%d path=%s",
                video_id, tag, downloaded,
                os.path.basename((early_holder or {}).get("early_path") or fp),
            )
            early_event.set()

    opts = {
        **_ydl_opts(audio_only=audio_only),
        "outtmpl": outtmpl,
        "extract_flat": False,
    }
    opts["progress_hooks"] = [*opts.get("progress_hooks", []), _cancel_hook]
    if early_event is not None:
        opts["progress_hooks"].append(_hook)

    # ROOT-CAUSE FIX (Sep 10 log: "#download complete elapsed=9.33s" with the
    # early handoff never firing): yt-dlp's progress hook is not a reliable
    # signal on every rung/downloader. A tiny watcher thread polls the staging
    # directory directly, so the growing WebM/Opus prefix is handed off as soon
    # as it exists no matter which code path produced it.
    _watch_stop = threading.Event()

    def _watch_staging():
        while not _watch_stop.wait(0.10):
            if early_event is None or early_event.is_set():
                return
            try:
                candidates = glob.glob(os.path.join(attempt_dir, "*"))
            except OSError:
                continue
            for cand in candidates:
                if not audio_only or not _early_audio_file_is_safe(cand):
                    continue
                try:
                    size = os.path.getsize(cand)
                except OSError:
                    continue
                if not _early_handoff_ready(size):
                    continue
                if early_holder is not None:
                    stable = f"/tmp/melody_{video_id}_{tag}.early"
                    try:
                        link_tmp = f"{stable}.{os.getpid()}.tmp"
                        if os.path.lexists(link_tmp):
                            os.unlink(link_tmp)
                        os.symlink(cand, link_tmp)
                        os.replace(link_tmp, stable)
                        early_holder["early_path"] = stable
                    except OSError:
                        early_holder["early_path"] = cand
                LOGGER.info(
                    "\u26a1 #download early audio handoff (watcher) %s variant=%s bytes=%d path=%s",
                    video_id, tag, size, os.path.basename(cand),
                )
                early_event.set()
                return

    _watcher = None
    if early_event is not None and audio_only:
        _watcher = threading.Thread(
            target=_watch_staging, daemon=True, name=f"melody-early-watch-{video_id[:8]}",
        )
        _watcher.start()

    try:
        try:
            info, filepath = _extract_with_retries(url, opts, audio_only)
        except FileNotFoundError:
            # yt-dlp .part rename race: yt-dlp downloads to a .part temp file
            # and then os.replace()s it to the final filename. If another
            # concurrent download (or the early-handoff path) already moved
            # or consumed the .part file, the rename raises
            # FileNotFoundError inside yt-dlp. The final file may still
            # exist on disk — check for it before giving up.
            found = _complete_cache_files(video_id, tag)
            if found:
                filepath = found[0]
            else:
                raise
        if not os.path.exists(filepath):
            found = _complete_cache_files(video_id, tag)
            if found:
                filepath = found[0]
            else:
                raise FileNotFoundError(f"Downloaded file not found for video_id={video_id}")
        # Move the finished file out of the per-attempt staging dir into the
        # canonical /tmp/melody_<id>_<tag>.<ext> cache location. os.replace()
        # is atomic on the same filesystem, so concurrent readers either see
        # the old file or the new one — never a half-moved file. Processes
        # that already opened the staging path (early-handoff playback) keep
        # their valid file descriptor across the rename on Linux.
        if filepath.startswith(attempt_dir + os.sep):
            ext = filepath.rsplit(".", 1)[-1] if "." in filepath else "webm"
            canonical = f"/tmp/melody_{video_id}_{tag}.{ext}"
            try:
                existing = _complete_cache_files(video_id, tag)
                if existing:
                    # Another chat finished the same track first — use theirs.
                    filepath = existing[0]
                else:
                    os.replace(filepath, canonical)
                    filepath = canonical
                    # Keep the stable early path resolvable after the atomic
                    # rename. The symlink lives outside the staging directory,
                    # so cleanup cannot remove the path while FFmpeg is opening.
                    early_path = (early_holder or {}).get("early_path")
                    if early_path and early_path.startswith("/tmp/melody_"):
                        try:
                            link_tmp = f"{early_path}.{os.getpid()}.tmp"
                            if os.path.lexists(link_tmp):
                                os.unlink(link_tmp)
                            os.symlink(canonical, link_tmp)
                            os.replace(link_tmp, early_path)
                        except OSError:
                            pass
            except OSError:
                # Move failed — the staging path itself is still a valid file.
                pass
        if early_holder is not None:
            early_holder["final_path"] = filepath
        try:
            LOGGER.info(
                "#download file %s variant=%s ext=%s size=%d early=%s",
                video_id, tag, filepath.rsplit(".", 1)[-1],
                os.path.getsize(filepath),
                bool(early_event is not None and early_event.is_set()),
            )
        except Exception:  # noqa: BLE001
            pass
        return filepath
    finally:
        try:
            _watch_stop.set()
        except Exception:  # noqa: BLE001
            pass
        # Best-effort cleanup of this attempt's staging leftovers.
        try:
            import shutil as _shutil
            _shutil.rmtree(attempt_dir, ignore_errors=True)
        except Exception:
            pass
        _downloads_in_progress.pop(f"{video_id}:{tag}", None)
        # Whatever happens (success or exception), make sure a waiter
        # blocked on early_event doesn't hang forever if the threshold was
        # never reached (e.g. the whole track is shorter than the threshold).
        if early_event is not None:
            early_event.set()


def _replied_media_object(message):
    """Return the playable media object on a message (audio/video/voice/
    video_note/audio-or-video document), or None if it isn't playable."""
    if not message:
        return None
    if message.audio:
        return message.audio
    if message.video:
        return message.video
    if message.voice:
        return message.voice
    if message.video_note:
        return message.video_note
    doc = message.document
    if doc:
        mime_kind = (getattr(doc, "mime_type", None) or "").split("/")[0].lower()
        filename = (getattr(doc, "file_name", None) or "").lower()
        ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
        # Telegram often labels files sent as documents as
        # application/octet-stream. Filename extension is a safe second signal
        # for the media types this bot can decode, and fixes tagged MP3/MP4
        # files that previously fell through to the plain-query path.
        if mime_kind in ("audio", "video") or ext in {
            "mp3", "mp4", "m4a", "aac", "flac", "wav", "ogg", "oga",
            "opus", "webm", "mkv", "mov", "avi", "m4v",
        }:
            return doc
    return None


def has_playable_media(message) -> bool:
    """True if `message` (typically message.reply_to_message) carries an
    audio/video file that /play or /vplay can stream directly — this is the
    "tag any audio/video and play it" feature."""
    return _replied_media_object(message) is not None


def tagged_media_limit_reason(message, video: bool = False) -> str | None:
    """Return a user-facing reason before a tagged file is downloaded.

    A 3GB MP4 cannot be converted into a playable local source on a 512MB
    Heroku dyno. Returning a clear reason is safer than starting a transfer
    that will inevitably hit R14/disk exhaustion and leave a broken queue.
    """
    media = _replied_media_object(message)
    if not media:
        return None
    # Large files use the range proxy, so size alone is no longer rejected.
    # Only duration is limited here; the proxy transfers bounded Telegram
    # chunks on demand and never materializes the whole movie locally.
    duration = int(getattr(media, "duration", 0) or 0)
    if duration > _TAGGED_MAX_DURATION:
        return (
            f"❌ Tagged media {duration // 3600}h se zyada lamba hai. "
            "Shorter audio/video ya direct `/live` stream use karo."
        )
    return None


# ── Tagged-Telegram-media download state ──────────────────────────────────
#
# BUG FIX #1 (reported log): `download_replied_media failed ... FileNotFound
# Error: '/tmp/melody_tgn1004442722409_60_v.mkv.temp' -> '...mkv'`.
# Pyrogram downloads to `<dest>.temp` and then `shutil.move()`s it onto
# `<dest>`. Two concurrent downloads of the SAME tagged message (a /play and a
# /vplay, a double tap, or a queue prefetch racing the player) therefore share
# one `.temp` path: whoever finishes first moves it away and the other one
# crashes on a file that no longer exists. Fix = one lock per (chat, message,
# variant) so a second caller reuses the first download instead of racing it,
# plus a unique per-attempt temp destination and a final "did the file appear
# anyway?" check before reporting failure.
#
# BUG FIX #2 (reported log): `download_audio: cached Telegram media for
# 'tgn..._239' not found (re-tag the message)`. /tmp is volatile (Heroku clears
# it on every dyno restart, and long queues outlive the file). Remembering
# which Telegram message a synthetic id came from lets download_audio()
# silently re-download it instead of killing playback with a dead end.
_tg_media_locks: "dict[str, asyncio.Lock]" = {}
# synthetic video_id -> (chat_id, message_id)
TG_MEDIA_SOURCES: "dict[str, tuple[int, int]]" = {}


# R14 FIX: these three dicts only ever grew. Every tagged audio/video the bot
# ever touched left a Lock, a (chat, message) tuple and an early-path string
# behind, so a long-running worker leaked them for the whole dyno lifetime.
# Bounded FIFO eviction keeps the useful recent entries and drops the rest.
_TG_STATE_MAX = 256


def _bound_dict(store: dict, limit: int) -> None:
    """Evict oldest insertions until `store` is back under `limit` (FIFO)."""
    while len(store) > limit:
        try:
            store.pop(next(iter(store)))
        except (StopIteration, KeyError):
            break


def _tg_media_lock(key: str) -> "asyncio.Lock":
    lock = _tg_media_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        # Never evict a lock that is currently held — that would re-open the
        # ".temp" rename race this lock exists to prevent.
        for stale in [k for k, v in list(_tg_media_locks.items()) if not v.locked()][
            : max(0, len(_tg_media_locks) - _TG_STATE_MAX)
        ]:
            _tg_media_locks.pop(stale, None)
        _tg_media_locks[key] = lock
    return lock


def _synthetic_media_id(chat_id: int, message_id: int) -> str:
    return f"tg{str(chat_id).replace('-', 'n')}_{message_id}"


def is_direct_url(video_id: "str | None") -> bool:
    """True when a Track's `video_id` is itself a playable http(s) source.

    /live, /stream, /m3u8 and /radio store the raw URL as the track id so the
    whole existing pipeline (resolve_stream_urls → _build_direct_stream) can
    play an HLS/ICY/radio endpoint with no YouTube lookup at all.
    """
    return bool(video_id) and str(video_id).lower().startswith(("http://", "https://"))


def is_tg_media_id(video_id: str) -> bool:
    """True for synthetic tagged-Telegram-media ids ("tg<chat>_<msg>").

    These have no YouTube source at all, so every yt-dlp/CDN code path
    (resolve_stream_urls, direct-stream race, search fallback) must be
    skipped for them — otherwise the player burns its whole resolver
    timeout on a doomed lookup before falling back to the local file,
    which is exactly why tagged audio/video felt slow or failed to play.
    """
    return bool(re.fullmatch(r"tg(n?)(\d+)_(\d+)", video_id or ""))


def _parse_synthetic_media_id(video_id: str) -> "tuple[int, int] | None":
    """Recover (chat_id, message_id) from a synthetic tagged-media id.

    The id already encodes both ("tg" + chat id with '-' written as 'n' + "_" +
    message id), so this works even after a restart wiped TG_MEDIA_SOURCES.
    """
    match = re.fullmatch(r"tg(n?)(\d+)_(\d+)", video_id or "")
    if not match:
        return None
    sign, chat_digits, message_id = match.groups()
    chat_id = int(chat_digits) * (-1 if sign else 1)
    return chat_id, int(message_id)


STAGING_DIR = "/tmp/melody_dl"


def _sweep_staging_dir(max_age: float = 1800.0) -> None:
    """Delete abandoned tagged-media staging files (>30 min old).

    Only files that no live download can still be writing are removed: an
    in-flight download touches its file continuously, so an mtime older than
    `max_age` means the writer is gone (crash, restart, FloodWait abort).
    """
    now = _time_mod.time()
    for f in glob.glob(os.path.join(STAGING_DIR, "*")):
        try:
            if now - os.path.getmtime(f) < max_age:
                continue
            os.unlink(f)
        except OSError:
            pass


# ── ⚡ Tagged-media early handoff ─────────────────────────────────────────
# SPEED FIX ("audio/video ko tag karke /play /vplay karne par 5-10 sec lagte
# hain"): a tagged file used to be downloaded from Telegram to 100% BEFORE the
# player was even told about it, so the whole transfer sat on the critical
# path. Telegram writes the file sequentially, exactly like yt-dlp does, so the
# same growing-file trick used for YouTube downloads works here: once a healthy
# prefix has landed, playback starts from the still-growing file while the rest
# keeps downloading in the background.
#
# Only container formats that can be decoded from the first bytes are handed
# off early. MP4/M4A keep their index (moov atom) at the END of the file, so a
# partial one cannot be probed — those still wait for the complete download.
_TG_STREAMABLE_EXTS = {"mp3", "ogg", "oga", "opus", "webm", "mkv", "flac", "wav"}
# A Telegram file is not a seekable public URL that PyTgCalls can hand to
# ffmpeg. On a 512MB dyno, downloading multi-GB tagged movies locally would
# exhaust disk/RSS before playback and previously surfaced as a generic play
# failure. Reject impossible requests before starting the transfer. Deployers
# may raise this only when they have a larger disk/memory profile.
_TAGGED_PROXY_THRESHOLD_MB = max(32, _env_int("TAGGED_PROXY_THRESHOLD_MB", 256))
_TAGGED_PROXY_THRESHOLD_BYTES = _TAGGED_PROXY_THRESHOLD_MB * 1024 * 1024
_TAGGED_MAX_DURATION = max(600, _env_int("TAGGED_MAX_DURATION_SECONDS", 6 * 3600))
# key "<vid>:<tag>" -> path of a file that is still being written
_TG_EARLY_PATHS: "dict[str, str]" = {}
# key "<vid>:<tag>" -> asyncio task finishing that download
_TG_INFLIGHT: "dict[str, asyncio.Task]" = {}
_TG_EARLY_MIN_BYTES = _env_int("TG_EARLY_BYTES", 1_500_000)
_TG_EARLY_RATIO = 0.30


def tg_early_path(video_id: str, audio_only: bool = True) -> "str | None":
    """Path of a tagged-media file that is downloading right now and already
    has enough bytes on disk to start playing."""
    path = _TG_EARLY_PATHS.get(f"{video_id}:{_cache_tag(audio_only)}")
    if path and os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    return None


async def _download_tg_media_file(client, message, dest: str,
                                  early_cb=None) -> "str | None":
    """Download `message`'s media to `dest`, race- and restart-safe.

    Downloads to a per-attempt unique temp name (so no two callers can ever
    share Pyrogram's `<dest>.temp` path) and atomically renames it into place.
    """
    # ROOT-CAUSE FIX ("No such file or directory: '/tmp/melody_..._v.mkv.2.<ts>.dl.temp'"):
    # the in-progress download used to live in the SAME /tmp/melody_* namespace
    # the cache janitor sweeps (`_cleanup_track_file`/`_cleanup_partial_files`
    # in call.py glob `/tmp/melody_*_?.*`, which happily matched
    # `melody_<id>_v.mkv.<pid>.<ts>.dl.temp`). So a track finishing — or a retry
    # purging "partials" — deleted Pyrogram's `.temp` file mid-download and the
    # final `shutil.move()` blew up with FileNotFoundError. Downloads now stage
    # in a SUB-DIRECTORY (/tmp/melody_dl/), which those globs cannot match, and
    # only the finished file is moved into the cache namespace.
    staging_dir = STAGING_DIR
    try:
        os.makedirs(staging_dir, exist_ok=True)
    except OSError:
        staging_dir = "/tmp"
    # The staging dir lives OUTSIDE the cache namespace, so call.py's size-cap
    # eviction never sees it. Without its own janitor a failed/aborted download
    # (FloodWait, dyno restart, network drop) leaves a multi-MB ".dl" orphan in
    # /tmp forever and Heroku's 512 MB disk slowly fills up. Sweep stale ones.
    _sweep_staging_dir()
    unique = os.path.join(
        staging_dir,
        f"{os.path.basename(dest)}.{os.getpid()}.{int(_time_mod.time() * 1000)}.dl",
    )
    got = None
    last_exc = None
    attempted: list = []
    try:
        for attempt in range(2):
            attempted.append(unique)
            try:
                if early_cb is not None:
                    _seen = {"fired": False}
                    _stem = unique

                    def _progress(current, total, _stem=_stem, _seen=_seen):
                        # Report the growing file exactly once, as soon as a
                        # healthy prefix is on disk. Pyrogram writes to a
                        # "<name>.temp" sibling, so find the real writer by
                        # picking the largest file sharing our unique stem.
                        if _seen["fired"]:
                            return
                        need = max(_TG_EARLY_MIN_BYTES, int((total or 0) * _TG_EARLY_RATIO))
                        if current < need:
                            return
                        candidates = [
                            f for f in glob.glob(f"{_stem}*")
                            if os.path.isfile(f) and os.path.getsize(f) > 0
                        ]
                        if not candidates:
                            return
                        _seen["fired"] = True
                        early_cb(max(candidates, key=os.path.getsize))

                    got = await client.download_media(
                        message, file_name=unique, progress=_progress,
                    )
                else:
                    got = await client.download_media(message, file_name=unique)
                break
            except FileNotFoundError as exc:
                # Another download of the same message already produced the final file.
                if os.path.exists(dest) and os.path.getsize(dest) > 0:
                    return dest
                last_exc = exc
                # Someone removed our staging file underneath us — retry once with a
                # brand-new name instead of failing the whole track.
                unique = f"{unique}.r{attempt + 1}"
                await asyncio.sleep(0.5)
    finally:
        # Drop every half-written staging file (and Pyrogram's "<name>.temp")
        # this call created, except the one we are about to move into place.
        for stale in attempted:
            for leftover in glob.glob(f"{stale}*"):
                if got and os.path.abspath(leftover) == os.path.abspath(got):
                    continue
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
    if got is None:
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest
        raise last_exc if last_exc else FileNotFoundError(dest)
    if not got or not os.path.exists(got):
        return dest if os.path.exists(dest) and os.path.getsize(dest) > 0 else None
    try:
        os.replace(got, dest)
    except OSError:
        return got
    return dest


def _tg_media_info(media, message, vid: str, stream_url: str = "") -> dict:
    """Adapt a tagged Telegram media object into get_video_info()'s shape."""
    title = (
        getattr(media, "file_name", None)
        or getattr(media, "title", None)
        or "Tagged Media"
    )
    uploader = (
        getattr(media, "performer", None)
        or (message.from_user.first_name if message.from_user else None)
        or "Telegram"
    )
    return {
        "id": vid,
        "title": title,
        "duration": int(getattr(media, "duration", 0) or 0),
        "url": "",
        "stream_url": stream_url or "",
        "thumbnail": "",
        "uploader": uploader,
    }


async def download_replied_media(client, message, video: bool = False) -> "dict | None":
    """
    Download a tagged (replied-to) Telegram audio/video/voice message and
    adapt it into the same info-dict shape get_video_info() returns, so the
    existing Track/queue/thumbnail pipeline in play.py works completely
    unchanged for tagged media too.

    ROOT-CAUSE for why this reuses download_audio()'s cache path instead of
    a separate code path: the file is saved directly at
    `/tmp/melody_<synthetic_id>_<a|v>.<ext>` — exactly where
    download_audio()'s cache check looks. So when _stream_track() later
    calls download_audio(track.video_id, audio_only=not video), it's a
    guaranteed cache hit and yt-dlp is never touched for tagged media.
    """
    media = _replied_media_object(message)
    if not media:
        return None
    if tagged_media_limit_reason(message, video=video):
        LOGGER.info("tagged media rejected before download: size/duration limit")
        return None

    # Synthetic id keyed on the source chat + message so re-tagging the same
    # message twice (e.g. /play then /vplay) reuses/creates the correct
    # audio vs video cached variant independently, same as YouTube tracks.
    vid = _synthetic_media_id(message.chat.id, message.id)
    tag = _cache_tag(not video)
    # Remember the source so download_audio() can re-fetch this file if /tmp
    # is wiped (dyno restart) or the queue outlives the download.
    TG_MEDIA_SOURCES[vid] = (message.chat.id, message.id)
    _bound_dict(TG_MEDIA_SOURCES, _TG_STATE_MAX)

    key = f"{vid}:{tag}"
    async with _tg_media_lock(key):
        cached = _complete_cache_files(vid, tag)
        if not cached:
            file_size = int(getattr(media, "file_size", 0) or 0)
            file_name = getattr(media, "file_name", None) or ""
            if file_size >= _TAGGED_PROXY_THRESHOLD_BYTES:
                try:
                    from utils.tg_media_proxy import create_media_proxy
                    proxy_url = await create_media_proxy(
                        client, message, size=file_size, filename=file_name or "media"
                    )
                    LOGGER.info(
                        "tagged media using range proxy id=%s size_mb=%.0f",
                        vid, file_size / 1048576,
                    )
                    return _tg_media_info(media, message, vid, proxy_url)
                except Exception as exc:
                    LOGGER.warning(
                        "tagged range proxy unavailable id=%s error=%s",
                        vid, type(exc).__name__,
                    )
                    return None
            ext = file_name.rsplit(".", 1)[-1] if "." in file_name else None
            if not ext:
                ext = "mp4" if message.video or message.video_note else ("ogg" if message.voice else "mp3")
            dest = f"/tmp/melody_{vid}_{tag}.{ext}"

            # ⚡ Early handoff: for streamable containers big enough to be
            # worth it, return as soon as a healthy prefix is on disk and let
            # the rest download in the background — the user hears the tagged
            # song in ~1-2s instead of after the whole transfer.
            size = int(getattr(media, "file_size", 0) or 0)
            can_stream_early = (
                ext.lower() in _TG_STREAMABLE_EXTS
                and size > 3_000_000
                and _EARLY_HANDOFF_ENABLED
            )
            if can_stream_early:
                loop = asyncio.get_running_loop()
                ready = asyncio.Event()

                def _on_early(path: str):
                    _TG_EARLY_PATHS[key] = path
                    _bound_dict(_TG_EARLY_PATHS, _TG_STATE_MAX)
                    loop.call_soon_threadsafe(ready.set)

                async def _run():
                    try:
                        return await _download_tg_media_file(
                            client, message, dest, early_cb=_on_early,
                        )
                    finally:
                        _TG_EARLY_PATHS.pop(key, None)
                        _TG_INFLIGHT.pop(key, None)

                task = asyncio.ensure_future(_run())
                _TG_INFLIGHT[key] = task
                ready_task = asyncio.ensure_future(ready.wait())
                try:
                    await asyncio.wait(
                        {task, ready_task},
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=_EARLY_HANDOFF_TIMEOUT,
                    )
                finally:
                    ready_task.cancel()
                if not task.done() and _TG_EARLY_PATHS.get(key):
                    LOGGER.info("⚡ tagged media %s handed off early while downloading", vid)
                    return _tg_media_info(media, message, vid)
                try:
                    filepath = await task
                except Exception as exc:
                    filepath = None
                    found = _complete_cache_files(vid, tag)
                    if found:
                        filepath = found[0]
                    else:
                        await send_error_log(
                            f"download_replied_media failed for message {message.id}",
                            exc,
                            context={
                                "chat_id": message.chat.id if message.chat else None,
                                "user_id": message.from_user.id if message.from_user else None,
                            },
                        )
                        return None
                if not filepath or not os.path.exists(filepath):
                    return None
                return _tg_media_info(media, message, vid)

            try:
                filepath = await _download_tg_media_file(client, message, dest)
            except Exception as exc:
                # Last chance: a parallel download may have completed it.
                found = _complete_cache_files(vid, tag)
                if found:
                    filepath = found[0]
                else:
                    await send_error_log(
                        f"download_replied_media failed for message {message.id}",
                        exc,
                        context={
                            "chat_id": message.chat.id if message.chat else None,
                            "user_id": message.from_user.id if message.from_user else None,
                        },
                    )
                    return None
            if not filepath or not os.path.exists(filepath):
                return None

    return _tg_media_info(media, message, vid)


async def download_audio(
    video_id: str, audio_only: bool = True, priority: int = 0, owner=None,
    allow_early: bool = False,
) -> str:
    """Return a path to play audio (or audio+video) from — usually a REAL
    file that is still being written to on disk (see below).

    ROOT-CAUSE FIX (audio/video mismatch on /vplay): the cache lookup used to
    key ONLY on video_id (`/tmp/melody_<video_id>.*`), with no distinction
    between an audio-only download and a video download of the very same
    YouTube video. So if a chat played a track with /play first (caching an
    AUDIO-ONLY file) and someone then ran /vplay on that same track, this
    function found the cached audio-only file and happily returned it —
    which _stream_track then wrapped in a MediaStream with video_parameters
    set, producing a "video" stream that actually carried no video track at
    all (or, in the opposite order, wasted a full video re-download for a
    plain /play). The cache key now includes an audio/video tag
    (`/tmp/melody_<video_id>_a.*` vs `/tmp/melody_<video_id>_v.*`) so /play
    and /vplay always fetch (and reuse) the correct variant independently.

    FIFO pipe streaming was REMOVED (root-cause fix, kept for history):
    PyTgCalls' MediaStream calls ffmpeg.check_stream() before playback,
    which runs `ffprobe` on the given path — a named pipe (FIFO) only
    supports being drained ONCE, so after ffprobe read it for the probe
    there was nothing left for the real playback ffmpeg process, causing
    `_stream_track failed ... FileNotFoundError`.

    SPEED FIX (strict <2-3s /play requirement) — growing-file early handoff:
    Simply blocking on the FULL download (the fallback this file settled
    on after removing FIFO) reintroduced the exact 5-10s delay FIFO was
    meant to avoid. The fix is a REAL file instead of a FIFO: yt-dlp writes
    fragments into the destination file in order as they download, so
    (unlike a FIFO) the file can be safely opened, probed, and read more
    than once WHILE it is still growing — ffprobe's initial read only needs
    the header + first bit of data (guaranteed by preferring WebM/Opus,
    which doesn't need an end-of-file index the way AAC/M4A does), and the
    real ffmpeg playback read afterwards keeps consuming new bytes as they
    land, staying far behind the download's write position since fragment
    downloads run several times faster than 1x realtime playback. So:
      1. Kick off the real download in a background thread immediately.
      2. As soon as ~_EARLY_HANDOFF_BYTES have landed on disk (typically a
         few hundred ms), hand that (still-growing) path back to the caller
         — _stream_track/PyTgCalls can start playing right away.
      3. If that threshold is never reached within _EARLY_HANDOFF_TIMEOUT
         (very slow connection, or a track shorter than the threshold),
         fall back to waiting for the completed download exactly as before
         — a pure fallback, never a regression versus the prior behaviour.
    The download itself keeps running to completion in the background
    either way, so the cache file is always complete for the next play.

    Files written to /tmp/melody_<video_id>_<a|v>.* are cleaned up by
    call.py after each track finishes to avoid filling the 512 MB /tmp on
    Heroku.
    """
    # Defensive guard: a valid YouTube video ID is exactly 11 chars of
    # [A-Za-z0-9_-]. If a search query or full URL leaked in as video_id
    # (the AutoPlay bug — see _pick_related_track), reject it here instead
    # of constructing an invalid URL that crashes yt-dlp with
    # "Unsupported URL" and kills playback silently.
    #
    # EXCEPTION: tagged Telegram media uses a synthetic id of the form
    # "tg<chat_id>_<message_id>" (see download_replied_media). The file is
    # already on disk at /tmp/melody_<synthetic_id>_<a|v>.<ext>, so this must
    # be a cache hit — yt-dlp is never touched. Let these through the guard
    # so they reach the cache check below instead of being rejected here.
    import re
    is_synthetic = bool(re.fullmatch(r"tg[A-Za-z0-9_]+", video_id or ""))
    if not is_synthetic and not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        raise ValueError(
            f"download_audio: invalid YouTube video_id {video_id!r} "
            "(expected 11-char [A-Za-z0-9_-] ID)"
        )

    tag = _cache_tag(audio_only)
    _download_requested_at = _time_mod.monotonic()
    # Direct CDN resolution is owned by _stream_track and runs before this
    # fallback. If a local file is needed after a restart, restore only from
    # the Telegram archive channel; MongoDB must never contain music bytes.
    if not is_synthetic:
        try:
            from utils.telegram_archive import restore_archived_file
            from melody import bot as _bot
            restored = await restore_archived_file(_bot, video_id, video=not audio_only)
            if restored and os.path.isfile(restored) and os.path.getsize(restored) > 0:
                LOGGER.info("#download telegram-archive-hit %s variant=%s", video_id, tag)
                return restored
        except Exception as exc:
            LOGGER.debug("telegram archive restore skipped for %s: %s", video_id, exc)
    # Cache check — reuse if already downloaded IN THE SAME VARIANT (audio
    # vs video). Never reuse a file downloaded for the other variant.
    cached = _complete_cache_files(video_id, tag)
    if cached:
        LOGGER.info(
            "#download cache-hit %s variant=%s wait=%.2fs",
            video_id, tag, _time_mod.monotonic() - _download_requested_at,
        )
        return cached[0]

    # ⚡ Tagged media that is still downloading but already playable — checked
    # before any network lookup so the tagged file never waits on YouTube.
    if is_synthetic:
        early = tg_early_path(video_id, audio_only)
        if early:
            LOGGER.info("⚡ using in-progress tagged media file for %s", video_id)
            return early

    # Synthetic Telegram-media ids have no YouTube source to download from —
    # if the cache miss here means download_replied_media() didn't save the
    # file (or /tmp was cleared), there is nothing yt-dlp can fetch. Fail
    # clearly instead of constructing an invalid YouTube URL.
    if is_synthetic:
        # ⚡ A tagged-media download handed off early is still running — play
        # the growing file instead of waiting for (or re-doing) the transfer.
        early = tg_early_path(video_id, audio_only)
        if early:
            LOGGER.info("⚡ using in-progress tagged media file for %s", video_id)
            return early
        inflight = _TG_INFLIGHT.get(f"{video_id}:{tag}")
        if inflight is not None and not inflight.done():
            try:
                done_path = await asyncio.shield(inflight)
                if done_path and os.path.exists(done_path):
                    return done_path
            except Exception as exc:
                LOGGER.warning("tagged-media in-flight download failed for %s: %s", video_id, exc)
        # ROOT-CAUSE FIX ("_stream_track failed ... cached Telegram media not
        # found (re-tag the message)"): /tmp is volatile — Heroku clears it on
        # every restart, and a long queue can outlive the downloaded file. But
        # the tagged message itself still exists on Telegram, so instead of
        # dead-ending playback, re-download it from the remembered source.
        source = TG_MEDIA_SOURCES.get(video_id) or _parse_synthetic_media_id(video_id)
        if source:
            chat_id, message_id = source
            try:
                from melody import bot as _bot
                if _bot is not None:
                    msg = await _bot.get_messages(chat_id, message_id)
                    info = await download_replied_media(_bot, msg, video=not audio_only)
                    if info:
                        proxy_url = info.get("stream_url") or ""
                        if proxy_url.startswith(("http://127.0.0.1:", "http://localhost:")):
                            LOGGER.info("♻️ Re-created tagged media range proxy for %s", video_id)
                            return proxy_url
                        found = _complete_cache_files(video_id, tag)
                        if found:
                            LOGGER.info("♻️ Re-downloaded tagged media for %s", video_id)
                            return found[0]
            except Exception as exc:
                LOGGER.warning("tagged-media re-download failed for %s: %s", video_id, exc)
        raise FileNotFoundError(
            f"download_audio: cached Telegram media for {video_id!r} not found "
            "(the tagged message file is no longer in /tmp — re-tag the message)"
        )

    # In-flight dedup: if a download for this exact (video_id, variant) is
    # already running, share its complete result instead of starting a
    # duplicate. An early caller may additionally receive the validated audio
    # prefix, but the shared future never resolves until the full file exists.
    dedup_key = f"{video_id}:{tag}"
    async with _download_futures_lock:
        existing = _download_futures.get(dedup_key)
        state = _download_early_states.get(dedup_key)
        if existing is None or existing.done():
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            fut.add_done_callback(_consume_download_future)
            _download_futures[dedup_key] = fut
            state = _EarlyDownloadState() if (allow_early or audio_only) else None
            if state is not None:
                _download_early_states[dedup_key] = state
            existing = None
        else:
            fut = existing
    if existing is not None:
        LOGGER.debug("⚡ download_audio dedup: reusing in-flight download for %s", dedup_key)
        if allow_early and state is not None:
            ready_task = asyncio.create_task(state.ready.wait())
            future_task = asyncio.ensure_future(asyncio.shield(fut))
            future_task.add_done_callback(_consume_download_future)
            try:
                done, _ = await asyncio.wait(
                    {ready_task, future_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if state.path and os.path.exists(state.path):
                    return state.path
            finally:
                for task in (ready_task, future_task):
                    if not task.done():
                        task.cancel()
        return await asyncio.shield(fut)

    cancel_event = threading.Event()
    _download_cancel_events[dedup_key] = (cancel_event, owner, int(priority))
    job = asyncio.create_task(
        _run_download_job(
            video_id, audio_only, tag, priority, _download_requested_at,
            cancel_event, fut, state,
        ),
        name=f"download-job:{video_id[:8]}",
    )
    _download_jobs.add(job)
    job.add_done_callback(_download_jobs.discard)
    job.add_done_callback(_consume_download_future)
    if allow_early and state is not None:
        ready_task = asyncio.create_task(state.ready.wait())
        future_task = asyncio.ensure_future(asyncio.shield(fut))
        future_task.add_done_callback(_consume_download_future)
        try:
            done, _ = await asyncio.wait(
                {ready_task, future_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if state.path and os.path.exists(state.path):
                return state.path
        finally:
            for task in (ready_task, future_task):
                if not task.done():
                    task.cancel()
    return await asyncio.shield(fut)


async def wait_for_download(video_id: str, audio_only: bool = True,
                            timeout: float | None = None) -> str | None:
    """Wait for an existing download to finish, never returning a partial path."""
    key = f"{video_id}:{_cache_tag(audio_only)}"
    async with _download_futures_lock:
        future = _download_futures.get(key)
    if future is None:
        found = _complete_cache_files(video_id, _cache_tag(audio_only))
        return found[0] if found else None
    waiter = asyncio.shield(future)
    if timeout is not None:
        return await asyncio.wait_for(waiter, timeout=max(0.1, timeout))
    return await waiter


async def wait_for_early_download(video_id: str, audio_only: bool = True,
                                  timeout: float | None = None) -> str | None:
    """Return a validated growing audio path as soon as one is available.

    ``download_audio(..., allow_early=True)`` normally hides this state behind
    its own task.  The stream race needs a direct waiter, however: the full
    download future may take tens of seconds while a safe WebM/Opus prefix is
    already playable.  Never return a partial path for video or for a
    downloader that did not opt into early handoff.
    """
    key = f"{video_id}:{_cache_tag(audio_only)}"
    async with _download_futures_lock:
        future = _download_futures.get(key)
        state = _download_early_states.get(key)
    if future is None:
        found = _complete_cache_files(video_id, _cache_tag(audio_only))
        return found[0] if found else None

    if state is None:
        waiter = asyncio.shield(future)
    else:
        ready = asyncio.create_task(state.ready.wait())
        completed = asyncio.ensure_future(asyncio.shield(future))
        ready.add_done_callback(_consume_download_future)
        completed.add_done_callback(_consume_download_future)
        waiter = asyncio.create_task(
            _first_download_path(ready, completed, state, audio_only)
        )
    try:
        if timeout is not None:
            return await asyncio.wait_for(waiter, timeout=max(0.1, timeout))
        return await waiter
    finally:
        if state is not None and not waiter.done():
            waiter.cancel()


async def _first_download_path(ready, completed, state, audio_only: bool):
    """Resolve an early state without exposing its internal future to callers."""
    try:
        done, _ = await asyncio.wait({ready, completed}, return_when=asyncio.FIRST_COMPLETED)
        if state.path and os.path.exists(state.path):
            return state.path
        if completed in done:
            path = completed.result()
            return path if path and os.path.exists(path) else None
        # The ready event can be set during cleanup without a usable path.
        path = await completed
        return path if path and os.path.exists(path) else None
    finally:
        for task in (ready, completed):
            if not task.done():
                task.cancel()


def is_download_inflight(video_id: str, audio_only: bool = True) -> bool:
    return f"{video_id}:{_cache_tag(audio_only)}" in _download_futures


class _LoopEvent(threading.Event):
    """A threading.Event that also wakes an asyncio.Event on the main loop.

    Lets a worker thread signal the event loop without any coroutine having
    to block a pool thread on `Event.wait()`.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, aio_event: asyncio.Event):
        super().__init__()
        self._loop = loop
        self._aio_event = aio_event

    def set(self) -> None:
        super().set()
        try:
            self._loop.call_soon_threadsafe(self._aio_event.set)
        except RuntimeError:  # loop already closed
            pass


async def _run_download_job(
    video_id: str, audio_only: bool, tag: str, priority: int,
    requested_at: float, cancel_event: threading.Event,
    future: asyncio.Future, early_state: _EarlyDownloadState | None,
) -> None:
    dedup_key = f"{video_id}:{tag}"
    try:
        result = await _download_audio_impl(
            video_id, audio_only, tag, priority=priority,
            requested_at=requested_at, cancel_event=cancel_event,
            early_state=early_state,
        )
        if not future.done():
            future.set_result(result)
    except BaseException as exc:
        if not future.done():
            if isinstance(exc, asyncio.CancelledError):
                future.cancel()
            else:
                future.set_exception(exc)
    finally:
        _download_cancel_events.pop(dedup_key, None)
        async with _download_futures_lock:
            if _download_futures.get(dedup_key) is future:
                _download_futures.pop(dedup_key, None)
            _download_early_states.pop(dedup_key, None)


async def _download_audio_impl(
    video_id: str, audio_only: bool, tag: str, priority: int = 0,
    requested_at: float | None = None,
    cancel_event: "threading.Event | None" = None,
    early_state: _EarlyDownloadState | None = None,
) -> str:
    """Actual download implementation — called by download_audio() after
    dedup/cache checks. Returns a path to the (possibly still-growing) file.

    R14 FIX: the whole body runs under `_download_semaphore()`, so at most
    MAX_CONCURRENT_DOWNLOADS yt-dlp working sets are alive in this process at
    any moment. Without it every simultaneous /play added its own extractor
    state + buffers and the dyno went from 102% to 157% of quota in seconds.
    """
    gate = _download_semaphore()
    request_started_at = requested_at if requested_at is not None else _time_mod.monotonic()
    await gate.acquire(priority)
    slot_started_at = _time_mod.monotonic()
    LOGGER.info(
        "#download start %s variant=%s priority=%d slot_wait=%.2fs",
        video_id, tag, priority, slot_started_at - request_started_at,
    )
    try:
        if cancel_event is not None and cancel_event.is_set():
            # A low-priority waiter may have been preempted while queued. This
            # check is inside the balanced try/finally so release() is safe.
            raise _DownloadCancelled("superseded by a newer playback request")
        result = await _download_audio_locked(
            video_id, audio_only, tag, cancel_event=cancel_event,
            early_state=early_state,
        )
        LOGGER.info(
            "#download complete %s variant=%s elapsed=%.2fs total=%.2fs",
            video_id, tag, _time_mod.monotonic() - slot_started_at,
            _time_mod.monotonic() - request_started_at,
        )
        return result
    finally:
        await gate.release()


async def _download_audio_locked(
    video_id: str, audio_only: bool, tag: str,
    cancel_event: "threading.Event | None" = None,
    early_state: _EarlyDownloadState | None = None,
) -> str:
    loop = asyncio.get_running_loop()
    early_holder: dict = {}

    # SPEED FIX: this used to be
    #     await loop.run_in_executor(None, early_event.wait, TIMEOUT)
    #     await loop.run_in_executor(None, dl_thread.join)
    # which PARKED a shared executor thread for the entire download. A few
    # concurrent /play commands drained the default pool, and every other
    # group's search/resolve then waited in the executor queue — that queue
    # wait was the 10-15 seconds users were seeing. _LoopEvent lets the
    # download thread wake the event loop directly, so waiting now costs
    # zero threads and the delay does not scale with the number of groups.
    early_async = asyncio.Event()
    done_async = asyncio.Event()
    early_event = _LoopEvent(loop, early_async)

    def _run_download():
        try:
            _download_audio_sync(
                video_id, audio_only,
                early_event=early_event if (
                    early_state is not None and _early_handoff_allowed(audio_only)
                ) else None,
                early_holder=early_holder,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            early_holder["error"] = exc
            early_event.set()
        finally:
            try:
                loop.call_soon_threadsafe(done_async.set)
            except RuntimeError:
                pass

    dl_thread = threading.Thread(
        target=_run_download, daemon=True, name=f"melody-early-dl-{video_id[:8]}",
    )
    dl_thread.start()

    if early_state is not None and _early_handoff_allowed(audio_only):
        early_task = asyncio.ensure_future(early_async.wait())
        done_task = asyncio.ensure_future(done_async.wait())
        try:
            await asyncio.wait(
                {early_task, done_task}, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (early_task, done_task):
                if not t.done():
                    t.cancel()
        path = early_holder.get("early_path")
        if path and os.path.exists(path):
            early_state.path = path
            early_state.ready.set()

    # The worker always waits for the full download before resolving the
    # shared future. Only the explicitly allowed playback caller sees the
    # prefix; cache/persistence callers can therefore never receive a partial.
    await done_async.wait()
    if "error" in early_holder:
        raise early_holder["error"]
    path = early_holder.get("final_path")
    if path and os.path.exists(path) and not path.endswith(".part"):
        return path
    found = _complete_cache_files(video_id, tag)
    if found:
        return found[0]
    raise FileNotFoundError(f"Downloaded file not found for video_id={video_id}")


# ─────────────────────────────────────────────────────────────────────────────
#  Autoplay helpers
# ─────────────────────────────────────────────────────────────────────────────

def _innertube_related_sync(video_id: str, exclude: set) -> list[dict]:
    """AutoPlay fallback: fetch YouTube's "up next" related videos via the
    InnerTube API directly (ANDROID client) — same bot-detection bypass
    trick as `_innertube_search_sync()`.

    ROOT-CAUSE FIX ("autoplay on hai phir bhi kaam nahi karta"):
    `get_related_videos()` used to rely ONLY on yt-dlp's mix/Radio playlist
    extraction. Unlike `get_video_info()` (which has a 3-way yt-dlp ->
    InnerTube -> Invidious fallback chain), it had NO fallback at all — any
    yt-dlp failure (bot-detection block, empty mix, transient error) made
    `_pick_related_track()` return None, `try_autoplay()` return False, and
    `_play_next()` silently leave the call. AutoPlay looked "on" (the DB
    flag was set correctly) but nothing ever actually played. This mirrors
    that same fallback pattern for the related-videos lookup specifically.
    """
    import json

    _ANDROID_KEY = "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w"
    _WEB_KEY = "AIzaSyAO_FJ2SlqU8Q4STEhlGCjw-FfWAH9IrJg"
    _NEXT_URL = "https://www.youtube.com/youtubei/v1/next"

    _CLIENT_CONTEXTS = [
        ("WEB", _WEB_KEY, "2.20260801.00.00",
         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        ("ANDROID", _ANDROID_KEY, "20.10.38",
         "com.google.android.youtube/20.10.38 (Linux; U; Android 14) gzip"),
        ("IOS", _ANDROID_KEY, "20.10.4",
         "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X)"),
        ("ANDROID_MUSIC", _ANDROID_KEY, "7.16.51",
         "com.google.android.apps.youtube.music/7.16.51 (Linux; U; Android 14)"),
        ("IOS_MUSIC", _ANDROID_KEY, "8.12.2",
         "com.google.ios.youtubemusic/8.12.2 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X)"),
    ]

    data = None
    for client_name, api_key, client_ver, ua in _CLIENT_CONTEXTS:
        url = f"{_NEXT_URL}?key={api_key}&prettyPrint=false"
        client_ctx = {"clientName": client_name, "clientVersion": client_ver,
                      "hl": "en", "gl": "US", "utcOffsetMinutes": 0}
        if client_name == "ANDROID":
            client_ctx["androidSdkVersion"] = 30
        elif client_name == "IOS":
            client_ctx["deviceMake"] = "Apple"
            client_ctx["deviceModel"] = "iPhone16,2"
        payload = json.dumps({"context": {"client": client_ctx}, "videoId": video_id}).encode("utf-8")
        try:
            client = get_http_sync_client()
            resp = client.post(
                url, content=payload,
                headers={"Content-Type": "application/json", "User-Agent": ua},
                timeout=3.0,
            )
            if resp.status_code != 200:
                LOGGER.warning(
                    "InnerTube %s related HTTP %s for %s", client_name,
                    resp.status_code, video_id,
                )
                continue
            data = json.loads(resp.text)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("InnerTube %s related error: %s", client_name, exc)
            continue
        if data:
            break

    if not data:
        LOGGER.warning("InnerTube related: all client contexts failed for %s", video_id)
        return []

    def _extract_video_id(renderer):
        """Extract videoId from any renderer type — YouTube nests it
        differently depending on the renderer and client version."""
        # compactVideoRenderer / videoRenderer: videoId is a direct key
        vid = renderer.get("videoId")
        if vid:
            return vid
        # playlistPanelVideoRenderer: videoId is nested inside
        # navigationEndpoint.watchEndpoint.videoId
        nav = renderer.get("navigationEndpoint", {})
        if isinstance(nav, dict):
            we = nav.get("watchEndpoint")
            if isinstance(we, dict) and we.get("videoId"):
                return we["videoId"]
            # Sometimes nested deeper: watchEndpoint.videoId
            we2 = nav.get("watchNextEndpoint", {})
            if isinstance(we2, dict) and we2.get("videoId"):
                return we2["videoId"]
        # lockupViewModel (newer YouTube layout): contentId holds the video id
        cid = renderer.get("contentId")
        if cid:
            return cid
        return ""

    def _walk(node, out):
        """Recursively find every video-bearing renderer in the response.

        YouTube's /next endpoint nests related/autoplay videos inside
        playlistPanelVideoRenderer nodes (not compactVideoRenderer, which
        is used by /search).  Newer layouts also use lockupViewModel and
        gridVideoRenderer.  The exact nesting path varies by client and
        layout version, so walking the whole tree is far more resilient
        than one fixed path.
        """
        if isinstance(node, dict):
            renderer = (
                node.get("playlistPanelVideoRenderer")
                or node.get("compactVideoRenderer")
                or node.get("videoRenderer")
                or node.get("gridVideoRenderer")
            )
            if renderer and _extract_video_id(renderer):
                out.append(renderer)
            # lockupViewModel wraps the video inside a contentId field
            lockup = node.get("lockupViewModel")
            if lockup and _extract_video_id(lockup):
                out.append(lockup)
            for v in node.values():
                _walk(v, out)
        elif isinstance(node, list):
            for item in node:
                _walk(item, out)

    renderers: list = []
    try:
        _walk(data, renderers)
    except Exception as exc:
        LOGGER.warning("InnerTube related parse error: %s", exc)
        return []

    results = []
    seen = set()
    for r in renderers:
        vid = _extract_video_id(r)
        if not vid or vid in exclude or vid in seen or vid == video_id:
            continue
        seen.add(vid)
        title = (
            r.get("title", {}).get("simpleText")
            or (r.get("title", {}).get("runs", [{}])[0].get("text"))
            or r.get("metadata", {}).get("lockupMetadataRenderer", {}).get("title", {}).get("runs", [{}])[0].get("text")
            or "Unknown"
        )
        length_text = (
            r.get("lengthText", {}).get("simpleText", "")
            or (r.get("lengthText", {}).get("runs", [{}])[0].get("text", "") if r.get("lengthText") else "")
        )
        duration = 0
        if length_text:
            parts = [int(p) for p in length_text.split(":") if p.isdigit()]
            if len(parts) == 2:
                duration = parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                duration = parts[0] * 3600 + parts[1] * 60 + parts[2]
        thumbs = (r.get("thumbnail", {}).get("thumbnails") or [])
        thumbnail = thumbs[-1].get("url", "") if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        byline = (
            r.get("longBylineText", {}).get("runs", [{}])[0].get("text")
            or r.get("shortBylineText", {}).get("runs", [{}])[0].get("text")
            or r.get("longBylineText", {}).get("simpleText")
            or r.get("shortBylineText", {}).get("simpleText")
            or "Unknown"
        )
        results.append({
            "id": vid,
            "title": title,
            "duration": duration,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumbnail": thumbnail,
            "uploader": byline,
        })
        if len(results) >= 10:
            break

    if results:
        LOGGER.info("✅ InnerTube related fallback OK: %d candidates for %s", len(results), video_id)
    else:
        LOGGER.warning("❌ InnerTube related fallback: no candidates for %s", video_id)
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  Single-video metadata by ID (used to enrich AutoPlay picks)
#  Flat "up next" playlist entries from yt-dlp usually carry only an id and a
#  title — no duration, no uploader — which is why AutoPlay cards showed
#  "Unknown / 00:00". This fills those gaps from InnerTube's /player endpoint
#  (videoDetails: title, author, lengthSeconds, thumbnails).
# ─────────────────────────────────────────────────────────────────────────────

def _innertube_player_sync(video_id: str) -> "dict | None":
    import json

    _PLAYER_URL = "https://www.youtube.com/youtubei/v1/player"

    # ROOT-CAUSE FIX (Sep 18 2026): ANDROID_VR has been 403'd entirely since
    # Aug 17 2026. VISIONOS is the new keyless client that still answers with
    # full videoDetails and needs no PO token, no JS player.
    _CLIENT_CONTEXTS = [
        ("VISIONOS", "1.02",
         "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"),
        ("WEB", "2.20260801.00.00",
         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    ]

    for client_name, client_ver, ua in _CLIENT_CONTEXTS:
        client_ctx = {"clientName": client_name, "clientVersion": client_ver,
                      "hl": "en", "gl": "US", "utcOffsetMinutes": 0}
        if client_name.startswith("ANDROID"):
            client_ctx["androidSdkVersion"] = 32
        elif client_name == "VISIONOS":
            client_ctx["deviceMake"] = "Apple"
            client_ctx["deviceModel"] = "RealityDevice17,1"
            client_ctx["osName"] = "visionOS"
            client_ctx["osVersion"] = "26.5.23O471"
        payload = json.dumps({
            "context": {"client": client_ctx},
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
        }).encode("utf-8")
        try:
            resp = get_http_sync_client().post(
                f"{_PLAYER_URL}?prettyPrint=false",
                content=payload,
                headers={"Content-Type": "application/json", "User-Agent": ua},
                timeout=8.0,
            )
            if resp.status_code != 200:
                continue
            details = (json.loads(resp.text) or {}).get("videoDetails") or {}
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("InnerTube %s player error for %s: %s", client_name, video_id, exc)
            continue

        if not details.get("videoId"):
            continue
        thumbs = (details.get("thumbnail", {}) or {}).get("thumbnails") or []
        return {
            "id": details.get("videoId", video_id),
            "title": details.get("title") or "Unknown",
            "duration": int(details.get("lengthSeconds") or 0),
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": (thumbs[-1].get("url") if thumbs
                          else f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"),
            "uploader": details.get("author") or "Unknown",
        }
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  InnerTube stream resolution — the fast path to "song starts NOW"
#
#  ROOT-CAUSE FIX ("play/vplay bohot time leta hai"): resolving playable CDN
#  URLs used to mean a SECOND full yt-dlp extraction after the search, which
#  costs 2-5s on Heroku (watch-page fetch, player JS, format probing). YouTube's
#  own InnerTube /player endpoint returns the exact same streamingData in a
#  single ~300ms HTTP POST. We race it against yt-dlp and take whichever comes
#  back first with a usable format, so playback starts seconds earlier while
#  keeping yt-dlp as the safety net.
# ─────────────────────────────────────────────────────────────────────────────

# Per-client InnerTube timeout. Kept BELOW the overall resolve budget so a
# single dead client can never push /play into the slow download fallback.
try:
    _INNERTUBE_TIMEOUT = float(os.getenv("INNERTUBE_TIMEOUT", "3"))
except Exception:  # noqa: BLE001
    _INNERTUBE_TIMEOUT = 3.0

_IT_WEB_VERSION = "2.20250606.01.00"
_IT_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_CLIENT_IDS = {
    "WEB_REMIX": "67", "TVHTML5_SIMPLY_EMBEDDED_PLAYER": "85",
    "IOS": "5", "IOS_MUSIC": "26", "ANDROID_MUSIC": "21",
    "ANDROID": "3", "ANDROID_VR": "28", "VISIONOS": "101",
    "TVHTML5": "7", "MWEB": "2", "WEB": "1", "WEB_EMBEDDED_PLAYER": "56",
}


# ── Authenticated InnerTube session ─────────────────────────────────────────
# SPEED ROOT CAUSE (Sep 10 log): every InnerTube player probe answered
# LOGIN_REQUIRED, so the fan-out was muted and EVERY /play paid the heavy
# cookie yt-dlp resolve (5.4-7.2s of the 6-8s "gana late" wait).
# The web-family clients only accept a datacenter request when it carries the
# full browser session, not just raw cookies:
#   • Authorization: SAPISIDHASH <ts>_<sha1(ts SAPISID origin)>
#   • X-Goog-Visitor-Id / context.client.visitorData
#   • serviceIntegrityDimensions.poToken from the warm local bgutil server
# With those attached the player call answers OK in ~300ms and hands back a
# directly playable HLS/CDN URL — no yt-dlp, no player JS, no Deno.
_IT_SESSION_TTL = 1800.0
_it_session: dict = {}
_it_session_lock = threading.Lock()


def _innertube_cookie_map() -> dict:
    """Netscape cookie jar -> {name: value} for youtube.com cookies."""
    out: dict = {}
    if not _COOKIE_TEXT:
        return out
    for line in _COOKIE_TEXT.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and "youtube.com" in parts[0]:
            out[parts[5]] = parts[6]
    return out


def _sapisid_hash(origin: str = "https://www.youtube.com") -> str:
    """`Authorization: SAPISIDHASH ...` value for the stored session (or "")."""
    import hashlib

    jar = _innertube_cookie_map()
    sapisid = (
        jar.get("__Secure-3PAPISID")
        or jar.get("SAPISID")
        or jar.get("__Secure-1PAPISID")
        or ""
    )
    if not sapisid:
        return ""
    ts = str(int(time.time()))
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def _fetch_visitor_data() -> str:
    """One cheap InnerTube call for a fresh visitorData token (or "")."""
    import json

    try:
        resp = get_http_sync_client().post(
            "https://www.youtube.com/youtubei/v1/visitor_id?prettyPrint=false",
            content=json.dumps(
                {
                    "context": {
                        "client": {
                            "clientName": "WEB",
                            "clientVersion": _IT_WEB_VERSION,
                            "hl": "en",
                            "gl": "US",
                        }
                    }
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": _IT_WEB_UA,
                     "Origin": "https://www.youtube.com",
                     "X-Youtube-Client-Name": "1",
                     "X-Youtube-Client-Version": _IT_WEB_VERSION},
            timeout=_INNERTUBE_TIMEOUT,
        )
        if resp.status_code != 200:
            return ""
        data = json.loads(resp.text) or {}
        return ((data.get("responseContext") or {}).get("visitorData") or "")
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("visitorData fetch failed: %s", exc)
        return ""


def _fetch_po_token(visitor_data: str) -> str:
    """Session-bound PO token from the warm local bgutil server (or "")."""
    import json

    if not visitor_data or not _bgutil_http_alive():
        return ""
    try:
        resp = get_http_sync_client().post(
            f"http://127.0.0.1:{_BGUTIL_HTTP_PORT}/get_pot",
            content=json.dumps({"content_binding": visitor_data}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=_env_float("BGUTIL_POT_TIMEOUT", 4.0),
        )
        if resp.status_code != 200:
            return ""
        data = json.loads(resp.text) or {}
        return data.get("poToken") or data.get("po_token") or ""
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("bgutil PO token fetch failed: %s", exc)
        return ""


def _innertube_session(refresh: bool = False) -> dict:
    """Cached {"visitor": ..., "pot": ...} used by the web-family probes.

    Both values are session-scoped, so they are fetched ONCE and reused for
    every following track — the fast path stays a single ~300ms player call.
    """
    now = time.monotonic()
    with _it_session_lock:
        cached = _it_session.get("data")
        if cached and not refresh and _it_session.get("at", 0) + _IT_SESSION_TTL > now:
            return cached
        visitor = _fetch_visitor_data()
        pot = _fetch_po_token(visitor) if visitor else ""
        data = {"visitor": visitor, "pot": pot}
        _it_session["data"] = data
        _it_session["at"] = now
        if visitor:
            LOGGER.info(
                "🔐 InnerTube session ready (visitorData ok, PO token %s)",
                "ok" if pot else "unavailable",
            )
        return data


def _innertube_cookie_header() -> str:
    """Netscape cookie jar -> `Cookie:` header value (empty when unavailable).

    The web-family InnerTube clients answer LOGIN_REQUIRED from datacenter
    IPs unless a real session is attached; YT_COOKIES is that session.
    """
    if not _COOKIE_TEXT:
        return ""
    pairs = []
    for line in _COOKIE_TEXT.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and "youtube.com" in parts[0]:
            pairs.append(f"{parts[5]}={parts[6]}")
    return "; ".join(pairs)


def _innertube_streams_sync(video_id: str, want_video: bool = False) -> "dict | None":
    """Fetch streamingData via InnerTube and shape it like a yt-dlp info dict.

    SPEED / RELIABILITY FIX (Heroku log: repeated
    "innertube: no directly streamable format" -> "falling back to download",
    which is what turned /play into a ~20s wait):

      • The ANDROID client now almost always answers with *ciphered* formats
        unless a PO token is attached, so the old two-client list failed for
        most videos and every /play silently fell back to a full download.
        IOS and the embedded TVHTML5 client still hand out plain, unciphered
        CDN URLs, so they are queried too.
      • The clients are probed CONCURRENTLY instead of one-after-another.
        Previously a dead client burned its full 6s timeout before the next
        one was even tried; now the first client with a usable format wins and
        the rest are dropped.
    """
    import json
    from concurrent.futures import as_completed

    _CLIENTS = [
        # (name, client version, user agent, extra context)
        # ORDER MATTERS: probes run concurrently but this is also the
        # preference order. Verified with a live probe (Aug 2026): IOS is the
        # only client that still hands out *unciphered*, PO-token-free CDN
        # URLs for ordinary music videos. ANDROID_VR / TVHTML5 answer
        # LOGIN_REQUIRED and MWEB / WEB answer UNPLAYABLE from a cloud IP —
        # which is exactly why every /play logged "innertube: no directly
        # streamable format" and fell back to a FULL DOWNLOAD (the 10-20s
        # wait users compared against other bots).
        ("IOS", "20.10.4",
         "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X)",
         {"deviceMake": "Apple", "deviceModel": "iPhone16,2",
          "osName": "iPhone", "osVersion": "18.3.2.22D82"}),
        ("IOS_MUSIC", "8.12.2",
         "com.google.ios.youtubemusic/8.12.2 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X)",
         {"deviceMake": "Apple", "deviceModel": "iPhone16,2",
          "osName": "iPhone", "osVersion": "18.3.2.22D82"}),
        ("ANDROID_MUSIC", "7.16.51",
         "com.google.android.apps.youtube.music/7.16.51 (Linux; U; Android 14)",
         {"androidSdkVersion": 34}),
        ("ANDROID", "20.10.38",
         "com.google.android.youtube/20.10.38 (Linux; U; Android 14)",
         {"androidSdkVersion": 34}),
        ("VISIONOS", "1.02",
         "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Version/26.0 Safari/605.1.15",
         {"deviceMake": "Apple", "deviceModel": "RealityDevice17,1",
          "osName": "visionOS", "osVersion": "26.5.23O471"}),
        # Web-family clients only help when YT_COOKIES is configured (the
        # session cookie is what lifts LOGIN_REQUIRED on datacenter IPs), so
        # they are skipped otherwise instead of burning a request + timeout
        # slot on the critical path.
        ("TVHTML5", "7.20250605",
         "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.5) AppleWebKit/537.36 "
         "(KHTML, like Gecko) 85.0.4183.93/6.5 TV Safari/537.36",
         {}),
        ("MWEB", "2.20250605.01.00",
         "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
         {}),
    ]

    _WEB_FAMILY = ("TVHTML5", "MWEB", "WEB", "WEB_EMBEDDED_PLAYER",
                   "WEB_REMIX", "TVHTML5_SIMPLY_EMBEDDED_PLAYER")
    cookie_header = _innertube_cookie_header()

    # SPEED FIX — LIVE-VERIFIED from a datacenter IP (Sep 10 2026), the exact
    # environment this bot runs in:
    #   IOS      -> OK in 0.17s, 23 unciphered googlevideo URLs (206 on fetch)
    #   ANDROID  -> OK in 0.12s, 26 unciphered URLs (206 on fetch)
    #   WEB / WEB_REMIX          -> UNPLAYABLE ("Video unavailable")
    #   MWEB                     -> UNPLAYABLE ("The page needs to be reloaded")
    #   TVHTML5 / ANDROID_VR     -> LOGIN_REQUIRED ("confirm you're not a bot")
    #   *_EMBEDDED_PLAYER        -> ERROR ("no longer supported in this app")
    # So the web family cannot win, it can only cost time: in the Sep 10 log
    # four dead web probes ran first and /play still paid the 4.8s yt-dlp
    # resolve. They are off the critical path now (opt back in with
    # INNERTUBE_WEB_CLIENTS=true), and the cookie-less mobile clients — which
    # need no visitorData, no PO token and no session round-trips — go first.
    _MOBILE_ORDER = ("IOS", "ANDROID", "ANDROID_MUSIC", "IOS_MUSIC", "VISIONOS")
    _by_name = {c[0]: c for c in _CLIENTS}

    session = {"visitor": "", "pot": ""}
    if cookie_header:
        # SPEED FIX (Sep 11 production logs): all five anonymous mobile
        # clients answer LOGIN_REQUIRED on this host.  A real YT_COOKIES
        # session can ask WEB directly and often receives its HLS manifest in
        # one player call (~300 ms), before the heavier yt-dlp fallback starts.
        # Probe only WEB here: launching mobile and WEB_REMIX requests too
        # merely competes for the small worker pool and repeats known failures.
        # Sep 20 fix: if WEB is per-client muted, fall through to mobile clients
        # instead of returning an empty list (which killed the fast path for 300s).
        if _innertube_client_muted("WEB"):
            session = {"visitor": "", "pot": ""}
            _CLIENTS = [_by_name[n] for n in _MOBILE_ORDER if n in _by_name and not _innertube_client_muted(n)]
        else:
            session = _innertube_session()
            _CLIENTS = [("WEB", _IT_WEB_VERSION, _IT_WEB_UA, {})]
    else:
        _CLIENTS = [_by_name[n] for n in _MOBILE_ORDER if n in _by_name and not _innertube_client_muted(n)]

    def _probe(entry):
        client_name, client_ver, ua, extra = entry
        ctx = {"clientName": client_name, "clientVersion": client_ver,
               "hl": "en", "gl": "US", "utcOffsetMinutes": 0, **extra}
        body = {
            "context": {"client": ctx},
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
        }
        if client_name.startswith("TVHTML5") or client_name == "WEB_EMBEDDED_PLAYER":
            body["context"]["thirdParty"] = {"embedUrl": "https://www.youtube.com/"}
        is_web = client_name in _WEB_FAMILY
        if is_web and session.get("visitor"):
            ctx["visitorData"] = session["visitor"]
            if session.get("pot"):
                body["serviceIntegrityDimensions"] = {"poToken": session["pot"]}
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": ua,
                   "X-Youtube-Client-Name": _CLIENT_IDS.get(client_name, "1"),
                   "X-Youtube-Client-Version": client_ver,
                   "Origin": "https://www.youtube.com"}
        if is_web and session.get("visitor"):
            headers["X-Goog-Visitor-Id"] = session["visitor"]
            headers["X-Origin"] = "https://www.youtube.com"
            auth = _sapisid_hash()
            if auth:
                # Without SAPISIDHASH the web clients answer LOGIN_REQUIRED
                # from a cloud IP even when the cookies are perfectly valid.
                headers["Authorization"] = auth
                headers["X-Goog-AuthUser"] = "0"
        # Cookies only help (and are only accepted) for the web-family
        # clients; sending a web session to the IOS client makes YouTube
        # answer LOGIN_REQUIRED instead of streaming formats.
        if cookie_header and client_name in _WEB_FAMILY:
            headers["Cookie"] = cookie_header
        try:
            resp = get_http_sync_client().post(
                "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                content=payload,
                headers=headers,
                # SPEED: this call sits on the critical path to "song starts".
                # 6s was longer than the whole resolve budget, so a hung
                # client could stall playback all by itself.
                timeout=_INNERTUBE_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            data = json.loads(resp.text) or {}
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("InnerTube %s streams error for %s: %s", client_name, video_id, exc)
            return None

        status = ((data.get("playabilityStatus") or {}).get("status") or "").upper()
        if status not in ("OK", "LIVE_STREAM_OFFLINE"):
            # INFO, not DEBUG: when every client answers LOGIN_REQUIRED /
            # UNPLAYABLE the direct path silently dies and every song falls
            # back to a full download. That is exactly the "gana late bajta
            # hai" symptom, and the logs used to show nothing about it.
            LOGGER.info("#stream innertube %s for %s -> %s (%s)", client_name, video_id,
                        status or "?",
                        str((data.get("playabilityStatus") or {}).get("reason") or "")[:60])
            # Per-client mute: only mute THIS client, not all of them
            _note_innertube_client(client_name, False)
            if status == "LOGIN_REQUIRED" and is_web:
                # Refresh visitorData + PO token in the background so the next
                # track gets a working session instead of muting the fast path.
                _it_session.pop("at", None)
            return None

        sd = data.get("streamingData") or {}
        raw = list(sd.get("formats") or []) + list(sd.get("adaptiveFormats") or [])
        formats = []
        for f in raw:
            url = f.get("url")
            # Ciphered formats need the player JS to unscramble — leave those
            # to yt-dlp instead of shipping a URL that 403s mid-song.
            if not url or f.get("signatureCipher") or f.get("cipher"):
                continue
            mime = f.get("mimeType") or ""
            is_video = mime.startswith("video/")
            has_audio = "mp4a" in mime or "opus" in mime or mime.startswith("audio/")
            
            # ⚡ SPEED FIX: Detect DASH vs progressive formats
            # DASH formats have "initRange" or "indexRange" fields
            is_dash = bool(f.get("initRange") or f.get("indexRange"))
            protocol = "dash" if is_dash else "https"
            if is_web and session.get("pot") and "pot=" not in url:
                sep = "&" if "?" in url else "?"
                url = url + sep + "pot=" + session["pot"]
            
            formats.append({
                "url": url,
                "protocol": protocol,
                "vcodec": "avc1" if is_video else "none",
                "acodec": "mp4a" if (has_audio and (not is_video or "," in mime)) else ("none" if is_video else "mp4a"),
                "height": f.get("height") or 0,
                "abr": (f.get("averageBitrate") or f.get("bitrate") or 0) / 1000 if not is_video else 0,
                "tbr": (f.get("bitrate") or 0) / 1000,
            })

        # ⚡ ULTRA SPEED FIX: When no unciphered format exists, use HLS or SABR fallback
        # This prevents "innertube: no directly streamable format" which forced 10s+ download
        if not formats:
            hls = sd.get("hlsManifestUrl")
            if hls:
                # HLS is always directly streamable by ffmpeg without any cipher/PO-token
                formats = [{
                    "url": hls, "protocol": "m3u8_native",
                    "vcodec": "avc1", "acodec": "mp4a",
                    "height": 480, "abr": 128, "tbr": 500,
                }]
            else:
                # Last resort: if we got SABR or ciphered-only formats, 
                # skip this client and try next one (other clients may have unciphered)
                LOGGER.debug("InnerTube %s: no unciphered format for %s, trying next client", 
                           client_name, video_id)
                return None
        _note_innertube_client(client_name, True)
        return {"formats": formats,
                "client": client_name,
                "headers": {
                    "User-Agent": ua,
                    "Referer": "https://www.youtube.com/",
                },
                # Keep the manifest at the top level. _pick_stream_formats()
                # prefers it for audio-only playback when the client exposes
                # only a muxed progressive WEB URL (often itag 18/SABR).
                "hlsManifestUrl": sd.get("hlsManifestUrl"),
                "is_live": bool((data.get("videoDetails") or {}).get("isLiveContent")
                                and not sd.get("formats"))}


    if not _CLIENTS:
        return None
    deadline = time.monotonic() + _INNERTUBE_TIMEOUT + 0.5
    # Reuse the bounded process-wide pool. Creating a fresh executor for every
    # resolve left losing client probes running after the winner returned; a
    # busy bot accumulated those threads and could cross Heroku R14 even with
    # only one yt-dlp download slot. The shared pool also caps InnerTube fan-out
    # together with search/resolve work.
    futures = {YTDL_POOL.submit(_probe, entry): entry[0] for entry in _CLIENTS}
    try:
        try:
            for fut in as_completed(futures, timeout=max(0.1, deadline - time.monotonic())):
                try:
                    result = fut.result()
                except Exception:  # noqa: BLE001
                    continue
                if result:
                    # A successful player response is not necessarily usable
                    # for the requested mode. For example, the first client
                    # may expose only a DASH video/audio pair, which we
                    # deliberately reject for /vplay to avoid A/V drift. Do
                    # not return that response before checking the next client;
                    # another concurrent client may expose a muxed or HLS URL.
                    picked = _pick_stream_formats(result, want_video)
                    if not picked:
                        LOGGER.debug(
                            "InnerTube: %s response has no usable %s format for %s",
                            result["client"], "video" if want_video else "audio", video_id,
                        )
                        continue
                    result["_picked"] = picked
                    LOGGER.debug("InnerTube: %s served %s formats for %s",
                                 result["client"], len(result["formats"]), video_id)
                    for other in futures:
                        other.cancel()
                    return result
        except Exception:  # noqa: BLE001  (as_completed timeout)
            LOGGER.debug("InnerTube: all clients timed out for %s", video_id)
    finally:
        # Losing probes are futures in the shared bounded pool. Cancel queued
        # ones; already-running calls finish under the same global cap.
        for future in futures:
            if not future.done():
                future.cancel()
    return None


def _resolve_stream_urls_innertube(video_id: str, want_video: bool) -> dict:
    """InnerTube-only resolve; raises when nothing usable comes back."""
    info = _innertube_streams_sync(video_id, want_video=want_video)
    picked = (info or {}).get("_picked") or _pick_stream_formats(info or {}, want_video)
    if not picked:
        _note_innertube_stream(False)
        raise ValueError("innertube: no directly streamable format")
    picked["is_live"] = bool((info or {}).get("is_live"))
    picked["headers"] = {
        k: v
        for k, v in ((info or {}).get("headers") or {}).items()
        if k in ("User-Agent", "Referer")
    } or {
        k: v
        for k, v in (_ydl_opts().get("http_headers") or {}).items()
        if k in ("User-Agent", "Referer")
    }
    urls = [u for u in (picked.get("video"), picked.get("audio")) if u]
    picked["expires_at"] = min(_url_expiry(u) for u in urls) - _STREAM_URL_SAFETY_MARGIN
    _note_innertube_stream(True)
    return picked


async def get_video_details(video_id: str) -> "dict | None":
    """Full metadata (title, uploader, duration, thumbnail) for one video ID.

    Cheap + cached; never raises. Used by AutoPlay so its card carries the
    exact same rich details as a manual /play card.
    """
    if not is_valid_video_id(video_id):
        return None

    cache_key = f"vid:{video_id}"
    cached = _meta_cache_get(cache_key)
    if cached:
        return cached

    loop = asyncio.get_running_loop()
    for fn in (_ytapi_details_sync, _innertube_player_sync, _innertube_next_sync):
        if fn is _ytapi_details_sync and not ytapi_enabled():
            continue
        try:
            info = await loop.run_in_executor(YTDL_POOL, fn, video_id)
        except Exception:
            info = None
        norm = _normalize_info(info)
        if norm and (norm.get("title") != "Unknown" or norm.get("duration")):
            _meta_cache_put(cache_key, norm)
            return norm
    return None


# seed video_id → (monotonic_deadline, full candidate list). YouTube's "up
# next" mix for a given song does not change second to second, and the logs
# showed the SAME seed being resolved twice within a second (call.py's
# prefetch and try_autoplay both ask) — each costing a yt-dlp attempt plus an
# InnerTube round-trip. Caching the raw list makes the second lookup free and
# keeps AutoPlay hand-off instant.
_RELATED_CACHE: dict = {}
_RELATED_TTL = 300.0
_RELATED_FAILURE_TTL = 45.0
_RELATED_FAILURES: dict[str, float] = {}


async def get_related_videos(video_id: str, exclude_ids: list[str] = None) -> list[dict]:
    """Fetch YouTube's own "up next" mix for autoplay (skip excluded IDs).

    Order is preserved exactly as YouTube ranks it, so the caller can simply
    take the first non-excluded entry to mirror YouTube's autoplay.
    """
    exclude = set(exclude_ids or [])

    def _filtered(items: list[dict]) -> list[dict]:
        return [i for i in items if i.get("id") and i["id"] not in exclude]

    now = time.monotonic()
    if _RELATED_FAILURES.get(video_id, 0.0) > now:
        return []
    cached = _RELATED_CACHE.get(video_id)
    if cached and cached[0] > time.monotonic():
        hit = _filtered(cached[1])
        if hit:
            return hit

    def _remember(items: list[dict]) -> list[dict]:
        if items:
            _RELATED_CACHE[video_id] = (time.monotonic() + _RELATED_TTL, items)
            if len(_RELATED_CACHE) > 256:
                for key in list(_RELATED_CACHE)[:128]:
                    _RELATED_CACHE.pop(key, None)
        return _filtered(items)

    # SPEED: on this host yt-dlp's RD-mix extraction has been returning nothing
    # on essentially every call (see "yt-dlp related-videos returned nothing —
    # trying InnerTube fallback" in the logs) and only then the InnerTube
    # lookup — which always succeeds — ran. Ask InnerTube first and keep
    # yt-dlp as the fallback, saving that wasted round-trip per pick.
    try:
        loop = asyncio.get_running_loop()
        fast = await loop.run_in_executor(YTDL_POOL, _innertube_related_sync, video_id, set())
        if fast:
            return _remember(fast)
    except Exception as exc:
        LOGGER.info("InnerTube related lookup failed — trying yt-dlp: %s", exc)

    def _related():
        # ROOT-CAUSE FIX: this call only needs lightweight metadata (title,
        # duration, thumbnail) for the autoplay queue — it must never try to
        # resolve a playable *format* for anything. The previous version
        # reused _ydl_opts(), which always sets a strict "format" selector
        # for downloading. Even with extract_flat=True, yt-dlp still fully
        # processes the primary watch-page result before flattening the
        # "up next" mix, so an unavailable format on that primary video
        # (age-restricted, region-locked, or a format list YouTube changed)
        # raised straight out of extract_info() and killed the whole
        # autoplay lookup — that's the "Requested format is not available"
        # crash from the error log.
        #
        # Fix: request YouTube's dedicated "up next" Mix/Radio playlist
        # (list=RD<id>) so entries come back as flat playlist items (never
        # format-resolved), strip the format selector entirely, and ignore
        # per-entry errors so one broken related video can't blank the list.
        url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        opts = {**_ydl_opts(), "extract_flat": True, "playlist_items": "1:10"}
        opts.pop("format", None)
        opts["ignoreerrors"] = True
        opts["skip_download"] = True
        with _locked_ytdl(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("entries") or [] if info else []

    try:
        loop = asyncio.get_running_loop()
        entries = await loop.run_in_executor(YTDL_POOL, _related)
        results = []
        for e in entries:
            if not e:
                continue  # ignoreerrors leaves a None slot for a broken entry
            vid = e.get("id", "")
            if vid and vid not in exclude:
                results.append({
                    "id": vid,
                    "title": e.get("title", "Unknown"),
                    "duration": e.get("duration", 0),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "thumbnail": e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    "uploader": e.get("uploader", "Unknown"),
                })
        if results:
            return _remember(results)
        LOGGER.info("yt-dlp related-videos returned nothing — trying InnerTube fallback | %s", video_id)
    except Exception as exc:
        LOGGER.warning("get_related_videos (yt-dlp) failed — trying InnerTube fallback: %s", exc)

    # yt-dlp's mix/Radio extraction returned nothing (or raised). Do not
    # immediately repeat the same exhausted InnerTube 403 cascade: the first
    # InnerTube attempt already tried every configured client.
    _RELATED_FAILURES[video_id] = time.monotonic() + _RELATED_FAILURE_TTL
    # yt-dlp's mix/Radio extraction returned nothing (or raised) — fall back
    # to a direct InnerTube "up next" lookup so AutoPlay doesn't just go
    # silent (see _innertube_related_sync()'s docstring for the root cause).
    # The first InnerTube attempt above already exhausted all clients. A
    # second identical call only repeats 403s and adds 10-30 seconds to
    # autoplay. Return an empty candidate list and let the next autoplay cycle
    # retry after the short negative TTL.
    return []


# ─────────────────────────────────────────────────────────────────────────────
#  Startup pre-warm: populate metadata cache with trending/popular songs
#  so the first few /play commands are instant cache hits instead of
#  paying a full InnerTube/yt-dlp search round-trip.
# ─────────────────────────────────────────────────────────────────────────────

_POPULAR_QUERIES = [
    "top songs 2026",
    "trending music 2026",
    "popular hindi songs 2026",
    "top english songs 2026",
    "punjabi hits 2026",
]


async def warm_popular_metadata() -> None:
    """Pre-fetch metadata for popular/trending searches at startup so the
    first few /play commands after a restart are instant cache hits."""
    try:
        for q in _POPULAR_QUERIES:
            info = await get_video_info(q)
            if info:
                LOGGER.debug("Pre-warmed metadata: %s → %s", q, info.get("title", "?")[:40])
        LOGGER.info("✅ Popular metadata pre-warm complete (%d queries)", len(_POPULAR_QUERIES))
    except Exception as exc:
        LOGGER.debug("Popular metadata pre-warm skipped: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
#  Direct CDN stream resolution (NO download) — used by /play + /vplay
# ─────────────────────────────────────────────────────────────────────────────
#
# ROOT-CAUSE FIX ("/vplay me audio aur video miss match ho rahi hai" +
# "pura video download mt Krna direct play Krna"):
#
# The old video path went through download_audio(video_id, audio_only=False),
# which handed PyTgCalls a STILL-GROWING `.part` file (the early-handoff speed
# trick). That is safe for a single-stream audio webm, but it is broken by
# design for video:
#
#   1. A /vplay MediaStream spawns TWO independent ffmpeg processes on the same
#      path — one for the camera track, one for the microphone track. Each
#      opens the half-written file at a different moment and therefore at a
#      different length, so the two processes start decoding from different
#      effective positions -> permanent A/V drift (the reported mismatch).
#   2. When the format selector fell back to a DASH pair, yt-dlp writes the
#      video stream FIRST and only muxes the audio in at the very end, so for
#      most of the track the file that is being played has no audio at all (or,
#      after the mux, ffmpeg is reading a file whose byte offsets just moved).
#   3. A 720p file is 10-40x the bytes of an audio stream, so "join VC and hear
#      the song instantly" was impossible — playback waited on a real download.
#
# Fix: don't download at all. Resolve the CDN URLs with yt-dlp
# (download=False) and let ffmpeg pull the bytes it needs, on demand, over
# HTTP — video from the video URL, audio from the audio URL. Each ffmpeg
# process now owns a complete, seekable, immutable source that starts at
# t=0, so the two tracks are frame-aligned from the first frame and stay
# aligned. Resolution is one metadata round-trip (~1s), so /vplay joins the
# call and starts playing immediately instead of after a full download.
#
# googlevideo URLs are signed and short-lived, so the cache honours the `expire`
# query parameter (minus a safety margin) instead of a fixed TTL.

_stream_url_cache: dict = {}
_stream_url_locks: dict = {}
# A cloud-hosted video can consistently expose no usable direct format. Cache
# that negative result briefly so the early warm task, metadata warm task, and
# playback handoff all reach the single download fallback instead of repeating
# the same resolver ladder three times. The short TTL still permits recovery
# from transient YouTube/CDN changes.
_stream_url_failures: dict = {}
_STREAM_URL_FAILURE_TTL = 8.0
_STREAM_URL_FALLBACK_TTL = 1800  # used when the URL carries no `expire`
_STREAM_URL_SAFETY_MARGIN = 300  # re-resolve this long before real expiry
_STREAM_URL_CACHE_MAX = 128


def _prune_stream_url_state(now: "float | None" = None) -> None:
    """Drop expired URLs and orphaned locks; cap process memory on busy bots."""
    now = _time_mod.time() if now is None else now
    expired = [
        key for key, value in _stream_url_cache.items()
        if value.get("expires_at", 0) <= now
    ]
    for key in expired:
        _stream_url_cache.pop(key, None)
        lock = _stream_url_locks.get(key)
        if lock is None or not lock.locked():
            _stream_url_locks.pop(key, None)
    expired_failures = [
        key for key, deadline in _stream_url_failures.items()
        if deadline <= _time_mod.monotonic()
    ]
    for key in expired_failures:
        _stream_url_failures.pop(key, None)

    if len(_stream_url_cache) > _STREAM_URL_CACHE_MAX:
        oldest = sorted(
            _stream_url_cache,
            key=lambda key: _stream_url_cache[key].get("expires_at", 0),
        )[:len(_stream_url_cache) - _STREAM_URL_CACHE_MAX]
        for key in oldest:
            _stream_url_cache.pop(key, None)
            lock = _stream_url_locks.get(key)
            if lock is None or not lock.locked():
                _stream_url_locks.pop(key, None)


def drop_caches() -> int:
    """Emergency cache eviction — called by utils/memguard when RSS nears the
    dyno quota (Heroku `Error R14`).

    These dicts are pure caches: dropping them costs one extra metadata/URL
    resolve later, which is far cheaper than the dyno swapping or being
    killed. Anything holding real playback state (in-flight downloads, held
    locks) is deliberately left alone.
    """
    freed = 0
    for store in (_meta_cache, _RELATED_CACHE, _stream_url_cache, _TG_EARLY_PATHS):
        try:
            freed += len(store)
            store.clear()
        except Exception:  # noqa: BLE001 — a watchdog helper must never raise
            pass
    try:
        freed += len(_stream_url_failures)
        _stream_url_failures.clear()
        for key, lock in list(_stream_url_locks.items()):
            if not lock.locked():
                _stream_url_locks.pop(key, None)
        for key, lock in list(_tg_media_locks.items()):
            if not lock.locked():
                _tg_media_locks.pop(key, None)
    except Exception:  # noqa: BLE001
        pass
    return freed


def _url_expiry(url: str) -> float:
    """Expiry timestamp of a signed CDN URL, from its `expire` parameter."""
    try:
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        for key in ("expire", "expires"):
            if qs.get(key):
                return float(qs[key][0])
    except Exception:
        pass
    return _time_mod.time() + _STREAM_URL_FALLBACK_TTL


def _max_stream_height() -> int:
    """Video height cap for /vplay, tied to the VC's VIDEO_QUALITY setting."""
    import os as _os

    return {
        "1080p": 1080,
        "720p": 720,
        "480p": 480,
        "360p": 360,
    }.get((_os.getenv("VIDEO_QUALITY") or "480p").strip().lower(), 480)


def _pick_stream_formats(info: dict, want_video: bool) -> dict:
    """Choose the best (video_url, audio_url) pair out of a yt-dlp info dict.

    Progressive (muxed) formats are preferred for video when one is available
    at an acceptable height, because a single container guarantees the encoder's
    own A/V alignment. Otherwise a video-only + audio-only pair is used, which
    is still perfectly in sync — each ffmpeg process decodes its own stream
    from its own t=0 (see the module note above).

    HLS/DASH manifests (`m3u8`, `mpd`) are excluded: ffmpeg has to re-fetch and
    re-parse the manifest mid-playback and its segment timestamps do not start
    at zero, which is the other classic source of A/V drift.
    """
    all_formats = [f for f in (info.get("formats") or []) if f.get("url")]

    def usable(f) -> bool:
        proto = (f.get("protocol") or "")
        # ⚡ ULTRA SPEED FIX: Accept DASH audio-only formats (ffmpeg can stream them directly)
        # Rejecting DASH was forcing 10-15s download fallback for every song from datacenter IPs
        if "m3u8" in proto:
            return False  # HLS handled separately
        is_audio_only = f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
        is_dash_audio = "dash" in proto and is_audio_only
        return proto.startswith("http") and (not ("dash" in proto) or is_dash_audio)

    def hls_ok(f) -> bool:
        """HLS is a perfectly good *streaming* source for ffmpeg.

        SPEED ROOT-CAUSE FIX ("gana pura 1 min baad baja", Heroku log:
        `stream=142.38s` after "no directly streamable http format found"):
        from a datacenter IP YouTube frequently answers with an HLS-only
        format list. Those were all filtered out here, so every /play fell
        back to downloading the COMPLETE file before a single frame played.
        ffmpeg opens an m3u8 in ~1s and streams it progressively, so HLS is
        now accepted as a last resort instead of triggering a full download.
        """
        proto = (f.get("protocol") or "")
        return "m3u8" in proto or (f.get("url") or "").split("?")[0].endswith(".m3u8")

    hls_formats = [f for f in all_formats if hls_ok(f)]
    formats = [f for f in all_formats if usable(f)] or hls_formats

    # ⚡ SPEED ROOT-CAUSE FIX ("direct-stream unavailable" -> 10s download fallback):
    # YouTube frequently answers with DASH/SABR-only formats from datacenter IPs.
    # The strict `usable()` filter rejected them all, forcing a full file download.
    # For AUDIO-ONLY requests, a DASH audio-only stream is perfectly streamable by
    # ffmpeg and has no A/V sync concerns. We explicitly allow DASH audio formats
    # to be picked, which restores instant direct streaming for most /play requests.
    def dash_audio_ok(f) -> bool:
        proto = (f.get("protocol") or "")
        return "dash" in proto and f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")

    if not want_video:
        dash_audio = [f for f in all_formats if dash_audio_ok(f)]
        if dash_audio:
            formats = list(formats) + dash_audio

    # ⚡ SPEED FIX: Include DASH audio formats in audio_only_fmts
    # DASH audio-only streams are perfectly valid for audio playback and should
    # not be filtered out just because they use the "dash" protocol
    audio_only_fmts = [
        f for f in formats 
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    video_only_fmts = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")]
    muxed_fmts = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]



    def abr(f):
        return f.get("abr") or f.get("tbr") or 0

            # Prefer high-quality stereo audio while keeping an upper bound so a
        # cloud dyno does not spend all bandwidth on a needlessly huge source.

    audio_pick = None
    if audio_only_fmts:
        try:
            max_abr = max(96, min(float(os.getenv("YT_AUDIO_MAX_ABR", "160")), 320))
        except (TypeError, ValueError):
            max_abr = 160.0
        capped = [f for f in audio_only_fmts if abr(f) and abr(f) <= max_abr]
        audio_pick = max(capped or audio_only_fmts, key=abr)

    if not want_video:
        # ⚡ SPEED FIX: Prioritize DASH audio formats when available
        # DASH audio-only streams are the fastest option from datacenter IPs
        dash_audio_fmts = [
            f for f in formats 
            if (f.get("protocol") or "") == "dash" 
            and f.get("acodec") not in (None, "none") 
            and f.get("vcodec") in (None, "none")
        ]
        
        # If DASH audio is available, use it immediately
        if dash_audio_fmts:
            best_dash = max(dash_audio_fmts, key=lambda f: f.get("abr") or f.get("tbr") or 0)
            LOGGER.info("#stream selected DASH audio format (abr=%s)", best_dash.get("abr") or best_dash.get("tbr"))
            return {"audio": best_dash["url"], "video": None}
        
        # If YouTube exposes an HLS manifest but no audio-only HTTPS format,
        # prefer the manifest over a WEB progressive itag (usually 18). The
        # latter can be a large muxed MP4 whose signed googlevideo URL is
        # rejected from cloud IPs, forcing a full download; HLS is designed for
        # progressive playback and avoids that multi-second fallback.
        hls_manifest = info.get("hlsManifestUrl")
        if not audio_pick and hls_manifest:
            return {"audio": hls_manifest, "video": None}
        if not audio_pick and hls_formats:
            # Some cloud responses expose only an HLS format entry and omit
            # the top-level manifest field. It is still a valid progressive
            # source for FFmpeg, so do not force a complete download.
            return {"audio": hls_formats[0]["url"], "video": None}
        # ROOT-CAUSE FIX ("/play karta hu to vplay ho raha hai + bohot lag"):
        # this used to fall back to a MUXED (video+audio) format when no
        # audio-only http format was listed. Handing PyTgCalls a 720p muxed
        # URL for an audio-only request meant (a) the camera track could still
        # be negotiated, so /play behaved like /vplay, and (b) ffmpeg pulled
        # the entire video bitrate just to throw the picture away — which is
        # exactly the heavy lag/stutter that was reported.
        # Audio requests now use an AUDIO-ONLY stream or nothing: returning {}
        # makes the caller fall back to download_audio(audio_only=True).
        if not audio_pick:
            # LAST RESORT: YouTube/InnerTube clients occasionally return a
            # valid progressive HTTP URL with missing or nonstandard codec
            # metadata. The old picker discarded it and forced a complete
            # yt-dlp download. For an audio request FFmpeg can safely select
            # the audio stream from a muxed/metadata-light URL, so preserve
            # direct playback whenever the URL is genuinely HTTP(S).
            if muxed_fmts:
                cheapest = min(muxed_fmts, key=lambda f: (f.get("height") or 0,
                                                          f.get("tbr") or 0))
                return {"audio": cheapest["url"], "video": None}
            metadata_light = [f for f in formats if str(f.get("url") or "").startswith(("http://", "https://"))]
            if metadata_light:
                cheapest = min(metadata_light, key=lambda f: (f.get("height") or 0,
                                                              f.get("tbr") or f.get("abr") or 0))
                LOGGER.info("#stream accepted metadata-light direct audio URL")
                return {"audio": cheapest["url"], "video": None}
            return {}
        return {"audio": audio_pick["url"], "video": None}

    def height(f):
        return f.get("height") or 0

    # LAG FIX: keep the picked video within the same cap the VC actually
    # broadcasts at (VIDEO_QUALITY, default 480p). Streaming a 720p source
    # only to downscale it wastes bandwidth and CPU and causes the stutter.
    cap = _max_stream_height()

    muxed_ok = [f for f in muxed_fmts if 0 < height(f) <= cap]
    if muxed_ok:
        best_muxed = max(muxed_ok, key=lambda f: (height(f), f.get("tbr") or 0))
        # One container carrying both tracks: hand the SAME url to both ffmpeg
        # processes and let each pick its own track.
        return {"video": best_muxed["url"], "audio": best_muxed["url"]}

    # Large-media production fix: when YouTube exposes only an adaptive
    # video/audio pair, stream both signed URLs directly. Returning {} here
    # previously forced yt-dlp to download and merge the entire movie before
    # playback, which is impossible to guarantee within 5-10 seconds for a
    # 3-hour/3GB input and can exhaust a small dyno's disk.
    if [f for f in video_only_fmts if 0 < height(f) <= cap] and audio_pick:
        video_pick = max(
            [f for f in video_only_fmts if 0 < height(f) <= cap],
            key=lambda f: (height(f), f.get("tbr") or 0),
        )
        LOGGER.info(
            "#stream direct adaptive pair selected height=%sp video+audio",
            height(video_pick),
        )
        return {"video": video_pick["url"], "audio": audio_pick["url"]}

    if muxed_fmts:
        best_muxed = max(muxed_fmts, key=lambda f: (height(f), f.get("tbr") or 0))
        return {"video": best_muxed["url"], "audio": best_muxed["url"]}
    return {}


def _invidious_streams_sync(video_id: str, want_video: bool) -> dict | None:
    """Last-resort direct resolver using public Invidious video metadata.

    Some YouTube client responses expose only SABR/ciphered formats, while an
    Invidious instance can still return a short-lived progressive or HLS URL.
    This is used only after InnerTube and yt-dlp direct extraction fail; normal
    searches and downloads do not depend on public instances.
    """
    import json
    for instance in _INVIDIOUS_INSTANCES[:_INVIDIOUS_MAX_INSTANCES]:
        try:
            response = get_http_sync_client().get(
                f"{instance}/api/v1/videos/{video_id}",
                headers={"User-Agent": "Mozilla/5.0 (compatible; MelodyBot/1.0)"},
                timeout=0.75,
            )
            if response.status_code != 200:
                continue
            data = json.loads(response.text) or {}
            formats = []
            for item in list(data.get("adaptiveFormats") or []) + list(data.get("formatStreams") or []):
                url = item.get("url")
                if not url:
                    continue
                mime = str(item.get("type") or item.get("mimeType") or "")
                is_video = mime.startswith("video/") or bool(item.get("size"))
                has_audio = mime.startswith("audio/") or "audio" in mime or bool(item.get("audioQuality"))
                formats.append({
                    "url": url,
                    "protocol": "m3u8_native" if ".m3u8" in url else "https",
                    "vcodec": "h264" if is_video else "none",
                    "acodec": "aac" if has_audio else "none",
                    "height": int(item.get("height") or 0),
                    "abr": float(item.get("bitrate") or 0) / 1000,
                    "tbr": float(item.get("bitrate") or 0) / 1000,
                })
            hls_url = data.get("hlsUrl")
            if hls_url:
                formats.append({
                    "url": hls_url, "protocol": "m3u8_native",
                    "vcodec": "h264", "acodec": "aac", "height": 480,
                    "abr": 160, "tbr": 600,
                })
            picked = _pick_stream_formats({"formats": formats}, want_video)
            if not picked:
                continue
            urls = [u for u in (picked.get("video"), picked.get("audio")) if u]
            picked["headers"] = {"User-Agent": "Mozilla/5.0 (compatible; MelodyBot/1.0)"}
            picked["expires_at"] = min(_url_expiry(u) for u in urls) - _STREAM_URL_SAFETY_MARGIN
            LOGGER.info("#stream Invidious direct fallback resolved %s via %s", video_id, instance)
            return picked
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Invidious direct resolver %s failed: %s", instance, exc)
    return None


def _resolve_stream_urls_sync(target: str, want_video: bool) -> dict:
    opts = {
        **_ydl_opts(audio_only=not want_video),
        "extract_flat": False,
        "skip_download": True,
        # SPEED FIX (playback started noticeably later than other music
        # bots): this metadata-only resolve sits directly on the critical
        # path to "song starts". Skipping the extra watch-page/config
        # round-trips, playlist expansion, comments/subtitle metadata and
        # per-format HEAD checks cuts seconds off every /play without
        # changing which stream gets picked.
        "noplaylist": True,
        "playlist_items": "1",
        "check_formats": False,
        "getcomments": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }
    _ex = dict(opts.get("extractor_args") or {})
    _yt = dict(_ex.get("youtube") or {})
    _yt.setdefault("player_skip", ["configs"])
    _yt.setdefault("skip", ["translated_subs"])
    _ex["youtube"] = _yt
    opts["extractor_args"] = _ex
    # The format selector only matters for a download; picking the streamable
    # pair by hand needs the FULL format list, so drop the selector here.
    opts.pop("format", None)
    info = _warm_extract(
        opts, target, cache_key=f"resolve:{'v' if want_video else 'a'}"
    )
    if info and info.get("entries"):
        info = info["entries"][0]
    info = info or {}
    picked = _pick_stream_formats(info, want_video)
    if not picked and info.get("url"):
        # Some extractor clients return only one top-level URL and omit the
        # formats array. Preserve both ordinary HTTPS and HLS top-level URLs:
        # dropping the latter incorrectly converted a progressive stream into
        # a complete-file download.
        top_url = str(info["url"])
        proto = str(info.get("protocol") or "").lower()
        is_hls = "m3u8" in proto or top_url.split("?", 1)[0].endswith(".m3u8")
        if proto.startswith("http") and (is_hls or "dash" not in proto):
            picked = {"video": top_url, "audio": top_url} if want_video else {"audio": top_url, "video": None}
    if not picked:
        safe_target_id = _extract_video_id(target) or "unknown"
        LOGGER.info(
            "#stream direct formats unavailable for %s: formats=%d http=%d hls=%d audio=%d muxed=%d video=%d",
            safe_target_id,
            len(info.get("formats") or []),
            sum(1 for f in info.get("formats") or [] if str(f.get("protocol") or "").startswith("http")),
            sum(1 for f in info.get("formats") or [] if "m3u8" in str(f.get("protocol") or "") or str(f.get("url") or "").split("?")[0].endswith(".m3u8")),
            sum(1 for f in info.get("formats") or [] if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")),
            sum(1 for f in info.get("formats") or [] if f.get("acodec") not in (None, "none") and f.get("vcodec") not in (None, "none")),
            sum(1 for f in info.get("formats") or [] if f.get("vcodec") not in (None, "none")),
        )
        raise ValueError("no directly streamable HTTP/HLS format found")
    picked["is_live"] = bool((info or {}).get("is_live"))
    urls = [u for u in (picked.get("video"), picked.get("audio")) if u]
    resolved_headers = dict((info or {}).get("http_headers") or {})
    if not resolved_headers:
        for fmt_info in (info or {}).get("formats") or []:
            if fmt_info.get("url") in urls and fmt_info.get("http_headers"):
                resolved_headers.update(fmt_info["http_headers"])
                break
    picked["headers"] = {
        k: v
        for k, v in (resolved_headers or _ydl_opts().get("http_headers") or {}).items()
        if k in ("User-Agent", "Referer")
    }
    picked["expires_at"] = min(_url_expiry(u) for u in urls) - _STREAM_URL_SAFETY_MARGIN
    return picked


def _cache_late_resolve(key: str):
    """Cache a direct-resolve result that arrived after the race budget."""

    def _done(task) -> None:
        try:
            late = task.result()
        except Exception:  # noqa: BLE001 - a late failure is just a loser
            return
        if not late:
            return
        cached = _stream_url_cache.get(key)
        if cached and cached.get("expires_at", 0) > _time_mod.time():
            return
        _stream_url_cache[key] = late
        _stream_url_failures.pop(key, None)
        _prune_stream_url_state()
        LOGGER.info("🔗 #stream late direct resolve cached for %s", key)

    return _done


async def resolve_stream_urls(
    video_id: str, want_video: bool = False, *, force: bool = False,
) -> dict:
    """Resolve direct CDN URLs to stream from, without downloading anything.

    Returns {"video": url|None, "audio": url, "headers": {...}, "is_live": bool}.
    Accepts a bare 11-char YouTube id or any full http(s) URL yt-dlp supports,
    so /play and /vplay behave identically for links, playlist entries and
    plain search results. Raises on anything unresolvable so the caller can
    fall back to the download path.
    """
    import re

    if re.match(r"https?://", video_id or ""):
        target = video_id
    elif re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        target = f"https://www.youtube.com/watch?v={video_id}"
    else:
        # Synthetic Telegram-media ids (tg<chat>_<msg>) and junk have no CDN
        # source — the caller must use the local-file path for those.
        raise ValueError(f"resolve_stream_urls: not a streamable source {video_id!r}")

    key = f"{video_id}:{'v' if want_video else 'a'}"
    _prune_stream_url_state()
    cached = None if force else _stream_url_cache.get(key)
    if cached and cached.get("expires_at", 0) > _time_mod.time():
        LOGGER.debug("⚡ stream-url cache HIT for %s", key)
        return cached
    # ⚡ SPEED FIX: force=True completely bypasses negative cache for instant retry
    failure_until = 0.0 if force else _stream_url_failures.get(key, 0.0)
    now = _time_mod.monotonic()
    # A background warm resolve may have just recorded a transient failure;
    # after the bounded cache age, allow the foreground request to retry.
    # This is the background warm resolve recovery path.
    if failure_until > now and not force:
        failure_age = _STREAM_URL_FAILURE_TTL - (failure_until - now)
        if failure_age >= 0.75:
            raise ValueError("direct stream temporarily unavailable (cached failure)")

    if failure_until:
        _stream_url_failures.pop(key, None)

    lock = _stream_url_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = None if force else _stream_url_cache.get(key)
        if cached and cached.get("expires_at", 0) > _time_mod.time():
            return cached
        # ⚡ SPEED FIX: force=True completely bypasses negative cache for instant retry
        failure_until = 0.0 if force else _stream_url_failures.get(key, 0.0)
        now = _time_mod.monotonic()
        if failure_until > now and not force:
            failure_age = _STREAM_URL_FAILURE_TTL - (failure_until - now)
            if failure_age >= 0.75:
                raise ValueError("direct stream temporarily unavailable (cached failure)")
        if failure_until:
            _stream_url_failures.pop(key, None)
        # SPEED FIX: race InnerTube (≈300ms) against yt-dlp (2-5s on Heroku).
        # The first source that returns a usable format wins, so the bot can
        # join the VC and start the song without waiting for a full extraction.
        vid_only = _extract_video_id(target) or video_id
        # run_in_executor() already returns an awaitable Future. Wrapping that
        # in create_task() raises ``TypeError: a coroutine was expected`` and
        # was forcing every request onto the slow full-download fallback.
        # ensure_future() accepts both coroutines and Futures and also lets us
        # observe loser exceptions cleanly.
        loop = asyncio.get_running_loop()
        _t0 = _time_mod.monotonic()

        # SPEED JUGAAD: InnerTube gets a HEAD START instead of being launched
        # side-by-side with yt-dlp. On a 1-CPU dyno the yt-dlp extraction
        # (player JS + Deno + PO token) saturates the box and actually slows
        # the ~300ms InnerTube POST down. So: fire InnerTube alone, and only
        # spawn the heavy yt-dlp fallback if InnerTube has not answered within
        # _INNERTUBE_HEADSTART seconds. In the common case yt-dlp never runs
        # at all, which is where most of the old "gana late start hota hai"
        # latency was coming from.
        tasks = []
        it_task = None
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid_only or "") and not _innertube_stream_muted():
            it_task = asyncio.ensure_future(
                loop.run_in_executor(YTDL_POOL, _resolve_stream_urls_innertube, vid_only, want_video)
            )
            tasks.append(it_task)

        ydl_task = None
        if it_task is not None:
            done_fast, _ = await asyncio.wait({it_task}, timeout=_INNERTUBE_HEADSTART)
            if done_fast:
                try:
                    fast = it_task.result()
                except Exception as exc:  # noqa: BLE001
                    fast = None
                    LOGGER.debug("innertube head-start failed for %s: %s", video_id, exc)
                if fast:
                    _stream_url_cache[key] = fast
                    _prune_stream_url_state()
                    LOGGER.info("⚡ #stream innertube head-start resolved %s", video_id)
                    return fast
        ydl_task = asyncio.ensure_future(
            loop.run_in_executor(YTDL_POOL, _resolve_stream_urls_sync, target, want_video)
        )
        tasks.append(ydl_task)

        resolved = None
        last_exc: Exception | None = None
        pending = {t for t in tasks if not t.done()}
        # SPEED FIX ("gana instant play kyu nahi hota"): the timeout used to
        # be passed to asyncio.wait() *inside the loop*, so it restarted on
        # every iteration — a fast InnerTube failure plus a slow yt-dlp meant
        # 2x (or more) the intended budget before the download fallback even
        # started. The Heroku log shows exactly that: 9s between the resolve
        # and "falling back to download". One absolute deadline instead.
        # Absolute budget measured from the very start of the resolve (the
        # InnerTube head start counts against it), with a small floor so the
        # yt-dlp fallback always gets a fair chance to answer.
        deadline = max(_t0 + _RESOLVE_TIMEOUT, _time_mod.monotonic() + 1.5)
        try:
            while pending:
                remaining = deadline - _time_mod.monotonic()
                if remaining <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED, timeout=remaining
                )
                if not done:
                    break
                for t in done:
                    try:
                        resolved = t.result()
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        continue
                    if resolved:
                        break
                if resolved:
                    break
        finally:
            # SPEED FIX: a resolve that is still running when the budget ends
            # used to be cancelled outright, throwing away work that was often
            # only a fraction of a second from finishing. The extraction thread
            # keeps running anyway, so let it finish and CACHE its result — the
            # retry, the next queue item and the auto-advance of the same song
            # then start instantly instead of paying the whole cost again.
            for t in pending:
                t.add_done_callback(_cache_late_resolve(key))

        if not resolved and re.fullmatch(r"[A-Za-z0-9_-]{11}", vid_only or ""):
            # Public Invidious metadata is slower and less predictable than
            # YouTube’s own APIs, so use it only as the final direct-stream
            # rescue before declaring the URL unavailable.
            try:
                resolved = await asyncio.wait_for(
                    loop.run_in_executor(
                        YTDL_POOL, _invidious_streams_sync, vid_only, want_video,
                    ),
                    timeout=_env_float("DIRECT_RESCUE_TIMEOUT", 1.5),
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc

        if not resolved:
            # Only remember a *real* failure. A resolve that merely ran out of
            # budget while still working must not poison the next attempt with
            # a cached "direct stream temporarily unavailable".
            if not pending:
                _stream_url_failures[key] = _time_mod.monotonic() + _STREAM_URL_FAILURE_TTL
            LOGGER.info(
                "#stream direct resolve gave up for %s after %.2fs (pending=%d): %s",
                video_id, _time_mod.monotonic() - _t0, len(pending), last_exc,
            )
            raise last_exc or ValueError("no directly streamable http format found")
        _stream_url_failures.pop(key, None)
        _stream_url_cache[key] = resolved
        _prune_stream_url_state()
        LOGGER.info(
            "🔗 #stream resolved direct %s URLs for %s in %.2fs (no download)",
            "video+audio" if want_video else "audio",
            video_id, _time_mod.monotonic() - _t0,
        )
        return resolved
