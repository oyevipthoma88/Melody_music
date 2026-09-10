"""
🎙 /vcnotify — VC join / left card ON-OFF switch.

REQUESTED ("vc join left wale ko toggle aur help menu me call kr"):
the join/left announcement cards already respected the `vc_activity` flag,
but that flag could only be flipped from the /settings panel. This adds a
direct command (and a tappable button) so any group admin can silence or
re-enable the "joined / left the voice chat" cards instantly.

    /vcnotify            → show current state + toggle button
    /vcnotify on|off     → set it directly
"""
from pyrogram import Client, enums, filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from melody import bot
from utils.admin_tools import is_admin
from utils.buttons import ikb
from utils.decorators import admin_or_auth, error_handler
from utils.gc_db import get_setting_flag, set_setting_flag

FLAG = "vc_activity"


def _kb(state: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [ikb("🔴 Tᴜʀɴ OFF" if state else "🟢 Tᴜʀɴ ON",
             callback_data="vcnotify_toggle")],
        [ikb("CLOSE", callback_data="help_close")],
    ])


def _text(state: bool) -> str:
    dot = "🟢 <b>ON</b>" if state else "🔴 <b>OFF</b>"
    return (
        "<blockquote>🎙 <b>VC Jᴏɪɴ / Lᴇғᴛ Aʟᴇʀᴛs :</b> "
        f"{dot}</blockquote>\n\n"
        "Jab koi voice chat me <b>join</b> ya <b>leave</b> kare, Melody uska "
        "card bhejta hai.\n"
        "<code>/vcnotify on</code> · <code>/vcnotify off</code> — ya niche "
        "button dabao."
    )


@bot.on_message(
    filters.command(["vcnotify", "vcalerts", "vcjoinleft", "vcactivity"])
    & filters.group
)
@error_handler
@admin_or_auth
async def vcnotify_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    state = bool(await get_setting_flag(chat_id, FLAG, True))

    arg = (message.command[1].lower() if len(message.command) > 1 else "")
    if arg in ("on", "enable", "yes"):
        state = True
        await set_setting_flag(chat_id, FLAG, True)
    elif arg in ("off", "disable", "no"):
        state = False
        await set_setting_flag(chat_id, FLAG, False)

    await message.reply(
        _text(state),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_kb(state),
    )


@bot.on_callback_query(filters.regex(r"^vcnotify_toggle$"))
@error_handler
async def vcnotify_cb(client: Client, cb: CallbackQuery):
    chat_id = cb.message.chat.id
    if not await is_admin(client, chat_id, cb.from_user.id):
        await cb.answer("Sirf admins 🔒", show_alert=True)
        return
    state = not bool(await get_setting_flag(chat_id, FLAG, True))
    await set_setting_flag(chat_id, FLAG, state)
    await cb.answer(f"VC join/left alerts {'ON' if state else 'OFF'}")
    try:
        await cb.message.edit_text(
            _text(state),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_kb(state),
        )
    except Exception:
        pass
