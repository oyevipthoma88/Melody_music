"""
🧨 Mass actions — /muteall /unmuteall /banall /unbanall

STRICT PERMISSION RULE (as requested):
    Only the **group owner (creator)**, the bot owner and **sudo users** may
    run these. A normal admin — even a full-promoted one — gets refused.

`muteall` does two things so it is genuinely "user message karte hi delete":
  1. sets the chat's DEFAULT permissions to "can't send messages", and
  2. flips a per-chat MUTEALL flag; while it's on, any message that still
     slips through (from an admin-exempt client, media groups, etc.) is
     deleted instantly by the watcher at the bottom of this file.
"""
import asyncio
import html

from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from melody import bot
from melody.logging import log_activity
from utils.admin_tools import (
    MUTED_PERMS,
    UNMUTED_PERMS,
    card,
    close_kb,
    is_admin,
    is_group_owner,
    mention,
    safe_call,
)
from utils.decorators import error_handler
from utils.gc_db import get_setting_flag, set_setting_flag
from utils.tasks import spawn


async def _owner_gate(client: Client, message: Message) -> bool:
    if not message.from_user:
        return False
    if await is_group_owner(client, message.chat.id, message.from_user.id):
        return True
    m = await message.reply(
        card(
            "Oᴡɴᴇʀ Oɴʟʏ",
            "👑 <b>Ye command sirf group OWNER ya SUDO user chala sakta hai.</b>\n"
            "<i>Admins ke liye nahi — mass actions bahut powerful hote hain.</i>",
        ),
        parse_mode=enums.ParseMode.HTML,
    )
    from utils.admin_tools import auto_delete

    await auto_delete(m, 30)
    return False


