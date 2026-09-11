"""Local-only metadata cache compatibility helpers.

Audio/media persistence was intentionally removed. These two small helpers keep
recent YouTube metadata across a dyno process restart using /tmp only; they do
not connect to MongoDB and never store media bytes.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import sqlite3
import time
from typing import Any

from utils.database import db

# Optional persistent media index. Media bytes are only written when the caller
# explicitly enables its cache flag; metadata caching below remains independent.
song_cache_col = db["song_cache"]
_gridfs_upload_lock = asyncio.Lock()
# Stable public alias for tests and future cache writers.
lock = _gridfs_upload_lock

_DB_PATH = "/tmp/melody_metadata.sqlite3"


def _get_meta_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=2)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS metadata ("
        "key TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL)"
    )
    return conn


def get_persistent_meta(key: str, ttl: float = 259200.0) -> dict[str, Any] | None:
    if not key:
        return None
    try:
        with _get_meta_conn() as conn:
            row = conn.execute(
                "SELECT data, updated FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        if not row or time.time() - float(row[1]) > max(0.0, ttl):
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def put_persistent_meta(key: str, data: dict[str, Any]) -> None:
    if not key or not isinstance(data, dict):
        return
    try:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        with _get_meta_conn() as conn:
            conn.execute(
                "INSERT INTO metadata(key, data, updated) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                (key, encoded, time.time()),
            )
    except Exception:
        return


async def remember_song(video_id: str, video: bool, file_id: str) -> None:
    """Index a Telegram file id for optional restart-safe media restoration."""
    if not video_id or not file_id:
        return
    await song_cache_col.update_one(
        {"video_id": video_id, "video": bool(video)},
        {"$set": {"video_id": video_id, "video": bool(video), "file_id": file_id}},
        upsert=True,
    )


async def restore_song(client, video_id: str, video: bool) -> str | None:
    """Restore complete media through Telegram or GridFS using atomic rename."""
    doc = await song_cache_col.find_one(
        {"video_id": video_id, "video": bool(video)},
        {"_id": 0, "file_id": 1, "gridfs_id": 1, "size": 1},
    )
    if not doc:
        return None
    tag = "v" if video else "a"
    target = f"/tmp/melody_{video_id}_{tag}.cache"
    partial = f"{target}.restore.part"
    try:
        if doc.get("file_id") and client is not None:
            path = await client.download_media(doc["file_id"], file_name=partial)
            if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                os.replace(path, target)
                return target
        if doc.get("gridfs_id"):
            from bson import ObjectId
            from motor.motor_asyncio import AsyncIOMotorGridFSBucket
            bucket = AsyncIOMotorGridFSBucket(db, bucket_name="song_audio")
            stream = await bucket.open_download_stream(ObjectId(str(doc["gridfs_id"])))
            with open(partial, "wb") as handle:
                while True:
                    chunk = await stream.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            expected = int(doc.get("size") or 0)
            if os.path.isfile(partial) and os.path.getsize(partial) > 0 and (
                not expected or os.path.getsize(partial) == expected
            ):
                os.replace(partial, target)
                return target
    except Exception:
        return None
    finally:
        for candidate in glob.glob(f"{partial}*"):
            try:
                os.unlink(candidate)
            except OSError:
                pass
    return None


async def remember_completed_file(video_id: str, video: bool, filepath: str) -> bool:
    """Upload a bounded complete file to GridFS; never upload staging files."""
    if not filepath or not os.path.isfile(filepath):
        return False
    if any(token in os.path.basename(filepath).lower() for token in (".part", ".ytdl", ".temp", ".early")):
        return False
    try:
        size = os.path.getsize(filepath)
        max_mb = max(8, int(os.getenv("MONGO_SONG_CACHE_MB", "64")))
        if size <= 0 or size > max_mb * 1024 * 1024:
            return False
        from bson import ObjectId
        from motor.motor_asyncio import AsyncIOMotorGridFSBucket
        async with lock:
            bucket = AsyncIOMotorGridFSBucket(db, bucket_name="song_audio")
            old = await song_cache_col.find_one(
                {"video_id": video_id, "video": bool(video)},
                {"_id": 0, "gridfs_id": 1, "size": 1},
            )
            if old and old.get("gridfs_id") and int(old.get("size") or 0) == size:
                return True
            if old and old.get("gridfs_id"):
                try:
                    await bucket.delete(ObjectId(str(old["gridfs_id"])))
                except Exception:
                    pass
            upload = bucket.open_upload_stream(
                f"{video_id}_{'video' if video else 'audio'}",
                metadata={"video_id": video_id, "video": bool(video), "size": size},
            )
            with open(filepath, "rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    await upload.write(chunk)
            await upload.close()
            await song_cache_col.update_one(
                {"video_id": video_id, "video": bool(video)},
                {"$set": {"video_id": video_id, "video": bool(video), "gridfs_id": str(upload._id), "size": size, "cached_at": time.time()}},
                upsert=True,
            )
            return True
    except Exception:
        return False
