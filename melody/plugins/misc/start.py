"""
🚀 /start — Ultra-premium Melody welcome
   • HTML blockquote + bold/italic formatting everywhere
   • Animated sticker on start (if WELCOME_STICKER set)
   • User buttons vs Owner panel buttons
   • Color-coded emoji buttons (🔵 normal | 🔴 danger | 🟢 music)
   • Rich "thank you" welcome card when bot is added to a group
"""
import html
import os
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, CallbackQuery
from pyrogram.types import LinkPreviewOptions
from utils.buttons import ikb, ButtonStyle  # premium-emoji + styled buttons (safe on every fork)
from melody import bot
from melody.config import Config
from melody.logging import LOGGER, log_activity
from utils.decorators import error_handler
from utils.database import is_banned, is_gbanned, get_chat_owner
from utils.formatters import send_quote, premium_emoji, PREMIUM_EMOJI_IDS
from strings.themes import BLUE, RED, GREEN, btn, fancy
from utils.melody_theme import headline
from utils.tasks import spawn

ASSETS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "assets")
BG_START = os.path.join(ASSETS, "bg_start.png")
# Dedicated picture for the "bot added to a group" welcome card — set via
# /setwelcomepic (see melody/plugins/owner/set_welcome_pic.py). Falls back
# to BG_START, then to a text-only card, if not set.
BG_WELCOME = os.path.join(ASSETS, "bg_welcome.png")

_START_DB_TIMEOUT = 2.0
_START_SEND_TIMEOUT = 12.0


async def _start_db_call(awaitable, default):
    """Keep /start responsive when Mongo is slow or temporarily unavailable."""
    try:
        return await asyncio.wait_for(awaitable, timeout=_START_DB_TIMEOUT)
    except Exception as exc:
        LOGGER.warning("/start database lookup failed or timed out: %s", exc)
        return default


async def _forget_bad_pic(key: str, value: str) -> None:
    """Drop a stored file_id Telegram no longer accepts.

    ROOT-CAUSE FIX for the repeating Heroku warning
      "/start photo failed; trying fallback: [400 MEDIA_EMPTY]".
    A file_id saved by /setstartpic goes stale when the source message is
    deleted or the bot token changes. Without deleting it, EVERY /start pays
    a failed upload round-trip before falling back — slow and noisy forever.
    """
    try:
        from utils.gc_db import del_pic

        await asyncio.wait_for(del_pic(key), timeout=_START_DB_TIMEOUT)
        LOGGER.warning(
            "Stored '%s' picture (%s…) is no longer valid on Telegram — removed "
            "from the database. Set a new one with /setstartpic.",
            key, str(value)[:18],
        )
    except Exception as exc:  # noqa: BLE001 — cleanup must never break /start
        LOGGER.warning("could not drop stale '%s' picture: %s", key, exc)


_MEDIA_DEAD = ("MEDIA_EMPTY", "FILE_REFERENCE", "WEBPAGE_MEDIA_EMPTY", "PHOTO_INVALID")


async def _send_start_welcome(message, client, markup, start_pic=None):
    """Send the DM welcome with a guaranteed text fallback.

    A stale Telegram file_id, a missing local image, or a slow media upload must
    never leave the user with only the command-lock reaction.
    """
    candidates: list[tuple[str, str | None]] = []
    if isinstance(start_pic, str) and start_pic.strip():
        candidates.append((start_pic.strip(), "start"))
    if os.path.exists(BG_START) and os.path.getsize(BG_START) > 0:
        candidates.append((BG_START, None))

    for picture, db_key in candidates:
        try:
            return await asyncio.wait_for(
                message.reply_photo(
                    picture,
                    caption=WELCOME_DM,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=markup,
                ),
                timeout=_START_SEND_TIMEOUT,
            )
        except Exception as exc:
            LOGGER.warning("/start photo failed; trying fallback: %s", exc)
            if db_key and any(code in str(exc).upper() for code in _MEDIA_DEAD):
                await _forget_bad_pic(db_key, picture)

    return await asyncio.wait_for(
        send_quote(message, WELCOME_DM, client=client, reply_markup=markup),
        timeout=_START_SEND_TIMEOUT,
    )


