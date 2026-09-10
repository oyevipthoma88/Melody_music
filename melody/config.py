"""
🔑 Configuration — reads from .env only, NEVER hardcoded
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int = 0) -> int:
    """Read an int env var without ever crashing the boot.

    BUG FIX: every numeric setting used `int(os.environ.get(NAME, 0))`. On
    Heroku/Docker an unset var is often present as an EMPTY string (or a value
    with quotes/spaces, e.g. `LOG_GROUP_ID="-100123"`), and `int("")` raises
    ValueError at import time — the bot died before the logger even started,
    with a traceback that pointed at config.py instead of the bad var.
    """
    raw = (os.environ.get(name) or "").strip().strip('"').strip("'")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().strip('"').strip("'").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y")


def _env_str(name: str, default: str = "") -> str:
    """Strip stray whitespace/quotes that dashboards keep adding to values."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1].strip()
    return raw or default


def _mongo_uri(name: str) -> str:
    """Normalize a URI pasted from a Deploy Button/config dashboard.

    Dashboards sometimes receive ``MONGO_DB_URI=mongodb+srv://...`` as the
    value instead of just the URI. Removing that accidental assignment prefix
    is safe; all other malformed values are rejected before Motor initializes.
    """
    value = _env_str(name)
    prefix = f"{name}="
    if value.startswith(prefix):
        value = value[len(prefix):].strip().strip('"\'')
    return value


