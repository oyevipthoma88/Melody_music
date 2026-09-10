"""
🗄️ Group-management / protection / settings storage (MongoDB).

Everything that must survive a reboot or a fresh Heroku deploy lives here
instead of in RAM:

  • sudo users              → `sudo`
  • per-chat warns          → `warns`
  • per-chat protection cfg → `protection`
  • users promoted BY the bot (needed by the anti-mass-ban watchdog)
                            → `botpromoted`
  • bot pictures (start / ping / alive / play) as Telegram file_ids
                            → `pics`
  • global toggles (random promo ads, etc.)
                            → `globals`
  • group → channel link used by /channelplay + /cplay
                            → `chanlink`

Storing pictures as Telegram `file_id`s (instead of files on disk) is what
makes "set once, never set again after a reboot" actually true: file_ids are
permanent for a given bot and can be re-sent forever without a re-upload,
and Mongo keeps them across dyno restarts.
"""
import time

from utils.database import db
from melody.core.settings_defaults import PROTECTION_DEFAULTS

sudo_col = db["sudo"]
warns_col = db["warns"]
protect_col = db["protection"]
botpromoted_col = db["botpromoted"]
pics_col = db["pics"]
globals_col = db["globals"]
chanlink_col = db["chanlink"]

# ─── Sudo users ──────────────────────────────────────────────────────────────

_sudo_cache: set = set()
_sudo_loaded = False


async def load_sudoers() -> set:
    """Load (and cache) sudo user ids. Called once at startup and refreshed
    on every add/remove so permission checks stay O(1) and never hit Mongo on
    a hot path."""
    global _sudo_cache, _sudo_loaded
    docs = await sudo_col.find({}, {"_id": 0, "user_id": 1}).to_list(None)
    _sudo_cache = {d["user_id"] for d in docs}
    _sudo_loaded = True
    return _sudo_cache


async def add_sudo(user_id: int, name: str = "") -> None:
    await sudo_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "name": name}},
        upsert=True,
    )
    await load_sudoers()


async def remove_sudo(user_id: int) -> None:
    await sudo_col.delete_one({"user_id": user_id})
    await load_sudoers()


async def get_sudoers() -> list:
    return await sudo_col.find({}, {"_id": 0}).to_list(None)


async def is_sudo(user_id: int) -> bool:
    from melody.config import Config

    if user_id == Config.OWNER_ID:
        return True
    if not _sudo_loaded:
        await load_sudoers()
    return user_id in _sudo_cache


# ─── Warns ───────────────────────────────────────────────────────────────────

async def add_warn(chat_id: int, user_id: int, reason: str = "") -> int:
    doc = await warns_col.find_one_and_update(
        {"chat_id": chat_id, "user_id": user_id},
        {"$inc": {"count": 1}, "$push": {"reasons": {"$each": [reason], "$slice": -10}}},
        upsert=True,
        return_document=True,
    )
    return (doc or {}).get("count", 1)


async def get_warns(chat_id: int, user_id: int) -> dict:
    doc = await warns_col.find_one({"chat_id": chat_id, "user_id": user_id})
    return doc or {"count": 0, "reasons": []}


async def reset_warns(chat_id: int, user_id: int) -> None:
    await warns_col.delete_one({"chat_id": chat_id, "user_id": user_id})


# ─── Protection settings ─────────────────────────────────────────────────────

DEFAULT_PROTECTION = dict(PROTECTION_DEFAULTS)


async def get_protection(chat_id: int) -> dict:
    doc = await protect_col.find_one({"chat_id": chat_id}) or {}
    cfg = dict(DEFAULT_PROTECTION)
    for key in cfg:
        if key in doc:
            cfg[key] = doc[key]
    return cfg


async def set_protection(chat_id: int, key: str, value) -> None:
    await protect_col.update_one(
        {"chat_id": chat_id}, {"$set": {key: value}}, upsert=True
    )


# ─── Users promoted BY the bot ───────────────────────────────────────────────

async def mark_bot_promoted(chat_id: int, user_id: int, by_id: int, full: bool = False) -> None:
    await botpromoted_col.update_one(
        {"chat_id": chat_id, "user_id": user_id},
        {"$set": {"chat_id": chat_id, "user_id": user_id, "by_id": by_id, "full": full}},
        upsert=True,
    )


