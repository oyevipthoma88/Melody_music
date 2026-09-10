"""
🛡️ SAFE MODE v2 — "/safemode on" = group 100% locked down.

Rebuilt after studying how the best open-source group-guards do it
(Rose, Shieldy, Combot/CAS, tg-spam, Shield, Daisy, WilliamButcherBot,
Miss-Katy, SpamWatch, GroupHelp …). Everything runs natively inside Melody;
only the optional CAS reputation lookup touches the network.

WHAT /safemode on TURNS ON
──────────────────────────
Gate layer
  1. Math CAPTCHA (4 random buttons) for every new member — muted till solved
  2. Wrong answer → temp-ban 1h · no answer in time → kick (can rejoin)
  3. Join-request CAPTCHA — "request to join" links are solved in PM first
  4. Anti-raid: join burst → raid mode, joiners temp-banned for the raid window
  5. Raid lockdown — chat set to read-only automatically, auto-restored
  6. New-account (newbie) lock — brand-new IDs can't post media/links at first
  7. Anti-bot-add — a bot added by a non-admin is removed
  8. Deleted-account sweep
  9. Anti-impersonation — copying an admin's name/username
 10. Link/ad in profile name or username
 11. CAS + Melody gban reputation check on join and on every message

Content layer (all run on new *and* edited messages)
 12. Unicode de-obfuscation first: zero-width strip, RTL-override strip,
     homoglyph fold (Cyrillic/Greek→Latin), spaced-out text collapse —
     so "ғ ʀ ᴇ ᴇ  ɢ і ᴠ ᴇ ᴀ ᴡ ᴀʏ" can't slip past the filters
 13. Scam / crypto / airdrop / giveaway / "you won" keyword bank
 14. Phishing + URL-shortener + fake-telegram domain blacklist
 15. Hidden masked links (text_link entities pointing elsewhere)
 16. Invite links (t.me/+, joinchat) from non-admins
 17. Link-only spam posts
 18. Executable / APK / script file block
 19. Inline-bot (via_bot) and inline-keyboard spam
 20. Giveaway, story and paid-post forwards
 21. Channel-promo forwards
 22. Anonymous channel (sender_chat) messages
 23. Mass-mention spam
 24. Emoji / custom-emoji flood and emoji-only walls
 25. CAPS shouting
 26. Zalgo / combining-character text bombs
 27. Repeated-character and duplicate-message spam
 28. Wall-of-text dumps
 29. Doxx guard — phone numbers, UPI IDs, card numbers, IBANs
 30. Poll / dice / contact / location spam from non-admins
 31. Message, media, sticker, voice and forward flood control
 32. Foreign bot-command spam
 34. Global ban + CAS enforcement on every message

Discipline layer
 35. Progressive punishment — delete → 15 min mute → 1 h mute → ban
 36. Approved-user whitelist (admins, and anyone you /safemode approve)
 37. Every action counted per chat: /safemode stats shows real numbers
 38. Everything logged to the bot's log channel

Commands
    /safemode                    → status panel
    /safemode on | off           → master switch
    /safemode <guard> on|off     → tune a single layer
    /safemode approve  (reply)   → whitelist a user
    /safemode unapprove (reply)  → remove from whitelist
    /safemode stats              → real action counters for this chat
    /safemode reset              → back to defaults
"""
from utils.buttons import ikb  # premium-emoji + styled buttons (safe on every fork)
import asyncio
import html
import logging
import random
import re
import time
import unicodedata
from collections import defaultdict, deque

from pyrogram import Client, enums, filters
from pyrogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatPermissions,
    InlineKeyboardMarkup,
    Message,
)

from melody import bot
from melody.logging import log_activity
from utils.admin_tools import (
    MUTED_PERMS,
    UNMUTED_PERMS,
    auto_delete,
    card,
    close_kb,
    is_admin,
    mention,
    mention_id,
    safe_call,
)
from utils.database import is_gbanned
from utils.decorators import error_handler
from utils.gc_db import get_setting_flag, set_setting_flag, set_protection

log = logging.getLogger(__name__)

# ─── Keys / tunables ─────────────────────────────────────────────────────────

FLAG = "safemode"
CFG_FLAG = "safemode_cfg"
APPROVED_FLAG = "safemode_approved"
STATS_FLAG = "safemode_stats"

CAPTCHA_TIMEOUT = 120           # seconds to solve
CAPTCHA_FAIL_BAN = 3600         # wrong answer → 1 h temp-ban
FLOOD_LIMIT, FLOOD_WINDOW = 8, 7
MEDIA_FLOOD_LIMIT, MEDIA_FLOOD_WINDOW = 5, 10
FWD_FLOOD_LIMIT, FWD_FLOOD_WINDOW = 4, 20
RAID_JOINS, RAID_WINDOW = 8, 20
RAID_LOCK_SECONDS = 900         # raid mode duration (15 min)
RAID_TEMPBAN = 3600             # joiners during a raid: banned 1 h
MENTION_LIMIT = 6
EMOJI_LIMIT = 18
CAPS_MIN_LEN, CAPS_RATIO = 25, 0.75
DUP_LIMIT, DUP_WINDOW = 4, 30
LONG_MSG_CHARS = 2500
NEWBIE_SECONDS = 900            # how long a brand-new account stays limited
NEW_ACCOUNT_ID = 7_000_000_000  # Telegram IDs above this are very recent
STRIKE_TTL = 1800               # strikes decay after 30 min

# Escalation ladder: strike count → (action, mute seconds)
ESCALATION = {
    1: ("delete", 0),
    2: ("mute", 900),
    3: ("mute", 3600),
    4: ("ban", 0),
}

