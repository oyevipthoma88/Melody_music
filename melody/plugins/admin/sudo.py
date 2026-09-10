"""
⚡ Sudo users — /addsudo /rmsudo /sudolist (bot owner only).

Sudo users are trusted globally: they bypass every group-admin gate, may run
the owner-only mass actions, and can never be banned/muted/kicked by Melody.
"""
import html

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from melody import bot
from melody.config import Config
from utils.admin_tools import card, close_kb, extract_target, mention
from utils.decorators import error_handler, owner_only
from utils.gc_db import add_sudo, get_sudoers, remove_sudo


@bot.on_message(filters.command(["addsudo", "sudo"]))
@owner_only
@error_handler
async def addsudo_cmd(client: Client, message: Message):
    user = await extract_target(client, message)
    if not user:
        return await message.reply(
            card("Aᴅᴅ Sᴜᴅᴏ", "🧾 <code>/addsudo @user</code> ya reply karo."), parse_mode=enums.ParseMode.HTML
        )
    await add_sudo(user.id, user.first_name or "")
    await message.reply(
        card("Sᴜᴅᴏ Aᴅᴅᴇᴅ", f"⚡ {mention(user)} ab <b>sudo</b> hai.\n🆔 <code>{user.id}</code>"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(filters.command(["rmsudo", "delsudo", "unsudo"]))
@owner_only
@error_handler
async def rmsudo_cmd(client: Client, message: Message):
    user = await extract_target(client, message)
    if not user:
        return await message.reply(
            card("Rᴇᴍᴏᴠᴇ Sᴜᴅᴏ", "🧾 <code>/rmsudo @user</code>"), parse_mode=enums.ParseMode.HTML
        )
    await remove_sudo(user.id)
    await message.reply(
        card("Sᴜᴅᴏ Rᴇᴍᴏᴠᴇᴅ", f"🚫 {mention(user)} ab sudo nahi hai."),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(filters.command(["sudolist", "sudoers", "listsudo"]))
@owner_only
@error_handler
async def sudolist_cmd(client: Client, message: Message):
    users = await get_sudoers()
    lines = "\n".join(
        f"  🔸 {html.escape(u.get('name') or 'User')} — <code>{u['user_id']}</code>" for u in users
    ) or "  🔸 —"
    await message.reply(
        card("Sᴜᴅᴏ Usᴇʀs", f"👑 <b>Oᴡɴᴇʀ :</b> <code>{Config.OWNER_ID}</code>\n\n⚡ <b>Sᴜᴅᴏ :</b>\n{lines}"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
