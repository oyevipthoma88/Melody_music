"""TTL cache for Pyrogram's `get_chat()` / `get_users()`.

WHY
═══
The Heroku logs were full of:

    WARNING pyrogram.session.session: [MelodyBot] Waiting for 13 seconds
            before continuing (required by "channels.GetFullChannel")
    WARNING pyrogram.session.session: [MelodyAssistant] Waiting for 3 seconds
            before continuing (required by "users.GetFullUser")

Those are Telegram FLOOD_WAITs. `get_chat(chat_id)` issues a full
`channels.GetFullChannel` request every single time, and the bot calls it on
every play / VC event / autoplay tick for the SAME handful of chats. Telegram
rate-limits full-info lookups aggressively, so the client ends up sleeping
several seconds inside the play path — slow playback *and* noisy logs.

Chat titles/usernames and user names barely change, so a short in-memory TTL
cache removes ~all of those requests while staying fresh enough. Applied
class-level (once, at startup) so every call site benefits without edits.
"""
from __future__ import annotations

import time
from typing import Any

from melody.logging import LOGGER

CHAT_TTL = 300.0   # seconds — full chat info
USER_TTL = 600.0   # seconds — full user info

_chat_cache: dict[Any, tuple[float, Any]] = {}
_user_cache: dict[Any, tuple[float, Any]] = {}
_MAX_ENTRIES = 2048

_installed = False


def _get(cache: dict, key: Any, ttl: float):
    entry = cache.get(key)
    if not entry:
        return None
    stamp, value = entry
    if time.monotonic() - stamp > ttl:
        cache.pop(key, None)
        return None
    return value


def _put(cache: dict, key: Any, value: Any) -> None:
    if len(cache) >= _MAX_ENTRIES:
        cache.clear()
    cache[key] = (time.monotonic(), value)


def forget(chat_id: Any) -> None:
    """Drop a cached entry (call after renames / setting changes)."""
    _chat_cache.pop(chat_id, None)
    _user_cache.pop(chat_id, None)


def install_chat_cache() -> None:
    global _installed
    if _installed:
        return
    try:
        from pyrogram.client import Client
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("chat cache not installed (%s)", exc)
        return

    original_get_chat = Client.get_chat
    original_get_users = Client.get_users
    if getattr(original_get_chat, "_melody_cached", False):
        _installed = True
        return

    async def get_chat(self, chat_id, *args, **kwargs):
        key = (id(self), chat_id)
        cached = _get(_chat_cache, key, CHAT_TTL)
        if cached is not None:
            return cached
        result = await original_get_chat(self, chat_id, *args, **kwargs)
        if result is not None:
            _put(_chat_cache, key, result)
        return result

    async def get_users(self, user_ids, *args, **kwargs):
        # Only single-id lookups are cached; bulk lists are already one request.
        if isinstance(user_ids, (list, tuple, set)):
            return await original_get_users(self, user_ids, *args, **kwargs)
        key = (id(self), user_ids)
        cached = _get(_user_cache, key, USER_TTL)
        if cached is not None:
            return cached
        result = await original_get_users(self, user_ids, *args, **kwargs)
        if result is not None:
            _put(_user_cache, key, result)
        return result

    get_chat._melody_cached = True  # type: ignore[attr-defined]
    get_users._melody_cached = True  # type: ignore[attr-defined]
    Client.get_chat = get_chat  # type: ignore[assignment]
    Client.get_users = get_users  # type: ignore[assignment]
    _installed = True
    LOGGER.info("chat/user info cache installed (FLOOD_WAIT guard)")
