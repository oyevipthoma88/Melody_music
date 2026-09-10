"""
📞 Voice call management — py-tgcalls 2.x (PyTgCalls + MediaStream API)

FIXES APPLIED:
  • Instant VC join (silence trick) — bot joins the voice chat IMMEDIATELY
    with a short silence stream before yt-dlp finishes searching/downloading.
    When the real audio is ready, change_stream() swaps in the song (~instant).
    User hears the bot join in <1 s instead of waiting 5-8 s for yt-dlp.

  • Silence race guard — _silence_playing[chat_id] flag prevents the silence
    file's stream_end event from being treated as "song finished". Without this,
    the bot would leave VC prematurely if the silence ends before the song
    download completes.

  • Pipe-safe cleanup — /tmp files from FIFO pipe paths are NOT cleaned up
    (the FIFO writer thread handles its own tmpdir). Only real /tmp/melody_*
    files are deleted after playback to free space on Heroku's 512 MB /tmp.

  • Per-chat play lock (FIX concurrent /play race) — if two users type /play
    at almost the same moment, both could previously pass the `_active` check
    before either had set _active[chat_id] = True, causing both to try to
    start a new stream instead of one playing and one queuing. _play_locks
    serialises play_stream() per chat: the second request always sees the
    correct _active state and is properly added to the queue.

FIX 1 (Silent crash): lazy PyTgCalls init inside start_call_py().
FIX 2 (AttributeError): AudioQuality.HIGH_QUALITY (2.x) with STUDIO fallback.
FIX 3: Handler registration moved inside start_call_py().
FIX 4: Per-chat asyncio locks eliminate the concurrent /play race condition.
"""
import asyncio
from html import escape as html_escape
import glob
import os
import tempfile
import time

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, StreamEnded
from pytgcalls.exceptions import NoActiveGroupCall


# Give direct-CDN resolution a short exclusive window before starting the
# fallback downloader. Starting yt-dlp immediately on cloud hosts caused
# YouTube 403/bot-check retries to compete with otherwise playable direct URLs.
# The delay is configurable with DOWNLOAD_START_DELAY and remains bounded by
# the resolver/fallback race; the semaphore in ytdl.py still caps extractors.
_IS_CLOUD_RUNTIME = bool(
    os.getenv("DYNO")
    or os.getenv("RAILWAY_ENVIRONMENT")
    or os.getenv("RENDER_SERVICE_ID")
    or os.getenv("FLY_APP_NAME")
)
try:
    # The fallback downloader must start immediately. A fixed grace period
    # made a blocked/slow direct resolver add latency even though the fallback
    # was the only source that could eventually play on cloud dynos.
    configured_download_delay = float(os.getenv("DOWNLOAD_START_DELAY", "0.0"))
except Exception:  # noqa: BLE001
    configured_download_delay = 0.0
# Start direct resolution and the fallback downloader in parallel. The old
# cloud-only grace period made blocked CDN URLs wait 8–20 seconds before yt-dlp
# even started. Deduplication and owner-scoped cancellation prevent duplicate
# downloads while preserving the fastest successful source.
_DOWNLOAD_START_DELAY = max(0.0, configured_download_delay)
# How long py-tgcalls' internal ffprobe gets to open a direct CDN URL before
# we give up on it and fall back to the (much slower) full download.
try:
    _PLAY_PROBE_TIMEOUT = max(0.8, float(os.getenv("PLAY_PROBE_TIMEOUT", "2.5")))
except Exception:  # noqa: BLE001
    _PLAY_PROBE_TIMEOUT = 2.5
try:
    _LOCAL_PROXY_PLAY_TIMEOUT = max(
        _PLAY_PROBE_TIMEOUT,
        float(os.getenv("LOCAL_PROXY_PLAY_TIMEOUT", "15")),
    )
except Exception:  # noqa: BLE001
    _LOCAL_PROXY_PLAY_TIMEOUT = max(_PLAY_PROBE_TIMEOUT, 15.0)
try:
    _LOCAL_PLAY_TIMEOUT = max(
        5.0, float(os.getenv("LOCAL_PLAY_TIMEOUT", "9"))
    )
except Exception:  # noqa: BLE001
    _LOCAL_PLAY_TIMEOUT = 9.0
try:
    # Long /vplay requests must remain remote-streaming. A failed direct
    # resolver must never turn a multi-GB movie into a /tmp download.
    _VIDEO_DIRECT_ONLY_SECONDS = max(
        300, int(os.getenv("VIDEO_DIRECT_ONLY_SECONDS", "900"))
    )
except (TypeError, ValueError):
    _VIDEO_DIRECT_ONLY_SECONDS = 900
try:
    _CONTROL_RPC_TIMEOUT = max(
        2.0, float(os.getenv("CONTROL_RPC_TIMEOUT", "5"))
    )
except Exception:  # noqa: BLE001
    _CONTROL_RPC_TIMEOUT = 5.0

try:
    # ⚡ 5-SECOND RULE: playback must be audible within ~5s of /play. Search
    # costs ~0.5s, the VC join is ~0s (pre-joined silence), so the source race
    # gets a hard 4s budget. Whatever is ready first — direct CDN URL or the
    # early WebM/Opus prefix of the fallback download — starts the song; the
    # rest keeps downloading in the background.
    _PLAY_START_BUDGET = max(
        2.0, min(10.0, float(os.getenv("PLAY_START_BUDGET", "4.0")))
    )
except Exception:  # noqa: BLE001
    _PLAY_START_BUDGET = 4.0
try:
    # The source race is intentionally short, but a fallback download that has
    # already started must not be cancelled at that boundary. Give it its own
    # bounded grace period so slow YouTube/CDN resolution does not become a
    # false playback crash.
    # ⚡ LONG-MIX FIX: Increased grace window to 90s (max 120s) so 1hr+ downloads don't crash
    _PLAY_FALLBACK_TIMEOUT = max(30.0, min(180.0, float(os.getenv("PLAY_FALLBACK_TIMEOUT", "30"))))
except Exception:  # noqa: BLE001
    _PLAY_FALLBACK_TIMEOUT = 30.0

try:  # py-tgcalls raises this when the assistant is not connected to the VC
    from pytgcalls.exceptions import NotInCallError
except ImportError:  # pragma: no cover - older py-tgcalls builds
    class NotInCallError(Exception):
        pass
from pyrogram import enums
from pyrogram.types import ChatPermissions
from pyrogram.errors import (
    ChannelInvalid,
    FloodWait,
    ChannelPrivate,
    ChatAdminRequired,
    PeerIdInvalid,
    UserBannedInChannel,
)
from melody.logging import LOGGER, redact_sensitive_text, send_error_log


async def _persist_completed_song(filepath, track) -> None:
    """Archive completed fallback media in Telegram, outside playback hot path."""
    if not filepath or not track or filepath.endswith((".early", ".part", ".temp")):
        return
    try:
        from melody import bot as _bot
        from utils.telegram_archive import archive_completed_file
        ok = await archive_completed_file(
            _bot,
            track.video_id,
            getattr(track, "title", "Melody"),
            filepath,
            video=bool(getattr(track, "video", False)),
        )
        if ok:
            LOGGER.info("telegram archive saved %s", getattr(track, "video_id", "?"))
    except Exception as exc:  # archive must never affect playback
        LOGGER.debug("telegram archive skipped for %s: %s", getattr(track, "video_id", "?"), exc)


from melody.core.queue import (
    get_current, set_current, pop_next, clear_queue,
    get_volume, set_volume_local, is_autoplay_on, get_queue, get_loop,
)

# Shown to the group whenever we try to (re)join / stream but Telegram
# reports there is no active voice/video chat at all (py-tgcalls raises
# NoActiveGroupCall — a video chat must already be started by someone in
# the app before the bot/assistant can join it).
NO_ACTIVE_VC_MESSAGE = (
    "ɴᴏ ᴀᴄᴛɪᴠᴇ ᴠɪᴅᴇᴏᴄʜᴀᴛ ꜰᴏᴜɴᴅ.\n\n"
    "ᴘʟᴇᴀsᴇ sᴛᴀʀᴛ ᴠɪᴅᴇᴏᴄʜᴀᴛ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ / ᴄʜᴀɴɴᴇʟ ᴀɴᴅ ᴛʀʏ ᴀɢᴀɪɴ."
)

VC_ADMIN_REQUIRED_MESSAGE = (
    "⚠️ <b>Voice chat start nahi ho paya — assistant account admin nahi hai.</b>\n\n"
    "Naya voice chat sirf group ka admin start kar sakta hai. Assistant account ko "
    "admin bana do, ya group me manually voice chat start karke phir <code>/play</code> try karo."
)

# A failed pre-join is permission information, not an audio failure. Remember it
# briefly so the same request does not download/probe a full track for 20+ sec
# only to receive the identical CHAT_ADMIN_REQUIRED error from createGroupCall.
_vc_admin_block: dict[int, float] = {}
_vc_admin_notified: dict[int, float] = {}
_VC_ADMIN_BLOCK_FOR = 45.0


def _vc_admin_blocked(chat_id: int) -> bool:
    deadline = _vc_admin_block.get(chat_id, 0.0)
    if deadline and time.monotonic() < deadline:
        return True
    _vc_admin_block.pop(chat_id, None)
    _vc_admin_notified.pop(chat_id, None)
    return False


def _block_vc_admin(chat_id: int) -> None:
    _vc_admin_block[chat_id] = time.monotonic() + _VC_ADMIN_BLOCK_FOR


def _vc_admin_notice_needed(chat_id: int) -> bool:
    return time.monotonic() >= _vc_admin_notified.get(chat_id, 0.0)


def _mark_vc_admin_notified(chat_id: int) -> None:
    _vc_admin_notified[chat_id] = time.monotonic() + _VC_ADMIN_BLOCK_FOR


def _clear_vc_admin_block(chat_id: int) -> None:
    _vc_admin_block.pop(chat_id, None)
    _vc_admin_notified.pop(chat_id, None)


def is_vc_admin_blocked(chat_id: int) -> bool:
    """Return whether a recent Telegram denial should short-circuit playback."""
    return _vc_admin_blocked(chat_id)


# Lazy — set by start_call_py() on first call
_pytgcalls: "PyTgCalls | None" = None

# Per-chat play state
_active: dict = {}             # chat_id → bool
# BUG FIX ("/leave /stop kaam nahi karta — bot wapas VC me aa jata hai"):
# leaving a call makes py-tgcalls emit StreamEnded for that chat. That update
# used to run the normal end-of-song path → _play_next() → AutoPlay → the
# assistant re-joined the voice chat it had just been told to leave (or the
# queue advanced and started the next song). Every deliberate disconnect now
# marks the chat here first; the StreamEnded that the disconnect itself causes
# is ignored for _LEAVE_GRACE seconds, after which the entry expires on its
# own so a later /play is never blocked.
_leaving: dict = {}            # chat_id → monotonic deadline
_LEAVE_GRACE = 8.0
_silence_playing: dict = {}    # chat_id → bool; True while silence stream is active
_is_video: dict = {}           # chat_id → bool; True if current track is streaming as video
_play_start_time: dict = {}    # chat_id → float (time.time()) when current track started/last sought
_seek_offset: dict = {}        # chat_id → int seconds; playback position baked into the last stream swap
_speed: dict = {}              # chat_id → float playback speed (1.0 = normal)
_muted: dict = {}              # chat_id → bool; True while the stream is muted
# 🎧 LISTENER MODE — the assistant is inside the voice chat ONLY to read the
# in-call (VC) chat, with no music playing. Telegram delivers
# `UpdateGroupCallMessage` / `UpdateGroupCallParticipants` exclusively to call
# participants, so without a body inside the call there is nothing to log.
# This is what makes the VC chat log work even when no song is playing.
_listener_mode: dict = {}      # chat_id → bool

# FIX 4: Per-chat locks that serialise play_stream() calls.
# Two concurrent /play commands for the same group acquire this lock in order;
# the second one always sees the updated _active state and gets queued instead
# of trying to start a second stream simultaneously.
_play_locks: dict[int, asyncio.Lock] = {}
# A source may resolve outside the play lock, but the final PyTgCalls swap must
# be atomic per chat. This closes the check-then-await race where an older
# stream passed its generation check, then completed play() after a newer
# request and became the audible (wrong) track.
_stream_commit_locks: dict[int, asyncio.Lock] = {}

# BUG FIX ("next song must download krke rakh taki delay na lage"): the old
# pre-download logic only ever warmed the /tmp cache for AutoPlay's own
# prediction — a manually queued song (e.g. /play used twice in a row) was
# NEVER pre-fetched, so every queued track still paid the full yt-dlp search
# + download latency the instant its turn came up, which is exactly the
# "gana bajne me time leta hai" delay for anything beyond the first song.
# chat_id -> video_ids currently being pre-downloaded for the next three
# queue positions. Repeated triggers cannot launch duplicate yt-dlp work for
# the same upcoming track.
_prefetch_inflight: dict[int, set[str]] = {}
_FORCE_DOWNLOAD_PRIORITY = -10

# ─── Play generation / idempotency guards ─────────────────────────────────
# BUG FIX (audio/video mismatch + "video plays twice"): a pre_join silence
# stream, a stale resolve/download from an earlier /play, and the natural
# end-of-song advance could all race to call `_pytgcalls.play()` for the
# same chat. Whichever call *finished last* won, regardless of whether it
# was still the track/mode the user actually wanted — that is exactly what
# produced "audio of one track with the video of another" and a track
# starting twice. Every attempt to actually commit a stream to py-tgcalls
# now carries a monotonically increasing per-chat generation token; only the
# call holding the CURRENT token for its chat is allowed to call play() or
# mutate playback state — anything else is a stale winner of an already-lost
# race and quietly discards itself instead.
_gen_counter: dict[int, int] = {}          # chat_id → last minted generation
_stream_generation: dict[int, int] = {}    # chat_id → generation that is authoritative right now
_resolving: dict[int, int] = {}            # chat_id → generation currently inside _stream_track
_resolving_track: dict[int, tuple[str, bool]] = {}  # chat_id → (video_id, video)
_resolving_origin: dict[int, str] = {}  # chat_id → manual|queue|autoplay


def _cancel_stale_download(chat_id: int, new_video_id: str, new_video: bool) -> None:
    """Cancel only a stale different-track download owned by this chat."""
    previous = _resolving_track.get(chat_id)
    if not previous or (previous[0] == new_video_id and previous[1] == new_video):
        return
    try:
        from melody.core.ytdl import cancel_download
        if cancel_download(previous[0], audio_only=not previous[1], owner=chat_id):
            LOGGER.info(
                "#download cancel stale chat=%s video=%s for new_video=%s",
                chat_id, previous[0], new_video_id,
            )
    except Exception as exc:  # noqa: BLE001 - cancellation is best effort
        LOGGER.debug("stale download cancellation skipped for %s: %s", chat_id, exc)


def _begin_generation(chat_id: int) -> int:
    """Mint a new generation for chat_id and make it authoritative.

    Any older generation's in-flight _stream_track() — even one already
    downloading/probing a source — loses the moment this runs: its later
    staleness checks (`_is_stale_generation`) will see it no longer matches
    and abort before touching py-tgcalls or playback state, instead of
    racing whatever THIS request starts.
    """
    gen = _gen_counter.get(chat_id, 0) + 1
    _gen_counter[chat_id] = gen
    _stream_generation[chat_id] = gen
    return gen


def _is_stale_generation(chat_id: int, gen: "int | None") -> bool:
    """True when `gen` is no longer the authoritative generation for chat_id.

    `gen is None` means the caller opted out of generation tracking (legacy/
    internal call sites) — treated as always-fresh.
    """
    return gen is not None and _stream_generation.get(chat_id) != gen


def _get_play_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _play_locks:
        _play_locks[chat_id] = asyncio.Lock()
    return _play_locks[chat_id]


def _get_stream_commit_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _stream_commit_locks:
        _stream_commit_locks[chat_id] = asyncio.Lock()
    return _stream_commit_locks[chat_id]


def _consume_task_exception(task: asyncio.Task) -> None:
    """Read a canceled/failed background task so asyncio emits no warning."""
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


# ─── Audio quality helper ─────────────────────────────────────────────────────

def _get_audio_quality():
    """Return best available AudioQuality constant for py-tgcalls 2.x."""
    from pytgcalls.types import AudioQuality
    return getattr(AudioQuality, "HIGH_QUALITY", None) or getattr(AudioQuality, "STUDIO", None)


def _get_video_quality():
    """Return the VideoQuality used for /vplay-style video streams.

    LAG FIX ("bohot jyada lag hota hai"): 720p was hardcoded. On the small
    containers this bot usually runs on, a 720p encode saturates CPU and
    uplink, which stalls the AUDIO ffmpeg too — the stutter users feel.
    480p is the default now (set VIDEO_QUALITY=720p / 360p to override).
    """
    from pytgcalls.types import VideoQuality

    wanted = (os.getenv("VIDEO_QUALITY") or "480p").strip().lower()
    table = {
        "1080p": "FHD_1080p",
        "720p": "HD_720p",
        "480p": "SD_480p",
        "360p": "SD_360p",
    }
    name = table.get(wanted, "SD_480p")
    return getattr(VideoQuality, name, None) or VideoQuality.SD_480p


# ─── Silence file (instant VC join) ──────────────────────────────────────────

