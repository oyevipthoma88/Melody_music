"""
📚 Owner Command Vault — every admin / sudo / owner command lives here.

WHY: the public /help menu used to advertise group-management, mass-action,
protection, auth, sudo and picture commands to everybody. Now /help only shows
what a normal user needs, and all of this is reachable ONLY from the owner
panel (/panel → 📚 Commands) or /ocmds, both gated on Config.OWNER_ID.
"""
from utils.buttons import ikb  # premium-emoji + styled buttons (safe on every fork)
from pyrogram import Client, enums, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)

from melody import bot
from melody.config import Config
from strings.themes import BLUE, RED, btn, fancy
from utils.decorators import error_handler, owner_only
from utils.melody_theme import headline

OWNER_PAGES = {
    "safemode": (
        "<blockquote>🛡 <b>Safe Mode</b> — one-tap full lockdown (38 layers)</blockquote>\n\n"
        "<code>/safemode on</code> — 🛡 Sab guards ON (group 100% safe)\n"
        "<code>/safemode off</code> — 📴 Turn it off\n"
        "<code>/safemode</code> — 📊 Status panel (kaunsi layer ON/OFF)\n"
        "<code>/safemode &lt;guard&gt; on/off</code> — ⚙️ Ek layer tune karo\n"
        "<code>/safemode approve</code> (reply) — ✅ User ko whitelist karo\n"
        "<code>/safemode unapprove</code> (reply) — ↩️ Whitelist se hatao\n"
        "<code>/safemode stats</code> — 📈 Is chat ke real action counters\n"
        "<code>/safemode reset</code> — ♻️ Default layers wapas\n\n"
        "<b>Gate :</b> captcha, joinrequest, raid, lockdown, newbie, botadd, "
        "deleted, impersonate, namelink, gban, cas\n"
        "<b>Content :</b> scam, phish, hidelink, invite, linksonly, files, "
        "inlinebot, keyboard, forwardspam, channel, mentions, emoji, caps, "
        "zalgo, duplicate, longmsg, doxx, pollspam, flood, cmdspam, stickers, "
        "service\n"
        "<b>Discipline :</b> escalate (delete → 15m mute → 1h mute → ban)\n\n"
        "<i>ON karte hi: math-captcha gate + join-request verification, anti-raid "
        "temp-ban + auto read-only lockdown, new-account link/media lock, CAS + "
        "gban reputation check, unicode bypass folding (zero-width, homoglyph, "
        "spaced text), hidden-link + phishing + scam bank, APK/exe block, emoji/"
        "caps/zalgo/duplicate/flood control, doxx & payment-info guard, anti-bot-"
        "add, anti-impersonation, service cleanup — sab ek saath.</i>"
    ),

    "gcmanage": (
        f"{headline('Group Management')}\n\n"
        "<code>/ban</code> <code>/unban</code> — 🔨 Ban / unban from group\n"
        "<code>/kick</code> <code>/dkick</code> — 👢 Remove a member\n"
        "<code>/mute</code> <code>/unmute</code> — 🔇 Restrict a member\n"
        "<code>/tmute [10m|2h|1d]</code> — ⏳ Timed mute\n"
        "<code>/promote</code> — ⭐ Promote to admin\n"
        "<code>/fpromote</code> — 👑 Full promote (all rights)\n"
        "<code>/demote</code> — ⬇️ Remove admin\n"
        "<code>/title [text]</code> — 🏷 Set admin title\n"
        "<code>/pin</code> <code>/unpin</code> — 📌 Pin control\n"
        "<code>/purge</code> — 🧹 Delete till replied msg (kitna bhi purana)\n"
        "<code>/purgefrom</code> + <code>/purgeto</code> — 🧹 Range purge\n"
        "<code>/del</code> — ❌ Delete replied message\n"
        "<code>/warn</code> <code>/unwarn</code> <code>/warns</code> — ⚠️ Warnings\n"
        "<code>/admins</code> <code>/staff</code> — 👮 Admin list\n"
        "<code>/tagall</code> <code>/utag</code> — 📣 Tag members\n"
        "<code>/lock</code> <code>/unlock</code> — 🔒 Lock media/links etc.\n"
        "<code>/info</code> — 🪪 User info card"
    ),
    "mass": (
        "<blockquote>🔴 <b>Mass Actions</b> <i>(GC Owner + Sudo only)</i></blockquote>\n\n"
        "<code>/muteall</code> — 🔇 Har user ka message delete hoga\n"
        "<code>/unmuteall</code> — 🔈 Wapas normal\n"
        "<code>/banall</code> — 🔨 Sab non-admins ban\n"
        "<code>/unbanall</code> — ♻️ Sabko unban\n"
        "<code>/kickall</code> — 👢 Sab non-admins remove\n\n"
        "⚠️ <i>Sirf group OWNER ya sudo user hi chala sakta hai — koi promoted "
        "admin ye commands use nahi kar sakta.</i>"
    ),
    "protect": (
        f"{headline('Group Protection')}\n\n"
        "<code>/protection on/off</code> — 🛡 Master switch\n"
        "<code>/antilink on/off</code> — 🔗 Link filter\n"
        "<code>/antiinvite on/off</code> — 📨 Invite-link filter\n"
        "<code>/antiabuse on/off</code> — 🤬 Gaali filter\n"
        "<code>/antispam on/off</code> — 🚦 Flood filter\n"
        "<code>/antiedit on/off</code> — ✏️ Edited-message check\n"
        "<code>/protectinfo</code> — 📊 Current settings\n\n"
        "<b>Mass-abuse guard :</b> ab ye <b>Owner Assistant</b> me shift ho gaya "
        "hai — limits custom hain, <code>/assistant</code> dekho."
    ),
    "greet": (
        f"{headline('Welcome &amp; Goodbye')}\n\n"
        "<code>/welcome</code> — 🎛 Settings panel\n"
        "<code>/welcome on/off</code> · <code>/goodbye on/off</code>\n"
        "<code>/setwelcome &lt;text&gt;</code> — ✍️ Custom welcome\n"
        "<code>/setgoodbye &lt;text&gt;</code> — ✍️ Custom goodbye\n"
        "<code>/resetwelcome</code> <code>/resetgoodbye</code> — ♻️ Default\n"
        "<code>/resetgreetings</code> — ♻️ Sab reset\n"
        "<code>/welcomecard on/off</code> — 🖼 Themed thumbnail\n"
        "<code>/setwelcomeimage</code> — 🖼 Apni image (reply)\n"
        "<code>/cleanwelcome on/off</code> — 🧹 Purana card delete\n"
        "<code>/cleanservice on/off</code> — 🧹 'X joined' service msg\n"
        "<code>/welcometest</code> <code>/goodbyetest</code> — 👀 Preview\n\n"
        "<i>Placeholders :</i> <code>{mention} {first} {id} {title} {count}</code>"
    ),
    "requests": (
        f"{headline('Join Requests')}\n\n"
        "<code>/rpending</code> — 📊 Kitni requests pending hain\n"
        "<code>/rapproveall</code> — ✅ Saari requests approve\n"
        "<code>/rdeclineall</code> — ❌ Saari requests decline\n\n"
        "<i>Har nayi request pe group me accept/reject buttons aate hain, aur "
        "koi admin Telegram menu se reject kare to bhi group notify hota hai.</i>"
    ),
    "assist": (
        "<blockquote>👑 <b>Owner Assistant</b> <i>(GC Owner + Sudo)</i></blockquote>\n\n"
        "<code>/assistant</code> — 🛡 Status panel + toggles\n"
        "<code>/assistant on/off</code> — Master switch\n"
        "<code>/oaset banlimit 3 5</code> — 🚨 3 ban / 5 sec\n"
        "<code>/oaset kicklimit 5 10</code> — 🚪 Mass-kick guard\n"
        "<code>/oaset mutelimit 5 10</code> — 🔇 Mass-mute guard\n"
        "<code>/oaset action demote|mute|both</code> — ⚡ Punishment\n"
        "<code>/oaset promoteguard on/off</code> — ⬆️ Takeover guard\n"
        "<code>/oaset ownershield on/off</code> — 🛡 Owner untouchable\n"
        "<code>/oaset settingsguard on/off</code> — ⚙️ Title/photo alert\n"
        "<code>/oaset audit on/off</code> — 📜 Owner DM audit trail\n"
        "<code>/oareset</code> — ♻️ Defaults\n\n"
        "<i>Limit cross hone pe admin demote + mute hota hai, bot-promotion "
        "revoke hota hai aur owner ko reason ke saath tag kiya jaata hai.</i>"
    ),

    "auth": (
        f"{headline('Auth &amp; Bot Access')}\n\n"
        "<code>/auth [user]</code> — ✅ Authorize a user\n"
        "<code>/unauth [user]</code> — ❌ Remove authorization\n"
        "<code>/authlist</code> — 📋 Authorized users\n"
        "<code>/botban [user] [reason]</code> — 🚫 Block from bot\n"
        "<code>/botunban [user]</code> — ✅ Unblock\n\n"
        "<i>Auth ke baad non-admin bhi music control kar sakta hai.</i>"
    ),
    "sudo": (
        f"{headline('Sudo &amp; Owner')}\n\n"
        "<code>/addsudo [user]</code> — ➕ Add sudo user\n"
        "<code>/rmsudo [user]</code> — ➖ Remove sudo\n"
        "<code>/sudolist</code> — 📋 All sudo users\n"
        "<code>/gban</code> <code>/ungban</code> — 🌍 Global ban\n"
        "<code>/broadcast</code> — 📢 Broadcast a message\n"
        "<code>/promoglobal on/off</code> — 📣 Global promo toggle"
    ),
    "pics": (
        f"{headline('Pictures &amp; Look')}\n\n"
        "<code>/setstartpic</code> — 🚀 Start card image\n"
        "<code>/setpingpic</code> — 🏓 Ping card image\n"
        "<code>/setalivepic</code> — 💚 Alive card image\n"
        "<code>/setplaypic</code> — 🎵 Play card fallback\n"
        "<code>/piclist</code> — 📋 What's set\n"
        "<code>/delpics [key]</code> — 🗑 Remove one\n\n"
        "<i>Photo ke caption me command bhejo ya photo pe reply karo. "
        "Sab kuch database me save hota hai — reboot ke baad bhi rahega.</i>"
    ),
}

