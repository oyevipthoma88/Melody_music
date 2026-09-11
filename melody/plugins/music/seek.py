"""
⏩⏪ /seek, /seekback, /rewind — position control inside the current track

Real seeking, not a stub: seek_stream() (melody/core/call.py) re-issues the
voice-call stream with an ffmpeg `-ss <seconds>` input offset, which is the
only seek mechanism py-tgcalls 2.1.1 exposes. /seekback and /rewind compute
the new absolute position from get_playback_position() and delegate to the
same seek_stream().
"""
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from melody.core.call import seek_stream, get_playback_position
from melody.core.queue import get_current
from utils.decorators import admin_or_auth, error_handler
from utils.formatters import quote_html


def _parse_seconds(args: list[str]) -> "int | None":
    if len(args) < 2:
        return None
    try:
        seconds = int(args[1])
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


@bot.on_message(filters.command("seek") & filters.group)
@error_handler
@admin_or_auth
async def seek_cmd(client: Client, message: Message):
    seconds = _parse_seconds(message.command)
    if seconds is None:
        await message.reply(
            quote_html("**Usage:** `/seek <seconds>`\nExample: `/seek 60` → jump to 1 minute"),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    track = get_current(message.chat.id)
    if not track:
        await message.reply(quote_html("❌ Nothing is playing right now."), parse_mode=enums.ParseMode.HTML)
        return
    if track.duration and seconds > track.duration:
        await message.reply(
            quote_html(f"❌ Cannot seek past track duration ({track.duration}s)."),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    try:
        new_pos = await seek_stream(message.chat.id, seconds)
    except RuntimeError:
        await message.reply(quote_html("❌ Nothing is playing right now."), parse_mode=enums.ParseMode.HTML)
        return
    except Exception:
        await message.reply(
            quote_html("⚠️ Could not seek — could not reload the audio stream."),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    await message.reply(quote_html(f"⏩ Seeked to `{new_pos}s`"), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command("seekback") & filters.group)
@error_handler
@admin_or_auth
async def seekback_cmd(client: Client, message: Message):
    seconds = _parse_seconds(message.command)
    if seconds is None:
        await message.reply(
            quote_html("**Usage:** `/seekback <seconds>`\nExample: `/seekback 15` → go back 15 seconds"),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    track = get_current(message.chat.id)
    if not track:
        await message.reply(quote_html("❌ Nothing is playing right now."), parse_mode=enums.ParseMode.HTML)
        return

    current_pos = get_playback_position(message.chat.id)
    new_target = max(0, current_pos - seconds)

    try:
        new_pos = await seek_stream(message.chat.id, new_target)
    except RuntimeError:
        await message.reply(quote_html("❌ Nothing is playing right now."), parse_mode=enums.ParseMode.HTML)
        return
    except Exception:
        await message.reply(
            quote_html("⚠️ Could not seek — could not reload the audio stream."),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    await message.reply(quote_html(f"⏪ Seeked back to `{new_pos}s`"), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command("rewind") & filters.group)
@error_handler
@admin_or_auth
async def rewind_cmd(client: Client, message: Message):
    """Alias of /seekback for anyone used to the old naming."""
    await seekback_cmd(client, message)