# ─── Formatted text blocks ────────────────────────────────────────────────────

WELCOME_DM = (
    "<blockquote>"
    f"{premium_emoji(PREMIUM_EMOJI_IDS['start'], '🎶')} <b>{fancy('MELODY MUSIC')}</b>\n"
    f"<i>{fancy('music + group manager + owner assistant')}</i>"
    "</blockquote>\n"
    "🎧 <b>Hey!</b> ᴍᴀɪɴ <b>Mᴇʟᴏᴅʏ</b> — ᴛᴇʀᴇ ɢʀᴏᴜᴘ ᴋɪ ᴀᴡᴀᴀᴢ ᴀᴜʀ ᴄʜᴏᴡᴋɪᴅᴀʀ 🎤🛡\n"
    "<i>Ek bot me VC music + full group management + security.</i>\n"
    "<blockquote expandable>"
    "⚡ <b>Mᴀɪɴ ᴋʏᴀ ᴋᴀʀ sᴀᴋᴛᴀ ʜᴜ̃</b>\n"
    "┌ 🎵 <b>Music :</b> HD VC play · <code>/vplay</code> video · queue · loop · autoplay · lyrics\n"
    "├ 👋 <b>Greetings :</b> welcome / goodbye cards with photo — <code>/welcome</code>\n"
    "├ 🚪 <b>Join Requests :</b> one-tap accept/reject · <code>/rapproveall</code>\n"
    "├ 🛠 <b>Admin Suite :</b> ban · mute · warn · purge · <code>/cleanall</code> · <code>/tagall</code>\n"
    "├ 🛡 <b>Protection :</b> links · NSFW · flood · abuse filter\n"
    "└ 👑 <b>Owner Assistant :</b> mass-ban / takeover se group bachata hai"
    "</blockquote>"
    "🚀 <b>3 sᴛᴇᴘ sᴇᴛᴜᴘ :</b> <i>Add karo → full admin do → </i><code>/play tum hi ho</code>\n"
    f"♛ <b>ʙʏ</b> <code>{html.escape(Config.OWNER_NAME)}</code> 💛"
)

# Richer welcome text shown when the bot is added to a group.
# {chat} and {user} are substituted at send time.
WELCOME_GROUP = (
    "<blockquote>"
    f"🎶 <b>{fancy('MELODY MUSIC')}</b> ɪs ʟɪᴠᴇ ɪɴ <b>{{chat}}</b>! 🎉"
    "</blockquote>\n"
    "🙏 <b>Thanks {user}</b> — ᴀʙ ᴍᴇʜғɪʟ ᴀᴜʀ sᴜʀᴀᴋsʜᴀ ᴅᴏɴᴏ̃ ᴍᴇʀɪ ᴢɪᴍᴍᴇᴅᴀʀɪ 🎧🛡\n"
    "<blockquote expandable>"
    "🎵 <b>Mᴜsɪᴄ</b>\n"
    "┌ <code>/play</code> · <code>/vplay</code> · <code>/queue</code> · <code>/skip</code> · <code>/stop</code>\n"
    "└ Loop · AutoPlay · Lyrics · live VC feed\n"
    "🛠 <b>Mᴀɴᴀɢᴇᴍᴇɴᴛ</b>\n"
    "┌ <code>/welcome</code> — welcome & goodbye cards\n"
    "├ <code>/promote</code> · <code>/demote</code> · <code>/ban</code> · <code>/mute</code> · <code>/warn</code>\n"
    "├ <code>/purge</code> · <code>/cleanall</code> · <code>/tagall</code> · <code>/rapproveall</code>\n"
    "└ <code>/protection</code> · <code>/settings</code> — one-tap toggles\n"
    "👑 <b>Sᴇᴄᴜʀɪᴛʏ</b>\n"
    "└ <code>/assistant</code> — mass-ban & takeover guard for the owner"
    "</blockquote>"
    "🔐 <b>Mujhe ғᴜʟʟ ᴀᴅᴍɪɴ banao</b> — <i>VC join, delete, invite, promote aur "
    "security guards tabhi kaam karte hain.</i> Kyun? Neeche button dabao 👇\n"
    f"♛ <i>Powered by {html.escape(Config.OWNER_NAME)}</i>"
)

