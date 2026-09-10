"""RSS watchdog — the fix for Heroku `Error R14 (Memory quota exceeded)`.

Heroku log:

    heroku[worker.1]: Process running mem=1090M(102.2%)
    heroku[worker.1]: Error R14 (Memory quota exceeded)

Root cause: nothing ever gave memory back. yt-dlp extractor state, InnerTube
JSON responses, Pillow thumbnail buffers and ffmpeg pipe buffers are all
short-lived, but CPython keeps the freed arenas mapped and glibc only returns
them to the OS on an explicit `malloc_trim()`. RSS therefore ratchets upward
until the dyno crosses its quota and Heroku starts swapping/killing — which is
also why commands felt progressively slower the longer the worker ran.

This watchdog samples RSS on a slow timer and, once it crosses a soft
threshold, runs a full `gc.collect()` followed by `malloc_trim(0)` so the freed
arenas actually go back to the kernel. Both operations are cheap (a few ms) and
run at most once per interval, so they never block playback.

Configurable with env vars:
    MEM_GUARD           "off" disables the watchdog entirely
    MEM_SOFT_LIMIT_MB   trim threshold; default = 300 MB (or 65% of quota)
    MEM_CHECK_SECONDS   sample interval, default 5 s
"""
from __future__ import annotations

import asyncio
import ctypes
import gc
import logging
import os

LOGGER = logging.getLogger("Melody")

_libc = None


def _malloc_trim() -> bool:
    """Ask glibc to return free arenas to the OS. No-op off glibc."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL("libc.so.6")
        _libc.malloc_trim(0)
        return True
    except Exception:  # noqa: BLE001 - musl/macOS have no malloc_trim
        return False


def rss_mb() -> float:
    """Resident set size in MB, read straight from /proc (no psutil cost)."""
    try:
        with open("/proc/self/statm", "rb") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except Exception:  # noqa: BLE001
        try:
            import resource

            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:  # noqa: BLE001
            return 0.0


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or 0)
    except ValueError:
        value = 0
    return value if value > 0 else default


def dyno_quota_mb() -> int:
    """Memory quota of the host dyno/container, 0 when unknown.

    Heroku exports MEMORY_AVAILABLE (MB) on every dyno; containers expose the
    cgroup limit. Deriving the trim threshold from the real quota is what stops
    `Error R14 (Memory quota exceeded)` after a dyno resize — a hard-coded
    420 MB default is far too low on standard-2x (1024 MB) and far too high on
    a 512 MB dyno.
    """
    try:
        available = int(float(os.getenv("MEMORY_AVAILABLE", "") or 0))
        if available > 0:
            return available
    except ValueError:
        pass
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        try:
            with open(path) as fh:
                raw = fh.read().strip()
            if raw and raw != "max":
                limit = int(raw) // (1024 * 1024)
                if 0 < limit < 64 * 1024:
                    return limit
        except (OSError, ValueError):
            continue
    return 0


def _soft_limit_mb() -> int:
    # 0.8 of the quota was too late: the attached Heroku log jumps from
    # 1056 MB (102%) to 1613 MB (157%) inside 18 seconds, i.e. the process can
    # cross the whole remaining 20% between two samples. Trimming from 0.65
    # keeps RSS flat during normal play instead of only reacting after R14.
    explicit = os.getenv("MEM_SOFT_LIMIT_MB", "").strip()
    quota = dyno_quota_mb()
    if explicit:
        requested = _env_int("MEM_SOFT_LIMIT_MB", 300)
        # Do not let an old/high Config Var (for example 420 on a 512 MB
        # dyno) defeat the guard. Keep the configured value on larger hosts,
        # but cap it to the early-warning threshold for small dynos.
        if quota and quota <= 768:
            return min(requested, int(quota * 0.65))
        return requested
    return int(quota * 0.65) if quota else 300


async def memory_guard() -> None:
    """Background task: trim memory whenever RSS crosses the soft limit."""
    if (os.getenv("MEM_GUARD", "on") or "on").lower() in ("off", "0", "false"):
        return

    soft_limit = _soft_limit_mb()
    quota = dyno_quota_mb()
    # A slow 60s sample was long enough for a couple of parallel downloads to
    # blow straight past the quota between checks (Heroku: mem=763M/148.9%).
    # 20 s was still long enough for two parallel downloads plus an ONNX load
    # to blow through the quota between samples (log: 102% -> 157% in 18 s).
    interval = _env_int("MEM_CHECK_SECONDS", 5)
    peak = 0.0
    LOGGER.info(
        "🧠 memguard active: trim above %d MB (dyno quota %s, every %ds)",
        soft_limit, f"{quota} MB" if quota else "unknown", interval,
    )

    while True:
        try:
            await asyncio.sleep(interval)
            before = rss_mb()
            peak = max(peak, before)
            if before < soft_limit:
                continue
            gc.collect()
            trimmed = _malloc_trim()
            after = rss_mb()
            LOGGER.info(
                "🧹 memguard: RSS %.0f MB → %.0f MB (limit %d MB, malloc_trim=%s)",
                before, after, soft_limit, "yes" if trimmed else "n/a",
            )
            # Still over quota after a trim? Drop the caches that actually hold
            # the memory (yt-dlp/InnerTube/thumbnail buffers) before Heroku
            # starts swapping and every command gets slower.
            if quota and after > quota * 0.80:
                LOGGER.warning(
                    "⚠️ memguard: RSS %.0f MB is near the %d MB dyno quota — "
                    "dropping caches to avoid Error R14.", after, quota,
                )
                _emergency_release()
                gc.collect()
                _malloc_trim()
                LOGGER.info("🧯 memguard: RSS after emergency release: %.0f MB", rss_mb())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a watchdog must never die
            LOGGER.warning("memguard iteration failed: %s", exc)


def _emergency_release() -> None:
    """Best-effort cache eviction; every step is optional and never fatal."""
    # The main releasable structures are yt-dlp / InnerTube metadata and
    # signed-URL caches. They rebuild lazily, so dropping them costs only one
    # extra resolve later and is cheaper than the dyno swapping or being killed.
    try:
        from melody.core.ytdl import drop_caches

        LOGGER.info("🧯 memguard: dropped %d cached ytdl entries", drop_caches())
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("memguard ytdl cache drop skipped: %s", exc)
    try:
        for obj in gc.get_objects():
            cache_clear = getattr(obj, "cache_clear", None)
            if callable(cache_clear) and hasattr(obj, "cache_info"):
                try:
                    cache_clear()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("memguard lru sweep skipped: %s", exc)
    try:
        from utils.diskguard import sweep as _disk_sweep

        _disk_sweep(aggressive=True)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("memguard disk sweep skipped: %s", exc)
    try:
        import glob
        import time as _time

        cutoff = _time.time() - 600
        for path in glob.glob("/tmp/*.part") + glob.glob("/tmp/melody_dl/*"):
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("memguard tmp sweep skipped: %s", exc)

