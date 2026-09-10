"""
📋 /logs — send bot logs (owner only)
FIX: decorator order corrected — @error_handler outer, @owner_only inner
     (consistent with all other command handlers in the codebase)
"""
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from utils.decorators import owner_only, error_handler
from utils.formatters import quote_html


@bot.on_message(filters.command("logs") & filters.private)
@error_handler
@owner_only
async def logs_cmd(client: Client, message: Message):
    await message.reply(
        quote_html("📋 **Bot Logs** are streamed to the LOG_GROUP channel.\nUse `/chatlist` to see all served chats."),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command("chatlist") & filters.private)
@error_handler
@owner_only
async def chatlist_cmd(client: Client, message: Message):
    from utils.database import get_chats_page, get_chat_count
    try:
        chats = await asyncio.wait_for(get_chats_page(limit=50), timeout=4.0)
        try:
            total = await asyncio.wait_for(get_chat_count(), timeout=2.0)
        except Exception:
            total = len(chats)
    except Exception:
        return await message.reply(
            quote_html("⚠️ Chat list abhi load nahi ho saki. Dobara try karo."),
            parse_mode=enums.ParseMode.HTML,
        )
    if not chats:
        return await message.reply(quote_html("❌ No chats in database."), parse_mode=enums.ParseMode.HTML)

    lines = [f"**📋 All Served Chats ({total} total):**\n"]
    for c in chats:
        lines.append(f"• `{c['chat_id']}` — {c.get('title', 'Unknown')[:30]}")
    if total > len(chats):
        lines.append(f"\n_Showing first {len(chats)} chats._")

    await message.reply(quote_html("\n".join(lines)), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command("maintenance") & filters.private)
@error_handler
@owner_only
async def maintenance_cmd(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        return await message.reply(
            quote_html("**Usage:** `/maintenance on` or `/maintenance off`"), parse_mode=enums.ParseMode.HTML
        )

    state = args[1].lower()
    if state == "on":
        import melody
        melody._maintenance = True
        await message.reply(
            quote_html("🔧 **Maintenance mode ON.** Regular users can't use the bot."),
            parse_mode=enums.ParseMode.HTML,
        )
    elif state == "off":
        import melody
        melody._maintenance = False
        await message.reply(quote_html("✅ **Maintenance mode OFF.**"), parse_mode=enums.ParseMode.HTML)
