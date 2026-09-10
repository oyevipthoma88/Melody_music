"""Responsive, reboot-proof picture persistence for owner picture commands.

The Telegram ``file_id`` is the user-visible source of truth: it can be sent
again without downloading the image. Local/Mongo binary backup is best-effort
and deliberately runs after the command has acknowledged the owner.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from melody.logging import LOGGER
from utils.gc_db import set_pic
from utils.github_assets import push_to_github
from utils.tasks import spawn

_PICTURE_DB_TIMEOUT = 5.0
_PICTURE_DOWNLOAD_TIMEOUT = 25.0
_PICTURE_BACKUP_TIMEOUT = 10.0


async def save_picture_file_id(key: str, file_id: str) -> None:
    """Persist the Telegram file ID without allowing Mongo to hang a command."""
    await asyncio.wait_for(set_pic(key, file_id), timeout=_PICTURE_DB_TIMEOUT)


async def _backup_picture_asset(
    client: Any,
    *,
    key: str,
    file_id: str,
    local_path: str,
    mongo_key: str,
) -> None:
    """Download and persist the binary picture off the command critical path."""
    target = Path(local_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(
        f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    )
    try:
        downloaded = await asyncio.wait_for(
            client.download_media(file_id, file_name=str(tmp)),
            timeout=_PICTURE_DOWNLOAD_TIMEOUT,
        )
        source = Path(downloaded or tmp)
        if not source.is_file() or source.stat().st_size <= 0:
            raise OSError("Telegram returned no image data")
        os.replace(source, target)
        ok, detail = await asyncio.wait_for(
            push_to_github(str(target), mongo_key, f"Update {key} picture"),
            timeout=_PICTURE_BACKUP_TIMEOUT,
        )
        if not ok:
            LOGGER.warning("Picture %s binary backup skipped: %s", key, detail)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # background backup must never affect playback/commands
        LOGGER.warning("Picture %s binary backup failed: %s", key, exc)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def schedule_picture_backup(
    client: Any,
    *,
    key: str,
    file_id: str,
    local_path: str,
    mongo_key: str,
) -> None:
    """Schedule the optional binary backup and return immediately to the owner."""
    spawn(
        _backup_picture_asset(
            client,
            key=key,
            file_id=file_id,
            local_path=local_path,
            mongo_key=mongo_key,
        ),
        name=f"picture-backup:{key}",
    )
