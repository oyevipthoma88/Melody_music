"""
📋 Logging setup — errors go to LOG_GROUP_ID only, never to users
"""
import html
import logging
import colorlog
import re
import traceback
from urllib.parse import urlsplit, urlunsplit
from pyrogram import enums
from pyrogram.types import LinkPreviewOptions
from melody.config import Config
from strings.themes import fancy

# Formatter
fmt = colorlog.ColoredFormatter(
    "%(log_color)s%(levelname)-8s%(reset)s %(blue)s%(name)s%(reset)s: %(message)s",
    log_colors={
        "DEBUG": "cyan",
        "INFO": "green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "bold_red",
    },
)

handler = logging.StreamHandler()
handler.setFormatter(fmt)

logging.basicConfig(level=logging.INFO, handlers=[handler])

# LOG NOISE FIX: third-party libraries logged every socket connect, every
# session handshake and every single HTTP request at INFO, which buried the
# bot's own lines (and the real errors) in the Heroku log. Warnings and above
# are still shown, so nothing that matters is hidden.
for _noisy, _level in (
    ("pyrogram.session", logging.WARNING),
    ("pyrogram.connection", logging.WARNING),
    ("pyrogram.client", logging.WARNING),
    ("pyrogram.dispatcher", logging.WARNING),
    ("httpx", logging.WARNING),
    ("httpcore", logging.WARNING),
    ("urllib3", logging.WARNING),
    ("yt_dlp", logging.WARNING),
    ("asyncio", logging.WARNING),
):
    logging.getLogger(_noisy).setLevel(_level)

# Pyrogram's FLOOD_WAIT notice ("Waiting for N seconds before continuing") is
# emitted by pyrogram.session.session at WARNING. Real floods still surface as
# errors from the call sites, and the chat/user cache (utils/chat_cache.py)
# removes the cause, so this repetitive line is downgraded to DEBUG.
class _FloodWaitNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "Waiting for" not in msg or "before continuing" not in msg


logging.getLogger("pyrogram.session.session").addFilter(_FloodWaitNoiseFilter())

LOGGER = logging.getLogger("Melody")


_SENSITIVE_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def redact_sensitive_text(value) -> str:
    """Remove signed URL query strings from text written to logs.

    yt-dlp/ffprobe exceptions may contain complete googlevideo URLs with the
    dyno IP, expiry, cookies, PO tokens, and signatures. Those details are not
    needed to diagnose playback and must never be forwarded to Heroku/Telegram
    logs. Keep only the scheme, host, and path so the failing endpoint remains
    identifiable without retaining credentials or signed parameters.
    """
    text = str(value or "")

    def _redact(match):
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
            host = (parts.hostname or "").lower()
            if host.endswith(("googlevideo.com", "youtube.com", "youtube-nocookie.com")):
                return urlunsplit((parts.scheme, parts.netloc.split("@")[-1], parts.path, "", ""))
            if parts.query or parts.fragment:
                return urlunsplit((parts.scheme, parts.netloc.split("@")[-1], parts.path, "", ""))
        except Exception:  # noqa: BLE001 - logging must never break playback
            return "<url-redacted>"
        return raw

    return _SENSITIVE_URL_RE.sub(_redact, text)