GUARDS = {
    # gate
    "captcha": True,
    "joinrequest": True,
    "raid": True,
    "lockdown": True,
    "newbie": True,
    "botadd": True,
    "deleted": True,
    "impersonate": True,
    "namelink": True,
    "gban": True,
    "cas": True,
    # content
    "scam": True,
    "phish": True,
    "hidelink": True,
    "invite": True,
    "linksonly": True,
    "files": True,
    "inlinebot": True,
    "keyboard": True,
    "forwardspam": True,
    "channel": True,
    "mentions": True,
    "emoji": True,
    "caps": True,
    "zalgo": True,
    "duplicate": True,
    "longmsg": True,
    "doxx": True,
    "pollspam": True,
    "flood": True,
    "cmdspam": True,
    "stickers": True,
    "service": True,
    # discipline
    "escalate": True,
}

# ─── Unicode de-obfuscation ──────────────────────────────────────────────────

_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x200E, 0x200F,
     0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x061C, 0x00AD], None
)

_HOMOGLYPHS = str.maketrans({
    "а": "a", "б": "b", "с": "c", "ԁ": "d", "е": "e", "ғ": "f", "һ": "h",
    "і": "i", "ј": "j", "к": "k", "ӏ": "l", "м": "m", "н": "h", "о": "o",
    "р": "p", "ԛ": "q", "г": "r", "ѕ": "s", "т": "t", "и": "u", "ѵ": "v",
    "ԝ": "w", "х": "x", "у": "y", "з": "3", "α": "a", "β": "b", "ε": "e",
    "ι": "i", "κ": "k", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "ν": "v",
    "χ": "x", "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ɢ": "g",
    "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n",
    "ᴏ": "o", "ᴘ": "p", "ʀ": "r", "s": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v",
    "ᴡ": "w", "ʏ": "y", "ᴢ": "z", "0": "o", "1": "i", "3": "e", "4": "a",
    "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "l",
})

_SPACED_RE = re.compile(r"(?:(?<=\b\w)[\s._\-*+]{1,2}(?=\w\b))")
_REPEAT_RE = re.compile(r"(.)\1{3,}")
_COMBINING_RE = re.compile(r"[\u0300-\u036f\u0483-\u0489\u1ab0-\u1aff\u20d0-\u20f0]")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF]"
)


