"""
▶️ /play and /vplay commands
BUG FIX: @error_handler moved OUTSIDE @admin_or_auth so DB errors are also caught
BUG FIX: Removed duplicate callback handlers (pause/resume/skip/stop/queue/lyrics
         are already registered in controls.py — having them here too caused
         duplicate handler registration and unpredictable behaviour)
BUG FIX: ENTITY_BOUNDS_INVALID — switched all dynamic-text messages to HTML
         parse_mode with html.escape() so song titles / uploader names that
         contain Markdown special characters (* _ ` [ etc.) never produce
         malformed entities.  Slicing a title string mid-Markdown-token was
         the direct trigger for [400 ENTITY_BOUNDS_INVALID].

⚡ FAST & RELIABLE /play (requirement #4):
   The bot now joins the voice chat (pre_join) in PARALLEL with the yt-dlp
   search instead of after it — the multi-second search is no longer on the
   critical path to "bot is in the call". As soon as the search resolves,
   the real track swaps in via change_stream (near-instant).

🔥 Animated status (requirement #5):
   The static "🔍 Searching..." message is replaced by AnimatedStatus, which
   cycles random fire/celebration emojis while the join+search race runs.
"""
import asyncio
import os
import time as _time
import html
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup
from melody import bot
from melody.logging import LOGGER
from melody.config import Config
from melody.core.ytdl import (
    download_replied_media,
    get_video_info,
    has_playable_media,
    tagged_media_limit_reason,
)
from melody.core.queue import set_last_user_track, Track, is_autoplay_on, get_queue
from melody.core.call import (
    play_stream,
    force_play_stream,
    abort_prejoin_if_idle,
    ensure_assistant_peer,
    pre_join,
    reset_playback_speed,
)
from melody.logging import log_activity
from utils.database import add_history
from utils.decorators import channel_admin_or_auth, error_handler, playmode_gate
from utils.formatters import format_duration, quote_html
from utils.thumbnails import make_thumbnail, fetch_dp, get_bot_dp, get_bot_identity
from utils.animation import AnimatedStatus
from utils.tasks import spawn


def _is_stale_message_error(exc: BaseException) -> bool:
    """Return True for Telegram errors caused by an already-gone message.

    Status messages are intentionally ephemeral: users, auto-cleanup, another
    handler, or a retry can delete them while a slow search/download is still
    running. These errors must never turn a successful playback request into a
    crash report.
    """
    name = type(exc).__name__
    text = str(exc).upper()
    return name in {"MessageIdInvalid", "MessageNotModified", "MessageToDeleteNotFound"} or any(
        marker in text for marker in ("MESSAGE_ID_INVALID", "MESSAGE_NOT_MODIFIED", "MESSAGE_TO_DELETE_NOT_FOUND")
    )


async def _safe_processing_edit(processing, fallback_message, text, **kwargs):
    """Edit the processing card, replying only when Telegram invalidated it."""
    try:
        return await processing.edit(text, **kwargs)
    except Exception as exc:  # Telegram versions expose different exception classes
        if not _is_stale_message_error(exc):
            raise
        if "MESSAGE_NOT_MODIFIED" in str(exc).upper() or type(exc).__name__ == "MessageNotModified":
            return processing
        try:
            return await fallback_message.reply(text, **kwargs)
        except Exception:
            LOGGER.debug("processing status message disappeared in chat=%s", getattr(getattr(fallback_message, "chat", None), "id", "?"), exc_info=True)
            return None


async def _safe_processing_delete(processing):
    """Best-effort cleanup that tolerates a status message deleted elsewhere."""
    try:
        return await processing.delete()
    except Exception as exc:
        if not _is_stale_message_error(exc):
            raise
        return None


# Strong references to background download tasks so they aren't GC'd
# before _stream_track picks them up via in-flight dedup in ytdl.py.
_bg_downloads: set = set()

