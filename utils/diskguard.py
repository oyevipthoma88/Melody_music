"""💾 Disk guard — keeps /tmp small so playback never stalls on a full dyno.

Why this exists
---------------
Song files were only ever evicted from one place: `_cleanup_track_file()` in
`melody/core/call.py`, which runs when a track *ends*. That leaves three real
holes on a 512 MB Heroku dyno (its whole filesystem, `/tmp` included):

  1. **Restart/crash** — every `/tmp/melody_*` file from the previous run stays
     on disk forever; nothing ever sweeps at boot.
  2. **Long tracks / idle bot** — a 1-hour live stream, or a chat that stops
     playing mid-queue, never fires a stream-end, so nothing is reclaimed for
     as long as the worker lives.
  3. **No free-space awareness** — the cap was a fixed `SONG_CACHE_MB`, blind
     to how much room the dyno actually has left. When the disk fills, yt-dlp
     writes fail, ffmpeg reads garbage, and playback dies with confusing
     "file not found" errors.

This module centralises eviction:

  * `sweep()`   — LRU eviction down to the cache cap, plus an extra, harder
                  pass whenever free space is low. Never touches a file that
                  is still being written, and never touches the tracks that
                  are playing/queued right now.
  * `disk_guard()` — a slow background loop (default 90 s) that calls it.
  * `boot_sweep()` — one aggressive pass at startup for orphans from the
                  previous run.

Everything is best-effort: a janitor must never raise into playback.

Env vars
    SONG_CACHE_MB      cache ceiling in MB (shared with call.py, default 96)
    DISK_MIN_FREE_MB   emergency threshold, default 128
    DISK_CHECK_SECONDS loop interval, default 90
    DISK_GUARD         "off" disables the background loop
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import time

LOGGER = logging.getLogger("Melody")

CACHE_GLOBS = ("/tmp/melody_*_?.*",)
STAGING_GLOB = "/tmp/melody_dl/*"
PARTIAL_GLOBS = ("/tmp/*.part", "/tmp/*.ytdl", "/tmp/melody_*.temp", "/tmp/melody_*.dl.temp")

# Suffixes/markers that mean "a download is writing this file right now".
_IN_PROGRESS_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp", ".dl")

# A file younger than this may still be an in-flight write even without a
# marker suffix, so the janitor leaves it alone.
_MIN_AGE_SECONDS = 60.0


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or 0)
    except ValueError:
        value = 0
    return value if value > 0 else default


def cache_limit_bytes() -> int:
    mb = _env_int("SONG_CACHE_MB", 96)
    return max(16, min(mb, 256)) * 1024 * 1024


def min_free_bytes() -> int:
    return _env_int("DISK_MIN_FREE_MB", 128) * 1024 * 1024


def free_bytes(path: str = "/tmp") -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _is_in_progress(path: str) -> bool:
    return path.endswith(_IN_PROGRESS_SUFFIXES) or ".part-" in path or ".dl." in path


def _protected_video_ids() -> set[str]:
    """video_ids the player must keep on disk: current + queued + prefetched."""
    ids: set[str] = set()
    try:
        from melody.core import queue as _queue

        ids |= _queue.active_video_ids()
    except Exception:  # noqa: BLE001 — janitor never depends on the player
        pass
    return {i for i in ids if i}


def _is_protected(path: str, protected: set[str]) -> bool:
    name = os.path.basename(path)
    return any(name.startswith(f"melody_{vid}_") for vid in protected)


def _collect(patterns) -> list[tuple[str, float, int]]:
    out: list[tuple[str, float, int]] = []
    for pattern in patterns:
        for f in glob.glob(pattern):
            if _is_in_progress(f):
                continue
            try:
                st = os.stat(f)
            except OSError:
                continue
            if not os.path.isfile(f):
                continue
            out.append((f, max(st.st_mtime, st.st_atime), st.st_size))
    return out


def _unlink(path: str) -> int:
    try:
        size = os.path.getsize(path)
        os.unlink(path)
        return size
    except OSError:
        return 0


def sweep(aggressive: bool = False) -> int:
    """Evict cached media until under the cap. Returns bytes reclaimed."""
    reclaimed = 0
    now = time.time()

    # 1. Abandoned partials + staging leftovers: pure waste, always go first.
    stale_cutoff = now - (300 if aggressive else 1800)
    for pattern in (*PARTIAL_GLOBS, STAGING_GLOB):
        for f in glob.glob(pattern):
            try:
                if os.path.getmtime(f) >= stale_cutoff:
                    continue
                if os.path.isdir(f) and os.path.basename(f).startswith("yt_"):
                    size = sum(
                        os.path.getsize(p)
                        for p in glob.glob(os.path.join(f, "**", "*"), recursive=True)
                        if os.path.isfile(p)
                    )
                    shutil.rmtree(f, ignore_errors=True)
                    reclaimed += size
                elif os.path.isfile(f):
                    reclaimed += _unlink(f)
            except OSError:
                pass

    # 2. LRU eviction of finished cache files down to the ceiling.
    protected = _protected_video_ids()
    files = _collect(CACHE_GLOBS)
    total = sum(f[2] for f in files)
    limit = cache_limit_bytes()
    if aggressive:
        limit //= 4
    # Low free space beats any configured ceiling — a full disk breaks playback.
    if free_bytes() < min_free_bytes():
        limit = min(limit, 16 * 1024 * 1024)

    files.sort(key=lambda x: x[1])  # oldest touched first
    for path, mtime, _size in files:
        if total <= limit:
            break
        if now - mtime < _MIN_AGE_SECONDS:
            continue  # possibly still being written / just handed to ffmpeg
        if _is_protected(path, protected):
            continue  # playing or queued right now
        freed = _unlink(path)
        total -= freed
        reclaimed += freed

    if reclaimed:
        LOGGER.info(
            "🧹 diskguard: reclaimed %.1f MB (cache %.1f MB, free %.0f MB)",
            reclaimed / 1048576, total / 1048576, free_bytes() / 1048576,
        )
    return reclaimed


def boot_sweep() -> int:
    """Startup pass: nothing is playing yet, so every leftover is an orphan."""
    reclaimed = 0
    for pattern in (*PARTIAL_GLOBS, STAGING_GLOB):
        for f in glob.glob(pattern):
            try:
                if os.path.isdir(f) and os.path.basename(f).startswith("yt_"):
                    reclaimed += sum(
                        os.path.getsize(p)
                        for p in glob.glob(os.path.join(f, "**", "*"), recursive=True)
                        if os.path.isfile(p)
                    )
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    reclaimed += _unlink(f)
            except OSError:
                pass
    reclaimed += sweep()
    return reclaimed


async def disk_guard() -> None:
    """Background janitor loop; disable with DISK_GUARD=off."""
    if (os.getenv("DISK_GUARD", "") or "").lower() in {"off", "0", "false"}:
        LOGGER.info("💾 diskguard disabled via DISK_GUARD")
        return
    interval = _env_int("DISK_CHECK_SECONDS", 90)
    LOGGER.info(
        "💾 diskguard active: cap %d MB, min free %d MB, every %ds",
        cache_limit_bytes() // 1048576, min_free_bytes() // 1048576, interval,
    )
    try:
        await asyncio.to_thread(boot_sweep)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("diskguard boot sweep skipped: %s", exc)
    while True:
        try:
            await asyncio.sleep(interval)
            low = free_bytes() < min_free_bytes()
            await asyncio.to_thread(sweep, low)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a watchdog must never die
            LOGGER.warning("diskguard iteration failed: %s", exc)