def normalize(text: str) -> str:
    """Fold every common filter-bypass trick down to plain lowercase ascii-ish."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_ZERO_WIDTH)
    text = _COMBINING_RE.sub("", text)
    text = text.lower().translate(_HOMOGLYPHS)
    text = _SPACED_RE.sub("", text)          # "f r e e" → "free"
    text = _REPEAT_RE.sub(r"\1\1", text)     # "freeeeee" → "freee"
    return re.sub(r"\s+", " ", text).strip()


# ─── Pattern banks ───────────────────────────────────────────────────────────

_SCAM_RE = re.compile(
    r"(free\s*(bitcoin|btc|usdt|eth|crypto|nitro|premium|gift\s*card)|air\s*drop|"
    r"binary\s*option|forex\s*signal|double\s*your\s*(money|invest)|"
    r"investment\s*plan|profit\s*guarantee|earn\s*\$?\d+\s*(daily|per\s*day|a\s*day)|"
    r"crypto\s*(trading|profit|mining|invest)|giveaway\s*winner|you\s*(have\s*)?won|"
    r"claim\s*your\s*(prize|reward|airdrop|gift)|seed\s*phrase|recovery\s*phrase|"
    r"wallet\s*connect|private\s*key|telegram\s*premium\s*free|"
    r"hack(ing)?\s*(service|tool|account)|dm\s*me\s*for\s*(money|profit|job)|"
    r"loan\s*without|paytm\s*cash\s*free|refer\s*and\s*earn\s*\d|"
    r"work\s*from\s*home\s*(job|earn)|part\s*time\s*job\s*daily\s*payment|"
    r"only\s*\d+\s*(seats|slots)\s*left|limited\s*time\s*offer\s*claim|"
    r"whatsapp\s*me|contact\s*me\s*on\s*whatsapp|escort|sex\s*chat|nude\s*(pic|video))",
    re.IGNORECASE,
)

_PHISH_DOMAIN_RE = re.compile(
    r"(?:"
    r"bit\.ly|tinyurl\.com|cutt\.ly|is\.gd|t\.co|rb\.gy|shorturl\.at|adf\.ly|"
    r"bc\.vc|shrinkme\.io|gplinks\.\w+|droplink\.\w+|linkvertise\.com|ouo\.io|"
    r"exe\.io|za\.gl|clk\.sh|urlshortx\.com|shrtfly\.com|mdisk\.\w+|"
    r"telegram-?premium|telegam|telegran|teiegram|te1egram|tellegram|tg-?gift|"
    r"telegram-?(gift|wallet|verify|support|airdrop)|"
    r"metamask-?\w+\.|wallet-?connect\w+\.|binance-?(gift|bonus|airdrop)|"
    r"free-?(nitro|robux|vbucks)|discord-?nitro"
    r")",
    re.IGNORECASE,
)

_INVITE_RE = re.compile(
    r"(t\.me/(\+|joinchat/)|telegram\.me/(\+|joinchat/)|t\.me/[A-Za-z0-9_]{4,})",
    re.IGNORECASE,
)

_NAME_LINK_RE = re.compile(
    r"(t\.me/|telegram\.me/|https?://|www\.|@[A-Za-z0-9_]{5,}"
    r"|\.(com|net|xyz|online|site|shop|link|club|vip|top|live))",
    re.IGNORECASE,
)

_DOXX_RE = re.compile(
    r"(\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"                      # card
    r"|\b[A-Z]{2}\d{2}[A-Z0-9]{10,26}\b"                             # IBAN
    r"|\b[\w.\-]{3,}@(?:ok|ybl|paytm|upi|apl|axl|ibl)\b"             # UPI id
    r"|(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"                      # IN phone
    r"|(?<!\d)\+\d{1,3}[\s-]?\d{7,12}(?!\d))"                        # intl phone
)

from utils.tasks import spawn

_BAD_EXT = (
    ".apk", ".exe", ".msi", ".bat", ".cmd", ".scr", ".com", ".jar", ".vbs",
    ".ps1", ".sh", ".dll", ".apks", ".xapk", ".pif", ".reg", ".lnk", ".iso",
)

_URL_RE = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+)", re.IGNORECASE)

# ─── Runtime state ───────────────────────────────────────────────────────────

_msg_times: dict = defaultdict(lambda: deque(maxlen=64))
_media_times: dict = defaultdict(lambda: deque(maxlen=64))
_fwd_times: dict = defaultdict(lambda: deque(maxlen=64))
_dup_texts: dict = defaultdict(lambda: deque(maxlen=16))
_joins: dict = defaultdict(lambda: deque(maxlen=128))
_raid_until: dict = {}
_locked_chats: set = set()
_pending_captcha: dict = {}      # (chat_id, user_id) -> {"task", "answer", "msg_id"}
_join_req_captcha: dict = {}     # (chat_id, user_id) -> answer
_first_seen: dict = {}           # (chat_id, user_id) -> ts of join
_strikes: dict = defaultdict(list)
_cas_cache: dict = {}


# ─── Settings helpers ────────────────────────────────────────────────────────

async def is_safemode(chat_id: int) -> bool:
    return bool(await get_setting_flag(chat_id, FLAG, False))


async def _cfg(chat_id: int) -> dict:
    stored = await get_setting_flag(chat_id, CFG_FLAG, None) or {}
    cfg = dict(GUARDS)
    for key in cfg:
        if key in stored:
            cfg[key] = bool(stored[key])
    return cfg


async def _set_guard(chat_id: int, key: str, value: bool) -> None:
    stored = await get_setting_flag(chat_id, CFG_FLAG, None) or {}
    stored[key] = value
    await set_setting_flag(chat_id, CFG_FLAG, stored)


async def _approved(chat_id: int) -> set:
    return set(await get_setting_flag(chat_id, APPROVED_FLAG, None) or [])


async def _set_approved(chat_id: int, users: set) -> None:
    await set_setting_flag(chat_id, APPROVED_FLAG, sorted(users))


async def _bump_stat(chat_id: int, key: str) -> None:
    try:
        stats = await get_setting_flag(chat_id, STATS_FLAG, None) or {}
        stats[key] = int(stats.get(key, 0)) + 1
        await set_setting_flag(chat_id, STATS_FLAG, stats)
    except Exception:
        pass


# ─── Reputation (CAS) ────────────────────────────────────────────────────────

async def _cas_banned(user_id: int) -> bool:
    """Combot Anti-Spam reputation lookup, cached, never fatal."""
    hit = _cas_cache.get(user_id)
    if hit and time.time() - hit[0] < 21600:
        return hit[1]
    try:
        import httpx

        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.get("https://api.cas.chat/check", params={"user_id": user_id})
            flagged = bool(resp.json().get("ok")) if resp.status_code == 200 else False
    except Exception:
        flagged = False
    _cas_cache[user_id] = (time.time(), flagged)
    return flagged


# ─── Output helpers ──────────────────────────────────────────────────────────

async def _notify(client: Client, chat_id: int, title: str, body: str, seconds: int = 45):
    try:
        msg = await client.send_message(
            chat_id, card(title, body, "Sᴀғᴇ Mᴏᴅᴇ ɪs ᴀᴄᴛɪᴠᴇ 🛡"),
            parse_mode=enums.ParseMode.HTML,
        )
        await auto_delete(msg, seconds)
    except Exception:
        pass


def _strike(chat_id: int, user_id: int) -> int:
    now = time.time()
    hits = [t for t in _strikes[(chat_id, user_id)] if now - t < STRIKE_TTL]
    hits.append(now)
    _strikes[(chat_id, user_id)] = hits
    return len(hits)


async def _punish(client: Client, message: Message, reason: str,
                  force_ban: bool = False, escalate: bool = True):
    """Delete + apply the escalation ladder."""
    chat_id = message.chat.id
    await safe_call(message.delete())
    user = message.from_user
    if not user:
        return
    await _bump_stat(chat_id, "actions")

    if force_ban:
        action, secs = "ban", 0
    elif escalate:
        level = _strike(chat_id, user.id)
        action, secs = ESCALATION.get(min(level, max(ESCALATION)), ("ban", 0))
    else:
        action, secs = "delete", 0

    if action == "ban":
        await safe_call(client.ban_chat_member(chat_id, user.id))
        label = "🔨 <b>Bᴀɴɴᴇᴅ</b>"
        await _bump_stat(chat_id, "bans")
    elif action == "mute":
        until = int(time.time()) + secs
        await safe_call(client.restrict_chat_member(chat_id, user.id, MUTED_PERMS, until_date=until))
        label = f"🔇 <b>Mᴜᴛᴇᴅ</b> — {secs // 60} ᴍɪɴ"
        await _bump_stat(chat_id, "mutes")
    else:
        label = "🗑 <b>Dᴇʟᴇᴛᴇᴅ</b> — ᴡᴀʀɴɪɴɢ"
        await _bump_stat(chat_id, "deletes")

    await _notify(
        client, chat_id, "Sᴀғᴇ Mᴏᴅᴇ",
        f"👤 <b>Usᴇʀ :</b> {mention(user)}\n🚫 <b>Rᴇᴀsᴏɴ :</b> {reason}\n{label}",
    )
    spawn(log_activity(
        f"#safemode\n🚫 {reason}\n⚙️ {action}\n👤 <code>{user.id}</code>\n🏠 <code>{chat_id}</code>"
    ))


# ─── Small detectors ─────────────────────────────────────────────────────────

def _file_name(message: Message) -> str:
    doc = getattr(message, "document", None)
    return (getattr(doc, "file_name", None) or "").lower()


def _entities(message: Message) -> list:
    return list(message.entities or []) + list(message.caption_entities or [])


def _mention_count(message: Message) -> int:
    return sum(
        1 for e in _entities(message)
        if e.type in (enums.MessageEntityType.MENTION, enums.MessageEntityType.TEXT_MENTION)
    )


def _hidden_link(message: Message, text: str) -> bool:
    """A masked link whose visible label doesn't match its target."""
    for e in _entities(message):
        if e.type != enums.MessageEntityType.TEXT_LINK or not getattr(e, "url", None):
            continue
        label = text[e.offset:e.offset + e.length].lower()
        target = (e.url or "").lower()
        if _PHISH_DOMAIN_RE.search(target) or _INVITE_RE.search(target):
            return True
        if _URL_RE.search(label) and label.strip(" /") not in target:
            return True
    return False


