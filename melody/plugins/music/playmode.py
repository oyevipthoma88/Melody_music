"""
🎛 /playmode — who can play, and how results are picked.

MISSING-FEATURE PARITY: every top music bot (Yukki, AnonXMusic, VIPMusic,
TgMusicBot) ships /playmode. Melody hard-coded BOTH halves of it:

  • access — /play was permanently admin/auth-only, so in a normal group no
    member could ever queue a song. Top bots default to "everyone" and let
    admins lock it down.
  • search — /play always auto-played the top result. Top bots let a chat
    switch to "inline", where /play lists the 5 best matches as buttons.

Both are per-chat settings, read by `utils.decorators.playmode_gate` and by
`/play` itself.

    /playmode                 → live status card
    /playmode everyone|admin  → who may use /play
    /playmode direct|inline   → auto-play top hit vs. show a chooser
"""
import html

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from melody import bot
from utils.database import get_setting, set_setting
from utils.decorators import admin_or_auth, error_handler
from utils.formatters import quote_html

ACCESS_KEY = "playmode_access"     # "everyone" | "admin"
SEARCH_KEY = "playmode_search"     # "direct" | "inline"

ACCESS_DEFAULT = "everyone"
SEARCH_DEFAULT = "direct"

_ACCESS_WORDS = {
    "everyone": "everyone", "all": "everyone", "member": "everyone",
    "members": "everyone", "public": "everyone", "open": "everyone",
    "admin": "admin", "admins": "admin", "adminonly": "admin", "group": "admin",
}
_SEARCH_WORDS = {
    "direct": "direct", "auto": "direct", "instant": "direct",
    "inline": "inline", "search": "inline", "choose": "inline", "list": "inline",
}


async def get_access_mode(chat_id: int) -> str:
    value = await get_setting(chat_id, ACCESS_KEY, ACCESS_DEFAULT)
    return value if value in ("everyone", "admin") else ACCESS_DEFAULT


async def get_search_mode(chat_id: int) -> str:
    value = await get_setting(chat_id, SEARCH_KEY, SEARCH_DEFAULT)
    return value if value in ("direct", "inline") else SEARCH_DEFAULT


def _card(access: str, search: str) -> str:
    access_line = (
        "👥 <b>Everyone</b> — koi bhi member gaana laga sakta hai"
        if access == "everyone"
        else "🛡 <b>Admins only</b> — sirf admin / auth users"
    )
    search_line = (
        "⚡ <b>Direct</b> — top result turant baj jata hai"
        if search == "direct"
        else "🔎 <b>Inline</b> — 5 results aate hain, tap karke chuno"
    )
    return (
        "🎛 <b>Play Mode</b>\n\n"
        f"🔸 Access : {access_line}\n"
        f"🔸 Search : {search_line}\n\n"
        "<code>/playmode everyone</code> · <code>/playmode admin</code>\n"
        "<code>/playmode direct</code> · <code>/playmode inline</code>"
    )


@bot.on_message(filters.command(["playmode", "pmode"]) & filters.group)
@error_handler
@admin_or_auth
async def playmode_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    args = [a.lower() for a in message.command[1:]]

    if not args:
        await message.reply(
            quote_html(_card(await get_access_mode(chat_id), await get_search_mode(chat_id))),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    changed = []
    for word in args:
        if word in _ACCESS_WORDS:
            await set_setting(chat_id, ACCESS_KEY, _ACCESS_WORDS[word])
            changed.append("access")
        elif word in _SEARCH_WORDS:
            await set_setting(chat_id, SEARCH_KEY, _SEARCH_WORDS[word])
            changed.append("search")

    if not changed:
        await message.reply(
            quote_html(
                f"❌ <b>“{html.escape(' '.join(args)[:30])}”</b> samajh nahi aaya.\n\n"
                + _card(await get_access_mode(chat_id), await get_search_mode(chat_id))
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    await message.reply(
        quote_html(
            "✅ <b>Play mode updated.</b>\n\n"
            + _card(await get_access_mode(chat_id), await get_search_mode(chat_id))
        ),
        parse_mode=enums.ParseMode.HTML,
    )
