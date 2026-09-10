"""
🤫 Whisper — secret messages, premium style.

REQUESTED (latest)
------------------
"whisper mesg bohot jada bada show hota hai usko thoda short me aur best premium
tym me show kr. premium user agr whisper karega to premium emojis me chala jaye.
Agr user ne whisper read kiya to whisper ko 'read' yese kuch show krna. Ek
everyone wala add krna — mtlb koi bhi random user wo mesg read kr skta hai aur
read krne ke bad uska name show hoga."

WHAT CHANGED
------------
• The public card is now ONE short premium line instead of a 4-line block.
• Telegram Premium senders automatically get the premium emoji set (💎🩷✨),
  normal senders get the clean set (🤫🔒) — same layout, different polish.
• As soon as the receiver opens it, the card is edited to
  "👀 Read by <name> · 10:42" (a real read receipt, like DM read ticks).
• New `everyone` target: `@bot everyone your text` / `/whisper everyone text`.
  ANY user can open it once, and every reader's name is shown on the card.

Ways to send one
----------------
    @YourBot @username your secret text
    @YourBot everyone your secret text
    /whisper @username your secret text
    /whisper your secret text            (as a reply)
"""
from __future__ import annotations

import html
import re
import time

from pyrogram import Client, enums, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
    Message,
)

from melody import bot
from melody.config import Config
from melody.logging import LOGGER
from utils.admin_tools import card, mention_id
from utils.decorators import error_handler
from utils.whisper_db import (
    MAX_TEXT,
    add_reader,
    bind_target,
    delete_whisper,
    get_whisper,
    mark_opened,
    new_token,
    save_whisper,
)

_USERNAME_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})")
_ID_RE = re.compile(r"(?:^|\s)(\d{5,15})(?:\s|$)")
_EVERYONE_RE = re.compile(r"(?:^|\s)(everyone|all|anyone)(?:\s|$)", re.I)

EVERYONE = "everyone"
ALERT_LIMIT = 190
MAX_NAMES_ON_CARD = 5


# ─────────────────────────── premium presentation ───────────────────────────
# REQUESTED: premium sender → premium emoji set.
_PREMIUM = {"lock": "💎", "secret": "🩷", "open": "✨ Rᴇᴠᴇᴀʟ", "read": "🩷", "tag": "ᴘʀᴇᴍɪᴜᴍ"}
_NORMAL = {"lock": "🔒", "secret": "🤫", "open": "🔓 Sʜᴏᴡ", "read": "👀", "tag": ""}


def _skin(doc: dict) -> dict:
    return _PREMIUM if doc.get("premium") else _NORMAL


def _target_label(doc: dict) -> str:
    if doc.get("everyone"):
        return "🌐 <b>Eᴠᴇʀʏᴏɴᴇ</b>"
    if doc.get("target_id") and doc.get("target_name"):
        return mention_id(doc["target_id"], doc["target_name"])
    if doc.get("target_username"):
        return f"@{html.escape(doc['target_username'])}"
    if doc.get("target_id"):
        return f"<code>{doc['target_id']}</code>"
    return "❓ Uɴᴋɴᴏᴡɴ"


def _readers_line(doc: dict) -> str:
    """"👀 Read by X, Y" — the read receipt (REQUESTED)."""
    readers = doc.get("readers") or []
    if not readers:
        return ""
    skin = _skin(doc)
    names = [mention_id(r["id"], r.get("name") or "User") for r in readers[:MAX_NAMES_ON_CARD]]
    extra = len(readers) - len(names)
    joined = ", ".join(names) + (f" +{extra}" if extra > 0 else "")
    when = time.strftime("%H:%M", time.localtime(readers[-1].get("at", time.time())))
    return f"\n{skin['read']} <b>Rᴇᴀᴅ ʙʏ</b> {joined} · <code>{when}</code>"


def _whisper_card(doc: dict) -> str:
    """Short + premium (REQUESTED: pehle bohot bada dikh raha tha)."""
    skin = _skin(doc)
    tag = f" · <i>{skin['tag']}</i>" if skin["tag"] else ""
    head = (
        f"{skin['secret']} <b>Wʜɪsᴘᴇʀ</b> ➡️ {_target_label(doc)}{tag}\n"
        f"{skin['lock']} <i>from</i> {mention_id(doc['sender_id'], doc['sender_name'])}"
    )
    return head + _readers_line(doc)


