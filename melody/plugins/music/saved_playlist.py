"""
💾 Personal saved playlists — Melody personal playlist storage.

Top music bots let every user keep a private playlist that survives restarts
and can be queued in any group. Melody had only `/playlist <youtube url>`
(one-shot import), so the whole "save my songs" feature set was missing.

Commands
--------
/addplaylist            → save the currently playing track to your playlist
/addplaylist <query>    → search YouTube and save the first hit
/myplaylist  (/mypl)    → list your saved tracks
/delplaylist <n|all>    → remove one entry (1-based) or wipe the playlist
/playplaylist (/pl)     → queue your whole saved playlist in this chat

Storage lives in the existing Mongo globals document (`utils.database`), so no
new collection/migration is needed.
"""
import asyncio
import html

from pyrogram import Client, filters, enums
from pyrogram.types import Message

from melody import bot
from melody.core.call import play_stream, pre_join, abort_prejoin_if_idle
from melody.core.queue import Track, add_to_queue, get_current, set_last_user_track
from melody.core.ytdl import search_youtube
from melody.logging import log_activity
from utils.database import get_global, set_global
from utils.decorators import admin_or_auth, error_handler
from utils.formatters import format_duration, send_quote
from utils.tasks import spawn

MAX_SAVED_TRACKS = 30


def _key(user_id: int) -> str:
    return f"uplaylist_{user_id}"


async def _load(user_id: int) -> list:
    saved = await get_global(_key(user_id), [])
    return saved if isinstance(saved, list) else []


async def _save(user_id: int, items: list) -> None:
    await set_global(_key(user_id), items[:MAX_SAVED_TRACKS])


def _entry_from_track(track) -> dict:
    return {
        "video_id": track.video_id,
        "title": track.title,
        "duration": int(track.duration or 0),
        "thumbnail": track.thumbnail or "",
        "uploader": track.uploader or "",
    }


@bot.on_message(filters.command(["addplaylist", "addpl"]))
@error_handler
async def addplaylist_cmd(client: Client, message: Message):
    user = message.from_user
    if not user:
        await send_quote(message, "❌ <b>Anonymous admins ki apni playlist nahi ho sakti.</b>", client=client)
        return

    query = " ".join(message.command[1:]) if len(message.command) > 1 else ""
    entry = None

    if query:
        results = await search_youtube(query, limit=1)
        result = results[0] if results else None
        if result:
            entry = {
                "video_id": result.get("id") or result.get("video_id", ""),
                "title": result.get("title", "Unknown"),
                "duration": int(result.get("duration") or 0),
                "thumbnail": result.get("thumbnail", ""),
                "uploader": result.get("uploader", ""),
            }
    else:
        current = get_current(message.chat.id)
        if current:
            entry = _entry_from_track(current)

    if not entry or not entry.get("video_id"):
        await send_quote(
            message,
            "❌ <b>Kuch save karne ko nahi mila.</b>\n"
            "<b>Usage:</b> <code>/addplaylist</code> (current song) ya <code>/addplaylist &lt;song name&gt;</code>",
            client=client,
        )
        return

    saved = await _load(user.id)
    if any(item.get("video_id") == entry["video_id"] for item in saved):
        await send_quote(message, "ℹ️ <b>Ye gaana pehle se aapki playlist me hai.</b>", client=client)
        return
    if len(saved) >= MAX_SAVED_TRACKS:
        await send_quote(
            message,
            f"❌ <b>Playlist full hai ({MAX_SAVED_TRACKS} tracks)</b> — <code>/delplaylist &lt;n&gt;</code> se jagah banao.",
            client=client,
        )
        return

    saved.append(entry)
    await _save(user.id, saved)
    await send_quote(
        message,
        f"💾 <b>Saved:</b> <code>{html.escape(entry['title'][:50])}</code>\n"
        f"📃 Playlist: <code>{len(saved)}/{MAX_SAVED_TRACKS}</code>",
        client=client,
    )


