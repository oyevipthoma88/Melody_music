"""Small process-local caches for immutable client metadata.

Pyrogram already caches some peer data, but `get_me()` is still an RPC in
several handler hot paths. Bot identity changes rarely, so a short-lived cache
removes duplicate latency without making a restart or account switch sticky.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

_ME_TTL = 3600.0
_me_cache: dict[int, tuple[Any, float]] = {}
_me_lock = asyncio.Lock()


async def get_me_cached(client: Any, *, ttl: float = _ME_TTL) -> Any:
    """Return ``client.get_me()`` with a bounded, client-specific cache."""
    key = id(client)
    now = time.monotonic()
    cached = _me_cache.get(key)
    if cached and cached[0] is not None and cached[1] > now:
        return cached[0]
    async with _me_lock:
        now = time.monotonic()
        cached = _me_cache.get(key)
        if cached and cached[0] is not None and cached[1] > now:
            return cached[0]
        me = await client.get_me()
        _me_cache[key] = (me, now + max(1.0, float(ttl)))
        if len(_me_cache) > 8:
            oldest = min(_me_cache, key=lambda item: _me_cache[item][1])
            _me_cache.pop(oldest, None)
        return me


def invalidate_me_cache(client: Any | None = None) -> None:
    """Invalidate one client or all cached identities after re-login/reload."""
    if client is None:
        _me_cache.clear()
    else:
        _me_cache.pop(id(client), None)