# SPEED FIX: the silence pre-join was allowed 8s. If Telegram has not let us
# into the voice chat by then, waiting longer only delays the song — the real
# track's own play() call joins the call anyway.
try:
    _PREJOIN_TIMEOUT = float(os.getenv("PREJOIN_TIMEOUT", "4"))
except ValueError:
    _PREJOIN_TIMEOUT = 4.0

_SILENCE_PATH: str | None = None
_SILENCE_LOCK = asyncio.Lock()


async def _get_silence_file() -> str | None:
    """
    Create once: a 60-second PCM silence MP3.

    It used to be 4 seconds, which was shorter than a slow yt-dlp resolve —
    the placeholder ended while the real track was still downloading, so the
    bot sat in the call with a dead stream. 60s of silence covers even the
    worst-case resolve and is only a few KB on disk.
    Used to join VC instantly before yt-dlp finishes downloading.
    """
    global _SILENCE_PATH
    if _SILENCE_PATH and os.path.exists(_SILENCE_PATH):
        return _SILENCE_PATH
    async with _SILENCE_LOCK:
        if _SILENCE_PATH and os.path.exists(_SILENCE_PATH):
            return _SILENCE_PATH
        path = os.path.join(tempfile.gettempdir(), "melody_vc_silence.mp3")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t", "60",
                "-c:a", "libmp3lame", "-b:a", "64k",
                path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=15)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                _SILENCE_PATH = path
                LOGGER.info("✅ Silence file created: %s", path)
        except Exception as e:
            LOGGER.debug("Silence file creation failed: %s", e)
    return _SILENCE_PATH


# ─── Startup ─────────────────────────────────────────────────────────────────

async def start_call_py():
    """
    Initialise PyTgCalls and register the stream-end handler.
    Called once from __main__.py AFTER assistant.start().
    """
    global _pytgcalls

    from melody import assistant  # safe: create_clients() already ran
    _pytgcalls = PyTgCalls(assistant)

    # 🎙 Voice-chat join / leave / close notifications. Telegram never sends
    # participant join/leave as a service message, so this is the only place
    # they can be observed — inside the assistant's group-call updates.
    @_pytgcalls.on_update()
    async def _on_vc_event(_, update):
        try:
            from pytgcalls.types import ChatUpdate, UpdatedGroupCallParticipant
            from melody.core import vc_notify

            if isinstance(update, UpdatedGroupCallParticipant):
                await vc_notify.notify_participant(update)
            elif isinstance(update, ChatUpdate):
                await vc_notify.notify_chat_update(update)
        except Exception:
            LOGGER.debug("VC event handler failed", exc_info=True)

    @_pytgcalls.on_update()
    async def _on_stream_end(_, update):
        # BUG FIX ("autoplay on hai, gana khatam hua, kuch response nahi
        # aata, jese sab normal ho — silent error"): this handler used to
        # have NO surrounding try/except at all. py-tgcalls dispatches
        # `@_pytgcalls.on_update()` handlers as fire-and-forget tasks, so
        # any unhandled exception raised in here (e.g. `is_autoplay_on()`
        # hitting a flaky Mongo call inside `_play_next()`) was swallowed by
        # py-tgcalls' own internal dispatcher — never reaching our
        # LOG_GROUP_ID, never reaching the chat, never reaching a LOGGER
        # line anyone would see. From the user's side the song just ends
        # and nothing happens next, indistinguishable from "everything is
        # fine". Wrapping the whole body guarantees every failure on this
        # path is now surfaced instead of vanishing.
        finished_title = None
        try:
            if not isinstance(update, StreamEnded):
                return
            chat_id = getattr(update, "chat_id", None)
            if chat_id is None:
                return

            # Deliberate disconnect (/stop, /leave, end-of-queue): the leave
            # itself raises StreamEnded — never advance or autoplay on it.
            if _is_leaving(chat_id):
                LOGGER.debug("stream_end during deliberate leave for %s — ignoring", chat_id)
                return

            # 🎧 Listener mode: the 60s silence loop just ended but we are in
            # the call purely to read the in-call chat — restart it instead of
            # advancing a queue that does not exist.
            if _listener_mode.get(chat_id) and not get_current(chat_id):
                await _replay_listener_silence(chat_id)
                return

            # Silence race guard: if the silence stream ends before the real
            # song is ready, do NOT advance the queue — let _stream_track
            # handle it.
            if _silence_playing.get(chat_id):
                LOGGER.debug("stream_end during silence for %d — ignoring (real song loading)", chat_id)
                return

            # The song may not really be over: a direct CDN URL can be
            # accepted by PyTgCalls and then end at 0s, while its retained
            # download fallback is still running. Recover that case FIRST.
            # The old order classified the 0.08s event as a stale replacement
            # event and returned, leaving the VC silent with no recovery.
            if await _resume_if_premature_end(chat_id):
                return

            # StreamEnded has chat/type/device but no stream-generation id.
            # A late event from the stream replaced by a newer /play can
            # therefore arrive against the new current track and incorrectly
            # advance its queue. Only debounce clearly impossible early ends
            # after the recovery path has had a chance to handle real failures.
            if _is_probably_stale_stream_end(chat_id):
                return

            # Clean up the finished track's /tmp file (skip FIFO paths)
            current = get_current(chat_id)
            finished_title = current.title if current else None
            if current:
                _cleanup_track_file(current.video_id)

            await _play_next(chat_id)
        except Exception as exc:
            chat_id = getattr(update, "chat_id", "?")
            LOGGER.exception("‼️ _on_stream_end crashed for chat %s", chat_id)
            try:
                await send_error_log(
                    f"_on_stream_end crashed in {chat_id} (song ended, autoplay/next-up never ran)",
                    exc,
                    context={
                        "chat_id": chat_id,
                        "song_title": finished_title,
                    },
                )
            except Exception:
                pass

    await _pytgcalls.start()
    LOGGER.info("PyTgCalls (py-tgcalls 2.x) started.")

    # Pre-generate the silence file so first /play is instant
    spawn(_get_silence_file())

    # Peer warming is scheduled once by __main__.py after the bot is live.
    # Starting a second full scan here wastes RPC/CPU and competed with the
    # first interactive playback on small dynos.


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _cache_limit_bytes() -> int:
    """Disk cache ceiling with a conservative 96 MB Heroku default."""
    try:
        megabytes = int(os.getenv("SONG_CACHE_MB", "96"))
    except ValueError:
        megabytes = 96
    return max(16, min(megabytes, 256)) * 1024 * 1024


_CACHE_MAX_BYTES = _cache_limit_bytes()


# In-progress download markers. A file carrying any of these suffixes is being
# written RIGHT NOW (yt-dlp fragments, Pyrogram's `.temp`, tagged-media staging
# files). Deleting one mid-flight is what produced the reported
# `FileNotFoundError: '/tmp/melody_..._v.mkv.2.<ts>.dl.temp'` crashes, so every
# cache sweep below must skip them.
_IN_PROGRESS_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp", ".dl")


def _is_in_progress(path: str) -> bool:
    return path.endswith(_IN_PROGRESS_SUFFIXES) or ".part-" in path or ".dl." in path


def _cleanup_partial_files(video_id: str) -> None:
    """Delete every cached/partial file for a track before a clean retry.

    A zero-byte or half-written /tmp/melody_<id>_<a|v>.* file is picked up as
    a cache hit on the next attempt, so the retry would fail exactly the same
    way. Removing them guarantees the retry actually re-downloads.
    """
    if not video_id:
        return
    for f in glob.glob(f"/tmp/melody_{video_id}_?.*"):
        if _is_in_progress(f):
            continue  # a download is still writing this file — never delete it
        try:
            os.unlink(f)
        except OSError:
            pass


def _cleanup_track_file(video_id: str):
    """Keep played songs cached for instant replay, but enforce a total size
    cap so /tmp doesn't fill up on Heroku (512 MB limit).

    Instead of deleting the just-finished track immediately, this now:
      1. Leaves the current track's file in place (so a replay is instant).
      2. Walks all /tmp/melody_* cache files and evicts oldest by mtime until
         total size is under _CACHE_MAX_BYTES.
    """
    cache_files = []
    for f in glob.glob("/tmp/melody_*_?.*"):
        if _is_in_progress(f):
            continue  # never evict a file that is still downloading
        try:
            st = os.stat(f)
            cache_files.append((f, st.st_mtime, st.st_size))
        except Exception:
            pass
    # Tagged-media downloads stage in /tmp/melody_dl/, outside the glob above.
    # Abandoned staging files there would otherwise never be reclaimed.
    try:
        from melody.core.ytdl import _sweep_staging_dir

        _sweep_staging_dir()
    except Exception:
        pass
    cache_files.sort(key=lambda x: x[1])  # oldest first
    total = sum(c[2] for c in cache_files)
    for f, _, size in cache_files:
        if total <= _CACHE_MAX_BYTES:
            break
        # Never evict a track that is playing/queued somewhere else right now:
        # deleting it under a live ffmpeg reader is what produced mid-song
        # "file not found" stalls. diskguard owns that protected set.
        try:
            from utils.diskguard import _is_protected, _protected_video_ids

            if _is_protected(f, _protected_video_ids()):
                continue
        except Exception:
            pass
        try:
            os.unlink(f)
            total -= size
        except Exception:
            pass

    # Free-space safety net: the fixed SONG_CACHE_MB ceiling is blind to how
    # much room the dyno actually has left, and a full /tmp breaks the next
    # download outright.
    try:
        from utils.diskguard import free_bytes, min_free_bytes, sweep

        if free_bytes() < min_free_bytes():
            sweep(aggressive=True)
    except Exception:
        pass


_premature_resumes: dict = {}   # "chat_id:video_id" → how many times we resumed it


async def _resume_if_premature_end(chat_id: int) -> bool:
    """Handle ffmpeg hitting EOF on a file that is still downloading.

    ROOT-CAUSE FIX ("assistant VC me aata hai par gana nahi bajta", and the
    log's AutoPlay chain firing every ~13 seconds): playback starts from a
    still-growing file (that is what makes /play instant). ffmpeg does not
    wait at the end of a regular file — if the reader ever catches up with
    yt-dlp's writer, PyTgCalls reports StreamEnded even though only a few
    seconds of a 4-minute song were actually played, and the bot "advances"
    to the next track. Instead of advancing, detect that the track ended far
    too early, wait for the download to get ahead again, and resume the same
    track from where it stopped (seek_stream re-issues play() with `-ss`).

    Returns True when the track was resumed (caller must NOT advance).
    """
    track = get_current(chat_id)
    if not track:
        return False
    duration = int(getattr(track, "duration", 0) or 0)
    elapsed = get_playback_position(chat_id)
    if duration > 0:
        # Genuine end-of-song: played (nearly) the whole known duration.
        if elapsed >= duration - 8:
            return False
    else:
        # ROOT-CAUSE FIX (autoplay card spam): AutoPlay/related-video tracks
        # often carry duration=0, so this guard used to bail out instantly and
        # every premature stream end became "song finished" -> _play_next ->
        # AutoPlay -> another card, several times a minute. With no known
        # duration, treat anything under 25 seconds of playback as premature.
        if elapsed >= 25:
            return False

    key = f"{chat_id}:{track.video_id}"
    count = _premature_resumes.get(key, 0)
    if count >= 15:
        LOGGER.warning("giving up resuming %s in %s after %d premature ends", track.video_id, chat_id, count)
        return False

    video = _is_video.get(chat_id, False)
    try:
        from melody.core.ytdl import is_download_in_progress
        # Let the writer get comfortably ahead of the reader again.
        for _ in range(30):
            if not is_download_in_progress(track.video_id, audio_only=not video):
                break
            await asyncio.sleep(0.5)
    except Exception:
        pass

    _premature_resumes[key] = count + 1
    try:
        await seek_stream(
            chat_id, max(elapsed - 1, 0),
            prefer_local=True,
        )
        LOGGER.info(
            "▶️ resumed %s in %s at %ss (stream ended early — file was still downloading)",
            track.video_id, chat_id, elapsed,
        )
        return True
    except Exception as exc:
        LOGGER.debug("premature-end resume failed for %s: %s", chat_id, exc)
        return False


async def _play_next(chat_id: int):
    """
    Advance queue or handle autoplay / stop.

    BUG FIX ("autoplay on kiya phir bhi bot VC left kar diya"): this used to
    unconditionally leave the call FIRST and only THEN check AutoPlay —
    so even with AutoPlay on, the bot visibly dropped out of the voice chat
    before (maybe) trying to rejoin, and `try_autoplay()`'s own `is_active`
    guard (see autoplay.py) made that rejoin silently no-op most of the
    time. Now: if the manual queue is empty, try AutoPlay WHILE still
    connected — the bot never leaves the call at all when AutoPlay has a
    track to play. Only leave if there really is nothing left to play.
    """
    from melody.core.autoplay import try_autoplay

    # IDEMPOTENCY FIX (duplicate/restarted playback): if a manual /play or
    # force-play for this exact chat is already resolving a stream, do not
    # start a second, competing _stream_track() here — that double-commit is
    # what produced a track (or a video) playing twice.
    lock = _get_play_lock(chat_id)
    async with lock:
        if chat_id in _resolving:
            LOGGER.debug("_play_next skipped for %s — a play request is already resolving", chat_id)
            return
        next_track = pop_next(chat_id)
        gen = None
        if next_track:
            # This track is now actually playing — drop any stale "upcoming"
            # prefetch guard for it so a genuinely different next-in-queue
            # track can be pre-downloaded fresh (see _prefetch_upcoming()).
            inflight = _prefetch_inflight.get(chat_id)
            if inflight is not None:
                inflight.discard(next_track.video_id)
                if not inflight:
                    _prefetch_inflight.pop(chat_id, None)
            gen = _begin_generation(chat_id)
            _resolving[chat_id] = gen
            _resolving_track[chat_id] = (next_track.video_id, bool(next_track.video))
            _resolving_origin[chat_id] = "queue"

    if next_track:
        try:
            # BUG FIX ("kabhi vplay kabhi play"): this used to call
            # _stream_track with no `video` argument at all, so it always
            # defaulted to False — a queued /vplay track silently turned into
            # audio-only the moment it advanced from the queue. Track now
            # carries its own video intent (see queue.Track), so replay it
            # exactly as it was requested.
            await _stream_track(
                chat_id, next_track, video=next_track.video, gen=gen, priority=0,
            )
        finally:
            if _resolving.get(chat_id) == gen:
                _resolving.pop(chat_id, None)
                _resolving_track.pop(chat_id, None)
                _resolving_origin.pop(chat_id, None)
        return

    _prefetch_inflight.pop(chat_id, None)
    if await is_autoplay_on(chat_id) and await try_autoplay(chat_id):
        return

    _mark_leaving(chat_id)
    # _forget_call_state clears VC flags but not queue._current. Clear both so
    # /np, /queue, and persisted recovery state do not retain a finished song.
    from melody.core.queue import clear_queue
    clear_queue(chat_id)
    _forget_call_state(chat_id)
    from utils.playback_state import mark_inactive
    await mark_inactive(chat_id)
    await _hard_leave(chat_id)


async def _prefetch_upcoming(chat_id: int) -> None:
    """Warm the next queued tracks without blocking the active playback path.

    Prefetch is enabled by default because it runs through the low-priority
    download gate; interactive playback always outranks and can preempt it.
    Set PREFETCH_ENABLED=false to disable it on very small dynos.
    """
    if os.getenv("PREFETCH_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    from melody.core.ytdl import (
        cached_file_path, download_audio, is_download_cancelled,
        on_cloud_host, resolve_stream_urls, should_try_direct_stream,
    )
    from melody.core.autoplay import _cloud_prefetch_enabled

    try:
        memory_mb = int(os.getenv("MEMORY_LIMIT_MB", "512"))
    except ValueError:
        memory_mb = 512
    try:
        requested = max(1, int(os.getenv("PREFETCH_WORKERS", "2")))
    except ValueError:
        requested = 2
    prefetch_workers = min(2, requested) if memory_mb >= 1024 else 1

    queue = get_queue(chat_id)
    if queue:
        inflight = _prefetch_inflight.setdefault(chat_id, set())
        candidates = [
            upcoming for upcoming in list(queue[:3])
            if upcoming.video_id not in inflight
            and not cached_file_path(upcoming.video_id, audio_only=not upcoming.video)
        ]
        candidates = candidates[:prefetch_workers]

        async def _warm(upcoming, rank: int) -> None:
            inflight.add(upcoming.video_id)
            try:
                if should_try_direct_stream() and not on_cloud_host():
                    try:
                        await resolve_stream_urls(
                            upcoming.video_id, want_video=upcoming.video,
                        )
                    except Exception:
                        pass
                if on_cloud_host() and not _cloud_prefetch_enabled():
                    LOGGER.info(
                        "prefetch: cloud download skipped for %s to protect interactive playback",
                        upcoming.video_id,
                    )
                    return
                path = await download_audio(
                    upcoming.video_id,
                    audio_only=not upcoming.video,
                    priority=20 + (rank * 15),
                    owner=chat_id,
                )
                if path:
                    LOGGER.info(
                        "prefetch: cached queue item %s (%s)",
                        upcoming.video_id, upcoming.title[:40],
                    )
            except Exception as exc:
                if is_download_cancelled(exc):
                    LOGGER.info(
                        "prefetch stopped for %s after interactive priority request",
                        chat_id,
                    )
                    return
                LOGGER.info(
                    "prefetch of queued track failed in %s (%s): %s",
                    chat_id, upcoming.video_id, type(exc).__name__,
                )
            finally:
                inflight.discard(upcoming.video_id)

        await asyncio.gather(
            *(_warm(track, rank) for rank, track in enumerate(candidates)),
            return_exceptions=True,
        )
        if not inflight:
            _prefetch_inflight.pop(chat_id, None)
        return
    if await is_autoplay_on(chat_id):
        from melody.core.autoplay import prefetch_next
        await prefetch_next(chat_id)

