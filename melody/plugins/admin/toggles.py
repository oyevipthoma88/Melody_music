"""
🎚️ /settings — one tap ON/OFF switch for EVERY filter Melody has.

REQUESTED
---------
"aur all options ko on/off ka system add kr"

Every protection guard and every safe-mode layer already had a text command
(`/safemode captcha off`), which nobody remembers.
This is the same thing as a tappable panel: each button shows the live state
and flips it instantly.

    /settings          → main panel
    /settings prot     → protection guards page
    /settings safe     → safe-mode layers page (paged)

Group admins only. Every callback re-checks admin rights, so a normal member
tapping someone else's panel changes nothing.
"""
import logging

from pyrogram import Client, enums, filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from utils.buttons import ikb  # premium-emoji + styled buttons (safe on every fork)

from melody import bot
from melody.plugins.admin.safemode import GUARDS, _cfg, _set_guard, is_safemode
from utils.admin_tools import card, is_admin
from utils.decorators import error_handler
from utils.gc_db import get_protection, get_setting_flag, set_protection, set_setting_flag

log = logging.getLogger(__name__)

SAFEMODE_FLAG = "safemode"

# Human labels for the protection guards.
PROT_LABELS = {
    "enabled": "🛡 Pʀᴏᴛᴇᴄᴛɪᴏɴ (ᴍᴀsᴛᴇʀ)",
    "links": "🔗 Aʟʟ ʟɪɴᴋs",
    "invites": "📨 Iɴᴠɪᴛᴇ ʟɪɴᴋs",
    "abuse": "🤬 Aʙᴜsᴇ",
    "forward": "↪️ Fᴏʀᴡᴀʀᴅs",
    "edit": "✏️ Eᴅɪᴛᴇᴅ ᴍsɢs",
}

# Human labels for the safe-mode layers, in panel order.
SAFE_LABELS = {
    "captcha": "🧮 Cᴀᴘᴛᴄʜᴀ ɢᴀᴛᴇ",
    "joinrequest": "📝 Jᴏɪɴ-ʀᴇǫᴜᴇsᴛ ᴠᴇʀɪғʏ",
    "raid": "🚨 Aɴᴛɪ-ʀᴀɪᴅ",
    "lockdown": "🔒 Aᴜᴛᴏ ʟᴏᴄᴋᴅᴏᴡɴ",
    "newbie": "🐣 Nᴇᴡʙɪᴇ ʟɪᴍɪᴛs",
    "botadd": "🤖 Aɴᴛɪ ʙᴏᴛ-ᴀᴅᴅ",
    "deleted": "👻 Dᴇʟᴇᴛᴇᴅ ᴀᴄᴄᴏᴜɴᴛs",
    "impersonate": "🎭 Iᴍᴘᴇʀsᴏɴᴀᴛɪᴏɴ",
    "namelink": "🏷 Nᴀᴍᴇ ᴀᴅs",
    "gban": "🌍 Gʟᴏʙᴀʟ ʙᴀɴs",
    "cas": "📡 CAS ʀᴇᴘᴜᴛᴀᴛɪᴏɴ",
    "scam": "💸 Sᴄᴀᴍ / ᴄʀʏᴘᴛᴏ",
    "phish": "🎣 Pʜɪsʜɪɴɢ ʟɪɴᴋs",
    "hidelink": "🕳 Hɪᴅᴅᴇɴ ʟɪɴᴋs",
    "invite": "📨 Iɴᴠɪᴛᴇ ʟɪɴᴋs",
    "linksonly": "🔗 Lɪɴᴋ-ᴏɴʟʏ sᴘᴀᴍ",
    "files": "📦 APK / EXE ғɪʟᴇs",
    "inlinebot": "🤖 Iɴʟɪɴᴇ-ʙᴏᴛ sᴘᴀᴍ",
    "keyboard": "⌨️ Bᴜᴛᴛᴏɴ sᴘᴀᴍ",
    "forwardspam": "↪️ Fᴏʀᴡᴀʀᴅ sᴘᴀᴍ",
    "channel": "📢 Cʜᴀɴɴᴇʟ ɪᴅᴇɴᴛɪᴛʏ",
    "mentions": "📣 Mᴀss ᴍᴇɴᴛɪᴏɴs",
    "emoji": "😀 Eᴍᴏᴊɪ ғʟᴏᴏᴅ",
    "caps": "🔠 CAPS ғʟᴏᴏᴅ",
    "zalgo": "🌀 Zᴀʟɢᴏ ᴛᴇxᴛ",
    "duplicate": "♻️ Dᴜᴘʟɪᴄᴀᴛᴇ sᴘᴀᴍ",
    "longmsg": "📜 Wᴀʟʟ ᴏғ ᴛᴇxᴛ",
    "doxx": "🪪 Dᴏxx / ᴘᴀʏᴍᴇɴᴛ ɪɴғᴏ",
    "pollspam": "📊 Pᴏʟʟ sᴘᴀᴍ",
    "flood": "🌊 Fʟᴏᴏᴅ ᴄᴏɴᴛʀᴏʟ",
    "cmdspam": "⌘ Cᴏᴍᴍᴀɴᴅ sᴘᴀᴍ",
    "stickers": "🔞 Pᴏʀɴ sᴛɪᴄᴋᴇʀ ᴘᴀᴄᴋs",
    "service": "🧹 Sᴇʀᴠɪᴄᴇ ᴍsɢs",
    "escalate": "⚖️ Pʀᴏɢʀᴇssɪᴠᴇ ᴘᴜɴɪsʜᴍᴇɴᴛ",
}

