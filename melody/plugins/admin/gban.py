"""
🌐 Global ban — owner only
BUG FIX: @error_handler moved OUTSIDE @owner_only so errors are caught and
logged instead of failing silently for the owner.
"""
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from melody.config import Config
from utils.database import gban_user, ungban_user
from utils.decorators import owner_only, error_handler
from utils.formatters import quote_html


@bot.on_message(filters.command("gban") & filters.private)
@error_handler
@owner_only
async def gban_cmd(client: Client, message: Message):
    user = await _get_target_user(message)
    if not user:
        return await message.reply(
            quote_html("**Usage:** `/gban @user` or reply to user"), parse_mode=enums.ParseMode.HTML
        )

    if user.id == Config.OWNER_ID:
        return await message.reply(quote_html("❌ Cannot gban yourself."), parse_mode=enums.ParseMode.HTML)

    reason = " ".join(message.command[2:]) if len(message.command) > 2 else ""
    await gban_user(user.id, reason)
    await message.reply(quote_html(f"🌐 `{user.id}` globally banned."), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command("ungban") & filters.private)
@error_handler
@owner_only
async def ungban_cmd(client: Client, message: Message):
    user = await _get_target_user(message)
    if not user:
        return await message.reply(
            quote_html("**Usage:** `/ungban @user` or reply to user"), parse_mode=enums.ParseMode.HTML
        )

    await ungban_user(user.id)
    await message.reply(
        quote_html(f"✅ `{user.id}` removed from global ban."), parse_mode=enums.ParseMode.HTML
    )


async def _get_target_user(message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    if len(message.command) > 1:
        try:
            return await message._client.get_users(message.command[1])
        except Exception:
            pass
    return None
