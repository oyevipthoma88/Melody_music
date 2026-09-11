"""Dedicated thread pools for blocking work (yt-dlp, ffprobe, disk I/O).

The pools are deliberately bounded.  YouTube metadata is network-bound, but
creating too many workers on Heroku also multiplies thread stacks, extractor
state, and temporary buffers.  Defaults therefore adapt to the declared memory
budget and remain overrideable through ``YTDL_WORKERS`` and ``IO_WORKERS``.
"""

from __future__ import annotations

import atexit
import os
import threading
from concurrent.futures import ThreadPoolExecutor


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or 0)
    except ValueError:
        value = 0
    return value if value > 0 else default


def _memory_limit_mb() -> int:
    """Return the configured/container memory budget when available."""
    for name in ("MEMORY_LIMIT_MB", "MEMORY_AVAILABLE_MB", "DYNO_RAM_MB"):
        raw = (os.getenv(name) or "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError:
                continue
            if value > 0:
                return value
    # Use the conservative Heroku 512 MB profile when no quota is exposed.
    return 512


_MEMORY_LIMIT_MB = _memory_limit_mb()

# Keep the small-host profile conservative. Operators with more memory can
# explicitly raise these values, but hard caps prevent accidental 32/64-thread
# explosions when a host reports its physical CPU count instead of its dyno
# quota.
_DEFAULT_YTDL_WORKERS = 2 if _MEMORY_LIMIT_MB <= 1024 else 4
_DEFAULT_IO_WORKERS = 1 if _MEMORY_LIMIT_MB <= 1024 else 2
_requested_ytdl_workers = _env_int("YTDL_WORKERS", _DEFAULT_YTDL_WORKERS)
_requested_io_workers = _env_int("IO_WORKERS", _DEFAULT_IO_WORKERS)
YTDL_WORKERS = (
    _DEFAULT_YTDL_WORKERS
    if _MEMORY_LIMIT_MB <= 1024
    else min(4, _requested_ytdl_workers)
)
IO_WORKERS = (
    _DEFAULT_IO_WORKERS
    if _MEMORY_LIMIT_MB <= 1024
    else min(2, _requested_io_workers)
)

# A Python thread normally reserves an 8 MB stack.  A 512 KB stack is enough
# for network callbacks and leaves substantially more RSS headroom for ffmpeg,
# yt-dlp, and the Telegram clients.
try:
    threading.stack_size(512 * 1024)
except (ValueError, RuntimeError):
    pass

YTDL_POOL = ThreadPoolExecutor(max_workers=YTDL_WORKERS, thread_name_prefix="melody-ytdl")

# SPEED ROOT FIX ("gana 10s baad bajta hai"): the InnerTube player probes are
# pure network waits (~150 ms each) but they used to be submitted into
# YTDL_POOL, which only has 2 workers on a 1 GB dyno and is normally busy with
# a yt-dlp extraction/download. The probes therefore sat in the queue until
# their own timeout expired, every /play logged "direct source unavailable"
# and fell back to the slow full download. They now get their own tiny pool so
# the fast path can never be starved by yt-dlp.
NET_WORKERS = _env_int("NET_WORKERS", 8)
NET_POOL = ThreadPoolExecutor(max_workers=NET_WORKERS, thread_name_prefix="melody-net")
IO_POOL = ThreadPoolExecutor(max_workers=IO_WORKERS, thread_name_prefix="melody-io")


@atexit.register
def _shutdown() -> None:  # pragma: no cover - process teardown
    YTDL_POOL.shutdown(wait=False, cancel_futures=True)
    IO_POOL.shutdown(wait=False, cancel_futures=True)
    NET_POOL.shutdown(wait=False, cancel_futures=True)