@bot.on_message(filters.command("muteall") & filters.group)
@error_handler
async def muteall_cmd(client: Client, message: Message):
    if not await _owner_gate(client, message):
        return
    ok, err = await safe_call(client.set_chat_permissions(message.chat.id, MUTED_PERMS))
    await set_setting_flag(message.chat.id, "muteall", True)
    await message.reply(
        card(
            "Mᴜᴛᴇ Aʟʟ",
            "🔇 <b>Pᴏᴏʀᴀ ɢʀᴏᴜᴘ ᴍᴜᴛᴇ.</b>\n"
            "🔸 Default permissions revoked\n"
            "🔸 Koi bhi message aayega to turant delete\n\n"
            f"👑 <b>Bʏ :</b> {mention(message.from_user)}"
            + ("" if ok else f"\n\n⚠️ <code>{err}</code>"),
            "/unmuteall se wapas khol dena.",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(f"#muteall #admin\n🏠 <code>{message.chat.id}</code> by <code>{message.from_user.id}</code>"))


async def _restore_restricted_members(client: Client, chat_id: int) -> tuple[int, int]:
    """Restore members that were individually restricted before /unmuteall.

    set_chat_permissions() only changes the default permission template; it does
    not undo restrict_chat_member() calls already stored on individual members.
    Telegram may also return FloodWait while a large group is being repaired, so
    this helper continues after per-user failures and reports the result.
    """
    restored = failed = 0
    try:
        async for member in client.get_chat_members(chat_id):
            user = getattr(member, "user", None)
            perms = getattr(member, "permissions", None)
            status = str(getattr(member, "status", "")).lower()
            if not user or getattr(user, "is_bot", False):
                continue
            if "restricted" not in status:
                continue
            if perms is not None and getattr(perms, "can_send_messages", True):
                continue
            for attempt in range(3):
                try:
                    await client.restrict_chat_member(
                        chat_id, user.id, permissions=UNMUTED_PERMS
                    )
                    restored += 1
                    break
                except FloodWait as fw:
                    await asyncio.sleep(int(getattr(fw, "value", getattr(fw, "x", 2))) + 1)
                except Exception:
                    if attempt == 2:
                        failed += 1
                    else:
                        await asyncio.sleep(0.5)
            if (restored + failed) % 20 == 0:
                await asyncio.sleep(1)
    except Exception:
        # The default permission change still succeeded; the caller surfaces
        # this as a partial result instead of making /unmuteall look successful.
        failed += 1
    return restored, failed


@bot.on_message(filters.command("unmuteall") & filters.group)
@error_handler
async def unmuteall_cmd(client: Client, message: Message):
    if not await _owner_gate(client, message):
        return
    ok, err = await safe_call(client.set_chat_permissions(message.chat.id, UNMUTED_PERMS))
    restored, failed = await _restore_restricted_members(client, message.chat.id)
    await set_setting_flag(message.chat.id, "muteall", False)
    detail = (
        "🔈 <b>Group khul gaya</b> — sab bol sakte hain 🎶\n"
        f"👥 <b>Existing muted users restored:</b> <code>{restored}</code>"
    )
    if failed:
        detail += f"\n⚠️ <b>Could not restore:</b> <code>{failed}</code>"
    if not ok:
        detail += f"\n⚠️ <code>{html.escape(str(err))}</code>"
    await message.reply(
        card("Uɴᴍᴜᴛᴇ Aʟʟ", detail),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(
        f"#unmuteall #admin\n🏠 <code>{message.chat.id}</code> · restored <code>{restored}</code>"
    ))


@bot.on_message(filters.command("banall") & filters.group)
@error_handler
async def banall_cmd(client: Client, message: Message):
    if not await _owner_gate(client, message):
        return
    status = await message.reply(card("Bᴀɴ Aʟʟ", "⏳ <b>Sᴄᴀɴɴɪɴɢ ᴍᴇᴍʙᴇʀs…</b>"), parse_mode=enums.ParseMode.HTML)
    banned, failed = 0, 0
    async for member in client.get_chat_members(message.chat.id):
        user = member.user
        if not user or user.is_bot:
            continue
        if await is_admin(client, message.chat.id, user.id):
            continue
        ok, _ = await safe_call(client.ban_chat_member(message.chat.id, user.id))
        banned += 1 if ok else 0
        failed += 0 if ok else 1
        if (banned + failed) % 15 == 0:
            await asyncio.sleep(1)
    await status.edit_text(
        card(
            "Bᴀɴ Aʟʟ Cᴏᴍᴘʟᴇᴛᴇ",
            f"🔨 <b>Bᴀɴɴᴇᴅ :</b> <code>{banned}</code>\n❌ <b>Fᴀɪʟᴇᴅ :</b> <code>{failed}</code>\n"
            f"👑 <b>Bʏ :</b> {mention(message.from_user)}",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(f"#banall #admin\n🏠 <code>{message.chat.id}</code> · banned <code>{banned}</code>"))


@bot.on_message(filters.command("unbanall") & filters.group)
@error_handler
async def unbanall_cmd(client: Client, message: Message):
    if not await _owner_gate(client, message):
        return
    status = await message.reply(card("Uɴʙᴀɴ Aʟʟ", "⏳ <b>Rᴇʟᴇᴀsɪɴɢ ʙᴀɴɴᴇᴅ ᴜsᴇʀs…</b>"), parse_mode=enums.ParseMode.HTML)
    freed = 0
    try:
        async for member in client.get_chat_members(message.chat.id, filter=enums.ChatMembersFilter.BANNED):
            if member.user:
                ok, _ = await safe_call(client.unban_chat_member(message.chat.id, member.user.id))
                freed += 1 if ok else 0
                if freed % 15 == 0:
                    await asyncio.sleep(1)
    except Exception as exc:
        return await status.edit_text(
            card("Uɴʙᴀɴ Aʟʟ", f"❌ <code>{html.escape(str(exc))}</code>"), parse_mode=enums.ParseMode.HTML
        )
    await status.edit_text(
        card("Uɴʙᴀɴ Aʟʟ Cᴏᴍᴘʟᴇᴛᴇ", f"✅ <b>Uɴʙᴀɴɴᴇᴅ :</b> <code>{freed}</code> users"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(f"#unbanall #admin\n🏠 <code>{message.chat.id}</code> · freed <code>{freed}</code>"))


@bot.on_message(filters.command("kickall") & filters.group)
@error_handler
async def kickall_cmd(client: Client, message: Message):
    """GAP FIX: documented in the help menu but never implemented.

    Unlike /banall this removes members WITHOUT blacklisting them, so they can
    rejoin with the invite link. Owner / sudo only, same gate as the rest.
    """
    if not await _owner_gate(client, message):
        return
    status = await message.reply(card("Kɪᴄᴋ Aʟʟ", "⏳ <b>Sᴄᴀɴɴɪɴɢ ᴍᴇᴍʙᴇʀs…</b>"), parse_mode=enums.ParseMode.HTML)
    kicked, failed = 0, 0
    async for member in client.get_chat_members(message.chat.id):
        user = member.user
        if not user or user.is_bot:
            continue
        if await is_admin(client, message.chat.id, user.id):
            continue
        ok, _ = await safe_call(client.ban_chat_member(message.chat.id, user.id))
        if ok:
            # unban immediately -> plain kick, user is free to come back
            await safe_call(client.unban_chat_member(message.chat.id, user.id))
            kicked += 1
        else:
            failed += 1
        if (kicked + failed) % 15 == 0:
            await asyncio.sleep(1)
    await status.edit_text(
        card(
            "Kɪᴄᴋ Aʟʟ Cᴏᴍᴘʟᴇᴛᴇ",
            f"👢 <b>Kɪᴄᴋᴇᴅ :</b> <code>{kicked}</code>\n❌ <b>Fᴀɪʟᴇᴅ :</b> <code>{failed}</code>\n"
            f"👑 <b>Bʏ :</b> {mention(message.from_user)}",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(f"#kickall #admin\n🏠 <code>{message.chat.id}</code> · kicked <code>{kicked}</code>"))


# ─── MUTEALL enforcement ─────────────────────────────────────────────────────

@bot.on_message(filters.group & ~filters.service, group=8)
async def _muteall_watch(client: Client, message: Message):
    try:
        if not await get_setting_flag(message.chat.id, "muteall", False):
            return
        if message.from_user and await is_admin(client, message.chat.id, message.from_user.id):
            return
        await message.delete()
    except Exception:
        pass
