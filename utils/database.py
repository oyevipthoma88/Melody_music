"""
🗄️ MongoDB database utilities using Motor (async)
"""
import logging
import time
from urllib.parse import urlsplit


_SETTINGS_TTL = 45.0
_settings_cache: dict[tuple[int, str], tuple[float, object]] = {}

import motor.motor_asyncio
from melody.config import Config, _validate_mongo_uri

# Validate before Motor constructs a client so a bad Deploy Button value gives
# an actionable error instead of PyMongo's opaque InvalidURI traceback.
_mongo_uri = _validate_mongo_uri("MONGO_DB_URI", Config.MONGO_DB_URI)


def _mongo_target(uri: str) -> str:
    """Return only the credential-free hostname for startup diagnostics."""
    try:
        return urlsplit(uri).hostname or "<unknown>"
    except ValueError:
        return "<invalid>"


logging.getLogger("Melody").info(
    "Mongo runtime target: %s (database=%s)",
    _mongo_target(_mongo_uri),
    "MelodyDB",
)

# SPEED FIX ("cmnd bohot slow response deti hai"): the default Motor client
# opens a tiny pool and waits up to 30s before giving up on a slow Atlas node,
# so one stalled query could hold up every command behind it. A bigger pool +
# short timeouts keep the hot path snappy and fail fast instead of hanging.
client = motor.motor_asyncio.AsyncIOMotorClient(
    _mongo_uri,
    maxPoolSize=100,
    minPoolSize=8,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=15000,
    retryWrites=True,
    compressors="zlib",
)
db = client["MelodyDB"]

# Collections
chats_col = db["chats"]
users_col = db["users"]
banned_col = db["banned"]
gban_col = db["gban"]
auth_col = db["auth"]
history_col = db["history"]
settings_col = db["settings"]


# ─── Chat management ─────────────────────────────────────────────────────────

async def add_chat(chat_id: int, title: str = "", owner_id: int = None, owner_name: str = None):
    """
    `owner_id`/`owner_name` capture the user who ADDED the bot to this group
    — used for the "👑 My Cute Owner" welcome button (utils/database.py ->
    melody/plugins/misc/start.py). Only set on first insert so a later
    `add_chat()` call (e.g. from another handler) never overwrites the
    original adder with `None`.
    """
    update = {"$set": {"chat_id": chat_id, "title": title}}
    if owner_id is not None:
        update["$setOnInsert"] = {"owner_id": owner_id, "owner_name": owner_name or ""}
    await chats_col.update_one({"chat_id": chat_id}, update, upsert=True)


async def get_all_chats() -> list:
    return await chats_col.find({}, {"_id": 0, "chat_id": 1, "title": 1}).to_list(None)


async def get_chats_page(limit: int = 30, offset: int = 0) -> list:
    """Return a small deterministic chat page without an unbounded fetch."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    cursor = (
        chats_col.find({}, {"_id": 0, "chat_id": 1, "title": 1})
        .sort("chat_id", 1)
        .skip(offset)
        .limit(limit)
    )
    return await cursor.to_list(length=limit)


async def get_chat_count() -> int:
    """Return the chat count through Mongo's bounded count operation."""
    return int(await chats_col.count_documents({}))


async def get_chat_owner(chat_id: int) -> "dict | None":
    """Return {"owner_id", "owner_name"} for whoever added the bot to this
    group, or None if unknown (e.g. chat predates this feature)."""
    doc = await chats_col.find_one({"chat_id": chat_id}, {"_id": 0, "owner_id": 1, "owner_name": 1})
    if doc and doc.get("owner_id"):
        return doc
    return None


# ─── Auth management ─────────────────────────────────────────────────────────

# chat_id → (expiry_monotonic, [user_ids]). The auth list was read from Mongo
# on EVERY command by admin_or_auth — one guaranteed network round-trip before
# any command could even start. It changes only via /auth and /unauth, both of
# which refresh this cache, so a TTL cache is safe and removes that round-trip.
_auth_cache: dict = {}
_AUTH_TTL = 120  # seconds


def invalidate_auth_cache(chat_id: int | None = None) -> None:
    if chat_id is None:
        _auth_cache.clear()
    else:
        _auth_cache.pop(chat_id, None)


async def auth_user(chat_id: int, user_id: int):
    await auth_col.update_one(
        {"chat_id": chat_id},
        {"$addToSet": {"users": user_id}},
        upsert=True,
    )
    invalidate_auth_cache(chat_id)