def _validate_mongo_uri(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(
            f"{name} is empty. Set the complete MongoDB URI beginning with "
            "mongodb:// or mongodb+srv:// in Heroku Config Vars."
        )
    if not value.startswith(("mongodb://", "mongodb+srv://")):
        raise RuntimeError(
            f"{name} is invalid. Paste only the complete MongoDB URI beginning "
            "with mongodb:// or mongodb+srv:// (not a label, dashboard URL, or placeholder)."
        )
    # Catch the exact PyMongo error seen in the latest deploy before a client
    # is constructed: a trailing comma or empty host in a copied URI.
    hosts = value.split("@", 1)[-1].split("?", 1)[0]
    if any(not host.strip() for host in hosts.split(",")):
        raise RuntimeError(
            f"{name} contains an empty host or extra comma. Copy the complete "
            "Atlas connection string again without editing its host list."
        )
    return value


class Config:
    # Telegram API credentials
    API_ID: int = _env_int("API_ID")
    API_HASH: str = _env_str("API_HASH")
    BOT_TOKEN: str = _env_str("BOT_TOKEN")
    STRING_SESSION: str = _env_str("STRING_SESSION")

    # Database
    # Validation happens immediately before Motor client construction in
    # utils.database, so lightweight modules can still load for diagnostics.
    MONGO_DB_URI: str = _mongo_uri("MONGO_DB_URI")

    # Owner settings (alias only — real identity NEVER exposed)
    OWNER_ID: int = _env_int("OWNER_ID")
    OWNER_NAME: str = _env_str("OWNER_NAME", "Maestro")

    # Logging
    LOG_GROUP_ID: int = _env_int("LOG_GROUP_ID")

    # 💬 Dedicated channel for the voice-chat (in-call) chat backup.
    # REQUESTED: VC chat lines go to the group they came from **and** to this
    # channel — never to LOG_GROUP_ID, which stays free of VC chat spam.
    # Disabled unless the deployer explicitly configures a private backup
    # channel. Never inherit a channel ID from another bot instance.
    VC_CHAT_LOG_CHANNEL_ID: int = _env_int("VC_CHAT_LOG_CHANNEL_ID")
    # Fallback music files are archived in Telegram, never MongoDB.
    MUSIC_ARCHIVE_CHANNEL_ID: int = _env_int("MUSIC_ARCHIVE_CHANNEL_ID")

    # YouTube
    YT_COOKIES: str = _env_str("YT_COOKIES")

    # ⚡ YouTube Data API v3 key(s) — the fastest possible metadata/search
    # path (a plain HTTPS GET to googleapis.com that answers in ~200-400ms,
    # with no bot-detection wall, no player-JS challenge and no cookies).
    # Set YOUTUBE_API_KEY in Heroku vars. Multiple keys may be given
    # comma-separated; the bot rotates to the next one automatically when a
    # key hits its daily quota (403 quotaExceeded).
    YOUTUBE_API_KEY: str = _env_str("YOUTUBE_API_KEY") or _env_str("YT_API_KEY")

    # Lyrics
    GENIUS_API_TOKEN: str = _env_str("GENIUS_API_TOKEN")

    # Bot settings
    # No song-duration cap: Melody plays full songs, mixes, even long live
    # sets, with no artificial cutoff anywhere in the codebase.
    AUTOPLAY: bool = _env_bool("AUTOPLAY", True)
    # Optional lean profile: load only music plugins and skip unrelated admin,
    # social and utility handlers. Full Melody remains the default.
    MUSIC_ONLY_MODE: bool = _env_bool("MUSIC_ONLY_MODE", False)
    BOT_USERNAME: str = _env_str("BOT_USERNAME").lstrip("@")
    SUPPORT_URL: str = _env_str("SUPPORT_URL")
    SOURCE_CODE_URL: str = _env_str("SOURCE_CODE_URL")
    # Welcome animated sticker (file_id of any Telegram sticker/animation)
    # Set in .env: WELCOME_STICKER=<file_id>
    # To get a file_id: forward any sticker to your bot and use /eval to print it
    WELCOME_STICKER: str = _env_str("WELCOME_STICKER")

    # Expensive startup warm-ups are opt-in. They launch yt-dlp/Deno work before
    # any user asks for music and can spike RSS on 512 MB dynos. The low-memory
    # profile wins over stale enable flags so no manual Config Var cleanup is
    # required after upgrading.
    MEMORY_LIMIT_MB: int = _env_int("MEMORY_LIMIT_MB", 512)
    _LOW_MEMORY_PROFILE: bool = MEMORY_LIMIT_MB <= 768
    STARTUP_WARMUPS: bool = (
        _env_bool("STARTUP_WARMUPS", False) and not _LOW_MEMORY_PROFILE
    )
    # The bgutil HTTP server is a single persistent process and is required
    # for fast direct YouTube URL resolution. Disabling it on 512 MB dynos
    # pushed its Deno startup into the first /play request, adding 3–6s and
    # making the direct path lose to the slow full-download fallback. It is
    # still explicitly opt-out via BGUTIL_STARTUP_WARMUP=false.
    BGUTIL_STARTUP_WARMUP: bool = (
        _env_bool("BGUTIL_STARTUP_WARMUP", True)
    )
    # Recovery re-joins every active snapshot after a dyno restart. On a
    # 512MB dyno this can start many simultaneous voice/download transitions
    # before the first user command and can resurrect an old track. Keep it
    # opt-in; fresh /play is authoritative and stable by default.
    PLAYBACK_RECOVERY: bool = _env_bool("PLAYBACK_RECOVERY", False)

    # GitHub integration — used by /setpic to persist the bot's start image
    # across fresh deployments. Set GITHUB_TOKEN to a Personal Access Token
    # with repo write permissions and GITHUB_REPO to "username/reponame".
    GITHUB_TOKEN: str = _env_str("GITHUB_TOKEN")
    GITHUB_REPO: str = _env_str("GITHUB_REPO")
    # GitHub username (owner of the repo) — used by /setpic and live-edit
    # commits so the bot can address the correct account without parsing it
    # out of GITHUB_REPO. Example: <owner>
    GITHUB_REPO_USERNAME: str = _env_str("GITHUB_REPO_USERNAME")

    # Theme colors (Modi-Meloni)
    COLORS = {
        "saffron":  "#FF6600",
        "gold":     "#FFD700",
        "green":    "#009246",
        "red":      "#CE2B37",
        "white":    "#FFFFFF",
        "dark":     "#1A0500",
        "overlay":  "rgba(0,0,0,0.55)",
    }
