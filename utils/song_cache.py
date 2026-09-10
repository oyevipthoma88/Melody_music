"""Local-only metadata cache compatibility helpers.

Audio/media persistence was intentionally removed. These two small helpers keep
recent YouTube metadata across a dyno process restart using /tmp only; they do
not connect to MongoDB and never store media bytes.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

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
