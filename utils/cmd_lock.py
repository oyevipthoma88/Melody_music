"""
⚡ Strict command lock + instant acknowledgement.

REQUESTED
---------
"All cmnds strictly lock krde, mtlb skip / end / play etc krte ho instant
response aaye uska."

HOW IT WORKS
------------
Every command handler (they all go through ``error_handler``) is wrapped by
:func:`guard`:

1. **Instant response.** The moment a command arrives, the user gets feedback
   within milliseconds — a reaction on their message (fire-and-forget task, so
   it never delays the real work). Nothing else in the pipeline can make the
   user wait before seeing that the bot heard them.

2. **Strict lock.** The same command in the same chat cannot run twice at the
   same time. Spam-tapping ``/skip`` five times used to start five skips (five
   race conditions in the queue). Now the first one runs and the rest are
   silently dropped (only the 👍 reaction, no chat spam such as "already chal
   raha hai"), so playback state can never be corrupted by double execution.

3. **Cooldown.** After a command finishes, the exact same command from the same
   user in the same chat is ignored for a fraction of a second — that kills
   double-tap duplicates without ever blocking a genuine second request.

The lock is process-local (a dict + asyncio), so it costs zero network calls
and adds no latency.
"""
from __future__ import annotations

import time
from utils.tasks import spawn

# Most commands are serialized per chat. Queueable play commands are keyed
# per user so one user's slow search/download cannot drop another user's /play.
_inflight: "dict[tuple[int, str, int], float]" = {}
_QUEUEABLE_PLAY_COMMANDS = {"play", "vplay", "cplay", "cvplay", "playforce", "vplayforce"}
# (chat_id, user_id, command) → finished_at — anti double-tap
_recent: "dict[tuple[int, int, str], float]" = {}
# (chat_id, message_id) → seen_at — the ONE message that already took the lock
_seen_messages: "dict[tuple[int, int], float]" = {}

# A command can never hold the lock longer than this (safety valve against a
# handler that dies without releasing, e.g. hard network hang).
MAX_HOLD = 90.0
COOLDOWN = 0.8

_ACK_EMOJI = "👍"


def _cmd_name(update) -> "str | None":
    text = getattr(update, "text", None) or getattr(update, "caption", None)
    if not text or text[0] not in "/!.":
        return None
    first = text.split(maxsplit=1)[0][1:]
    return first.split("@", 1)[0].lower() or None


def _prune(now: float) -> None:
    for key, ts in list(_inflight.items()):
        if now - ts > MAX_HOLD:
            _inflight.pop(key, None)
    for key, ts in list(_recent.items()):
        if now - ts > 30:
            _recent.pop(key, None)
    for key, ts in list(_seen_messages.items()):
        if now - ts > 120:
            _seen_messages.pop(key, None)


def instant_ack(update) -> None:
    """Give the user feedback immediately, without blocking the handler."""
    try:
        react = getattr(update, "react", None)
        if react is None:
            return

        async def _go():
            try:
                await react(_ACK_EMOJI)
            except Exception:  # noqa: BLE001 — reactions may be off in the chat
                pass

        spawn(_go())
    except Exception:  # noqa: BLE001
        pass


async def acquire(update) -> "tuple[bool, str | None]":
    """Try to take the lock for this command.

    Returns ``(allowed, key)``. When ``allowed`` is False the caller must stop;
    the user has already been answered.
    """
    cmd = _cmd_name(update)
    if not cmd:
        return True, None

    chat = getattr(update, "chat", None)
    user = getattr(update, "from_user", None)
    chat_id = getattr(chat, "id", 0) or 0
    user_id = getattr(user, "id", 0) or 0

    now = time.monotonic()
    _prune(now)

    # ROOT-CAUSE FIX ("/start DM me kaam nahi karta"): Pyrogram delivers the
    # SAME message to every handler group, and `error_handler` wraps them all —
    # including catch-all listeners such as the owner's private-text handler in
    # group -3. That listener grabbed the lock for "start" first, returned
    # immediately (not the owner), and its release() stamped a cooldown, so the
    # real /start handler in group 0 was silently dropped as a "double tap".
    # A message may now take the lock only once: any later handler seeing the
    # very same message is simply let through without locking again.
    msg_key = (chat_id, getattr(update, "id", 0) or 0)
    if msg_key[1] and msg_key in _seen_messages:
        return True, None

    recent_key = (chat_id, user_id, cmd)
    last = _recent.get(recent_key)
    if last is not None and now - last < COOLDOWN:
        return False, None  # silent: it is the user's own double tap

    lock_user_id = user_id if cmd in _QUEUEABLE_PLAY_COMMANDS else 0
    key = (chat_id, cmd, lock_user_id)
    if key in _inflight:
        instant_ack(update)
        return False, None

    _inflight[key] = now
    if msg_key[1]:
        _seen_messages[msg_key] = now
    instant_ack(update)
    return True, f"{chat_id}|{cmd}|{lock_user_id}"


def release(key: "str | None", update=None) -> None:
    if not key:
        return
    try:
        chat_str, cmd, lock_user_str = key.split("|", 2)
        chat_id = int(chat_str)
        lock_user_id = int(lock_user_str)
        _inflight.pop((chat_id, cmd, lock_user_id), None)
        user = getattr(update, "from_user", None)
        _recent[(chat_id, getattr(user, "id", 0) or 0, cmd)] = time.monotonic()
    except Exception:  # noqa: BLE001
        pass
