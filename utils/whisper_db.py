"""
🤫 Whisper (secret message) storage — MongoDB.

REQUESTED: "Whisper bot ki trh apne bot me bhi whisper message add kr. Secret
message aur iska log gc me send karna."

Design merged from the three most-used open-source whisper bots on GitHub
(inline-query bots such as `whisper-bot` / `SecretMsgBot` / `WhisperRoBot`):

  • the secret text is NEVER put inside `callback_data` (64-byte limit and
    it would travel in clear text through every client) — only a short random
    token goes in the button, the text lives here in Mongo;
  • the whisper row is written while answering the inline query, so the
    callback can always find it even if Telegram delivers the chosen result
    a while later;
  • target may be a @username or a numeric id, resolved lazily when the
    receiver taps the button (so "@user has not started the bot" is fine);
  • rows expire automatically (TTL index) so old secrets do not pile up.
"""
from __future__ import annotations

import secrets
import time

from melody.logging import LOGGER
from utils.database import db

whisper_col = db["whispers"]

TTL_SECONDS = 48 * 3600      # a whisper is readable for 48h, then it expires
MAX_TEXT = 4000              # Telegram alert caps ~200 chars, message is bigger
_index_ready = False


async def _ensure_indexes() -> None:
    global _index_ready
    if _index_ready:
        return
    try:
        await whisper_col.create_index("created_at", expireAfterSeconds=TTL_SECONDS)
        await whisper_col.create_index("token", unique=True)
        _index_ready = True
        LOGGER.info("whisper_db: indexes ready (TTL %ss)", TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "whisper_db: index creation failed (%s: %s) — feature still works, "
            "old whispers just won't self-expire",
            type(exc).__name__, exc,
        )


def new_token() -> str:
    """Short, URL-safe, unguessable id that fits easily in callback_data."""
    return secrets.token_urlsafe(9)


async def save_whisper(
    token: str,
    sender_id: int,
    sender_name: str,
    text: str,
    target_id: "int | None" = None,
    target_username: "str | None" = None,
    target_name: "str | None" = None,
    everyone: bool = False,
    premium: bool = False,
) -> dict:
    await _ensure_indexes()
    doc = {
        "token": token,
        "sender_id": int(sender_id),
        "sender_name": sender_name or "User",
        "text": text[:MAX_TEXT],
        "target_id": int(target_id) if target_id else None,
        "target_username": (target_username or "").lstrip("@").lower() or None,
        "target_name": target_name or None,
        "created_at": time.time(),
        "opens": 0,
        "opened_at": None,
        # REQUESTED: "everyone" whispers can be opened by any random user, and
        # every reader's name is shown on the card; `premium` remembers whether
        # the sender is a Telegram Premium user so the card keeps the premium
        # emoji skin even after a restart.
        "everyone": bool(everyone),
        "premium": bool(premium),
        "readers": [],
    }
    await whisper_col.update_one({"token": token}, {"$set": doc}, upsert=True)
    LOGGER.info(
        "whisper_db: stored token=%s from %s(%s) for %s (%d chars)",
        token, doc["sender_name"], sender_id,
        doc["target_username"] or doc["target_id"] or "?", len(doc["text"]),
    )
    return doc


async def get_whisper(token: str) -> "dict | None":
    return await whisper_col.find_one({"token": token}, {"_id": 0})


async def mark_opened(token: str) -> int:
    """Count a successful read and return the new open count."""
    try:
        from pymongo import ReturnDocument

        doc = await whisper_col.find_one_and_update(
            {"token": token},
            {"$set": {"opened_at": time.time()}, "$inc": {"opens": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if doc:
            return int(doc.get("opens", 1))
    except Exception as exc:  # noqa: BLE001
        LOGGER.info(
            "whisper_db: atomic open-count failed for %s (%s: %s) — falling back",
            token, type(exc).__name__, exc,
        )
    await whisper_col.update_one(
        {"token": token},
        {"$set": {"opened_at": time.time()}, "$inc": {"opens": 1}},
    )
    fresh = await get_whisper(token)
    return int((fresh or {}).get("opens", 1))


async def bind_target(token: str, target_id: int) -> None:
    """Remember the resolved numeric id once we finally know it."""
    await whisper_col.update_one({"token": token}, {"$set": {"target_id": int(target_id)}})


async def delete_whisper(token: str) -> None:
    await whisper_col.delete_one({"token": token})


async def add_reader(token: str, user_id: int, name: str) -> "dict | None":
    """Record a read receipt (REQUESTED: "read kr bad uska name show hoga").

    Idempotent: the same user tapping twice is stored once, so the card never
    repeats a name. Returns the fresh document so the caller can re-render.
    """
    try:
        doc = await get_whisper(token)
        readers = list((doc or {}).get("readers") or [])
        if any(int(r.get("id", 0)) == int(user_id) for r in readers):
            return doc
        readers.append({"id": int(user_id), "name": name or "User", "at": time.time()})
        await whisper_col.update_one({"token": token}, {"$set": {"readers": readers}})
        return await get_whisper(token)
    except Exception as exc:  # noqa: BLE001
        LOGGER.info(
            "whisper_db: read receipt save failed for %s (%s: %s) — secret still delivered",
            token, type(exc).__name__, exc,
        )
        return await get_whisper(token)
