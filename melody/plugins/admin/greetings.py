"""
👋 Welcome / Goodbye greetings — Rose-bot command set, Melody (Modi–Meloni) theme.

REQUESTED
---------
"welcome, left add kr for group. Koi user group me aata hai, ya left hota hai to
 best thumbnail, mast text … Meloni Modi ki melody theme me. Aur rose bot ki trh
 reset welcome, welcome on/off, custom welcome etc all cmnds add krna."

Commands (group admins / sudo)
------------------------------
    /welcome                      → status card + inline toggles
    /welcome on|off               → enable / disable welcomes
    /setwelcome <text>            → custom text (reply works too)
    /resetwelcome                 → back to the Melody default text
    /welcometest  /testwelcome    → preview the welcome on yourself
    /welcomecard on|off           → the generated thumbnail on/off
    /setwelcomeimage              → reply to a photo → custom welcome image
    /delwelcomeimage              → remove the custom image
    /cleanwelcome on|off          → delete the previous welcome card
    /cleanservice on|off          → delete Telegram's "X joined" service msg
    /goodbye on|off               → enable / disable goodbyes
    /setgoodbye <text>            → custom goodbye text
    /resetgoodbye                 → back to the Melody default
    /goodbyetest                  → preview the goodbye
    /goodbyecard on|off           → goodbye thumbnail on/off
    /cleangoodbye on|off          → delete the previous goodbye card
    /resetgreetings               → wipe ALL greeting settings for this chat
    /greetings                    → the full panel

Placeholders usable in /setwelcome + /setgoodbye
------------------------------------------------
    {mention} {first} {last} {fullname} {username} {id}
    {title} {chatname} {count}
Buttons (Rose syntax):  [Label](buttonurl://https://example.com)
                        [Same row](buttonurl://https://x.com:same)
"""
from __future__ import annotations

import html
import re

from pyrogram import Client, enums, filters
from pyrogram.errors import ChatSendPhotosForbidden, Forbidden
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from pyrogram.types import LinkPreviewOptions
from utils.reply import reply_params

from melody import bot
from utils.client_cache import get_me_cached
from melody.logging import LOGGER, log_activity
from utils.admin_tools import auto_delete, card, close_kb, is_admin, mention
from utils.buttons import ikb
from utils.decorators import cb_admin_or_auth, error_handler
from utils.greet_db import get_greet, reset_greet, set_greet
from utils.welcome_card import cleanup as _card_cleanup
from utils.welcome_card import render_greet_card
from utils.tasks import spawn

# ── default themed texts ─────────────────────────────────────────────────────

DEFAULT_WELCOME = (
    "<blockquote>🎶 <b>Sᴡᴀᴀɢᴀᴛ ʜᴀɪ {mention} !</b></blockquote>\n"
    "🌸 <b>{title}</b> ᴍᴇɪɴ ᴀᴀᴘᴋᴀ ᴅɪʟ sᴇ ᴡᴇʟᴄᴏᴍᴇ.\n"
    "👥 <b>Mᴇᴍʙᴇʀ :</b> <code>#{count}</code>\n\n"
    "🎧 <i>VC on karo aur</i> <code>/play tum hi ho</code> <i>bhejo — "
    "mehfil meri zimmedari 💛</i>\n"
    "📖 <code>/help</code> — saari commands"
)

DEFAULT_GOODBYE = (
    "<blockquote>🥀 <b>Aʟᴠɪᴅᴀ {mention}</b></blockquote>\n"
    "🚪 <b>{title}</b> sᴇ ᴄʜᴀʟᴇ ɢᴀʏᴇ…\n"
    "👥 <b>Bᴀᴄʜᴇ ʜᴜᴇ ᴍᴇᴍʙᴇʀs :</b> <code>{count}</code>\n\n"
    "🎶 <i>Gaana chalta rahega, yaadein bhi 💛</i>"
)

_BUTTON_RE = re.compile(r"\[([^\[\]]+?)\]\(buttonurl://(.+?)(:same)?\)", re.IGNORECASE)


# ── helpers ──────────────────────────────────────────────────────────────────

