"""
🤖 AutoPlay logic — fetch related videos from history AND pre-download the
predicted next track ahead of time.

REQUIREMENT: "/autoplay on hote hi next jo bhi song rahega pehle hi download
krke rakh" — as soon as a track is live and AutoPlay is on for that chat,
predict what AutoPlay would play next and download its audio into /tmp in the
background. By the time the current song actually ends, try_autoplay() has
nothing left to wait on — the file is already sitting on disk.
"""
import html
import asyncio
from pyrogram import enums
from melody.logging import LOGGER, send_error_log, log_activity
from melody.core.ytdl import get_related_videos
from melody.core.queue import (
    Track, set_current, set_predownloaded, pop_predownloaded,
    peek_predownloaded,
)
from utils.database import get_history, add_history

# chat_id → asyncio.Lock; stops two concurrent callers (call.py's
# _prefetch_upcoming and try_autoplay's own follow-up task) from running the
# same related-videos lookup + download twice, which is why the logs showed
# every "InnerTube related fallback" line duplicated.
_prefetch_locks: dict = {}

# chat_id → list of video ids whose AutoPlay playback died immediately. They
# are excluded from future suggestion picks so one unplayable video can never
# stall the whole AutoPlay chain again (root cause of "⚠️ AutoPlay rok diya").
_failed_ids: dict = {}
_FAILED_MEMORY = 12

# chat_id → list of video ids AutoPlay already played/queued in this session.
#
# BUG FIX ("autoplay me ek hi song kabhi kabhi repeat bajta hai back to back"):
# the only de-dup source used to be the MongoDB play history, which is written
# AFTER playback starts (`add_history` in try_autoplay), while the next pick is
# predicted the moment the track starts. That race meant the just-started song
# was frequently still absent from the exclude list, and since the pick was
# always `related[0]` — the most stable entry of YouTube's mix — AutoPlay chose
# the very same video again, back to back. Now every pick is remembered
# in-process the instant it is made, and picks are chosen from the top few
# candidates instead of always the first one.
_recent_ids: dict = {}
_RECENT_MEMORY = 30


def remember_played(chat_id: int, video_id: str) -> None:
    if not video_id:
        return
    ids = _recent_ids.setdefault(chat_id, [])
    if video_id in ids:
        ids.remove(video_id)
    ids.append(video_id)
    del ids[:-_RECENT_MEMORY]


def recent_ids(chat_id: int) -> list:
    return list(_recent_ids.get(chat_id, []))


def remember_failed(chat_id: int, video_id: str) -> None:
    if not video_id:
        return
    ids = _failed_ids.setdefault(chat_id, [])
    if video_id in ids:
        return
    ids.append(video_id)
    del ids[:-_FAILED_MEMORY]

# REQUIREMENT: "Autoplay me 5 min se badi audios play mat karna".
import os as _os
from utils.tasks import spawn

# Keep autoplay song-oriented. Related-video responses sometimes contain
# multi-hour mixes/live recordings; allowing those into the queue causes a
# cold full download and can look like a playback failure. The limit remains
# configurable for groups that explicitly want longer media.
def _autoplay_duration_limit() -> int:
    """Read the duration cap without allowing a malformed env var to crash boot."""
    raw = (_os.environ.get("AUTOPLAY_MAX_DURATION") or "").strip()
    try:
        value = int(raw) if raw else 600
    except (TypeError, ValueError):
        value = 600
    return max(1, value)


AUTOPLAY_MAX_DURATION = _autoplay_duration_limit()


def _cloud_prefetch_enabled() -> bool:
    """Enable next-track warming by default; allow a deploy-time opt-out.

    The priority gate gives interactive /play requests precedence and the
    playback layer cancels only lower-priority downloads owned by the chat, so
    enabling this does not turn a small cloud worker into a serial queue.
    """
    raw = (_os.environ.get("AUTOPLAY_PREDOWNLOAD") or "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on"}
    return True


