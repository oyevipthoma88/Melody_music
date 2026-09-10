"""Persistent economy state for Melody's lightweight group game.

The economy is intentionally non-gambling: users earn coins through daily and
work actions, can move coins between wallet and bank, transfer them to another
member, and compete on a leaderboard. All state lives in MongoDB so a dyno
restart does not reset progress.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from pymongo import ReturnDocument

from utils.database import db


economy_col = db["economy_users"]
_STARTING_WALLET = 250
_DAILY_COOLDOWN = 20 * 60 * 60
_WORK_COOLDOWN = 45
_user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def _now() -> float:
    return time.time()


def _display_name(name: str | None) -> str:
    return (name or "Player").strip()[:80] or "Player"


async def ensure_account(user_id: int, name: str = "") -> dict[str, Any]:
    doc = await economy_col.find_one_and_update(
        {"user_id": int(user_id)},
        {
            "$set": {"name": _display_name(name), "updated_at": datetime.now(timezone.utc)},
            "$setOnInsert": {
                "user_id": int(user_id),
                "wallet": _STARTING_WALLET,
                "bank": 0,
                "xp": 0,
                "level": 1,
                "streak": 0,
                "last_daily": 0.0,
                "last_work": 0.0,
                "created_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc or {"user_id": int(user_id), "wallet": _STARTING_WALLET, "bank": 0, "xp": 0, "level": 1}


def _level_for_xp(xp: int) -> int:
    return max(1, min(100, int(max(0, xp) ** 0.5 / 3) + 1))


async def _save_progress(user_id: int, *, xp_delta: int = 0, **changes: Any) -> dict[str, Any]:
    update: dict[str, Any] = {"$set": {**changes, "updated_at": datetime.now(timezone.utc)}}
    if xp_delta:
        update["$inc"] = {"xp": int(xp_delta)}
    doc = await economy_col.find_one_and_update(
        {"user_id": int(user_id)}, update,
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        return await ensure_account(user_id)
    xp = int(doc.get("xp") or 0)
    level = _level_for_xp(xp)
    if int(doc.get("level") or 1) != level:
        doc = await economy_col.find_one_and_update(
            {"user_id": int(user_id)},
            {"$set": {"level": level, "updated_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        ) or doc
    return doc


async def profile(user_id: int, name: str = "") -> dict[str, Any]:
    return await ensure_account(user_id, name)


async def claim_daily(user_id: int, name: str = "") -> tuple[bool, int, int, dict[str, Any]]:
    async with _user_locks[int(user_id)]:
        doc = await ensure_account(user_id, name)
        now = _now()
        last = float(doc.get("last_daily") or 0)
        if last and now - last < _DAILY_COOLDOWN:
            return False, 0, int(_DAILY_COOLDOWN - (now - last)), doc
        streak = int(doc.get("streak") or 0) + 1 if last and now - last < 48 * 60 * 60 else 1
        reward = random.randint(180, 320) + min(streak, 7) * 20
        doc = await economy_col.find_one_and_update(
            {"user_id": int(user_id)},
            {
                "$inc": {"wallet": reward, "xp": 25},
                "$set": {"last_daily": now, "streak": streak, "updated_at": datetime.now(timezone.utc)},
            },
            return_document=ReturnDocument.AFTER,
        ) or doc
        doc["level"] = _level_for_xp(int(doc.get("xp") or 0))
        return True, reward, streak, doc


async def work(user_id: int, name: str = "") -> tuple[bool, int, int, dict[str, Any]]:
    async with _user_locks[int(user_id)]:
        doc = await ensure_account(user_id, name)
        now = _now()
        last = float(doc.get("last_work") or 0)
        if last and now - last < _WORK_COOLDOWN:
            return False, 0, int(_WORK_COOLDOWN - (now - last)), doc
        reward = random.randint(45, 120) + int(doc.get("level") or 1) * 5
        doc = await economy_col.find_one_and_update(
            {"user_id": int(user_id)},
            {
                "$inc": {"wallet": reward, "xp": 10},
                "$set": {"last_work": now, "updated_at": datetime.now(timezone.utc)},
            },
            return_document=ReturnDocument.AFTER,
        ) or doc
        doc["level"] = _level_for_xp(int(doc.get("xp") or 0))
        return True, reward, 0, doc


async def move_money(user_id: int, amount: int, source: str, target: str) -> tuple[bool, dict[str, Any]]:
    amount = max(1, int(amount))
    async with _user_locks[int(user_id)]:
        await ensure_account(user_id)
        doc = await economy_col.find_one_and_update(
            {"user_id": int(user_id), source: {"$gte": amount}},
            {"$inc": {source: -amount, target: amount}, "$set": {"updated_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )
        return bool(doc), doc or await ensure_account(user_id)


async def transfer(from_id: int, to_id: int, amount: int, from_name: str = "", to_name: str = "") -> tuple[bool, str, dict[str, Any] | None]:
    amount = max(1, int(amount))
    if int(from_id) == int(to_id):
        return False, "Khud ko coins transfer nahi kar sakte.", None
    first, second = sorted((int(from_id), int(to_id)))
    async with _user_locks[first]:
        async with _user_locks[second]:
            await ensure_account(from_id, from_name)
            await ensure_account(to_id, to_name)
            sender = await economy_col.find_one_and_update(
                {"user_id": int(from_id), "wallet": {"$gte": amount}},
                {"$inc": {"wallet": -amount}, "$set": {"updated_at": datetime.now(timezone.utc)}},
                return_document=ReturnDocument.AFTER,
            )
            if not sender:
                return False, "Wallet me itne coins nahi hain.", None
            receiver = await economy_col.find_one_and_update(
                {"user_id": int(to_id)},
                {"$inc": {"wallet": amount}, "$set": {"updated_at": datetime.now(timezone.utc)}},
                return_document=ReturnDocument.AFTER,
            )
            if not receiver:
                await economy_col.update_one({"user_id": int(from_id)}, {"$inc": {"wallet": amount}})
                return False, "Receiver account update nahi ho saka.", None
            return True, "", receiver


async def leaderboard(limit: int = 10) -> list[dict[str, Any]]:
    cursor = economy_col.find(
        {}, {"_id": 0, "user_id": 1, "name": 1, "wallet": 1, "bank": 1, "level": 1, "xp": 1}
    ).sort([("wallet", -1), ("bank", -1)]).limit(max(1, min(25, int(limit))))
    return await cursor.to_list(length=max(1, min(25, int(limit))))