def _split_buttons(text: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Extract Rose-style [Label](buttonurl://url) buttons from the text."""
    rows: list[list] = []
    for label, url, same in _BUTTON_RE.findall(text):
        button = ikb(label.strip(), url=url.strip())
        if same and rows:
            rows[-1].append(button)
        else:
            rows.append([button])
    clean = _BUTTON_RE.sub("", text).strip()
    return clean, (InlineKeyboardMarkup(rows) if rows else None)


def _fill(template: str, user, chat, count: int | None) -> str:
    first = html.escape(getattr(user, "first_name", None) or "User")
    last = html.escape(getattr(user, "last_name", None) or "")
    uname = f"@{user.username}" if getattr(user, "username", None) else first
    title = html.escape(getattr(chat, "title", None) or "this group")
    values = {
        "mention": f'<a href="tg://user?id={user.id}">{first}</a>',
        "first": first,
        "last": last,
        "fullname": (first + (" " + last if last else "")),
        "username": html.escape(uname),
        "id": str(getattr(user, "id", 0)),
        "title": title,
        "chatname": title,
        "count": str(count if count is not None else "?"),
    }
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


async def _member_count(client: Client, chat_id: int) -> int | None:
    try:
        return await client.get_chat_members_count(chat_id)
    except Exception:
        return None


async def _gate(client: Client, message: Message) -> bool:
    if not message.from_user:
        return False
    if await is_admin(client, message.chat.id, message.from_user.id):
        return True
    m = await message.reply(
        card("Aᴄᴄᴇss Dᴇɴɪᴇᴅ", "⚠️ <b>Sɪʀғ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴs</b> greetings badal sakte hain."),
        parse_mode=enums.ParseMode.HTML,
    )
    await auto_delete(m, 20)
    return False


def _arg(message: Message) -> str:
    parts = (message.text or "").split(None, 1)
    return parts[1].strip().lower() if len(parts) > 1 else ""


def _on_off(value: str) -> bool | None:
    if value in ("on", "yes", "true", "enable", "enabled", "1"):
        return True
    if value in ("off", "no", "false", "disable", "disabled", "0"):
        return False
    return None


async def _toggle_cmd(client: Client, message: Message, key: str, label: str):
    if not await _gate(client, message):
        return
    state = _on_off(_arg(message))
    cfg = await get_greet(message.chat.id)
    if state is None:
        return await message.reply(
            card(
                label,
                f"⚙️ <b>Aʙʜɪ :</b> {'✅ ON' if cfg.get(key) else '❌ OFF'}\n\n"
                f"<i>Use</i> <code>/{message.command[0]} on</code> <i>ya</i> "
                f"<code>/{message.command[0]} off</code>",
            ),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=close_kb(),
        )
    await set_greet(message.chat.id, key, state)
    await message.reply(
        card(label, f"{'✅' if state else '❌'} <b>{label} ab {'ON' if state else 'OFF'} hai.</b>"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


# ── the actual greeting sender ───────────────────────────────────────────────

async def _send_greeting(client: Client, chat, user, kind: str, cfg: dict, reply_to: int | None = None):
    template = cfg.get(f"{kind}_text") or (DEFAULT_WELCOME if kind == "welcome" else DEFAULT_GOODBYE)
    count = await _member_count(client, chat.id)
    text, markup = _split_buttons(_fill(template, user, chat, count))

    # REQUESTED: "non premium user ke welcome mesg me bhi premium emojis khud
    # se laga dena." Plain glyphs get a premium custom-emoji entity; the ids a
    # premium user explicitly set (already inside <emoji> tags) are protected
    # and pass through untouched.
    try:
        from utils.telegram_html import auto_premium_emoji

        text = auto_premium_emoji(text)
    except Exception as exc:  # never let cosmetics kill a greeting
        LOGGER.debug("premium-emoji upgrade skipped for %s: %s", chat.id, exc)

    # Clean the previous card so the group never fills up with greetings.
    if cfg.get(f"clean_{kind}") and cfg.get(f"last_{kind}"):
        try:
            await client.delete_messages(chat.id, cfg[f"last_{kind}"])
        except Exception:
            pass

    photo = cfg.get("welcome_media") if kind == "welcome" else None
    generated = None
    if not photo and cfg.get(f"{kind}_card", True):
        generated = await render_greet_card(client, user, chat, kind=kind, member_no=count)
        photo = generated

    sent = None
    try:
        if photo:
            sent = await client.send_photo(
                chat.id,
                photo,
                caption=text,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=markup,
                reply_parameters=reply_params(reply_to),
            )
        else:
            sent = await client.send_message(
                chat.id,
                text,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_parameters=reply_params(reply_to),
            )
    except (ChatSendPhotosForbidden, Forbidden) as exc:
        # Some channels/groups explicitly disallow media. The text greeting is
        # still valid; do not report an expected permission fallback as a bot
        # warning or let it obscure real greeting failures.
        LOGGER.info("greeting photo unavailable in %s; using text fallback: %s", chat.id, exc)
        try:
            sent = await client.send_message(
                chat.id, text, parse_mode=enums.ParseMode.HTML,
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception:
            sent = None
    except Exception as exc:
        LOGGER.warning("greeting send failed in %s: %s", chat.id, exc)
        try:
            sent = await client.send_message(
                chat.id, text, parse_mode=enums.ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception:
            sent = None
    finally:
        _card_cleanup(generated)

    if sent:
        await set_greet(chat.id, f"last_{kind}", sent.id)
    return sent


# ── join / left handlers ─────────────────────────────────────────────────────

# Unique dispatcher group: safemode also watches new members (group=7).
@bot.on_message(filters.new_chat_members & filters.group, group=6)
@error_handler
async def on_join(client: Client, message: Message):
    me = await get_me_cached(client)
    cfg = await get_greet(message.chat.id)

    if cfg.get("clean_service"):
        try:
            await message.delete()
        except Exception:
            pass

    if not cfg.get("welcome", True):
        return

    for member in message.new_chat_members or []:
        if member.id == me.id:
            continue  # bot's own "thanks for adding me" card lives in start.py
        if getattr(member, "is_bot", False):
            continue
        try:
            await _send_greeting(client, message.chat, member, "welcome", cfg)
        except Exception as exc:
            LOGGER.warning("welcome failed in %s: %s", message.chat.id, exc)
        cfg = await get_greet(message.chat.id)


@bot.on_message(filters.left_chat_member & filters.group, group=6)
@error_handler
async def on_left(client: Client, message: Message):
    me = await get_me_cached(client)
    member = message.left_chat_member
    if not member or member.id == me.id or getattr(member, "is_bot", False):
        return
    cfg = await get_greet(message.chat.id)

    if cfg.get("clean_service"):
        try:
            await message.delete()
        except Exception:
            pass
    if not cfg.get("goodbye", True):
        return
    try:
        await _send_greeting(client, message.chat, member, "goodbye", cfg)
    except Exception as exc:
        LOGGER.warning("goodbye failed in %s: %s", message.chat.id, exc)


# ── /welcome, /goodbye status + toggles ──────────────────────────────────────

def _panel_kb(cfg: dict) -> InlineKeyboardMarkup:
    def mark(key: str, label: str) -> str:
        return f"{'✅' if cfg.get(key) else '❌'} {label}"

    return InlineKeyboardMarkup([
        [ikb(mark("welcome", "Welcome"), callback_data="greet_t_welcome"),
         ikb(mark("goodbye", "Goodbye"), callback_data="greet_t_goodbye")],
        [ikb(mark("welcome_card", "Welcome Card"), callback_data="greet_t_welcome_card"),
         ikb(mark("goodbye_card", "Goodbye Card"), callback_data="greet_t_goodbye_card")],
        [ikb(mark("clean_welcome", "Clean Welcome"), callback_data="greet_t_clean_welcome"),
         ikb(mark("clean_goodbye", "Clean Goodbye"), callback_data="greet_t_clean_goodbye")],
        [ikb(mark("clean_service", "Delete 'joined' msg"), callback_data="greet_t_clean_service")],
        [ikb("🧪 Tᴇsᴛ Wᴇʟᴄᴏᴍᴇ", callback_data="greet_test_welcome"),
         ikb("♻️ Rᴇsᴇᴛ Aʟʟ", callback_data="greet_reset_all")],
        [ikb("✖️ Cʟᴏsᴇ", callback_data="gc_close")],
    ])


def _status_text(cfg: dict) -> str:
    wt = "custom" if cfg.get("welcome_text") else "default (Melody theme)"
    gt = "custom" if cfg.get("goodbye_text") else "default (Melody theme)"
    img = "custom photo" if cfg.get("welcome_media") else (
        "generated thumbnail" if cfg.get("welcome_card") else "text only")
    return card(
        "Gʀᴇᴇᴛɪɴɢs",
        "👋 <b>Wᴇʟᴄᴏᴍᴇ / Gᴏᴏᴅʙʏᴇ sᴇᴛᴜᴘ</b>\n\n"
        f"🌸 <b>Wᴇʟᴄᴏᴍᴇ :</b> {'✅ ON' if cfg.get('welcome') else '❌ OFF'} · <i>{wt}</i>\n"
        f"🖼 <b>Vɪsᴜᴀʟ :</b> <i>{img}</i>\n"
        f"🥀 <b>Gᴏᴏᴅʙʏᴇ :</b> {'✅ ON' if cfg.get('goodbye') else '❌ OFF'} · <i>{gt}</i>\n"
        f"🧹 <b>Cʟᴇᴀɴ Oʟᴅ Cᴀʀᴅs :</b> "
        f"{'✅' if cfg.get('clean_welcome') else '❌'} welcome · "
        f"{'✅' if cfg.get('clean_goodbye') else '❌'} goodbye\n"
        f"🗑 <b>Sᴇʀᴠɪᴄᴇ Msɢ Dᴇʟᴇᴛᴇ :</b> {'✅' if cfg.get('clean_service') else '❌'}\n\n"
        "<blockquote expandable>"
        "🧾 <b>Cᴏᴍᴍᴀɴᴅs</b>\n"
        "┌ <code>/welcome on|off</code> · <code>/setwelcome &lt;text&gt;</code>\n"
        "├ <code>/resetwelcome</code> · <code>/welcometest</code>\n"
        "├ <code>/setwelcomeimage</code> (reply photo) · <code>/delwelcomeimage</code>\n"
        "├ <code>/welcomecard on|off</code> · <code>/cleanwelcome on|off</code>\n"
        "├ <code>/goodbye on|off</code> · <code>/setgoodbye &lt;text&gt;</code>\n"
        "├ <code>/resetgoodbye</code> · <code>/goodbyetest</code>\n"
        "└ <code>/cleanservice on|off</code> · <code>/resetgreetings</code>\n\n"
        "🔤 <b>Pʟᴀᴄᴇʜᴏʟᴅᴇʀs</b>\n"
        "<code>{mention} {first} {last} {fullname} {username} {id} {title} {count}</code>\n\n"
        "🔗 <b>Bᴜᴛᴛᴏɴs</b>\n"
        "<code>[Rules](buttonurl://https://t.me/x)</code>\n"
        "<code>[Support](buttonurl://https://t.me/y:same)</code>"
        "</blockquote>",
        "Melody · Modi–Meloni greeting engine 🎶",
    )


@bot.on_message(filters.command(["welcome", "greetings", "greeting"]) & filters.group)
@error_handler
async def welcome_cmd(client: Client, message: Message):
    if not await _gate(client, message):
        return
    state = _on_off(_arg(message))
    if state is not None:
        await set_greet(message.chat.id, "welcome", state)
        return await message.reply(
            card("Wᴇʟᴄᴏᴍᴇ", f"{'✅' if state else '❌'} <b>Welcome messages ab "
                             f"{'ON' if state else 'OFF'} hain.</b>"),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=close_kb(),
        )
    cfg = await get_greet(message.chat.id)
    await message.reply(
        _status_text(cfg), parse_mode=enums.ParseMode.HTML, reply_markup=_panel_kb(cfg)
    )


@bot.on_message(filters.command(["goodbye", "leftmsg", "left"]) & filters.group)
@error_handler
async def goodbye_cmd(client: Client, message: Message):
    await _toggle_cmd(client, message, "goodbye", "Gᴏᴏᴅʙʏᴇ")


@bot.on_message(filters.command(["cleanwelcome"]) & filters.group)
@error_handler
async def cleanwelcome_cmd(client: Client, message: Message):
    await _toggle_cmd(client, message, "clean_welcome", "Cʟᴇᴀɴ Wᴇʟᴄᴏᴍᴇ")


@bot.on_message(filters.command(["cleangoodbye"]) & filters.group)
@error_handler
async def cleangoodbye_cmd(client: Client, message: Message):
    await _toggle_cmd(client, message, "clean_goodbye", "Cʟᴇᴀɴ Gᴏᴏᴅʙʏᴇ")


@bot.on_message(filters.command(["cleanservice", "cleanservicemsg"]) & filters.group)
@error_handler
async def cleanservice_cmd(client: Client, message: Message):
    await _toggle_cmd(client, message, "clean_service", "Sᴇʀᴠɪᴄᴇ Msɢ Cʟᴇᴀɴ")


@bot.on_message(filters.command(["welcomecard", "welcomethumb"]) & filters.group)
@error_handler
async def welcomecard_cmd(client: Client, message: Message):
    await _toggle_cmd(client, message, "welcome_card", "Wᴇʟᴄᴏᴍᴇ Cᴀʀᴅ")


@bot.on_message(filters.command(["goodbyecard", "goodbyethumb"]) & filters.group)
@error_handler
async def goodbyecard_cmd(client: Client, message: Message):
    await _toggle_cmd(client, message, "goodbye_card", "Gᴏᴏᴅʙʏᴇ Cᴀʀᴅ")


# ── set / reset text ─────────────────────────────────────────────────────────

def _raw_arg(message: Message) -> str:
    """The greeting template as HTML — premium emoji and formatting intact.

    ROOT FIX (REQUESTED: "welcome mesg me premium emojis support add kr …
    premium user jo emojis set karega bs vahi emojis use kar"): this used to
    return `message.text`, the PLAIN unicode string. Every custom-emoji entity
    (that IS the premium emoji) collapsed to its boring fallback glyph before
    it ever reached the database, so a premium user's animated emoji could
    never be greeted back. `message_html()` re-emits the user's own entities —
    `<emoji id="…">` included — so exactly the ids they picked are stored.
    Plain glyphs typed by a NON-premium user stay plain here and are upgraded
    automatically by `auto_premium_emoji()` at send time, which never touches
    a glyph that is already inside an <emoji> tag.
    """
    from utils.entity_html import message_html

    replied = message.reply_to_message
    if replied and (replied.text or replied.caption):
        return message_html(replied).strip()
    return message_html(message, skip_command=True).strip()


async def _set_text(client: Client, message: Message, kind: str, label: str):
    if not await _gate(client, message):
        return
    text = _raw_arg(message)
    if not text:
        return await message.reply(
            card(
                f"Sᴇᴛ {label}",
                f"🧾 <code>/set{kind} &lt;text&gt;</code> <i>ya kisi text pe reply karo.</i>\n\n"
                "🔤 <b>Placeholders :</b> <code>{mention} {first} {fullname} "
                "{username} {id} {title} {count}</code>\n"
                "🔗 <b>Button :</b> <code>[Rules](buttonurl://https://t.me/x)</code>",
            ),
            parse_mode=enums.ParseMode.HTML,
        )
    if len(text) > 3500:
        return await message.reply("⚠️ <i>Text bahut lamba hai (max 3500 chars).</i>",
                                   parse_mode=enums.ParseMode.HTML)
    await set_greet(message.chat.id, f"{kind}_text", text)
    await message.reply(
        card(f"{label} Sᴀᴠᴇᴅ", f"✅ <b>Naya {label.lower()} set ho gaya.</b>\n\n"
                                f"<i>Preview :</i> <code>/{kind}test</code>"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(filters.command("setwelcome") & filters.group)
@error_handler
async def setwelcome_cmd(client: Client, message: Message):
    await _set_text(client, message, "welcome", "Wᴇʟᴄᴏᴍᴇ")


@bot.on_message(filters.command("setgoodbye") & filters.group)
@error_handler
async def setgoodbye_cmd(client: Client, message: Message):
    await _set_text(client, message, "goodbye", "Gᴏᴏᴅʙʏᴇ")


@bot.on_message(filters.command(["resetwelcome", "clearwelcome"]) & filters.group)
@error_handler
async def resetwelcome_cmd(client: Client, message: Message):
    if not await _gate(client, message):
        return
    await reset_greet(message.chat.id, "welcome")
    await message.reply(
        card("Wᴇʟᴄᴏᴍᴇ Rᴇsᴇᴛ", "♻️ <b>Welcome wapas Melody default theme pe aa gaya.</b>"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(filters.command(["resetgoodbye", "cleargoodbye"]) & filters.group)
@error_handler
async def resetgoodbye_cmd(client: Client, message: Message):
    if not await _gate(client, message):
        return
    await reset_greet(message.chat.id, "goodbye")
    await message.reply(
        card("Gᴏᴏᴅʙʏᴇ Rᴇsᴇᴛ", "♻️ <b>Goodbye wapas Melody default theme pe.</b>"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(filters.command(["resetgreetings", "resetgreeting"]) & filters.group)
@error_handler
async def resetgreetings_cmd(client: Client, message: Message):
    if not await _gate(client, message):
        return
    await reset_greet(message.chat.id, "all")
    await message.reply(
        card("Gʀᴇᴇᴛɪɴɢs Rᴇsᴇᴛ", "♻️ <b>Saari greeting settings default ho gayi.</b>"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


# ── custom welcome image ─────────────────────────────────────────────────────

@bot.on_message(filters.command(["setwelcomeimage", "setwelcomepicgc"]) & filters.group)
@error_handler
async def setwelcomeimage_cmd(client: Client, message: Message):
    if not await _gate(client, message):
        return
    photo = None
    if message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo.file_id
    elif message.photo:
        photo = message.photo.file_id
    if not photo:
        return await message.reply(
            card("Sᴇᴛ Wᴇʟᴄᴏᴍᴇ Iᴍᴀɢᴇ",
                 "🖼 <i>Kisi photo pe reply karke</i> <code>/setwelcomeimage</code> <i>bhejo.</i>"),
            parse_mode=enums.ParseMode.HTML,
        )
    await set_greet(message.chat.id, "welcome_media", photo)
    await message.reply(
        card("Wᴇʟᴄᴏᴍᴇ Iᴍᴀɢᴇ Sᴇᴛ", "✅ <b>Ab isi photo pe welcome bhejunga.</b>\n\n"
                                    "<i>Generated card wapas chahiye to</i> <code>/delwelcomeimage</code>"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(filters.command(["delwelcomeimage", "rmwelcomeimage"]) & filters.group)
@error_handler
async def delwelcomeimage_cmd(client: Client, message: Message):
    if not await _gate(client, message):
        return
    await set_greet(message.chat.id, "welcome_media", None)
    await message.reply(
        card("Wᴇʟᴄᴏᴍᴇ Iᴍᴀɢᴇ Rᴇᴍᴏᴠᴇᴅ",
             "🗑 <b>Custom photo hata di — ab generated Melody card aayega.</b>"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


# ── tests ────────────────────────────────────────────────────────────────────

@bot.on_message(filters.command(["welcometest", "testwelcome"]) & filters.group)
@error_handler
async def welcometest_cmd(client: Client, message: Message):
    if not await _gate(client, message):
        return
    cfg = await get_greet(message.chat.id)
    cfg = dict(cfg)
    cfg["clean_welcome"] = False  # a test must never eat the real last card
    await _send_greeting(client, message.chat, message.from_user, "welcome", cfg,
                         reply_to=message.id)


@bot.on_message(filters.command(["goodbyetest", "testgoodbye"]) & filters.group)
@error_handler
async def goodbyetest_cmd(client: Client, message: Message):
    if not await _gate(client, message):
        return
    cfg = dict(await get_greet(message.chat.id))
    cfg["clean_goodbye"] = False
    await _send_greeting(client, message.chat, message.from_user, "goodbye", cfg,
                         reply_to=message.id)


# ── panel callbacks ──────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^greet_(t|test|reset)_([a-z_]+)$"))
@error_handler
async def greet_cb(client: Client, cb: CallbackQuery):
    _, action, key = cb.data.split("_", 2)
    chat_id = cb.message.chat.id
    if not await cb_admin_or_auth(client, cb, chat_id=chat_id):
        return await cb.answer("❌ Sirf admins / sudo.", show_alert=True)

    if action == "t":
        cfg = await get_greet(chat_id)
        await set_greet(chat_id, key, not bool(cfg.get(key)))
        cfg = await get_greet(chat_id)
        await cb.answer(f"{key.replace('_', ' ')} → {'ON' if cfg.get(key) else 'OFF'}")
        try:
            await cb.message.edit_text(
                _status_text(cfg), parse_mode=enums.ParseMode.HTML, reply_markup=_panel_kb(cfg)
            )
        except Exception:
            pass
        return

    if action == "test":
        cfg = dict(await get_greet(chat_id))
        cfg["clean_welcome"] = False
        await cb.answer("Bhej diya 🎶")
        return await _send_greeting(client, cb.message.chat, cb.from_user, "welcome", cfg)

    await reset_greet(chat_id, "all")
    cfg = await get_greet(chat_id)
    await cb.answer("Reset ✅")
    try:
        await cb.message.edit_text(
            _status_text(cfg), parse_mode=enums.ParseMode.HTML, reply_markup=_panel_kb(cfg)
        )
    except Exception:
        pass
    spawn(log_activity(
        f"#greetings #reset\n🏠 <code>{chat_id}</code>\n👮 {mention(cb.from_user)}"
    ))