# Trust panel: exactly which right is used for what, and what Melody never does.
TRUST_TEXT = (
    f"{headline('Wʜʏ Mᴇʟᴏᴅʏ ɴᴇᴇᴅs ᴀᴅᴍɪɴ ʀɪɢʜᴛs')}\n"
    "<b>Har right ka saaf kaam hai:</b>\n"
    "┌ 🎙 <b>Manage Video Chats</b> — VC start/join karke gaana bajane ke liye\n"
    "├ 🗑 <b>Delete Messages</b> — <code>/purge</code>, <code>/cleanall</code>, spam & "
    "welcome card cleanup\n"
    "├ 🚫 <b>Ban / Restrict</b> — <code>/ban</code>, <code>/mute</code>, warn system, zombies\n"
    "├ ➕ <b>Invite Users</b> — invite link, join request accept/reject\n"
    "├ 📌 <b>Pin Messages</b> — <code>/pin</code>, now-playing pin\n"
    "└ ⬆️ <b>Add New Admins</b> — <code>/promote</code> aur <b>Owner Assistant</b> ka "
    "auto-demote (iske bina rogue admin ko rok nahi sakta)\n\n"
    "<blockquote expandable>"
    "🤝 <b>Mᴇʀᴀ ᴠᴀᴀᴅᴀ</b>\n"
    "┌ ❌ Kabhi group owner ko touch nahi karta\n"
    "├ ❌ Bina command khud se kisi ko ban/kick nahi karta\n"
    "├ ❌ Members ki personal chat kahin forward nahi hoti\n"
    "├ ✅ Har admin action log hota hai (<code>/assistant</code> audit)\n"
    "└ ✅ Owner jab chaahe <code>/settings</code> se sab off kar sakta hai"
    "</blockquote>"
    "<i>Trust ka koi shortcut nahi — sab kuch owner ke control me rehta hai.</i> 💛"
)




# ─── Button layouts (color-coded with emoji) ──────────────────────────────────

def user_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            ikb(btn("▶ Play Music"), switch_inline_query_current_chat=""),
            ikb(btn("📖 Help"), callback_data="help_main", style=ButtonStyle.SUCCESS),
        ],
        [
            ikb(
                btn("➕ Add Me to Group"),
                url=f"https://t.me/{Config.BOT_USERNAME.lstrip('@')}?startgroup=true",
            ),
        ],
        [
            ikb(btn("⚡ What I Can Do"), callback_data="melody_tour"),
            ikb(btn("🛡 Why Admin Rights?"), callback_data="melody_trust"),
        ],
        [
            ikb(btn("📢 Support"), url="https://t.me/+0000000000000000"),
            ikb(btn("ℹ About"), callback_data="about_cb"),
        ],

        [
            ikb(btn("🧑‍💻 Source Code"), callback_data="srccode_main"),
        ],
    ])


def owner_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            ikb(btn("▶ Play Music", RED), switch_inline_query_current_chat=""),
            ikb(btn("📖 Help", BLUE), callback_data="help_main"),
        ],
        [
            ikb(
                btn("➕ Add Me to Group", BLUE),
                url=f"https://t.me/{Config.BOT_USERNAME.lstrip('@')}?startgroup=true",
            ),
        ],
        [
            ikb(btn("🧑‍💻 Source Code", BLUE), callback_data="srccode_main"),
        ],
        [
            ikb(btn("👑 Owner Panel", RED), callback_data="owner_panel"),
        ],
    ])