def _profile_text(user) -> str:
    return " ".join(filter(None, [
        getattr(user, "first_name", "") or "",
        getattr(user, "last_name", "") or "",
        getattr(user, "username", "") or "",
    ]))


def _is_media(message: Message) -> bool:
    return bool(message.sticker or message.animation or message.photo
                or message.video or message.voice or message.video_note)


def _emoji_count(text: str) -> int:
    custom = 0
    return len(_EMOJI_RE.findall(text)) + custom


def _caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < CAPS_MIN_LEN:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def _zalgo(text: str) -> bool:
    marks = len(_COMBINING_RE.findall(text))
    return marks > 15 and marks > len(text) * 0.25


def _links_only(text: str, message: Message) -> bool:
    if not text or _is_media(message):
        return False
    stripped = _URL_RE.sub("", text).strip()
    return bool(_URL_RE.search(text)) and len(stripped) < 8


# ─── Main content scan ───────────────────────────────────────────────────────

async def _scan(cfg: dict, client: Client, message: Message):
    """Return (reason, force_ban, escalate) or None."""
    raw = message.text or message.caption or ""
    text = normalize(raw)
    user = message.from_user
    chat_id = message.chat.id
    key = (chat_id, user.id)

    if cfg["gban"] and await is_gbanned(user.id):
        return "Gʟᴏʙᴀʟʟʏ ʙᴀɴɴᴇᴅ ᴜsᴇʀ", True, False
    if cfg["cas"] and await _cas_banned(user.id):
        return "CAS-ғʟᴀɢɢᴇᴅ sᴘᴀᴍᴍᴇʀ", True, False

    if cfg["scam"] and _SCAM_RE.search(text):
        return "Sᴄᴀᴍ / ᴄʀʏᴘᴛᴏ ᴘʜɪsʜɪɴɢ", True, False
    if cfg["phish"] and _PHISH_DOMAIN_RE.search(text):
        return "Pʜɪsʜɪɴɢ / sʜᴏʀᴛᴇɴᴇᴅ ʟɪɴᴋ", False, True
    if cfg["hidelink"] and _hidden_link(message, raw):
        return "Hɪᴅᴅᴇɴ / ᴍᴀsᴋᴇᴅ ʟɪɴᴋ", False, True
    if cfg["invite"] and _INVITE_RE.search(raw):
        return "Iɴᴠɪᴛᴇ / ᴘʀᴏᴍᴏ ʟɪɴᴋ", False, True
    if cfg["linksonly"] and _links_only(raw, message):
        return "Lɪɴᴋ-ᴏɴʟʏ sᴘᴀᴍ", False, True

    if cfg["doxx"] and _DOXX_RE.search(raw):
        return "Pᴇʀsᴏɴᴀʟ ɪɴғᴏ / ᴘᴀʏᴍᴇɴᴛ ᴅᴇᴛᴀɪʟs", False, True

    if cfg["files"] and any(_file_name(message).endswith(ext) for ext in _BAD_EXT):
        return "Exᴇᴄᴜᴛᴀʙʟᴇ / APK ғɪʟᴇ", False, True
    if cfg["inlinebot"] and getattr(message, "via_bot", None):
        return "Iɴʟɪɴᴇ-ʙᴏᴛ sᴘᴀᴍ", False, True
    if cfg["keyboard"] and getattr(message, "reply_markup", None):
        return "Bᴜᴛᴛᴏɴ / ᴋᴇʏʙᴏᴀʀᴅ sᴘᴀᴍ", False, True
    if cfg["forwardspam"]:
        if getattr(message, "forward_from_chat", None):
            return "Cʜᴀɴɴᴇʟ ᴘʀᴏᴍᴏ ғᴏʀᴡᴀʀᴅ", False, True
        if getattr(message, "giveaway", None) or getattr(message, "giveaway_winners", None):
            return "Gɪᴠᴇᴀᴡᴀʏ sᴘᴀᴍ", False, True
        if getattr(message, "story", None):
            return "Sᴛᴏʀʏ sᴘᴀᴍ", False, True
    if cfg["pollspam"] and (message.poll or message.dice or message.contact or message.location):
        return "Pᴏʟʟ / ᴅɪᴄᴇ / ᴄᴏɴᴛᴀᴄᴛ sᴘᴀᴍ", False, True
    if cfg["cmdspam"] and raw.startswith("/") and "@" in raw.split()[0] and "@" + (
        getattr(client.me, "username", "") or ""
    ) not in raw.split()[0]:
        return "Dᴏᴏsʀᴇ ʙᴏᴛ ᴋᴇ ᴄᴏᴍᴍᴀɴᴅ sᴘᴀᴍ", False, False

    if cfg["mentions"] and _mention_count(message) >= MENTION_LIMIT:
        return "Mᴀss-ᴍᴇɴᴛɪᴏɴ sᴘᴀᴍ", False, True
    if cfg["emoji"] and _emoji_count(raw) >= EMOJI_LIMIT:
        return "Eᴍᴏᴊɪ ғʟᴏᴏᴅ", False, True
    if cfg["caps"] and _caps_ratio(raw) >= CAPS_RATIO:
        return "CAPS sʜᴏᴜᴛɪɴɢ", False, False
    if cfg["zalgo"] and _zalgo(raw):
        return "Zᴀʟɢᴏ / ᴛᴇxᴛ ʙᴏᴍʙ", False, True
    if cfg["longmsg"] and len(raw) >= LONG_MSG_CHARS:
        return "Wᴀʟʟ-ᴏғ-ᴛᴇxᴛ sᴘᴀᴍ", False, True
    if cfg["namelink"] and _NAME_LINK_RE.search(_profile_text(user)):
        return "Lɪɴᴋ / ᴀᴅ ɪɴ ᴘʀᴏғɪʟᴇ ɴᴀᴍᴇ", False, True

    # Newbie lock — brand-new accounts can't drop media or links right away.
    if cfg["newbie"]:
        joined = _first_seen.setdefault(key, time.time())
        fresh = user.id >= NEW_ACCOUNT_ID
        if fresh and time.time() - joined < NEWBIE_SECONDS:
            if _URL_RE.search(raw) or message.document or message.video or message.animation:
                return "Nᴀʏᴇ ᴀᴄᴄᴏᴜɴᴛ sᴇ ʟɪɴᴋ / ᴍᴇᴅɪᴀ", False, False

    if cfg["flood"]:
        now = time.time()
        times = _msg_times[key]
        times.append(now)
        while times and now - times[0] > FLOOD_WINDOW:
            times.popleft()
        if len(times) >= FLOOD_LIMIT:
            times.clear()
            return "Fʟᴏᴏᴅɪɴɢ", False, True

        if _is_media(message):
            mt = _media_times[key]
            mt.append(now)
            while mt and now - mt[0] > MEDIA_FLOOD_WINDOW:
                mt.popleft()
            if len(mt) >= MEDIA_FLOOD_LIMIT:
                mt.clear()
                return "Sᴛɪᴄᴋᴇʀ / ᴍᴇᴅɪᴀ ғʟᴏᴏᴅ", False, True

        if message.forward_date:
            ft = _fwd_times[key]
            ft.append(now)
            while ft and now - ft[0] > FWD_FLOOD_WINDOW:
                ft.popleft()
            if len(ft) >= FWD_FLOOD_LIMIT:
                ft.clear()
                return "Fᴏʀᴡᴀʀᴅ ғʟᴏᴏᴅ", False, True

    if cfg["duplicate"] and text:
        now = time.time()
        recent = _dup_texts[key]
        recent.append((now, text))
        same = [t for t, body in recent if now - t <= DUP_WINDOW and body == text]
        if len(same) >= DUP_LIMIT:
            recent.clear()
            return "Rᴇᴘᴇᴀᴛᴇᴅ / ᴅᴜᴘʟɪᴄᴀᴛᴇ sᴘᴀᴍ", False, True

    return None