SAFE_KEYS = [k for k in SAFE_LABELS if k in GUARDS]
PAGE_SIZE = 10


def _dot(value: bool) -> str:
    return "🟢" if value else "🔴"


def _rows(items, prefix: str) -> list:
    rows = []
    for key, label, value in items:
        rows.append([ikb(f"{_dot(value)} {label}", callback_data=f"{prefix}{key}")])
    return rows


async def _main_panel(chat_id: int):
    prot = await get_protection(chat_id)
    safe_on = await is_safemode(chat_id)
    vc_activity = bool(await get_setting_flag(chat_id, "vc_activity", True))
    join_notify = bool(await get_setting_flag(chat_id, "join_notify", True))
    # Other-bot command trigger (default ON). Stored in the same place the
    # /trigger command and utils/command_patch read from, so panel + command +
    # filter can never disagree.
    from utils.database import get_setting as _get_setting
    _trig = await _get_setting(chat_id, "trigger_response", None)
    trigger_on = True if _trig is None else bool(_trig)
    text = card(
        "Mᴇʟᴏᴅʏ Sᴇᴛᴛɪɴɢs",
        "🎚 <b>Eᴠᴇʀʏ ғɪʟᴛᴇʀ, ᴏɴᴇ ᴛᴀᴘ.</b>\n\n"
        f"┌ 🛡 <b>Pʀᴏᴛᴇᴄᴛɪᴏɴ :</b> {_dot(prot.get('enabled'))}\n"
        f"├ 🔐 <b>Sᴀғᴇ Mᴏᴅᴇ :</b> {_dot(safe_on)}\n"
        f"├ 🎙 <b>VC Aᴄᴛɪᴠɪᴛʏ Lᴏɢ :</b> {_dot(vc_activity)}\n"
        f"├ 🚪 <b>Jᴏɪɴ Rᴇϙᴜᴇsᴛ Aʟᴇʀᴛ :</b> {_dot(join_notify)}\n"
        f"├ 🤖 <b>Oᴛʜᴇʀ-Bᴏᴛ Cᴍᴅ Rᴇᴘʟʏ :</b> {_dot(trigger_on)}\n"
        f"└ ⚙️ <b>Aᴄᴛɪᴏɴ :</b> <code>{prot.get('action', 'delete')}</code>\n\n"
        "<i>Tᴀᴘ ᴀ sᴇᴄᴛɪᴏɴ ᴛᴏ ᴛᴜʀɴ ɪɴᴅɪᴠɪᴅᴜᴀʟ ᴏᴘᴛɪᴏɴs ᴏɴ / ᴏғғ.</i>\n"
        "<i>Aᴘᴘʀᴏᴠᴇᴅ ᴜsᴇʀs (<code>/approve</code>) ᴀʀᴇ ɴᴇᴠᴇʀ ғɪʟᴛᴇʀᴇᴅ.</i>",
        "Melody · settings",
    )
    kb = InlineKeyboardMarkup([
        [
            ikb(f"{_dot(prot.get('enabled'))} Pʀᴏᴛᴇᴄᴛɪᴏɴ", callback_data="set_m_enabled"),
            ikb(f"{_dot(safe_on)} Sᴀғᴇ Mᴏᴅᴇ", callback_data="set_t_safemaster"),
        ],
        [
            ikb("🛡 Pʀᴏᴛᴇᴄᴛɪᴏɴ ᴏᴘᴛɪᴏɴs", callback_data="set_prot"),
        ],
        [
            ikb("🔐 Sᴀғᴇ ᴍᴏᴅᴇ ᴏᴘᴛɪᴏɴs", callback_data="set_safe_0"),
        ],
        [ikb(f"{_dot(vc_activity)} VC Aᴄᴛɪᴠɪᴛʏ Lᴏɢ", callback_data="set_t_vcactivity")],
        [ikb(f"{_dot(join_notify)} Jᴏɪɴ Rᴇϙᴜᴇsᴛ Aʟᴇʀᴛ", callback_data="set_t_joinnotify")],
        [ikb(f"{_dot(trigger_on)} Oᴛʜᴇʀ-Bᴏᴛ Cᴍᴅ Rᴇᴘʟʏ", callback_data="set_t_trigger")],
        [
            ikb("✅ Aᴘᴘʀᴏᴠᴇᴅ ᴜsᴇʀs", callback_data="set_approved"),
            ikb("✖️ Cʟᴏsᴇ", callback_data="close"),
        ],
    ])
    return text, kb


