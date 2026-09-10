"""
🧑‍💻 Source Code button + owner-side URL management.

Everyone sees a single "Source Code" button. Tapping it opens a small card
with an "Open Repository" link button.

The OWNER additionally gets management controls on that same card:

    • Set URL      → owner sends the new URL in the next private message
    • Remove URL   → restores the default repository URL
    • Current URL  → shows the active URL

Authorisation is enforced SERVER-SIDE on every callback (the buttons are not
merely hidden): each owner-only callback re-checks `cb.from_user.id` against
`Config.OWNER_ID`, so a spoofed/forwarded callback_data cannot reach it.

The custom URL is persisted in the existing `globals` collection through
utils.gc_db, so it survives restarts and fresh Heroku deploys.
"""
import html
import os
import re
import time

from pyrogram import Client, enums, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from melody import bot
from melody.config import Config
from utils.buttons import ikb
from utils.decorators import error_handler
from utils.gc_db import get_global, set_global
from strings.themes import BLUE, GREEN, RED, btn
from utils.melody_theme import headline

# PRIVACY: the repository URL is NEVER hardcoded here. It comes from the
# optional SOURCE_CODE_URL env var, or from the owner's in-bot "Set URL".
# With neither configured the card simply says the source is private.
DEFAULT_SOURCE_URL = (os.environ.get("SOURCE_CODE_URL") or "").strip()

_URL_RE = re.compile(r"^https?://[^\s<>\"']+\.[^\s<>\"']+$", re.IGNORECASE)

# owner_id -> deadline; set while we wait for the owner's next private message
_awaiting_url: dict = {}
_AWAIT_TTL = 180


async def get_source_url() -> str:
    """Active repository URL (owner override, else SOURCE_CODE_URL, else "")."""
    url = await get_global("source_url", None)
    return url if isinstance(url, str) and url else DEFAULT_SOURCE_URL


def _is_owner(user_id: int) -> bool:
    return user_id == Config.OWNER_ID


async def _safe_edit(cb: CallbackQuery, text: str, markup) -> None:
    """Edit the card, tolerating Telegram's MESSAGE_NOT_MODIFIED.

    BUG FIX ("Error in cb_source_remove: [400 MESSAGE_NOT_MODIFIED]"):
    removing an URL that was already the default re-rendered the SAME card,
    and Telegram rejects a no-op edit with a 400 that the error handler then
    reported as a crash. A no-op edit is a success for us.
    """
    try:
        await cb.message.edit_text(text, parse_mode=enums.ParseMode.HTML,
                                   reply_markup=markup)
    except MessageNotModified:
        pass
    except Exception:
        try:
            await cb.message.reply(text, parse_mode=enums.ParseMode.HTML,
                                   reply_markup=markup)
        except Exception:
            pass


async def _card(user_id: int) -> tuple:
    url = await get_source_url()
    if url:
        text = (
            f"{headline('Sᴏᴜʀᴄᴇ Cᴏᴅᴇ')}\n\n"
            "Melody is built in the open — read the code, learn from it, "
            "or self-host your own instance.\n\n"
            f"<blockquote>🔗 <code>{html.escape(url)}</code></blockquote>"
        )
        rows = [[ikb(btn("🌐 Open Repository", GREEN), url=url)]]
    else:
        text = (
            f"{headline('Sᴏᴜʀᴄᴇ Cᴏᴅᴇ')}\n\n"
            "This instance runs a <b>private</b> build — no public repository "
            "is published for it."
        )
        rows = []
    if _is_owner(user_id):
        rows.append([
            ikb(btn("✏️ Set URL", BLUE), callback_data="srccode_set"),
            ikb(btn("🗑 Remove URL", RED), callback_data="srccode_remove"),
        ])
        rows.append([ikb(btn("🔗 Current URL", BLUE), callback_data="srccode_current")])
    rows.append([ikb(btn("◀ Back", RED), callback_data="start_back")])
    return text, InlineKeyboardMarkup(rows)


