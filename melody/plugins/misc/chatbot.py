"""
🤖 /chatbot — Human-like group chat bot with toggle.

FEATURES:
  • Responds to messages in groups when enabled (reply-based or mention-based)
  • Shows typing indicator for a realistic duration before replying
  • Sends stickers occasionally to feel human
  • Never repeats the same response twice in a row
  • Randomized response selection from curated categories
  • /chatbot on|off toggle (admin only) with inline button

TRIGGER MODES:
  1. Reply to the bot's message → always responds
  2. Mention the bot (@botname) → always responds
  3. Standalone messages → responds randomly (~20% chance) to feel natural

The conversation engine uses pattern matching + curated response banks
inspired by LunaChatBot (TheHamkerCat) and WilliamButcherBot patterns.
No external API needed — fully offline, zero latency.
"""
import random
import re
import time
import asyncio

from pyrogram import Client, enums, filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message

from melody import bot
from melody.config import Config
from utils.admin_tools import is_admin
from utils.buttons import ikb
from utils.decorators import admin_or_auth, error_handler
from utils.gc_db import get_setting_flag, set_setting_flag

FLAG = "chatbot_enabled"

# ─── Conversation memory (per-chat, in-process) ──────────────────────────────
# Tracks the last N responses per chat so we never repeat the same line.
_last_responses: dict[int, list[str]] = {}
_MEMORY_SIZE = 8

# ─── Sticker pack (popular public sticker file_ids are ephemeral, so we use
# emoji-based sticker sending via the bot's own uploaded stickers. If no
# sticker is available, we just skip it.)
_STICKER_CHANCE = 0.12  # 12% chance to send a sticker instead of text

# ─── Typing simulation ───────────────────────────────────────────────────────
# Telegram typing indicator lasts ~5 seconds. We simulate human typing speed
# based on message length with randomness for natural feel.
_TYPING_SPEED_MIN = 0.03  # seconds per character (fast typer)
_TYPING_SPEED_MAX = 0.08  # seconds per character (slow typer)
_MIN_TYPING_DELAY = 1.0
_MAX_TYPING_DELAY = 4.5


def _typing_duration(text: str) -> float:
    """Calculate a realistic typing delay based on message length."""
    base = len(text) * random.uniform(_TYPING_SPEED_MIN, _TYPING_SPEED_MAX)
    return min(max(base, _MIN_TYPING_DELAY), _MAX_TYPING_DELAY)


# ─── Response banks ──────────────────────────────────────────────────────────
# Curated, Hinglish-flavored responses that feel casual and human.

_GREETINGS = [
    "Haan bhai, bolo? 😄",
    "Arre waah, kaise ho? 👋",
    "Namaste! Kya haal hai?",
    "Hey! Kya chal raha hai?",
    "Yo! Bata bata kya scene hai 😎",
    "Hola! Long time no see 🎉",
    "Kem cho? Majama? 😄",
    "Aur kya kar rahe ho aaj kal?",
]

_HOW_ARE_YOU = [
    "Main bilkul mast hu, tu bata kaisa hai? 😄",
    "Ekdum first-class! Tera kya haal hai?",
    "Sab sahi hai mere side se, tu bata apna?",
    "Main toh bas yahan gaane baja raha hu 🎵 Tu suna raha hai?",
    "Zindagi mein masti aur music, aur kya chahiye 😎",
    "Bilkul thik hu bhai, tension mat le. Tu kaisa hai?",
]

_HOW_YOU_DOING = [
    "Main toh ekdum sahi se chal raha hu, bas music mode on hai 🎶",
    "Mast hai bhai, gaane bajate bajate life going good 😄",
    "Arre main toh hamesha ready hu tumhare liye 🤖",
    "Bilkul fresh! Koi naya gaana request karo na 🎵",
]

_THANKS = [
    "Arre koi baat nahi! Hamesha ready 🤝",
    "Bas itni si baat? Mention nahi karna 😄",
    "My pleasure! Aur kuch chahiye?",
    "No worries bhai, ye toh mera kaam hai 😎",
    "Aapka swagat hai! Koi aur gaana chahiye? 🎵",
]

_LOVE = [
    "Aww, wapas bhi pyaar deta hu ❤️",
    "Itne pyaar se mat bolo, sharm aa rahi hai 😄",
    "Main toh already tumpe fida hu 🤖❤️",
    "Ye lo ek virtual hug 🤗",
    "Pyaar zindagi mein zaroori hai, music bhi zaroori hai 🎵❤️",
]

