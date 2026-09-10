"""
💬 /vcchatlog — voice-chat chat log: status card + 2 buttons (no timers).

Buttons on every card (REQUESTED — sirf 2):
  • 🟢/🔴 VC chat log ON/OFF (OFF flips back ON automatically after 30 min)
  • ✵ Close ✵

REQUESTED: "vc chat card se all typ ka time hata de" — the card carries no
clock, no countdown and no auto-delete timer any more, so the old timer
presets / `/vcchatlog time` option are gone and cards stay forever.

Only real in-call chat is logged. The group-chat mirror was removed
(REQUESTED: "gc chat log system hata de").
"""

from pyrogram import Client, enums, filters
from pyrogram.types import CallbackQuery, Message

from melody import assistant, bot
from melody.core import vc_chat_log, vc_listener
from utils.admin_tools import is_admin
from utils.decorators import error_handler
from utils.tasks import spawn

# Attach the in-call chat reader to the assistant account.
vc_chat_log.register(assistant)


async def _card(chat_id: int) -> tuple:
    state = await vc_chat_log.log_enabled(chat_id)

    try:
        from melody.core.vc_notify import call_is_live

        reader = "🛰 ᴠᴄ ʟɪᴠᴇ" if await call_is_live(chat_id) else "💤 ɴᴏ ʟɪᴠᴇ ᴠᴏɪᴄᴇ ᴄʜᴀᴛ"
    except Exception:
        reader = "—"

    from melody.core.vc_theme import FLAGS, vc_card

    # No time of any kind on this card (REQUESTED).
    text = vc_card(
        "Vc Chat Log",
        f"⚡ <b>Sᴛᴀᴛᴜs :</b> {'🟢 ON' if state else '🔴 OFF'}"
        f"\n🎙 <b>Vᴏɪᴄᴇ ᴄʜᴀᴛ :</b> {reader}"
        "\n🗂 <b>Cᴀʀᴅs :</b> ᴘᴇʀᴍᴀɴᴇɴᴛ (ɴᴏ ᴀᴜᴛᴏ-ᴅᴇʟᴇᴛᴇ)"
        "\n🎯 <b>Sᴏᴜʀᴄᴇ :</b> sɪʀғ VC ᴋᴇ ᴀɴᴅᴀʀ ᴋɪ ᴄʜᴀᴛ (ɢʀᴏᴜᴘ ᴄʜᴀᴛ ɴᴇᴠᴇʀ)",
        footer=(
            "<code>/vcchatlog on</code> · <code>/vcchatlog off</code> · "
            f"{FLAGS}"
        ),
        tags="#VᴄCʜᴀᴛLᴏɢ 💬",
    )
    return text, vc_chat_log.log_keyboard(chat_id, state)


@bot.on_message(filters.command("vcchatlog") & filters.group)
@error_handler
async def vcchatlog_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    args = message.text.split()[1:]
    vc_listener.watch(chat_id)

    if args:
        action = args[0].lower()
        if action in ("on", "off", "enable", "disable", "time", "sec", "delete"):
            if not await is_admin(client, chat_id, message.from_user.id):
                await message.reply_text("🚫 Aᴅᴍɪɴs ᴏɴʟʏ.")
                return
        if action in ("on", "enable"):
            await vc_chat_log.set_enabled(chat_id, True)
        elif action in ("off", "disable"):
            await vc_chat_log.set_enabled(chat_id, False)
        elif action in ("time", "sec", "delete"):
            # REQUESTED: every kind of time was removed from the VC chat card.
            await message.reply_text(
                "⏱ Tɪᴍᴇ ᴏᴘᴛɪᴏɴ ʜᴀᴛᴀ ᴅɪ ɢᴀʏɪ ʜᴀɪ — VC ᴄʜᴀᴛ ᴄᴀʀᴅ ᴀʙ ᴋᴀʙʜɪ "
                "ᴅᴇʟᴇᴛᴇ ɴᴀʜɪ ʜᴏᴛᴀ.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

    await vc_listener.ensure(chat_id)
    text, keyboard = await _card(chat_id)
    await message.reply_text(
        text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard
    )


@bot.on_callback_query(filters.regex(r"^vclog:toggle:(-?\d+)$"))
@error_handler
async def vclog_cb(client: Client, query: CallbackQuery):
    chat_id = int(query.data.split(":")[-1])

    if not await is_admin(client, chat_id, query.from_user.id):
        await query.answer("🚫 Admins only.", show_alert=True)
        return

    state = await vc_chat_log.log_enabled(chat_id)
    await vc_chat_log.set_enabled(chat_id, not state)
    await query.answer(
        "🔴 VC chat log OFF — 30 min me khud ON ho jayega."
        if state else "🟢 VC chat log ON."
    )

    text, keyboard = await _card(chat_id)
    try:
        await query.message.edit_text(
            text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard
        )
    except Exception:
        pass


# ───────────── keep the outside-the-call VC watcher armed (no logging) ───────
# Ordinary group traffic is NEVER logged any more; it is only a free trigger to
# re-check whether a voice chat is live in this chat.
@bot.on_message(filters.group & ~filters.service, group=28)
async def _vc_watch_tap(client: Client, message: Message):
    from pyrogram import ContinuePropagation

    try:
        spawn(vc_listener.note(message.chat.id))
    except Exception:
        pass
    raise ContinuePropagation
