"""
📣 Random "add me to your group" promo.

REQUEST: "Randomly kabhi bhi kisi bhi user ko tag karke usko bot add karne ke
liye bolna, button ke saath — /promo on off command."

Behaviour: while a group is active, roughly one in every `_CHANCE` messages
triggers a beautifully-formatted card that tags the user who just spoke and
invites them to add Melody to their own group. Rate-limited to one card per
chat per `_COOLDOWN` seconds so it never feels like spam, and auto-deletes
after 3 minutes.

Control:  OWNER PANEL only (👑 /panel → Promo Reminders).
          Normal users / group admins have NO /promo command any more —
          the old `/promo off` user command was removed on request.
Global:   /promoglobal on | off      (bot owner — kills it everywhere)
"""
import random
import time

from pyrogram import Client, ContinuePropagation, enums, filters
from pyrogram.types import InlineKeyboardMarkup, Message
from utils.buttons import ikb  # premium-emoji + styled buttons (safe on every fork)

from melody import bot
from melody.config import Config
from utils.admin_tools import auto_delete, card, mention
from utils.decorators import error_handler, owner_only
from utils.gc_db import get_global, set_global

_CHANCE = 60          # 1-in-60 messages
_COOLDOWN = 1800      # at most one promo per chat per 30 min
_last_promo: dict = {}

_LINES = (
    "Apne group me bhi Melody bulao — HD music, zero lag 🎧",
    "Tumhare group me gaane kaun bajata hai? Melody try karo 🎶",
    "Ek click me apne group ka DJ set karo 🎛",
    "Voice chat sunsan hai? Melody ko add karo 🔥",
)


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [ikb(
            "➕ Aᴅᴅ Mᴇʟᴏᴅʏ ᴛᴏ Yᴏᴜʀ Gʀᴏᴜᴘ",
            url=f"https://t.me/{Config.BOT_USERNAME.lstrip('@')}?startgroup=true",
        )],
        [
            ikb("📖 Hᴇʟᴘ", callback_data="help_main"),
            ikb("✵ CLOSE ✵", callback_data="gc_close"),
        ],
    ])


# Belt-and-braces: this filter matches EVERY group message, so if anything is
# ever registered in group 11 again it must not be starved. `ContinuePropagation`
# is raised OUTSIDE the try/except below (a bare `except Exception` would eat it).
@bot.on_message(filters.group & ~filters.service, group=11)
async def random_promo(client: Client, message: Message):
    await _random_promo(client, message)
    raise ContinuePropagation


async def _random_promo(client: Client, message: Message):
    try:
        if not message.from_user or message.from_user.is_bot:
            return
        if not await get_global("promo", True):
            return
        now = time.time()
        if now - _last_promo.get(message.chat.id, 0) < _COOLDOWN:
            return
        if random.randint(1, _CHANCE) != 1:
            return
        _last_promo[message.chat.id] = now

        sent = await message.reply(
            card(
                "Mᴇʟᴏᴅʏ Iɴᴠɪᴛᴇ",
                f"👋 {mention(message.from_user)}\n\n🎵 <i>{random.choice(_LINES)}</i>",
            ),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_kb(),
        )
        await auto_delete(sent, 180)
    except Exception:
        pass


@bot.on_message(filters.command("promoglobal"))
@owner_only
@error_handler
async def promo_global_cmd(client: Client, message: Message):
    arg = (message.command[1].lower() if len(message.command) > 1 else "status")
    if arg in ("on", "off"):
        await set_global("promo", arg == "on")
    state = await get_global("promo", True)
    await message.reply(
        card("Gʟᴏʙᴀʟ Pʀᴏᴍᴏ", f"🌍 <b>Sᴛᴀᴛᴜs :</b> {'🟢 ON' if state else '🔴 OFF'}"),
        parse_mode=enums.ParseMode.HTML,
    )
