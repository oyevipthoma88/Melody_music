"""
🚪 Join Request Manager — Melody theme

• Jab bhi group me koi join request aati hai, group me ek card notify hota
  hai (user ka naam / numeric id / username / profile link) with
  ACCEPT ✅ / REJECT ❌ buttons.
• Buttons sirf OWNER + SUDO + group admins/auth users dabaa sakte hain
  (cb_admin_or_auth ka full check).
• Agar request kisi aur admin ne Telegram UI se (bina button dabaye)
  accept ya reject kar di, tab bhi log group me #joinrequest #external
  notification jaata hai.
• Har event log group me hashtag format me jaata hai:
  #joinrequest #accepted #rejected #external.
• Per-group toggle: `join_notify` (default ON) — /settings panel se on/off.
"""

from __future__ import annotations

import html
import time

from pyrogram import Client, filters, enums
from pyrogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
)

from melody import bot
from melody.logging import log_activity, LOGGER
from utils.admin_tools import card, safe_call
from utils.buttons import ikb
from utils.decorators import cb_admin_or_auth, error_handler
from utils.gc_db import get_setting_flag
from utils.tasks import spawn

# (chat_id, user_id) -> {"msg_id": int, "ts": float}
_pending: dict[tuple[int, int], dict] = {}
_PENDING_TTL = 7 * 24 * 3600


def _prune() -> None:
    now = time.time()
    for key, meta in list(_pending.items()):
        if now - meta["ts"] > _PENDING_TTL:
            _pending.pop(key, None)


def forget_chat_pending(chat_id: int) -> int:
    """Drop every tracked pending request of a chat.

    Called after a bulk /rapproveall or /rdeclineall so the external-resolution
    watcher does not spam one "resolved" card per user for an action the group
    already saw a summary of.
    """
    keys = [key for key in _pending if key[0] == chat_id]
    for key in keys:
        _pending.pop(key, None)
    return len(keys)


def _user_block(user) -> str:
    if not user:
        return "• Usᴇʀ: <code>unknown</code>"
    name = html.escape(user.first_name or "User")
    uname = f"@{user.username}" if user.username else "—"
    return (
        f"• Nᴀᴍᴇ: <a href=\"tg://user?id={user.id}\">{name}</a>\n"
        f"• Usᴇʀ ID: <code>{user.id}</code>\n"
        f"• Usᴇʀɴᴀᴍᴇ: {html.escape(uname)}"
    )


def _chat_block(chat) -> str:
    title = html.escape(getattr(chat, "title", "") or "Group")
    line = f"• Cʜᴀᴛ: <b>{title}</b>\n• Cʜᴀᴛ ID: <code>{chat.id}</code>"
    uname = getattr(chat, "username", None)
    if uname:
        line += f"\n• Lɪɴᴋ: https://t.me/{uname}"
    return line


async def _notify_enabled(chat_id: int) -> bool:
    try:
        return bool(await get_setting_flag(chat_id, "join_notify", True))
    except Exception:
        return True