def new_group_buttons(owner_id: int = None, owner_name: str = None) -> InlineKeyboardMarkup:
    """
    Rich button row shown when the bot is first added to a group.

    REQUEST: "jo user bot ko group add karega uska naam my cute owner me
    show kr" — whoever added the bot to this specific group gets credited
    as its "Cute Owner" with their own dedicated button (tapping it opens
    their profile). Falls back to no extra row if we somehow don't have an
    adder (e.g. legacy chat with no stored owner).
    """
    rows = [
        [
            ikb(btn("🎵 Play Music", GREEN), switch_inline_query_current_chat=""),
            ikb(btn("📖 Help", BLUE), callback_data="help_main"),
        ],
    ]
    if owner_id and owner_name:
        # FIX: tapping this button used to only pop up a text alert
        # (callback_data="cute_owner") instead of actually opening the
        # owner's profile. `tg://user?id=<id>` IS a Bot-API-supported URL
        # scheme for inline buttons (Telegram resolves it to that user's
        # profile inside the client) — it opens their chat/profile directly,
        # which is what was actually requested.
        rows.append([
            ikb(
                btn(f"👑 My Cute Owner: {owner_name}", GREEN),
                url=f"tg://user?id={owner_id}",
            ),
        ])
    rows.extend([
        [
            ikb(
                btn("➕ Add to Another Group", BLUE),
                url=f"https://t.me/{Config.BOT_USERNAME.lstrip('@')}?startgroup=true",
            ),
        ],
        [
            ikb(btn("🛡 Why Full Admin?", GREEN), callback_data="melody_trust"),
            ikb(btn("⚡ Feature Tour", BLUE), callback_data="melody_tour"),
        ],
        [
            ikb(btn("📢 Support Channel", BLUE), url="https://t.me/+0000000000000000"),
            ikb(btn("ℹ About", BLUE), callback_data="about_cb"),
        ],

        [
            ikb(btn("🧑‍💻 Source Code", BLUE), callback_data="srccode_main"),
        ],
    ])
    return InlineKeyboardMarkup(rows)


# ─── /start in DM ─────────────────────────────────────────────────────────────

@bot.on_message(filters.command("start") & filters.private)
@error_handler
async def start_dm(client: Client, message: Message):
    if message.from_user:
        user_id = message.from_user.id
        gbanned, banned = await asyncio.gather(
            _start_db_call(is_gbanned(user_id), False),
            _start_db_call(is_banned(user_id), False),
        )
        if gbanned:
            return
        if banned:
            return await message.reply(
                "<b>❌ You are banned from using this bot.</b>",
                parse_mode=enums.ParseMode.HTML,
            )

    is_owner = bool(message.from_user and message.from_user.id == Config.OWNER_ID)

    # ── Log who started the bot (owner launches vs. a regular user's first DM) ──
    user = message.from_user
    if user:
        role = "👑 Owner" if is_owner else "🙋 User"
        uname = f"@{user.username}" if user.username else "—"
        spawn(log_activity(
            f"#start #newuser\n"
            f"🚀 <b>Bot Started</b>\n"
            f"• {role}: <code>{html.escape(user.first_name)}</code> ({uname})\n"
            f"• User ID: <code>{user.id}</code>"
        ))

    # ── Animated sticker (if configured) ──
    if Config.WELCOME_STICKER:
        try:
            await message.reply_sticker(Config.WELCOME_STICKER)
        except Exception:
            pass

    markup = owner_buttons() if is_owner else user_buttons()

    # Owner-configured start picture (/setstartpic) wins over the bundled
    # background — it lives in the database as a file_id, so it survives
    # reboots and redeploys without re-uploading anything.
    from utils.gc_db import get_pic

    start_pic = await _start_db_call(get_pic("start"), None)
    await _send_start_welcome(message, client, markup, start_pic)


