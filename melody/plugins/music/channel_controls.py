"""
📺 Channel playback controls — full control set for channel voice chats.

Two usage sites, one implementation:

  • Inside the channel itself  → the command controls that channel's VC.
  • Inside a group linked with `/channelplay` → the command controls the
    LINKED channel's VC (same target `/cplay` streams into).

Previously only `/cpause`, `/cresume`, `/cskip` and `/cstop` existed, and only
when typed inside the channel — so a group that linked a channel could start
playback with `/cplay` but had no way to skip, seek, loop, shuffle, change
volume or read the queue. Every group command now has a channel twin here.
"""
import html

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from melody import bot
from melody.core.call import (
    change_volume,
    get_playback_position,
    is_active,
    mute_stream,
    pause_stream,
    resume_stream,
    seek_stream,
    set_playback_speed,
    skip_stream,
    stop_stream,
    unmute_stream,
)
from melody.core.queue import (
    format_queue,
    get_current,
    is_autoplay_on,
    set_loop,
    set_volume_local,
    shuffle_queue,
)
from melody.logging import log_activity
from utils.decorators import error_handler, is_admin_or_auth
from utils.formatters import quote_html, send_quote
from utils.gc_db import get_linked_channel
from utils.tasks import spawn

_IN_GROUP_OR_CHANNEL = filters.group | filters.channel