_INDEX = [
    [("🛡 Safe Mode", "safemode"), ("🛡 Protect", "protect")],
    [("🔴 GC Manage", "gcmanage"), ("🔴 Mass", "mass")],
    [("👋 Greetings", "greet"), ("🚪 Requests", "requests")],
    [("👑 Assistant", "assist")],
    [("🟠 Auth", "auth"), ("👑 Sudo", "sudo")],
    [("🖼 Pics", "pics")],
]

INDEX_TEXT = (
    f"<blockquote>📚 <b>{fancy('COMMAND VAULT')}</b></blockquote>\n\n"
    "<i>Owner-only command reference. Choose a section:</i>"
)


def index_kb(with_back: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [ikb(btn(label, BLUE), callback_data=f"ownercmd_{key}") for label, key in row]
        for row in _INDEX
    ]
    if with_back:
        rows.append([ikb(btn("✵ BACK ✵", RED), callback_data="owner_panel")])
    return InlineKeyboardMarkup(rows)


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        ikb(btn("✵ BACK ✵", RED), callback_data="ownercmd_index"),
    ]])


@bot.on_message(filters.command(["ocmds", "ownercmds"]) & filters.private)
@owner_only
@error_handler
async def owner_cmds_cmd(client: Client, message: Message):
    await message.reply(
        INDEX_TEXT, parse_mode=enums.ParseMode.HTML, reply_markup=index_kb(with_back=False)
    )


@bot.on_callback_query(filters.regex(r"^ownercmd_(.+)$"))
@error_handler
async def owner_cmds_cb(client: Client, cb: CallbackQuery):
    if not cb.from_user or cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    key = cb.data.split("ownercmd_", 1)[1]
    if key == "index":
        text, markup = INDEX_TEXT, index_kb()
    else:
        text = OWNER_PAGES.get(key)
        if not text:
            return await cb.answer("Ye page available nahi hai.", show_alert=True)
        markup = _back_kb()

    editor = (
        cb.message.edit_caption
        if (cb.message.photo or cb.message.video or cb.message.animation)
        else cb.message.edit_text
    )
    try:
        await editor(text, parse_mode=enums.ParseMode.HTML, reply_markup=markup)
    except Exception:
        await client.send_message(
            cb.message.chat.id, text, parse_mode=enums.ParseMode.HTML, reply_markup=markup
        )
    await cb.answer()
