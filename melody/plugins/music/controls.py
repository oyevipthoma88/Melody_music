"""
🎛 Playback controls — pause, resume, skip, stop + inline button handlers
BUG FIX: @error_handler moved OUTSIDE @admin_or_auth
"""
import html
import asyncio

# Keep Genius/ONNX work off asyncio's tiny default executor (see pools.py).
from melody.core.pools import IO_POOL
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery
from melody import bot
from melody.core.call import (
    pause_stream, resume_stream, skip_stream, stop_stream, force_play_stream,
)
from melody.core.queue import (
    format_queue, get_current, is_autoplay_on, set_autoplay,
    add_to_queue, set_predownloaded,
)
from melody.core.autoplay import prefetch_next
from melody.logging import LOGGER, log_activity
from utils.decorators import admin_or_auth, cb_admin_or_auth, error_handler
from utils.formatters import send_quote, premium_emoji, PREMIUM_EMOJI_IDS
from utils.thumbnails import get_bot_identity
from utils.tasks import spawn

# One in-flight skip per chat: rapid "/skip /skip /stop" sequences used to
# race (each skip fired its own task), which shuffled the queue order and
# made the bot look confused. A per-chat lock makes skips strictly sequential.
_skip_locks: dict[int, asyncio.Lock] = {}


def _skip_lock(chat_id: int) -> asyncio.Lock:
    lock = _skip_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _skip_locks[chat_id] = lock
    return lock


async def _do_skip(chat_id: int) -> None:
    async with _skip_lock(chat_id):
        await skip_stream(chat_id)


async def _finish_pause_resume(status, chat_id: int, resume: bool, client, message) -> None:
    try:
        ok = await (resume_stream(chat_id) if resume else pause_stream(chat_id))
        text = (
            "▶️ <b>Resumed.</b>" if ok and resume else
            "⏸ <b>Paused.</b>" if ok else
            "❌ <b>Nothing is paused right now.</b>" if resume else
            "❌ <b>Nothing is playing right now.</b>"
        )
        await send_quote(status, text, client=client, edit=True)
        if ok:
            actor, chat_name = _who(message)
            action = "Resumed" if resume else "Paused"
            glyph = "▶️" if resume else "⏸"
            spawn(log_activity(
                f"{glyph} <b>{action}</b>\n• By: <code>{actor}</code>\n• Chat: <code>{chat_name}</code>"
            ))
    except Exception as exc:
        try:
            await send_quote(
                status, "⚠️ <b>Playback control complete nahi ho paya.</b>",
                client=client, edit=True,
            )
        except Exception:
            pass
        LOGGER.debug("pause/resume background operation failed: %s", exc)


async def _callback_pause_resume(cb: CallbackQuery, resume: bool) -> None:
    try:
        ok = await (resume_stream(cb.message.chat.id) if resume else pause_stream(cb.message.chat.id))
        if not ok:
            await cb.message.reply(
                "❌ Nothing is paused right now." if resume else "❌ Nothing is playing right now."
            )
    except Exception:
        try:
            await cb.message.reply("⚠️ Playback control complete nahi ho paya.")
        except Exception:
            pass


_PAUSE = premium_emoji(PREMIUM_EMOJI_IDS["pause"], "⏸")
_RESUME = premium_emoji(PREMIUM_EMOJI_IDS["resume"], "▶️")
_SKIP = premium_emoji(PREMIUM_EMOJI_IDS["skip"], "⏭")
_STOP = premium_emoji(PREMIUM_EMOJI_IDS["stop"], "⏹")


def _who(message: Message) -> tuple[str, str]:
    """Return (actor_html, chat_html) for a rich activity-log line."""
    user = message.from_user
    actor = html.escape(user.first_name) if user else "Someone"
    chat_name = html.escape(message.chat.title or str(message.chat.id))
    return actor, chat_name


# ─── Message commands ─────────────────────────────────────────────────────────

@bot.on_message(filters.command("pause") & filters.group)
@error_handler
@admin_or_auth
async def pause_cmd(client: Client, message: Message):
    status = await send_quote(message, "⏳ <b>Pausing...</b>", client=client)
    spawn(
        _finish_pause_resume(status, message.chat.id, False, client, message),
        name=f"pause-{message.chat.id}",
    )


