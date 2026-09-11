"""
⏩⏪ /seek, /seekback, /rewind — position control inside the current track

Real seeking, not a stub: seek_stream() (melody/core/call.py) re-issues the
voice-call stream with an ffmpeg `-ss <seconds>` input offset, which is the
only seek mechanism py-tgcalls 2.1.1 exposes. /seekback and /rewind compute
the new absolute position from get_playback_position() and delegate to the
same seek_stream().
"""
import re

from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from melody.core.call import seek_stream, get_playback_position
from melody.core.queue import get_current
from utils.decorators import admin_or_auth, error_handler
from utils.formatters import quote_html


_DURATION_TOKEN = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>[hms]?)", re.IGNORECASE)


def _parse_duration(args: list[str]) -> "int | None":
    """Parse seek input as seconds or one/more ``<number><unit>`` tokens.

    Examples: ``60``, ``1s``, ``1m``, ``2h``, ``2h 1m 4s``. A unitless
    number means seconds for backwards compatibility. Tokens must be complete;
    malformed input such as ``1 hour`` or ``2hfoo`` is rejected rather than
    silently seeking to an unintended position.
    """
    if len(args) < 2:
        return None
    total = 0.0
    saw_unit = False
    for token in args[1:]:
        match = _DURATION_TOKEN.fullmatch(token.strip())
        if not match:
            return None
        value = float(match.group("value"))
        unit = match.group("unit").lower()
        if unit == "h":
            multiplier = 3600
        elif unit == "m":
            multiplier = 60
        else:
            multiplier = 1
        saw_unit = saw_unit or bool(unit)
        total += value * multiplier
    if total < 0 or total != int(total):
        return None
    # A multi-token unitless expression is ambiguous; only plain `/seek 60`
    # is accepted without units. This keeps typos from becoming large jumps.
    if not saw_unit and len(args) > 2:
        return None
    return int(total)


# Backwards-compatible internal name for callers/tests that used the old helper.
def _parse_seconds(args: list[str]) -> "int | None":
    return _parse_duration(args)


@bot.on_message(filters.command("seek") & filters.group)
@error_handler
@admin_or_auth
async def seek_cmd(client: Client, message: Message):
    seconds = _parse_seconds(message.command)
    if seconds is None:
        await message.reply(
            quote_html("**Usage:** `/seek <time>`\nExamples: `/seek 60`, `/seek 1m`, `/seek 2h 1m 4s`"),
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
            quote_html("**Usage:** `/seekback <time>`\nExamples: `/seekback 15`, `/seekback 1m 30s`"),
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