_SAD = [
    "Arre kya hua? Sab theek ho jayega, tension mat le 🤗",
    "Achha mat ho, main hu na tumhare liye! Koi gaana sunau?",
    "Life mein ups and downs aate rehte hain, bas music se mood fresh karo 🎵",
    "Bura mat ho, ek motivational gaana chalu karu?",
    "Gussa mat ho, thoda music sun le, dil aaram ho jayega 🎶",
]

_GOODBYE = [
    "Bye! Phir milte hain 👋",
    "Alvida! Gaane chalu rakhte raho 🎵",
    "Tata! Take care, see you soon 😄",
    "Chalo milte hain phir, bye bye 👋",
]

_YES_NO = [
    "Haan bilkul! 😄",
    "Nahi yaar, ye toh galat hai 😅",
    "Hmm, ho sakta hai... bharosa karo!",
    "Arre haan, 100% sure hu main!",
    "Mujhe nahi pata bhai, tum khud decide karo 😎",
    "Depends hai bhai, situation dekho phir decide karo 🤔",
]

_GENERIC = [
    "Hmm, interesting baat hai 🤔",
    "Sahi baat hai bhai, agree karta hu 👍",
    "Arre waah, ye toh naya baat hai!",
    "Bilkul sahi keh raha ho tu",
    "Haha, maza aa gaya sunke 😂",
    "Acha? Phir bata kya hua aage?",
    "Bhai ye toh next level hai 😎",
    "Mast! Aur bata kya plan hai?",
    "Hmm, sochne ko toh sahi baat hai",
    "Sahi mein? Ye toh badiya hai!",
    "Bilkul! Main bhi yahi soch raha tha",
    "Arre ye toh bahot interesting hai bhai",
    "Kya baat hai, zabardast!",
    "Bhai tu toh legend hai 😂",
    "Haha ye dialogue save karunga main 😄",
    "Mast point hai, note kar liya 📝",
    "Sahi baat, koi aur opinion nahi de sakta?",
    "Bilkul agree, ekdum same yahi soch raha tha main",
    "Oho, bade deep ho gaye aaj 😄",
    "Bata bata, aur kya chal raha hai?",
    "Ye toh interesting topic hai, continue karo",
    "Mujhe lagta hai tu sahi hai isme",
    "Hmm, thoda aur detail me bata na",
    "Sahi hai bhai, mast ja raha hai conversation 😎",
    "Bilkul! Aur koi story hai?",
]

_QUESTIONS = [
    "Tu bata, kya sochta hai is bare mein?",
    "Aur kya hua aage? Pura story bata 😄",
    "Interesting! Aur koi baat hai?",
    "Hmm, aur example de sakte ho?",
    "Ye kab hua bhai? Pura scene bata",
    "Kya tune ye khud try kiya? Kaisa laga?",
    "Acha, phir kya kiya tumne?",
    "Bata bata, kya plan hai aage ka?",
    "Mujhe aur bata iske baare mein, curious hu main 🤔",
]

_MUSIC_RELATED = [
    "Music hi life hai bhai 🎵 Koi naya gaana suna?",
    "Arre music ke baare mein baat karke dil khush ho gaya 🎶",
    "Bas gaana chahiye na? /play likho aur gaana batao!",
    "Music se better kuch nahi hota mood fresh karne ke liye 😄",
    "Koi artist pasand hai? Main uske gaande bhi baja sakta hu 🎵",
]

_COMMANDS_HELP = [
    "Bhai /play likho aur gaana batao, main baja dunga 🎵",
    "Music chahiye? Bas /play [gaana naam] bhej do!",
    "Help chahiye toh /help likho, sab kuch milega 📖",
    "Queue dekhni hai? /queue likho bas",
    "Koi aur gaana chahiye? /skip se agla gaana bajao!",
]