# ─── /start in Groups ─────────────────────────────────────────────────────────

@bot.on_message(filters.command("start") & filters.group)
@error_handler
async def start_group(client: Client, message: Message):
    if message.from_user:
        if await is_gbanned(message.from_user.id):
            return
        if await is_banned(message.from_user.id):
            return

    buttons = InlineKeyboardMarkup([
        [
            ikb(btn("▶ Play Music", RED), switch_inline_query_current_chat=""),
            ikb(btn("📖 Help", BLUE), callback_data="help_main"),
        ],
        [
            ikb(
                btn("➕ Add to Another Group", BLUE),
                url=f"https://t.me/{Config.BOT_USERNAME.lstrip('@')}?startgroup=true",
            ),
        ],
    ])
    text = (
        "<blockquote>"
        f"🎶 <b>{fancy('MELODY MUSIC')}</b> ɪs ʀᴇᴀᴅʏ ᴛᴏ ʀᴏᴄᴋ! 🎵\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
        "</blockquote>\n\n"
        "🎙 <b>Voice chat on karo, phir:</b>\n"
        "┌ 🎵 <code>/play &lt;song / YouTube link&gt;</code>\n"
        "├ 🎬 <code>/vplay &lt;song&gt;</code> — video stream\n"
        "├ 📋 <code>/queue</code> · ⏭ <code>/skip</code> · ⏹ <code>/stop</code>\n"
        "├ 🎚 <code>/settings</code> — sab features ON/OFF\n"
        "└ 📖 <code>/help</code> — poori command list\n\n"
        f"♛ <i>Powered by {html.escape(Config.OWNER_NAME)}</i>"
    )
    from utils.gc_db import get_pic

    start_pic = await get_pic("start")
    if start_pic:
        try:
            return await message.reply_photo(
                start_pic,
                caption=text,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=buttons,
            )
        except Exception:
            pass
    await send_quote(message, text, client=client, reply_markup=buttons)


# ─── Bot added to a new group — rich "thank you" welcome card ─────────────────

@bot.on_message(filters.new_chat_members)
@error_handler
async def new_group_handler(client: Client, message: Message):
    bot_user = await client.get_me()
    for member in message.new_chat_members:
        if member.id != bot_user.id:
            continue

        chat = message.chat
        from utils.owner_view import HIDDEN, is_anonymous_message

        anonymous_adder = is_anonymous_message(message)
        # Telegram uses sender_chat when an owner/admin acts anonymously.
        # Do not persist, log, mention, or button-link that hidden identity.
        adder = None if anonymous_adder else message.from_user

        from utils.database import add_chat
        await add_chat(
            chat.id,
            chat.title or "",
            owner_id=adder.id if adder else None,
            owner_name=(adder.first_name if adder else None),
        )

        member_count = None
        try:
            member_count = await client.get_chat_members_count(chat.id)
        except Exception:
            pass

        adder_uname = f"@{adder.username}" if (adder and adder.username) else "—"
        chat_link = (
            f"https://t.me/{chat.username}" if getattr(chat, "username", None)
            else "<i>private group</i>"
        )
        adder_link = (
            f"<a href=\"tg://user?id={adder.id}\">"
            f"{html.escape(adder.first_name or 'User')}</a>" if adder else HIDDEN
        )
        spawn(log_activity(
            "#newgc #added\n"
            f"➕ <b>New Group Added</b>\n"
            f"• Chat: <code>{html.escape(chat.title or '')}</code>\n"
            f"• Chat ID: <code>{chat.id}</code>\n"
            f"• Link: {chat_link}\n"
            f"• Type: <code>{chat.type.value if chat.type else 'unknown'}</code>\n"
            + (f"• Members: <code>{member_count}</code>\n" if member_count is not None else "")
            + f"• Added by: {adder_link} "
              + (f"({adder_uname}, <code>{adder.id}</code>)" if adder else "")
        ))

        adder_name = html.escape(adder.first_name) if adder else HIDDEN
        chat_name  = html.escape(chat.title or "this group")
        members_line = (
            f"\n👥 <b>Members:</b> <code>{member_count}</code>" if member_count else ""
        )

        # Build the caption with member count appended nicely
        caption = WELCOME_GROUP.format(chat=chat_name, user=adder_name) + members_line
        buttons = new_group_buttons(
            owner_id=adder.id if adder else None,
            owner_name=(adder.first_name if adder else None),
        )

        # Prefer the Mongo-stored welcome pic (file_id — reboot proof), then
        # the Mongo start pic, then the GitHub-restored files on disk, then a
        # text-only card. Same picture pipeline as /start, so whatever the
        # owner sets once shows up everywhere.
        from utils.gc_db import get_pic

        welcome_pic = await get_pic("welcome") or await get_pic("start")
        if not welcome_pic:
            welcome_pic = (
                BG_WELCOME if os.path.exists(BG_WELCOME)
                else (BG_START if os.path.exists(BG_START) else None)
            )

        sent_photo = False
        if welcome_pic:
            try:
                await message.reply_photo(
                    welcome_pic,
                    caption=caption,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=buttons,
                )
                sent_photo = True
            except Exception:
                sent_photo = False
        if not sent_photo:
            # No custom pic set yet — send an attractive text card with a
            # blockquote header so it still looks polished in the chat.
            await send_quote(
                message,
                "<blockquote>"
                "🎶 <b>𝑴𝒆𝒍𝒐𝒅𝒚 𝑴𝒖𝒔𝒊𝒄 𝑩𝒐𝒕</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━"
                "</blockquote>\n\n"
                + caption,
                client=client,
                reply_markup=buttons,
            )
        break


