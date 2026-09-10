"""
🤖 /autoplay command
BUG FIX: @error_handler moved OUTSIDE @admin_or_auth
"""
import html
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from melody.core.queue import set_autoplay, is_autoplay_on, get_current, add_to_queue, set_predownloaded
from melody.core.autoplay import prefetch_next
from melody.logging import log_activity
from utils.decorators import admin_or_auth, error_handler
from utils.tasks import spawn


@bot.on_message(filters.command("autoplay") & filters.group)
@error_handler
@admin_or_auth
async def autoplay_cmd(client: Client, message: Message):
    args = message.command
    chat = message.chat
    if len(args) < 2:
        status = await is_autoplay_on(chat.id)
        state = "<b>ON</b> 🟢" if status else "<b>OFF</b> 🔴"
        await message.reply(
            f"<blockquote>🤖 <b>AutoPlay:</b> {state}</blockquote>\n\n"
            "Use <code>/autoplay on</code> or <code>/autoplay off</code> to toggle.",
            parse_mode=enums.ParseMode.HTML,
        )
        return

    arg = args[1].lower()
    actor = message.from_user
    actor_name = html.escape(actor.first_name) if actor else "Someone"
    chat_name = html.escape(chat.title or str(chat.id))

    if arg == "on":
        await set_autoplay(chat.id, True)
        await message.reply(
            "<blockquote>🤖 <b>AutoPlay enabled!</b> 🟢</blockquote>\n\n"
            "Melody will keep the music going with related songs — the next "
            "one is being added to the queue and downloaded right now for "
            "zero-gap playback. 🎶",
            parse_mode=enums.ParseMode.HTML,
        )
        # REQUIREMENT: "jese hi on hoga next song queue me add + download krna"
        # — as soon as AutoPlay is turned on, immediately predict AND
        # download the next song, then drop it straight into the visible
        # queue (not just the internal pre-download cache) so /queue shows
        # it right away and it plays automatically the moment the current
        # song ends — no separate AutoPlay hand-off needed.
        if get_current(chat.id):
            async def _queue_next_autoplay_track():
                track = await prefetch_next(chat.id)
                if track:
                    add_to_queue(chat.id, track)
                    # BUG FIX ("autoplay on hota hai but uske baad kuch nahi
                    # hota hai"): this track is now tracked by the VISIBLE
                    # queue, so it will be consumed by pop_next() the normal
                    # way. If we leave it sitting in _predownloaded too,
                    # peek_predownloaded()/prefetch_next() keeps treating it
                    # as "already cached" forever (see prefetch_next's early
                    # `if cached: return cached`), so every future AutoPlay
                    # hop after this one just replays this exact same stale
                    # Track object instead of predicting a genuinely new
                    # song — AutoPlay looks like it silently stops advancing
                    # after the first track. Clearing it here makes the
                    # queue the single source of truth for this track, so
                    # the next real prefetch_next() call (triggered once
                    # this track actually starts playing) computes a fresh
                    # pick instead of returning a used-up reference.
                    set_predownloaded(chat.id, None)
            spawn(_queue_next_autoplay_track())
        spawn(log_activity(
            f"🤖 <b>AutoPlay Enabled</b>\n"
            f"• By: <code>{actor_name}</code> (<code>{actor.id if actor else '—'}</code>)\n"
            f"• Chat: <code>{chat_name}</code> (<code>{chat.id}</code>)"
        ))
    elif arg == "off":
        await set_autoplay(chat.id, False)
        await message.reply(
            "<blockquote>🤖 <b>AutoPlay disabled.</b> 🔴</blockquote>",
            parse_mode=enums.ParseMode.HTML,
        )
        spawn(log_activity(
            f"🤖 <b>AutoPlay Disabled</b>\n"
            f"• By: <code>{actor_name}</code> (<code>{actor.id if actor else '—'}</code>)\n"
            f"• Chat: <code>{chat_name}</code> (<code>{chat.id}</code>)"
        ))
    else:
        await message.reply(
            "<b>Usage:</b> <code>/autoplay on</code> or <code>/autoplay off</code>",
            parse_mode=enums.ParseMode.HTML,
        )