def _utc_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def log_activity(text: str):
    """
    A2Z activity logger — sends every notable bot action (not just errors) to
    LOG_GROUP_ID: commands used, songs played, joins/leaves, admin actions,
    ban/auth changes, startup/shutdown, etc.

    Kept completely separate from send_error_log() so a logging failure here
    never raises into command handlers. Never blocks the caller for long —
    callers should fire this with asyncio.create_task() when on a latency
    sensitive path (e.g. /play).
    """
    try:
        from melody import bot
        from melody.config import Config
        if not Config.LOG_GROUP_ID:
            return
        await bot.send_message(
            Config.LOG_GROUP_ID,
            text,
            parse_mode=enums.ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as exc:
        LOGGER.debug("log_activity failed to deliver: %s | text=%s", exc, text)


async def send_error_log(
    text: str,
    exc: Exception = None,
    context: dict | None = None,
    human: str | None = None,
):
    """Send error traceback + structured context to LOG_GROUP_ID only.

    ``context`` is an optional dict of structured details that make the log
    actually debuggable — pass any of: ``chat_id``, ``chat_title``,
    ``user_id``, ``user_name``, ``song_title``, ``video_id``, ``command``,
    ``uploader``, etc. Every key present is rendered as its own line so the
    log channel shows the full picture (which group, which user, which song,
    which command) instead of just a bare error string and a traceback.

    BUG FIX: Use HTML parse mode instead of Markdown to avoid
    ENTITY_BOUNDS_INVALID errors when the error text itself contains
    backticks, asterisks, or other Markdown special characters.
    html.escape() ensures angle brackets, ampersands, etc. don't
    break the HTML entity parser either.
    """
    try:
        from melody import bot
        safe_text = html.escape(redact_sensitive_text(text))
        # REQUESTED ("all logs must in # format" + "log gc me owner ko tag
        # krna"): every error card now carries hashtags for filtering and
        # pings the owner so a crash in ANY group is never missed.
        tags = "#error #crash"
        cmd = (context or {}).get("command")
        if cmd:
            tags += f" #{str(cmd).lower().replace(' ', '_')}"
        msg = (
            f"<b>⚠️ {fancy('Melody Error Log')}</b>\n"
            f"{tags}\n\n<code>{safe_text}</code>"
        )

        if context:
            lines = []
            label_map = {
                "chat_title": "🏠 Chat",
                "chat_id": "🆔 Chat ID",
                "user_name": "🙋 User",
                "user_id": "🆔 User ID",
                "command": "💬 Command",
                "song_title": "🎵 Song",
                "video_id": "🎬 Video ID",
                "uploader": "👤 Uploader",
            }
            for key, label in label_map.items():
                val = context.get(key)
                if val is not None and val != "":
                    lines.append(f"{label}: <code>{html.escape(redact_sensitive_text(val))}</code>")
            # Clickable deep links so the owner can jump straight to the
            # offending chat / user from the log group.
            chat_username = context.get("chat_username")
            if chat_username:
                uname = str(chat_username).lstrip("@")
                lines.append(f"🔗 Cʜᴀᴛ Lɪɴᴋ: https://t.me/{html.escape(uname)}")
            if context.get("user_id"):
                lines.append(
                    "🔗 Usᴇʀ: "
                    f'<a href="tg://user?id={context["user_id"]}">open profile</a>'
                )
            # Include any extra keys not in the label map verbatim
            for key, val in context.items():
                if key in label_map or key == "chat_username" or val is None or val == "":
                    continue
                lines.append(f"{html.escape(key)}: <code>{html.escape(redact_sensitive_text(val))}</code>")
            if lines:
                msg += "\n\n" + "\n".join(lines)

        # DEEP DETAIL (REQUESTED: "log group ke log me details aane chaiye
        # aur jada detailing chaiye puri deeply. Aur error kya hai + real
        # error bug human language me bata dena.")
        if human:
            msg += f"\n\n🧠 <b>Rᴇᴀʟ ɪssᴜᴇ (human language):</b>\n{html.escape(redact_sensitive_text(human))}"

        if exc:
            msg += (
                f"\n\n🐞 <b>Eʀʀᴏʀ Tʏᴘᴇ:</b> <code>{html.escape(type(exc).__name__)}</code>"
                f"\n📄 <b>Mᴇssᴀɢᴇ:</b> <code>{html.escape(redact_sensitive_text(exc) or '(empty)')}</code>"
            )
            tb_obj = getattr(exc, "__traceback__", None)
            frames = traceback.extract_tb(tb_obj) if tb_obj else []
            if frames:
                last = frames[-1]
                msg += (
                    f"\n📍 <b>Wʜᴇʀᴇ:</b> <code>{html.escape(str(last.filename))}"
                    f":{last.lineno}</code> in <code>{html.escape(str(last.name))}</code>"
                    f"\n🧾 <b>Cᴏᴅᴇ:</b> <code>{html.escape(str(last.line or ''))}</code>"
                )
                chain = " → ".join(
                    f"{f.name}:{f.lineno}" for f in frames[-6:]
                )
                msg += f"\n🧗 <b>Cᴀʟʟ ᴘᴀᴛʜ:</b> <code>{html.escape(chain)}</code>"
            cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
            if cause is not None:
                msg += (
                    f"\n🔗 <b>Cᴀᴜsᴇᴅ ʙʏ:</b> <code>"
                    f"{html.escape(type(cause).__name__)}: {html.escape(redact_sensitive_text(str(cause)[:200]))}</code>"
                )
            msg += f"\n🕒 <b>Tɪᴍᴇ (UTC):</b> <code>{_utc_now()}</code>"

            tb = "".join(traceback.format_exception(type(exc), exc, tb_obj)) if tb_obj else traceback.format_exc()
            safe_tb = html.escape(redact_sensitive_text(tb[-3000:]))
            msg += f"\n\n<pre>{safe_tb}</pre>"

        owner_id = getattr(Config, "OWNER_ID", 0)
        if owner_id:
            msg += f'\n\n👑 <a href="tg://user?id={owner_id}">Oᴡɴᴇʀ</a> — please check.'
        # HEROKU LOG FIX (400 MESSAGE_TOO_LONG): long yt-dlp errors + tracebacks
        # pushed the card past Telegram's 4096-char limit, so the error never
        # reached the log group at all. Trim from the middle, keeping the head
        # (what broke) and the tail (owner tag / deepest frames).
        if len(msg) > 4000:
            msg = msg[:2600] + "\n\n<i>… trimmed …</i>\n\n" + msg[-1200:]
        await bot.send_message(
            Config.LOG_GROUP_ID, msg, parse_mode=enums.ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    except Exception as delivery_exc:
        # BUG FIX: this used to pass `exc_info=exc` — the *original* error
        # being reported, not the exception raised by send_message() itself.
        # That hid the real delivery failure (bad parse_mode, peer not
        # found, network issue, etc.) behind an unrelated traceback, making
        # "Failed to send log to group" impossible to actually debug.
        # Log both: the delivery failure (with its own traceback) and the
        # original error text/traceback that failed to reach LOG_GROUP_ID.
        LOGGER.error("Failed to send log to group: %s", redact_sensitive_text(text), exc_info=delivery_exc)
        if exc:
            LOGGER.error("Original error that failed to deliver: %s", redact_sensitive_text(exc), exc_info=exc)