async def unmark_bot_promoted(chat_id: int, user_id: int) -> None:
    await botpromoted_col.delete_one({"chat_id": chat_id, "user_id": user_id})


async def is_bot_promoted(chat_id: int, user_id: int) -> bool:
    return bool(await botpromoted_col.find_one({"chat_id": chat_id, "user_id": user_id}))


async def get_bot_promoted(chat_id: int, user_id: int) -> "dict | None":
    """The bot-promotion record (who promoted them, and with what rights).

    /demote needs the promoter's id so that "jise aapne bot ke through promote
    kiya hai, use aap demote kar sakte ho" can be honoured even when the
    demoter does not hold full rights.
    """
    return await botpromoted_col.find_one({"chat_id": chat_id, "user_id": user_id})


# ─── Pictures (file_id based, reboot-proof) ──────────────────────────────────

async def set_pic(key: str, file_id: str) -> None:
    await pics_col.update_one(
        {"key": key}, {"$set": {"key": key, "file_id": file_id}}, upsert=True
    )


async def get_pic(key: str) -> "str | None":
    doc = await pics_col.find_one({"key": key})
    return (doc or {}).get("file_id")


async def del_pic(key: str) -> None:
    await pics_col.delete_one({"key": key})


async def all_pics() -> dict:
    docs = await pics_col.find({}, {"_id": 0}).to_list(length=50)
    return {d["key"]: d["file_id"] for d in docs}


# ─── Global toggles ──────────────────────────────────────────────────────────

async def get_global(key: str, default=None):
    cached = _global_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    doc = await globals_col.find_one({"key": key})
    value = (doc or {}).get("value", default)
    _global_cache[key] = (time.monotonic() + _FLAG_CACHE_TTL, value)
    return value


async def set_global(key: str, value) -> None:
    await globals_col.update_one(
        {"key": key}, {"$set": {"key": key, "value": value}}, upsert=True
    )
    _global_cache[key] = (time.monotonic() + _FLAG_CACHE_TTL, value)


# ─── Group ⇄ Channel link (for /channelplay + /cplay) ────────────────────────

async def link_channel(chat_id: int, channel_id: int, channel_title: str = "") -> None:
    await chanlink_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "channel_id": channel_id, "title": channel_title}},
        upsert=True,
    )


async def unlink_channel(chat_id: int) -> None:
    await chanlink_col.delete_one({"chat_id": chat_id})


async def get_linked_channel(chat_id: int) -> "dict | None":
    return await chanlink_col.find_one({"chat_id": chat_id}, {"_id": 0})


# ─── Per-chat boolean flags (muteall, protection state, promo, …) ────────────

flags_col = db["gcflags"]

# These flags are read by several catch-all watchers on every group message.
# Hitting Mongo independently from muteall, safemode and promo made
# all plugin dispatch scale with database latency. A short process-local TTL
# keeps the hot path in memory while writes invalidate/update immediately.
_FLAG_CACHE_TTL = 15.0
_flag_cache: dict[int, tuple[float, dict]] = {}
_global_cache: dict[str, tuple[float, object]] = {}


def invalidate_flag_cache(chat_id: int | None = None) -> None:
    """Drop cached per-chat setting flags (used by /reload)."""
    if chat_id is None:
        _flag_cache.clear()
    else:
        _flag_cache.pop(chat_id, None)


async def set_setting_flag(chat_id: int, key: str, value) -> None:
    await flags_col.update_one({"chat_id": chat_id}, {"$set": {key: value}}, upsert=True)
    expires, values = _flag_cache.get(chat_id, (0.0, {}))
    values = dict(values)
    values[key] = value
    _flag_cache[chat_id] = (time.monotonic() + _FLAG_CACHE_TTL, values)


async def get_setting_flag(chat_id: int, key: str, default=None):
    cached = _flag_cache.get(chat_id)
    if cached and cached[0] > time.monotonic():
        return cached[1].get(key, default)
    doc = await flags_col.find_one({"chat_id": chat_id})
    values = dict(doc or {})
    _flag_cache[chat_id] = (time.monotonic() + _FLAG_CACHE_TTL, values)
    return values.get(key, default)