def _whisper_kb(doc: dict) -> InlineKeyboardMarkup:
    from utils.buttons import ikb, STYLE_SUCCESS, STYLE_DANGER

    skin = _skin(doc)
    return InlineKeyboardMarkup(
        [
            [ikb(skin["open"], style=STYLE_SUCCESS, callback_data=f"wh:open:{doc['token']}")],
            [ikb("🗑 Dᴇsᴛʀᴏʏ", style=STYLE_DANGER, callback_data=f"wh:del:{doc['token']}")],
        ]
    )


# ─────────────────────────────── parsing ────────────────────────────────────
def _parse(query: str):
    """Return (username, user_id, everyone, text)."""
    text = (query or "").strip()
    username = None
    user_id = None
    everyone = False

    m = _EVERYONE_RE.search(text)
    if m:
        everyone = True
        text = (text[: m.start()] + " " + text[m.end():]).strip()
    else:
        m = _USERNAME_RE.search(text)
        if m:
            username = m.group(1).lower()
            text = (text[: m.start()] + " " + text[m.end():]).strip()
        else:
            m = _ID_RE.search(text)
            if m:
                user_id = int(m.group(1))
                text = (text[: m.start()] + " " + text[m.end():]).strip()

    return username, user_id, everyone, re.sub(r"\s+", " ", text).strip()


async def _resolve(username, user_id):
    target = username or user_id
    if not target:
        return None
    try:
        return await bot.get_users(target)
    except Exception as exc:  # noqa: BLE001
        LOGGER.info(
            "whisper: target %s abhi resolve nahi hua (%s: %s) — open ke waqt dubara try karenge",
            target, type(exc).__name__, exc,
        )
        return None


# ─────────────────────────────── logging ────────────────────────────────────
async def _log(title: str, body: str) -> None:
    LOGGER.info("whisper-log | %s | %s", title, re.sub(r"<[^>]+>", "", body).replace("\n", " · "))
    chat_id = getattr(Config, "LOG_GROUP_ID", 0)
    if not chat_id:
        return
    try:
        await bot.send_message(
            chat_id,
            card(title, body),
            parse_mode=enums.ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "whisper: LOG_GROUP_ID (%s) me log nahi bhej paye (%s: %s) — bot ko us group me admin banao",
            chat_id, type(exc).__name__, exc,
        )


def _where(obj) -> str:
    chat = getattr(obj, "chat", None)
    if chat is None:
        return "inline (unknown chat)"
    title = html.escape(str(getattr(chat, "title", "") or "private"))
    return f"{title} (<code>{chat.id}</code>)"


def _usage_card() -> str:
    return card(
        "Wʜɪsᴘᴇʀ",
        "🤫 <code>@bot @username secret text</code>\n"
        "🌐 <code>@bot everyone secret text</code>\n"
        "💬 <code>/whisper @username secret text</code>",
    )


