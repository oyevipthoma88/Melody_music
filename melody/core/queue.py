"""
📋 Queue management — per-chat in-memory queues with settings

FIX: format_queue() now returns HTML (not Markdown) so queue_cmd.py can
     send it with parse_mode=HTML and avoid ENTITY_BOUNDS_INVALID when song
     titles contain Markdown special characters (* _ ` [ etc.).
"""
import asyncio
import html
import random
import weakref
from dataclasses import dataclass, fields
from typing import Optional
from utils.database import get_setting, set_setting
from utils.formatters import premium_emoji, PREMIUM_EMOJI_IDS
from melody.core.settings_defaults import AUTOPLAY_DEFAULT
from utils.tasks import spawn


@dataclass
class Track:
    video_id: str
    title: str
    duration: int
    stream_url: str
    thumbnail: str
    uploader: str
    requester_id: int
    requester_name: str
    requested_in: int  # chat_id
    # BUG FIX ("kabhi vplay kabhi play" — video intent silently lost): this
    # used to have no field at all recording whether the track was requested
    # via /vplay (video) or /play (audio-only). _play_next() and
    # try_autoplay() re-play tracks from the queue / AutoPlay without any
    # video argument, so they always defaulted to audio-only — meaning any
    # /vplay request that got queued (something else already playing) came
    # back as audio-only the moment it was actually dequeued and played.
    # Storing the intent on the Track itself makes it survive being queued,
    # popped, looped, or replayed by AutoPlay.
    video: bool = False
    # Original text query, retained so a failed top YouTube hit can be replaced
    # with the next streamable candidate without losing the user's intent.
    source_query: str = ""


# In-memory per-chat state
_queues: dict[int, list[Track]] = {}
_current: dict[int, Track] = {}
_loop: dict[int, str] = {}       # "none" | "single" | "all"
_volume: dict[int, int] = {}     # 0-200 (0 = muted)
_predownloaded: dict[int, Track] = {}  # chat_id -> next AutoPlay track, already cached to /tmp
_persist_locks = weakref.WeakValueDictionary()


async def _save_snapshot_ordered(lock, chat_id, current, queue, loop, volume):
    async with lock:
        from utils.playback_state import save_snapshot
        await save_snapshot(chat_id, current, queue, loop, volume)


def _persist(chat_id: int) -> None:
    """Persist mutations without putting Mongo latency on playback's hot path."""
    try:
        lock = _persist_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            _persist_locks[chat_id] = lock
        # Copy the queue at submission time. The per-chat lock preserves
        # mutation order, preventing an older async write from landing after a
        # later /clear or /skip and resurrecting stale playback state.
        spawn(_save_snapshot_ordered(
            lock, chat_id, _current.get(chat_id), list(_queues.get(chat_id, [])),
            _loop.get(chat_id, "none"), _volume.get(chat_id, 100),
        ))
    except RuntimeError:
        pass


def _track_from_dict(data) -> Optional[Track]:
    """Build a Track from a stored snapshot, ignoring stale/renamed fields.

    A snapshot written by an older build can carry keys this Track no longer
    has (or miss new ones). Filtering here keeps restart-recovery working
    across deploys instead of raising TypeError and losing the whole queue.
    """
    if not isinstance(data, dict):
        return None
    allowed = {f.name for f in fields(Track)}
    payload = {k: v for k, v in data.items() if k in allowed}
    if not payload.get("video_id"):
        return None
    try:
        return Track(**payload)
    except TypeError:
        return None


def restore_snapshot(snapshot: dict) -> None:
    """Restore one validated Mongo snapshot into the in-memory state."""
    chat_id = int(snapshot["chat_id"])
    current = snapshot.get("current")
    queue = snapshot.get("queue") or []
    # A restore can run more than once during reconnect/recovery. Remove old
    # state first; otherwise a snapshot with no current track resurrects the
    # previous track and a malformed volume value can abort the whole restore.
    _current.pop(chat_id, None)
    if current:
        restored = _track_from_dict(current)
        if restored is not None:
            _current[chat_id] = restored
    _queues[chat_id] = [t for t in (_track_from_dict(i) for i in queue) if t]
    mode = snapshot.get("loop", "none")
    _loop[chat_id] = mode if isinstance(mode, str) and mode in {
        "none", "single", "all"
    } else "none"
    try:
        volume = int(snapshot.get("volume", 100))
    except (TypeError, ValueError):
        volume = 100
    _volume[chat_id] = max(0, min(200, volume))


def get_queue(chat_id: int) -> list[Track]:
    return _queues.get(chat_id, [])


def add_to_queue(chat_id: int, track: Track):
    if chat_id not in _queues:
        _queues[chat_id] = []
    _queues[chat_id].append(track)
    _persist(chat_id)