# ROOT-CAUSE FIX ("VC me aata hai par gana nahi bajta" + "0:00 par track end"
# + AutoPlay card spam): these -reconnect* flags are AVOptions of ffmpeg's
# HTTP protocol ONLY. Passing them as input options for a LOCAL FILE makes
# ffmpeg abort immediately with:
#     Option reconnect not found.
#     Error opening input file /tmp/melody_<id>_a.webm
# ffmpeg exits before a single audio frame is produced, so py-tgcalls fires
# StreamEnded at 0s -> _resume_if_premature_end resumes at 0s (up to 15x,
# exactly what the Heroku log shows) -> "giving up resuming" -> _play_next ->
# AutoPlay -> new card -> repeat. Since FIFO/CDN streaming was removed and
# every track (plus the silence pre-join file) is now a real local file,
# these flags are always wrong. They are only added back when the input is
# actually an http(s) URL.
# `-rw_timeout` (microseconds) is what stops a blocked CDN from hanging
# ffmpeg forever — without it a dead googlevideo.com URL stalls the whole play.
_HTTP_RECONNECT_FFMPEG = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 2 -rw_timeout 5000000"
)


def _is_remote_source(path: str | None) -> bool:
    """True only for http(s) inputs, where -reconnect* is valid."""
    return bool(path) and str(path).lower().startswith(("http://", "https://"))


def _is_local_media_proxy(path: str | None) -> bool:
    return bool(path) and str(path).lower().startswith(
        ("http://127.0.0.1:", "http://localhost:")
    )


async def _build_direct_stream(chat_id: int, track, video: bool, seconds: int = 0,
                               force: bool = False):
    """Build a MediaStream that plays straight off the CDN — nothing downloaded.

    ROOT-CAUSE FIX ("/vplay me audio aur video miss match ho rahi hai" +
    "pura video download mt Krna direct play Krna" + "play/vplay krte hi
    direct VC aake gana baje"):

    The old path always went through download_audio(). For /vplay that meant
    PyTgCalls got a still-growing `.part` file, and a video MediaStream opens
    that path TWICE — once for the camera ffmpeg, once for the microphone
    ffmpeg. Each process opened the file at a different length (and, on a DASH
    fallback, before yt-dlp had muxed the audio track in at all), so the two
    tracks started from different effective positions and drifted apart for the
    rest of the song. That is the A/V mismatch.

    Streaming the CDN URLs instead gives each ffmpeg process a complete,
    immutable, seekable source that starts at t=0, so both tracks are aligned
    from the first frame — and because only metadata is fetched (~1s) rather
    than a 720p file, the bot joins the call and starts playing immediately.

    Returns None when the track has no CDN source (tagged Telegram media) or
    resolution fails, so the caller can fall back to the download path.
    """
    try:
        from melody.core.ytdl import resolve_stream_urls

        proxy_url = getattr(track, "stream_url", "") or ""
        if proxy_url.startswith(("http://127.0.0.1:", "http://localhost:")):
            # Tagged large media is exposed through the bounded local range
            # proxy. PyTgCalls/ffmpeg consumes Telegram chunks on demand, so
            # no full 3GB file is buffered or written to /tmp.
            urls = {
                "audio": proxy_url,
                "video": proxy_url if video else None,
                "headers": {},
            }
        else:
            urls = await resolve_stream_urls(
                track.video_id, want_video=video, force=force,
            )
    except Exception as exc:
        # Resolver-level failures (403, empty formats, expired profile, or a
        # stale signed URL) happen before PyTgCalls can perform its probe. Give
        # the provider/client ladder one fresh attempt as well; otherwise the
        # first failed profile immediately forces a 30-40s download even when a
        # second profile would provide a playable URL in under 5s. The force
        # flag prevents recursion and retry storms.
        if not force and not str(getattr(track, "stream_url", "") or "").lower().startswith(
            ("http://127.0.0.1:", "http://localhost:")
        ):
            try:
                if os.getenv('DISABLE_DIRECT_STREAM', '0') == '1':
                    raise ValueError('FastPlay: skip direct stream')
                return await _build_direct_stream(
                    chat_id, track, video, seconds, force=True,
                )
            except Exception as retry_exc:
                LOGGER.info(
                    "#stream fresh resolver retry failed for %s (%s)",
                    getattr(track, "video_id", "?"), type(retry_exc).__name__,
                )
        LOGGER.info(
            "#stream direct-stream unavailable for %s (%s: %r) — falling back to download",
            getattr(track, "video_id", "?"), type(exc).__name__, exc,
        )
        return None

    audio_url = urls.get("audio")
    video_url = urls.get("video")
    if not audio_url or (video and not video_url):
        return None

    # `-ss` (used by /seek) is applied to both ffmpeg inputs by _ffmpeg_params,
    # so a seek keeps the two tracks aligned exactly like playback from 0 does.
    # include_reconnect=False: py-tgcalls' own build_command() already injects
    # the full -reconnect* set for http(s) inputs, so adding ours would emit
    # each flag twice on the same ffmpeg command line.
    ffmpeg_params = (
        _ffmpeg_params(chat_id, seconds, source=audio_url, include_reconnect=False) or None
    )
    headers = urls.get("headers") or None

    if video:
        return MediaStream(
            video_url,
            audio_parameters=_get_audio_quality(),
            video_parameters=_get_video_quality(),
            # Separate audio source: for a muxed format this is the same URL and
            # ffmpeg just picks the audio track out of it; for a DASH pair it is
            # the audio-only stream. Either way the microphone ffmpeg never
            # depends on the video download finishing.
            audio_path=audio_url,
            headers=headers,
            ffmpeg_parameters=ffmpeg_params,
        )

    return MediaStream(
        audio_url,
        audio_parameters=_get_audio_quality(),
        # Explicit audio_path: without it PyTgCalls only derives the microphone
        # input from media_path inside check_stream(), leaving the ffmpeg command
        # inputless if anything plays the stream before that runs.
        audio_path=audio_url,
        # Audio-only request: never let PyTgCalls auto-detect a camera track
        # off a muxed URL, or /play would silently broadcast video.
        video_flags=MediaStream.Flags.IGNORE,
        headers=headers,
        ffmpeg_parameters=ffmpeg_params,
    )


def get_speed(chat_id: int) -> float:
    return float(_speed.get(chat_id, 1.0))


def _ffmpeg_params(
    chat_id: int,
    seconds: int = 0,
    source: str | None = None,
    include_reconnect: bool = True,
) -> str:
    """Build the ffmpeg parameter string for a MediaStream.

    py-tgcalls splits this string into `--audio` / `--video` sections and
    into `-atstart` / `-atmid` / `-atend` slots (see pytgcalls/ffmpeg.py),
    which is what makes real seeking AND real speed control possible:

    * `-ss <sec>`  — input-side seek, applies to both sections.
    * `-af atempo=<x>` — post-input audio filter (audio process only);
      ffmpeg's atempo accepts 0.5–2.0, exactly the range /speed allows.
    * `-itsscale <1/x>` — input timestamp scaling for the video process,
      because its `-vf` slot is already taken by py-tgcalls' own scale
      filter and a second `-vf` would silently override it.
    """
    parts = []
    if include_reconnect and _is_remote_source(source):
        parts.append(_HTTP_RECONNECT_FFMPEG)
    if seconds > 0:
        parts.append(f"-ss {seconds}")
    speed = get_speed(chat_id)
    if abs(speed - 1.0) > 0.001:
        parts.append(f"--audio -atmid -af atempo={speed:.3f}")
        parts.append(f"--video -itsscale {1 / speed:.6f}")
    return " ".join(parts)


def _local_media_stream(chat_id: int, filepath: str, video: bool, seconds: int = 0):
    """MediaStream for an already-downloaded local file."""
    audio_quality = _get_audio_quality()
    if video:
        return MediaStream(
            filepath,
            audio_parameters=audio_quality,
            video_parameters=_get_video_quality(),
            ffmpeg_parameters=_ffmpeg_params(chat_id, seconds, source=filepath) or None,
        )
    return MediaStream(
        filepath,
        audio_parameters=audio_quality,
        # ROOT-CAUSE FIX from the uploaded Heroku log:
        #   "ffprobe check_stream failed (NoVideoSourceFound: No video source
        #    found on /tmp/melody_<id>_a.webm) attempt 1/2"  (x16 per track)
        #   -> "playing without probe" -> stream ends at 0s
        #   -> "resumed <id> at 0s (stream ended early)"  (x15)
        # An audio-only download has no camera track, but this MediaStream did
        # not say so, so PyTgCalls probed for video, failed twice (with the
        # ffprobe timeout each time), and then negotiated a stream whose video
        # side dies instantly — which is the constant restart/stutter that felt
        # like "bohot jyada lag". Declaring the file audio-only removes the
        # failed probes AND the premature stream ends.
        video_flags=MediaStream.Flags.IGNORE,
        ffmpeg_parameters=_ffmpeg_params(chat_id, seconds, source=filepath) or None,
    )



# ROOT-CAUSE FIX (⚠️ Melody Error Log: "_stream_track failed" ->
# json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
# ... during handling ... ProcessLookupError in ffmpeg.py check_stream):
#
# Before py-tgcalls plays anything it runs `ffprobe` on the source to detect
# which audio/video streams exist. When we hand it a *direct CDN URL* that
# ffprobe cannot open (expired googlevideo link, 403 from YouTube, region
# block, throttled/hung range request, ffprobe timeout), ffprobe writes
# NOTHING to stdout -> `loads("")` raises JSONDecodeError. py-tgcalls' own
# `except` handler then calls `ffprobe.terminate()` on a process that has
# ALREADY exited, which raises ProcessLookupError on uvloop and completely
# masks the real cause. Either way `play()` explodes and the song never
# starts, even though the very same track plays fine from a local file.
#
# Fix: treat any probe failure as "this CDN URL is unusable" instead of a
# fatal error — fall back to the download path (which produces a complete,
# always-probeable local file) and play that. Only if THAT also fails does
# the error bubble up to the reporting/notification handler below.
from utils.pytgcalls_patch import (
    apply_pytgcalls_probe_patch,
)
from utils.tasks import spawn

# Install the ffprobe hardening patch before any stream is ever built (see
# utils/pytgcalls_patch.py for the full root-cause write-up).
apply_pytgcalls_probe_patch()

_PROBE_ERROR_NAMES = (
    "StreamProbeUnavailable",
    "JSONDecodeError",
    "ProcessLookupError",
    "TimeoutError",
    "NoAudioSourceFound",
    "NoVideoSourceFound",
    "InvalidMediaPath",
    "FFmpegNotInstalled",
)


def _is_probe_error(exc: BaseException) -> bool:
    """True when `exc` came from py-tgcalls' ffprobe `check_stream()` step."""
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ in _PROBE_ERROR_NAMES:
            return True
        text = str(cur)
        if "check_stream" in text or "ffprobe" in text.lower():
            return True
        cur = cur.__cause__ or cur.__context__
    return False


_UNAVAILABLE_MARKERS = (
    "video unavailable",
    "video is unavailable",
    "error code 152",
    "removed by the uploader",
    "private video",
    "this video is not available",
    "video has been removed",
    "account associated with this video has been terminated",
    "sign in to confirm your age",
    "who has blocked it in your country",
    "is not available in your country",
    # yt-dlp emits ASCII or curly-apostrophe variants depending on the
    # YouTube client; both must enter alternate-candidate recovery.
    "this content isn't available",
    "this content isn’t available",
    "members-only content",
    "this live event has ended",
    "requested format is not available",
)


