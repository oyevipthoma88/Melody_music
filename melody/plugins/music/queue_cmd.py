"""
📋 Queue commands
FIX: All replies use HTML parse_mode — mixing Markdown **bold** with backtick
     inline-code in the same string produces malformed entity ranges that
     Telegram rejects with ENTITY_BOUNDS_INVALID (especially when the text
     also contains '<' / '>' characters such as <position>).
"""
import html
from pyrogram import Client, filters
from pyrogram.types import Message
from melody import bot
from melody.core.queue import (
    format_queue, clear_queue, remove_from_queue, is_autoplay_on,
    get_queue, get_current, get_loop,
)
from utils.decorators import admin_or_auth, error_handler
from utils.formatters import send_quote, format_duration


@bot.on_message(filters.command(["queue", "q"]) & filters.group)
@error_handler
async def queue_cmd(client: Client, message: Message):
    # format_queue() already wraps its own <blockquote> — send as-is.
    autoplay_on = await is_autoplay_on(message.chat.id)
    text = format_queue(message.chat.id, autoplay_on=autoplay_on)
    await send_quote(message, text, client=client)


@bot.on_message(filters.command("clearqueue") & filters.group)
@error_handler
@admin_or_auth
async def clearqueue_cmd(client: Client, message: Message):
    clear_queue(message.chat.id)
    await send_quote(message, "🗑 <b>Queue cleared.</b>", client=client)


@bot.on_message(filters.command("remove") & filters.group)
@error_handler
@admin_or_auth
async def remove_cmd(client: Client, message: Message):
    args = message.command
    if len(args) < 2 or not args[1].isdigit():
        # FIX: was `"**Usage:** \`/remove <position>\`"` — mixing Markdown bold
        # with backtick code AND literal '<' '>' chars caused ENTITY_BOUNDS_INVALID.
        await send_quote(message, "<b>Usage:</b> <code>/remove &lt;position&gt;</code>", client=client)
        return
    pos = int(args[1])
    removed = remove_from_queue(message.chat.id, pos)
    if removed:
        safe_title = html.escape(removed.title[:40])
        await send_quote(message, f"🗑 Removed: <code>{safe_title}</code>", client=client)
    else:
        await send_quote(message, "❌ <b>Invalid position.</b>", client=client)


# PARITY (/queuepos in Yukki, AnonXMusic, VIPMusic): a one-line summary of the
# queue — what is playing, how many tracks are pending and how long that is.
@bot.on_message(filters.command(["queuepos", "qpos"]) & filters.group)
@error_handler
async def queuepos_cmd(client: Client, message: Message):
    current = get_current(message.chat.id)
    pending = get_queue(message.chat.id)
    if not current and not pending:
        await send_quote(message, "❌ <b>Queue khali hai — kuch bhi nahi chal raha.</b>", client=client)
        return

    remaining = sum(int(track.duration or 0) for track in pending)
    lines = ["📍 <b>Queue Position</b>\n"]
    if current:
        lines.append(
            f"▶️ Playing: <code>{html.escape(current.title[:45])}</code> "
            f"(<code>{format_duration(current.duration)}</code>)"
        )
    lines.append(f"📋 Pending: <code>{len(pending)}</code> track(s)")
    lines.append(f"⏱ Remaining: <code>{format_duration(remaining)}</code>")
    lines.append(f"🔁 Loop: <code>{get_loop(message.chat.id)}</code>")
    if pending:
        lines.append(f"⏭ Up next: <code>{html.escape(pending[0].title[:45])}</code>")
    await send_quote(message, "\n".join(lines), client=client)
