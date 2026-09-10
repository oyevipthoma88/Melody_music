"""
📡 /live · /stream · /m3u8 · /radio · /replay

Direct-stream support:
Melody accepts raw HLS/m3u8, internet-radio ICY and direct media URLs in
addition to searchable sources. The `/replay` command restarts the current
track from 00:00 when the source supports seeking.

Implementation note: the raw URL is stored as the Track's `video_id`. That is
deliberate — `resolve_stream_urls()` already accepts any http(s) source, and
`call._stream_track()` now detects a direct URL (`ytdl.is_direct_url`) and
skips the download fallback entirely, because a live stream never finishes
downloading.
"""
import asyncio
import html
import re

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from melody import bot
from melody.core.call import (
    abort_prejoin_if_idle,
    get_playback_position,
    play_stream,
    pre_join,
    seek_stream,
)
from melody.core.queue import Track, get_current
from melody.logging import log_activity
from utils.decorators import admin_or_auth, error_handler
from utils.formatters import quote_html
from utils.tasks import spawn

_URL_RE = re.compile(r"^https?://\S+$", re.I)

# A handful of well-known always-on stations so /radio is useful without the
# user having to hunt down a stream URL first.
RADIO_STATIONS = {
    "lofi": ("Lofi Girl Radio", "https://play.streamafrica.net/lofiradio"),
    "hindi": ("Hindi Bollywood FM", "https://stream.zeno.fm/0r0xa792kwzuv"),
    "punjabi": ("Punjabi Hits FM", "https://stream.zeno.fm/rz96psn13uhvv"),
    "bbc": ("BBC World Service", "https://stream.live.vc.bbcmedia.co.uk/bbc_world_service"),
    "chill": ("Chillhop Radio", "https://streams.fluxfm.de/Chillhop/mp3-320/streams.fluxfm.de/"),
}


def _station_list() -> str:
    return "\n".join(
        f"<code>/radio {key}</code> — 📻 {html.escape(name)}"
        for key, (name, _url) in RADIO_STATIONS.items()
    )


async def _start_live(client: Client, message: Message, url: str, title: str, video: bool):
    chat = message.chat
    user = message.from_user
    msg = await message.reply(
        quote_html("📡 <b>Live stream connect ho raha hai...</b>"),
        parse_mode=enums.ParseMode.HTML,
    )

    join_task = asyncio.create_task(pre_join(chat.id, video=video))

    track = Track(
        video_id=url,          # raw URL == the playable source (see module doc)
        title=title,
        duration=0,            # live: unknown/endless, so no progress bar
        stream_url=url,
        thumbnail="",
        uploader="Live Stream",
        requester_id=user.id if user else chat.id,
        requester_name=(user.first_name if user else (chat.title or "Channel")),
        requested_in=chat.id,
        video=video,
    )

    await join_task
    try:
        playing = await play_stream(chat.id, track, video=video)
    except Exception:
        await abort_prejoin_if_idle(chat.id)
        await msg.edit(
            quote_html(
                "❌ <b>Ye stream play nahi ho payi.</b>\n"
                "Link direct media/HLS ka hona chahiye (m3u8, mp3, aac, mp4)."
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    state = "▶️ <b>Live on air</b>" if playing else "📋 <b>Queue me add ho gaya</b>"
    await msg.edit(
        quote_html(
            f"{state}\n"
            f"📡 <b>{html.escape(title[:60])}</b>\n"
            f"🔗 <code>{html.escape(url[:80])}</code>"
        ),
        parse_mode=enums.ParseMode.HTML,
    )
    spawn(
        log_activity(f"📡 <b>Live stream</b>\n• Chat: <code>{chat.id}</code>\n• URL: <code>{html.escape(url[:90])}</code>")
    )


@bot.on_message(filters.command(["live", "stream", "m3u8", "livestream"]) & filters.group)
@error_handler
@admin_or_auth
async def live_cmd(client: Client, message: Message):
    args = message.command[1:]
    video = message.command[0].lower() in ("m3u8", "livestream") or "-v" in args
    args = [a for a in args if a != "-v"]
    url = args[0] if args else None

    if not url or not _URL_RE.match(url):
        await message.reply(
            quote_html(
                "<b>Usage:</b> <code>/live &lt;stream url&gt;</code>\n"
                "Example: <code>/live https://example.com/live.m3u8</code>\n\n"
                "<i>Video ke liye: /live &lt;url&gt; -v</i>"
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    title = " ".join(args[1:]).strip() or "Live Stream"
    await _start_live(client, message, url, title, video)


@bot.on_message(filters.command(["radio", "fm"]) & filters.group)
@error_handler
@admin_or_auth
async def radio_cmd(client: Client, message: Message):
    args = message.command[1:]
    if not args:
        await message.reply(
            quote_html(
                "<b>📻 Radio stations:</b>\n" + _station_list() +
                "\n\n<i>Ya seedha URL do: /radio https://.../stream</i>"
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    target = args[0]
    if _URL_RE.match(target):
        await _start_live(client, message, target, " ".join(args[1:]) or "Radio", False)
        return

    station = RADIO_STATIONS.get(target.lower())
    if not station:
        await message.reply(
            quote_html(
                f"❌ <b>“{html.escape(target[:30])}”</b> naam ka station nahi hai.\n\n" + _station_list()
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    name, url = station
    await _start_live(client, message, url, name, False)


@bot.on_message(filters.command(["replay", "restart"]) & filters.group)
@error_handler
@admin_or_auth
async def replay_cmd(client: Client, message: Message):
    """Restart the current track from the beginning."""
    track = get_current(message.chat.id)
    if not track:
        await message.reply(
            quote_html("❌ <b>Abhi kuch chal hi nahi raha.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    position = get_playback_position(message.chat.id) or 0
    try:
        await seek_stream(message.chat.id, 0)
    except RuntimeError:
        await message.reply(
            quote_html("❌ <b>Abhi kuch chal hi nahi raha.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return
    except Exception:
        await message.reply(
            quote_html("⚠️ <b>Replay fail — stream reload nahi ho payi.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    await message.reply(
        quote_html(
            f"🔄 <b>Replaying from start</b>\n"
            f"🎵 <code>{html.escape(track.title[:55])}</code>\n"
            f"⏱ <i>{position}s se wapas 0s</i>"
        ),
        parse_mode=enums.ParseMode.HTML,
    )