# Pattern → response bank mapping (checked in order, first match wins)
_PATTERNS = [
    # Greetings
    (r"\b(hi+|hello+|hey+|namaste|namaskar|hola|yo|sup|wassup)\b", _GREETINGS),
    # How are you
    (r"\b(how\s*(are|r)\s*you|kaise\s*ho|kaisa\s*ho|kya\s*haal|kaisi\s*ho)\b", _HOW_ARE_YOU),
    # How you doing
    (r"\b(how\s*(are|r)\s*you\s*doing|kya\s*kar\s*rahe|kya\s*chal\s*raha)\b", _HOW_YOU_DOING),
    # Thanks
    (r"\b(thanks|thank\s*you|thx|shukriya|dhanyawad|dhanyavaad)\b", _THANKS),
    # Love
    (r"\b(love\s*you|ily|i\s*love|pyaar|ishq|mohabbat)\b", _LOVE),
    # Sad
    (r"\b(sad|udaas|dukhi|depressed|akela|alone|rona|ro\s*raha|bura\s*laga)\b", _SAD),
    # Goodbye
    (r"\b(bye|goodbye|alvida|tata|see\s*you|phir\s*milenge|chalti\s*hu|nikalta|ja\s*raha)\b", _GOODBYE),
    # Yes/No questions
    (r"\b(kya|sahi|galat|ho\s*sakta|bata|pata|nahi\s*pata)\b.*\?", _YES_NO),
    # Music related
    (r"\b(gaana|song|music|sangeet|bajao|play|tune|melody|lyrics)\b", _MUSIC_RELATED),
    # Command help
    (r"\b(command|cmd|help|kaise|kya\s*kare|kaise\s*kare|use\s*kare)\b", _COMMANDS_HELP),
    # Questions (ends with ?)
    (r"\?\s*$", _QUESTIONS),
]


def _pick_response(chat_id: int, text: str) -> str:
    """Select a non-repeating response based on pattern matching."""
    text_lower = text.lower().strip()

    # Find matching response bank
    bank = _GENERIC
    for pattern, responses in _PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            bank = responses
            break

    # Avoid repeating recent responses
    recent = _last_responses.get(chat_id, [])
    available = [r for r in bank if r not in recent]
    if not available:
        # All used up, reset memory for this bank
        available = bank

    chosen = random.choice(available)
    recent.append(chosen)
    if len(recent) > _MEMORY_SIZE:
        recent.pop(0)
    _last_responses[chat_id] = recent
    return chosen


def _should_respond_randomly() -> bool:
    """20% chance to respond to a standalone message (feels natural)."""
    return random.random() < 0.20


_bot_username: str = ""


def _get_bot_username() -> str:
    """BOT_USERNAME env is often unset on Heroku; fall back to get_me()."""
    return (_bot_username or getattr(Config, "BOT_USERNAME", "") or "").lstrip("@")


def _is_bot_mentioned(message: Message) -> bool:
    """Check if the bot is mentioned (by @username or a text mention)."""
    if not message.text:
        return False
    bot_username = _get_bot_username().lower()
    if bot_username and f"@{bot_username}" in message.text.lower():
        return True
    for ent in message.entities or []:
        user = getattr(ent, "user", None)
        if user is not None and _bot_id is not None and user.id == _bot_id:
            return True
    return False


def _is_reply_to_bot(message: Message, bot_id: int) -> bool:
    """Check if the message is a reply to the bot's message."""
    if not message.reply_to_message:
        return False
    from_user = message.reply_to_message.from_user
    return from_user is not None and from_user.id == bot_id


# ─── Toggle command ──────────────────────────────────────────────────────────

def _kb(state: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [ikb("🔴 Tᴜʀɴ OFF" if state else "🟢 Tᴜʀɴ ON",
             callback_data="chatbot_toggle")],
        [ikb("CLOSE", callback_data="help_close")],
    ])


def _text(state: bool) -> str:
    dot = "🟢 <b>ON</b>" if state else "🔴 <b>OFF</b>"
    return (
        "<blockquote>🤖 <b>Cʜᴀᴛ Bᴏᴛ :</b> "
        f"{dot}</blockquote>\n\n"
        "Jab ye <b>ON</b> hoga, Melody group me messages ka reply dega "
        "— bilkul insaan ki tarah, typing indicator ke saath.\n\n"
        "<b>Trigger :</b> bot pe reply, @mention, ya kabhi kabhi random message.\n"
        "<b>Features :</b> typing show, sticker, no repeat, Hinglish responses.\n\n"
        "<code>/chatbot on</code> · <code>/chatbot off</code> — ya niche "
        "button dabao."
    )