async def _prot_panel(chat_id: int):
    prot = await get_protection(chat_id)
    items = [(k, PROT_LABELS[k], bool(prot.get(k))) for k in PROT_LABELS]
    rows = _rows(items, "set_t_")
    rows.append([ikb("◀ Bᴀᴄᴋ", callback_data="set_main")])
    text = card(
        "Pʀᴏᴛᴇᴄᴛɪᴏɴ Oᴘᴛɪᴏɴs",
        "🛡 <b>Tᴀᴘ ᴛᴏ ᴛᴏɢɢʟᴇ.</b>\n"
    )
    return text, InlineKeyboardMarkup(rows)


async def _safe_panel(chat_id: int, page: int):
    cfg = await _cfg(chat_id)
    keys = SAFE_KEYS[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    items = [(k, SAFE_LABELS[k], bool(cfg.get(k))) for k in keys]
    rows = _rows(items, "set_s_")

    pages = (len(SAFE_KEYS) + PAGE_SIZE - 1) // PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(ikb("⏪ Pʀᴇᴠ", callback_data=f"set_safe_{page - 1}"))
    nav.append(ikb(f"{page + 1}/{pages}", callback_data="set_noop"))
    if page + 1 < pages:
        nav.append(ikb("Nᴇxᴛ ⏩", callback_data=f"set_safe_{page + 1}"))
    rows.append(nav)
    rows.append([
        ikb("🟢 Aʟʟ ᴏɴ", callback_data="set_s_all_on"),
        ikb("🔴 Aʟʟ ᴏғғ", callback_data="set_s_all_off"),
    ])
    rows.append([ikb("◀ Bᴀᴄᴋ", callback_data="set_main")])

    text = card(
        "Sᴀғᴇ Mᴏᴅᴇ Lᴀʏᴇʀs",
        f"🔐 <b>{len(SAFE_KEYS)} ʟᴀʏᴇʀs</b> — ᴛᴀᴘ ᴀɴʏ ᴏɴᴇ ᴛᴏ ᴛᴜʀɴ ɪᴛ ᴏɴ / ᴏғғ.\n"
        f"⚡ <b>Sᴀғᴇ ᴍᴏᴅᴇ :</b> {_dot(await is_safemode(chat_id))}",
    )
    return text, InlineKeyboardMarkup(rows)


async def _approved_panel(chat_id: int):
    from utils.approvals import approved_list

    users, everyone = await approved_list(chat_id)
    if everyone:
        body = "✅ <b>Cʜᴀᴛ-ᴡɪᴅᴇ ᴀᴘᴘʀᴏᴠᴀʟ ɪs ON</b> — ɴᴏ ᴄᴏɴᴛᴇɴᴛ ɪs ғɪʟᴛᴇʀᴇᴅ."
    elif users:
        body = "✅ <b>Aᴘᴘʀᴏᴠᴇᴅ ᴜsᴇʀs :</b> " + ", ".join(f"<code>{u}</code>" for u in users[:40])
    else:
        body = "📭 <b>Nᴏ ᴀᴘᴘʀᴏᴠᴇᴅ ᴜsᴇʀs.</b>"
    body += (
        "\n\n🧾 <code>/approve</code> · <code>/unapprove</code>\n"
        "🧾 <code>/approveall</code> · <code>/unapproveall</code>"
    )
    kb = InlineKeyboardMarkup([[ikb("◀ Bᴀᴄᴋ", callback_data="set_main")]])
    return card("Aᴘᴘʀᴏᴠᴇᴅ Usᴇʀs", body), kb


@bot.on_message(filters.command(["settings", "toggles", "options"]) & filters.group)
@error_handler
async def settings_cmd(client: Client, message: Message):
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        await message.reply(
            card("Sᴇᴛᴛɪɴɢs", "🔒 <b>Oɴʟʏ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴs ᴄᴀɴ ᴏᴘᴇɴ ᴛʜɪs ᴘᴀɴᴇʟ.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    arg = (message.command[1].lower() if len(message.command) > 1 else "")
    if arg.startswith("prot"):
        text, kb = await _prot_panel(message.chat.id)
    elif arg.startswith("safe"):
        text, kb = await _safe_panel(message.chat.id, 0)
    else:
        text, kb = await _main_panel(message.chat.id)
    await message.reply(text, parse_mode=enums.ParseMode.HTML, reply_markup=kb)


@bot.on_callback_query(filters.regex(r"^set_"))
@error_handler
async def settings_cb(client: Client, cb: CallbackQuery):
    chat_id = cb.message.chat.id
    if not await is_admin(client, chat_id, cb.from_user.id):
        await cb.answer("🔒 Admins only.", show_alert=True)
        return

    data = cb.data
    if data == "set_noop":
        await cb.answer()
        return

    if data == "set_main":
        text, kb = await _main_panel(chat_id)
    elif data == "set_prot":
        text, kb = await _prot_panel(chat_id)
    elif data == "set_approved":
        text, kb = await _approved_panel(chat_id)
    elif data.startswith("set_safe_"):
        text, kb = await _safe_panel(chat_id, int(data.rsplit("_", 1)[1]))
    elif data == "set_m_enabled":
        prot = await get_protection(chat_id)
        new = not bool(prot.get("enabled"))
        await set_protection(chat_id, "enabled", new)
        await cb.answer(f"🛡 Protection {'ON' if new else 'OFF'}")
        text, kb = await _main_panel(chat_id)
    elif data == "set_t_safemaster":
        new = not await is_safemode(chat_id)
        await set_setting_flag(chat_id, SAFEMODE_FLAG, new)
        await cb.answer(f"🔐 Safe mode {'ON' if new else 'OFF'}")
        text, kb = await _main_panel(chat_id)
    elif data == "set_t_vcactivity":
        current = bool(await get_setting_flag(chat_id, "vc_activity", True))
        new = not current
        await set_setting_flag(chat_id, "vc_activity", new)
        await cb.answer(f"VC activity log {'ON' if new else 'OFF'}")
        text, kb = await _main_panel(chat_id)
    elif data == "set_t_joinnotify":
        current = bool(await get_setting_flag(chat_id, "join_notify", True))
        new = not current
        await set_setting_flag(chat_id, "join_notify", new)
        await cb.answer(f"Join request alert {'ON' if new else 'OFF'}")
        text, kb = await _main_panel(chat_id)
    elif data == "set_t_trigger":
        from utils.database import get_setting as _get_setting, set_setting as _set_setting
        cur = await _get_setting(chat_id, "trigger_response", None)
        cur = True if cur is None else bool(cur)
        new = not cur
        await _set_setting(chat_id, "trigger_response", new)
        await cb.answer(
            "🤖 Other bots ke commands par reply " + ("ON" if new else "OFF")
        )
        text, kb = await _main_panel(chat_id)
    elif data.startswith("set_t_"):
        key = data[len("set_t_"):]
        if key not in PROT_LABELS:
            await cb.answer()
            return
        prot = await get_protection(chat_id)
        new = not bool(prot.get(key))
        await set_protection(chat_id, key, new)
        await cb.answer(f"{PROT_LABELS[key]} → {'ON' if new else 'OFF'}")
        text, kb = await _prot_panel(chat_id)
    elif data in ("set_s_all_on", "set_s_all_off"):
        value = data.endswith("_on")
        stored = await get_setting_flag(chat_id, "safemode_cfg", None) or {}
        for key in SAFE_KEYS:
            stored[key] = value
        await set_setting_flag(chat_id, "safemode_cfg", stored)
        await cb.answer(f"All layers {'ON' if value else 'OFF'}")
        text, kb = await _safe_panel(chat_id, 0)
    elif data.startswith("set_s_"):
        key = data[len("set_s_"):]
        if key not in SAFE_LABELS:
            await cb.answer()
            return
        cfg = await _cfg(chat_id)
        new = not bool(cfg.get(key))
        await _set_guard(chat_id, key, new)
        await cb.answer(f"{SAFE_LABELS[key]} → {'ON' if new else 'OFF'}")
        page = max(0, SAFE_KEYS.index(key) // PAGE_SIZE)
        text, kb = await _safe_panel(chat_id, page)
    else:
        await cb.answer()
        return

    try:
        if cb.message.caption is not None:
            await cb.message.edit_caption(text, parse_mode=enums.ParseMode.HTML, reply_markup=kb)
        else:
            await cb.message.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=kb)
    except Exception as exc:
        log.debug("settings: edit failed: %s", exc)


# ─── /trigger — toggle responding to other bots' commands ───────────────────
#
# REQUEST: "bot jese kisi other bot ko trigger krne pe bhi aapna bot reply
# deta tha usko ab toggle kr (response on off kr) taki baki bot ke mesg
# ignore kr de."
#
# When ON (default): /play@OtherBot, /skip@OtherBot, etc. are all answered
# by Melody too — useful when you want Melody to take over from another bot.
# When OFF: only bare commands (/play) and /cmd@MelodyBot are answered;
# /cmd@OtherBot is silently ignored.

@bot.on_message(filters.command(["trigger", "tagger", "triggers"]) & filters.group)
@error_handler
async def trigger_toggle_cmd(client: Client, message: Message):
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        await message.reply(
            card("Tʀɪɢɢᴇʀ", "🔒 <b>Oɴʟʏ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴs ᴄᴀɴ ᴛᴏɢɢʟᴇ ᴛʜɪs.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    from utils.database import get_setting, set_setting
    arg = (message.command[1].lower() if len(message.command) > 1 else "")
    current = bool(await get_setting(message.chat.id, "trigger_response", True))

    if arg in ("on", "true", "1", "yes"):
        new = True
    elif arg in ("off", "false", "0", "no"):
        new = False
    else:
        new = not current

    await set_setting(message.chat.id, "trigger_response", new)
    status = "ON 🟢 — Melody will respond to commands aimed at other bots too" if new else "OFF 🔴 — Melody will ignore commands aimed at other bots"
    await message.reply(
        card("Tʀɪɢɢᴇʀ Rᴇsᴘᴏɴsᴇ", f"⚙️ <b>{status}</b>"),
        parse_mode=enums.ParseMode.HTML,
    )