def pop_next(chat_id: int) -> Optional[Track]:
    """Pop next track from queue. Handles loop modes."""
    mode = _loop.get(chat_id, "none")
    current = _current.get(chat_id)

    if mode == "single" and current:
        return current

    if _queues.get(chat_id):
        track = _queues[chat_id].pop(0)
        if mode == "all" and current:
            _queues[chat_id].append(current)
        _current[chat_id] = track
        _persist(chat_id)
        return track

    if mode == "all" and current:
        _current[chat_id] = current
        return current

    return None


def set_current(chat_id: int, track: Track):
    _current[chat_id] = track
    _persist(chat_id)


def get_current(chat_id: int) -> Optional[Track]:
    return _current.get(chat_id)


def active_video_ids() -> set[str]:
    """Every video_id the player still needs on disk.

    Used by utils/diskguard.py so the cache janitor can never delete a file
    that is playing right now, sits in a queue, or was pre-downloaded for
    AutoPlay (deleting those caused mid-song "file not found" stalls).
    """
    ids: set[str] = set()
    for track in _current.values():
        if track and track.video_id:
            ids.add(track.video_id)
    for tracks in _queues.values():
        for track in tracks:
            if track and track.video_id:
                ids.add(track.video_id)
    for track in _predownloaded.values():
        if track and track.video_id:
            ids.add(track.video_id)
    return ids


def clear_queue(chat_id: int, persist: bool = True):
    _queues[chat_id] = []
    _current.pop(chat_id, None)
    # A predownloaded AutoPlay track belongs to the old queue/session. Keeping
    # it here made /stop or /clear followed by a new /play unexpectedly start
    # stale audio, and also kept its media protected from cache eviction.
    _predownloaded.pop(chat_id, None)
    if persist:
        _persist(chat_id)


def remove_from_queue(chat_id: int, position: int) -> Optional[Track]:
    """Remove track at 1-based position."""
    q = _queues.get(chat_id, [])
    idx = position - 1
    if 0 <= idx < len(q):
        track = q.pop(idx)
        _persist(chat_id)
        return track
    return None


def remove_matching_track(chat_id: int, video_id: str) -> bool:
    """Remove the first queued copy of ``video_id`` for force-play."""
    q = _queues.get(chat_id, [])
    for idx, track in enumerate(q):
        if str(getattr(track, "video_id", "")) == str(video_id):
            q.pop(idx)
            _persist(chat_id)
            return True
    return False


def shuffle_queue(chat_id: int):
    q = _queues.get(chat_id, [])
    random.shuffle(q)
    _queues[chat_id] = q
    _persist(chat_id)


# ─── Loop ────────────────────────────────────────────────────────────────────

def set_loop(chat_id: int, mode: str):
    """mode: 'none' | 'single' | 'all'"""
    _loop[chat_id] = mode
    _persist(chat_id)


def get_loop(chat_id: int) -> str:
    return _loop.get(chat_id, "none")


# ─── Volume ──────────────────────────────────────────────────────────────────

def get_volume(chat_id: int) -> int:
    return _volume.get(chat_id, 100)


def set_volume_local(chat_id: int, vol: int):
    # Allow 0 (mute) up to 200
    _volume[chat_id] = max(0, min(200, vol))
    _persist(chat_id)


# ─── AutoPlay pre-download cache ──────────────────────────────────────────────
# Holds the NEXT track AutoPlay has already predicted + downloaded for this
# chat, so when the current song ends there is zero download wait.

def set_predownloaded(chat_id: int, track: Optional[Track]):
    if track is None:
        _predownloaded.pop(chat_id, None)
    else:
        _predownloaded[chat_id] = track


def pop_predownloaded(chat_id: int) -> Optional[Track]:
    return _predownloaded.pop(chat_id, None)


def peek_predownloaded(chat_id: int) -> Optional[Track]:
    return _predownloaded.get(chat_id)

# ─── Last USER-requested song (AutoPlay seed) ─────────────────────────────────
# REQUIREMENT ("autoplay hamesha user ke last played song ke suggestion baje;
# agar autoplay chalte waqt koi user naya song lagaye to uske next wala baje"):
# history also contains AutoPlay's own picks, so seeding from history[-1] made
# AutoPlay drift away from what the humans in the chat actually asked for.
# Every manual /play, /vplay, playlist item and search pick records its id
# here, and AutoPlay always seeds from this id instead.

_last_user_track: dict = {}
# REQUIREMENT ("Vplay hoga to bs vplay autoplay me, play hoga to bs play"):
# remember the MODE (audio /play vs video /vplay) of that last human request
# so AutoPlay keeps streaming in exactly the same mode.
_last_user_mode: dict = {}