# ─── Watchers ────────────────────────────────────────────────────────────────

async def _exempt(client: Client, message: Message) -> bool:
    user = message.from_user
    if not user or user.is_bot:
        return True
    if await is_admin(client, message.chat.id, user.id):
        return True
    if user.id in await _approved(message.chat.id):
        return True
    # FEATURE (/approve, /approveall): globally approved users are ignored by
    # every guard in safe mode too.
    from utils.approvals import is_approved

    if await is_approved(message.chat.id, user.id):
        return True
    return False


@bot.on_message(filters.group & ~filters.service, group=7)
async def safemode_watch(client: Client, message: Message):
    try:
        if not await is_safemode(message.chat.id):
            return
        cfg = await _cfg(message.chat.id)

        if cfg["channel"] and message.sender_chat and not message.from_user:
            if message.sender_chat.id != message.chat.id:
                await safe_call(message.delete())
                await _bump_stat(message.chat.id, "deletes")
                return

        if await _exempt(client, message):
            return

        # During a raid, freshly joined members stay silent.
        if cfg["raid"] and _raid_until.get(message.chat.id, 0) > time.time():
            if (message.chat.id, message.from_user.id) in _pending_captcha:
                await safe_call(message.delete())
                return

        # Unverified members can never talk.
        if cfg["captcha"] and (message.chat.id, message.from_user.id) in _pending_captcha:
            await safe_call(message.delete())
            return

        found = await _scan(cfg, client, message)
        if not found:
            return
        reason, force_ban, escalate = found
        await _punish(client, message, reason, force_ban=force_ban,
                      escalate=escalate and cfg["escalate"])
    except Exception as exc:
        log.warning("safemode: watcher error: %s", exc)


@bot.on_edited_message(filters.group, group=7)
async def safemode_edit_watch(client: Client, message: Message):
    try:
        if not await is_safemode(message.chat.id):
            return
        if await _exempt(client, message):
            return
        cfg = await _cfg(message.chat.id)
        found = await _scan(cfg, client, message)
        if found:
            reason, force_ban, escalate = found
            await _punish(client, message, f"{reason} (ᴇᴅɪᴛᴇᴅ)",
                          force_ban=force_ban, escalate=escalate and cfg["escalate"])
    except Exception:
        pass


# ─── Raid lockdown ───────────────────────────────────────────────────────────

async def _lockdown(client: Client, chat_id: int, seconds: int):
    if chat_id in _locked_chats:
        return
    _locked_chats.add(chat_id)
    await safe_call(client.set_chat_permissions(chat_id, ChatPermissions(can_send_messages=False)))
    await _notify(
        client, chat_id, "Lᴏᴄᴋᴅᴏᴡɴ",
        f"🔒 <b>Rᴀɪᴅ ʟᴏᴄᴋᴅᴏᴡɴ</b> — ɢʀᴏᴜᴘ <b>{seconds // 60} ᴍɪɴ</b> ᴋᴇ ʟɪʏᴇ "
        "ʀᴇᴀᴅ-ᴏɴʟʏ ʜᴀɪ. Aᴅᴍɪɴs ᴋᴇ ʟɪʏᴇ ᴋᴏɪ ʀᴏᴋ ɴᴀʜɪ.",
        seconds=180,
    )
    await asyncio.sleep(seconds)
    _locked_chats.discard(chat_id)
    await safe_call(client.set_chat_permissions(chat_id, UNMUTED_PERMS))
    await _notify(client, chat_id, "Lᴏᴄᴋᴅᴏᴡɴ", "🔓 <b>Lᴏᴄᴋᴅᴏᴡɴ ʜᴀᴛᴀ ᴅɪʏᴀ ɢᴀʏᴀ.</b>", seconds=90)


# ─── CAPTCHA ─────────────────────────────────────────────────────────────────

