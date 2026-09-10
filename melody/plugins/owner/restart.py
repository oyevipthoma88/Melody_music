"""
🔁 /restart — restart bot (owner only)
"""
import asyncio
import os
import sys
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from utils.decorators import owner_only, error_handler
from utils.formatters import quote_html


@bot.on_message(filters.command("restart") & filters.private)
@owner_only
@error_handler
async def restart_cmd(client: Client, message: Message):
    await message.reply(quote_html("🔁 Restarting Melody..."), parse_mode=enums.ParseMode.HTML)
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable, "-m", "melody"])
