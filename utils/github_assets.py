"""
📦 MongoDB-backed persistent assets (start pic, welcome-group pic, etc.)

WHY THIS EXISTS
════════════════
Heroku (and most PaaS dynos) wipe the filesystem on every restart/redeploy,
so anything saved only to the local `assets/` folder (e.g. via /setpic)
disappears the moment the dyno cycles.

This used to push each image as a commit to the bot's own GitHub repo. That
turned GitHub into a live database and got the account flagged for
automation, so storage now lives in MongoDB (the same MONGO_DB_URI the bot
already uses) — no commits, no API rate limits, no account risk.

Function names are unchanged so every existing call site keeps working.
Both save and restore are best-effort: any failure is logged, never fatal.
"""
import asyncio
import os
import pathlib

from melody.config import Config
from melody.logging import LOGGER

try:
    from utils.database import db
    _assets_col = db["assets"]
except Exception:  # database not configured
    _assets_col = None

_ASSET_DB_TIMEOUT = 8.0


def _enabled() -> bool:
    return bool(Config.MONGO_DB_URI) and _assets_col is not None


async def push_to_github(local_path: str, gh_path: str, commit_message: str = "") -> tuple[bool, str]:
    """Store local_path in MongoDB under the key gh_path.

    Returns (success, message). Signature kept for backwards compatibility.
    """
    if not _enabled():
        return False, "MONGO_DB_URI not set — skipping persistent asset save."
    if commit_message:
        LOGGER.debug("Asset persistence request: %s", commit_message)
    try:
        data = await asyncio.to_thread(pathlib.Path(local_path).read_bytes)
    except OSError as e:
        return False, f"Could not read local file: {e}"

    try:
        await asyncio.wait_for(
            _assets_col.update_one(
                {"_id": gh_path},
                {"$set": {"data": data, "name": os.path.basename(local_path)}},
                upsert=True,
            ),
            timeout=_ASSET_DB_TIMEOUT,
        )
        return True, "Image saved to database ✅"
    except Exception as exc:
        LOGGER.warning("Could not save %s to MongoDB: %s", gh_path, exc)
        return False, f"Database error: {exc}"


async def pull_from_github(local_path: str, gh_path: str) -> bool:
    """Restore gh_path from MongoDB into local_path. Returns True if written.

    Safe to call even when local_path already exists — used on startup to
    restore assets wiped by an ephemeral filesystem restart. Silent no-op when
    MongoDB isn't configured or the asset was never saved.
    """
    if not _enabled():
        return False
    try:
        doc = await asyncio.wait_for(
            _assets_col.find_one({"_id": gh_path}), timeout=_ASSET_DB_TIMEOUT
        )
        if not doc or not doc.get("data"):
            return False
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        await asyncio.to_thread(
            pathlib.Path(local_path).write_bytes, bytes(doc["data"])
        )
        return True
    except Exception as exc:
        LOGGER.warning("Could not restore %s from MongoDB: %s", gh_path, exc)
        return False


async def restore_persistent_assets(assets: list[tuple[str, str]]) -> None:
    """Restore every (local_path, key) pair that's missing locally.

    Call once on startup, before any handler needs these files, so a fresh
    ephemeral dyno gets back the images saved via /setpic and /setwelcomepic.
    """
    if not _enabled():
        return
    restored = 0
    for local_path, gh_path in assets:
        if os.path.exists(local_path):
            continue
        if await pull_from_github(local_path, gh_path):
            restored += 1
            LOGGER.info("Restored %s from database (%s)", local_path, gh_path)
    if restored:
        LOGGER.info("Restored %d persistent asset(s) from MongoDB.", restored)