def set_last_user_track(chat_id: int, video_id: Optional[str], video: bool = False):
    """Remember the last human-requested song (and whether it was /vplay) and
    invalidate any AutoPlay pick that was predicted from the previous seed."""
    if not video_id:
        _last_user_track.pop(chat_id, None)
        _last_user_mode.pop(chat_id, None)
        return
    previous_id = _last_user_track.get(chat_id)
    previous_mode = _last_user_mode.get(chat_id, False)
    changed = previous_id != video_id or bool(previous_mode) != bool(video)
    _last_user_track[chat_id] = video_id
    _last_user_mode[chat_id] = bool(video)
    if changed:
        # Force AutoPlay to re-seed from this brand-new song or playback mode.
        # The same YouTube ID can be requested through /play and /vplay; a
        # predownload made for one mode is not safe to reuse for the other.
        _predownloaded.pop(chat_id, None)


def get_last_user_track(chat_id: int) -> Optional[str]:
    return _last_user_track.get(chat_id)


def get_last_user_mode(chat_id: int) -> bool:
    """True when the last human request in this chat was a /vplay (video)."""
    return bool(_last_user_mode.get(chat_id, False))




# ─── Autoplay (persisted in DB) ───────────────────────────────────────────────
#
# REQUIREMENT ("autoplay by default on rakh aur off krne ke baad 30 min baad
# auto on kr"): AutoPlay is ON for every chat by default. Turning it OFF is a
# TEMPORARY mute — the OFF timestamp is stored alongside the flag and, once
# AUTOPLAY_OFF_TTL has elapsed, is_autoplay_on() reports ON again (and heals
# the stored flag) without anybody having to run /autoplay on.

AUTOPLAY_OFF_TTL = 30 * 60  # seconds


async def is_autoplay_on(chat_id: int) -> bool:
    val = await get_setting(chat_id, "autoplay", AUTOPLAY_DEFAULT)
    if bool(val):
        return True

    import time as _time

    off_at = await get_setting(chat_id, "autoplay_off_at", 0) or 0
    try:
        off_at = float(off_at)
    except (TypeError, ValueError):
        off_at = 0.0
    if off_at and (_time.time() - off_at) >= AUTOPLAY_OFF_TTL:
        # 30 minutes are up — switch AutoPlay back on automatically.
        await set_autoplay(chat_id, True)
        return True
    return False


async def set_autoplay(chat_id: int, enabled: bool):
    import time as _time

    await set_setting(chat_id, "autoplay", enabled)
    # Remember WHEN it was switched off so it can auto-heal 30 min later.
    await set_setting(chat_id, "autoplay_off_at", 0 if enabled else _time.time())



# ─── Queue display (HTML) ─────────────────────────────────────────────────────

def format_queue(chat_id: int, autoplay_on: bool = False) -> str:
    """
    Returns an HTML-formatted queue string, wrapped in a native Telegram
    <blockquote> so /queue always renders as a quote card.
    Song titles / requester names are html.escape()'d so characters like
    & < > ' " never break Telegram's HTML entity parser.

    BUG FIX ("autoplay on kiya but queue is empty dikha raha"): when the
    manual queue is empty AND no predownloaded track exists yet, this used
    to unconditionally print "Queue is empty" — even when AutoPlay was ON
    and a related track was being predicted/downloaded in the background.
    That misleading message made users think AutoPlay was broken. Now:
    when AutoPlay is ON, show "🤖 AutoPlay is ON — next song will be picked
    automatically" instead of the dead-end "Queue is empty" text.
    """
    current = _current.get(chat_id)
    q = _queues.get(chat_id, [])
    predownloaded = _predownloaded.get(chat_id)

    lines = [f"<b>{premium_emoji(PREMIUM_EMOJI_IDS['queue'], '📋')} Music Queue</b>\n"]
    if current:
        safe_title = html.escape(current.title[:45])
        safe_name  = html.escape(current.requester_name)
        lines.append(f"<b>▶️ Now Playing:</b>\n<code>{safe_title}</code> — {safe_name}\n")

    if q:
        lines.append("<b>⏳ Up Next:</b>")
        for i, t in enumerate(q[:10], 1):
            safe_t = html.escape(t.title[:40])
            safe_n = html.escape(t.requester_name)
            lines.append(f"<code>{i}.</code> {safe_t} — <i>{safe_n}</i>")
        if len(q) > 10:
            lines.append(f"\n<i>...and {len(q) - 10} more</i>")
        if predownloaded:
            safe_p = html.escape(predownloaded.title[:40])
            lines.append(f"\n<b>🤖 AutoPlay ready:</b> <code>{safe_p}</code> (pre-downloaded)")
    elif predownloaded:
        safe_p = html.escape(predownloaded.title[:40])
        lines.append("<b>⏳ Up Next (AutoPlay):</b>")
        lines.append(f"<code>{safe_p}</code>")
        lines.append("🙋 Requested by: <i>AutoPlay</i>")
    elif autoplay_on:
        lines.append("🤖 <b>AutoPlay is ON</b> — next song will be picked automatically")
    else:
        lines.append("<i>Queue is empty</i>")

    return f"<blockquote>{chr(10).join(lines)}</blockquote>"
