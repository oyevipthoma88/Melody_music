"""
🛡️ Group Protection — Melody's own anti-porn / anti-spam guard.

What it blocks (per-chat configurable, OFF by default):
  • telegram invite links & join-links (t.me/joinchat, +invite hashes)
  • abusive slurs
  • optional: every external link, forwarded channel spam, edited messages

Actions: delete (default) → warn → mute → ban, set with /protection action <x>.

Commands
    /protection            — show the panel
    /protection on|off     — master switch
    /protection action mute
"""
import logging
import re

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from melody import bot
from melody.logging import log_activity
from utils.admin_tools import (
    MUTED_PERMS,
    auto_delete,
    card,
    close_kb,
    is_admin,
    mention,
    safe_call,
)
from utils.approvals import is_approved
from utils.decorators import error_handler
from utils.gc_db import add_warn, get_protection, set_protection
from utils.tasks import spawn

log = logging.getLogger(__name__)

# ─── Pattern banks ───────────────────────────────────────────────────────────

_INVITE_RE = re.compile(r"(t\.me/(joinchat|\+)|telegram\.me/joinchat|t\.me/[A-Za-z0-9_]{5,})", re.IGNORECASE)
_LINK_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_ABUSE_RE = re.compile(
    r"\b(madarchod|behenchod|bhenchod|bsdk|mc bc|chutiya|gaand|lund|lawda|"
    r"randi|harami|kutta sala|motherfucker|fuck you|bitch|asshole)\b",
    re.IGNORECASE,
)

_GUARDS = ("links", "invites", "abuse", "forward", "edit")


def _detect(cfg: dict, message: Message) -> "str | None":
    text = (message.text or message.caption or "")
    if cfg.get("abuse") and _ABUSE_RE.search(text):
        return "Aʙᴜsɪᴠᴇ ʟᴀɴɢᴜᴀɢᴇ"
    if cfg.get("invites") and _INVITE_RE.search(text):
        return "Iɴᴠɪᴛᴇ / ᴘʀᴏᴍᴏ ʟɪɴᴋ"
    if cfg.get("links") and _LINK_RE.search(text):
        return "Exᴛᴇʀɴᴀʟ ʟɪɴᴋ"
    if cfg.get("forward") and (message.forward_from or message.forward_from_chat):
        return "Fᴏʀᴡᴀʀᴅᴇᴅ sᴘᴀᴍ"
    return None


async def _send_safe(coro):
    try:
        return await coro
    except Exception:
        return None



