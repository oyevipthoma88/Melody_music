"""
🎶 Melody — Telegram Music Bot

FIX (Silent crash root cause):
    `python -m melody` runs this file BEFORE any code in __main__.py executes —
    which means BEFORE validate_config() can run. The old code created Pyrogram
    Client objects here at module import time. If STRING_SESSION is empty (""),
    Pyrogram raises `ValueError: Invalid session string` during import with zero
    log output — the process just dies silently.

    Fix: expose module-level `bot` and `assistant` as None initially.
    `create_clients()` in __main__.py sets them after validate_config() passes.
"""
import os

from pyrogram import Client, enums

from melody.logging import LOGGER


def _memory_limit_mb() -> int:
    for name in ("MEMORY_LIMIT_MB", "MEMORY_AVAILABLE_MB", "DYNO_RAM_MB"):
        raw = (os.getenv(name) or "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError:
                continue
            if value > 0:
                return value
    return 512


_CLIENT_MEMORY_LIMIT_MB = _memory_limit_mb()


def _worker_count(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, "") or 0)
    except ValueError:
        value = 0
    # On a small dyno, old Config Vars must not silently recreate the original
    # worker explosion. Larger deployments can still opt into the higher cap.
    hard_max = default if _CLIENT_MEMORY_LIMIT_MB <= 768 else maximum
    return max(1, min(hard_max, value or default))

# Set by create_clients() in __main__.py — always None until then.
bot: "Client | None" = None
assistant: "Client | None" = None

# False when the assistant STRING_SESSION could not log in (e.g. Telegram's
# 406 AUTH_KEY_DUPLICATED). The bot still serves text commands; anything that
# needs a voice chat checks this flag and tells the user what to fix instead of
# throwing a raw traceback.
ASSISTANT_READY: bool = True


def create_clients() -> tuple:
    """
    Instantiate both Pyrogram clients and store as module globals so every
    plugin can safely do `from melody import bot, assistant`.
    Must be called AFTER validate_config() succeeds in __main__.py.
    """
    global bot, assistant
    from melody.config import Config

    bot = Client(
        "MelodyBot",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.BOT_TOKEN,
        parse_mode=enums.ParseMode.HTML,
        # FloodWait handling — tuned from the Heroku logs.
        # Pyrogram "handles" a FloodWait by sleeping INSIDE the session, which
        # blocks every other request on that session. With sleep_threshold=60
        # a single 40s edit-flood froze the whole bot for 40s — that was the
        # real reason /play felt like it took forever. 15s keeps small bursts
        # invisible while long waits raise instead, so error_handler/safe_send
        # (and the animation's own FloodWait trip-switch) can drop the
        # non-essential request and keep playback responsive.
        sleep_threshold=15,
        # SPEED FIX ("cmnd bohot slow response deti hai"): Pyrogram dispatches
        # each update on one of `workers` tasks and runs that update's whole
        # handler chain there. The default (cpu+4 ≈ 6 on a Heroku dyno) meant a
        # handful of slow commands (/play, /broadcast, protection scans) could
        # occupy every worker and queue everything else behind them.
        # Keep the bot responsive without creating a large handler pool on a
        # 512 MB dyno. Slow handlers are already isolated by command locks and
        # background tasks; excess workers only increase RSS under bursts.
        workers=_worker_count("BOT_WORKERS", 8, 16),
        max_concurrent_transmissions=_worker_count("BOT_TRANSMISSIONS", 2, 4),
    )
    assistant = Client(
        "MelodyAssistant",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        session_string=Config.STRING_SESSION,
        sleep_threshold=15,
        workers=_worker_count("ASSISTANT_WORKERS", 4, 8),
        max_concurrent_transmissions=_worker_count("ASSISTANT_TRANSMISSIONS", 2, 4),
        parse_mode=enums.ParseMode.HTML,
    )

    # Premium emoji everywhere: patch Client.send_message/edit_message_text/
    # send_photo so every outgoing text/caption from either client is
    # automatically wrapped with real premium custom-emoji ids (see
    # utils/emoji_patch.py for why this is done centrally instead of
    # per-plugin).
    from utils.emoji_patch import apply_emoji_patch

    apply_emoji_patch()

    # Respond to commands aimed at ANY bot username (/help@otherbot) — see
    # utils/command_patch.py.
    from utils.command_patch import apply_command_patch

    apply_command_patch()

    # A tapped button's query id expires (and is single-use), so answering a
    # stale one raises 400 QUERY_ID_INVALID and used to abort the handler and
    # spam the error log — see utils/callback_patch.py.
    from utils.callback_patch import apply_callback_patch

    apply_callback_patch()

    # BUG FIX: the codebase fires ~60 `asyncio.create_task(...)` calls without
    # keeping a reference (log_activity, auto-delete, prefetch, watchdogs).
    # CPython only holds a WEAK reference to running tasks, so any of them can
    # be garbage-collected mid-await — symptom: log-channel messages, auto
    # deletes and prefetches randomly never happen. Keep a strong ref until
    # each task completes, and log the ones that die with an exception instead
    # of losing them to "Task exception was never retrieved".
    _keep_background_tasks_alive()

    return bot, assistant


_BG_TASKS: set = set()


def _keep_background_tasks_alive() -> None:
    import asyncio

    if getattr(asyncio.create_task, "_melody_patched", False):
        return
    original = asyncio.create_task

    def create_task(coro, **kwargs):
        task = original(coro, **kwargs)
        _BG_TASKS.add(task)

        def _done(t):
            _BG_TASKS.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is None:
                return
            # Cooperative yt-dlp preemption is expected when a newer manual
            # request takes over. It must not look like an unhandled worker
            # crash, while real background failures remain visible.
            try:
                from melody.core.ytdl import is_download_cancelled
                if is_download_cancelled(exc):
                    return
            except Exception:
                pass
            LOGGER.error("Background task failed: %s", exc)

        task.add_done_callback(_done)
        return task

    create_task._melody_patched = True
    asyncio.create_task = create_task