@bot.on_message(filters.command(["myplaylist", "mypl"]))
@error_handler
async def myplaylist_cmd(client: Client, message: Message):
    user = message.from_user
    if not user:
        await send_quote(message, "❌ <b>Anonymous admins ki apni playlist nahi hoti.</b>", client=client)
        return

    saved = await _load(user.id)
    if not saved:
        await send_quote(
            message,
            "📃 <b>Aapki playlist khali hai.</b>\n<code>/addplaylist</code> se gaane add karo.",
            client=client,
        )
        return

    lines = [f"📃 <b>{html.escape(user.first_name)} ki Playlist</b> — <code>{len(saved)}</code> track(s)\n"]
    for index, item in enumerate(saved, start=1):
        lines.append(
            f"<code>{index}.</code> {html.escape(str(item.get('title', 'Unknown'))[:45])} "
            f"— <code>{format_duration(item.get('duration', 0))}</code>"
        )
    lines.append("\n<i>/playplaylist se poori playlist bajao · /delplaylist &lt;n&gt; se hatao</i>")
    await send_quote(message, "\n".join(lines), client=client)


@bot.on_message(filters.command(["delplaylist", "delpl"]))
@error_handler
async def delplaylist_cmd(client: Client, message: Message):
    user = message.from_user
    if not user:
        return

    saved = await _load(user.id)
    if not saved:
        await send_quote(message, "📃 <b>Playlist pehle se khali hai.</b>", client=client)
        return

    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if arg in ("all", "clear"):
        await _save(user.id, [])
        await send_quote(message, "🗑 <b>Poori playlist clear ho gayi.</b>", client=client)
        return

    if not arg.isdigit():
        await send_quote(
            message,
            "<b>Usage:</b> <code>/delplaylist &lt;number&gt;</code> ya <code>/delplaylist all</code>",
            client=client,
        )
        return

    position = int(arg)
    if position < 1 or position > len(saved):
        await send_quote(message, f"❌ <b>Playlist me sirf {len(saved)} track hain.</b>", client=client)
        return

    removed = saved.pop(position - 1)
    await _save(user.id, saved)
    await send_quote(
        message,
        f"🗑 <b>Removed:</b> <code>{html.escape(str(removed.get('title', ''))[:45])}</code>",
        client=client,
    )


@bot.on_message(filters.command(["playplaylist", "pl"]) & filters.group)
@error_handler
@admin_or_auth
async def playplaylist_cmd(client: Client, message: Message):
    user = message.from_user
    if not user:
        return

    saved = await _load(user.id)
    if not saved:
        await send_quote(
            message,
            "📃 <b>Aapki playlist khali hai.</b>\n<code>/addplaylist</code> se gaane add karo.",
            client=client,
        )
        return

    status = await message.reply(
        "📃 <b>Playlist queue ho rahi hai...</b>",
        parse_mode=enums.ParseMode.HTML,
    )

    join_task = asyncio.create_task(pre_join(message.chat.id))
    queued = 0
    first_playing = False

    for item in saved:
        track = Track(
            video_id=item.get("video_id", ""),
            title=item.get("title", "Unknown"),
            duration=int(item.get("duration") or 0),
            stream_url="",
            thumbnail=item.get("thumbnail", ""),
            uploader=item.get("uploader", ""),
            requester_id=user.id,
            requester_name=user.first_name,
            requested_in=message.chat.id,
        )
        if queued == 0:
            await join_task
            first_playing = await play_stream(message.chat.id, track)
        else:
            add_to_queue(message.chat.id, track)
        set_last_user_track(message.chat.id, track.video_id)
        queued += 1

    if queued == 0:
        await join_task
        await abort_prejoin_if_idle(message.chat.id)
        await status.edit("❌ <b>Playlist queue nahi ho payi.</b>", parse_mode=enums.ParseMode.HTML)
        return

    prefix = "▶️ Now Playing + " if first_playing else "📋 Added "
    await status.edit(
        f"📃 <b>Playlist Queued</b>\n{prefix}<code>{queued}</code> track(s).",
        parse_mode=enums.ParseMode.HTML,
    )
    spawn(log_activity(
        f"📃 <b>Saved playlist played</b>\n"
        f"• Tracks: <code>{queued}</code>\n"
        f"• By: {html.escape(user.first_name or 'Unknown')} (<code>{user.id}</code>)"
    ))