@bot.on_message(filters.group & ~filters.service, group=9)
async def protection_watch(client: Client, message: Message):
    try:
        if not message.from_user:
            return
        # FEATURE (/approve): an approved user is ignored completely — their
        # content is never scanned, deleted, warned, muted or banned.
        if await is_approved(message.chat.id, message.from_user.id):
            return
        cfg = await get_protection(message.chat.id)
        if not cfg.get("enabled"):
            return

        is_group_admin = await is_admin(client, message.chat.id, message.from_user.id)
        reason = None if is_group_admin else _detect(cfg, message)

        if not reason:
            return

        ok, _err = await safe_call(message.delete())
        if not ok:
            # No delete rights → tell the group once instead of failing silently.
            warn = await _send_safe(client.send_message(
                message.chat.id,
                card(
                    "Pʀᴏᴛᴇᴄᴛɪᴏɴ Sʜɪᴇʟᴅ",
                    f"🚫 <b>{reason}</b> ᴅᴇᴛᴇᴄᴛᴇᴅ, ʙᴜᴛ I ᴄᴀɴ'ᴛ ᴅᴇʟᴇᴛᴇ ɪᴛ.\n"
                    "🔑 Gɪᴠᴇ ᴍᴇ <b>Dᴇʟᴇᴛᴇ Mᴇssᴀɢᴇs</b> ᴀᴅᴍɪɴ ʀɪɢʜᴛ.",
                ),
                parse_mode=enums.ParseMode.HTML,
            ))
            if warn:
                await auto_delete(warn, 60)
            return

        action = cfg.get("action", "delete")
        extra = ""
        if is_group_admin:
            action = "delete"  # never punish an admin, just remove the content
        if action == "warn":
            count = await add_warn(message.chat.id, message.from_user.id, reason)
            extra = f"\n⚠️ <b>Wᴀʀɴ :</b> <code>{count}/3</code>"
        elif action == "mute":
            await safe_call(client.restrict_chat_member(message.chat.id, message.from_user.id, MUTED_PERMS))
            extra = "\n🔇 <b>Usᴇʀ ᴍᴜᴛᴇᴅ</b>"
        elif action == "ban":
            await safe_call(client.ban_chat_member(message.chat.id, message.from_user.id))
            extra = "\n🔨 <b>Usᴇʀ ʙᴀɴɴᴇᴅ</b>"

        notice = await client.send_message(
            message.chat.id,
            card(
                "Pʀᴏᴛᴇᴄᴛɪᴏɴ Sʜɪᴇʟᴅ",
                f"🛡 <b>Mᴇssᴀɢᴇ ʀᴇᴍᴏᴠᴇᴅ</b>\n"
                f"👤 <b>Usᴇʀ :</b> {mention(message.from_user)}\n"
                f"🚫 <b>Rᴇᴀsᴏɴ :</b> {reason}{extra}",
                "Melody is guarding this group 24×7 🎶",
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        await auto_delete(notice, 60)
        spawn(log_activity(
            f"#protection #deleted\n🚫 {reason}\n👤 <code>{message.from_user.id}</code>\n🏠 <code>{message.chat.id}</code>"
        ))
    except Exception as exc:
        log.warning("protection: watcher error: %s", exc)



# BUG FIX: `filters.edited` does not exist in Pyrogram 2.x / pyrofork — it
# raised AttributeError at import time, so this whole protection plugin was
# dropped by load_plugins() (no NSFW/abuse/link filtering at all). Edited
# messages arrive on their own update type, handled by @on_edited_message.
@bot.on_edited_message(filters.group, group=9)
async def protection_edit_watch(client: Client, message: Message):
    try:
        cfg = await get_protection(message.chat.id)
        if message.from_user and await is_approved(message.chat.id, message.from_user.id):
            return
        if cfg.get("enabled") and cfg.get("edit") and message.from_user:
            if not await is_admin(client, message.chat.id, message.from_user.id):
                await safe_call(message.delete())
    except Exception:
        pass


def _panel(cfg: dict) -> str:
    def dot(v):
        return "🟢 ON" if v else "🔴 OFF"

    return card(
        "Gʀᴏᴜᴘ Pʀᴏᴛᴇᴄᴛɪᴏɴ",
        f"🛡 <b>Mᴀsᴛᴇʀ :</b> {dot(cfg['enabled'])}\n\n"
        f"  🔸 <b>Aʙᴜsᴇ :</b> {dot(cfg['abuse'])}\n"
        f"  🔸 <b>Iɴᴠɪᴛᴇ ʟɪɴᴋs :</b> {dot(cfg['invites'])}\n"
        f"  🔸 <b>Aʟʟ ʟɪɴᴋs :</b> {dot(cfg['links'])}\n"
        f"  🔸 <b>Fᴏʀᴡᴀʀᴅs :</b> {dot(cfg['forward'])}\n"
        f"  🔸 <b>Eᴅɪᴛᴇᴅ ᴍsɢs :</b> {dot(cfg['edit'])}\n\n"
        f"⚙️ <b>Aᴄᴛɪᴏɴ :</b> <code>{cfg['action']}</code>",
        "/protection abuse|links|invites on/off · /protection action mute",
    )


@bot.on_message(filters.command(["protection", "guard", "antispam"]) & filters.group)
@error_handler
async def protection_cmd(client: Client, message: Message):
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        return
    args = [a.lower() for a in message.command[1:]]
    chat_id = message.chat.id

    if not args:
        return await message.reply(_panel(await get_protection(chat_id)), parse_mode=enums.ParseMode.HTML, reply_markup=close_kb())

    if args[0] in ("on", "off"):
        await set_protection(chat_id, "enabled", args[0] == "on")
    elif args[0] == "action" and len(args) > 1 and args[1] in ("delete", "warn", "mute", "ban"):
        await set_protection(chat_id, "action", args[1])
    elif args[0] in _GUARDS and len(args) > 1 and args[1] in ("on", "off"):
        await set_protection(chat_id, args[0], args[1] == "on")
    else:
        return await message.reply(
            card("Usᴀɢᴇ", "🧾 <code>/protection on|off</code>\n<code>/protection abuse|invites|links|forward|edit on|off</code>\n<code>/protection action delete|warn|mute|ban</code>"),
            parse_mode=enums.ParseMode.HTML,
        )

    await message.reply(_panel(await get_protection(chat_id)), parse_mode=enums.ParseMode.HTML, reply_markup=close_kb())


# Per-guard shortcut commands are thin aliases over the same protection store.

_ALIAS_GUARD = {
    "antilink": "links",
    "antiinvite": "invites",
    "antiabuse": "abuse",
    "antiedit": "edit",
    "antiforward": "forward",
}


@bot.on_message(filters.command(list(_ALIAS_GUARD.keys())) & filters.group)
@error_handler
async def guard_alias_cmd(client: Client, message: Message):
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        return
    guard = _ALIAS_GUARD[message.command[0].lower()]
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    cfg = await get_protection(message.chat.id)

    if arg not in ("on", "off"):
        # No argument -> toggle the selected protection guard.
        arg = "off" if cfg.get(guard) else "on"

    await set_protection(message.chat.id, guard, arg == "on")
    if arg == "on" and not cfg.get("enabled"):
        # Turning a guard on while the master switch is off would be a no-op.
        await set_protection(message.chat.id, "enabled", True)

    await message.reply(
        _panel(await get_protection(message.chat.id)),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(filters.command(["protectinfo", "guardinfo"]) & filters.group)
@error_handler
async def protectinfo_cmd(client: Client, message: Message):
    cfg = await get_protection(message.chat.id)
    await message.reply(
        _panel(cfg),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