@bot.on_chat_join_request(group=8)
async def joinreq_notify(client: Client, req: ChatJoinRequest):
    """Group me join-request ka card + accept/reject buttons bhejo."""
    try:
        chat, user = req.chat, req.from_user
        if not user:
            return

        # REQUESTED ("join required aaye to accept kr"): the music assistant's
        # own join request must never wait for a human — approve it instantly
        # so /play keeps working in approval-gated groups.
        try:
            from melody.core.call import _get_assistant_id, forget_assistant_peer

            assistant_id = await _get_assistant_id()
            if assistant_id and user.id == assistant_id:
                await client.approve_chat_join_request(chat.id, user.id)
                forget_assistant_peer(chat.id)
                LOGGER.info("Auto-approved assistant join request in %s", chat.id)
                return
        except Exception as exc:
            LOGGER.warning("Assistant join-request auto-approve failed in %s: %s",
                           chat.id, exc)

        _prune()
        invite = getattr(req, "invite_link", None)
        invite_line = ""
        if invite is not None and getattr(invite, "invite_link", None):
            invite_line = f"\n• Vɪᴀ Lɪɴᴋ: {html.escape(invite.invite_link)}"

        spawn(log_activity(
            "#joinrequest #new\n"
            f"{_chat_block(chat)}\n"
            f"{_user_block(user)}{invite_line}"
        ))

        if not await _notify_enabled(chat.id):
            return

        kb = InlineKeyboardMarkup([[
            ikb("✅ Aᴄᴄᴇᴘᴛ", callback_data=f"jrq_ok_{chat.id}_{user.id}"),
            ikb("❌ Rᴇᴊᴇᴄᴛ", callback_data=f"jrq_no_{chat.id}_{user.id}"),
        ]])
        sent = None
        try:
            sent = await client.send_message(
                chat.id,
                card(
                    "Nᴇᴡ Jᴏɪɴ Rᴇϙᴜᴇsᴛ",
                    f"🚪 <b>Ek nayi join request aayi hai!</b>\n\n"
                    f"{_user_block(user)}{invite_line}\n\n"
                    "🎶 <i>Sirf admins / sudo users hi decide kar sakte hain.</i>",
                ),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception as exc:
            LOGGER.debug("joinreq: group notify failed: %s", exc)

        _pending[(chat.id, user.id)] = {
            "msg_id": getattr(sent, "id", None),
            "ts": time.time(),
        }
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("joinreq notify error: %s", exc)


@bot.on_callback_query(filters.regex(r"^jrq_(ok|no)_(-?\d+)_(\d+)$"))
@error_handler
async def joinreq_action_cb(client: Client, cb: CallbackQuery):
    _, action, chat_id, user_id = cb.data.split("_", 3)
    chat_id, user_id = int(chat_id), int(user_id)

    if not await cb_admin_or_auth(client, cb, chat_id=chat_id):
        return await cb.answer(
            "❌ Sirf admins / sudo users hi ye kar sakte hain.", show_alert=True
        )

    approve = action == "ok"
    call = (
        client.approve_chat_join_request(chat_id, user_id)
        if approve
        else client.decline_chat_join_request(chat_id, user_id)
    )
    ok, err = await safe_call(call)
    _pending.pop((chat_id, user_id), None)

    actor = cb.from_user
    actor_name = html.escape(actor.first_name or "Admin")
    if not ok:
        await cb.answer("⚠️ Request ab valid nahi hai.", show_alert=True)
        spawn(log_activity(
            "#joinrequest #failed\n"
            f"• Cʜᴀᴛ ID: <code>{chat_id}</code>\n"
            f"• Usᴇʀ ID: <code>{user_id}</code>\n"
            f"• Eʀʀᴏʀ: <code>{err}</code>"
        ))
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    verdict = "#accepted" if approve else "#rejected"
    spawn(log_activity(
        f"#joinrequest {verdict}\n"
        f"• Cʜᴀᴛ ID: <code>{chat_id}</code>\n"
        f"• Usᴇʀ ID: <code>{user_id}</code>\n"
        f"• Bʏ: <a href=\"tg://user?id={actor.id}\">{actor_name}</a> "
        f"(<code>{actor.id}</code>)"
    ))

    await cb.answer("✅ Accepted!" if approve else "❌ Rejected!", show_alert=False)
    try:
        await cb.message.edit_text(
            card(
                "Jᴏɪɴ Rᴇϙᴜᴇsᴛ",
                ("✅ <b>Request accept ho gayi.</b>" if approve
                 else "❌ <b>Request reject kar di gayi.</b>")
                + f"\n\n• Usᴇʀ ID: <code>{user_id}</code>\n"
                  f"• Bʏ: <a href=\"tg://user?id={actor.id}\">{actor_name}</a>",
            ),
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass


@bot.on_chat_member_updated(group=8)
async def joinreq_external_resolution(client: Client, upd: ChatMemberUpdated):
    """Button dabaye bina accept/reject hua to bhi log group me notify."""
    try:
        member = upd.new_chat_member or upd.old_chat_member
        user = getattr(member, "user", None)
        if not user:
            return
        key = (upd.chat.id, user.id)
        meta = _pending.pop(key, None)
        if meta is None:
            return

        status = str(getattr(upd.new_chat_member, "status", "")).lower()
        joined = "member" in status or "administrator" in status or "owner" in status
        actor = upd.from_user
        actor_line = "• Bʏ: <code>unknown</code>"
        if actor:
            actor_line = (
                f"• Bʏ: <a href=\"tg://user?id={actor.id}\">"
                f"{html.escape(actor.first_name or 'Admin')}</a> "
                f"(<code>{actor.id}</code>)"
            )

        spawn(log_activity(
            f"#joinrequest #external {'#accepted' if joined else '#rejected'}\n"
            f"{_chat_block(upd.chat)}\n"
            f"{_user_block(user)}\n"
            f"{actor_line}\n"
            "• Nᴏᴛᴇ: <i>button dabaye bina resolve hui</i>"
        ))

        if meta.get("msg_id"):
            await safe_call(client.edit_message_reply_markup(
                upd.chat.id, meta["msg_id"], reply_markup=None
            ))

        # REQUESTED: "koi random admin join request reject karta hai to notify
        # karna" — group me bhi ek card bhejo, sirf log channel me nahi.
        if await get_setting_flag(upd.chat.id, "join_notify", True) is not False:
            verdict = ("✅ <b>Aᴄᴄᴇᴘᴛᴇᴅ</b>" if joined else "❌ <b>Rᴇᴊᴇᴄᴛᴇᴅ</b>")
            await safe_call(client.send_message(
                upd.chat.id,
                card(
                    "Jᴏɪɴ Rᴇqᴜᴇsᴛ Rᴇsᴏʟᴠᴇᴅ",
                    f"🚪 <b>Sᴛᴀᴛᴜs :</b> {verdict}\n"
                    f"{_user_block(user)}\n"
                    f"{actor_line.replace('• Bʏ:', '👮 <b>Bʏ :</b>')}\n\n"
                    "<i>Ye action Telegram ke apne menu se hua tha — buttons se nahi.</i>",
                    "Join Request Manager 🎶",
                ),
                parse_mode=enums.ParseMode.HTML,
            ))

    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("joinreq external resolution: %s", exc)
