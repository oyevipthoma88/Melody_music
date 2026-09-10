"""
✅ /approve — whitelist a user so Melody ignores everything they send.

REQUESTED
---------
"Approve ka option add kr ( /approve, /unapprove, /approveall, /unapproveall )
krte hi bot user ko ignore karega mtlb uska kuch content delete nahi karega."

Commands (group admins only)
    /approve            (reply / @user / id)  → ignore this user completely
    /unapprove          (reply / @user / id)  → put them back under filters
    /approveall                               → ignore EVERY member's content
    /unapproveall                             → filters back on for everyone
                                                (also clears the user list)
    /approved                                 → show the whitelist

An approved user is skipped by group protection, safe mode, the NSFW media
guard and every other content filter — nothing of theirs is deleted, warned,
muted or banned.
"""
import logging

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from melody import bot
from utils.admin_tools import auto_delete, card, close_kb, extract_target, is_admin, mention
from utils.approvals import (
    approve_all,
    approve_user,
    approved_list,
    unapprove_all,
    unapprove_user,
)
from utils.decorators import error_handler

log = logging.getLogger(__name__)


async def _deny(client: Client, message: Message) -> bool:
    """True when the sender may not change the whitelist."""
    if not message.from_user:
        return True
    if await is_admin(client, message.chat.id, message.from_user.id):
        return False
    warn = await message.reply(
        card("Aᴘᴘʀᴏᴠᴇ", "🔒 <b>Oɴʟʏ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴs ᴄᴀɴ ᴜsᴇ ᴛʜɪs.</b>"),
        parse_mode=enums.ParseMode.HTML,
    )
    await auto_delete(warn, 30)
    return True


@bot.on_message(filters.command(["approve", "unapprove", "unapprov"]) & filters.group)
@error_handler
async def approve_cmd(client: Client, message: Message):
    if await _deny(client, message):
        return

    adding = message.command[0].lower() == "approve"
    user = await extract_target(client, message)
    if not user:
        await message.reply(
            card(
                "Aᴘᴘʀᴏᴠᴇ",
                "🧾 <b>Usᴀɢᴇ</b>\n"
                "┌ <code>/approve</code> (ʀᴇᴘʟʏ / @ᴜsᴇʀ / ɪᴅ)\n"
                "├ <code>/unapprove</code> (ʀᴇᴘʟʏ / @ᴜsᴇʀ / ɪᴅ)\n"
                "├ <code>/approveall</code> · <code>/unapproveall</code>\n"
                "└ <code>/approved</code> — ᴡʜɪᴛᴇʟɪsᴛ",
            ),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=close_kb(),
        )
        return

    if adding:
        changed = await approve_user(message.chat.id, user.id)
        body = (
            f"✅ {mention(user)} <b>ɪs ᴀᴘᴘʀᴏᴠᴇᴅ.</b>\n"
            "🛡 Mᴇʟᴏᴅʏ ᴡɪʟʟ ɴᴏᴡ <b>ɪɢɴᴏʀᴇ</b> ᴇᴠᴇʀʏᴛʜɪɴɢ ᴛʜᴇʏ sᴇɴᴅ —\n"
            "ɴᴏᴛʜɪɴɢ ᴏғ ᴛʜᴇɪʀs ɪs ᴅᴇʟᴇᴛᴇᴅ, ᴡᴀʀɴᴇᴅ, ᴍᴜᴛᴇᴅ ᴏʀ ʙᴀɴɴᴇᴅ."
            if changed else
            f"ℹ {mention(user)} <b>ᴡᴀs ᴀʟʀᴇᴀᴅʏ ᴀᴘᴘʀᴏᴠᴇᴅ.</b>"
        )
    else:
        changed = await unapprove_user(message.chat.id, user.id)
        body = (
            f"🚫 {mention(user)} <b>ɪs ɴᴏ ʟᴏɴɢᴇʀ ᴀᴘᴘʀᴏᴠᴇᴅ.</b>\n"
            "🛡 Aʟʟ ғɪʟᴛᴇʀs ᴀᴘᴘʟʏ ᴛᴏ ᴛʜᴇᴍ ᴀɢᴀɪɴ."
            if changed else
            f"ℹ {mention(user)} <b>ᴡᴀsɴ'ᴛ ᴀᴘᴘʀᴏᴠᴇᴅ.</b>"
        )

    await message.reply(
        card("Aᴘᴘʀᴏᴠᴇ", body), parse_mode=enums.ParseMode.HTML, reply_markup=close_kb()
    )