@bot.on_message(filters.command("resume") & filters.group)
@error_handler
@admin_or_auth
async def resume_cmd(client: Client, message: Message):
    status = await send_quote(message, "⏳ <b>Resuming...</b>", client=client)
    spawn(
        _finish_pause_resume(status, message.chat.id, True, client, message),
        name=f"resume-{message.chat.id}",
    )


@bot.on_message(filters.command(["skip", "s", "next"]) & filters.group)
@error_handler
@admin_or_auth
async def skip_cmd(client: Client, message: Message):
    from melody.core.queue import get_queue as _get_queue
    if not get_current(message.chat.id) and not _get_queue(message.chat.id):
        await send_quote(message, "❌ <b>Kuch chal hi nahi raha — skip karne ko kuch nahi hai.</b>", client=client)
        return

    # MISSING-FEATURE PARITY: `/skip <n>` jumps straight to queue position n
    # (Yukki/AnonXMusic/VIPMusic all support it). Everything before position n
    # is dropped, then the normal skip advances into it.
    if len(message.command) > 1:
        from melody.core.queue import remove_from_queue
        try:
            position = int(message.command[1])
        except ValueError:
            await send_quote(message, "❌ <b>Usage:</b> <code>/skip &lt;queue position&gt;</code>", client=client)
            return
        queue = _get_queue(message.chat.id)
        if position < 1 or position > len(queue):
            await send_quote(
                message,
                f"❌ <b>Queue me sirf {len(queue)} track hain — position {position} exist nahi karti.</b>",
                client=client,
            )
            return
        target = queue[position - 1]
        # Drop everything before the target so pop_next() lands exactly on it.
        for _ in range(position - 1):
            remove_from_queue(message.chat.id, 1)
        spawn(_do_skip(message.chat.id))
        await send_quote(
            message,
            f"{_SKIP} <b>Skipped to #{position}</b> — <code>{html.escape(target.title[:50])}</code>",
            client=client,
        )
        actor, chat_name = _who(message)
        spawn(log_activity(
            f"⏭ <b>Skipped to #{position}</b>\n• By: <code>{actor}</code>\n• Chat: <code>{chat_name}</code>"
        ))
        return
    # Confirm first, advance after: resolving the next track can take seconds
    # (download/stream setup), and waiting for it made /skip feel frozen.
    # The per-chat lock keeps rapid consecutive skips in order.
    spawn(_do_skip(message.chat.id))
    await send_quote(message, f"{_SKIP} <b>Skipped.</b>", client=client)
    actor, chat_name = _who(message)
    spawn(log_activity(f"⏭ <b>Skipped</b>\n• By: <code>{actor}</code>\n• Chat: <code>{chat_name}</code>"))


@bot.on_message(filters.command(["stop", "end"]) & filters.group)
@error_handler
@admin_or_auth
async def stop_cmd(client: Client, message: Message):
    # A stalled leave RPC must never delay the command acknowledgement. State
    # is cleared by stop_stream() in the background and its task is tracked.
    spawn(stop_stream(message.chat.id), name=f"stop-{message.chat.id}")
    await send_quote(message, f"{_STOP} <b>Music stopped and queue cleared.</b>", client=client)
    actor, chat_name = _who(message)
    spawn(log_activity(f"⏹ <b>Stopped</b>\n• By: <code>{actor}</code>\n• Chat: <code>{chat_name}</code>"))


# ─── Channel controls ───────────────────────────────────────────────────────
# Moved to melody/plugins/music/channel_controls.py, which serves the same
# /c* commands both inside a channel AND from a group linked via
# /channelplay. Keeping copies here would register duplicate handlers.


# ─── Inline button callbacks ──────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^pause$"))
@error_handler
async def pause_callback(client: Client, cb: CallbackQuery):
    if not await cb_admin_or_auth(client, cb):
        return
    await cb.answer("⏳ Pausing...")
    spawn(
        _callback_pause_resume(cb, False),
        name=f"button-pause-{cb.message.chat.id}",
    )


@bot.on_callback_query(filters.regex("^resume$"))
@error_handler
async def resume_callback(client: Client, cb: CallbackQuery):
    if not await cb_admin_or_auth(client, cb):
        return
    await cb.answer("⏳ Resuming...")
    spawn(
        _callback_pause_resume(cb, True),
        name=f"button-resume-{cb.message.chat.id}",
    )