# ─── Cute Owner callback ────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^cute_owner$"))
@error_handler
async def cb_cute_owner(client: Client, cb: CallbackQuery):
    owner = await get_chat_owner(cb.message.chat.id)
    # REQUESTED: hidden / anonymous owner ka naam kabhi show nahi karna.
    if owner and owner.get("owner_name") and not owner.get("anonymous"):
        text = f"👑 {owner['owner_name']} added Melody to this group. Say thanks! 🎶"
    else:
        text = "👑 This group's owner is hidden. 🎶"
    await cb.answer(text, show_alert=True)


# ─── About callback ───────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^about_cb$"))
@error_handler
async def cb_about(client: Client, cb: CallbackQuery):
    await send_quote(
        cb.message,
        f"{headline('About Melody')}\n\n"
        "<b>Melody</b> is a premium Telegram music bot that streams\n"
        "high-quality audio from YouTube.\n\n"
        "<blockquote>"
        "🔥 <b>Features:</b>\n"
        "  🔸 HD YouTube streaming\n"
        "  🔸 Smart queue management\n"
        "  🔸 AutoPlay with related songs\n"
        "  🔸 Genius lyrics integration\n"
        "  🔸 Beautiful now-playing cards\n"
        "  🔸 Group admin controls"
        "</blockquote>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Made with 💛 for music lovers\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        client=client,
        edit=True,
        reply_markup=InlineKeyboardMarkup([
            [ikb(btn("◀ Back", RED), callback_data="start_back")],
        ]),
    )
    await cb.answer()


# ─── Back to Home ─────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^owner_back_home$"))
@error_handler
async def cb_owner_back_home(client: Client, cb: CallbackQuery):
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    await send_quote(cb.message, WELCOME_DM, client=client, edit=True, reply_markup=owner_buttons())
    await cb.answer()


@bot.on_callback_query(filters.regex(r"^start_back$"))
@error_handler
async def cb_start_back(client: Client, cb: CallbackQuery):
    is_owner = cb.from_user.id == Config.OWNER_ID
    await send_quote(
        cb.message,
        WELCOME_DM,
        client=client,
        edit=True,
        reply_markup=owner_buttons() if is_owner else user_buttons(),
    )
    await cb.answer()


