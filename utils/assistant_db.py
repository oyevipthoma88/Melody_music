"""
🛡️ Owner Assistant configuration store (MongoDB, per chat).

Everything the owner-side security engine
(`melody/plugins/admin/owner_assistant.py`) reads lives here so limits set with
`/oaset` survive restarts.

Keys
----
enabled        bool  master switch                                (default on)
ban_limit      int   bans allowed inside ban_window               (default 3)
ban_window     int   seconds for the ban burst window             (default 5)
kick_limit     int   kicks allowed inside kick_window             (default 5)
kick_window    int   seconds                                      (default 10)
mute_limit     int   restrictions allowed inside mute_window      (default 5)
mute_window    int   seconds                                      (default 10)
action         str   demote | mute | both  (punishment)           (default both)
promote_guard  bool  bot-promoted admin promoting others → revoke (default on)
owner_shield   bool  owner / sudo / bot untouchable, auto-undo    (default on)
settings_guard bool  title / photo / permission change alerts     (default on)
audit          bool  DM every admin action to the group owner     (default off)
tag_owner      bool  tag the owner in the report card             (default on)
"""
from __future__ import annotations

import time

from utils.database import db

oa_col = db["ownerassist"]

DEFAULTS: dict = {
    "enabled": True,
    "ban_limit": 3,
    "ban_window": 5,
    "kick_limit": 5,
    "kick_window": 10,
    "mute_limit": 5,
    "mute_window": 10,
    "action": "both",
    "promote_guard": True,
    "owner_shield": True,
    "settings_guard": True,
    "audit": False,
    "tag_owner": True,
}

INT_KEYS = ("ban_limit", "ban_window", "kick_limit", "kick_window",
            "mute_limit", "mute_window")
BOOL_KEYS = ("enabled", "promote_guard", "owner_shield", "settings_guard",
             "audit", "tag_owner")

_CACHE_TTL = 20.0
_cache: dict[int, tuple[float, dict]] = {}


async def get_cfg(chat_id: int) -> dict:
    cached = _cache.get(chat_id)
    if cached and cached[0] > time.monotonic():
        return dict(cached[1])
    doc = await oa_col.find_one({"chat_id": chat_id}) or {}
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in doc:
            cfg[key] = doc[key]
    _cache[chat_id] = (time.monotonic() + _CACHE_TTL, cfg)
    return dict(cfg)


async def set_cfg(chat_id: int, key: str, value) -> None:
    await oa_col.update_one({"chat_id": chat_id}, {"$set": {key: value}}, upsert=True)
    cached = _cache.get(chat_id)
    cfg = dict(cached[1]) if cached else dict(DEFAULTS)
    cfg[key] = value
    _cache[chat_id] = (time.monotonic() + _CACHE_TTL, cfg)


async def reset_cfg(chat_id: int) -> None:
    await oa_col.delete_one({"chat_id": chat_id})
    _cache.pop(chat_id, None)
