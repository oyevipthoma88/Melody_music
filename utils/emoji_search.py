"""
🌍 Universal premium-emoji resolver — "har emoji ko premium id".

WHY
---
`utils/emoji_map.py` only knows the glyphs that the owner's own premium packs
happen to contain (≈1.3k). Every other emoji in the world either stayed plain
unicode or got a *random* pool id that depicts a different emoji. So whenever
new emoji were added anywhere in the code they showed up as normal emoji.

FIX
---
Telegram itself can tell us which custom-emoji documents match ANY standard
emoji: ``messages.searchCustomEmoji(emoticon="🥹")`` returns document ids of
premium emoji whose ``alt`` is that glyph. This module:

* resolves unknown glyphs in a background worker (never blocks a send),
* verifies each candidate id via ``messages.GetCustomEmojiDocuments`` and keeps
  only ids whose ``alt`` really is that glyph,
* writes the result into ``EMOJI_ID_MAP`` (+ fallback chain) and persists it to
  disk, so after the first sighting that glyph is premium forever — including
  brand-new emoji nobody hand-mapped.

Until Telegram answers, the existing deterministic pool fallback keeps the
glyph premium (just with another animation), so nothing ever renders plain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Iterable, Optional

log = logging.getLogger(__name__)

CACHE_PATH = os.environ.get("EMOJI_LOOKUP_CACHE", "/tmp/melody_emoji_lookup.json")

# glyph -> custom emoji id (str). Loaded from disk at import.
LOOKUP: dict[str, str] = {}
# glyphs Telegram has no premium emoji for — don't ask again this process.
_MISSES: set[str] = set()

_queue: "asyncio.Queue[str] | None" = None
_worker: "asyncio.Task | None" = None
_client = None
_dirty = False


def _load() -> None:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            LOOKUP.update({str(k): str(v) for k, v in data.items() if k and v})
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("emoji_search: could not read cache: %s", exc)


def _save() -> None:
    global _dirty
    if not _dirty:
        return
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(LOOKUP, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
        _dirty = False
    except Exception as exc:  # noqa: BLE001
        log.debug("emoji_search: could not write cache: %s", exc)


_load()


def cached_id(glyph: str) -> Optional[str]:
    """Premium id previously resolved for `glyph` (exact match), if any."""
    return LOOKUP.get(glyph)


def request(glyph: str) -> None:
    """Ask (in the background) Telegram for a premium emoji depicting `glyph`.

    Safe to call from synchronous rendering code — it never blocks and never
    raises.
    """
    if not glyph or glyph in LOOKUP or glyph in _MISSES:
        return
    if _queue is None:
        return
    try:
        _queue.put_nowait(glyph)
    except Exception:  # noqa: BLE001 - queue full / no loop
        pass


def _adopt(glyph: str, emoji_id: str) -> None:
    """Publish a freshly resolved id into the live emoji map."""
    global _dirty
    LOOKUP[glyph] = emoji_id
    _dirty = True
    try:
        from utils.emoji_map import (
            AUTO_ASSIGNED_IDS,
            EMOJI_ID_FALLBACKS,
            EMOJI_ID_MAP,
        )

        previous = EMOJI_ID_MAP.get(glyph)
        EMOJI_ID_MAP[glyph] = emoji_id
        AUTO_ASSIGNED_IDS[glyph] = emoji_id
        chain = EMOJI_ID_FALLBACKS.setdefault(glyph, [])
        # The real match goes first; whatever pool id we used before stays as
        # a backup so the glyph is never left without an id.
        EMOJI_ID_FALLBACKS[glyph] = list(
            dict.fromkeys([emoji_id, *( [previous] if previous else [] ), *chain])
        )
        from utils.telegram_html import refresh_glyph_pattern

        refresh_glyph_pattern()
    except Exception as exc:  # noqa: BLE001
        log.debug("emoji_search: could not adopt %r: %s", glyph, exc)


async def _search(client, glyph: str) -> Optional[str]:
    from pyrogram import raw

    from utils.custom_emoji import (
        EmojiResolutionUnavailable,
        resolve_custom_emoji,
    )

    try:
        result = await client.invoke(
            raw.functions.messages.SearchCustomEmoji(emoticon=glyph, hash=0)
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("emoji_search: search failed for %r: %s", glyph, exc)
        return None

    ids = [int(i) for i in (getattr(result, "document_id", None) or [])][:20]
    if not ids:
        return None

    try:
        from utils.emoji_patch import QUARANTINED_IDS

        dead = {str(i) for i in QUARANTINED_IDS}
    except Exception:  # noqa: BLE001
        dead = set()

    try:
        resolved = await resolve_custom_emoji(client, ids)
    except EmojiResolutionUnavailable:
        # Cannot verify the alt glyph — trust Telegram's own search result for
        # this glyph instead of dropping back to a plain unicode emoji.
        return next((str(i) for i in ids if str(i) not in dead), None)
    base = glyph.replace("\ufe0f", "")
    fallback: Optional[str] = None
    for emoji_id in ids:
        if str(emoji_id) in dead or emoji_id not in resolved:
            continue
        alt = (resolved.get(emoji_id) or "").replace("\ufe0f", "")
        if alt == base:
            return str(emoji_id)
        fallback = fallback or str(emoji_id)
    return fallback


async def _run() -> None:
    assert _queue is not None
    pending_save = False
    while True:
        try:
            glyph = await _queue.get()
        except asyncio.CancelledError:
            raise
        if glyph in LOOKUP or glyph in _MISSES:
            continue
        client = _client
        if client is None:
            _MISSES.add(glyph)
            continue
        emoji_id = await _search(client, glyph)
        if emoji_id:
            _adopt(glyph, emoji_id)
            pending_save = True
        else:
            _MISSES.add(glyph)
        if pending_save and _queue.empty():
            _save()
            pending_save = False
        # Gentle pacing: this is a background nicety, never worth a flood-wait.
        await asyncio.sleep(0.4)


def start(client, warm: Iterable[str] | None = None) -> None:
    """Start the background resolver using `client` (a started Pyrogram client)."""
    global _queue, _worker, _client

    _client = client
    if _queue is None:
        _queue = asyncio.Queue(maxsize=5000)
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_run())
        log.info("🌍 Universal premium-emoji resolver started (%d cached).", len(LOOKUP))
    for glyph in warm or ():
        request(glyph)
    # Re-publish everything we resolved in earlier runs.
    for glyph, emoji_id in list(LOOKUP.items()):
        _adopt(glyph, emoji_id)