@bot.on_message(
    filters.command(["approveall", "unapproveall", "unapprovall", "approvall"]) & filters.group
)
@error_handler
async def approve_all_cmd(client: Client, message: Message):
    if await _deny(client, message):
        return

    cmd = message.command[0].lower()
    turning_on = cmd in ("approveall", "approvall")
    if turning_on:
        await approve_all(message.chat.id)
        body = (
            "✅ <b>Eᴠᴇʀʏᴏɴᴇ ɪɴ ᴛʜɪs ɢʀᴏᴜᴘ ɪs ᴀᴘᴘʀᴏᴠᴇᴅ.</b>\n"
            "🛡 Aʟʟ ᴄᴏɴᴛᴇɴᴛ ғɪʟᴛᴇʀs (ᴘʀᴏᴛᴇᴄᴛɪᴏɴ · sᴀғᴇ ᴍᴏᴅᴇ · NSFW)\n"
            "ᴀʀᴇ ɴᴏᴡ <b>ɪɢɴᴏʀɪɴɢ ᴇᴠᴇʀʏ ᴍᴇᴍʙᴇʀ</b>.\n\n"
            "↩️ Uɴᴅᴏ ᴡɪᴛʜ <code>/unapproveall</code>."
        )
    else:
        await unapprove_all(message.chat.id)
        body = (
            "🚫 <b>Cʜᴀᴛ-ᴡɪᴅᴇ ᴀᴘᴘʀᴏᴠᴀʟ ᴏғғ</b> — ᴀɴᴅ ᴛʜᴇ ᴡʜɪᴛᴇʟɪsᴛ ɪs ᴄʟᴇᴀʀᴇᴅ.\n"
            "🛡 Eᴠᴇʀʏ ғɪʟᴛᴇʀ ɪs ʙᴀᴄᴋ ᴏɴ ғᴏʀ ᴀʟʟ ᴍᴇᴍʙᴇʀs."
        )

    await message.reply(
        card("Aᴘᴘʀᴏᴠᴇ Aʟʟ", body), parse_mode=enums.ParseMode.HTML, reply_markup=close_kb()
    )


@bot.on_message(filters.command(["approved", "approvelist"]) & filters.group)
@error_handler
async def approved_cmd(client: Client, message: Message):
    users, everyone = await approved_list(message.chat.id)

    if everyone:
        body = (
            "✅ <b>Cʜᴀᴛ-ᴡɪᴅᴇ ᴀᴘᴘʀᴏᴠᴀʟ ɪs ON</b>\n"
            "🛡 Nᴏʙᴏᴅʏ's ᴄᴏɴᴛᴇɴᴛ ɪs ғɪʟᴛᴇʀᴇᴅ ʀɪɢʜᴛ ɴᴏᴡ."
        )
    elif not users:
        body = "📭 <b>Nᴏ ᴀᴘᴘʀᴏᴠᴇᴅ ᴜsᴇʀs ʏᴇᴛ.</b>\n🧾 <code>/approve</code> (ʀᴇᴘʟʏ)"
    else:
        lines = []
        for user_id in users[:50]:
            try:
                user = await client.get_users(user_id)
                lines.append(f"┃ {mention(user)} — <code>{user_id}</code>")
            except Exception:
                lines.append(f"┃ <code>{user_id}</code>")
        extra = f"\n┃ …ᴀɴᴅ {len(users) - 50} ᴍᴏʀᴇ" if len(users) > 50 else ""
        body = f"✅ <b>Aᴘᴘʀᴏᴠᴇᴅ ᴜsᴇʀs ({len(users)})</b>\n" + "\n".join(lines) + extra

    await message.reply(
        card("Aᴘᴘʀᴏᴠᴇᴅ", body), parse_mode=enums.ParseMode.HTML, reply_markup=close_kb()
    )