@bot.on_callback_query(filters.regex(r"^srccode_main$"))
@error_handler
async def cb_source_main(client: Client, cb: CallbackQuery):
    text, markup = await _card(cb.from_user.id)
    await _safe_edit(cb, text, markup)
    await cb.answer()


@bot.on_callback_query(filters.regex(r"^srccode_current$"))
@error_handler
async def cb_source_current(client: Client, cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        return await cb.answer("❌ Owner only!", show_alert=True)
    await cb.answer(await get_source_url() or "No source URL configured.", show_alert=True)


@bot.on_callback_query(filters.regex(r"^srccode_set$"))
@error_handler
async def cb_source_set(client: Client, cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        return await cb.answer("❌ Owner only!", show_alert=True)
    _awaiting_url[cb.from_user.id] = time.monotonic() + _AWAIT_TTL
    await _safe_edit(
        cb,
        f"{headline('Sᴇᴛ Sᴏᴜʀᴄᴇ URL')}\n\n"
        "Send me the new repository URL in your next message "
        "(must start with <code>http://</code> or <code>https://</code>).\n\n"
        "<i>Expires in 3 minutes — send /cancel to abort.</i>",
        InlineKeyboardMarkup([
            [ikb(btn("◀ Back", RED), callback_data="srccode_main")],
        ]),
    )

    await cb.answer()


@bot.on_callback_query(filters.regex(r"^srccode_remove$"))
@error_handler
async def cb_source_remove(client: Client, cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        return await cb.answer("❌ Owner only!", show_alert=True)
    await set_global("source_url", "")
    _awaiting_url.pop(cb.from_user.id, None)
    text, markup = await _card(cb.from_user.id)
    await _safe_edit(cb, text, markup)
    await cb.answer("Custom URL removed ✅", show_alert=True)


# BUG FIX ("Source button me link set na ho rahi"): capture this private
# owner message in an early, dedicated handler group and stop propagation once
# the Set-URL flow consumes it.
@bot.on_message(filters.private & filters.text, group=-3)
@error_handler
async def capture_source_url(client: Client, message: Message):
    """Consume the owner's next private message while a Set-URL flow is open."""
    user = message.from_user
    if not user or not _is_owner(user.id):
        return
    deadline = _awaiting_url.get(user.id)
    if not deadline:
        return
    if time.monotonic() > deadline:
        _awaiting_url.pop(user.id, None)
        return

    raw = (message.text or "").strip()
    if raw.lower() in ("/cancel", "cancel"):
        _awaiting_url.pop(user.id, None)
        return await message.reply("❌ Cancelled.", parse_mode=enums.ParseMode.HTML)
    if raw.startswith("/"):
        return
    if not _URL_RE.match(raw) or len(raw) > 256:
        return await message.reply(
            "⚠️ That doesn't look like a valid URL. Send a full "
            "<code>https://…</code> link, or /cancel.",
            parse_mode=enums.ParseMode.HTML,
        )

    await set_global("source_url", raw)
    _awaiting_url.pop(user.id, None)
    text, markup = await _card(user.id)
    await message.reply(text, parse_mode=enums.ParseMode.HTML, reply_markup=markup)
    message.stop_propagation()


@bot.on_message(filters.command(["setsource", "setsourceurl"]) & filters.private)
@error_handler
async def set_source_cmd(client: Client, message: Message):
    """Direct owner command — works even if the button flow is interrupted."""
    user = message.from_user
    if not user or not _is_owner(user.id):
        return
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2:
        _awaiting_url[user.id] = time.monotonic() + _AWAIT_TTL
        return await message.reply(
            "✏️ Send me the repository URL in your next message "
            "(or use <code>/setsource https://…</code>).",
            parse_mode=enums.ParseMode.HTML,
        )
    raw = parts[1].strip()
    if not _URL_RE.match(raw) or len(raw) > 256:
        return await message.reply(
            "⚠️ That doesn't look like a valid URL.",
            parse_mode=enums.ParseMode.HTML,
        )
    await set_global("source_url", raw)
    _awaiting_url.pop(user.id, None)
    text, markup = await _card(user.id)
    await message.reply(text, parse_mode=enums.ParseMode.HTML, reply_markup=markup)