@bot.on_callback_query(filters.regex("^skip$"))
@error_handler
async def skip_callback(client: Client, cb: CallbackQuery):
    if not await cb_admin_or_auth(client, cb):
        return
    await cb.answer("⏭ Skipped")
    spawn(_do_skip(cb.message.chat.id), name=f"skip-{cb.message.chat.id}")


@bot.on_callback_query(filters.regex("^stop$"))
@error_handler
async def stop_callback(client: Client, cb: CallbackQuery):
    if not await cb_admin_or_auth(client, cb):
        return
    await cb.answer("⏹ Stopped")
    spawn(stop_stream(cb.message.chat.id), name=f"stop-{cb.message.chat.id}")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@bot.on_callback_query(filters.regex("^close$"))
@error_handler
async def close_callback(client: Client, cb: CallbackQuery):
    """Close button — instantly deletes the play card from the chat."""
    try:
        await cb.message.delete()
    except Exception:
        pass
    # Delete FIRST: a stale/expired query id makes answer() fail (400
    # QUERY_ID_INVALID), which previously aborted the handler before the card
    # was ever removed — so Close looked broken. The answer is cosmetic.
    await cb.answer("✖ Closed")


@bot.on_callback_query(filters.regex("^queue$"))
@error_handler
async def queue_callback(client: Client, cb: CallbackQuery):
    await cb.answer()
    autoplay_on = await is_autoplay_on(cb.message.chat.id)
    text = format_queue(cb.message.chat.id, autoplay_on=autoplay_on)
    await send_quote(cb.message, text, client=client)


@bot.on_callback_query(filters.regex("^autoplay_toggle$"))
@error_handler
async def autoplay_toggle_callback(client: Client, cb: CallbackQuery):
    """Inline AutoPlay toggle button on the play card.

    REQUEST: "Play card buttons me add kr autoplay" — lets group members
    flip AutoPlay on/off directly from the play card instead of typing
    /autoplay on|off, and re-renders the card's buttons in place so the
    label always reflects the current state.
    """
    chat_id = cb.message.chat.id
    if not await cb_admin_or_auth(client, cb, chat_id):
        return
    new_state = not await is_autoplay_on(chat_id)
    await set_autoplay(chat_id, new_state)
    await cb.answer(f"🤖 AutoPlay {'ON 🟢' if new_state else 'OFF 🔴'}")

    if new_state and get_current(chat_id):
        async def _queue_next_autoplay_track():
            track = await prefetch_next(chat_id)
            if track:
                add_to_queue(chat_id, track)
                set_predownloaded(chat_id, None)
        spawn(_queue_next_autoplay_track())

    try:
        from melody.plugins.music.play import get_play_buttons
        bot_username, bot_name = await get_bot_identity(client)
        await cb.message.edit_reply_markup(
            reply_markup=get_play_buttons(
                cb.message.chat.title or "", new_state, bot_username, bot_name,
                cb.message.chat.type, chat_id=chat_id,
            )
        )
    except Exception:
        pass  # button label refresh is best-effort; the toggle itself already applied

    actor = html.escape(cb.from_user.first_name) if cb.from_user else "Someone"
    chat_name = html.escape(cb.message.chat.title or str(cb.message.chat.id))
    spawn(log_activity(
        f"🤖 <b>AutoPlay {'Enabled' if new_state else 'Disabled'} (via button)</b>\n"
        f"• By: <code>{actor}</code>\n• Chat: <code>{chat_name}</code>"
    ))


@bot.on_callback_query(filters.regex("^noop$"))
async def noop_callback(client: Client, cb: CallbackQuery):
    await cb.answer()


@bot.on_callback_query(filters.regex("^lyrics$"))
@error_handler
async def lyrics_callback(client: Client, cb: CallbackQuery):
    await cb.answer("🎵 Fetching lyrics...")
    track = get_current(cb.message.chat.id)
    if not track:
        await send_quote(cb.message, "❌ Nothing is playing right now.", client=client)
        return

    try:
        import lyricsgenius
        from melody.config import Config
        if not Config.GENIUS_API_TOKEN:
            await send_quote(cb.message, "⚠️ Genius API token not configured.", client=client)
            return

        genius = lyricsgenius.Genius(Config.GENIUS_API_TOKEN, verbose=False, remove_section_headers=True)
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        song = await loop.run_in_executor(IO_POOL, lambda: genius.search_song(track.title, track.uploader))
        if song and song.lyrics:
            safe_title = html.escape(track.title)
            lyrics_text = html.escape(song.lyrics[:3500])
            await send_quote(
                cb.message,
                f"🎵 <b>{safe_title}</b>\n\n<blockquote expandable>{lyrics_text}</blockquote>",
                client=client,
            )
        else:
            await send_quote(cb.message, "❌ Lyrics not found.", client=client)
    except Exception:
        await send_quote(cb.message, "❌ Could not fetch lyrics.", client=client)


