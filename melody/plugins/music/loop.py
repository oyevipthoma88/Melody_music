"""
🔁 Loop commands
BUG FIX: @error_handler moved OUTSIDE @admin_or_auth
"""
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from melody.core.queue import set_loop
from utils.decorators import admin_or_auth, error_handler
from utils.formatters import quote_html


@bot.on_message(filters.command("loop") & filters.group)
@error_handler
@admin_or_auth
async def loop_cmd(client: Client, message: Message):
    # PARITY: top bots accept `/loop enable|disable` (and a plain number) as
    # well as the bare command. Melody only understood the bare form, so
    # people copying commands from other bots got no loop at all.
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if arg in ("disable", "off", "0", "end", "stop"):
        set_loop(message.chat.id, "none")
        await message.reply(quote_html("▶️ **Loop disabled.**"), parse_mode=enums.ParseMode.HTML)
        return
    if arg in ("all", "queue"):
        set_loop(message.chat.id, "all")
        await message.reply(quote_html("🔁 **Looping entire queue.**"), parse_mode=enums.ParseMode.HTML)
        return
    set_loop(message.chat.id, "single")
    await message.reply(quote_html("🔂 **Looping current song.**"), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command("loopall") & filters.group)
@error_handler
@admin_or_auth
async def loopall_cmd(client: Client, message: Message):
    set_loop(message.chat.id, "all")
    await message.reply(quote_html("🔁 **Looping entire queue.**"), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command("noloop") & filters.group)
@error_handler
@admin_or_auth
async def noloop_cmd(client: Client, message: Message):
    set_loop(message.chat.id, "none")
    await message.reply(quote_html("▶️ **Loop disabled.**"), parse_mode=enums.ParseMode.HTML)
