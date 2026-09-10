"""
📡 /activevc — List all active voice chat sessions (owner only)
"""
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from utils.decorators import owner_only, error_handler
from utils.database import get_all_chats
from utils.formatters import quote_html


@bot.on_message(filters.command(["activevc", "ac"]) & filters.private)
@owner_only
@error_handler
async def activevc_cmd(client: Client, message: Message):
    """Show all chats where the bot is currently in a voice call."""
    from melody.core.call import _active

    active_ids = [cid for cid, is_active in _active.items() if is_active]

    if not active_ids:
        return await message.reply(
            quote_html(
                "📡 **Active Voice Chats**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "🔇 No active voice chats right now.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            parse_mode=enums.ParseMode.HTML,
        )

    # Match IDs to titles from DB
    all_chats = await get_all_chats()
    chat_map = {c["chat_id"]: c.get("title", "Unknown") for c in all_chats}

    from melody.core.queue import get_current

    lines = [
        f"📡 **Active Voice Chats** ({len(active_ids)} active)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    ]
    for cid in active_ids:
        title = chat_map.get(cid, "Unknown Group")[:30]
        current = get_current(cid)
        song = f"`{current.title[:25]}...`" if current else "_Unknown_"
        lines.append(f"🎵 **{title}**\n   ID: `{cid}`\n   Now: {song}\n")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
    await message.reply(quote_html("\n".join(lines)), parse_mode=enums.ParseMode.HTML)