def _is_unavailable_media_error(exc: BaseException) -> bool:
    """True when the failure is 'this YouTube video cannot be played', not a bug."""
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 8:
        text = str(cur).lower()
        if any(marker in text for marker in _UNAVAILABLE_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


# ─── Assistant peer cache warm-up ────────────────────────────────────────────
# ROOT-CAUSE FIX (KeyError: 'ID not found: -100…' raised from
# pyrogram/storage/sqlite_storage.py inside pytgcalls' create_group_call):
# the ASSISTANT account joins the call, and MTProto can only address a chat
# whose peer (id + access_hash) is present in that account's own session
# storage. A freshly restarted / freshly generated session has an EMPTY peer
# table, so the very first /play in a chat blows up before any audio work
# happens. Priming the peer once — cheap `get_chat()` over the assistant —
# writes the access_hash into its storage and every later call resolves from
# cache instantly (this also shaves a full RPC round-trip off /play).
_peer_ready: set = set()

# Chats whose cached access_hash has been PROVEN good with a real API call in
# this process. See ensure_assistant_peer() for why "present in storage" is
# not the same as "valid".
_peer_verified: set = set()

# ─── Auto-join back-off ──────────────────────────────────────────────────────
# chat_id -> monotonic time until which auto-join/auto-unban must not be retried.
_join_block: dict = {}
_join_warned: set = set()
_JOIN_BLOCK_FOR = 900.0  # 15 min


def _join_blocked(chat_id: int) -> bool:
    import time as _time

    return _time.monotonic() < _join_block.get(chat_id, 0.0)


def _block_join(chat_id: int) -> None:
    import time as _time

    _join_block[chat_id] = _time.monotonic() + _JOIN_BLOCK_FOR


def _join_log_level(chat_id: int) -> int:
    """Warn once per chat, then keep the repeats at debug level."""
    import logging as _logging

    if chat_id in _join_warned:
        return _logging.DEBUG
    _join_warned.add(chat_id)
    return _logging.WARNING


def _join_peek_level(chat_id: int) -> int:
    """Like _join_log_level() but does not consume the one-time warn slot."""
    import logging as _logging

    return _logging.DEBUG if chat_id in _join_warned else _logging.INFO


_peer_inflight: dict[int, asyncio.Task] = {}


async def ensure_assistant_peer(chat_id: int) -> bool:
    """Share one peer-validation task per chat across concurrent playback paths."""
    existing = _peer_inflight.get(chat_id)
    if existing is not None and not existing.done():
        return bool(await asyncio.shield(existing))

    task = asyncio.create_task(_ensure_assistant_peer_impl(chat_id))
    _peer_inflight[chat_id] = task
    try:
        return bool(await asyncio.shield(task))
    finally:
        if _peer_inflight.get(chat_id) is task and task.done():
            _peer_inflight.pop(chat_id, None)


async def _ensure_assistant_peer_impl(chat_id: int) -> bool:
    """Make sure the assistant account can resolve `chat_id`. Never raises.

    ROOT-CAUSE FIX (KeyError: 'ID not found: -100…' still crashing playback):
    `_peer_ready` used to be treated as proof, but entries were also added by
    `warm_assistant_peers()` WITHOUT ever resolving the peer — so a chat could
    be marked "ready" while the assistant's session storage held nothing for
    it, and pytgcalls' create_group_call() then blew up with exactly that
    KeyError. The cached fast-path now still does a storage-only
    `resolve_peer()` (no network round-trip when the peer is present) and
    self-heals by falling through to the full warm-up when it is not.
    """
    try:
        from melody import assistant
    except Exception:
        return False
    if assistant is None:
        return False

    try:
        peer = await assistant.resolve_peer(chat_id)
        if chat_id in _peer_verified:
            _peer_ready.add(chat_id)
            return True
        # The row exists but has never been proven this process. Validate it
        # with one cheap API call: a stale access_hash answers CHANNEL_INVALID
        # here instead of inside pytgcalls' create_group_call(), where it
        # surfaced as an uncatchable crash mid-playback.
        try:
            await assistant.get_chat(chat_id)
            _peer_verified.add(chat_id)
            _peer_ready.add(chat_id)
            return True
        except (ChannelInvalid, ChannelPrivate, PeerIdInvalid) as exc:
            LOGGER.warning(
                "assistant peer for %s is stale (%s) — purging and re-learning it",
                chat_id, type(exc).__name__,
            )
            purge_assistant_peer_storage(chat_id)
            _peer_ready.discard(chat_id)
        except Exception:
            # Network hiccup / flood wait: keep the peer, don't punish it.
            _peer_ready.add(chat_id)
            return True
        del peer
    except Exception:
        _peer_ready.discard(chat_id)


    # Not in storage (or the cached hash is stale) — fetch it once.
    try:
        await assistant.get_chat(chat_id)
        await assistant.resolve_peer(chat_id)
        _peer_ready.add(chat_id)
        _peer_verified.add(chat_id)
        return True
    except Exception as exc:
        LOGGER.debug("assistant peer warm-up failed for %s: %s", chat_id, exc)

    # ROOT FIX (log: KeyError 'ID not found: -100…' -> CHANNEL_INVALID from
    # channels.GetChannels): `get_chat(<numeric id>)` can NEVER work on an
    # account whose session has never seen that channel — Telegram needs the
    # access_hash we are trying to learn. The only ways in are (a) a dialog
    # scan, (b) an invite link / public username, or (c) actually joining.
    # Try all three before declaring failure.
    if await _peer_from_dialogs(chat_id):
        _peer_ready.add(chat_id)
        _peer_verified.add(chat_id)
        return True

    if await _peer_from_invite_link(chat_id):
        _peer_ready.add(chat_id)
        _peer_verified.add(chat_id)
        return True

    # Last resort: the assistant is simply not in the chat yet.
    try:
        if await _auto_join_assistant(chat_id):
            try:
                await assistant.resolve_peer(chat_id)
            except Exception:
                await assistant.get_chat(chat_id)
                await assistant.resolve_peer(chat_id)
            _peer_ready.add(chat_id)
            _peer_verified.add(chat_id)
            return True
    except Exception as exc:
        LOGGER.debug("assistant peer join-warm-up failed for %s: %s", chat_id, exc)
    return False


async def _peer_from_dialogs(chat_id: int) -> bool:
    """The assistant IS in the chat but its fresh session storage has no
    access_hash for it — walking the dialog list writes every peer it is a
    member of straight into that storage, which is exactly what
    channels.GetChannels was missing."""
    try:
        from melody import assistant
        async for dialog in assistant.get_dialogs(limit=400):
            if getattr(dialog.chat, "id", None) == chat_id:
                await assistant.resolve_peer(chat_id)
                LOGGER.debug("✅ assistant peer for %s recovered from dialogs", chat_id)
                return True
    except Exception as exc:
        LOGGER.debug("dialog peer scan failed for %s: %s", chat_id, exc)
    return False


async def _peer_from_invite_link(chat_id: int) -> bool:
    """Learn the peer through the chat's public username or an invite link
    exported by the BOT (which can always resolve the chat)."""
    try:
        from melody import bot, assistant
    except Exception:
        return False
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as exc:
        LOGGER.debug("bot could not read chat %s: %s", chat_id, exc)
        return False

    username = getattr(chat, "username", None)
    if username:
        try:
            await assistant.get_chat(username)
            await assistant.resolve_peer(chat_id)
            LOGGER.debug("✅ assistant peer for %s resolved via @%s", chat_id, username)
            return True
        except Exception as exc:
            LOGGER.debug("assistant could not resolve @%s: %s", username, exc)

    link = getattr(chat, "invite_link", None)
    if not link:
        try:
            link = await bot.export_chat_invite_link(chat_id)
        except Exception as exc:
            LOGGER.debug("bot could not export invite link for %s: %s", chat_id, exc)
            return False
    try:
        # Reading the invite (no join) already teaches the session the peer
        # when the assistant is a member; if it is not, join through it.
        await assistant.get_chat(link)
        await assistant.resolve_peer(chat_id)
        LOGGER.debug("✅ assistant peer for %s resolved via invite link", chat_id)
        return True
    except Exception as exc:
        LOGGER.debug("invite-link peer resolve failed for %s: %s", chat_id, exc)
    return False


def forget_assistant_peer(chat_id: int) -> None:
    _peer_ready.discard(chat_id)
    _peer_verified.discard(chat_id)


def purge_assistant_peer_storage(chat_id: int) -> bool:
    """Delete the assistant's cached peer row for `chat_id`.

    ROOT-CAUSE FIX (the reported crash: `KeyError: 'ID not found:
    -1003747372364'` followed by `[400 CHANNEL_INVALID] … channels.GetChannels`):
    a row in the assistant's session storage is NOT proof that the peer is
    usable. When a group is migrated/recreated, or the row was written by a
    different session, the stored `access_hash` is stale — `resolve_peer()`
    then succeeds *from cache* and Telegram rejects the very next call with
    CHANNEL_INVALID. Re-resolving cannot help while the bad row keeps winning,
    so it has to be deleted before the peer is re-learned from dialogs / the
    invite link / a fresh join.
    """
    try:
        from melody import assistant

        storage = getattr(assistant, "storage", None)
        conn = getattr(storage, "conn", None)
        if conn is None:
            return False
        with conn:
            conn.execute("DELETE FROM peers WHERE id = ?", (chat_id,))
        LOGGER.debug("🧹 purged stale assistant peer row for %s", chat_id)
        return True
    except Exception as exc:  # noqa: BLE001 — never let cleanup break playback
        LOGGER.debug("could not purge assistant peer row for %s: %s", chat_id, exc)
        return False


async def warm_assistant_peers(limit: int = 200) -> int:
    """Walk the assistant's dialogs once at startup so its session storage
    holds the access_hash of every chat it is already in. Without this, the
    first /play after every restart hits the `ID not found` path above."""
    try:
        from melody import assistant
    except Exception:
        return 0
    if assistant is None:
        return 0
    count = 0
    try:
        async for dialog in assistant.get_dialogs(limit=limit):
            chat = getattr(dialog, "chat", None)
            if chat is None or not getattr(chat, "id", None):
                continue
            # Only mark a chat ready when the peer REALLY landed in the
            # assistant's session storage — a blind add here is what made
            # ensure_assistant_peer() skip its warm-up and let pytgcalls die
            # with KeyError('ID not found: -100…').
            try:
                await assistant.resolve_peer(chat.id)
            except Exception:
                continue
            _peer_ready.add(chat.id)
            count += 1

    except Exception as exc:
        LOGGER.debug("assistant dialog warm-up failed: %s", exc)
    if count:
        LOGGER.debug("✅ Assistant peer cache warmed for %d chats", count)
    return count


async def _stream_track(chat_id: int, track, video: bool = False, _retry: bool = False,
                        start_at: int = 0, _dl_retry: bool = False,
                        gen: "int | None" = None, priority: int = 0):
    """
    Download (or pipe-stream) a track and start/swap into the active VC.

    If the bot joined VC early with silence (_active[chat_id] is already True),
    this calls change_stream() which is near-instant — the user hears music
    within ~100 ms of the download path returning.

    `_retry` is internal-only: set when this call is a single automatic
    retry after `_auto_join_assistant()` successfully joined the assistant
    to the chat, so a second failure doesn't loop forever.
    """
    startup_deadline = time.monotonic() + _PLAY_START_BUDGET

    # A fresh manual/queued track must never inherit a previous track's seek
    # position. Recovery is the only path allowed to pass a nonzero start_at.
    if _resolving_origin.get(chat_id) in {"manual", "queue", "autoplay"} and not _retry:
        if start_at:
            LOGGER.debug(
                "#stream reset stale start offset chat=%s video=%s offset=%s",
                chat_id, getattr(track, "video_id", "?"), start_at,
            )
        start_at = 0

    # If the optimistic pre-join already proved that Telegram will not let the
    # assistant create a call, do not download/probe the song before failing.
    if _vc_admin_blocked(chat_id) and not _active.get(chat_id):
        if _vc_admin_notice_needed(chat_id):
            _mark_vc_admin_notified(chat_id)
            await _notify_playback_failed(chat_id, VC_ADMIN_REQUIRED_MESSAGE)
        return False

    try:
        from melody.core.ytdl import download_audio

        # SPEED ROOT FIX: direct URL resolution and the local-file fallback
        # used to run serially. A failed resolver consumed its full timeout
        # before download even started (10s + download = the logged 15-20s).
        # Race both sources instead. Cached/replayed files now win immediately;
        # on a cold play either the fast CDN URL or the first completed file
        # wins, while the other task is retained as a cache warmer.
        from melody.core.ytdl import (
            cached_file_path,
            is_direct_url,
            is_tg_media_id,
            should_try_direct_stream,
            direct_stream_muted,
            is_download_inflight,
            wait_for_download,
            wait_for_early_download,
        )

        # /live, /radio and /m3u8 hand us a raw http(s) source. There is
        # nothing to download (a live HLS endpoint never "finishes"), so the
        # local-file fallback must not be raced against the direct path.
        live_source = is_direct_url(track.video_id) or is_direct_url(
            getattr(track, "stream_url", "")
        )

        stream = None
        filepath = None
        local_proxy_source = _is_local_media_proxy(
            getattr(track, "stream_url", "") or ""
        )
        direct_only_video = bool(
            video
            and not local_proxy_source
            and int(getattr(track, "duration", 0) or 0) >= _VIDEO_DIRECT_ONLY_SECONDS
        )
        source_errors = []
        direct_task = None
        download_task = None
        early_task = None
        download_cancelled = False
        early_file = False
        pending: set = set()

        # ⚡ FAST PATH 1 — already on disk (replay / prefetched / warmed).
        # Zero network calls, zero probing: play it right now.
        cached = cached_file_path(track.video_id, audio_only=not video)
        if cached:
            filepath = cached
            stream = _local_media_stream(chat_id, filepath, video, start_at)
            LOGGER.info("#stream cache-hit %s → instant play", track.video_id)
        else:
            # A prefetch may already be downloading this exact variant. Reuse
            # that future; download_audio() deduplicates it and the later
            # wait_for_download() path never exposes its partial file.
            if is_download_inflight(track.video_id, audio_only=not video):
                LOGGER.debug("#stream reusing in-flight download %s", track.video_id)
            # ⚡ FAST PATH 2 — race direct CDN against a local download.
            # Uploaded Aug 25 logs show the cloud host *can* resolve direct
            # audio URLs quickly, while the local fallback was probing .part
            # files and taking 9-21s. Do not disable the only instant path just
            # because DYNO/RAILWAY/RENDER is present. Race direct CDN everywhere
            # (unless DIRECT_STREAM=false); if the CDN is blocked, play() is
            # bounded and the local download keeps progressing in parallel.
            # Tagged Telegram media (synthetic "tg<chat>_<msg>" id) has no CDN
            # URL — racing the direct path only wastes the resolver timeout.
            direct_first = live_source or (
                should_try_direct_stream()
                and not direct_stream_muted()
                and not is_tg_media_id(track.video_id)
            )
            if direct_first:
                direct_task = asyncio.create_task(
                    _build_direct_stream(chat_id, track, video, start_at)
                )
                pending.add(direct_task)

            # SPEED JUGAAD: the full download used to start at the exact same
            # instant as the direct-CDN resolve. On a small dyno that download
            # eats the CPU and the bandwidth the resolve needs, so the "fast"
            # path was being slowed down by its own fallback. Give the direct
            # resolve a short exclusive window first; the download still starts
            # automatically right after, so nothing is lost when the CDN path
            # fails — only the wasted contention is gone.
            async def _delayed_download(delay: float):
                nonlocal download_cancelled
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    filepath = await download_audio(
                        track.video_id, audio_only=not video, priority=priority,
                        owner=chat_id, allow_early=not video,
                    )
                    # Archiving is scheduled by the race owner below so the
                    # same download cannot be uploaded twice when direct and
                    # fallback tasks finish in the same event-loop tick.
                    return filepath
                except Exception as exc:
                    from melody.core.ytdl import is_download_cancelled
                    if is_download_cancelled(exc):
                        download_cancelled = True
                        LOGGER.info(
                            "#download intentional cancellation chat=%s video=%s",
                            chat_id, track.video_id,
                        )
                        return None
                    raise

            async def _archive_download_task(task):
                try:
                    archived_path = await asyncio.shield(task)
                    if archived_path and archived_path.endswith(".early"):
                        archived_path = await wait_for_download(
                            track.video_id, audio_only=not video
                        )
                    if archived_path and not archived_path.endswith(".early"):
                        await _persist_completed_song(archived_path, track)
                except Exception as exc:
                    LOGGER.debug("fallback archive skipped for %s: %s", track.video_id, exc)

            early_task = None
            if not live_source and not direct_only_video:
                download_task = asyncio.create_task(
                    _delayed_download(_DOWNLOAD_START_DELAY if direct_first else 0.0)
                )
                pending.add(download_task)

                # ⚡ 5-SECOND RULE: do not wait for the whole download (8-9s in
                # the Heroku logs) or for the direct resolver to give up. An
                # audio-only download exposes a playable WebM/Opus prefix after
                # a few hundred KB, so race that prefix as a first-class source.
                # Whichever source is ready first wins the race and the song
                # starts; the download keeps running to completion behind it.
                if not video:
                    async def _await_early_prefix():
                        try:
                            return await wait_for_early_download(
                                track.video_id,
                                audio_only=True,
                                timeout=max(
                                    0.5, startup_deadline - time.monotonic()
                                ),
                            )
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            return None
                        except Exception:  # noqa: BLE001 - never fail the race
                            return None

                    early_task = asyncio.create_task(_await_early_prefix())
                    pending.add(early_task)
        async def _finish_started_fallback():
            """Let an already-started fallback finish after the race budget.

            The short source budget only decides when to stop waiting for the
            direct resolver. Cancelling the download at that exact point made a
            slow-but-valid track fail with `playback source startup budget
            exceeded`, even though its fallback was already progressing.
            """
            if download_task is None:
                return None
            if download_task.done():
                try:
                    return download_task.result()
                except Exception as exc:
                    source_errors.append(exc)
                    return None
            try:
                # Audio downloads can expose a safe WebM/Opus prefix long
                # before yt-dlp finishes post-processing. Prefer that path so
                # a slow extractor never turns an otherwise playable request
                # into the old "fallback startup budget expired" crash.
                try:
                    early_path = await wait_for_early_download(
                        track.video_id,
                        audio_only=not video,
                        timeout=min(float(os.getenv("FASTPLAY_MAX_WAIT", "1.5")), _PLAY_FALLBACK_TIMEOUT),
                    )
                except asyncio.TimeoutError:
                    # The prefix threshold is optional; a normal completed
                    # file may still arrive within the wider fallback grace.
                    early_path = None
                if early_path:
                    return early_path
                return await asyncio.wait_for(
                    asyncio.shield(download_task),
                    timeout=_PLAY_FALLBACK_TIMEOUT,
                )
            except asyncio.TimeoutError:
                LOGGER.warning(
                    "fallback source exceeded grace window for %s in %s",
                    track.video_id, chat_id,
                )
                raise asyncio.TimeoutError(
                    "fallback download exceeded startup budget; startup grace window expired"
                )

        while pending and stream is None:
            remaining = startup_deadline - time.monotonic()
            if remaining <= 0:
                fallback_path = await _finish_started_fallback()
                if fallback_path:
                    filepath = fallback_path
                    early_file = bool(filepath.endswith(".early"))
                    stream = _local_media_stream(chat_id, filepath, video, start_at)
                    spawn(_archive_download_task(download_task), name=f"archive:{track.video_id}")
                    break
                for task in pending:
                    if task is not download_task:
                        task.cancel()
                raise asyncio.TimeoutError("playback source startup budget exceeded")
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED, timeout=remaining
            )
            if not done:
                fallback_path = await _finish_started_fallback()
                if fallback_path:
                    filepath = fallback_path
                    early_file = bool(filepath.endswith(".early"))
                    stream = _local_media_stream(chat_id, filepath, video, start_at)
                    spawn(_archive_download_task(download_task), name=f"archive:{track.video_id}")
                    break
                for task in pending:
                    if task is not download_task:
                        task.cancel()
                raise asyncio.TimeoutError("playback source startup budget exceeded")
            for task in done:
                try:
                    result = task.result()
                except Exception as source_exc:
                    source_errors.append(source_exc)
                    continue
                if task is direct_task and result is not None:
                    stream = result
                    break
                if task is early_task and result:
                    filepath = result
                    early_file = bool(filepath.endswith(".early"))
                    LOGGER.info(
                        "⚡ #stream early-prefix handoff %s (instant start)",
                        track.video_id,
                    )
                    if download_task is not None:
                        spawn(
                            _archive_download_task(download_task),
                            name=f"archive:{track.video_id}",
                        )
                    stream = _local_media_stream(chat_id, filepath, video, start_at)
                    break
                if task is download_task and result:
                    filepath = result
                    early_file = bool(filepath and filepath.endswith(".early"))
                    # One owner archives this download. If it was an early
                    # handoff, _archive_download_task waits for completion.
                    spawn(_archive_download_task(task), name=f"archive:{track.video_id}")
                    stream = _local_media_stream(chat_id, filepath, video, start_at)
                    break

        if stream is None:
            if download_cancelled:
                from melody.core.ytdl import _DownloadCancelled
                raise _DownloadCancelled("superseded by a newer playback request")
            if source_errors:
                raise source_errors[-1]
            raise RuntimeError(f"No playable source for {track.video_id}")

        # A successful direct stream is authoritative, but retain the
        # fallback as a low-priority recovery backup. Signed googlevideo URLs
        # can expire or stall mid-song; a completed local backup lets the
        # StreamEnded handler resume at the current position instead of
        # stopping/restarting the song from zero. The fallback starts only
        # after the direct-first grace window, so it cannot starve resolution.
        if direct_task is not None and direct_task in pending:
            direct_task.cancel()
        if early_task is not None and not early_task.done():
            early_task.cancel()
        if filepath is None and download_task is not None:
            if not download_task.done():
                download_task.add_done_callback(_consume_task_exception)
            spawn(_archive_download_task(download_task), name=f"archive:{track.video_id}")
            LOGGER.debug(
                "#download recovery backup retained after direct stream chat=%s video=%s",
                chat_id, track.video_id,
            )

        # Clear silence flag before change_stream so any stream_end events
        # from now on are treated as the real song finishing.
        _silence_playing.pop(chat_id, None)

        # py-tgcalls 2.x has a single `play()` entrypoint: it joins the call
        # if not already active, or swaps the stream source in-place if the
        # chat is already in a call. There is no separate join_group_call()/
        # change_stream() pair like older 1.x releases.
        was_active = _active.get(chat_id)

        # Prime the assistant's peer storage BEFORE play(): pytgcalls'
        # create_group_call() resolves the peer over the assistant account and
        # raises KeyError('ID not found: …') when it is missing.
        # Always (cheap, cached) — the fallback play() below and a re-join
        # after a dropped call need the peer just as much as the first join.
        if not await ensure_assistant_peer(chat_id):
            # Peer could not be resolved from cache/dialogs/invite — the
            # assistant is most likely not a member yet. Join it now BEFORE
            # calling play(), otherwise pytgcalls' create_group_call() blows
            # up with KeyError('ID not found') / CHANNEL_INVALID and we only
            # recover via the slow exception path below.
            LOGGER.debug("peer miss for %s — auto-joining assistant before play()", chat_id)
            await _auto_join_assistant(chat_id)

        async with _get_stream_commit_lock(chat_id):
        # RACE FIX (/stop and /end came back after a few seconds):
            # _stream_track() runs fire-and-forget while the download races. If
            # the user issued /stop, /end or /leave in that window, stop_stream()
            # already left the call and cleared state — but this coroutine was
            # still holding a finished stream and happily called play() again,
            # which RE-JOINED the voice chat. Abort here instead; the leaving
            # grace window is exactly the signal for it.
            if _is_leaving(chat_id):
                LOGGER.debug("stream aborted for %s — leave requested mid-download",
                            chat_id)
                return

            # RACE FIX (audio/video mismatch, duplicate/restarted playback): a
            # newer /play, /vplay, force-play or queue-advance may have started
            # for this chat while this resolve/download was still in flight.
            # This result is now stale — never hand it to py-tgcalls.
            if _is_stale_generation(chat_id, gen):
                LOGGER.info(
                    "#stream discarding stale resolve for %s in %s (gen=%s no "
                    "longer authoritative — a newer play request superseded it)",
                    track.video_id, chat_id, gen,
                )
                return

            try:
                if filepath is None:
                    # A remote CDN URL can hang ffprobe indefinitely (no network
                    # timeout exists in py-tgcalls). Bound it hard: if the CDN is
                    # unusable we want to know in ~6s, not in 20.
                    # Bound a genuinely blocked cloud route aggressively while the
                    # local download keeps progressing in parallel.
                    # SPEED ROOT FIX ("gaana play karne ke baad 10s+ lagta
                    # hai"): this budget used to be 3s on a cloud host. ffprobe
                    # opening a googlevideo URL from a small dyno regularly needs
                    # 3-6s, so the *working* direct stream was being abandoned on
                    # almost every cold /play — and the fallback is a FULL file
                    # download plus ffmpeg transcode, i.e. the exact 10-20s wait
                    # users reported. Waiting a few more seconds on the direct
                    # path is always faster than downloading the whole song, so
                    # give ffprobe a realistic window (tunable via env).
                    await asyncio.wait_for(
                        _pytgcalls.play(chat_id, stream),
                        timeout=(
                            _LOCAL_PROXY_PLAY_TIMEOUT
                            if local_proxy_source else _PLAY_PROBE_TIMEOUT
                        ),
                    )
                else:
                    # A local file can still hang inside ffprobe/ffmpeg when
                    # the dyno is overloaded. Bound it so the playback task
                    # cannot hold a chat’s control path forever.
                    await asyncio.wait_for(
                        _pytgcalls.play(chat_id, stream),
                        timeout=_LOCAL_PLAY_TIMEOUT,
                    )
            except Exception as play_exc:
                # See _is_probe_error(): a dead/unreadable CDN URL makes ffprobe
                # return empty output, which surfaces as JSONDecodeError +
                # ProcessLookupError instead of anything actionable. Download the
                # track and play the local file instead of failing the request.
                # A local Telegram range proxy is the source of truth for a
                # tagged large file. Recreating it via download_audio() after a
                # probe timeout creates a second proxy entry and can race the first
                # stream. Fail this attempt cleanly instead; the longer local-only
                # budget above handles normal ffprobe startup latency.
                if local_proxy_source:
                    LOGGER.warning(
                        "local Telegram media proxy handoff failed for %s in %s (%s)",
                        track.video_id, chat_id, type(play_exc).__name__,
                    )
                    return False
                if (filepath is not None and not early_file) or not (
                    _is_probe_error(play_exc) or isinstance(play_exc, asyncio.TimeoutError)
                ):
                    raise

                # A signed CDN URL can expire or be rejected by one cloud POP
                # after metadata resolution. Give a fresh resolver/profile one
                # bounded chance before waiting for the slower local download.
                # This keeps the normal path under the 5-10s target while still
                # ensuring one transient CDN failure cannot kill playback.
                direct_retry_ok = False
                if (
                    filepath is None
                    and not early_file
                    and not local_proxy_source
                    and startup_deadline - time.monotonic() > 2.0
                ):
                    try:
                        fresh_stream = await _build_direct_stream(
                            chat_id, track, video, start_at, force=True,
                        )
                        if fresh_stream is not None:
                            await asyncio.wait_for(
                                _pytgcalls.play(chat_id, fresh_stream),
                                timeout=_PLAY_PROBE_TIMEOUT,
                            )
                            stream = fresh_stream
                            direct_retry_ok = True
                            LOGGER.info(
                                "#stream fresh direct retry recovered %s in %s",
                                track.video_id, chat_id,
                            )
                    except Exception as retry_exc:
                        LOGGER.info(
                            "#stream fresh direct retry failed for %s in %s (%s)",
                            track.video_id, chat_id, type(retry_exc).__name__,
                        )
                if direct_retry_ok:
                    pass
                elif early_file:
                    # The prefix was valid enough to return, but this particular
                    # FFmpeg/PyTgCalls probe still rejected it. Reuse the same
                    # in-flight job and retry once from the complete atomic file;
                    # never start a second downloader.
                    LOGGER.info(
                        "Early audio prefix probe failed for %s in %s — waiting for completed file.",
                        track.video_id, chat_id,
                    )
                    completed = await wait_for_download(
                        track.video_id, audio_only=not video,
                    )
                    filepath = completed
                    if not filepath:
                        raise RuntimeError("early audio download completed without a file")
                    early_file = False
                elif direct_only_video:
                    LOGGER.warning(
                        "#stream direct-only video unavailable for %s in %s; "
                        "skipping unsafe full download fallback",
                        track.video_id, chat_id,
                    )
                    raise RuntimeError(
                        "long video direct stream unavailable; local fallback disabled"
                    ) from play_exc
                else:
                    # Handled fallback, not a failure: log at INFO so real problems
                    # stay visible in the logs.
                    LOGGER.info(
                        "Direct CDN stream unusable for %s in %s (%s: %s) — "
                        "falling back to download.",
                        track.video_id, chat_id, type(play_exc).__name__,
                        redact_sensitive_text(play_exc),
                    )
                    # Reuse the download already racing in the background instead of
                    # starting a second one from scratch.
                    if download_task is not None and not download_task.done():
                        # The direct probe already consumed the short source
                        # race budget. Do not use that expired deadline to kill
                        # a valid fallback that is still downloading.
                        filepath = await asyncio.wait_for(
                            asyncio.shield(download_task),
                            timeout=_PLAY_FALLBACK_TIMEOUT,
                        )
                    else:
                        filepath = await download_audio(
                            track.video_id, audio_only=not video, priority=priority,
                            owner=chat_id, allow_early=not video,
                        )
                if filepath and not filepath.endswith(".early"):
                    spawn(_persist_completed_song(filepath, track), name=f"song-cache:{track.video_id}")
                stream = _local_media_stream(chat_id, filepath, video, start_at)
                if _is_stale_generation(chat_id, gen):
                    LOGGER.info(
                        "#stream discarding stale fallback resolve for %s in %s "
                        "(gen=%s no longer authoritative)", track.video_id, chat_id, gen,
                    )
                    return
                # The direct attempt may have burnt the cached peer (CHANNEL_INVALID
                # in the log came from THIS second play, not the first): re-prime it.
                forget_assistant_peer(chat_id)
                await ensure_assistant_peer(chat_id)
                try:
                    await asyncio.wait_for(
                        _pytgcalls.play(chat_id, stream),
                        timeout=_LOCAL_PLAY_TIMEOUT,
                    )
                except ChatAdminRequired:
                    # A direct probe can fail first and the fallback play can then
                    # be the first call-creating RPC. Do not let that second
                    # CHAT_ADMIN_REQUIRED escape into the generic crash logger.
                    _block_vc_admin(chat_id)
                    if download_task is not None and not download_task.done():
                        download_task.cancel()
                        download_task.add_done_callback(_consume_task_exception)
                    if _vc_admin_notice_needed(chat_id):
                        _mark_vc_admin_notified(chat_id)
                        await _notify_playback_failed(
                            chat_id, VC_ADMIN_REQUIRED_MESSAGE,
                        )
                    LOGGER.info(
                        "fallback VC creation blocked in %s: assistant needs admin rights",
                        chat_id,
                    )
                    return False

            # A newer request can arrive while the Telegram RPC above is in
            # flight. The external call cannot be rolled back, but its stale
            # result must not become authoritative or update playback state;
            # the newer generation will acquire this commit lock next.
            if _is_stale_generation(chat_id, gen):
                LOGGER.info(
                    "#stream stale play result fenced for %s in %s (gen=%s)",
                    track.video_id, chat_id, gen,
                )
                return False

            # The leave could also have landed while play() itself was in flight:
            # in that case we are now connected again and nobody will clean it up.
            if _is_leaving(chat_id):
                LOGGER.debug("stream started for %s during a leave — disconnecting",
                            chat_id)
                await _hard_leave(chat_id)
                _forget_call_state(chat_id)
                return

            # The speculative fallback was canceled after a successful direct
            # handoff. If the signed URL later expires, StreamEnded recovery
            # starts a fresh completed local download rather than reusing a
            # stale or half-written fallback.

            _clear_vc_admin_block(chat_id)
            _active[chat_id] = True
            _listener_mode.pop(chat_id, None)
            _is_video[chat_id] = video
            _play_start_time[chat_id] = time.time()
            _seek_offset[chat_id] = max(0, int(start_at or 0))
            for k in [k for k in _premature_resumes if k.startswith(f"{chat_id}:") and not k.endswith(f":{track.video_id}")]:
                _premature_resumes.pop(k, None)

            if not was_active:
                vol = get_volume(chat_id)
                if vol != 100:
                    try:
                        await _pytgcalls.change_volume_call(chat_id, vol)
                    except Exception:
                        pass

        # Do not prefetch or mirror completed media in the background. The
        # requested track is the only interactive workload; this keeps RAM,
        # bandwidth, and extractor slots available for the next user request.

        from utils.playback_state import save_snapshot
        await save_snapshot(
            chat_id, get_current(chat_id), get_queue(chat_id), get_loop(chat_id), get_volume(chat_id),
            active=True, position=_seek_offset[chat_id], video=video, speed=get_speed(chat_id),
        )
        return True

    except Exception as exc:
        _active.pop(chat_id, None)
        _silence_playing.pop(chat_id, None)

        # A newer same-chat request intentionally stopped this old download.
        # Do not retry it, notify the group, or send an error card; the newer
        # generation owns playback now.
        try:
            from melody.core.ytdl import is_download_cancelled
            if is_download_cancelled(exc):
                LOGGER.info(
                    "#download cancellation consumed chat=%s video=%s gen=%s",
                    chat_id, getattr(track, "video_id", "?"), gen,
                )
                return False
        except Exception:
            pass

        # ROOT-CAUSE FIX (⚠️ Melody Error Log: ChannelInvalid / PeerIdInvalid
        # / ChannelPrivate — "_stream_track failed"): joining a Telegram
        # group/voice-chat call happens over the ASSISTANT (userbot) account,
        # not the bot account. MTProto requires the calling account to
        # actually be a member of that chat to resolve its peer at all — if
        # the assistant was never added to the group (or was removed from
        # it), Telegram rejects the join with CHANNEL_INVALID/PEER_ID_INVALID
        # every single time, and no amount of retrying fixes it.
        #
        # AUTO-FIX (no more manual "add the assistant" step): the BOT account
        # is already in the group (that's how it received /play at all) and
        # is normally group-admin (needed for other commands), so it can
        # export/reuse an invite link and have the ASSISTANT join through it
        # automatically — no human action required. Only if that auto-join
        # itself fails (bot lacks invite permission, assistant banned, etc.)
        # do we fall back to telling the group to add the assistant manually.
        # FEATURE: no active voice/video chat in the group at all — this is
        # an expected user-side condition (nobody started the VC yet), not a
        # bug, so show the clear instructional message instead of the
        # generic failure text and skip the error-log spam.
        # REQUIREMENT ("error aaye to error mat dikha, situation batao"):
        # a deleted/private/region-blocked YouTube video is a CONTENT problem,
        # not a bug. Previously this raised a full traceback error-log card and
        # left the VC stuck. Now the chat gets a one-line note and the bot
        # simply moves on to the next song.
        if _is_unavailable_media_error(exc):
            # Search metadata may survive after the actual YouTube media is
            # deleted/private/blocked. Try one validated alternate before
            # abandoning the user's request or advancing the queue.
            source_query = getattr(track, "source_query", "") or ""
            if source_query and not getattr(track, "_alternate_attempted", False):
                setattr(track, "_alternate_attempted", True)
                try:
                    from melody.core.ytdl import find_playable_candidate
                    alternate = await find_playable_candidate(
                        source_query,
                        exclude_video_id=getattr(track, "video_id", ""),
                        want_video=video,
                    )
                except Exception as alternate_exc:
                    alternate = None
                    LOGGER.info("alternate candidate recovery failed: %s", alternate_exc)
                if alternate:
                    old_id = track.video_id
                    track.video_id = alternate["id"]
                    track.title = alternate.get("title", track.title)
                    track.duration = int(alternate.get("duration") or 0)
                    track.stream_url = alternate.get("stream_url", "")
                    track.thumbnail = alternate.get("thumbnail", track.thumbnail)
                    track.uploader = alternate.get("uploader", track.uploader)
                    LOGGER.info(
                        "Retrying playback with alternate candidate old=%s new=%s chat=%s",
                        old_id, track.video_id, chat_id,
                    )
                    started = await _stream_track(
                        chat_id, track, video=video, _retry=True,
                        start_at=start_at, gen=gen, priority=priority,
                    )
                    if started:
                        return started

            LOGGER.info("Skipping unavailable track %s in %s", getattr(track, "video_id", "?"), chat_id)
            title = html_escape((getattr(track, "title", "") or "Ye gaana")[:50])
            await _notify_playback_failed(
                chat_id,
                f"⏭ <b>{title}</b> YouTube par available nahi hai (removed/private).\n"
                "Next song play kar raha hoon…",
            )
            try:
                await _play_next(chat_id)
            except Exception:
                pass
            return

        if isinstance(exc, NoActiveGroupCall):
            await _notify_playback_failed(chat_id, NO_ACTIVE_VC_MESSAGE)
            return

        # ⏳ FLOOD_WAIT_X on phone.JoinGroupCall (⚠️ crash report).
        # Telegram rate-limits how often ONE account may join group calls.
        # This is not a bug and a traceback helps nobody: wait it out once
        # when the window is short, otherwise tell the group in plain words
        # how long the assistant has to cool down.
        flood_wait = _flood_seconds(exc)
        if flood_wait is not None:
            LOGGER.warning("FLOOD_WAIT %ss while joining VC in %s", flood_wait, chat_id)
            if flood_wait <= _FLOOD_RETRY_LIMIT and not _retry:
                await _notify_playback_failed(
                    chat_id,
                    f"⏳ Tᴇʟᴇɢʀᴀᴍ ɴᴇ ᴛʜᴏᴅᴀ ʀᴜᴋɴᴇ ᴋᴏ ʙᴏʟᴀ ʜᴀɪ "
                    f"(<code>{flood_wait}s</code>) — ᴀᴜᴛᴏ-ʀᴇᴛʀʏ ᴋᴀʀ ʀᴀʜᴀ ʜᴏᴏɴ 🔁",
                )
                await asyncio.sleep(flood_wait + 1)
                if _is_leaving(chat_id) or _is_stale_generation(chat_id, gen):
                    return
                await _stream_track(chat_id, track, video=video, _retry=True,
                                    start_at=start_at, gen=gen)
                return
            minutes = max(1, round(flood_wait / 60))
            await _notify_playback_failed(
                chat_id,
                "⏳ <b>Tᴇʟᴇɢʀᴀᴍ ғʟᴏᴏᴅ ʟɪᴍɪᴛ</b>\n"
                f"Aꜱꜱɪꜱᴛᴀɴᴛ ᴋᴏ <code>{flood_wait}s</code> (~{minutes} ᴍɪɴ) ᴡᴀɪᴛ "
                "ᴋᴀʀɴᴀ ᴘᴀᴅᴇɢᴀ ᴠᴏɪᴄᴇ ᴄʜᴀᴛ ᴊᴏɪɴ ᴋᴀʀɴᴇ ᴋᴇ ʟɪʏᴇ.\n"
                "Uᴛɴᴇ ᴅᴇʀ ʙᴀᴀᴅ ᴅᴏʙᴀʀᴀ <code>/play</code> ᴋᴀʀᴏ 🎧",
            )
            return

        # ROOT-CAUSE FIX (⚠️ Melody Error Log: ChannelInvalid / PeerIdInvalid
        # / ChannelPrivate — "_stream_track failed"): joining a Telegram
        # group/voice-chat call happens over the ASSISTANT (userbot) account,
        # not the bot account. MTProto requires the calling account to
        # actually be a member of that chat to resolve its peer at all — if
        # the assistant was never added to the group (or was removed from
        # it), Telegram rejects the join with CHANNEL_INVALID/PEER_ID_INVALID
        # every single time, and no amount of retrying fixes it.
        #
        # FEATURE (auto-unban/unmute): UserBannedInChannel means the
        # assistant WAS a member but got banned or muted — handled the same
        # way, since _auto_join_assistant() now lifts a ban/mute with the
        # bot's own admin rights before attempting the invite-link rejoin.
        #
        # AUTO-FIX (no more manual "add the assistant" step): the BOT account
        # is already in the group (that's how it received /play at all) and
        # is normally group-admin (needed for other commands), so it can
        # export/reuse an invite link and have the ASSISTANT join through it
        # automatically — no human action required. Only if that auto-join
        # itself fails (bot lacks invite permission, assistant banned, etc.)
        # do we fall back to telling the group to add the assistant manually.
        _peer_missing = isinstance(exc, KeyError) and "ID not found" in str(exc)
        if _peer_missing and not _retry:
            # Session storage had no access_hash for this chat: warm it and
            # replay once instead of failing the request.
            forget_assistant_peer(chat_id)
            purge_assistant_peer_storage(chat_id)
            if await ensure_assistant_peer(chat_id) and not _is_stale_generation(chat_id, gen):
                LOGGER.debug("✅ Assistant peer resolved for %s — retrying playback.", chat_id)
                await _stream_track(chat_id, track, video=video, _retry=True, start_at=start_at, gen=gen)
                return

        if isinstance(exc, (ChannelInvalid, ChannelPrivate, PeerIdInvalid, UserBannedInChannel)) and not _retry:
            LOGGER.warning("Assistant cannot resolve chat %s (%s) — attempting auto-join/unban.",
                            chat_id, type(exc).__name__)
            forget_assistant_peer(chat_id)
            # CHANNEL_INVALID with a row in storage == stale access_hash: it
            # must be deleted, otherwise every re-resolve returns the same
            # broken peer and the auto-join below "succeeds" into nothing.
            purge_assistant_peer_storage(chat_id)
            joined = await _auto_join_assistant(chat_id) or await ensure_assistant_peer(chat_id)
            if joined and not _is_stale_generation(chat_id, gen):
                LOGGER.debug("✅ Assistant auto-joined/unbanned in chat %s — retrying playback.", chat_id)
                await _stream_track(chat_id, track, video=video, _retry=True,
                                    start_at=start_at, gen=gen)
                return
            await _notify_playback_failed(
                chat_id,
                "⚠️ <b>Voice assistant account is group mein nahi hai, aur auto-join bhi fail ho gaya.</b>\n\n"
                "Voice chat me gaana bajane ke liye assistant account ka bhi is group ka "
                "member hona zaroori hai. Please assistant ko group mein manually add karo "
                "(ya bot ko 'Invite Users via Link' admin permission do) aur phir se "
                "<code>/play</code> try karo.",
            )
            # Expected Telegram access/permission failure: the group has been
            # told what to do; never forward this traceback to the owner log.
            return False
        elif isinstance(exc, (ChannelInvalid, ChannelPrivate, PeerIdInvalid, UserBannedInChannel)):
            # Already retried once after auto-join/unban and it still failed.
            await _notify_playback_failed(
                chat_id,
                "⚠️ <b>Assistant ko group mein add/unban karne ke baad bhi gaana play nahi ho paya.</b>\n\n"
                "Dobara <code>/play</code> try karo.",
            )
            return False
        elif isinstance(exc, ChatAdminRequired):
            _block_vc_admin(chat_id)
            # ROOT-CAUSE FIX (⚠️ "CHAT_ADMIN_REQUIRED ... The method requires
            # chat admin privileges" — the delay/failure the user actually
            # reported here): Telegram only lets a chat ADMIN start a brand
            # new voice chat (`phone.createGroupCall`, called internally by
            # pytgcalls' play() the very first time a call becomes active in
            # a chat). Joining an ALREADY-RUNNING voice chat needs no special
            # rights at all — this only fires when no voice chat exists yet
            # and the assistant account trying to create one is a plain
            # (non-admin) member.
            #
            # This used to fall through to the generic "❌ Gaana play nahi ho
            # paya" branch below with no explanation, AND pre_join() (the
            # instant silence-join meant to make /play feel instant) hits the
            # exact same error and silently swallows it — so the bot skipped
            # its fast path, silently fell back to the slow path, and only
            # surfaced a vague failure several seconds later. That combo is
            # exactly what read as "bohot jada delay" to the user: a long
            # wait for nothing, ending in an unhelpful error.
            #
            # There is no code-side fix for a Telegram-enforced permission —
            # either promote the assistant to admin, or a human starts the
            # voice chat once (after which the bot can join it normally every
            # time, since joining an existing call never needs admin rights).
            # Naming the real cause immediately (instead of a generic retry
            # loop) is the actual fix for the reported "delay".
            if _vc_admin_notice_needed(chat_id):
                _mark_vc_admin_notified(chat_id)
                await _notify_playback_failed(chat_id, VC_ADMIN_REQUIRED_MESSAGE)
            # Permission is already explained in the GC; do not emit an owner
            # error-log entry for this expected condition.
            return False
        else:
            # ROOT-CAUSE FIX ("kabhi kabhi _stream_track failed aata hai"):
            # the vast majority of these are transient — an expired CDN URL,
            # a SABR/empty download, a half-written cache file left behind by
            # a killed attempt. Previously that surfaced as a hard failure to
            # the user on the very first hiccup. Purge the poisoned cache
            # files for this track and give it exactly ONE clean retry before
            # reporting anything.
            if not _dl_retry:
                LOGGER.warning(
                    "playback failed for %s in %s (%s: %s) — one clean retry",
                    getattr(track, "video_id", "?"), chat_id,
                    type(exc).__name__, exc,
                )
                try:
                    _cleanup_partial_files(getattr(track, "video_id", ""))
                except Exception:
                    pass
                await asyncio.sleep(0.5)
                if _is_stale_generation(chat_id, gen):
                    return
                try:
                    await _stream_track(chat_id, track, video=video,
                                        start_at=start_at, _dl_retry=True, gen=gen)
                    return
                except Exception as retry_exc:  # noqa: BLE001
                    exc = retry_exc
            await _notify_playback_failed(
                chat_id,
                "❌ <b>Gaana play nahi ho paya.</b>\n\nDobara <code>/play</code> try karo.",
            )

        await send_error_log(
            f"_stream_track failed in {chat_id}",
            exc,
            context={
                "chat_id": chat_id,
                "song_title": track.title if track else None,
                "video_id": track.video_id if track else None,
                "uploader": track.uploader if track else None,
            },
        )