def get_play_buttons(
    chat_title: str,
    autoplay_on: bool = False,
    bot_username: "str | None" = None,
    bot_name: str = "Melody",
    chat_type: "enums.ChatType | None" = None,
    chat_id: int = 0,
    paused: bool = False,
) -> InlineKeyboardMarkup:
    """Play-card keyboard — premium-emoji labels (aage + piche) and real
    coloured buttons, built by `utils.inline` (AnonXMusic-style)."""
    from utils.inline import inline

    return inline.play_card(
        chat_id=chat_id,
        chat_title=chat_title,
        autoplay_on=autoplay_on,
        paused=paused,
        bot_username=bot_username,
        bot_name=bot_name,
    )



async def _play_core(client: Client, message: Message, video: bool = False, force: bool = False, stream_chat=None):
    """`stream_chat` overrides WHERE the audio is streamed (used by
    /channelplay: command typed in a group, music plays in the linked
    channel's voice chat). Replies always stay in `message.chat`."""
    # Defensive mode guard: custom command patches or forwarded commands can
    # lose the handler's boolean argument. Derive the mode from the actual
    # command too, otherwise /vplay silently enters the audio-only path.
    command_name = ""
    if getattr(message, "command", None):
        command_name = str(message.command[0]).lower().split("@", 1)[0]
    video = bool(video or command_name in {"vplay", "cvplay", "vplayforce"})
    # Assistant session dead (e.g. 406 AUTH_KEY_DUPLICATED in the Heroku logs)?
    # Then no voice chat can ever be joined — say so clearly instead of letting
    # the request fail deep inside py-tgcalls after a long wait.
    import melody as _melody_pkg

    if not getattr(_melody_pkg, "ASSISTANT_READY", True):
        return await message.reply(
            "<blockquote>⚠️ <b>Aꜱꜱɪꜱᴛᴀɴᴛ ᴏғғʟɪɴᴇ</b>\n\n"
            "🔑 <b>STRING_SESSION</b> invalid ya duplicate hai, isliye voice chat "
            "join nahi ho sakta.\n"
            "🛠 Owner: naya session generate karke <code>STRING_SESSION</code> "
            "update karo, phir bot restart karo.</blockquote>",
            parse_mode=enums.ParseMode.HTML,
        )

    query = " ".join(message.command[1:]) if len(message.command) > 1 else None
    chat = stream_chat or message.chat
    user = message.from_user

    # A fresh user request must never inherit a stale `/speed 2` state.
    reset_playback_speed(chat.id)

    # 🏷 Tag-to-play: reply /play or /vplay to any audio/video/voice message
    # (or an audio/video document) to stream that exact file — no text query
    # needed. This takes priority over a text query when both are present.
    replied = message.reply_to_message
    tagged = has_playable_media(replied)
    if tagged:
        limit_reason = tagged_media_limit_reason(replied, video=video)
        if limit_reason:
            await message.reply(quote_html(limit_reason), parse_mode=enums.ParseMode.HTML)
            return

    if not query and not tagged:
        usage_cmd = "/cplay" if chat.type == enums.ChatType.CHANNEL else "/play"
        from melody.plugins.misc.pics import send_with_pic
        await send_with_pic(
            client, message.chat.id, "play",
            quote_html(
                f"🎵 <b>Song ka naam ya YouTube link bhejo</b>\n\n"
                f"<code>{usage_cmd} &lt;song name / URL&gt;</code>\n"
                f"Audio/video ko reply karke <code>{usage_cmd}</code> bhi chala sakte ho."
            ), reply_to=message.id,
        )
        return

    # 🔎 Inline play mode (/playmode inline) — instead of auto-playing the top
    # hit, show the 5 best matches as tappable buttons (Yukki/AnonX parity).
    # Only for a plain text query: links and tagged media are unambiguous, and
    # forcing a chooser on them would just add a pointless extra tap.
    if query and not tagged and not force and not query.lower().startswith(("http://", "https://")):
        try:
            from melody.plugins.music.playmode import get_search_mode
            _mode = await get_search_mode(chat.id)
        except Exception:
            _mode = "direct"
        if _mode == "inline":
            from melody.core.ytdl import search_youtube
            from utils.buttons import ikb
            from strings.themes import BLUE, RED, GREEN, btn

            picker = await message.reply(
                quote_html("🔎 <b>Searching...</b>"), parse_mode=enums.ParseMode.HTML
            )
            results = await search_youtube(query, limit=5)
            if not results:
                await picker.edit(
                    quote_html("❌ <b>Kuch match nahi hua.</b>"),
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            colors = [RED, BLUE, GREEN]
            text = f"🔎 <b>“{html.escape(query[:48])}”</b> — koi ek chuno 👇"
            buttons = [
                [ikb(btn(f"{i}. {r['title'][:30]}", colors[(i - 1) % len(colors)]),
                     callback_data=f"play_search_{r['id']}")]
                for i, r in enumerate(results[:5], 1) if r.get("id")
            ]
            await picker.edit(
                quote_html(text),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

    # Known YouTube links/IDs are unambiguous. Start their direct stream
    # resolver before metadata enrichment; resolve_stream_urls() has its own
    # single-flight lock/cache, so _warm_sources/_build_direct_stream reuse it.
    if query and not tagged:
        from melody.core.ytdl import extract_video_id, is_valid_video_id
        direct_id = extract_video_id(query) or (
            query.strip() if is_valid_video_id(query.strip()) else None
        )
        if direct_id:
            async def _warm_direct_source():
                try:
                    from melody.core.ytdl import resolve_stream_urls
                    await resolve_stream_urls(direct_id, want_video=video)
                except Exception as exc:
                    LOGGER.debug("early direct warm failed for %s: %s", direct_id, exc)

            spawn(_warm_direct_source(), name=f"direct-warm-{direct_id}")

        # A manual request owns the single safe yt-dlp slot. Stop only lower
        # priority background work; never cancel a same-video shared download
        # that this request can safely deduplicate onto.
        from melody.core.ytdl import cancel_lower_priority_downloads
        cancel_lower_priority_downloads(
            max_priority=0,
            exclude_video_id=direct_id,
        )

    if not (query and not tagged):
        from melody.core.ytdl import cancel_lower_priority_downloads
        cancel_lower_priority_downloads(max_priority=0)

    # Channel posts have no from_user — attribute the request to the channel
    # itself so requester_id/name/mention/dp fetching all stay well-defined.
    requester_id = user.id if user else chat.id
    requester_name = user.first_name if user else (chat.title or "Channel")
    requester_mention = user.mention if user else html.escape(requester_name)

    # ⏱ Stage timing instrumentation — the only reliable way to see WHERE the
    # "gaana 15 sec baad bajta hai" delay actually comes from (search vs.
    # extraction vs. VC join vs. PyTgCalls handoff). One compact log line.
    _t0 = _time.monotonic()
    try:
        # Cold YouTube CDN routes can legitimately need more than 10s across
        # metadata resolution and ffprobe startup. Keep one bounded deadline
        # shared with call._stream_track so the caller cannot cancel a healthy
        # playback handoff prematurely.
        _startup_budget = min(
            30.0, max(8.0, float(os.getenv("PLAY_STARTUP_DEADLINE", "20")))
        )
    except (TypeError, ValueError):
        _startup_budget = 20.0
    _startup_deadline = _t0 + _startup_budget

    def _lap() -> float:
        return round(_time.monotonic() - _t0, 2)

    # Start peer validation and the audio-only optimistic VC pre-join at the
    # same instant as metadata search. Both helpers are idempotent/single-flight;
    # _stream_track reuses the ready peer and swaps real audio into the silence
    # call instead of waiting to create the VC after YouTube resolves.
    spawn(ensure_assistant_peer(chat.id), name=f"peer-ready-{chat.id}")
    if not video:
        spawn(pre_join(chat.id), name=f"vc-prejoin-{chat.id}")

    # ⚡ SPEED FIX: the "Getting your vibe ready..." reply is a full Telegram
    # round-trip and it used to be AWAITED before the VC join and the search
    # even started — 300-600ms of pure dead time on every single /play. The
    # join, the profile-photo fetches, the status message AND the search all
    # start in the same instant now; the status message is only awaited later,
    # when we actually need to edit or delete it.
    # Audio-first policy is retained for video requests; audio requests use the
    # parallel silence pre-join above so the real MediaStream only swaps in.
    bot_dp_task = asyncio.create_task(get_bot_dp(client))
    user_dp_task = asyncio.create_task(fetch_dp(client, requester_id))

    if tagged:
        info_task = asyncio.create_task(download_replied_media(client, replied, video=video))
    else:
        info_task = asyncio.create_task(get_video_info(query))

    processing_task = asyncio.create_task(
        message.reply(
            quote_html("🔥 Getting your vibe ready..."), parse_mode=enums.ParseMode.HTML
        )
    )

    async def _start_anim():
        return AnimatedStatus(await processing_task, "Getting your vibe ready").start()

    anim_task = asyncio.create_task(_start_anim())
    anim = None

    try:
        # The status message is deliberately not awaited before playback:
        # Telegram UI round-trips are non-audio work and can add seconds on a
        # busy group. Keep the task running, but let search/stream/VC progress
        # as soon as metadata is available.
        info = await asyncio.wait_for(
            info_task, timeout=max(0.05, _startup_deadline - _time.monotonic())
        )
        _t_info = _lap()

        if not info:
            processing = await processing_task
            anim = await anim_task
            await abort_prejoin_if_idle(chat.id)
            await anim.stop()
            if tagged:
                await _safe_processing_edit(processing, message,
                    quote_html("❌ Ye tagged file play nahi ho payi 🌸"),
                    parse_mode=enums.ParseMode.HTML,
                )
                return

            # 🪄 Bare error ke bajaye "shayad aap ye dhundh rahe the" —
            # fuzzy-ranked related results, tap karke seedha play.
            suggestions = []
            try:
                from melody.core.query_fix import find_suggestions

                suggestions = await asyncio.wait_for(
                    find_suggestions(query, limit=5), timeout=8.0
                )
            except Exception as exc:
                LOGGER.debug("suggestion lookup failed for %r: %s", (query or "")[:40], exc)

            if suggestions:
                from utils.buttons import ikb
                from strings.themes import BLUE, RED, GREEN, btn

                colors = [RED, BLUE, GREEN]
                text = (
                    f"🔎 <b>“{html.escape((query or '')[:48])}”</b> exactly nahi mili 🌸\n"
                    "Shayad in me se koi chahiye? Tap karo aur baj jayegi 👇"
                )
                buttons = [
                    [
                        ikb(
                            btn(f"{i}. {r.get('title', 'Unknown')[:30]}", colors[(i - 1) % len(colors)]),
                            callback_data=f"play_search_{r['id']}",
                        )
                    ]
                    for i, r in enumerate(suggestions[:5], 1)
                    if r.get("id")
                ]
                await _safe_processing_edit(processing, message,
                    quote_html(text),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
                return

            await _safe_processing_edit(processing, message,
                quote_html(
                    "❌ <b>Kuch bhi match nahi hua</b> 🌸\n"
                    "Naam thoda alag likh ke ya artist ka naam jod ke try karo."
                ),
                parse_mode=enums.ParseMode.HTML,
            )
            return


        # ⚡ Kick off the audio download in parallel with the VC join — the
        # download is usually the slowest step, so starting it the instant
        # we have the video_id means the file is often already on disk by
        # the time join_task finishes, eliminating the download wait.
        # NOTE: We do NOT await the download here — _stream_track calls
        # download_audio() itself, and the in-flight dedup in ytdl.py
        # ensures this pre-started download is reused (not duplicated).
        # Awaiting the full download before play_stream() would block for
        # the entire download duration, defeating the early-handoff
        # mechanism that starts playback as soon as the first bytes land.
        # SPEED FIX ("bot bohot late gaana play karta hai"): the PREFERRED
        # playback path in _stream_track() is the direct-CDN stream, which
        # only needs resolve_stream_urls() — a metadata-only call. Warming
        # THAT (instead of a full file download) is what actually removes
        # wait time: by the time the VC join finishes, the URL is cached and
        # play() starts instantly. The download is only the fallback, so it
        # is warmed at low priority afterwards and no longer competes for
        # bandwidth/CPU with the stream that's actually playing.
        from melody.core.ytdl import (
            cached_file_path,
            download_audio,
            is_tg_media_id,
            should_try_direct_stream,
        )

        async def _warm_sources(video_id: str, want_video: bool):
            # Already cached (replay) → nothing to warm, playback is instant.
            if cached_file_path(video_id, audio_only=not want_video):
                return []
            # Tagged Telegram media has no CDN source — resolving stream URLs
            # for a synthetic id can only fail (after a long yt-dlp timeout).
            if is_tg_media_id(video_id):
                # download_replied_media() already owns the Telegram fetch and
                # may have returned a localhost range-proxy URL on the Track.
                # Do not start a second background fetch for tagged media.
                return []

            # SPEED ROOT FIX ("play karne ke baad 10s+ lagta hai"): this used
            # to gather() the direct-URL resolve AND a full file download at
            # the very same instant. On a 1-CPU dyno the download's yt-dlp +
            # ffmpeg processes ate the CPU and bandwidth that the ~300 ms
            # InnerTube resolve needed, so the fast path was throttled by its
            # own fallback — and call.py's DOWNLOAD_START_DELAY stagger was
            # defeated too, because its delayed download simply deduped onto
            # this already-running one.
            # Warm ONLY the cheap metadata resolve here. call.py starts the
            # download fallback itself if (and only if) the direct path is
            # slow or unusable.
            if not should_try_direct_stream():
                return await asyncio.gather(
                    download_audio(
                        video_id, audio_only=not want_video, priority=0, owner=chat.id,
                    ),
                    return_exceptions=True,
                )
            # Do not resolve the current track here. _stream_track() starts
            # the authoritative direct resolver a few lines later; warming it
            # here created a duplicate same-key resolve which serialized behind
            # the resolver lock and made the real playback task wait 8-10s
            # (especially after a large /vplay had occupied the dyno). The
            # interactive owner also starts the download fallback when needed,
            # so this warm task must stay idle for direct playback.
            return []

        warm_task = asyncio.create_task(_warm_sources(info["id"], video))
        _bg_downloads.add(warm_task)
        warm_task.add_done_callback(_bg_downloads.discard)

        track = Track(
            video_id=info["id"],
            title=info["title"],
            duration=info["duration"],
            stream_url=info["stream_url"],
            thumbnail=info["thumbnail"],
            uploader=info["uploader"],
            requester_id=requester_id,
            requester_name=requester_name,
            requested_in=chat.id,
            # BUG FIX: record whether this was /play or /vplay on the track
            # itself so the video intent survives queuing / auto-advance /
            # loop-single instead of silently reverting to audio-only.
            video=video,
            source_query=query if query and not tagged and not query.lower().startswith(("http://", "https://")) else "",
        )

        # Audio-first handoff: the real MediaStream creates/joins the VC and
        # starts the song. The primary /play command no longer waits for a
        # speculative silence join or a UI round-trip before this point.
        _t_join = _lap()

        if force:
            playing_now = await force_play_stream(
                chat.id, track, video=video, prejoin=False,
                deadline=_startup_deadline,
            )
        else:
            playing_now = await play_stream(
                chat.id, track, video=video, prejoin=False,
                deadline=_startup_deadline,
            )
        _total_elapsed = _lap()
        _stream_elapsed = max(0.0, _total_elapsed - _t_join) if playing_now else 0.0
        # `play_stream()` returns False for both queueing and a failed handoff.
        # The queue itself is the source of truth; force-play never queues.
        queued = track in get_queue(chat.id)
        if not playing_now and (force or not queued):
            processing = await processing_task
            anim = await anim_task
            await anim.stop()
            await _safe_processing_edit(processing, message,
                quote_html(
                    "❌ <b>Gana play nahi ho paya.</b>\n"
                    "YouTube stream unavailable ho sakti hai ya VC handoff complete nahi hua.\n"
                    "VC permissions tabhi check karo jab VC-related message aaye."
                ),
                parse_mode=enums.ParseMode.HTML,
            )
            LOGGER.warning(
                "playback handoff failed | force=%s chat=%s video=%s total=%.2fs",
                force, chat.id, info["id"], _total_elapsed,
            )
            return
        _outcome = "playing" if playing_now else "queued"
        LOGGER.info(
            "⏱ play timings | outcome=%s search=%.2fs join=%.2fs stream=%.2fs total=%.2fs | %s",
            _outcome, _t_info, _t_join - _t_info, _stream_elapsed,
            _total_elapsed, info["id"],
        )
        await add_history(chat.id, info["id"], info["title"])
        set_last_user_track(chat.id, info["id"], video=video)

        # Audio is already handed to PyTgCalls above. Only now wait for the
        # optional Telegram processing/status message before building the
        # thumbnail/card, so a slow group RPC cannot delay first sound.
        processing = await processing_task
        anim = await anim_task

        activity_label = "Force Played" if force else ("Now Playing" if playing_now else "Queued")
        spawn(log_activity(
            f"#play #{'vplay' if video else 'play'}\n"
            f"🎵 <b>{activity_label}</b>\n"
            f"• Song: <code>{html.escape(info['title'][:60])}</code>\n"
            f"• Uploader: <code>{html.escape(str(info.get('uploader') or 'Unknown'))}</code>\n"
            f"• Video ID: <code>{html.escape(str(info.get('id') or '—'))}</code>\n"
            f"• Requested by: {html.escape(requester_name or 'Unknown')} "
            f"(<code>{requester_id}</code>)\n"
            f"• Group: <b>{html.escape(chat.title or 'Private')}</b> (<code>{chat.id}</code>)\n"
            f"• Mode: <code>{'VIDEO' if video else 'AUDIO'}</code> · Result: <b>{_outcome.upper()}</b>\n"
            f"• Timing: search=<code>{_t_info:.2f}s</code> join=<code>{_t_join - _t_info:.2f}s</code> "
            f"stream=<code>{_stream_elapsed:.2f}s</code> total=<code>{_total_elapsed:.2f}s</code>"
        ))

        status_label = "Force Played" if force else ("Now Playing" if playing_now else "Added to Queue")
        safe_title    = html.escape(info["title"][:50])
        safe_uploader = html.escape(info["uploader"])
        safe_duration = html.escape(format_duration(info["duration"]))

        bot_dp_path, user_dp_path = await asyncio.gather(bot_dp_task, user_dp_task)
        bot_username, bot_name = await get_bot_identity(client)
        autoplay_on = await is_autoplay_on(chat.id)
        play_buttons = get_play_buttons(
            chat.title or "", autoplay_on, bot_username, bot_name, chat.type,
            chat_id=chat.id,
        )

        caption = (
            f"<blockquote>🎶 <b>{html.escape(status_label)}</b> · "
            f"🔁 AutoPlay: <b>{'ON' if autoplay_on else 'OFF'}</b>\n"
            f"<b>{safe_title}</b>\n"
            f"👤 {safe_uploader} · ⏱ {safe_duration}\n"
            f"🙋 {requester_mention}</blockquote>"
        )
        try:
            thumb_path = await make_thumbnail(
                song_title=info["title"],
                artist=info["uploader"],
                duration=format_duration(info["duration"]),
                requester_name=requester_name,
                group_name=chat.title or "Private",
                owner_name=Config.OWNER_NAME,
                yt_thumbnail_url=info["thumbnail"],
                requester_dp_path=user_dp_path,
                bot_dp_path=bot_dp_path,
            )
            await anim.stop()
            # BUG FIX: previously processing.delete() ran BEFORE reply_photo,
            # so when reply_photo failed (CHAT_SEND_PHOTOS_FORBIDDEN — the group
            # doesn't allow photos), the fallback tried to .edit() the already-
            # deleted message → MESSAGE_ID_INVALID crash. Now we send the photo
            # FIRST and only delete the processing message on success.
            await message.reply_photo(
                thumb_path,
                caption=caption,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=play_buttons,
            )
            await _safe_processing_delete(processing)
        except Exception as thumb_exc:
            from melody.logging import send_error_log
            # CHAT_SEND_PHOTOS_FORBIDDEN is an expected group-permission
            # condition, not a bug — the fallback below already shows the
            # now-playing card as text. Skip the owner error-log spam.
            _is_photo_forbidden = (
                type(thumb_exc).__name__ == "ChatSendPhotosForbidden"
                or "CHAT_SEND_PHOTOS_FORBIDDEN" in str(thumb_exc)
            )
            if not _is_photo_forbidden:
                await send_error_log(
                    f"make_thumbnail / reply_photo failed in {chat.id}",
                    thumb_exc,
                    context={
                        "chat_id": chat.id,
                        "chat_title": chat.title,
                        "user_id": requester_id,
                        "user_name": requester_name,
                        "song_title": info["title"] if info else None,
                        "video_id": info["id"] if info else None,
                    },
                )
            status = "Force Played ⚡" if force else ("Now Playing ▶️" if playing_now else "Added to Queue 📋")
            await anim.stop()
            # BUG FIX: the processing message may still exist (photo send
            # failed before delete), so edit it. But if it was already deleted
            # (partial success path), fall back to a new reply.
            try:
                await _safe_processing_edit(processing, message,
                    quote_html(
                        f"🎵 <b>{html.escape(status)}</b>\n\n"
                        f"<code>{safe_title}</code>\n"
                        f"👤 <code>{safe_uploader}</code>  ⏱ <code>{safe_duration}</code>\n"
                        f"🏠 {html.escape((chat.title or 'Private')[:60])}"
                    ),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=play_buttons,
                )
            except Exception:
                await message.reply(
                    quote_html(
                        f"🎵 <b>{html.escape(status)}</b>\n\n"
                        f"<code>{safe_title}</code>\n"
                        f"👤 <code>{safe_uploader}</code>  ⏱ <code>{safe_duration}</code>\n"
                        f"🏠 {html.escape((chat.title or 'Private')[:60])}"
                    ),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=play_buttons,
                )
    finally:
        # `anim` is only bound once the status message exists — if the search
        # itself blew up we still have to stop the animation task cleanly.
        try:
            if anim is None:
                anim = await anim_task
            await anim.stop()
        except Exception:
            pass


# BUG FIX: @error_handler is OUTER decorator — catches errors from admin_or_auth too
@bot.on_message(filters.command("play", prefixes=["/", ".", "!", "$"]) & filters.group)
@error_handler
@playmode_gate
async def play_cmd(client: Client, message: Message):
    await _play_core(client, message, video=False)


@bot.on_message(filters.command("vplay", prefixes=["/", ".", "!", "$"]) & filters.group)
@error_handler
@playmode_gate
async def vplay_cmd(client: Client, message: Message):
    await _play_core(client, message, video=True)


# ─── Force play — interrupts whatever is currently playing/queued ────────────

@bot.on_message(filters.command("playforce", prefixes=["/", ".", "!", "$"]) & filters.group)
@error_handler
async def playforce_cmd(client: Client, message: Message):
    await _play_core(client, message, video=False, force=True)


@bot.on_message(filters.command("vplayforce", prefixes=["/", ".", "!", "$"]) & filters.group)
@error_handler
async def vplayforce_cmd(client: Client, message: Message):
    await _play_core(client, message, video=True, force=True)


# ─── Channel play — /play and /vplay usable directly inside a channel ────────
# Channels can host a voice chat exactly like groups (py-tgcalls joins by
# chat_id either way); only the permission model differs, since a channel
# "message" is usually a post authored by the channel itself rather than a
# specific user. channel_admin_or_auth() handles that distinction.

@bot.on_message(filters.command("cplay") & filters.channel)
@error_handler
@channel_admin_or_auth
async def cplay_cmd(client: Client, message: Message):
    await _play_core(client, message, video=False)


@bot.on_message(filters.command("cvplay") & filters.channel)
@error_handler
@channel_admin_or_auth
async def cvplay_cmd(client: Client, message: Message):
    await _play_core(client, message, video=True)
