"""
🚫 Bot-level ban / unban (block a user from using Melody).

BUG FIX — "ban/unban properly work nahi karti":
  • The admin check compared `member.status` to the strings
    `"administrator"` / `"creator"`. Pyrogram 2.x returns a
    `ChatMemberStatus` enum, so that comparison was ALWAYS False and every
    ban attempt died with "Admins only" — even for the group owner.
    It now uses `utils.admin_tools.is_admin()` (enum-based, sudo-aware).
  • `/ban @user reason` read the reason from `command[2:]`, dropping the
    first reason word when a username was given, and reading the whole
    reason as blank on a reply. Handled properly now.
  • Failures were silent; every action now confirms with a Melody card and
    logs to the log group.

Group-restriction bans (kick out of the group) live in
`melody/plugins/admin/gcmanage.py` as /gban-style chat bans — this file is
only about who may *use the bot*.
"""
import html

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from melody import bot
from melody.config import Config
from melody.logging import log_activity
from utils.admin_tools import (
    card,
    close_kb,
    extract_reason,
    extract_target,
    is_admin,
    mention,
)
from utils.database import ban_user, is_banned, unban_user
from utils.decorators import error_handler
from utils.tasks import spawn


@bot.on_message(filters.command(["botban", "banuser"]) & filters.group)
@error_handler
async def ban_cmd(client: Client, message: Message):
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        return await message.reply(
            card("Pᴇʀᴍɪssɪᴏɴ Dᴇɴɪᴇᴅ", "⚠️ Ye command sirf group admins ke liye hai."),
            parse_mode=enums.ParseMode.HTML,
        )

    user = await extract_target(client, message)
    if not user:
        return await message.reply(
            card("Bᴏᴛ Bᴀɴ", "🧾 <code>/botban @user [reason]</code>\n<i>Ya kisi user ke message pe reply karo.</i>"),
            parse_mode=enums.ParseMode.HTML,
        )

    if user.id == Config.OWNER_ID:
        return await message.reply(
            card("Nᴏᴘᴇ", "❌ Owner ko ban nahi kar sakte."), parse_mode=enums.ParseMode.HTML
        )
    if user.id == (await client.get_me()).id:
        return await message.reply(
            card("Nᴏᴘᴇ", "🤖 Main khud ko ban nahi karungi."), parse_mode=enums.ParseMode.HTML
        )

    reason = extract_reason(message, 1 if message.reply_to_message else 2) or "Not specified"
    await ban_user(user.id, reason)
    await message.reply(
        card(
            "Bᴏᴛ Bᴀɴɴᴇᴅ",
            f"🚫 <b>Usᴇʀ :</b> {mention(user)}\n"
            f"🆔 <code>{user.id}</code>\n"
            f"📝 <b>Rᴇᴀsᴏɴ :</b> {html.escape(reason)}\n"
            f"👮 <b>Bʏ :</b> {mention(message.from_user)}",
            "Ab ye user Melody ki koi command use nahi kar payega.",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(
        f"#botban\n👤 <code>{user.id}</code>\n🏠 <code>{message.chat.id}</code>\n📝 {html.escape(reason)}"
    ))


@bot.on_message(filters.command(["botunban", "unbanuser"]) & filters.group)
@error_handler
async def unban_cmd(client: Client, message: Message):
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        return await message.reply(
            card("Pᴇʀᴍɪssɪᴏɴ Dᴇɴɪᴇᴅ", "⚠️ Ye command sirf group admins ke liye hai."),
            parse_mode=enums.ParseMode.HTML,
        )

    user = await extract_target(client, message)
    if not user:
        return await message.reply(
            card("Bᴏᴛ Uɴʙᴀɴ", "🧾 <code>/botunban @user</code>\n<i>Ya reply karo.</i>"),
            parse_mode=enums.ParseMode.HTML,
        )

    if not await is_banned(user.id):
        return await message.reply(
            card("Nᴏᴛ Bᴀɴɴᴇᴅ", f"ℹ️ {mention(user)} pehle se hi free hai."),
            parse_mode=enums.ParseMode.HTML,
        )

    await unban_user(user.id)
    await message.reply(
        card(
            "Bᴏᴛ Uɴʙᴀɴɴᴇᴅ",
            f"✅ <b>Usᴇʀ :</b> {mention(user)}\n"
            f"🆔 <code>{user.id}</code>\n"
            f"👮 <b>Bʏ :</b> {mention(message.from_user)}",
            "Wapas se saare commands use kar sakta hai.",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(f"#botunban\n👤 <code>{user.id}</code>\n🏠 <code>{message.chat.id}</code>"))