# Cached assistant user id — resolved once via get_me(), reused everywhere
# below instead of hitting Telegram on every auto-join check.
_assistant_id: "int | None" = None


async def _get_assistant_id() -> "int | None":
    global _assistant_id
    if _assistant_id is not None:
        return _assistant_id
    try:
        from melody import assistant
        me = await assistant.get_me()
        _assistant_id = me.id
        return _assistant_id
    except Exception as exc:
        LOGGER.warning("Could not resolve assistant's own user id: %s", exc)
        return None


async def _unban_or_unmute_assistant(chat_id: int) -> str:
    """FEATURE: if the assistant account is banned or muted (restricted) in
    `chat_id`, lift that with the BOT's own admin rights before attempting
    to (re)join — no group admin has to do this by hand.

    Safe / best-effort: if the bot itself lacks ban/restrict rights, or the
    assistant was never a member at all (get_chat_member 404s), this just
    logs and falls through so the normal invite-link join below still runs.
    """
    try:
        from melody import bot
    except Exception:
        return "unknown"

    assistant_id = await _get_assistant_id()
    if not assistant_id:
        return "unknown"

    try:
        member = await bot.get_chat_member(chat_id, assistant_id)
    except Exception:
        # Not a member yet at all (or never was) — nothing to unban/unmute.
        return "not_member"

    was_banned = member.status == enums.ChatMemberStatus.BANNED
    try:
        if was_banned:
            LOGGER.log(
                _join_peek_level(chat_id),
                "Assistant is banned in %s — auto-unbanning with bot's admin rights.", chat_id,
            )
            await bot.unban_chat_member(chat_id, assistant_id)
            return "unbanned"
        elif member.status == enums.ChatMemberStatus.RESTRICTED:
            perms = member.permissions
            is_muted = not (perms and perms.can_send_messages)
            if is_muted:
                LOGGER.warning("Assistant is muted/restricted in %s — auto-lifting restriction.", chat_id)
                await bot.restrict_chat_member(
                    chat_id, assistant_id, permissions=ChatPermissions(all_perms=True),
                )
    except Exception as exc:
        # Bot may not actually have ban/restrict rights in this chat — that's
        # a real limitation, not a bug, so just log and let the join attempt
        # below proceed (and fail with its own clear message if it must).
        LOGGER.log(
            _join_log_level(chat_id),
            "Could not auto-unban/unmute assistant in %s: %s", chat_id, exc,
        )
        if was_banned:
            # REQUESTED: bot has no ban/unban rights -> ask the group for them
            # (or for a manual unban) and hand them the exact command.
            await _ask_for_unban(chat_id, assistant_id)
            return "banned_no_rights"

    return "ok"


