"""
💚 /alive — Melody heartbeat card (with its own picture).

Picture is set once with /setalivepic and survives reboots (stored in Mongo
as a file_id — see melody/plugins/misc/pics.py).
"""
import time

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, Message
from utils.buttons import ikb  # premium-emoji + styled buttons (safe on every fork)

from melody import bot
from melody.config import Config
from utils.admin_tools import card
from utils.decorators import error_handler
from melody.plugins.misc.pics import send_with_pic

_START_TS = time.time()


def _uptime() -> str:
    delta = int(time.time() - _START_TS)
    d, rem = divmod(delta, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}d {h}h {m}m {s}s"


@bot.on_message(filters.command(["alive", "uptime"]))
@error_handler
async def alive_cmd(client: Client, message: Message):
    text = card(
        "MELODY IS ALIVE",
        "💚 <b>Sᴛᴀᴛᴜs :</b> Online &amp; streaming\n"
        f"⏱ <b>Uᴘᴛɪᴍᴇ :</b> <code>{_uptime()}</code>\n"
        "🎧 <b>Eɴɢɪɴᴇ :</b> py-tgcalls · HD audio\n"
        f"♛ <b>Pᴏᴡᴇʀᴇᴅ ʙʏ :</b> <code>{Config.OWNER_NAME}</code>",
        "Music never stops here 🎶",
    )
    markup = InlineKeyboardMarkup([
        [
            ikb("📖 Hᴇʟᴘ", callback_data="help_main"),
            ikb("🏓 Pɪɴɢ", callback_data="ping_cb"),
        ],
        [ikb("✵ CLOSE ✵", callback_data="gc_close")],
    ])
    await send_with_pic(client, message.chat.id, "alive", text, markup, message.id)


@bot.on_callback_query(filters.regex(r"^alive_cb$"))
@error_handler
async def alive_cb(client: Client, cb):
    await cb.answer("💚 Melody is alive!")
    await send_with_pic(
        client,
        cb.message.chat.id,
        "alive",
        card(
            "MELODY IS ALIVE",
            "💚 <b>Sᴛᴀᴛᴜs :</b> Online &amp; streaming\n"
            f"⏱ <b>Uᴘᴛɪᴍᴇ :</b> <code>{_uptime()}</code>\n"
            f"♛ <b>Pᴏᴡᴇʀᴇᴅ ʙʏ :</b> <code>{Config.OWNER_NAME}</code>",
            "Music never stops here 🎶",
        ),
        InlineKeyboardMarkup([[ikb("✵ CLOSE ✵", callback_data="gc_close")]]),
    )
