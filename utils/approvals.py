"""
✅ Approved-user whitelist (per chat, persistent).

WHY THIS EXISTS
----------------
Reported: "Approve ka option add kr (/approve, /unapprove, /approveall,
/unapproveall) — krte hi bot user ko ignore karega, mtlb uska kuch content
delete nahi karega."

Every content filter in the bot (group protection, safe mode, NSFW media
guard, link/abuse filters) now asks `is_approved(chat_id, user_id)` FIRST and
returns immediately when it is True — so an approved user's messages are
never scanned, never deleted and never punished.

Storage
-------
`approvals` collection, one document per chat:

    {"chat_id": -100…, "users": [id, …], "all": false}

`all: true` is the /approveall switch — the whole chat is ignored by every
filter until /unapproveall.

A tiny in-memory cache keeps the hot path (every single group message) off
Mongo; it is invalidated on every write, so /approve takes effect instantly.
"""
from __future__ import annotations

import logging

from utils.database import db

log = logging.getLogger(__name__)

approve_col = db["approvals"]

# chat_id → (users:set, all:bool)
_cache: dict[int, tuple[set, bool]] = {}


async def _load(chat_id: int) -> tuple[set, bool]:
    cached = _cache.get(chat_id)
    if cached is not None:
        return cached
    try:
        doc = await approve_col.find_one({"chat_id": chat_id}) or {}
    except Exception as exc:  # DB hiccup must never block message handling
        log.warning("approvals: load failed for %s: %s", chat_id, exc)
        return set(), False
    state = ({int(u) for u in (doc.get("users") or [])}, bool(doc.get("all")))
    _cache[chat_id] = state
    return state


def _invalidate(chat_id: int) -> None:
    _cache.pop(chat_id, None)


async def is_approved(chat_id: int, user_id: int | None) -> bool:
    """True when this user's content must be ignored by every filter."""
    if not user_id:
        return False
    users, everyone = await _load(chat_id)
    return everyone or int(user_id) in users


async def approve_user(chat_id: int, user_id: int) -> bool:
    """Returns False when the user was already approved."""
    users, _ = await _load(chat_id)
    if int(user_id) in users:
        return False
    await approve_col.update_one(
        {"chat_id": chat_id},
        {"$addToSet": {"users": int(user_id)}, "$setOnInsert": {"chat_id": chat_id}},
        upsert=True,
    )
    _invalidate(chat_id)
    return True


async def unapprove_user(chat_id: int, user_id: int) -> bool:
    users, _ = await _load(chat_id)
    if int(user_id) not in users:
        return False
    await approve_col.update_one(
        {"chat_id": chat_id}, {"$pull": {"users": int(user_id)}}, upsert=True
    )
    _invalidate(chat_id)
    return True


async def approve_all(chat_id: int) -> None:
    await approve_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "all": True}},
        upsert=True,
    )
    _invalidate(chat_id)


async def unapprove_all(chat_id: int, clear_users: bool = True) -> None:
    """Turn the chat-wide switch off. `clear_users=True` (the /unapproveall
    behaviour) also empties the individual whitelist, so one command really
    does put the whole group back under the filters."""
    update = {"$set": {"chat_id": chat_id, "all": False}}
    if clear_users:
        update["$set"]["users"] = []
    await approve_col.update_one({"chat_id": chat_id}, update, upsert=True)
    _invalidate(chat_id)


async def approved_list(chat_id: int) -> tuple[list, bool]:
    users, everyone = await _load(chat_id)
    return sorted(users), everyone