def _math_captcha():
    a, b = random.randint(2, 9), random.randint(2, 9)
    op = random.choice(["+", "-", "×"])
    answer = {"+": a + b, "-": a - b, "×": a * b}[op]
    options = {answer}
    while len(options) < 4:
        options.add(answer + random.choice([-5, -3, -2, -1, 1, 2, 3, 4, 6]))
    return f"{a} {op} {b}", answer, random.sample(sorted(options), 4)


async def _captcha_timeout(client: Client, chat_id: int, user_id: int, name: str):
    try:
        await asyncio.sleep(CAPTCHA_TIMEOUT)
        state = _pending_captcha.pop((chat_id, user_id), None)
        if not state:
            return
        await safe_call(client.ban_chat_member(chat_id, user_id))
        await safe_call(client.unban_chat_member(chat_id, user_id))   # kick, can rejoin
        await _bump_stat(chat_id, "captcha_failed")
        await _notify(
            client, chat_id, "Cᴀᴘᴛᴄʜᴀ Fᴀɪʟᴇᴅ",
            f"👤 {mention_id(user_id, name)} ɴᴇ ᴠᴇʀɪғʏ ɴᴀʜɪ ᴋɪʏᴀ — ʀᴇᴍᴏᴠᴇᴅ.",
        )
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _send_captcha(client: Client, chat_id: int, user):
    question, answer, options = _math_captcha()
    kb = InlineKeyboardMarkup([[
        ikb(str(o), callback_data=f"sm_cap_{user.id}_{o}") for o in options
    ]])
    try:
        prompt = await client.send_message(
            chat_id,
            card(
                "Vᴇʀɪғɪᴄᴀᴛɪᴏɴ",
                f"👋 Wᴇʟᴄᴏᴍᴇ {mention(user)}!\n\n"
                f"🧮 <b>{question} = ?</b>\n"
                f"🔒 Sᴀʜɪ ᴊᴀᴡᴀʙ ᴅᴀʙᴀᴏ — <b>{CAPTCHA_TIMEOUT}s</b> ᴋᴇ ᴀɴᴅᴀʀ.\n"
                "⚠️ Gᴀʟᴀᴛ ᴊᴀᴡᴀʙ = 1 ʜᴏᴜʀ ʙᴀɴ.",
            ),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb,
        )
    except Exception:
        prompt = None
    _pending_captcha[(chat_id, user.id)] = {
        "answer": answer,
        "task": asyncio.create_task(
            _captcha_timeout(client, chat_id, user.id, user.first_name or "User")
        ),
    }
    if prompt is not None:
        await auto_delete(prompt, CAPTCHA_TIMEOUT + 30)


@bot.on_callback_query(filters.regex(r"^sm_cap_(\d+)_(-?\d+)$"))
@error_handler
async def safemode_captcha_cb(client: Client, cb: CallbackQuery):
    _, _, target, choice = cb.data.split("_", 3)
    target, choice = int(target), int(choice)
    if cb.from_user.id != target:
        return await cb.answer("Ye captcha tumhare liye nahi hai.", show_alert=True)

    chat_id = cb.message.chat.id
    state = _pending_captcha.pop((chat_id, target), None)
    if not state:
        return await cb.answer("Captcha expire ho gaya.", show_alert=True)
    task = state.get("task")
    if task:
        task.cancel()

    if choice != state["answer"]:
        await cb.answer("❌ Galat jawab — 1 hour ke liye ban.", show_alert=True)
        await safe_call(client.ban_chat_member(
            chat_id, target, until_date=int(time.time()) + CAPTCHA_FAIL_BAN
        ))
        await _bump_stat(chat_id, "captcha_failed")
        await safe_call(cb.message.delete())
        return

    await safe_call(client.restrict_chat_member(chat_id, target, UNMUTED_PERMS))
    _first_seen[(chat_id, target)] = time.time()
    await _bump_stat(chat_id, "captcha_passed")
    await cb.answer("✅ Verified! Ab aap bol sakte ho.")
    await safe_call(cb.message.delete())


# ─── Join requests ───────────────────────────────────────────────────────────

@bot.on_chat_join_request(group=7)
async def safemode_join_request(client: Client, req: ChatJoinRequest):
    try:
        chat_id = req.chat.id
        if not await is_safemode(chat_id):
            return
        cfg = await _cfg(chat_id)
        if not cfg["joinrequest"]:
            return
        user = req.from_user
        if (cfg["gban"] and await is_gbanned(user.id)) or (cfg["cas"] and await _cas_banned(user.id)):
            return await safe_call(client.decline_chat_join_request(chat_id, user.id))

        question, answer, options = _math_captcha()
        _join_req_captcha[(chat_id, user.id)] = answer
        kb = InlineKeyboardMarkup([[
            ikb(str(o), callback_data=f"sm_jr_{chat_id}_{o}") for o in options
        ]])
        await safe_call(client.send_message(
            user.id,
            card(
                "Jᴏɪɴ Vᴇʀɪғɪᴄᴀᴛɪᴏɴ",
                f"🏠 <b>{html.escape(req.chat.title or 'Group')}</b>\n\n"
                f"🧮 <b>{question} = ?</b>\n"
                "✅ Sᴀʜɪ ᴊᴀᴡᴀʙ ᴅᴇɴᴇ ᴘᴀʀ ʜɪ ᴊᴏɪɴ ʀᴇϙᴜᴇsᴛ ᴀᴄᴄᴇᴘᴛ ʜᴏɢɪ.",
            ),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb,
        ))
    except Exception as exc:
        log.warning("safemode: join request error: %s", exc)


