"""
🩹 Global command-filter patch.

REQUEST: "Gc me kisi bhi bot ki cmnd trigger ho to apna bot response dega —
bhale /help@otherbotusername ho."

Pyrogram's built-in `filters.command()` only fires when a command carries no
`@username` suffix or carries THIS bot's username; `/help@SomeOtherBot` is
ignored. This patch replaces the username check so any `@anything` suffix is
accepted, while still splitting arguments exactly like Pyrogram does. Melody
therefore answers common commands even when they were typed at another music
bot in the same group.

TOGGLE: The owner can turn this behaviour on/off with /trigger on|off (off =
only respond to bare commands and /cmd@OurOwnBot, ignoring /cmd@OtherBot).
Default is ON (the original requested behaviour).

EXCEPTION — /end and /stop: these are destructive (clear the queue + leave
the VC). If the command carries @SomeOtherBot, it was clearly meant for that
other bot, not us — silently ignore it instead of clearing someone's queue.

Applied once from `melody/__init__.create_clients()`.
"""
import re

import pyrogram
from pyrogram import filters as _filters

_applied = False
_cached_username = ""

# Commands that should be IGNORED when aimed at another bot via @username.
# These are destructive — clearing the queue or stopping playback — and
# acting on /end@OtherBot would wipe our queue when the user clearly meant
# the other bot.
_DESTRUCTIVE_CMDS = {"end", "stop", "cend", "cstop"}


def _get_bot_username_lower() -> str:
    """Return the bot's own username (lowercased, without @), or '' if unset.

    BUG FIX: this read ONLY Config.BOT_USERNAME. That value is empty until (or
    unless) the deploy sets it, and with an empty username the toggle below
    could never tell "our bot" from "another bot" — so Melody kept answering
    /cmd@OtherBot even with /trigger OFF. We now fall back to the live client's
    own `me.username` and cache it.
    """
    global _cached_username
    if _cached_username:
        return _cached_username
    try:
        from melody.config import Config
        u = (getattr(Config, "BOT_USERNAME", "") or "").lstrip("@").lower()
    except Exception:
        u = ""
    if not u:
        try:
            from melody import bot as _bot
            me = getattr(_bot, "me", None)
            u = (getattr(me, "username", "") or "").lstrip("@").lower()
        except Exception:
            u = ""
    if u:
        _cached_username = u
    return u


async def _trigger_enabled(chat_id: int) -> bool:
    """Check if responding to other bots' commands is enabled for this chat.
    Default: True (ON)."""
    try:
        from utils.database import get_setting
        val = await get_setting(chat_id, "trigger_response", None)
        if val is None:
            return True
        return bool(val)
    except Exception:
        return True


def apply_command_patch() -> None:
    global _applied
    if _applied:
        return

    original_command = _filters.command

    def command(commands, prefixes="/", case_sensitive=False, **kwargs):
        cmds = [commands] if isinstance(commands, str) else list(commands)
        lowered = {c if case_sensitive else c.lower() for c in cmds}
        prefix_list = (
            [prefixes] if isinstance(prefixes, str) and len(prefixes) == 1
            else list(prefixes or [])
        )
        is_destructive = any(c in _DESTRUCTIVE_CMDS for c in lowered)

        async def func(flt, client, message):
            text = message.text or message.caption
            if not text:
                return False
            text = text.strip()
            if prefix_list and text[0] not in prefix_list:
                return False
            without_prefix = text[1:] if prefix_list else text
            parts = re.split(r"\s+", without_prefix)
            if not parts or not parts[0]:
                return False
            head_full = parts[0]
            head = head_full.split("@", 1)[0]
            if not (head if case_sensitive else head.lower()) in lowered:
                return False

            # ── @username suffix handling ──────────────────────────────
            if "@" in head_full:
                suffix = head_full.split("@", 1)[1].lower()
                our_bot = _get_bot_username_lower()

                # Destructive commands (/end, /stop) aimed at another bot:
                # silently ignore — don't clear our queue.
                if is_destructive and our_bot and suffix != our_bot:
                    return False

                # Non-destructive commands aimed at another bot: only
                # respond if the trigger toggle is ON for this chat.
                # NOTE: when our own username is still unknown, treat any
                # @suffix as "another bot" — the safe side of the toggle.
                if suffix != our_bot:
                    chat_id = message.chat.id if message.chat else 0
                    if not await _trigger_enabled(chat_id):
                        return False

            message.command = [head] + parts[1:]
            return True

        return _filters.create(
            func,
            "CommandAnyBot",
            commands=lowered,
            prefixes=set(prefix_list) if prefix_list else {""},
            case_sensitive=case_sensitive,
        )

    _filters.command = command
    pyrogram.filters.command = command
    _applied = True
    return original_command