async def unauth_user(chat_id: int, user_id: int):
    await auth_col.update_one(
        {"chat_id": chat_id},
        {"$pull": {"users": user_id}},
    )
    invalidate_auth_cache(chat_id)


async def get_auth_users(chat_id: int) -> list:
    cached = _auth_cache.get(chat_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    doc = await auth_col.find_one({"chat_id": chat_id})
    users = doc.get("users", []) if doc else []
    _auth_cache[chat_id] = (time.monotonic() + _AUTH_TTL, users)
    return users


# ─── Ban management ───────────────────────────────────────────────────────────

async def ban_user(user_id: int, reason: str = ""):
    await banned_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "reason": reason}},
        upsert=True,
    )


async def unban_user(user_id: int):
    await banned_col.delete_one({"user_id": user_id})


async def is_banned(user_id: int) -> bool:
    return bool(await banned_col.find_one({"user_id": user_id}))


# ─── Global ban management ────────────────────────────────────────────────────

async def gban_user(user_id: int, reason: str = ""):
    await gban_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "reason": reason}},
        upsert=True,
    )


async def ungban_user(user_id: int):
    await gban_col.delete_one({"user_id": user_id})


async def is_gbanned(user_id: int) -> bool:
    return bool(await gban_col.find_one({"user_id": user_id}))


# ─── Play history ─────────────────────────────────────────────────────────────

async def add_history(chat_id: int, video_id: str, title: str = ""):
    await history_col.update_one(
        {"chat_id": chat_id},
        {"$push": {"history": {"$each": [{"id": video_id, "title": title}], "$slice": -15}}},
        upsert=True,
    )


async def get_history(chat_id: int) -> list:
    doc = await history_col.find_one({"chat_id": chat_id})
    return doc.get("history", []) if doc else []


# ─── Chat settings (autoplay, loop, etc.) ────────────────────────────────────

async def get_setting(chat_id: int, key: str, default=None):
    """Read a setting with a short process-local cache for command hot paths."""
    cache_key = (int(chat_id), str(key))
    cached = _settings_cache.get(cache_key)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    doc = await settings_col.find_one({"chat_id": chat_id})
    value = doc.get(key, default) if doc else default
    _settings_cache[cache_key] = (now + _SETTINGS_TTL, value)
    return value


async def set_setting(chat_id: int, key: str, value):
    await settings_col.update_one(
        {"chat_id": chat_id},
        {"$set": {key: value}},
        upsert=True,
    )
    _settings_cache[(int(chat_id), str(key))] = (time.monotonic() + _SETTINGS_TTL, value)


# ─── Stats ────────────────────────────────────────────────────────────────────

async def get_stats() -> dict:
    total_chats = await chats_col.count_documents({})
    total_banned = await banned_col.count_documents({})
    total_gbanned = await gban_col.count_documents({})
    return {
        "chats": total_chats,
        "banned": total_banned,
        "gbanned": total_gbanned,
    }


# ─── Lists (gban / botban / blacklisted chats) ───────────────────────────────
# Top music bots (Yukki / AnonXMusic / VIPMusic) all expose the *list* form of
# every global action — /gbannedusers, /blockedusers, /blacklistedchats — but
# Melody only had the add/remove half, so an owner could gban someone and then
# have no way to audit or undo it without remembering the id. These read the
# same collections the existing setters already write to.

blacklist_col = db["blacklist_chats"]
globals_col = db["globals"]


async def get_gbanned_users() -> list:
    return await gban_col.find({}, {"_id": 0, "user_id": 1, "reason": 1}).to_list(None)


async def get_banned_users() -> list:
    return await banned_col.find({}, {"_id": 0, "user_id": 1, "reason": 1}).to_list(None)


async def blacklist_chat(chat_id: int, reason: str = ""):
    await blacklist_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "reason": reason}},
        upsert=True,
    )


async def whitelist_chat(chat_id: int):
    await blacklist_col.delete_one({"chat_id": chat_id})


async def is_blacklisted_chat(chat_id: int) -> bool:
    return bool(await blacklist_col.find_one({"chat_id": chat_id}))


async def get_blacklisted_chats() -> list:
    return await blacklist_col.find({}, {"_id": 0, "chat_id": 1, "reason": 1}).to_list(None)


# ─── Bot-wide (global) settings ──────────────────────────────────────────────

async def get_global(key: str, default=None):
    doc = await globals_col.find_one({"_id": "config"})
    if doc:
        return doc.get(key, default)
    return default


async def set_global(key: str, value):
    await globals_col.update_one({"_id": "config"}, {"$set": {key: value}}, upsert=True)