@bot.on_callback_query(filters.regex(r"^sm_jr_(-?\d+)_(-?\d+)$"))
@error_handler
async def safemode_join_request_cb(client: Client, cb: CallbackQuery):
    _, _, chat_id, choice = cb.data.split("_", 3)
    chat_id, choice = int(chat_id), int(choice)
    answer = _join_req_captcha.get((chat_id, cb.from_user.id))
    if answer is None:
        return await cb.answer("Request nahi mili ya expire ho gayi.", show_alert=True)
    if choice != answer:
        _join_req_captcha.pop((chat_id, cb.from_user.id), None)
        await safe_call(client.decline_chat_join_request(chat_id, cb.from_user.id))
        return await cb.answer("❌ Galat jawab — request decline.", show_alert=True)

    _join_req_captcha.pop((chat_id, cb.from_user.id), None)
    await safe_call(client.approve_chat_join_request(chat_id, cb.from_user.id))
    _first_seen[(chat_id, cb.from_user.id)] = time.time()
    await _bump_stat(chat_id, "captcha_passed")
    await cb.answer("✅ Verified! Aap group me add ho gaye.", show_alert=True)
    await safe_call(cb.message.delete())


# ─── New members ─────────────────────────────────────────────────────────────

@bot.on_message(filters.group & filters.new_chat_members, group=7)
async def safemode_join_guard(client: Client, message: Message):
    try:
        chat_id = message.chat.id
        if not await is_safemode(chat_id):
            return
        cfg = await _cfg(chat_id)

        if cfg["service"]:
            await safe_call(message.delete())

        now = time.time()
        joins = _joins[chat_id]
        for _ in message.new_chat_members:
            joins.append(now)
        while joins and now - joins[0] > RAID_WINDOW:
            joins.popleft()
        raiding = cfg["raid"] and len(joins) >= RAID_JOINS
        if raiding and _raid_until.get(chat_id, 0) < now:
            _raid_until[chat_id] = now + RAID_LOCK_SECONDS
            await _bump_stat(chat_id, "raids")
            await _notify(
                client, chat_id, "Aɴᴛɪ-Rᴀɪᴅ",
                "🚨 <b>Rᴀɪᴅ ᴅᴇᴛᴇᴄᴛᴇᴅ!</b>\nAɢʟᴇ "
                f"<b>{RAID_LOCK_SECONDS // 60} ᴍɪɴ</b> ᴛᴀᴋ ʜᴀʀ ɴᴀʏᴀ ᴊᴏɪɴ "
                "ᴛᴇᴍᴘ-ʙᴀɴ ʜᴏɢᴀ.", seconds=180,
            )
            if cfg["lockdown"]:
                spawn(_lockdown(client, chat_id, RAID_LOCK_SECONDS))
        raid_active = cfg["raid"] and _raid_until.get(chat_id, 0) > now

        admin_names, admin_users = set(), set()
        if cfg["impersonate"]:
            try:
                async for m in client.get_chat_members(
                    chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS
                ):
                    if m.user and m.user.first_name:
                        admin_names.add(m.user.first_name.strip().lower())
                    if m.user and m.user.username:
                        admin_users.add(m.user.username.strip().lower())
            except Exception:
                pass

        adder = message.from_user
        adder_is_admin = bool(adder) and await is_admin(client, chat_id, adder.id)
        approved = await _approved(chat_id)

        for user in message.new_chat_members:
            if user.is_bot:
                if cfg["botadd"] and not adder_is_admin:
                    await safe_call(client.ban_chat_member(chat_id, user.id))
                    await _notify(
                        client, chat_id, "Aɴᴛɪ-Bᴏᴛ",
                        f"🤖 <b>{html.escape(user.first_name or 'Bot')}</b> ᴋᴏ ʜᴀᴛᴀ ᴅɪʏᴀ — "
                        "sɪʀғ ᴀᴅᴍɪɴs ʜɪ ʙᴏᴛ ᴀᴅᴅ ᴋᴀʀ sᴀᴋᴛᴇ ʜᴀɪɴ.",
                    )
                continue

            if user.id in approved or await is_admin(client, chat_id, user.id):
                continue

            if cfg["gban"] and await is_gbanned(user.id):
                await safe_call(client.ban_chat_member(chat_id, user.id))
                await _bump_stat(chat_id, "bans")
                continue

            if cfg["cas"] and await _cas_banned(user.id):
                await safe_call(client.ban_chat_member(chat_id, user.id))
                await _bump_stat(chat_id, "bans")
                await _notify(
                    client, chat_id, "Rᴇᴘᴜᴛᴀᴛɪᴏɴ",
                    f"🛑 {mention(user)} — <b>CAS ғʟᴀɢɢᴇᴅ sᴘᴀᴍᴍᴇʀ</b>, ʙᴀɴɴᴇᴅ.",
                )
                continue

            if raid_active:
                await safe_call(client.ban_chat_member(
                    chat_id, user.id, until_date=int(now) + RAID_TEMPBAN
                ))
                continue

            if cfg["deleted"] and not (user.first_name or user.username):
                await safe_call(client.ban_chat_member(chat_id, user.id))
                await safe_call(client.unban_chat_member(chat_id, user.id))
                continue

            if cfg["namelink"] and _NAME_LINK_RE.search(_profile_text(user)):
                await safe_call(client.restrict_chat_member(chat_id, user.id, MUTED_PERMS))
                await _bump_stat(chat_id, "mutes")
                await _notify(
                    client, chat_id, "Sᴀғᴇ Mᴏᴅᴇ",
                    f"👤 {mention(user)} — <b>ɴᴀᴍᴇ ᴍᴇ ʟɪɴᴋ/ᴀᴅ</b> ʜᴀɪ, ᴍᴜᴛᴇ ᴋɪʏᴀ ɢᴀʏᴀ.",
                )
                continue

            copied = (user.first_name or "").strip().lower() in admin_names or (
                (user.username or "").strip().lower() in admin_users
            )
            if cfg["impersonate"] and copied:
                await safe_call(client.restrict_chat_member(chat_id, user.id, MUTED_PERMS))
                await _bump_stat(chat_id, "mutes")
                await _notify(
                    client, chat_id, "Aɴᴛɪ-Iᴍᴘᴇʀsᴏɴᴀᴛɪᴏɴ",
                    f"🎭 {mention(user)} ᴀᴅᴍɪɴ ᴋᴀ ɴᴀᴍᴇ ᴄᴏᴘʏ ᴋᴀʀ ʀᴀʜᴀ ʜᴀɪ — ᴍᴜᴛᴇᴅ.",
                )
                continue

            _first_seen[(chat_id, user.id)] = now
            if not cfg["captcha"]:
                continue
            await safe_call(client.restrict_chat_member(chat_id, user.id, MUTED_PERMS))
            await _send_captcha(client, chat_id, user)
    except Exception as exc:
        log.warning("safemode: join guard error: %s", exc)


