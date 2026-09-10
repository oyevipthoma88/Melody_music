"""
👋 Welcome / Goodbye (greetings) storage — MongoDB backed, reboot proof.

Rose-bot style per-group greeting configuration lives here so /setwelcome,
/welcome off, /cleanwelcome … survive dyno restarts and redeploys.

Every value is per chat_id. Reads go through a short process-local TTL cache
because the new-member handler runs on a hot path (mass joins / raids).

Keys
----
welcome        bool  — send a welcome when someone joins            (default on)
welcome_text   str   — custom text with {placeholders}              (None = theme default)
welcome_media  str   — Telegram file_id of a custom welcome photo   (None = generated card)
welcome_card   bool  — render the generated Melody thumbnail        (default on)
clean_welcome  bool  — delete the previous welcome card             (default on)
clean_service  bool  — delete Telegram's "X joined" service message (default off)
last_welcome   int   — message id of the last welcome card (internal)
goodbye        bool  — send a goodbye when someone leaves           (default on)
goodbye_text   str   — custom goodbye text                          (None = theme default)
goodbye_card   bool  — render the generated goodbye thumbnail       (default on)
clean_goodbye  bool  — delete the previous goodbye card             (default on)
last_goodbye   int   — message id of the last goodbye card (internal)
"""
from __future__ import annotations

import time

from utils.database import db

greet_col = db["greetings"]

DEFAULTS: dict = {
    "welcome": True,
    "welcome_text": None,
    "welcome_media": None,
    "welcome_card": True,
    "clean_welcome": True,
    "clean_service": False,
    "goodbye": True,
    "goodbye_text": None,
    "goodbye_card": True,
    "clean_goodbye": True,
}

_CACHE_TTL = 20.0
_cache: dict[int, tuple[float, dict]] = {}


def _merge(doc: dict | None) -> dict:
    cfg = dict(DEFAULTS)
    for key, value in (doc or {}).items():
        if key in ("_id", "chat_id"):
            continue
        cfg[key] = value
    return cfg


async def get_greet(chat_id: int) -> dict:
    cached = _cache.get(chat_id)
    if cached and cached[0] > time.monotonic():
        return dict(cached[1])
    doc = await greet_col.find_one({"chat_id": chat_id})
    cfg = _merge(doc)
    _cache[chat_id] = (time.monotonic() + _CACHE_TTL, cfg)
    return dict(cfg)


async def set_greet(chat_id: int, key: str, value) -> None:
    await greet_col.update_one(
        {"chat_id": chat_id}, {"$set": {key: value}}, upsert=True
    )
    cached = _cache.get(chat_id)
    cfg = dict(cached[1]) if cached else dict(DEFAULTS)
    cfg[key] = value
    _cache[chat_id] = (time.monotonic() + _CACHE_TTL, cfg)


async def unset_greet(chat_id: int, keys: list[str]) -> None:
    await greet_col.update_one(
        {"chat_id": chat_id}, {"$unset": {k: "" for k in keys}}, upsert=True
    )
    _cache.pop(chat_id, None)


async def reset_greet(chat_id: int, kind: str = "all") -> None:
    """kind: 'welcome' | 'goodbye' | 'all' — back to the Melody defaults."""
    if kind == "welcome":
        keys = ["welcome_text", "welcome_media", "welcome_card", "clean_welcome",
                "clean_service", "welcome", "last_welcome"]
    elif kind == "goodbye":
        keys = ["goodbye_text", "goodbye_card", "clean_goodbye", "goodbye",
                "last_goodbye"]
    else:
        keys = list(DEFAULTS) + ["last_welcome", "last_goodbye"]
    await unset_greet(chat_id, keys)
