"""
🎴 Shared "now playing" card builder — used by both /play (manual) and
AutoPlay so every track that starts streaming gets the same rich card
(thumbnail + song info + control buttons) in the chat.

Extracted into its own module to avoid the circular import between
play.py → call.py → autoplay.py → play.py.
"""
import html
from pyrogram import Client, enums
from utils.reply import reply_params
from melody.config import Config
from melody.core.queue import is_autoplay_on
from utils.formatters import format_duration
from utils.thumbnails import make_thumbnail, get_bot_dp, fetch_dp, get_bot_identity


async def send_playing_card(
    client: Client,
    chat,
    track,
    *,
    status_label: str = "Now Playing",
    requester_mention: str | None = None,
    reply_to_message_id: int | None = None,
) -> None:

    """Send the full playing card (thumbnail + caption + buttons) to `chat`.

    Best-effort: if the group forbids photos or the thumbnail build fails,
    falls back to a plain text message with the same buttons — never raises.

    Parameters
    ----------
    track : Track
        The track that just started playing.
    status_label : str
        Shown bold at the top — "Now Playing", "AutoPlay ▶️", etc.
    requester_mention : str | None
        HTML-safe mention or name for the "Requested by" line. Falls back
        to the track's requester_name.
    reply_to_message_id : int | None
        If given, replies to that message (used by /play); None for AutoPlay.
    """
    # Last-resort metadata enrichment: AutoPlay picks come from YouTube's flat
    # "up next" feed, which sometimes has no uploader/duration at all — that's
    # why AutoPlay cards used to read "Unknown / 00:00" while manual /play
    # cards were complete. Fill the gaps here so EVERY card is full-detail.
    if track.title in ("", "Unknown") or not track.duration or track.uploader in ("", "Unknown"):
        try:
            from melody.core.ytdl import get_video_details
            details = await get_video_details(track.video_id)
            if details:
                if track.title in ("", "Unknown"):
                    track.title = details.get("title") or track.title
                if not track.duration:
                    track.duration = int(details.get("duration") or 0)
                if track.uploader in ("", "Unknown"):
                    track.uploader = details.get("uploader") or track.uploader
                if not track.thumbnail:
                    track.thumbnail = details.get("thumbnail") or track.thumbnail
        except Exception:
            pass

    safe_title    = html.escape(track.title[:50])
    safe_uploader = html.escape(track.uploader)
    safe_duration = html.escape(format_duration(track.duration))
    safe_group    = html.escape((chat.title or "Private")[:60])
    video_link    = f"https://www.youtube.com/watch?v={track.video_id}"

    who = requester_mention or html.escape(track.requester_name)
    caption = (
        f"<blockquote>🎶 <b>{html.escape(status_label)}</b>\n\n"
        f"🎧 <b>{safe_title}</b>\n"
        f"👤 <b>Aʀᴛɪꜱᴛ :</b> <code>{safe_uploader}</code>\n"
        f"⏱ <b>Dᴜʀᴀᴛɪᴏɴ :</b> <code>{safe_duration}</code>\n"
        f"🔗 <b>Lɪɴᴋ :</b> <a href=\"{video_link}\">YouTube</a>\n"
        f"🏠 <b>Cʜᴀᴛ :</b> {safe_group}\n"
        f"🙋 <b>Rᴇqᴜᴇꜱᴛᴇᴅ ʙʏ :</b> {who}</blockquote>"
    )


    bot_username, bot_name = await get_bot_identity(client)
    autoplay_on = await is_autoplay_on(chat.id)
    from melody.plugins.music.play import get_play_buttons  # lazy: avoid circular import
    play_buttons = get_play_buttons(
        chat.title or "", autoplay_on, bot_username, bot_name, chat.type,
        chat_id=chat.id,
    )

    # AutoPlay tracks (requester_id == 0) have no real user DP to fetch.
    fetch_user_dp = track.requester_id not in (0, None)
    user_dp_task = None
    if fetch_user_dp:
        user_dp_task = _await_or_none(fetch_dp(client, track.requester_id))
    bot_dp_task = _await_or_none(get_bot_dp(client))

    bot_dp_path = await bot_dp_task
    user_dp_path = await user_dp_task if user_dp_task else None

    try:
        thumb_path = await make_thumbnail(
            song_title=track.title,
            artist=track.uploader,
            duration=format_duration(track.duration),
            requester_name=track.requester_name,
            group_name=chat.title or "Private",
            owner_name=Config.OWNER_NAME,
            yt_thumbnail_url=track.thumbnail,
            requester_dp_path=user_dp_path,
            bot_dp_path=bot_dp_path,
        )
        await client.send_photo(
            chat.id,
            thumb_path,
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=play_buttons,
            reply_parameters=reply_params(reply_to_message_id),
        )
    except Exception:
        # Group forbids photos or thumbnail failed — send text with buttons.
        try:
            await client.send_message(
                chat.id,
                caption,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=play_buttons,
                reply_parameters=reply_params(reply_to_message_id),
            )
        except Exception:
            pass


async def _await_or_none(coro):
    """Await a coroutine, returning None on any failure."""
    try:
        return await coro
    except Exception:
        return None