# ─── AnonXMusic-style unified `controls <action> <chat_id>` router ────────────
#
# The new premium/coloured play card (utils/inline.py) sends one callback
# scheme for every transport button, carrying the target chat id so the card
# keeps working even when it was posted in a linked/channel chat. The old
# bare `pause`/`skip`/... callbacks above stay wired for cards that are still
# sitting in chat history from before the update.

@bot.on_callback_query(filters.regex(r"^controls "))
@error_handler
async def controls_router(client: Client, cb: CallbackQuery):
    parts = (cb.data or "").split()
    action = parts[1] if len(parts) > 1 else ""
    try:
        chat_id = int(parts[2])
    except (IndexError, ValueError):
        chat_id = cb.message.chat.id

    async def _refresh(paused: bool):
        """Re-render the card so ⏸ flips to ▶ (and back) in place."""
        try:
            from melody.plugins.music.play import get_play_buttons
            bot_username, bot_name = await get_bot_identity(client)
            await cb.message.edit_reply_markup(
                reply_markup=get_play_buttons(
                    cb.message.chat.title or "",
                    await is_autoplay_on(chat_id),
                    bot_username, bot_name, cb.message.chat.type,
                    chat_id=chat_id, paused=paused,
                )
            )
        except Exception:
            pass  # best-effort; the action itself already applied

    # SECURITY FIX: the target chat id arrives inside the callback data, so
    # without a check any user could pause/skip/stop playback in a chat they
    # are not even an admin of. "status" stays open (read-only).
    if action != "status" and not await cb_admin_or_auth(client, cb, chat_id):
        return

    if action == "status":
        track = get_current(chat_id)
        await cb.answer(
            f"🎵 {track.title[:60]}" if track else "❌ Nothing is playing right now.",
            show_alert=bool(track),
        )
        return

    if action == "pause":
        if not await pause_stream(chat_id):
            await cb.answer("❌ Nothing is playing right now.", show_alert=True)
            return
        await cb.answer("⏸ Paused")
        await _refresh(paused=True)
        return

    if action == "resume":
        if not await resume_stream(chat_id):
            await cb.answer("❌ Nothing is playing right now.", show_alert=True)
            return
        await cb.answer("▶️ Resumed")
        await _refresh(paused=False)
        return

    if action == "replay":
        track = get_current(chat_id)
        if not track:
            await cb.answer("❌ Nothing is playing right now.", show_alert=True)
            return
        await cb.answer("🔄 Replaying")
        async def _replay_and_report():
            ok = await force_play_stream(chat_id, track)
            if not ok:
                try:
                    await client.send_message(
                        chat_id,
                        "❌ Replay start nahi ho paya. VC permissions/session check karo.",
                        reply_to_message_id=cb.message.id,
                    )
                except Exception:
                    pass

        spawn(_replay_and_report(), name=f"replay-{chat_id}")
        return

    if action == "skip":
        await cb.answer("⏭ Skipped")
        spawn(skip_stream(chat_id), name=f"skip-{chat_id}")
        return

    if action == "stop":
        await cb.answer("⏹ Stopped")
        spawn(stop_stream(chat_id), name=f"stop-{chat_id}")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if action == "force":
        from melody.core.queue import get_queue
        item_id = parts[3] if len(parts) > 3 else ""
        target = None
        for item in list(get_queue(chat_id) or []):
            if str(getattr(item, "video_id", "")) == str(item_id):
                target = item
                break
        if target is None:
            await cb.answer("❌ Ye track ab queue me nahi hai.", show_alert=True)
            return
        await cb.answer("⚡ Playing now")
        async def _force_and_report():
            ok = await force_play_stream(chat_id, target)
            if not ok:
                try:
                    await client.send_message(
                        chat_id,
                        "❌ Force play start nahi ho paya. VC permissions/session check karo.",
                        reply_to_message_id=cb.message.id,
                    )
                except Exception:
                    pass

        spawn(_force_and_report(), name=f"force-{chat_id}")
        return

    await cb.answer()
