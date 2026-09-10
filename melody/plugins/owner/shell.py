"""
💻 /shell — run shell commands (owner only)
"""
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from utils.decorators import owner_only, error_handler
from utils.formatters import quote_html


@bot.on_message(filters.command(["shell", "sh", "bash", "exec"]) & filters.private)
@owner_only
@error_handler
async def shell_cmd(client: Client, message: Message):
    cmd = " ".join(message.command[1:])
    if not cmd:
        return await message.reply(quote_html("**Usage:** `/shell <command>`"), parse_mode=enums.ParseMode.HTML)

    msg = await message.reply(quote_html(f"⚙️ Running: `{cmd[:100]}`..."), parse_mode=enums.ParseMode.HTML)

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = (stdout + stderr).decode().strip()
        if not output:
            output = "✅ Done (no output)"
        await msg.edit(
            quote_html(f"**$ {cmd[:50]}**\n```\n{output[:3500]}\n```", expandable=True),
            parse_mode=enums.ParseMode.HTML,
        )
    except asyncio.TimeoutError:
        await msg.edit(quote_html("⏰ Command timed out after 30 seconds."), parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        await msg.edit(quote_html(f"❌ Error: `{e}`"), parse_mode=enums.ParseMode.HTML)