async def _ask_for_unban(chat_id: int, assistant_id: int) -> None:
    """REQUESTED: when the assistant is BANNED in a group and the bot itself
    has no ban/unban rights, don't fail silently — tell the group that Melody
    needs "Ban Users" permission (then it unbans the assistant itself and
    joins the voice chat automatically), and give them the ready-made manual
    command as well: /unban <numeric id>."""
    try:
        from melody import bot
        from pyrogram import enums as _enums

        await bot.send_message(
            chat_id,
            "⚠️ <b>Mᴇʀᴀ ᴀssɪsᴛᴀɴᴛ ᴀᴄᴄᴏᴜɴᴛ ɪs ɢʀᴏᴜᴘ ᴍᴇɪɴ BAN ʜᴀɪ</b> 🚫\n\n"
            "Isliye voice chat me gaana play nahi ho sakta. Do options hain:\n\n"
            "1️⃣ <b>Mujhe \"Ban Users\" admin permission do</b> — phir main khud "
            "assistant ko unban karke VC me bula lunga (kuch karna nahi padega).\n\n"
            "2️⃣ <b>Ya khud unban kar do</b> — ye command bhejo:\n"
            f"<code>/unban {assistant_id}</code>\n\n"
            "Uske baad <code>/play</code> dobara bhejo 🎶",
            parse_mode=_enums.ParseMode.HTML,
        )
    except Exception as exc:
        LOGGER.debug("Could not ask %s for unban rights: %s", chat_id, exc)


async def _auto_join_assistant(chat_id: int) -> bool:
    """Make the assistant (userbot) account join `chat_id` automatically via
    an invite link exported by the bot account, so a group admin never has
    to manually add the assistant for voice chat playback to work.

    Also auto-fixes the far more common cause of the assistant "not being
    in the group": it WAS a member but got banned or muted at some point
    (e.g. an over-eager anti-raid bot, or a leftover restriction from
    before Melody was even added) — see _unban_or_unmute_assistant().

    Requires the BOT to already be a member with "invite users via link"
    permission (true for any group where /play works at all, since that's
    the standard admin permission set music bots ask for). Returns True only
    if the assistant is confirmedly a member afterwards.
    """
    try:
        from melody import bot, assistant
    except Exception as exc:
        LOGGER.warning("Auto-join: could not import bot/assistant clients: %s", exc)
        return False

    # BUG FIX (log spam: the same "could not export invite link …
    # CHAT_ADMIN_REQUIRED" / "Could not auto-unban … CHAT_ADMIN_REQUIRED"
    # warning repeated for a dozen chats every single minute). The cause is a
    # permanent, group-side permission problem — retrying it in a loop only
    # burns RPC budget (and helps Telegram hand us a FLOOD_WAIT). Once a chat
    # answers "you are not admin", back off for a while and stay quiet.
    if _join_blocked(chat_id):
        LOGGER.debug("Auto-join: skipping %s (cooling down after a permission failure)", chat_id)
        return False

    status = await _unban_or_unmute_assistant(chat_id)
    if status == "banned_no_rights":
        # The group has to act (give the bot ban rights, or /unban manually).
        # _ask_for_unban() already told them exactly what to do.
        _block_join(chat_id)
        return False

    # Public groups need no invite link at all — the @username is enough and
    # never depends on the bot having "invite users via link" rights.
    link = None
    try:
        chat = await bot.get_chat(chat_id)
        if getattr(chat, "username", None):
            link = chat.username
        else:
            link = getattr(chat, "invite_link", None)
    except Exception as exc:
        LOGGER.debug("Auto-join: bot could not read chat %s: %s", chat_id, exc)

    if not link:
        try:
            link = await bot.export_chat_invite_link(chat_id)
        except Exception as exc:
            _block_join(chat_id)
            LOGGER.log(
                _join_log_level(chat_id),
                "Auto-join: bot could not export invite link for %s: %s", chat_id, exc,
            )
            return False

    try:
        await assistant.join_chat(link)
    except Exception as exc:
        # REQUESTED ("agar join request bheji to accept karna"): groups with
        # "approve new members" turned on convert the assistant's join into a
        # PENDING join request instead of a membership, and join_chat raises
        # (INVITE_REQUEST_SENT). The bot is already an admin here, so it can
        # approve its own assistant's request and playback continues without
        # a human ever touching it.
        if "REQUEST" in str(exc).upper() or "APPROV" in str(exc).upper():
            try:
                me = await assistant.get_me()
                await bot.approve_chat_join_request(chat_id, me.id)
                LOGGER.debug("Auto-join: approved assistant join request in %s", chat_id)
            except Exception as approve_exc:
                LOGGER.warning(
                    "Auto-join: could not approve assistant join request in %s: %s",
                    chat_id, approve_exc,
                )
        # "USER_ALREADY_PARTICIPANT" etc. — assistant may already be in the
        # chat but pyrogram's local peer cache just doesn't know about it
        # yet (e.g. added by someone else after the assistant last started).
        # Either way, fall through to the get_chat() re-check below instead
        # of giving up immediately.
        LOGGER.debug("Auto-join: assistant.join_chat raised (may be harmless): %s", exc)

    try:
        await assistant.get_chat(chat_id)
        # Success — forget any previous permission back-off for this chat.
        _join_block.pop(chat_id, None)
        _join_warned.discard(chat_id)
        return True
    except Exception as exc:
        _block_join(chat_id)
        LOGGER.log(
            _join_log_level(chat_id),
            "Auto-join: assistant still cannot resolve %s after join attempt: %s", chat_id, exc,
        )
        return False



# ───────────────────────────── flood-wait helper ─────────────────────────────
# Telegram answers phone.JoinGroupCall with FLOOD_WAIT_X when one account joins
# calls too often. py-tgcalls wraps the join, so the FloodWait can arrive
# nested inside a TimeoutError chain — dig it out of the cause chain too.
_FLOOD_RETRY_LIMIT = 45          # auto-retry only when the cooldown is short


def _flood_seconds(exc: BaseException) -> "int | None":
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, FloodWait):
            return int(getattr(exc, "value", 0) or getattr(exc, "x", 0) or 0)
        exc = exc.__cause__ or exc.__context__
    return None

_playback_notice_until: dict[int, float] = {}
_PLAYBACK_NOTICE_TTL = 60.0


async def _notify_playback_failed(chat_id: int, text: str):
    """Best-effort, deduplicated user-facing failure notice.

    Playback is fire-and-forget, so a single Telegram permission failure can
    otherwise be reported by pre-join, direct play, fallback play, and the
    generic task handler. One notice per chat per short window keeps the GC
    usable while retaining the actionable instruction for the user.
    """
    now = time.monotonic()
    deadline = _playback_notice_until.get(chat_id, 0.0)
    if deadline > now:
        return
    _playback_notice_until[chat_id] = now + _PLAYBACK_NOTICE_TTL
    try:
        from melody import bot
        from pyrogram import enums
        await bot.send_message(chat_id, text, parse_mode=enums.ParseMode.HTML)
    except Exception:
        # A notification failure must never become a second playback error.
        pass


# ─── Public API ───────────────────────────────────────────────────────────────

