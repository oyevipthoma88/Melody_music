"""
🏓 /ping — latency card with its own picture (set via /setpingpic).
"""
import time

from pyrogram import Client, enums, filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from utils.buttons import ikb  # premium-emoji + styled buttons (safe on every fork)

from melody import bot
from utils.admin_tools import card
from utils.decorators import error_handler
from melody.plugins.misc.pics import send_with_pic


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            ikb("📖 Hᴇʟᴘ", callback_data="help_main"),
            ikb("💚 Aʟɪᴠᴇ", callback_data="alive_cb"),
        ],
        [ikb("✵ CLOSE ✵", callback_data="gc_close")],
    ])


def _text(ms: float) -> str:
    bar = "🟩" * max(1, min(5, 6 - int(ms // 150))) + "⬜" * (5 - max(1, min(5, 6 - int(ms // 150))))
    return card(
        "Pᴏɴɢ !",
        f"🏓 <b>Rᴇsᴘᴏɴsᴇ :</b> <code>{ms:.2f} ms</code>\n"
        f"📶 <b>Sɪɢɴᴀʟ :</b> {bar}\n"
        "🎧 <b>Sᴛᴀᴛᴜs :</b> Online &amp; ready",
        "Melody is alive and kicking 🎶",
    )


@bot.on_message(filters.command("ping"))
@error_handler
async def ping_cmd(client: Client, message: Message):
    start = time.monotonic()
    probe = await message.reply("🏓 <b>Pinging...</b>", parse_mode=enums.ParseMode.HTML)
    elapsed = (time.monotonic() - start) * 1000
    try:
        await probe.delete()
    except Exception:
        pass
    await send_with_pic(client, message.chat.id, "ping", _text(elapsed), _kb(), message.id)


@bot.on_callback_query(filters.regex(r"^ping_cb$"))
@error_handler
async def ping_cb(client: Client, cb: CallbackQuery):
    start = time.monotonic()
    await cb.answer("🏓 Pinging...")
    elapsed = (time.monotonic() - start) * 1000
    await send_with_pic(client, cb.message.chat.id, "ping", _text(elapsed), _kb())
