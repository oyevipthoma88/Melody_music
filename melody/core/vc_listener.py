"""
🛰 VC watcher — voice-chat presence & chat tracking **without joining the call**.

WHY THIS CHANGED
----------------
The previous version pushed the assistant account *into* every live voice
chat as a silent muted listener so it could read in-call updates. That was
wrong for three reasons:

  1. Users saw the music/assistant account sitting in every VC of every group
     (REQUESTED: "assistant bot sbke vc pe jake join ho raha hai, wo hata de").
  2. Every silent join is a `phone.JoinGroupCall` call, so Telegram answered
     with `FLOOD_WAIT_X` and even *real* music joins started failing.
  3. It burned bandwidth/CPU for a feature that does not need a seat.

WHAT IT DOES NOW
----------------
Nothing ever joins a call here. The assistant only *reads* the call from the
outside with plain MTProto lookups (`channels.GetFullChannel` +
`phone.GetGroupParticipants`), which work for any group the assistant is a
member of, whether or not it is inside the call:

  • polls the participant list of every watched chat,
  • diffs it against the previous snapshot → JOIN / LEFT cards,
  • keeps `vc_notify`'s roster fresh so the "in VC" counter is real,
  • drops all state when the voice chat ends.

The VC chat log is fed by `melody.core.vc_chat_log` (group traffic during a
live VC + the raw in-call updates whenever the assistant happens to be in the
call for music). See that module for details.
"""
import asyncio
import time

from melody.logging import LOGGER

_POLL = 10.0                # how often watched chats are re-polled
_watch: set = set()         # chat ids that (recently) had a live voice chat
_note_stamp: dict = {}      # chat_id -> monotonic of last passive probe
_NOTE_COOLDOWN = 20.0        # chats with a known live VC
_COLD_COOLDOWN = 300.0       # chats we have never seen a VC in (flood safety)
_last_poll: dict = {}       # chat_id -> monotonic of last roster poll
_POLL_COOLDOWN = 6.0
_snapshot: dict = {}        # chat_id -> set(user_id) of the last known roster
_task = None


def watch(chat_id: int) -> None:
    """Mark a chat as interesting (VC started / bot used there)."""
    if isinstance(chat_id, int) and chat_id < 0:
        _watch.add(chat_id)


def unwatch(chat_id: int) -> None:
    _watch.discard(chat_id)
    _last_poll.pop(chat_id, None)
    _snapshot.pop(chat_id, None)


def watched() -> list:
    return sorted(_watch)


async def _wanted(chat_id: int) -> bool:
    """Is any VC feature enabled here? Chat log OR join/left cards are enough."""
    from melody.core.vc_chat_log import log_enabled

    if await log_enabled(chat_id):
        return True
    try:
        from utils.gc_db import get_setting_flag

        return bool(await get_setting_flag(chat_id, "vc_activity", True))
    except Exception:  # noqa: BLE001
        return True


async def note(chat_id: int) -> None:
    """Cheap passive hook: called on ordinary group traffic.

    Any message in a group is a free trigger to (re-)check whether a voice
    chat is running there, so a VC that started before the bot came online is
    still picked up — without ever joining it.
    """
    if not isinstance(chat_id, int) or chat_id >= 0:
        return
    now = time.monotonic()
    # FLOOD FIX: probing every chat on every message made Telegram answer
    # channels.GetFullChannel with FLOOD_WAIT, and pyrogram then slept the
    # whole session — that is what made /play and other commands feel dead.
    # Chats with a live VC stay hot, unknown chats are probed rarely.
    cooldown = _NOTE_COOLDOWN if chat_id in _watch else _COLD_COOLDOWN
    if now - _note_stamp.get(chat_id, 0.0) < cooldown:
        return
    _note_stamp[chat_id] = now
    try:
        await ensure(chat_id)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("vc_listener.note failed for %s: %s", chat_id, exc)


