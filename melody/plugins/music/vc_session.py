"""
🛰 VC session commands — Melody voice-chat session controls.

Missing before this module:

  /userbotjoin  (/joinvc)   → assistant ko manually VC me bulao
  /userbotleave (/leavevc)  → assistant ko VC se nikalo
  /autoend on|off           → VC me koi na bache to stream khud band

Autoend is the one every top bot ships and Melody had no equivalent of: if
everyone leaves the voice chat the stream used to keep running (and keep
burning bandwidth) until an admin noticed. The watcher below polls the roster
of chats that are actually streaming and stops playback after two consecutive
"only the assistant is left" checks.
"""
import asyncio
import html

from pyrogram import Client, filters
from pyrogram.types import Message

from melody import bot
from melody.core.call import (
    is_listener,
    join_as_listener,
    leave_listener,
    stop_stream,
)
from melody.core.queue import get_current
from melody.core.vc_notify import refresh_roster
from melody.logging import LOGGER, log_activity
from utils.database import get_setting, set_setting
from utils.decorators import admin_or_auth, error_handler
from utils.formatters import send_quote
from utils.tasks import spawn

_CHECK_INTERVAL = 45.0     # seconds between roster sweeps
_STRIKES_TO_END = 2        # consecutive empty checks before stopping
_strikes: dict[int, int] = {}
_watcher: "asyncio.Task | None" = None


# ─── /userbotjoin · /userbotleave ────────────────────────────────────────────

@bot.on_message(filters.command(["userbotjoin", "joinvc"]) & filters.group)
@error_handler
@admin_or_auth
async def userbotjoin_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    if is_listener(chat_id) or get_current(chat_id):
        await send_quote(message, "ℹ️ <b>Assistant pehle se VC me hai.</b>", client=client)
        return

    status = await message.reply("🛰 <b>Assistant ko VC me bula raha hoon...</b>")
    joined = await join_as_listener(chat_id)
    if joined:
        await status.edit("✅ <b>Assistant VC me join ho gaya.</b>")
        spawn(log_activity(
            f"🛰 <b>Assistant joined VC</b>\n• Chat: <code>{html.escape(message.chat.title or str(chat_id))}</code>"
        ))
    else:
        await status.edit(
            "❌ <b>Join nahi ho paya.</b> Check karo ki VC chalu hai aur assistant group me hai "
            "(<code>/invitelink</code> se add karo)."
        )


@bot.on_message(filters.command(["userbotleave", "leavevc"]) & filters.group)
@error_handler
@admin_or_auth
async def userbotleave_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    if get_current(chat_id):
        await stop_stream(chat_id)
    await leave_listener(chat_id)
    _strikes.pop(chat_id, None)
    await send_quote(message, "👋 <b>Assistant VC se nikal gaya.</b>", client=client)


# ─── /autoend ────────────────────────────────────────────────────────────────

async def autoend_enabled(chat_id: int) -> bool:
    return bool(await get_setting(chat_id, "autoend", False))


@bot.on_message(filters.command("autoend") & filters.group)
@error_handler
@admin_or_auth
async def autoend_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    arg = message.command[1].lower() if len(message.command) > 1 else ""

    if arg not in ("on", "off", "enable", "disable"):
        state = "ON ✅" if await autoend_enabled(chat_id) else "OFF ❌"
        await send_quote(
            message,
            f"⏳ <b>AutoEnd:</b> <code>{state}</code>\n"
            "<b>Usage:</b> <code>/autoend on</code> · <code>/autoend off</code>\n"
            "<i>ON hone par VC khali hote hi stream khud band ho jayega.</i>",
            client=client,
        )
        return

    enabled = arg in ("on", "enable")
    await set_setting(chat_id, "autoend", enabled)
    _strikes.pop(chat_id, None)
    if enabled:
        _ensure_watcher()
    await send_quote(
        message,
        f"⏳ <b>AutoEnd {'enabled ✅' if enabled else 'disabled ❌'}.</b>",
        client=client,
    )


# ─── watcher ─────────────────────────────────────────────────────────────────

async def _sweep_chat(chat_id: int) -> None:
    if not get_current(chat_id):
        _strikes.pop(chat_id, None)
        return
    if not await autoend_enabled(chat_id):
        _strikes.pop(chat_id, None)
        return

    roster = await refresh_roster(chat_id, max_age=_CHECK_INTERVAL / 2)
    # An empty roster usually means the lookup failed — don't end on that.
    if not roster or len(roster) > 1:
        _strikes.pop(chat_id, None)
        return

    strikes = _strikes.get(chat_id, 0) + 1
    _strikes[chat_id] = strikes
    if strikes < _STRIKES_TO_END:
        return

    _strikes.pop(chat_id, None)
    await stop_stream(chat_id)
    try:
        await bot.send_message(
            chat_id,
            "⏳ <b>AutoEnd:</b> VC me koi nahi bacha, isliye stream band kar diya.",
        )
    except Exception as exc:  # chat may have blocked the bot
        LOGGER.debug("autoend notice failed for %s: %s", chat_id, exc)
    spawn(log_activity(
        f"⏳ <b>AutoEnd stopped stream</b>\n• Chat: <code>{chat_id}</code>"
    ))


async def _watchdog() -> None:
    while True:
        await asyncio.sleep(_CHECK_INTERVAL)
        try:
            from melody.core.queue import _current  # live streaming chats

            for chat_id in list(_current.keys()):
                try:
                    await _sweep_chat(chat_id)
                except Exception as exc:
                    LOGGER.debug("autoend sweep failed for %s: %s", chat_id, exc)
        except Exception as exc:
            LOGGER.debug("autoend watchdog loop error: %s", exc)


def _ensure_watcher() -> None:
    global _watcher
    # The watcher is optional housekeeping, not part of the playback critical
    # path. Skip its always-on task in the lean music profile.
    try:
        from melody.config import Config
        if Config.MUSIC_ONLY_MODE:
            return
    except Exception:
        pass
    if _watcher and not _watcher.done():
        return
    try:
        _watcher = asyncio.get_running_loop().create_task(_watchdog())
    except RuntimeError:
        _watcher = None  # no loop yet — started on first /autoend


try:  # start with the bot when the event loop already exists at import time
    _ensure_watcher()
except Exception:  # pragma: no cover - defensive
    pass