async def pre_join(chat_id: int, video: bool = False) -> bool:
    """
    ⚡ Requirement #4 — join the voice chat with silence THE INSTANT the
    command arrives, before we even know which track we're looking for.

    Call this from the command handler in parallel with (not after) the
    yt-dlp search, e.g.:

        asyncio.create_task(pre_join(chat.id))
        info = await get_video_info(query)   # runs concurrently

    Previously the VC join only happened inside play_stream(), which itself
    only ran AFTER get_video_info() resolved — meaning the multi-second
    search always happened before the bot ever appeared in the call. Calling
    pre_join() up front removes the search from the critical path entirely:
    the bot joins in the time it takes one Telegram RPC round-trip, and the
    real song swaps in via change_stream the moment yt-dlp resolves it.

    Idempotent / safe to call even if the chat is already active (either
    mid silence-join or already playing a real track) — no-ops in that case.

    ROOT-CAUSE FIX (/vplay video never showing): this silence trick always
    joined with an AUDIO-ONLY file (no camera track). Telegram/py-tgcalls
    negotiate whether a participant is broadcasting video at *join* time —
    swapping in a video MediaStream afterwards via play() does not upgrade
    an already-audio-only join to a video one, which is why /vplay used to
    play sound with no picture. For video requests we skip the silence
    pre-join entirely so the very first play() call (in _stream_track) is
    the one that joins the call, and it always carries the real video —
    fixing the join at the source instead of trying to patch it after.
    """
    if video:
        return False

    if _active.get(chat_id):
        return False

    silence = await _get_silence_file()
    if not silence or not _pytgcalls:
        return False

    # SPEED/RACE FIX: pre_join now holds the same per-chat play lock that
    # _stream_track() uses. The caller (play.py) therefore no longer has to
    # await the whole silence-join before starting the real track — it waits
    # a couple of seconds at most and hands off; if the pre-join is still in
    # flight, _stream_track simply queues on this lock instead of firing a
    # second, conflicting play() at PyTgCalls.
    async with _get_play_lock(chat_id):
        if _active.get(chat_id):
            return False
        return await _pre_join_locked(chat_id, silence)


async def _pre_join_locked(chat_id: int, silence: str) -> bool:
    # Same race as _stream_track(): never (re)join a chat the user just asked
    # us to leave.
    if _is_leaving(chat_id):
        return False
    try:
        await ensure_assistant_peer(chat_id)
        audio_quality = _get_audio_quality()
        # Same fix as _ffmpeg_params(): the silence file is a local file, so
        # -reconnect* would make ffmpeg refuse to open it and the pre-join
        # would carry a dead (silent, instantly-ended) stream.
        sstream = MediaStream(
            silence,
            audio_parameters=audio_quality,
            # LOG FIX (heroku log: 'NoVideoSourceFound ... melody_vc_silence.mp3'
            # probed twice on every pre-join): the silence file has no video
            # track, so let PyTgCalls skip the camera probe entirely.
            video_flags=MediaStream.Flags.IGNORE,
        )

        _silence_playing[chat_id] = True
        await asyncio.wait_for(_pytgcalls.play(chat_id, sstream), timeout=_PREJOIN_TIMEOUT)
        _active[chat_id] = True
        LOGGER.debug("⚡ pre_join: instant VC join done for %d", chat_id)
        return True
    except asyncio.TimeoutError:
        LOGGER.debug("pre_join timed out for %d — real song will join VC normally", chat_id)
        _silence_playing.pop(chat_id, None)
        return False
    except ChatAdminRequired:
        _block_vc_admin(chat_id)
        _silence_playing.pop(chat_id, None)
        if _vc_admin_notice_needed(chat_id):
            _mark_vc_admin_notified(chat_id)
            await _notify_playback_failed(chat_id, VC_ADMIN_REQUIRED_MESSAGE)
        LOGGER.info("pre_join blocked in %s: assistant needs admin rights to create the VC", chat_id)
        return False
    except Exception as e:
        LOGGER.debug("pre_join failed for %d: %s — real song will join VC normally", chat_id, e)
        _silence_playing.pop(chat_id, None)
        return False


async def abort_prejoin_if_idle(chat_id: int):
    """
    If we optimistically pre_join()'d a chat with silence but the search
    afterwards failed (song not found / too long / etc.), leave the call
    instead of leaving the bot sitting silently in the voice chat forever.
    No-ops if a real track ended up playing.
    """
    if _silence_playing.get(chat_id) and not get_current(chat_id):
        _mark_leaving(chat_id)
        _active.pop(chat_id, None)
        _silence_playing.pop(chat_id, None)
        await _hard_leave(chat_id)


# ───────────────────────── 🎧 VC-chat listener mode ──────────────────────────

async def _replay_listener_silence(chat_id: int) -> None:
    """Re-arm the silence loop that keeps the assistant inside the call."""
    silence = await _get_silence_file()
    if not silence or not _pytgcalls or not _listener_mode.get(chat_id):
        return
    try:
        await _pytgcalls.play(chat_id, MediaStream(
            silence,
            audio_parameters=_get_audio_quality(),
            video_flags=MediaStream.Flags.IGNORE,
        ))
        _silence_playing[chat_id] = True
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("listener silence loop failed for %s: %s", chat_id, exc)
        _listener_mode.pop(chat_id, None)
        _forget_call_state(chat_id)
        await _hard_leave(chat_id)


def is_listener(chat_id: int) -> bool:
    return bool(_listener_mode.get(chat_id))


async def join_as_listener(chat_id: int) -> bool:
    """DISABLED — the assistant must never sit in a voice chat without music.

    Kept as a no-op so older call sites keep working. Presence-free VC
    tracking lives in `melody/core/vc_listener.py` (pure MTProto polling) and
    the VC chat log in `melody/core/vc_chat_log.py`; neither needs a seat in
    the call. Silent joins were also the source of the
    `FLOOD_WAIT_X ... phone.JoinGroupCall` crashes.
    """
    LOGGER.debug("join_as_listener is disabled (no silent VC presence) — %s", chat_id)
    return False


async def leave_listener(chat_id: int) -> None:
    """Leave a call we joined purely as a chat-log listener."""
    if not _listener_mode.pop(chat_id, None):
        return
    if get_current(chat_id):     # a real song started meanwhile — stay
        return
    _mark_leaving(chat_id)
    _forget_call_state(chat_id)
    await _hard_leave(chat_id)
    LOGGER.debug("🎧 listener mode left VC in %s", chat_id)


async def force_play_stream(
    chat_id: int, track, video: bool = False, prejoin: bool = False,
) -> bool:
    """Immediately play ``track`` without racing another state transition."""
    _clear_leaving(chat_id)
    try:
        from melody.core.ytdl import cancel_lower_priority_downloads
        cancel_lower_priority_downloads(0, exclude_video_id=track.video_id)
    except Exception:
        pass

    # Serialize only the authoritative state swap. The expensive source
    # resolve/download and PyTgCalls handoff stay outside the lock, so a
    # concurrent /skip or /play can still take over immediately.
    async with _get_play_lock(chat_id):
        _cancel_stale_download(chat_id, track.video_id, bool(video))
        try:
            from melody.core.queue import remove_matching_track
            remove_matching_track(chat_id, track.video_id)
        except Exception:
            pass
        set_current(chat_id, track)
        # Do not let an old seek/progress value leak into a newly requested song.
        _seek_offset[chat_id] = 0

        # Mint a fresh generation before releasing the lock. Every older
        # _stream_track() now becomes stale and is fenced before its handoff.
        gen = _begin_generation(chat_id)
        _resolving[chat_id] = gen
        _resolving_track[chat_id] = (track.video_id, bool(video))
        _resolving_origin[chat_id] = "manual"
        if prejoin and not _active.get(chat_id):
            silence = await _get_silence_file()
            if silence and _pytgcalls and not video:
                await _pre_join_locked(chat_id, silence)

    result = False
    try:
        result = await _stream_track(
            chat_id, track, video=video, gen=gen, priority=_FORCE_DOWNLOAD_PRIORITY,
        )
        return result is True and not is_vc_admin_blocked(chat_id)
    finally:
        if result is not True and _resolving.get(chat_id) == gen and get_current(chat_id) is track:
            set_current(chat_id, None)
        if _resolving.get(chat_id) == gen:
            _resolving.pop(chat_id, None)
            _resolving_track.pop(chat_id, None)
            _resolving_origin.pop(chat_id, None)


async def play_stream(
    chat_id: int, track, video: bool = False, prejoin: bool = True,
) -> bool:
    """
    Start or queue a track.
    Returns True if playing now, False if queued.

    FIX 4 — CONCURRENT /play RACE:
    The per-chat lock (_play_locks) ensures that if two users issue /play at
    almost the same time, the second coroutine waits until the first has
    finished updating _active. Without the lock, both could read
    _active.get(chat_id) == False simultaneously, both set_current(), and both
    call _stream_track() — resulting in only one song playing while the other
    is silently lost. With the lock, the second request always sees
    _active == True and is correctly added to the queue.

    ⚡ INSTANT VC JOIN:
    If pre_join() already got the bot into the call with silence, this skips
    straight to streaming the real track (near-instant change_stream swap).
    If pre_join() was never called or failed, this falls back to joining
    with silence itself before streaming, exactly as before.
    """
    from melody.core.queue import add_to_queue

    # A manual request is interactive work: stop unrelated AutoPlay/recovery
    # downloads before it waits for a resolver. Same-video work is excluded so
    # deduplication can safely promote/share it instead of canceling the manual
    # request's own future.
    try:
        from melody.core.ytdl import cancel_lower_priority_downloads
        cancel_lower_priority_downloads(0, exclude_video_id=track.video_id)
    except Exception:
        pass

    # A new /play cancels any pending leave-suppression window.
    _clear_leaving(chat_id)
    lock = _get_play_lock(chat_id)

    async with lock:
        # A real track is already playing (not just our silence placeholder) —
        # queue this one instead of interrupting it.
        if (
            _active.get(chat_id)
            and not _silence_playing.get(chat_id)
            and _resolving_origin.get(chat_id) != "autoplay"
        ):
            add_to_queue(chat_id, track)
            # BUG FIX ("next song must download krke rakh taki delay na
            # lage"): don't wait for the currently-playing track to finish
            # before warming the cache for this one — if this is the first
            # (or a new) queue head, start downloading it right away so
            # it's already cached by the time its turn comes.
            spawn(_prefetch_upcoming(chat_id))
            return False

        # If AutoPlay is resolving after a natural end, the real current
        # stream is already finished even though `_active` remains true until
        # the replacement commits. A manual request must take over that slot;
        # the fresh generation below fences the AutoPlay coroutine.
        if _resolving_origin.get(chat_id) == "autoplay":
            LOGGER.info("#play manual takeover of autoplay transition in %s", chat_id)

        # IDEMPOTENCY FIX (duplicate/restarted playback + audio/video
        # mismatch): a previous /play is still resolving/downloading its
        # track for this chat (typically while the silence pre-join is up).
        # Racing a second concurrent _stream_track() here is exactly what
        # let two competing play() calls land for the same chat — queue
        # this request instead of starting another one.
        if chat_id in _resolving:
            # No real audio is active yet: make the newest interactive request
            # authoritative and stop only the older different-track download.
            # Once a real track is active, normal `/play` remains queueing.
            if (
                not _active.get(chat_id)
                or _silence_playing.get(chat_id)
                or _resolving_origin.get(chat_id) == "autoplay"
            ):
                _cancel_stale_download(chat_id, track.video_id, bool(video))
            else:
                add_to_queue(chat_id, track)
                spawn(_prefetch_upcoming(chat_id))
                return False
        set_current(chat_id, track)
        # A new manual track always starts at the beginning; only explicit
        # /seek and opt-in restart recovery may use a nonzero offset.
        _seek_offset[chat_id] = 0
        gen = _begin_generation(chat_id)
        _resolving[chat_id] = gen
        _resolving_track[chat_id] = (track.video_id, bool(video))
        _resolving_origin[chat_id] = "manual"
        # If nobody pre-joined for us yet, do it now (keeps old callers
        # working). Audio-first /play passes prejoin=False so the real direct
        # MediaStream creates the VC without waiting for a silence RPC.
        if prejoin and not _active.get(chat_id):
            # We already hold this chat's play lock. Calling pre_join() here
            # would try to acquire the same non-reentrant asyncio.Lock again
            # and deadlock forever (commands appeared to start slowly or not
            # at all whenever the optimistic pre-join failed). Use the locked
            # helper directly instead.
            silence = await _get_silence_file()
            if silence and _pytgcalls and not video:
                await _pre_join_locked(chat_id, silence)

    # ⚡ Download / pipe-stream the real song concurrently. If silence join
    # succeeded (either here or via an earlier pre_join()), change_stream
    # swaps in the audio the instant it's ready. If silence join failed,
    # _stream_track joins VC normally with the real song.
    # NOTE: _stream_track is kicked off OUTSIDE the lock so it doesn't block
    # the next /play request from queuing while the download is in progress.
    # A fresh manual /play clears the AutoPlay anti-spam counter.
    try:
        from melody.core.autoplay import reset_autoplay_guard
        reset_autoplay_guard(chat_id)
    except Exception:
        pass

    # Await the handoff itself. The previous fire-and-forget call returned and
    # logged `stream=0.00s` while the VC was still playing silence, hiding the
    # real 10-20 second delay. Awaiting does not block other Telegram handlers;
    # it only keeps this command's status/timing truthful until PyTgCalls has
    # accepted the audible stream.
    result = False
    try:
        result = await _stream_track(chat_id, track, video=video, gen=gen, priority=0)
    finally:
        if result is not True and _resolving.get(chat_id) == gen and get_current(chat_id) is track:
            set_current(chat_id, None)
        if _resolving.get(chat_id) == gen:
            _resolving.pop(chat_id, None)
            _resolving_track.pop(chat_id, None)
            _resolving_origin.pop(chat_id, None)
    if result is not True or is_vc_admin_blocked(chat_id):
        return False
    return True