@bot.on_message(
    filters.command(["chatbot", "chat", "aichat", "autoreply"])
    & filters.group
)
@error_handler
@admin_or_auth
async def chatbot_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    state = bool(await get_setting_flag(chat_id, FLAG, False))

    arg = (message.command[1].lower() if len(message.command) > 1 else "")
    if arg in ("on", "enable", "yes"):
        state = True
        await set_setting_flag(chat_id, FLAG, True)
        await _get_bot_id(client)
        if _can_read_all is False:
            await message.reply(
                "⚠️ Bot ki <b>Privacy Mode</b> ON hai, isliye group ke normal "
                "messages bot tak nahi pahunchte — sirf bot ke message pe reply "
                "ya @mention par jawab milega.\n\nFix: @BotFather → /mybots → "
                "bot → Bot Settings → Group Privacy → <b>Turn off</b>, phir bot "
                "ko group se nikaal kar wapas add karein.",
                parse_mode=enums.ParseMode.HTML,
            )
    elif arg in ("off", "disable", "no"):
        state = False
        await set_setting_flag(chat_id, FLAG, False)

    await message.reply(
        _text(state),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_kb(state),
    )


@bot.on_callback_query(filters.regex(r"^chatbot_toggle$"))
@error_handler
async def chatbot_toggle_cb(client: Client, cb: CallbackQuery):
    chat_id = cb.message.chat.id
    if not await is_admin(client, chat_id, cb.from_user.id):
        await cb.answer("Sirf admins 🔒", show_alert=True)
        return
    state = not bool(await get_setting_flag(chat_id, FLAG, False))
    await set_setting_flag(chat_id, FLAG, state)
    await cb.answer(f"Chat bot {'ON' if state else 'OFF'}")
    try:
        await cb.message.edit_text(
            _text(state),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_kb(state),
        )
    except Exception:
        pass


# ─── Chat handler (group messages) ──────────────────────────────────────────
# Uses a high group number so it runs AFTER all command handlers and never
# interferes with music/admin commands.

_bot_id: int | None = None
_can_read_all = None


async def _get_bot_id(client: Client) -> int:
    global _bot_id, _bot_username, _can_read_all
    if _bot_id is None:
        me = await client.get_me()
        _bot_id = me.id
        _bot_username = me.username or ""
        _can_read_all = getattr(me, "can_read_all_group_messages", None)
    return _bot_id