# ─── Trust panel + feature tour ───────────────────────────────────────────────
# REQUESTED: "bot ko add krne pe + dm me start krne pe itna best UI ki users ko
# bot ka actual use pata chale, gc me full power denge to bot properly kaam
# karega, trust issue kam ho." These two panels do exactly that: one explains
# every right in plain language with a promise list, the other is a guided tour
# of the whole command suite.

def _panel_back_kb(in_group: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            ikb(btn("⚡ Feature Tour", BLUE), callback_data="melody_tour"),
            ikb(btn("🛡 Admin Rights", GREEN), callback_data="melody_trust"),
        ],
    ]
    if in_group:
        rows.append([ikb(btn("✖ Close", RED), callback_data="gc_close")])
    else:
        rows.append([ikb(btn("◀ Back", RED), callback_data="start_back")])
    return InlineKeyboardMarkup(rows)


TOUR_TEXT = (
    f"{headline('Mᴇʟᴏᴅʏ Fᴇᴀᴛᴜʀᴇ Tᴏᴜʀ')}\n"
    "🎵 <b>Mᴜsɪᴄ</b>\n"
    "┌ <code>/play &lt;song&gt;</code> — VC me HD audio\n"
    "├ <code>/vplay</code> — video stream · <code>/queue</code> · <code>/skip</code>\n"
    "└ Loop · AutoPlay · <code>/lyrics</code> · live VC feed\n\n"
    "👋 <b>Gʀᴇᴇᴛɪɴɢs</b>\n"
    "┌ <code>/welcome on</code> · <code>/goodbye on</code>\n"
    "├ <code>/setwelcome</code> — custom text + buttons\n"
    "└ <code>/setwelcomeimage</code> · <code>/resetwelcome</code>\n\n"
    "🚪 <b>Jᴏɪɴ Rᴇqᴜᴇsᴛs</b>\n"
    "┌ Har request pe accept / reject buttons\n"
    "└ <code>/rapproveall</code> · <code>/rdeclineall</code> · <code>/rpending</code>\n\n"
    "🛠 <b>Aᴅᴍɪɴ Sᴜɪᴛᴇ</b>\n"
    "┌ <code>/promote</code> · <code>/fpromote</code> · <code>/demote</code>\n"
    "├ <code>/ban</code> · <code>/mute</code> · <code>/warn</code> · <code>/kick</code>\n"
    "├ <code>/purge</code> · <code>/del</code> · <code>/cleanall</code> · <code>/zombies</code>\n"
    "└ <code>/tagall</code> · <code>/pin</code> · <code>/lock</code> · <code>/adminlist</code>\n\n"
    "🛡 <b>Sᴀғᴇᴛʏ</b>\n"
    "┌ <code>/protection</code> — links · NSFW · abuse · flood\n"
    "└ <code>/assistant</code> — Owner Assistant security engine\n\n"
    "<i>Sab kuch</i> <code>/settings</code> <i>se one-tap on/off.</i> 🎚"
)


@bot.on_callback_query(filters.regex(r"^melody_trust$"))
@error_handler
async def cb_melody_trust(client: Client, cb: CallbackQuery):
    in_group = cb.message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
    try:
        await cb.message.edit_text(
            TRUST_TEXT,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_panel_back_kb(in_group),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception:
        await cb.message.reply(
            TRUST_TEXT,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_panel_back_kb(in_group),
        )
    await cb.answer("🛡 Trust panel")


@bot.on_callback_query(filters.regex(r"^melody_tour$"))
@error_handler
async def cb_melody_tour(client: Client, cb: CallbackQuery):
    in_group = cb.message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
    try:
        await cb.message.edit_text(
            TOUR_TEXT,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_panel_back_kb(in_group),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception:
        await cb.message.reply(
            TOUR_TEXT,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_panel_back_kb(in_group),
        )
    await cb.answer("⚡ Feature tour")
