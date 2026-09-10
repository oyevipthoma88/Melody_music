"""
📢 Broadcast — owner only
BUG FIX: @error_handler moved OUTSIDE @owner_only so errors during broadcast
are caught and logged instead of failing silently for the owner.
"""
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from utils.database import get_all_chats
from utils.decorators import owner_only, error_handler
from utils.formatters import quote_html


@bot.on_message(filters.command("broadcast") & filters.private)
@error_handler
@owner_only
async def broadcast_cmd(client: Client, message: Message):
    if not message.reply_to_message:
        return await message.reply(
            quote_html("**Reply to a message to broadcast it.**"), parse_mode=enums.ParseMode.HTML
        )

    chats = await get_all_chats()
    if not chats:
        return await message.reply(quote_html("❌ No chats in database."), parse_mode=enums.ParseMode.HTML)

    msg = await message.reply(
        quote_html(f"📢 Broadcasting to **{len(chats)}** chats..."), parse_mode=enums.ParseMode.HTML
    )
    success, failed = 0, 0

    for chat in chats:
        try:
            await message.reply_to_message.copy(chat["chat_id"])
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # Rate limit

    await msg.edit(
        quote_html(
            f"✅ **Broadcast Complete**\n\n"
            f"• Sent: `{success}`\n"
            f"• Failed: `{failed}`"
        ),
        parse_mode=enums.ParseMode.HTML,
    )