async def _resolve_target(client: Client, message: Message) -> "int | None":
    """Chat whose voice chat the command controls, or None (reason replied)."""
    if message.chat.type == enums.ChatType.CHANNEL:
        return message.chat.id

    # Group: an admin/auth user may control the linked channel, matching the
    # permission policy used by the regular group music commands.
    if message.from_user and not await is_admin_or_auth(
        client, message.chat.id, message.from_user.id
    ):
        await message.reply(
            quote_html(
                "⚠️ Only group admins or authorized users can control the "
                "linked channel's voice chat."
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return None

    link = await get_linked_channel(message.chat.id)
    if not link:
        await message.reply(
            quote_html(
                "📺 **No channel linked.**\n"
                "Use `/channelplay @channelusername` first."
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return None
    return int(link["channel_id"])


async def _reply(message: Message, text: str):
    return await message.reply(quote_html(text), parse_mode=enums.ParseMode.HTML)


async def _finish_channel_transport(status, message: Message, target: int, action: str) -> None:
    try:
        if action == "pause":
            ok = await pause_stream(target)
            text = "⏸ **Paused.**" if ok else "❌ **Nothing is playing in the channel VC.**"
        elif action == "resume":
            ok = await resume_stream(target)
            text = "▶️ **Resumed.**" if ok else "❌ **Nothing is paused in the channel VC.**"
        else:
            await stop_stream(target)
            ok = True
            text = "⏹ **Stopped — queue cleared and VC left.**"
        await send_quote(status, text, client=None, edit=True)
        if ok:
            _log(action.title(), message, target)
    except Exception:
        try:
            await send_quote(
                status, "⚠️ **Channel playback control complete nahi ho paya.**",
                client=None, edit=True,
            )
        except Exception:
            pass


def _log(action: str, message: Message, target: int):
    actor = html.escape(message.from_user.first_name) if message.from_user else "Channel"
    spawn(log_activity(
        f"📺 <b>{action} (channel)</b>\n"
        f"• By: <code>{actor}</code>\n"
        f"• Target: <code>{target}</code>"
    ))


def _seconds(message: Message) -> "int | None":
    if len(message.command) < 2:
        return None
    try:
        value = int(message.command[1])
    except ValueError:
        return None
    return value if value >= 0 else None


# ── transport ────────────────────────────────────────────────────────────────

@bot.on_message(filters.command("cpause") & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cpause_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    status = await _reply(message, "⏳ **Pausing channel playback...**")
    spawn(
        _finish_channel_transport(status, message, target, "pause"),
        name=f"cpause-{target}",
    )


@bot.on_message(filters.command("cresume") & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cresume_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    status = await _reply(message, "⏳ **Resuming channel playback...**")
    spawn(
        _finish_channel_transport(status, message, target, "resume"),
        name=f"cresume-{target}",
    )


@bot.on_message(filters.command(["cskip", "cs", "cnext"]) & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cskip_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    spawn(skip_stream(target))
    await _reply(message, "⏭ **Skipped.**")
    _log("Skipped", message, target)


@bot.on_message(filters.command(["cstop", "cend", "cleave"]) & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cstop_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    status = await _reply(message, "⏳ **Stopping channel playback...**")
    spawn(
        _finish_channel_transport(status, message, target, "stop"),
        name=f"cstop-{target}",
    )


# ── seeking ──────────────────────────────────────────────────────────────────

async def _do_seek(message: Message, target: int, position: int):
    try:
        new_pos = await seek_stream(target, position)
    except RuntimeError:
        return await _reply(message, "❌ **Nothing is playing in the channel VC.**")
    except Exception:
        return await _reply(message, "⚠️ **Could not seek — stream reload failed.**")
    await _reply(message, f"⏩ **Seeked to** `{new_pos}s`")


@bot.on_message(filters.command("cseek") & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cseek_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    seconds = _seconds(message)
    if seconds is None:
        return await _reply(message, "**Usage:** `/cseek <seconds>` — e.g. `/cseek 60`")
    track = get_current(target)
    if not track:
        return await _reply(message, "❌ **Nothing is playing in the channel VC.**")
    if track.duration and seconds > track.duration:
        return await _reply(message, f"❌ **Cannot seek past track duration** (`{track.duration}s`).")
    await _do_seek(message, target, seconds)


@bot.on_message(filters.command(["cseekback", "crewind"]) & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cseekback_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    seconds = _seconds(message)
    if seconds is None:
        return await _reply(message, "**Usage:** `/cseekback <seconds>` — e.g. `/cseekback 15`")
    if not get_current(target):
        return await _reply(message, "❌ **Nothing is playing in the channel VC.**")
    await _do_seek(message, target, max(0, get_playback_position(target) - seconds))


# ── queue / info ─────────────────────────────────────────────────────────────

@bot.on_message(filters.command(["cqueue", "cq"]) & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cqueue_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    autoplay_on = await is_autoplay_on(target)
    await message.reply(format_queue(target, autoplay_on), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command(["cnp", "cplaying", "cnowplaying"]) & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cnp_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    track = get_current(target)
    if not track or not is_active(target):
        return await _reply(message, "❌ **Nothing is playing in the channel VC.**")
    position = get_playback_position(target)
    await _reply(
        message,
        f"🎧 **Now playing:** {track.title}\n⏱ **Position:** `{position}s`",
    )


# ── queue behaviour ──────────────────────────────────────────────────────────

@bot.on_message(filters.command(["cloop", "cloopall", "cnoloop"]) & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cloop_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    cmd = message.command[0].lower().split("@")[0]
    mode, label = {
        "cloop": ("single", "🔂 **Looping current song.**"),
        "cloopall": ("all", "🔁 **Looping entire queue.**"),
        "cnoloop": ("none", "▶️ **Loop disabled.**"),
    }[cmd]
    set_loop(target, mode)
    await _reply(message, label)


@bot.on_message(filters.command("cshuffle") & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cshuffle_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    shuffle_queue(target)
    await _reply(message, "🔀 **Channel queue shuffled!**")


# ── audio ────────────────────────────────────────────────────────────────────

@bot.on_message(filters.command(["cvolume", "cvol"]) & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cvolume_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    level = _seconds(message)
    if level is None or not 1 <= level <= 200:
        return await _reply(message, "**Usage:** `/cvolume <1-200>`")
    if not await change_volume(target, level):
        return await _reply(message, "❌ **Nothing is playing in the channel VC.**")
    set_volume_local(target, level)
    await _reply(message, f"🔊 **Volume set to** `{level}%`")


@bot.on_message(filters.command("cspeed") & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cspeed_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    if len(message.command) < 2:
        return await _reply(message, "**Usage:** `/cspeed <0.5-2.0>`")
    try:
        speed = float(message.command[1])
    except ValueError:
        return await _reply(message, "**Usage:** `/cspeed <0.5-2.0>`")
    if not 0.5 <= speed <= 2.0:
        return await _reply(message, "❌ **Speed must be between 0.5x and 2.0x.**")
    if not await set_playback_speed(target, speed):
        return await _reply(message, "❌ **Nothing is playing in the channel VC.**")
    await _reply(message, f"⚡ **Playback speed:** `{speed}x`")


@bot.on_message(filters.command("cmute") & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cmute_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    if not await mute_stream(target):
        return await _reply(message, "❌ **Nothing is playing in the channel VC.**")
    await _reply(message, "🔇 **Muted.**")


@bot.on_message(filters.command("cunmute") & _IN_GROUP_OR_CHANNEL)
@error_handler
async def cunmute_cmd(client: Client, message: Message):
    target = await _resolve_target(client, message)
    if target is None:
        return
    if not await unmute_stream(target):
        return await _reply(message, "❌ **Nothing is playing in the channel VC.**")
    await _reply(message, "🔊 **Unmuted.**")