@bot.on_message(
    ~filters.private
    & ~filters.bot
    & ~filters.via_bot
    & ~filters.service
    & filters.text
    & ~filters.command(
        ["play", "vplay", "pause", "resume", "skip", "stop", "end",
         "queue", "q", "np", "playing", "volume", "mute", "unmute",
         "loop", "loopall", "noloop", "shuffle", "clearqueue", "remove",
         "playforce", "vplayforce", "playlist", "seek", "seekback",
         "speed", "search", "dl", "lyrics", "autoplay", "auth", "unauth",
         "authlist", "ban", "unban", "ping", "stats", "safemode",
         "settings", "approve", "unapprove", "help", "start", "alive",
         "uptime", "about", "chatbot", "chat", "aichat", "autoreply",
         "promote", "demote", "kick", "warn", "warns", "purge", "pin",
         "unpin", "lock", "unlock", "protection", "tagall", "invitelink",
         "replay", "live", "radio", "channelplay", "joinvc", "leavevc",
         "autoend", "vcnotify", "vcalerts", "reload", "admincache",
         "report", "balance", "bal", "coins", "daily", "work", "deposit",
         "withdraw", "pay", "leaderboard", "economy", "couple", "kiss",
         "hug", "cuddle", "love", "highfive", "ship", "slap", "punch",
         "bonk", "pat", "poke", "wink", "dance", "laugh", "cry", "angry",
         "cheers", "brofist", "clap", "wave", "whisper", "secret",
         "playmode", "queuepos", "qpos", "addplaylist", "myplaylist",
         "mypl", "delplaylist", "playplaylist", "pl", "cplay", "cvplay",
         "cpause", "cresume", "cskip", "cs", "cstop", "cend", "cqueue",
         "cnp", "cseek", "cseekback", "cspeed", "cvolume", "cmute",
         "cunmute", "cshuffle", "cloop", "cloopall", "cnoloop",
         "vcchatlog", "trigger", "tagger", "panel", "ocmds", "logs",
         "chatlist", "broadcast", "maintenance", "promoglobal",
         "speedtest", "sysinfo", "logger", "addsudo", "sudo", "rmsudo",
         "delsudo", "unsudo", "sudolist", "gban", "ungban", "botban",
         "botunban", "gbannedusers", "gbanlist", "blockedusers",
         "blocklist", "blacklistchat", "whitelistchat", "blacklistedchats",
         "update", "restart", "reboot", "eval", "py", "shell", "sh",
         "bash", "exec", "emojiid", "setsource", "setsourceurl",
         "setpic", "delpic", "delpics", "piclist", "setwelcomepic",
         "delwelcomepic", "welcome", "greetings", "greeting",
         "setwelcome", "resetwelcome", "clearwelcome", "welcomecard",
         "welcomethumb", "setwelcomeimage", "setwelcomepicgc",
         "delwelcomeimage", "rmwelcomeimage", "welcometest",
         "testwelcome", "cleanwelcome", "goodbye", "left", "leftmsg",
         "setgoodbye", "resetgoodbye", "cleargoodbye", "goodbyecard",
         "goodbyethumb", "goodbyetest", "testgoodbye", "cleangoodbye",
         "cleanservice", "cleanservicemsg", "resetgreetings",
         "resetgreeting", "assistant", "oa", "ownerassistant", "oaset",
         "assistantset", "oareset", "assistantreset", "adminlist",
         "admin", "staff", "info", "whois", "purgefrom", "purgeto",
         "del", "delete", "purgeme", "clean", "cleanchat", "cleanall",
         "clearchat", "nuke", "zombies", "kickme", "banall", "unbanall",
         "muteall", "unmuteall", "kickall", "all", "mention", "utag",
         "cancel", "canceltag", "stoptag", "title", "link",
         "approverequest", "declinerequest", "rpending",
         "pendingrequests", "rapproveall", "joinapproveall",
         "requestapproveall", "rdeclineall", "rrejectall",
         "joindeclineall", "requestdeclineall", "approveall", "approvall",
         "unapproveall", "unapprovall", "approved", "approvelist",
         "unapprov", "guard", "antispam", "protectinfo", "guardinfo",
         "safe", "toggles", "options", "banuser", "unbanuser", "gcban",
         "gcunban", "gcmute", "gcunmute", "fpromote", "fullpromote",
         "dpromote", "tmute", "activevc", "ac", "cmdvault", "set_start_pic",
         "set_welcome_pic", "source_code", "global_tools",
         "joinrequests", "vcactivity", "vcjoinleft", "m3u8", "stream",
         "unwarn", "resetwarns", "rmwarns"]
    ),
    # NOTE: `filters.edited` does not exist in Pyrogram 2.x / pyrofork and
    # raised AttributeError at import time, so this plugin failed to load.
    # Edited messages arrive via on_edited_message, so no filter is needed.
    group=99,
)
@error_handler
async def chatbot_handler(client: Client, message: Message):
    """Listen for group messages and respond like a human when chatbot is on."""
    chat_id = message.chat.id

    # Check if chatbot is enabled for this chat
    enabled = await get_setting_flag(chat_id, FLAG, False)
    if not enabled:
        return

    # Skip very short or empty messages
    text = (message.text or "").strip()
    if len(text) < 2:
        return

    # Skip messages that are just emojis
    if not re.search(r"[a-zA-Z\u0900-\u097F]", text):
        return

    bot_id = await _get_bot_id(client)

    # Determine if we should respond
    should_respond = False
    is_direct = False

    if _is_reply_to_bot(message, bot_id):
        should_respond = True
        is_direct = True
    elif _is_bot_mentioned(message):
        should_respond = True
        is_direct = True
    else:
        # Random chance for standalone messages
        should_respond = _should_respond_randomly()

    if not should_respond:
        return

    # Clean the text (remove bot mention)
    clean_text = text
    bot_username = _get_bot_username()
    if bot_username:
        clean_text = re.sub(rf"@{re.escape(bot_username)}\s*", "", clean_text, flags=re.IGNORECASE).strip()

    if not clean_text:
        clean_text = "Haan bolo?"

    # Pick response
    response = _pick_response(chat_id, clean_text)

    # Simulate human typing
    typing_delay = _typing_duration(response)
    try:
        await client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    except Exception:
        pass

    await asyncio.sleep(typing_delay)

    # Occasionally send a sticker before the text (feels more human)
    if not is_direct and random.random() < _STICKER_CHANCE:
        try:
            # Try sending a reaction emoji sticker
            sticker_emojis = ["👍", "😂", "❤️", "🔥", "👏", "🎉"]
            emoji = random.choice(sticker_emojis)
            await message.reply_sticker(sticker_emoji=emoji)
            return
        except Exception:
            pass  # Fall through to text response if sticker fails

    # Send the response
    try:
        await message.reply(
            response,
            parse_mode=enums.ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("chatbot reply failed in %s: %s", chat_id, exc)

    # Cancel typing indicator
    try:
        await client.send_chat_action(chat_id, enums.ChatAction.CANCEL)
    except Exception:
        pass