# ─────────────────────────────── inline mode ────────────────────────────────
@bot.on_inline_query(group=3)
@error_handler
async def whisper_inline(client: Client, iq: InlineQuery):
    query = (iq.query or "").strip()
    if not query:
        return

    username, user_id, everyone, text = _parse(query)

    if not text or not (username or user_id or everyone):
        try:
            await iq.answer(
                results=[
                    InlineQueryResultArticle(
                        title="🤫 Whisper — usage",
                        description="@bot @username secret · @bot everyone secret",
                        input_message_content=InputTextMessageContent(
                            _usage_card(), parse_mode=enums.ParseMode.HTML
                        ),
                    )
                ],
                cache_time=1,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("whisper: usage hint answer failed: %s", exc)
        return

    user = None if everyone else await _resolve(username, user_id)
    token = new_token()
    doc = await save_whisper(
        token,
        sender_id=iq.from_user.id,
        sender_name=iq.from_user.first_name or "User",
        text=text[:MAX_TEXT],
        target_id=None if everyone else (getattr(user, "id", None) or user_id),
        target_username=None if everyone else (username or getattr(user, "username", None)),
        target_name=None if everyone else getattr(user, "first_name", None),
        everyone=everyone,
        premium=bool(getattr(iq.from_user, "is_premium", False)),
    )

    who = (
        "everyone"
        if everyone
        else (getattr(user, "first_name", None) or ("@" + username if username else user_id))
    )
    await iq.answer(
        results=[
            InlineQueryResultArticle(
                id=token,
                title=f"{_skin(doc)['secret']} Whisper to {who}",
                description=f"{len(text)} chars · tap to send",
                input_message_content=InputTextMessageContent(
                    _whisper_card(doc), parse_mode=enums.ParseMode.HTML
                ),
                reply_markup=_whisper_kb(doc),
            )
        ],
        cache_time=0,
        is_personal=True,
    )

    await _log(
        "Wʜɪsᴘᴇʀ Cʀᴇᴀᴛᴇᴅ",
        f"✍️ <b>Fʀᴏᴍ :</b> {mention_id(iq.from_user.id, iq.from_user.first_name or 'User')} "
        f"(<code>{iq.from_user.id}</code>)\n"
        f"💎 <b>Pʀᴇᴍɪᴜᴍ :</b> <code>{bool(getattr(iq.from_user, 'is_premium', False))}</code>\n"
        f"🎯 <b>Tᴏ :</b> {_target_label(doc)}\n"
        f"🔑 <b>Tᴏᴋᴇɴ :</b> <code>{token}</code>\n"
        f"📥 <b>Vɪᴀ :</b> inline\n"
        f"📝 <b>Sᴇᴄʀᴇᴛ :</b> <code>{html.escape(text)}</code>",
    )


# ─────────────────────────────── /whisper command ───────────────────────────
@bot.on_message(filters.command(["whisper", "secret"]) & ~filters.private, group=3)
@error_handler
async def whisper_cmd(client: Client, msg: Message):
    payload = (msg.text or "").split(None, 1)
    body = payload[1].strip() if len(payload) > 1 else ""
    username, user_id, everyone, text = _parse(body)
    target_user = None

    if msg.reply_to_message and msg.reply_to_message.from_user and not (username or user_id or everyone):
        target_user = msg.reply_to_message.from_user
        user_id = target_user.id
        text = re.sub(r"\s+", " ", body).strip()
    elif not everyone:
        target_user = await _resolve(username, user_id)

    if not text or not (username or user_id or everyone):
        await msg.reply_text(_usage_card(), parse_mode=enums.ParseMode.HTML)
        return

    token = new_token()
    doc = await save_whisper(
        token,
        sender_id=msg.from_user.id,
        sender_name=msg.from_user.first_name or "User",
        text=text[:MAX_TEXT],
        target_id=None if everyone else (getattr(target_user, "id", None) or user_id),
        target_username=None if everyone else (username or getattr(target_user, "username", None)),
        target_name=None if everyone else getattr(target_user, "first_name", None),
        everyone=everyone,
        premium=bool(getattr(msg.from_user, "is_premium", False)),
    )

    await msg.reply_text(
        _whisper_card(doc),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_whisper_kb(doc),
    )
    try:
        await msg.delete()
    except Exception as exc:  # noqa: BLE001
        LOGGER.info(
            "whisper[%s]: original /whisper command delete nahi hua (%s: %s) — delete permission chahiye",
            msg.chat.id, type(exc).__name__, exc,
        )

    await _log(
        "Wʜɪsᴘᴇʀ Cʀᴇᴀᴛᴇᴅ",
        f"✍️ <b>Fʀᴏᴍ :</b> {mention_id(msg.from_user.id, msg.from_user.first_name or 'User')} "
        f"(<code>{msg.from_user.id}</code>)\n"
        f"💎 <b>Pʀᴇᴍɪᴜᴍ :</b> <code>{bool(getattr(msg.from_user, 'is_premium', False))}</code>\n"
        f"🎯 <b>Tᴏ :</b> {_target_label(doc)}\n"
        f"💬 <b>Cʜᴀᴛ :</b> {_where(msg)}\n"
        f"🔑 <b>Tᴏᴋᴇɴ :</b> <code>{token}</code>\n"
        f"📥 <b>Vɪᴀ :</b> /whisper command\n"
        f"📝 <b>Sᴇᴄʀᴇᴛ :</b> <code>{html.escape(text)}</code>",
    )


# ─────────────────────────────── open / destroy ─────────────────────────────
def _is_receiver(doc: dict, user) -> bool:
    if doc.get("everyone"):
        return True
    if doc.get("target_id") and user.id == doc["target_id"]:
        return True
    uname = (getattr(user, "username", "") or "").lower()
    return bool(doc.get("target_username") and uname == doc["target_username"])


@bot.on_callback_query(filters.regex(r"^wh:(open|del):(.+)$"), group=3)
@error_handler
async def whisper_cb(client: Client, cb: CallbackQuery):
    action, token = cb.matches[0].group(1), cb.matches[0].group(2)
    doc = await get_whisper(token)
    user = cb.from_user

    if not doc:
        await cb.answer("⌛ Ye whisper expire ho chuka ya destroy ho gaya hai.", show_alert=True)
        return

    is_sender = user.id == doc["sender_id"]
    is_receiver = _is_receiver(doc, user)

    if action == "del":
        if not is_sender:
            await cb.answer("🚫 Sirf bhejne wala hi ise destroy kar sakta hai.", show_alert=True)
            return
        await delete_whisper(token)
        try:
            await cb.edit_message_text(
                "🗑 <b>Wʜɪsᴘᴇʀ Dᴇsᴛʀᴏʏᴇᴅ</b> — sender ne delete kar diya.",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:  # noqa: BLE001
            pass
        await cb.answer("🗑 Destroyed.")
        await _log(
            "Wʜɪsᴘᴇʀ Dᴇsᴛʀᴏʏᴇᴅ",
            f"🗑 <b>Bʏ :</b> {mention_id(user.id, user.first_name or 'User')} "
            f"(<code>{user.id}</code>)\n🔑 <b>Tᴏᴋᴇɴ :</b> <code>{token}</code>",
        )
        return

    if not (is_receiver or is_sender):
        who = doc.get("target_name") or (
            "@" + doc["target_username"] if doc.get("target_username") else "receiver"
        )
        await cb.answer(f"🚫 Ye whisper aapke liye nahi hai — sirf {who} khol sakta hai.", show_alert=True)
        await _log(
            "Wʜɪsᴘᴇʀ Dᴇɴɪᴇᴅ",
            f"🚫 <b>Tʀɪᴇᴅ Bʏ :</b> {mention_id(user.id, user.first_name or 'User')} "
            f"(<code>{user.id}</code>)\n"
            f"🎯 <b>Bᴇʟᴏɴɢs Tᴏ :</b> {_target_label(doc)}\n"
            f"🔑 <b>Tᴏᴋᴇɴ :</b> <code>{token}</code>",
        )
        return

    if is_receiver and not doc.get("everyone") and not doc.get("target_id"):
        await bind_target(token, user.id)

    text = doc["text"]
    opens = await mark_opened(token) if is_receiver else int(doc.get("opens", 0))

    if len(text) <= ALERT_LIMIT:
        await cb.answer(f"{_skin(doc)['secret']} {text}", show_alert=True)
    else:
        sent_dm = False
        try:
            await bot.send_message(
                user.id,
                f"{_skin(doc)['secret']} <b>Wʜɪsᴘᴇʀ</b>\n\n{html.escape(text)}",
                parse_mode=enums.ParseMode.HTML,
            )
            sent_dm = True
        except Exception as exc:  # noqa: BLE001
            LOGGER.info(
                "whisper: DM delivery failed for %s (%s: %s) — alert me trimmed text bhej rahe hain",
                user.id, type(exc).__name__, exc,
            )
        await cb.answer(
            "🤫 Message bada hai — maine aapko DM kar diya hai."
            if sent_dm
            else f"🤫 {text[:ALERT_LIMIT]}… (pura padhne ke liye bot ko /start karo)",
            show_alert=True,
        )

    # ── read receipt (REQUESTED) ────────────────────────────────────────────
    if is_receiver:
        fresh = await add_reader(token, user.id, user.first_name or "User") or doc
        try:
            await cb.edit_message_text(
                _whisper_card(fresh),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=_whisper_kb(fresh),
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("whisper: read-receipt edit skipped (%s: %s)", type(exc).__name__, exc)
        doc = fresh

    await _log(
        "Wʜɪsᴘᴇʀ Rᴇᴀᴅ" if is_receiver else "Wʜɪsᴘᴇʀ Rᴇ-ʀᴇᴀᴅ Bʏ Sᴇɴᴅᴇʀ",
        f"👀 <b>Oᴘᴇɴᴇᴅ Bʏ :</b> {mention_id(user.id, user.first_name or 'User')} "
        f"(<code>{user.id}</code>)\n"
        f"✍️ <b>Sᴇɴᴅᴇʀ :</b> {mention_id(doc['sender_id'], doc['sender_name'])}\n"
        f"🎯 <b>Tᴀʀɢᴇᴛ :</b> {_target_label(doc)}\n"
        f"🌐 <b>Eᴠᴇʀʏᴏɴᴇ ᴍᴏᴅᴇ :</b> <code>{bool(doc.get('everyone'))}</code>\n"
        f"🔁 <b>Oᴘᴇɴs :</b> <code>{opens}</code>\n"
        f"⏱ <b>Aɢᴇ :</b> <code>{int(time.time() - doc.get('created_at', time.time()))}s</code>\n"
        f"🔑 <b>Tᴏᴋᴇɴ :</b> <code>{token}</code>\n"
        f"📝 <b>Sᴇᴄʀᴇᴛ :</b> <code>{html.escape(text)}</code>",
    )
