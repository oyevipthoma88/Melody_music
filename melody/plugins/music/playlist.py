"""
📃 /playlist — queue an entire YouTube playlist in one go

Uses get_playlist_entries() (melody/core/ytdl.py, extract_flat metadata
only — no per-video download here) to pull up to MAX_PLAYLIST_TRACKS
entries, plays the first immediately (or queues it behind whatever's
already playing) and queues the rest in order.
"""
import asyncio
import html
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from melody.core.ytdl import get_playlist_entries
from melody.core.queue import set_last_user_track, Track, add_to_queue
from melody.core.call import play_stream, pre_join, abort_prejoin_if_idle
from melody.logging import log_activity
from utils.database import add_history
from utils.decorators import admin_or_auth, error_handler
from utils.formatters import quote_html
from utils.tasks import spawn

MAX_PLAYLIST_TRACKS = 50


@bot.on_message(filters.command("playlist") & filters.group)
@error_handler
@admin_or_auth
async def playlist_cmd(client: Client, message: Message):
    query = " ".join(message.command[1:]) if len(message.command) > 1 else None
    chat = message.chat
    user = message.from_user

    if not query:
        await message.reply(
            quote_html(
                "**Usage:** `/playlist <YouTube playlist URL>`\n"
                "Example: `/playlist https://youtube.com/playlist?list=XXXXXXXX`"
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    requester_id = user.id if user else chat.id
    requester_name = user.first_name if user else (chat.title or "Channel")

    msg = await message.reply(quote_html("📃 Loading playlist..."), parse_mode=enums.ParseMode.HTML)

    join_task = asyncio.create_task(pre_join(chat.id))
    entries = await get_playlist_entries(query, limit=MAX_PLAYLIST_TRACKS)

    if not entries:
        await join_task
        await abort_prejoin_if_idle(chat.id)
        await msg.edit(
            quote_html("❌ Playlist load nahi ho payi — link check karo ya baad me try karo."),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    queued = 0
    first_title = None
    first_playing = False

    for info in entries:
        track = Track(
            video_id=info["id"],
            title=info["title"],
            duration=info["duration"],
            stream_url="",
            thumbnail=info["thumbnail"],
            uploader=info["uploader"],
            requester_id=requester_id,
            requester_name=requester_name,
            requested_in=chat.id,
        )

        if queued == 0:
            await join_task
            first_playing = await play_stream(chat.id, track)
            first_title = info["title"]
        else:
            add_to_queue(chat.id, track)

        await add_history(chat.id, info["id"], info["title"])
        set_last_user_track(chat.id, info["id"])
        queued += 1

    if queued == 0:
        await msg.edit(
            quote_html("❌ Koi bhi track queue nahi ho payi."),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    status = "▶️ Now Playing + " if first_playing else "📋 Added "
    safe_first_title = html.escape((first_title or "")[:50])
    await msg.edit(
        quote_html(
            f"📃 **Playlist Queued**\n"
            f"{status}`{queued}` track(s) to the queue.\n"
            f"First: `{safe_first_title}`"
        ),
        parse_mode=enums.ParseMode.HTML,
    )

    spawn(log_activity(
        f"📃 <b>Playlist Queued</b>\n"
        f"• Tracks: <code>{queued}</code>\n"
        f"• By: {html.escape(requester_name or 'Unknown')} (<code>{requester_id}</code>)\n"
        f"• Chat: {html.escape(chat.title or 'Private')} (<code>{chat.id}</code>)"
    ))