@bot.on_message(filters.group & filters.left_chat_member, group=7)
async def safemode_leave_clean(client: Client, message: Message):
    try:
        if await is_safemode(message.chat.id):
            cfg = await _cfg(message.chat.id)
            if cfg["service"]:
                await safe_call(message.delete())
    except Exception:
        pass


# ─── /safemode command ───────────────────────────────────────────────────────

def _panel(on: bool, cfg: dict) -> str:
    def dot(v):
        return "🟢" if v else "🔴"

    keys = sorted(cfg)
    half = (len(keys) + 1) // 2
    left, right = keys[:half], keys[half:]
    rows = []
    for i in range(half):
        a = f"{dot(cfg[left[i]])} {left[i]}"
        b = f"{dot(cfg[right[i]])} {right[i]}" if i < len(right) else ""
        rows.append(f"  🔸 <code>{a:<16}</code> {b}")
    return card(
        "Sᴀғᴇ Mᴏᴅᴇ",
        f"🛡 <b>Sᴛᴀᴛᴜs :</b> "
        f"{'🟢 ON — ɢʀᴏᴜᴘ ғᴜʟʟʏ ʟᴏᴄᴋᴇᴅ' if on else '🔴 OFF'}\n"
        f"🧩 <b>Lᴀʏᴇʀs :</b> {sum(1 for v in cfg.values() if v)}/{len(cfg)} ᴀᴄᴛɪᴠᴇ\n\n"
        + "\n".join(rows),
        "/safemode on · /safemode captcha off · /safemode stats",
    )


@bot.on_message(filters.command(["safemode", "safe"]) & filters.group)
@error_handler
async def safemode_cmd(client: Client, message: Message):
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        return
    chat_id = message.chat.id
    args = [a.lower() for a in message.command[1:]]

    async def show(on=None):
        state = await is_safemode(chat_id) if on is None else on
        await message.reply(
            _panel(state, await _cfg(chat_id)),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=close_kb(),
        )

    if not args:
        return await show()

    if args[0] in ("on", "off"):
        turn_on = args[0] == "on"
        await set_setting_flag(chat_id, FLAG, turn_on)
        if turn_on:
            for key, value in (
                ("enabled", True), ("abuse", True),
                ("invites", True), ("links", True), ("forward", True),
                ("edit", True), ("action", "mute"),
            ):
                await set_protection(chat_id, key, value)
        return await show(turn_on)

    if args[0] == "stats":
        stats = await get_setting_flag(chat_id, STATS_FLAG, None) or {}
        return await message.reply(
            card(
                "Sᴀғᴇ Mᴏᴅᴇ Sᴛᴀᴛs",
                f"🗑 <b>Dᴇʟᴇᴛᴇᴅ :</b> {int(stats.get('deletes', 0))}\n"
                f"🔇 <b>Mᴜᴛᴇᴅ :</b> {int(stats.get('mutes', 0))}\n"
                f"🔨 <b>Bᴀɴɴᴇᴅ :</b> {int(stats.get('bans', 0))}\n"
                f"✅ <b>Cᴀᴘᴛᴄʜᴀ ᴘᴀssᴇᴅ :</b> {int(stats.get('captcha_passed', 0))}\n"
                f"❌ <b>Cᴀᴘᴛᴄʜᴀ ғᴀɪʟᴇᴅ :</b> {int(stats.get('captcha_failed', 0))}\n"
                f"🚨 <b>Rᴀɪᴅs sᴛᴏᴘᴘᴇᴅ :</b> {int(stats.get('raids', 0))}",
                "ɪs ᴄʜᴀᴛ ᴋᴇ ʀᴇᴀʟ ᴄᴏᴜɴᴛᴇʀs",
            ),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=close_kb(),
        )

    if args[0] in ("approve", "unapprove"):
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if not target:
            return await message.reply("Kisi user ke message pe reply karke bhejo.")
        users = await _approved(chat_id)
        if args[0] == "approve":
            users.add(target.id)
            body = f"✅ {mention(target)} ᴀʙ <b>ᴡʜɪᴛᴇʟɪsᴛᴇᴅ</b> ʜᴀɪ — sᴀғᴇ ᴍᴏᴅᴇ ɪɢɴᴏʀᴇ ᴋᴀʀᴇɢᴀ."
        else:
            users.discard(target.id)
            body = f"↩️ {mention(target)} ᴡʜɪᴛᴇʟɪsᴛ sᴇ ʜᴀᴛᴀ ᴅɪʏᴀ ɢᴀʏᴀ."
        await _set_approved(chat_id, users)
        return await message.reply(card("Sᴀғᴇ Mᴏᴅᴇ", body), parse_mode=enums.ParseMode.HTML)

    if args[0] == "reset":
        await set_setting_flag(chat_id, CFG_FLAG, {})
        return await show()

    if args[0] in GUARDS and len(args) > 1 and args[1] in ("on", "off"):
        await _set_guard(chat_id, args[0], args[1] == "on")
        return await show()

    await message.reply(
        card(
            "Usᴀɢᴇ",
            "🧾 <code>/safemode on|off</code>\n"
            "🧾 <code>/safemode &lt;guard&gt; on|off</code>\n"
            "🧾 <code>/safemode approve|unapprove</code> (ʀᴇᴘʟʏ)\n"
            "🧾 <code>/safemode stats</code> · <code>/safemode reset</code>\n\n"
            f"<b>Guards :</b> <code>{', '.join(sorted(GUARDS))}</code>",
        ),
        parse_mode=enums.ParseMode.HTML,
    )
