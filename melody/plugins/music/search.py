"""
🔍 /search command — inline YouTube search results
"""
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup
from utils.buttons import ikb  # premium-emoji + styled buttons (safe on every fork)
from melody import bot
from melody.core.ytdl import search_youtube
from utils.decorators import error_handler
from utils.formatters import format_duration, quote_html
from strings.themes import BLUE, RED, GREEN, btn


@bot.on_message(filters.command("search") & filters.group)
@error_handler
async def search_cmd(client: Client, message: Message):
    query = " ".join(message.command[1:])
    if not query:
        await message.reply(quote_html("**Usage:** `/search <song name>`"), parse_mode=enums.ParseMode.HTML)
        return

    msg = await message.reply(quote_html("🔍 Searching YouTube..."), parse_mode=enums.ParseMode.HTML)
    results = await search_youtube(query, limit=5)

    if not results:
        await msg.edit(quote_html("❌ No results found."), parse_mode=enums.ParseMode.HTML)
        return

    text = "**🔍 Search Results:**\n\n"
    colors = [RED, BLUE, GREEN]
    buttons = []
    for i, r in enumerate(results[:5], 1):
        dur = format_duration(r["duration"]) if r["duration"] else "?"
        text += f"`{i}.` **{r['title'][:45]}**\n   👤 {r['uploader']}  ⏱ {dur}\n\n"
        buttons.append([
            ikb(
                btn(f"{i}. {r['title'][:30]}", colors[(i - 1) % len(colors)]),
                callback_data=f"play_search_{r['id']}"
            )
        ])

    await msg.edit(quote_html(text), parse_mode=enums.ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


@bot.on_callback_query(filters.regex(r"^play_search_(.+)$"))
@error_handler
async def play_search_cb(client: Client, cb):
    from melody.core.ytdl import get_video_info, resolve_stream_urls
    from melody.core.queue import set_last_user_track, Track
    from melody.core.call import (
        play_stream, ensure_assistant_peer, pre_join, reset_playback_speed,
    )
    from utils.formatters import format_duration
    from utils.database import add_history
    from utils.decorators import cb_playmode_gate
    from utils.tasks import spawn

    chat = cb.message.chat
    user = cb.from_user

    # SECURITY FIX: search buttons had no permission check. Honour the same
    # everyone-vs-admin playmode and ban policy as the /play command.
    if not await cb_playmode_gate(client, cb, chat.id):
        return

    video_id = cb.data.split("play_search_")[1]
    reset_playback_speed(chat.id)
    await cb.answer("🎵 Loading...")

    # Search selections used to start direct resolution only after metadata
    # finished, leaving the first playback path with a 4–8s cold resolve.
    # Start both cheap preparations immediately; resolve_stream_urls has a
    # single-flight cache, so play_stream reuses the result instead of doing a
    # second network request.
    import asyncio
    warm_stream = asyncio.create_task(
        resolve_stream_urls(video_id, want_video=False),
        name=f"search-direct-warm-{video_id}",
    )
    spawn(ensure_assistant_peer(chat.id), name=f"search-peer-ready-{chat.id}")
    spawn(pre_join(chat.id), name=f"search-prejoin-{chat.id}")

    info = await get_video_info(f"https://www.youtube.com/watch?v={video_id}")
    # The warm resolver is strictly speculative. Never await it here: a
    # blocked YouTube client must not hold the search-button playback path
    # hostage. play_stream() has the authoritative direct/download race and
    # will reuse the resolver result if it wins; consume the task outcome so a
    # rejected speculative resolve cannot become an unhandled-task warning.
    def _consume_warm_result(task):
        try:
            task.result()
        except BaseException:
            pass

    warm_stream.add_done_callback(_consume_warm_result)
    if not info:
        await cb.answer("❌ Could not load song.", show_alert=True)
        return

    track = Track(
        video_id=info["id"],
        title=info["title"],
        duration=info["duration"],
        stream_url=info["stream_url"],
        thumbnail=info["thumbnail"],
        uploader=info["uploader"],
        requester_id=user.id,
        requester_name=user.first_name,
        requested_in=chat.id,
    )

    playing = await play_stream(chat.id, track)
    await add_history(chat.id, info["id"], info["title"])
    set_last_user_track(chat.id, info["id"])

    status = "▶️ Now Playing" if playing else "📋 Added to Queue"
    await cb.message.reply(
        quote_html(f"🎵 **{status}**\n`{info['title'][:50]}`\n⏱ `{format_duration(info['duration'])}`"),
        parse_mode=enums.ParseMode.HTML,
    )
