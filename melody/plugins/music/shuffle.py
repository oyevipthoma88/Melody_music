"""
🔀 /shuffle command
BUG FIX: @error_handler moved OUTSIDE @admin_or_auth
"""
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from melody.core.queue import shuffle_queue
from utils.decorators import admin_or_auth, error_handler
from utils.formatters import quote_html


@bot.on_message(filters.command("shuffle") & filters.group)
@error_handler
@admin_or_auth
async def shuffle_cmd(client: Client, message: Message):
    shuffle_queue(message.chat.id)
    await message.reply(quote_html("🔀 **Queue shuffled!**"), parse_mode=enums.ParseMode.HTML)