def _candidate_duration(candidate) -> int:
    """Parse related duration defensively; malformed values are unknown."""
    try:
        value = int(candidate.get("duration") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _get_prefetch_lock(chat_id: int) -> asyncio.Lock:
    lock = _prefetch_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _prefetch_locks[chat_id] = lock
    return lock


async def _pick_related_track(chat_id: int) -> "Track | None":
    """Ask history + YouTube's related-videos graph for the next AutoPlay pick.

    SEED (requirement): YouTube's own autoplay chain — the suggestions of the
    song that just played (current track), so the mix keeps moving forward
    exactly like YouTube's "Autoplay" toggle does.

    BUG FIX ("autoplay button hota hai uske baad kuch nhi hota"):
    This used to take the top related video's URL and feed it BACK through
    get_video_info() — a redundant search round-trip. get_video_info() runs
    the query through yt-dlp's ytsearch1: path, and when the InnerTube/
    Invidious fallback kicks in, the returned ``id`` / ``webpage_url`` can
    be the raw search query text instead of a clean 11-char YouTube video ID.
    get_related_videos() already returns id, title, duration, url, thumbnail
    and uploader, so the Track is built directly from that data.
    """
    from melody.core.queue import get_current, get_last_user_track

    history = await get_history(chat_id)
    exclude_ids = [h["id"] for h in history] if history else []
    # Never re-pick a candidate that already failed to play in this chat, one
    # AutoPlay already played in this session, the track playing right now, or
    # the one already sitting in the pre-download slot (the back-to-back
    # repeat came from exactly these gaps).
    blocked = [*exclude_ids, *_failed_ids.get(chat_id, []), *_recent_ids.get(chat_id, [])]
    _cur = get_current(chat_id)
    if _cur and getattr(_cur, "video_id", None):
        blocked.append(_cur.video_id)
    _pre = peek_predownloaded(chat_id)
    if _pre and getattr(_pre, "video_id", None):
        blocked.append(_pre.video_id)
    exclude_ids = [v for v in dict.fromkeys(blocked) if v]

    # SEED PRIORITY (requirement: "YouTube ka autoplay use karna — jo last
    # played song rahega uska hi next YouTube ka bajana"):
    # AutoPlay must behave exactly like YouTube's own autoplay, which always
    # continues the mix of the song that JUST PLAYED. So the seed is the track
    # currently/last playing in this chat — whether a human queued it or
    # AutoPlay itself picked it. Previously the seed was pinned to the last
    # HUMAN request, so every AutoPlay pick kept asking YouTube for the same
    # song's mix and the chain never moved forward with the music.
    current = get_current(chat_id)
    last_id = current.video_id if current and current.video_id else None
    # Fall back to the last human pick, then to the newest history entry, for
    # the case where nothing is loaded as "current" (e.g. queue just drained).
    if not last_id:
        last_id = get_last_user_track(chat_id)
    if not last_id and history:
        last_id = history[-1]["id"]
    if not last_id:
        return None

    # Tagged Telegram media ("tg<chat>_<msg>") is not a YouTube video, so it
    # can never seed the related-videos graph. Fall back to the newest real
    # YouTube id in history instead of firing a doomed lookup.
    from melody.core.ytdl import is_tg_media_id

    if is_tg_media_id(last_id):
        last_id = next(
            (h["id"] for h in reversed(history or []) if not is_tg_media_id(h.get("id", ""))),
            None,
        )
        if not last_id:
            return None

    related = await get_related_videos(last_id, exclude_ids=exclude_ids)
    if not related:
        return None
    # get_related_videos() only filters what the extractor gives it, and the
    # InnerTube fallback path can still echo an already-played id back — so
    # re-apply the full exclude set locally before choosing.
    _blocked_set = set(exclude_ids)
    related = [
        c for c in related
        if (c.get("id") or "") and (c.get("id") or "") not in _blocked_set
    ]
    if not related:
        return None


    # REQUIREMENT ("Autoplay me 5 min se badi audios play mat karna"):
    # AutoPlay picks from YouTube's own suggestion graph, which loves hour-long
    # jukebox/live mixes. Anything longer than AUTOPLAY_MAX_DURATION is skipped
    # so AutoPlay only ever queues real songs. Candidates with an unknown
    # duration (flat suggestion entries often carry none) are kept — the real
    # duration is checked again in prefetch_next() once metadata is known.
    playable = [
        c for c in related
        if not _candidate_duration(c)
        or _candidate_duration(c) <= AUTOPLAY_MAX_DURATION
    ]
    if not playable:
        LOGGER.info(
            "AutoPlay: every suggestion for %s was longer than %ds — skipping",
            last_id, AUTOPLAY_MAX_DURATION,
        )
        return None
    related = playable

    # Take YouTube's OWN next-up order (requirement: "YouTube ka autoplay
    # use karna"): related[] is the RD<last-played-id> mix / InnerTube
    # "up next" list in YouTube's ranking, and the exclude filtering above
    # already removed everything played recently — so the head of the list is
    # literally what YouTube would autoplay next, with no back-to-back repeat.
    top = related[0]
    vid = top.get("id") or ""
    # Defensive guard: a valid YouTube video ID is exactly 11 chars of
    # [A-Za-z0-9_-]. If the related-videos source returned anything else
    # (a search query leaking through, an empty string, etc.), skip it
    # instead of letting a garbage ID reach download_audio().
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        LOGGER.warning("AutoPlay: skipping invalid related video_id %r", vid)
        # Try the next candidate if the first was bad.
        for cand in related[1:]:
            cand_vid = cand.get("id") or ""
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", cand_vid):
                top = cand
                vid = cand_vid
                break
        else:
            return None

    # "AutoPlay song ka full details do jaise manual wale ka deta hai":
    # yt-dlp's flat "up next" entries (and InnerTube's compact renderers)
    # often carry only an id + title — no uploader, no duration — which is
    # why the AutoPlay card showed "Unknown" and "00:00". Fill the gaps with
    # a cheap, cached per-ID metadata lookup so the AutoPlay card is exactly
    # as rich as a manual /play card.
    title    = top.get("title") or "Unknown"
    duration = int(top.get("duration") or 0)
    uploader = top.get("uploader") or "Unknown"
    thumbnail = top.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    if title == "Unknown" or not duration or uploader == "Unknown":
        try:
            from melody.core.ytdl import get_video_details
            details = await get_video_details(vid)
        except Exception:
            details = None
        if details:
            if title == "Unknown":
                title = details.get("title") or title
            if not duration:
                duration = int(details.get("duration") or 0)
            if uploader == "Unknown":
                uploader = details.get("uploader") or uploader
            thumbnail = details.get("thumbnail") or thumbnail

    return Track(
        video_id=vid,
        title=title,
        duration=duration,
        stream_url="",
        thumbnail=thumbnail,
        uploader=uploader,
        requester_id=0,
        requester_name="AutoPlay",
        requested_in=chat_id,
    )


async def prefetch_next(chat_id: int) -> "Track | None":
    if _os.environ.get("AUTOPLAY_PREDOWNLOAD", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    """
    Predict + pre-download the next AutoPlay track for `chat_id`.

    Called right after a track starts playing (from call.py) whenever the
    manual queue is empty and AutoPlay is ON for that chat, AND right when
    a chat turns AutoPlay on (see autoplay_cmd.py) — "jese hi on hoga next
    song queue me add + download". Safe / cheap to call repeatedly — it
    no-ops if a pre-download is already queued. Returns the track (already
    cached to /tmp) so callers can also surface it in the visible queue.
    """
    try:
        cached = peek_predownloaded(chat_id)
        if cached:
            return cached  # already have the next one cached

        async with _get_prefetch_lock(chat_id):
            cached = peek_predownloaded(chat_id)
            if cached:
                return cached
            return await _prefetch_next_locked(chat_id)
    except Exception as exc:
        # ROOT-CAUSE FIX (⚠️ "prefetch_next failed in -100…" error cards):
        # prefetching is a pure OPTIMISATION — the track it failed to warm up
        # is downloaded again (with the full retry ladder) the moment AutoPlay
        # actually needs it, so a failure here breaks nothing for the user. It
        # used to raise a full error card into the owner's log on every flaky
        # CDN hiccup. Now it is logged locally and retried once with the NEXT
        # suggestion instead of shouting.
        LOGGER.warning("AutoPlay prefetch failed in %s: %s", chat_id, exc)
        try:
            async with _get_prefetch_lock(chat_id):
                if peek_predownloaded(chat_id):
                    return peek_predownloaded(chat_id)
                return await _prefetch_next_locked(chat_id)
        except Exception as retry_exc:  # noqa: BLE001 — still non-fatal
            LOGGER.warning(
                "AutoPlay prefetch retry also failed in %s: %s", chat_id, retry_exc
            )
        return None


async def _prefetch_next_locked(chat_id: int) -> "Track | None":
    """Body of prefetch_next(), guarded by the per-chat prefetch lock."""
    track = await _pick_related_track(chat_id)
    if not track:
        return None

    # REQUIREMENT ("Autoplay me 5 min se badi audios play mat karna"): the
    # suggestion entry may have carried no duration at all, so re-check it
    # here — by now _pick_related_track() has filled in real metadata.
    track_duration = _candidate_duration({"duration": track.duration})
    if track_duration > AUTOPLAY_MAX_DURATION:
        LOGGER.info(
            "AutoPlay: skipping %r (%ds > %ds cap)",
            track.title, track_duration, AUTOPLAY_MAX_DURATION,
        )
        return None

    # REQUIREMENT ("Pre download system bitha taki next instant play ho"):
    # AutoPlay now genuinely PRE-DOWNLOADS the predicted next track to /tmp
    # (and warms its CDN urls as a cheap side-benefit), so when the current
    # song ends the file is already on disk and _stream_track()'s cache-hit
    # fast path starts it instantly instead of waiting for yt-dlp.
    #
    # MODE (requirement "vplay hoga to bs vplay autoplay me, play hoga to bs
    # play"): the pre-download uses exactly the mode the chat is playing in —
    # a /vplay session pre-downloads video, a /play session audio only.
    from melody.core.call import is_active, is_video_active
    from melody.core.ytdl import resolve_stream_urls, download_audio, on_cloud_host
    from melody.core.queue import get_last_user_mode

    if is_active(chat_id):
        want_video = is_video_active(chat_id)
    else:
        want_video = get_last_user_mode(chat_id) or bool(getattr(track, "video", False))
    track.video = want_video

    # Interactive requests have higher priority than this prefetch and may
    # cancel it when necessary; keeping the warm file is worth the small worker
    # cost because the next AutoPlay transition starts immediately.
    if on_cloud_host() and not _cloud_prefetch_enabled():
        # Deployments with tight memory can explicitly disable full prefetch.
        remember_played(chat_id, track.video_id)
        set_predownloaded(chat_id, track)
        LOGGER.info(
            "AutoPlay: cloud pre-download skipped for %s to protect interactive playback",
            track.video_id,
        )
        return track

    # SPEED FIX (logs: "AutoPlay: stream-url warm failed … no directly
    # streamable http format found", 5-8s before the pre-download even
    # started): on cloud hosts _stream_track() never uses the direct-CDN
    # path at all, so warming those URLs was pure dead time on the critical
    # path to the next song. Only warm it where it can actually be used.
    from melody.core.ytdl import on_cloud_host, should_try_direct_stream

    if should_try_direct_stream() and not on_cloud_host():
        try:
            await resolve_stream_urls(track.video_id, want_video=want_video)
        except Exception as exc:
            LOGGER.info(
                "AutoPlay: stream-url warm failed for %s (%s) — will resolve at play time.",
                track.video_id, exc,
            )

    # The actual pre-download. Non-fatal on failure: playback re-tries with
    # the full retry ladder when the track is really needed.
    try:
        path = await download_audio(
            track.video_id, audio_only=not want_video, priority=50, owner=chat_id,
        )
        if path:
            from melody.core.call import _persist_completed_song
            upcoming = track
            await _persist_completed_song(path, upcoming)
            LOGGER.info("AutoPlay: pre-downloaded %s → instant next play", track.video_id)
    except Exception as exc:
        LOGGER.info("AutoPlay: pre-download failed for %s (%s)", track.video_id, exc)

    remember_played(chat_id, track.video_id)
    set_predownloaded(chat_id, track)
    return track


# chat_id → (timestamp of last AutoPlay start, video_id, consecutive fails).
#
# ROOT-CAUSE FIX ("⚠️ AutoPlay rok diya — lagatar tracks play hone se pehle hi
# end ho rahe the"): the old guard counted every short-lived track as a
# generic failure and, after 3 of them, KILLED AutoPlay for the chat until
# somebody ran /play again. In practice the short life came from ONE
# unplayable suggestion (dead CDN url / region-locked video / peer error)
# that the chain kept re-picking, so a single bad video permanently disabled
# AutoPlay. Now:
#   • the video that died is blacklisted (see remember_failed) so the next
#     pick is a genuinely different song,
#   • the pre-download cache for it is dropped,
#   • AutoPlay is never killed — after a burst of failures it just cools down
#     for _AUTOPLAY_COOLDOWN seconds and resumes on its own.
_last_autoplay: dict = {}
_autoplay_cooldown: dict = {}
_AUTOPLAY_MIN_ALIVE = 20.0   # seconds a track must survive to count as "played"
_AUTOPLAY_MAX_FAILS = 4
_AUTOPLAY_COOLDOWN = 60.0    # short breather, then AutoPlay retries by itself


def reset_autoplay_guard(chat_id: int) -> None:
    """Clear the AutoPlay anti-spam counter (called on a fresh /play)."""
    _last_autoplay.pop(chat_id, None)
    _autoplay_cooldown.pop(chat_id, None)
    _failed_ids.pop(chat_id, None)
    # Keep the recently-played memory: a fresh /play must not make AutoPlay
    # forget what it just played, otherwise the repeat can come right back.


async def try_autoplay(chat_id: int) -> bool:
    """
    Play the next AutoPlay track for a chat — uses the pre-downloaded one
    if available, otherwise falls back to a fresh lookup+download.

    Returns True if a track started playing, False if there was nothing
    to play (caller should then actually leave the call).
    """
    try:
        import time as _time
        from melody.core.call import (
            _stream_track, is_active, is_video_active, _begin_generation,
            _resolving, _resolving_track, _resolving_origin,
            _is_stale_generation,
        )
        from melody.core.queue import get_last_user_mode, get_current

        cooling = _autoplay_cooldown.get(chat_id, 0.0)
        if cooling and _time.time() < cooling:
            LOGGER.info("AutoPlay in %s is cooling down for %.0fs more",
                        chat_id, cooling - _time.time())
            return False

        last_ts, last_vid, fails = _last_autoplay.get(chat_id, (0.0, None, 0))
        if last_ts and (_time.time() - last_ts) < _AUTOPLAY_MIN_ALIVE:
            # The previous AutoPlay pick died almost immediately — never pick
            # it (or anything already known-bad) again.
            fails += 1
            if last_vid:
                remember_failed(chat_id, last_vid)
                set_predownloaded(chat_id, None)
            if fails >= _AUTOPLAY_MAX_FAILS:
                _last_autoplay.pop(chat_id, None)
                _autoplay_cooldown[chat_id] = _time.time() + _AUTOPLAY_COOLDOWN
                LOGGER.warning(
                    "AutoPlay chain in %s died %d times — cooling down %.0fs, "
                    "then resuming automatically", chat_id, fails, _AUTOPLAY_COOLDOWN,
                )
                return False
        else:
            fails = 0

        track = pop_predownloaded(chat_id)
        if track and track.video_id in _failed_ids.get(chat_id, []):
            track = None
        # Never play the same video twice in a row, even if the pre-download
        # slot still holds it (e.g. it was warmed before the song played).
        current = get_current(chat_id)
        if track and current and track.video_id == getattr(current, "video_id", None):
            track = None
        if not track:
            track = await _pick_related_track(chat_id)
        if not track:
            return False
        # AutoPlay is a background transition. Reserve a generation before
        # resolving so a manual /play arriving at any await point immediately
        # makes this candidate stale and prevents it from reaching PyTgCalls.
        autoplay_gen = _begin_generation(chat_id)
        _resolving[chat_id] = autoplay_gen
        _resolving_track[chat_id] = (track.video_id, bool(track.video))
        _resolving_origin[chat_id] = "autoplay"

        # MODE (requirement "vplay hoga to bs vplay autoplay me, play hoga to
        # bs play hoga"): mirror the live call's mode, and when starting a
        # fresh call mirror the last HUMAN request's mode.
        if is_active(chat_id):
            video = is_video_active(chat_id)
        else:
            video = get_last_user_mode(chat_id) or bool(track.video)
        track.video = video
        _last_autoplay[chat_id] = (_time.time(), track.video_id, fails)

        set_current(chat_id, track)
        try:
            started = await _stream_track(
                chat_id, track, video=video, gen=autoplay_gen, priority=25,
            )
        finally:
            if _resolving.get(chat_id) == autoplay_gen:
                _resolving.pop(chat_id, None)
                _resolving_track.pop(chat_id, None)
                _resolving_origin.pop(chat_id, None)

        # A canceled/stale/failed AutoPlay attempt is not a played track. Do
        # not write history, send a success card, or return True (which would
        # make `_play_next` believe the VC has advanced).
        if started is not True or _is_stale_generation(chat_id, autoplay_gen):
            if _is_stale_generation(chat_id, autoplay_gen):
                LOGGER.info("AutoPlay candidate %s superseded in %s", track.video_id, chat_id)
            if not _is_stale_generation(chat_id, autoplay_gen):
                current = get_current(chat_id)
                if current is track or getattr(current, "video_id", None) == track.video_id:
                    set_current(chat_id, None)
            return False

        remember_played(chat_id, track.video_id)
        await add_history(chat_id, track.video_id, track.title)

        # 🎴 BUG FIX ("autoplay playing card add kr"): AutoPlay used to send
        # a plain text line — no thumbnail, no control buttons. Now it sends
        # the exact same rich playing card that /play sends (thumbnail +
        # song info + Pause/Skip/Stop/Queue/AutoPlay/Close buttons), so users
        # can control the AutoPlay track directly from the card.
        # NOTE: safe_title must be computed BEFORE the try/except below, since
        # the activity log line further down uses it on the success path too.
        safe_title = html.escape((track.title or "Unknown")[:60])
        try:
            from melody.core.playcard import send_playing_card
            from melody import bot
            chat_obj = await bot.get_chat(chat_id)
            await send_playing_card(
                bot,
                chat_obj,
                track,
                status_label="AutoPlay ▶️ (ᴀᴜᴛᴏ-ᴘɪᴄᴋᴇᴅ)",
                requester_mention="🤖 <i>AutoPlay</i>",
            )
        except Exception:
            # Fall back to the old plain text if the card send fails.
            from melody import bot

            try:
                await bot.send_message(
                    chat_id,
                    f"<blockquote>🎶 <b>AutoPlay ▶️</b> <code>{safe_title}</code>\n"
                    f"<i>Melody ne sunwaya!</i>\n"
                    f"🙋 Requested by: <i>AutoPlay</i></blockquote>",
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                pass
        spawn(log_activity(
            f"🤖 <b>AutoPlay Triggered</b>\n"
            f"• Song: <code>{safe_title}</code>\n"
            f"• Chat: <code>{chat_id}</code>"
        ))

        # Immediately line up (and download) the one after this too.
        spawn(prefetch_next(chat_id), name=f"autoplay-prefetch:{chat_id}")
        return True

    except Exception as exc:
        current = get_current(chat_id)
        await send_error_log(
            f"try_autoplay failed in {chat_id}",
            exc,
            context={
                "chat_id": chat_id,
                "song_title": current.title if current else None,
                "video_id": current.video_id if current else None,
            },
        )
        return False