def _is_probably_stale_stream_end(chat_id: int) -> bool:
    """Ignore only impossible early ends after a recent stream replacement.

    PyTgCalls does not include a stream-generation token in StreamEnded. A
    normal-length track ending within the first 1.5s is much more likely to be
    a late event from the previous stream than a genuine end. Tracks of three
    seconds or less, and tracks with unknown duration, are never suppressed.
    """
    current = get_current(chat_id)
    if current is None:
        return False
    try:
        duration = int(getattr(current, "duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 3:
        return False
    started = _play_start_time.get(chat_id)
    if not started:
        return False
    elapsed = time.time() - started
    if 0 <= elapsed < 1.5:
        LOGGER.info(
            "#stream ignoring early StreamEnded for %s (%s, %.2fs old)",
            chat_id, getattr(current, "video_id", "?"), elapsed,
        )
        return True
    return False


def _mark_leaving(chat_id: int) -> None:
    """Suppress the self-inflicted StreamEnded caused by leaving a call."""
    _leaving[chat_id] = time.monotonic() + _LEAVE_GRACE


def _is_leaving(chat_id: int) -> bool:
    deadline = _leaving.get(chat_id)
    if not deadline:
        return False
    if time.monotonic() > deadline:
        _leaving.pop(chat_id, None)
        return False
    return True


def _clear_leaving(chat_id: int) -> None:
    _leaving.pop(chat_id, None)


async def _hard_leave(chat_id: int) -> None:
    """Actually disconnect, tolerating an already-disconnected call.

    py-tgcalls raises NotInCallError / ConnectionNotFound when the VC was
    already closed manually or the assistant was kicked — that is a SUCCESS
    for us, not an error, so it must not abort the state cleanup that follows.
    A single retry covers the transient network case.
    """
    for attempt in (1, 2):
        try:
            await asyncio.wait_for(_pytgcalls.leave_call(chat_id), timeout=10)
            return
        except Exception as exc:  # noqa: BLE001
            if _is_not_in_call(exc):
                return
            if attempt == 2:
                LOGGER.warning("leave_call failed for %s: %s", chat_id, exc)
                return
            await asyncio.sleep(0.5)


def _forget_call_state(chat_id: int) -> None:
    """Drop every per-chat playback flag for a call we are no longer in.

    Without this, a dropped/ended voice chat left `_active[chat_id] = True`
    behind, so later /pause, /resume, /seek … kept talking to a connection
    that no longer exists (ntgcalls.ConnectionNotFound → NotInCallError).
    """
    _active.pop(chat_id, None)
    _silence_playing.pop(chat_id, None)
    _is_video.pop(chat_id, None)
    _play_start_time.pop(chat_id, None)
    _seek_offset.pop(chat_id, None)
    _prefetch_inflight.pop(chat_id, None)
    for k in [k for k in _premature_resumes if k.startswith(f"{chat_id}:")]:
        _premature_resumes.pop(k, None)
    _speed.pop(chat_id, None)
    _muted.pop(chat_id, None)
    _listener_mode.pop(chat_id, None)


def _is_not_in_call(exc: BaseException) -> bool:
    """True for 'the userbot is not in a call' style errors.

    Covers py-tgcalls' NotInCallError and the raw binding error it wraps
    (ntgcalls.ConnectionNotFound), which is not importable from Python.
    """
    if isinstance(exc, NotInCallError):
        return True
    names = {type(exc).__name__}
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        names.add(type(cause).__name__)
    if names & {"NotInCallError", "ConnectionNotFound", "GroupCallNotFound"}:
        return True
    # BUG FIX (log: "leave_call failed for -100…: No active group call"):
    # py-tgcalls also reports this state as a plain message-only error, so
    # matching on the class name alone logged a scary warning for the most
    # boring case there is — we already left the call.
    text = " ".join(str(e).lower() for e in (exc, cause) if e is not None)
    return any(
        marker in text
        for marker in (
            "no active group call",
            "not in a call",
            "group call not found",
            "groupcall_invalid",
            "connectionnotfound",
        )
    )


async def pause_stream(chat_id: int) -> bool:
    """Pause playback. Returns False if nothing is actually playing."""
    if not _pytgcalls or not _active.get(chat_id):
        _forget_call_state(chat_id)
        return False
    try:
        await asyncio.wait_for(_pytgcalls.pause(chat_id), timeout=_CONTROL_RPC_TIMEOUT)
        return True
    except Exception as exc:
        # BUG FIX: a stale call (VC ended / assistant kicked or disconnected)
        # is an expected state, not a crash — clean up and tell the caller
        # instead of spamming the error-log channel with NotInCallError.
        if _is_not_in_call(exc):
            LOGGER.info("pause_stream: not in call for %s — clearing state", chat_id)
            _forget_call_state(chat_id)
            return False
        current = get_current(chat_id)
        await send_error_log(
            f"pause_stream failed in {chat_id}",
            exc,
            context={
                "chat_id": chat_id,
                "song_title": current.title if current else None,
            },
        )
        return False


async def resume_stream(chat_id: int) -> bool:
    """Resume playback. Returns False if nothing is actually playing."""
    if not _pytgcalls or not _active.get(chat_id):
        _forget_call_state(chat_id)
        return False
    try:
        await asyncio.wait_for(_pytgcalls.resume(chat_id), timeout=_CONTROL_RPC_TIMEOUT)
        return True
    except Exception as exc:
        if _is_not_in_call(exc):
            LOGGER.info("resume_stream: not in call for %s — clearing state", chat_id)
            _forget_call_state(chat_id)
            return False
        current = get_current(chat_id)
        await send_error_log(
            f"resume_stream failed in {chat_id}",
            exc,
            context={
                "chat_id": chat_id,
                "song_title": current.title if current else None,
            },
        )
        return False


async def skip_stream(chat_id: int):
    """Skip the current track **immediately**.

    BUG FIX ("skip karne pe jaldi skip nahi hota"): while a track was still
    being resolved/downloaded, `chat_id` sat in `_resolving`, so `_play_next()`
    returned early and the skip appeared to do nothing until the slow download
    finished (sometimes tens of seconds). A skip is an explicit user intent and
    must always win: we invalidate the in-flight resolve by bumping the stream
    generation (every stage of `_stream_track` checks `_is_stale_generation`
    and aborts) and drop the `_resolving` guard before advancing the queue.
    """
    current = get_current(chat_id)
    if current:
        try:
            from melody.core.ytdl import cancel_download
            cancel_download(
                current.video_id,
                audio_only=not bool(getattr(current, "video", False)),
                owner=chat_id,
            )
        except Exception:
            pass
    _cancel_stale_download(chat_id, "__control_skip__", False)
    _resolving.pop(chat_id, None)
    _begin_generation(chat_id)
    await _play_next(chat_id)


async def stop_stream(chat_id: int):
    """Stop playback, clear queue, leave voice chat — in that order.

    Ordering matters: the chat is marked as "leaving" BEFORE the disconnect so
    the StreamEnded the disconnect itself produces can never re-trigger
    AutoPlay / next-track (the reported "/stop and /leave don't work, bot
    comes back" bug). State is cleared even when the call was already gone.
    """
    # CONFUSION FIX ("skip/end/stop krne pe bot confuse hota hai"): a track
    # that was still resolving/downloading when /stop arrived used to survive
    # the stop — _stream_track only noticed the leave AFTER it started
    # playing (log: "stream started ... during a leave — disconnecting"), so
    # the bot appeared to ignore /stop and come back on its own. Minting a
    # new generation + dropping the resolving guard here makes every
    # in-flight play/skip abort at its next staleness check, BEFORE it can
    # touch py-tgcalls, so /stop is final the instant it is issued.
    current = get_current(chat_id)
    if current:
        try:
            from melody.core.ytdl import cancel_download
            cancel_download(
                current.video_id,
                audio_only=not bool(getattr(current, "video", False)),
                owner=chat_id,
            )
        except Exception:
            pass
    _cancel_stale_download(chat_id, "__control_stop__", False)
    _resolving.pop(chat_id, None)
    _begin_generation(chat_id)
    _mark_leaving(chat_id)
    clear_queue(chat_id, persist=False)
    set_current(chat_id, None)
    _forget_call_state(chat_id)
    await _hard_leave(chat_id)
    from utils.playback_state import mark_inactive
    await mark_inactive(chat_id, clear=True)


async def recover_playback() -> int:
    """Rejoin calls that were active at shutdown and continue near last position."""
    from melody.core.queue import restore_snapshot
    from utils.playback_state import load_snapshots, mark_inactive

    recovered = 0
    for snapshot in await load_snapshots(active_only=True):
        chat_id = snapshot.get("chat_id")
        current = snapshot.get("current")
        if not chat_id or not current:
            continue
        try:
            restore_snapshot(snapshot)
            track = get_current(int(chat_id))
            if track is None:
                continue
            saved_position = max(0, int(snapshot.get("position", 0)))
            speed = max(0.5, min(2.0, float(snapshot.get("speed", 1.0))))
            _speed[int(chat_id)] = speed
            # Queue/volume writes do not move this timestamp. Account for the
            # track's actual rate when continuing after downtime.
            position_updated_at = int(snapshot.get(
                "position_updated_at", snapshot.get("updated_at", time.time())
            ))
            downtime = max(0, int(time.time()) - position_updated_at)
            position = saved_position + int(downtime * speed)
            if track.duration:
                position = min(position, max(track.duration - 2, 0))
            await _stream_track(
                int(chat_id), track,
                video=bool(snapshot.get("video", getattr(track, "video", False))),
                start_at=position,
                priority=30,
            )
            recovered += 1
        except Exception as exc:
            LOGGER.warning("Playback recovery failed for %s: %s", chat_id, exc)
            await mark_inactive(int(chat_id))
    return recovered


def get_playback_position(chat_id: int) -> int:
    """Best-effort elapsed seconds into the current track (baked-in seek
    offset + wall-clock time since the stream last started/was sought).
    Returns 0 if nothing is playing.
    """
    if chat_id not in _play_start_time:
        return 0
    # BUG FIX: at 1.5x the track advances 1.5s per wall-clock second, so
    # /seekback and the progress readout were wrong whenever /speed was used.
    played = (time.time() - _play_start_time[chat_id]) * get_speed(chat_id)
    elapsed = _seek_offset.get(chat_id, 0) + int(played)
    track = get_current(chat_id)
    if track and track.duration:
        elapsed = min(elapsed, track.duration)
    return max(elapsed, 0)


async def seek_stream(
    chat_id: int, seconds: int, *, prefer_local: bool = False,
) -> int:
    """Seek to an absolute position (in seconds) into the currently playing
    track.

    py-tgcalls 2.1.1 has no native seek/time-offset API (no seek_stream, no
    set_time), so real seeking is implemented by re-issuing play() with a
    fresh MediaStream whose ffmpeg command carries an input-side `-ss
    <seconds>` — this makes ffmpeg start decoding from that offset instead
    of from the beginning. Because play() on an already-active call swaps
    the stream in place (see _stream_track's docstring), this behaves like a
    real seek from the listener's perspective.

    Returns the resulting position in seconds. Raises RuntimeError if
    nothing is currently playing in this chat.
    """
    track = get_current(chat_id)
    if not track or not _pytgcalls or not _active.get(chat_id):
        raise RuntimeError("Nothing is playing right now.")

    seconds = max(0, int(seconds))
    if track.duration:
        seconds = min(seconds, max(track.duration - 1, 0))

    from melody.core.ytdl import cached_file_path, download_audio

    video = _is_video.get(chat_id, False)

    # Recovery after a premature direct-CDN end must not immediately retry the
    # same signed URL. Prefer the retained completed fallback file; if it is
    # still downloading, wait for that one job to finish and then seek locally.
    local_path = cached_file_path(track.video_id, audio_only=not video) if prefer_local else None
    if prefer_local and not local_path:
        # Recovery must never receive the growing-file early handoff. A
        # direct CDN stream that ended at 0s needs a complete, seekable
        # local file; accepting an `.early` path here would immediately
        # reproduce the same EOF and trigger another StreamEnded cycle.
        local_path = await download_audio(
            track.video_id,
            audio_only=not video,
            allow_early=False,
        )

    stream = None
    if local_path:
        stream = _local_media_stream(chat_id, local_path, video, seconds)
    else:
        # Normal /seek keeps the low-latency direct path.
        stream = await _build_direct_stream(chat_id, track, video, seconds)
        if stream is None:
            local_path = await download_audio(track.video_id, audio_only=not video)
            stream = _local_media_stream(chat_id, local_path, video, seconds)

    _silence_playing.pop(chat_id, None)
    try:
        await _pytgcalls.play(chat_id, stream)
    except Exception as play_exc:
        # Same ffprobe/dead-CDN-URL failure as in _stream_track (see
        # _is_probe_error): /seek must not die just because the resolved
        # googlevideo link went stale mid-song — re-seek off a local file.
        if local_path is not None or not _is_probe_error(play_exc):
            raise
        LOGGER.warning(
            "Seek: direct CDN stream unusable for %s in %s (%s) — downloading.",
            track.video_id, chat_id, type(play_exc).__name__,
        )
        local_path = await download_audio(track.video_id, audio_only=not video)
        stream = _local_media_stream(chat_id, local_path, video, seconds)
        await _pytgcalls.play(chat_id, stream)
    _active[chat_id] = True
    _seek_offset[chat_id] = seconds
    _play_start_time[chat_id] = time.time()
    from utils.playback_state import save_snapshot
    await save_snapshot(
        chat_id, get_current(chat_id), get_queue(chat_id), get_loop(chat_id), get_volume(chat_id),
        active=True, position=seconds, video=video, speed=get_speed(chat_id),
    )
    return seconds


async def change_volume(chat_id: int, volume: int, _retry: bool = False) -> bool:
    """Returns True on success, False on failure — so callers (e.g.
    /volume, /mute, /unmute) can tell the user the truth instead of always
    replying "Volume set" even when nothing actually happened.

    BUG FIX (GROUPCALL_FORBIDDEN): this Telegram error means the assistant
    isn't currently a recognized participant of the chat's voice call — the
    exact same recoverable situation _stream_track already handles via
    _auto_join_assistant() for ChannelInvalid/PeerIdInvalid/etc. Previously
    change_volume() only logged this to LOG_GROUP_ID and gave up, so a
    fixable "assistant not in the call" state looked like a dead end from
    /volume even though /play's auto-join would have fixed it.
    """
    set_volume_local(chat_id, volume)
    try:
        await _pytgcalls.change_volume_call(chat_id, volume)
        return True
    except (ChannelInvalid, ChannelPrivate, PeerIdInvalid, UserBannedInChannel) as exc:
        if not _retry and await _auto_join_assistant(chat_id):
            return await change_volume(chat_id, volume, _retry=True)
        current = get_current(chat_id)
        await send_error_log(
            f"change_volume failed in {chat_id}",
            exc,
            context={
                "chat_id": chat_id,
                "song_title": current.title if current else None,
            },
        )
        return False
    except Exception as exc:
        if not _retry and "GROUPCALL_FORBIDDEN" in str(exc) and await _auto_join_assistant(chat_id):
            return await change_volume(chat_id, volume, _retry=True)
        current = get_current(chat_id)
        await send_error_log(
            f"change_volume failed in {chat_id}",
            exc,
            context={
                "chat_id": chat_id,
                "song_title": current.title if current else None,
            },
        )
        return False


async def set_playback_speed(chat_id: int, speed: float) -> bool:
    """Apply playback speed (0.5–2.0) to the CURRENT track immediately.

    BUG FIX: /speed used to be a pure placeholder — it replied "Playback
    speed set" and changed nothing at all, in this track or the next one.
    The speed is now baked into the ffmpeg command (atempo / itsscale) and
    re-issued from the current playback position, so it takes effect right
    away and stays applied to every following track until /stop or 1.0x.
    """
    speed = round(float(speed), 2)
    if not (0.5 <= speed <= 2.0):
        return False
    if not get_current(chat_id) or not _pytgcalls or not _active.get(chat_id):
        return False

    position = get_playback_position(chat_id)
    _speed[chat_id] = speed
    try:
        await seek_stream(chat_id, position)
        return True
    except Exception as exc:
        if _is_not_in_call(exc):
            _forget_call_state(chat_id)
            return False
        await send_error_log(
            f"set_playback_speed failed in {chat_id}",
            exc,
            context={"chat_id": chat_id, "speed": speed},
        )
        return False


async def mute_stream(chat_id: int) -> bool:
    """Mute using py-tgcalls' native mute() instead of volume 0.

    BUG FIX: /mute used to call change_volume(chat_id, 0) and /unmute
    hard-reset the volume to 100 — so unmuting silently threw away whatever
    volume the chat had set with /volume, and the "muted" volume of 0 was
    also persisted as the chat's real volume.
    """
    if not _pytgcalls or not _active.get(chat_id):
        return False
    try:
        await _pytgcalls.mute(chat_id)
        _muted[chat_id] = True
        return True
    except Exception as exc:
        if _is_not_in_call(exc):
            _forget_call_state(chat_id)
            return False
        await send_error_log(f"mute_stream failed in {chat_id}", exc, context={"chat_id": chat_id})
        return False


async def unmute_stream(chat_id: int) -> bool:
    """Unmute and keep the chat's own volume setting intact."""
    if not _pytgcalls or not _active.get(chat_id):
        return False
    try:
        await _pytgcalls.unmute(chat_id)
        _muted.pop(chat_id, None)
        return True
    except Exception as exc:
        if _is_not_in_call(exc):
            _forget_call_state(chat_id)
            return False
        await send_error_log(f"unmute_stream failed in {chat_id}", exc, context={"chat_id": chat_id})
        return False


def is_muted(chat_id: int) -> bool:
    return bool(_muted.get(chat_id))


def is_active(chat_id: int) -> bool:
    return bool(_active.get(chat_id))


def is_video_active(chat_id: int) -> bool:
    """Whether the call currently connected for `chat_id` was joined with
    video capability. Used by AutoPlay to keep replaying in the same mode
    the call was actually negotiated in, instead of silently guessing.
    """
    return bool(_is_video.get(chat_id))


async def get_participants(chat_id: int) -> list:
    try:
        return await _pytgcalls.get_participants(chat_id)
    except Exception:
        return []


# ── Auto-leave watchdog ───────────────────────────────────────────────────
#
# BUG FIX (reported: "VC left koi work na kar raha"): nothing ever made the
# assistant hang up on its own. When the last human left the voice chat, or
# the call sat paused/idle with an empty queue, the assistant stayed connected
# forever — burning a dyno slot and making a later /play join a stale call
# that PyTgCalls answers with ConnectionNotFound. This watchdog leaves the
# call (through the same stop_stream() path /stop uses, so queue + persisted
# state are cleared too) once the VC has been empty, or completely idle, for
# _AUTO_LEAVE_AFTER seconds.
AUTO_LEAVE_AFTER = int(os.environ.get("AUTO_LEAVE_AFTER", "60") or 60)
_AUTO_LEAVE_POLL = 20
_empty_since: dict = {}


def _is_playing_track(chat_id: int) -> bool:
    """True while a REAL track (not the silence placeholder) is streaming."""
    if not _active.get(chat_id):
        return False
    if _silence_playing.get(chat_id):
        return False
    try:
        from melody.core.queue import get_current, get_queue

        return bool(get_current(chat_id) or get_queue(chat_id))
    except Exception:  # noqa: BLE001
        return True


async def _vc_is_empty(chat_id: int) -> bool:
    """True when nobody except our own assistant is left in the voice chat."""
    try:
        participants = await get_participants(chat_id)
    except Exception:  # noqa: BLE001
        return False
    if not participants:
        # ROOT FIX ("bot bich me hi VC se chala jata hai"): PyTgCalls returns
        # [] both for "really empty" and for "roster unavailable" (peer not
        # cached, transient MTProto error). Treating that as empty hung up on
        # a live song mid-playback. An empty answer is only trusted when
        # nothing is actually being streamed right now.
        if _is_playing_track(chat_id):
            return False
        return bool(_active.get(chat_id))
    assistant_id = await _get_assistant_id()
    for participant in participants:
        pid = getattr(participant, "user_id", None) or getattr(participant, "id", None)
        if pid and pid != assistant_id:
            return False
    return True


async def auto_leave_watchdog() -> None:
    """Background task: hang up on empty / idle voice chats."""
    if AUTO_LEAVE_AFTER <= 0:
        LOGGER.info("Auto-leave disabled (AUTO_LEAVE_AFTER=0).")
        return
    while True:
        await asyncio.sleep(_AUTO_LEAVE_POLL)
        for chat_id in list(_active.keys()):
            try:
                if _is_leaving(chat_id):
                    _empty_since.pop(chat_id, None)
                    continue
                # Listener-mode presence exists only to mirror the in-call
                # chat; it plays nothing and must not be auto-hung-up.
                # melody/core/vc_listener.py drops it when the VC really ends.
                if _listener_mode.get(chat_id):
                    _empty_since.pop(chat_id, None)
                    continue
                # Music is playing: listeners can be silent/hidden, never
                # hang up on an active track.
                if _is_playing_track(chat_id):
                    _empty_since.pop(chat_id, None)
                    continue
                idle = await _vc_is_empty(chat_id)
                if not idle:
                    _empty_since.pop(chat_id, None)
                    continue
                since = _empty_since.setdefault(chat_id, time.monotonic())
                if time.monotonic() - since < AUTO_LEAVE_AFTER:
                    continue
                _empty_since.pop(chat_id, None)
                LOGGER.debug("Auto-leaving empty voice chat %s", chat_id)
                await stop_stream(chat_id)
            except Exception as exc:  # noqa: BLE001 — watchdog must never die
                LOGGER.warning("auto_leave_watchdog failed for %s: %s", chat_id, exc)