async def ensure(chat_id: int) -> bool:
    """Poll a chat's voice chat from the outside. NEVER joins the call.

    Returns True when a voice chat is live in this chat.
    """
    try:
        from melody.core.vc_notify import call_is_live

        if not await _wanted(chat_id):
            return False
        now = time.monotonic()
        if now - _last_poll.get(chat_id, 0.0) < _POLL_COOLDOWN:
            return chat_id in _snapshot
        _last_poll[chat_id] = now

        if not await call_is_live(chat_id):
            if chat_id in _snapshot:
                await release(chat_id)
            return False

        watch(chat_id)
        await _poll_roster(chat_id)
        return True
    except Exception as exc:  # noqa: BLE001 — never break a caller
        LOGGER.debug("vc_listener.ensure failed for %s: %s", chat_id, exc)
        return False


async def _poll_roster(chat_id: int) -> None:
    """Diff the live participant list and emit join/left cards."""
    from melody.core import vc_notify

    roster = await vc_notify.refresh_roster(chat_id, max_age=_POLL_COOLDOWN)
    current = set(roster or ())
    if not vc_notify.roster_is_known(chat_id):
        return

    previous = _snapshot.get(chat_id)
    _snapshot[chat_id] = current
    if previous is None:
        LOGGER.debug(
            "vc_listener[%s]: first roster snapshot — %d member(s) already in the VC "
            "(baseline only, no cards)", chat_id, len(current),
        )
        return  # first sighting: baseline only, no card spam

    joined = sorted(current - previous)
    left = sorted(previous - current)
    if joined or left:
        LOGGER.debug(
            "vc_listener[%s]: roster diff — joined=%s left=%s now=%d member(s)",
            chat_id, joined or "-", left or "-", len(current),
        )
    for user_id in joined:
        await vc_notify.announce(chat_id, user_id, "JOINED")
    for user_id in left:
        await vc_notify.announce(chat_id, user_id, "LEFT")


async def release(chat_id: int) -> None:
    """The voice chat ended — drop every cached bit of state for this chat."""
    try:
        from melody.core import vc_chat_log, vc_notify

        vc_chat_log.forget_chat(chat_id)
        vc_notify.clear_roster(chat_id)
        LOGGER.debug(
            "vc_listener[%s]: voice chat ended — roster, call map and chat-log "
            "buffers cleared", chat_id,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("vc_listener.release failed for %s: %s", chat_id, exc)
    unwatch(chat_id)


async def _startup_scan() -> None:
    """After a restart, find chats that already have a live voice chat."""
    try:
        from utils.database import get_all_chats
        from melody.core.vc_notify import call_is_live

        chats = [c.get("chat_id") for c in await get_all_chats()]
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("vc_listener startup scan skipped: %s", exc)
        return

    # Gentle on purpose: the previous 4-way fan-out over hundreds of chats
    # made Telegram answer channels.GetFullChannel with FLOOD_WAIT, which
    # pyrogram absorbs by sleeping the whole session (log: "Waiting for N
    # seconds … required by channels.GetFullChannel").
    sem = asyncio.Semaphore(2)

    async def probe(chat_id):
        if not isinstance(chat_id, int) or chat_id >= 0:
            return
        # Skip chats where no VC feature is switched on — no reason to spend
        # an RPC (and a slice of the flood budget) on them at all.
        try:
            if not await _wanted(chat_id):
                return
        except Exception:  # noqa: BLE001
            pass
        async with sem:
            try:
                if await call_is_live(chat_id):
                    watch(chat_id)
                    await ensure(chat_id)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.6)

    await asyncio.gather(*(probe(c) for c in chats[:400]), return_exceptions=True)
    LOGGER.info("🛰 vc_listener startup scan done — watching %d chat(s)", len(_watch))


async def watchdog() -> None:
    """Background loop: keep the roster of every watched, live VC fresh."""
    await asyncio.sleep(15)
    try:
        await _startup_scan()
    except Exception:  # noqa: BLE001
        pass
    while True:
        await asyncio.sleep(_POLL)
        for chat_id in list(_watch):
            try:
                from melody.core.vc_notify import call_is_live

                if not await call_is_live(chat_id):
                    await release(chat_id)
                    continue
                await _poll_roster(chat_id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "vc_listener watchdog error for %s (%s: %s)",
                    chat_id, type(exc).__name__, exc,
                )


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(watchdog())
