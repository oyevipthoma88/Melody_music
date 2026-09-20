import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_ytdl_disables_implicit_ffmpeg_fixups():
    source = _source("melody/core/ytdl.py")
    assert '"fixup": "never"' in source
    assert "Recovered completed download after temp rename race" in source


def test_docker_deploy_runs_post_compile_runtime_bootstrap():
    source = _source("Dockerfile")
    assert "RUN bash /app/bin/post_compile /app" in source
    assert "Deno/bgutil PO-token" in source


def test_bgutil_health_flag_recovers_after_provider_process_exit():
    source = _source("melody/core/ytdl.py")
    assert "provider process is not alive" in source
    assert 'def _bgutil_http_alive()' in source
    health_fn = source[source.index('def _bgutil_http_alive()'):source.index('async def warm_up_bgutil_server(')]
    assert 'global _BGUTIL_HTTP_PROC' in health_fn
    assert "if _BGUTIL_HTTP_PROC is not None and _BGUTIL_HTTP_PROC.poll() is None" in source
    assert "_BGUTIL_HTTP_READY = False" in source
    assert "PO-token server stopped" in source
    assert "server became unresponsive — restarting it" in source
    assert "proc.terminate()" in source
    assert "http://127.0.0.1:{_BGUTIL_HTTP_PORT}/ping" in source


def test_no_result_is_not_reported_as_a_crash_or_fake_video_id():
    source = _source("melody/core/ytdl.py")
    assert 'context={"video_id": f"ytsearch1:{url_or_query}"' not in source
    assert "No playable search result" in source


def test_admin_lookup_failure_is_not_cached_as_not_admin():
    source = _source("utils/decorators.py")
    tree = ast.parse(source)
    fetch = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_fetch_admin_status"
    )
    assert any(
        isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and node.value.value is None
        for node in ast.walk(fetch)
    )
    assert "_ADMIN_NEGATIVE_TTL = 5" in source


def test_fast_path_does_not_cancel_unstarted_ytdlp_future():
    source = _source("melody/core/ytdl.py")
    assert "if ytdlp_future is not None:" in source
    assert "Calling .cancel() on None used to raise" in source
    assert "_meta_cache_put(cache_key, result)" in source


def test_client_specific_format_errors_continue_retry_ladder():
    source = _source("melody/core/ytdl.py")
    assert "_PERMANENT_DOWNLOAD_MARKERS" in source
    assert "no video formats found" in source
    assert "drm protected" in source
    assert '"requested format is not available"' not in source
    assert "Format availability is client/rung-specific" in source
    assert "_is_permanent_download_error(exc)" in source
    assert "skipping remaining fallback clients" in source


def test_http_forbidden_downloads_remain_retryable_across_clients():
    source = _source("melody/core/ytdl.py")
    # A cloud CDN may reject one signed URL/client with 403; the retry ladder
    # must reach alternate YouTube clients instead of aborting on the first rung.
    assert '"http error 403"' not in source
    assert '"http error 429"' not in source
    assert '"sign in to confirm you’re not a bot"' not in source
    assert '"_client": ["android_music", "android", "android_vr"]' in source
    assert '"_client": ["ios", "ios_music", "mweb"]' in source


def test_expected_vc_permission_errors_do_not_reach_owner_error_log():
    source = _source("melody/core/call.py")
    assert "Expected Telegram access/permission failure" in source
    assert "Permission is already explained in the GC" in source
    assert "_playback_notice_until" in source
    assert "_PLAYBACK_NOTICE_TTL = 60.0" in source
    assert "if deadline > now:" in source


def test_remote_probe_preserves_stream_headers():
    source = _source("utils/pytgcalls_patch.py")
    assert "def _http_reachable_sync(url: str, timeout: float, headers: dict | None = None)" in source
    assert "supplied.get(\"Referer\")" in source
    assert "_remote_reachable(path, stream_headers)" in source


def test_cdn_probe_prefers_curl_cffi_with_urllib_fallback():
    source = _source("utils/pytgcalls_patch.py")
    assert "from curl_cffi import requests as curl_requests" in source
    assert 'impersonate="chrome124"' in source
    assert "retain urllib as a dependency-free" in source


def test_youtube_client_policy_keeps_cloud_direct_fallback_order():
    """Cookies must never be replayed to the mobile app clients.

    Sep 20 2026 05:13 production log: every client answered "Sign in to
    confirm you're not a bot", the only surviving format was the muxed
    itag 18, so the direct CDN URL had no clean audio track
    (NoAudioSourceFound) and /play paid an 18 MB download (stream=16.68s).
    With a cookiefile attached, ask the cookie-compatible web/TV clients;
    the mobile clients stay reachable through the ladder rungs that drop
    the cookiefile first.
    """
    source = _source("melody/core/ytdl.py")
    assert '["web_safari", "web", "tv"] if has_cookies' in source
    assert 'else ["ios", "visionos", "web"]' in source
    assert '"player_client": ["android_music", "android_vr", "tv", "ios", "web_safari"]' not in source
    assert '"client": client_name' in source
    assert '"User-Agent": ua' in source


def test_innertube_mobile_probes_stay_session_free():
    """Web-minted visitorData / PO token / cookies are web-family only.

    Attaching the WEB `visitorData` to the IOS / ANDROID / *_MUSIC /
    VISIONOS player probes made YouTube answer LOGIN_REQUIRED on every
    single one of them (Sep 20 2026 05:13 log), which killed the direct
    stream path entirely. Bare mobile probes answer OK with unciphered
    CDN URLs, so no session material may leak into them.
    """
    source = _source("melody/core/ytdl.py")
    assert 'if is_web and session.get("visitor"):\n            ctx["visitorData"]' in source
    assert 'if session.get("visitor"):\n            ctx["visitorData"]' not in source
    assert 'if session.get("visitor"):\n            headers["X-Goog-Visitor-Id"]' not in source
    assert 'if cookie_header and client_name in _WEB_FAMILY:' in source


def test_ytdlp_direct_resolver_preserves_format_headers():
    source = _source("melody/core/ytdl.py")
    assert 'resolved_headers = dict((info or {}).get("http_headers") or {})' in source
    assert 'fmt_info.get("http_headers")' in source


def test_audio_resolver_can_prefer_hls_manifest_before_muxed_fallback():
    source = _source("melody/core/ytdl.py")
    assert '"hlsManifestUrl": sd.get("hlsManifestUrl")' in source
    assert 'hls_manifest = info.get("hlsManifestUrl")' in source
    assert 'return {"audio": hls_manifest, "video": None}' in source


def test_direct_picker_accepts_hls_and_top_level_playable_urls():
    from melody.core.ytdl import _pick_stream_formats

    hls = {
        "formats": [{
            "url": "https://media.example/live.m3u8",
            "protocol": "m3u8_native",
            "acodec": "opus",
            "vcodec": "none",
        }]
    }
    assert _pick_stream_formats(hls, False)["audio"].endswith("live.m3u8")

    top_level = {
        "url": "https://media.example/audio.webm",
        "protocol": "https",
    }
    picked = _pick_stream_formats(top_level, False)
    assert picked == {}
    # The top-level fallback is applied by `_resolve_stream_urls_sync`; the
    # picker itself intentionally only classifies format-list entries.
    source = _source("melody/core/ytdl.py")
    assert 'if not picked and info.get("url"):' in source
    assert 'proto.startswith("http")' in source


def test_cloud_playback_uses_audio_only_early_handoff_and_keeps_video_safe():
    source = _source("melody/core/ytdl.py")
    assert '_EARLY_HANDOFF_ENABLED = _env_flag("EARLY_HANDOFF", False) and not _ON_CLOUD_HOST' in source
    assert '_EARLY_AUDIO_HANDOFF_ENABLED = _env_flag("EARLY_AUDIO_HANDOFF", True)' in source
    assert 'def _early_handoff_allowed(audio_only: bool)' in source
    assert 'if not audio_only or not _early_audio_path_is_safe' in source
    assert 'await done_async.wait()' in source
    assert '_download_jobs: set[asyncio.Task] = set()' in source
    assert 'job.add_done_callback(_download_jobs.discard)' in source
    call = _source("melody/core/call.py")
    assert 'allow_early=not video' in call
    assert 'is_download_inflight(track.video_id' in call
    assert 'wait_for_download(' in call


def test_early_audio_paths_never_persist_until_download_completion():
    call = _source("melody/core/call.py")
    assert 'if is_download_inflight(track.video_id, audio_only=not video):' in call
    assert 'completed = await wait_for_download(' in call
    assert 'await _persist_completed_song(archived_path, track)' in call
    assert 'from utils.telegram_archive import archive_completed_file' in call
    assert 'Archive completed fallback media in Telegram' in call


def test_fallback_vc_admin_error_is_caught_before_generic_crash_logging():
    source = _source("melody/core/call.py")
    marker = "Do not let that second"
    assert marker in source
    block_start = source.rfind("except ChatAdminRequired:", 0, source.index(marker))
    block = source[block_start:block_start + 1100]
    assert "except ChatAdminRequired:" in block
    assert "_block_vc_admin(chat_id)" in block
    assert "_notify_playback_failed" in block
    assert "return False" in block


def test_current_youtube_unavailable_wording_stops_retries_and_consumes_future_errors():
    ytdl = _source("melody/core/ytdl.py")
    assert '"video unavailable"' in ytdl
    assert '"this content isn\'t available"' in ytdl
    assert "def _consume_download_future" in ytdl
    assert "fut.add_done_callback(_consume_download_future)" in ytdl


def test_interactive_downloads_outrank_autoplay_prefetch():
    ytdl = _source("melody/core/ytdl.py")
    autoplay = _source("melody/core/autoplay.py")
    call = _source("melody/core/call.py")
    assert "class _PriorityDownloadGate" in ytdl
    assert "async def acquire(self, priority: int = 0)" in ytdl
    assert "priority=50" in autoplay
    assert "priority=0" in call
    assert "queue[:3]" in call
    assert "_prefetch_inflight: dict[int, set[str]]" in call
    assert "_FORCE_DOWNLOAD_PRIORITY = -10" in call
    assert "priority=_FORCE_DOWNLOAD_PRIORITY" in call
    assert "candidates = [" in call
    assert "candidates[:prefetch_workers]" in call
    assert "await _persist_completed_song(path, upcoming)" in autoplay
    assert "if is_download_cancelled(exc):" in call
    assert "fut.add_done_callback(_consume_download_future)" in ytdl
    assert "cancel_lower_priority_downloads(" in _source("melody/plugins/music/play.py")
    assert "exclude_video_id=direct_id" in _source("melody/plugins/music/play.py")
    assert 'PREFETCH_ENABLED", "true"' in call
    assert 'RESOLVE_TIMEOUT", "4.0"' in ytdl
    assert 'INNERTUBE_HEADSTART", "0.20"' in ytdl
    assert 're.sub(r"\\.part-frag\\d+$", "", name)' in ytdl
    assert 'picked = _pick_stream_formats(result, want_video)' in ytdl
    assert 'result["_picked"] = picked' in ytdl
    assert '_innertube_streams_sync(video_id, want_video=want_video)' in ytdl
    config = _source("melody/config.py")
    assert '_env_bool("BGUTIL_STARTUP_WARMUP", True)' in config


def test_atlas_quota_cannot_break_autoplay_persistence():
    database = _source("utils/database.py")
    assert "_STORAGE_WRITES_DISABLED = False" in database
    assert "def _is_storage_quota_error" in database
    assert "async def add_history" in database
    assert "if _STORAGE_WRITES_DISABLED:" in database
    assert '"writes are blocked" in text' in database


def test_fresh_log_network_and_autoplay_races_are_single_flight():
    ytdl = _source("melody/core/ytdl.py")
    autoplay = _source("melody/core/autoplay.py")
    assert "_http_sync_client_lock = threading.Lock()" in ytdl
    assert "timeout=_env_float(\"BGUTIL_HTTP_READY_WAIT\", 3.0)" in ytdl
    assert "hls_formats = [f for f in all_formats if hls_ok(f)]" in ytdl
    assert "set_predownloaded(chat_id, track)" in autoplay
    assert 'name=f"autoplay-prefetch:{chat_id}"' in autoplay


def test_fresh_log_permission_failures_do_not_escape_handlers():
    events = _source("melody/plugins/misc/vc_events.py")
    greetings = _source("melody/plugins/admin/greetings.py")
    assert "ChatWriteForbidden" in events
    assert "ChatAdminRequired" in events
    assert 'LOGGER.info("VC notification skipped' in events
    assert "ChatSendPhotosForbidden" in greetings
    assert 'using text fallback' in greetings


def test_cloud_autoplay_does_not_consume_the_interactive_download_slot():
    source = _source("melody/core/autoplay.py")
    assert "from melody.core.ytdl import resolve_stream_urls, download_audio, on_cloud_host" in source
    assert "if on_cloud_host() and not _cloud_prefetch_enabled():" in source
    assert "cloud pre-download skipped" in source
    cloud_branch = source[source.index("if on_cloud_host() and not _cloud_prefetch_enabled():"):source.index("if on_cloud_host() and not _cloud_prefetch_enabled():") + 900]
    assert "return track" in cloud_branch


def test_signed_cdn_details_are_redacted_from_playback_errors():
    from melody.logging import redact_sensitive_text

    raw = (
        'NoAudioSourceFound on '
        'https://rr2---sn.example.googlevideo.com/videoplayback?expire=123&ip=1.2.3.4&pot=secret&sig=secret'
    )
    safe = redact_sensitive_text(raw)
    assert "googlevideo.com/videoplayback" in safe
    assert "expire=" not in safe
    assert "ip=" not in safe
    assert "pot=" not in safe
    assert "sig=" not in safe


def test_autoplay_duration_parser_rejects_four_hour_related_media():
    source = _source("melody/core/autoplay.py")
    assert "def _candidate_duration(candidate) -> int:" in source
    assert "value = int(candidate.get(\"duration\") or 0)" in source
    assert "value = int(raw) if raw else 600" in source
    assert "if track_duration > AUTOPLAY_MAX_DURATION:" in source


def test_queued_cloud_prefetch_skips_full_download_after_resolve_failure():
    source = _source("melody/core/call.py")
    assert "cached_file_path, download_audio, is_download_cancelled" in source
    assert "on_cloud_host, resolve_stream_urls, should_try_direct_stream" in source
    assert '"prefetch: cloud download skipped for %s to protect interactive playback"' in source
    assert "if on_cloud_host() and not _cloud_prefetch_enabled():" in source


def test_priority_download_gate_orders_and_cleans_waiters():
    import asyncio
    from melody.core.ytdl import _PriorityDownloadGate

    async def scenario():
        gate = _PriorityDownloadGate(1)
        await gate.acquire(priority=0)
        order = []

        async def waiter(priority, label):
            await gate.acquire(priority=priority)
            order.append(label)

        low = asyncio.create_task(waiter(50, "low"))
        await asyncio.sleep(0)
        canceled = asyncio.create_task(waiter(80, "canceled"))
        await asyncio.sleep(0)
        high = asyncio.create_task(waiter(0, "high"))
        await asyncio.sleep(0)
        canceled.cancel()
        try:
            await canceled
        except asyncio.CancelledError:
            pass
        await gate.release()
        await asyncio.wait_for(high, timeout=1)
        assert order == ["high"]
        assert gate.running == 1
        await gate.release()
        await asyncio.wait_for(low, timeout=1)
        assert order == ["high", "low"]
        await gate.release()
        assert gate.running == 0

    asyncio.run(scenario())


def test_stale_download_cancellation_is_owner_scoped_and_cooperative():
    ytdl = _source("melody/core/ytdl.py")
    call = _source("melody/core/call.py")
    assert "def cancel_download(video_id: str, audio_only: bool = True, owner=None) -> bool:" in ytdl
    assert "owner token" in ytdl
    assert "class _DownloadCancelled" in ytdl
    assert "raise _DownloadCancelled" in ytdl
    assert "cancel_download(previous[0], audio_only=not previous[1], owner=chat_id)" in call
    assert "retained as a cache warmer" in call
    assert "download_task.add_done_callback(_consume_task_exception)" in call
    assert "if is_download_cancelled(exc):" in call
    assert "_get_stream_commit_lock(chat_id)" in call


def test_low_priority_preemption_is_owner_safe_and_excludes_shared_media():
    import threading
    from melody.core import ytdl

    previous = ytdl._download_cancel_events.copy()
    try:
        same_video = threading.Event()
        other_video = threading.Event()
        manual = threading.Event()
        ytdl._download_cancel_events.clear()
        ytdl._download_cancel_events.update({
            "same-video:a": (same_video, 1, 25),
            "other-video:a": (other_video, 2, 25),
            "manual-video:a": (manual, 3, 0),
        })
        assert ytdl.cancel_lower_priority_downloads(
            0, exclude_video_id="same-video"
        ) == 1
        assert not same_video.is_set()
        assert other_video.is_set()
        assert not manual.is_set()
    finally:
        ytdl._download_cancel_events.clear()
        ytdl._download_cancel_events.update(previous)


def test_autoplay_only_reports_success_after_authoritative_stream_start():
    source = _source("melody/core/autoplay.py")
    assert "autoplay_gen = _begin_generation(chat_id)" in source
    assert "_resolving_origin[chat_id] = \"autoplay\"" in source
    assert "started = await _stream_track(" in source
    assert "gen=autoplay_gen, priority=25" in source
    assert "if started is not True or _is_stale_generation" in source
    assert "await add_history(chat_id, track.video_id, track.title)" in source


def test_startup_recovery_and_peer_warmup_cannot_block_commands():
    main = _source("melody/__main__.py")
    config = _source("melody/config.py")
    assert "PLAYBACK_RECOVERY: bool = _env_bool(\"PLAYBACK_RECOVERY\", False)" in config
    assert "spawn(_recover_in_background())" in main
    assert "warm_recovery_peers" in main
    assert "await warm_bot_peer_cache(bot)" not in main
    assert "spawn(_finish_secondary_startup(), name=\"startup-log-sync\")" in main


def test_control_commands_ack_before_slow_voice_operations():
    source = _source("melody/plugins/music/controls.py")
    volume = _source("melody/plugins/music/volume.py")
    channel = _source("melody/plugins/music/channel_controls.py")
    assert "spawn(stop_stream(message.chat.id), name=f\"stop-{message.chat.id}\")" in source
    assert "spawn(_do_skip(cb.message.chat.id), name=f\"skip-{cb.message.chat.id}\")" in source
    assert "spawn(stop_stream(cb.message.chat.id), name=f\"stop-{cb.message.chat.id}\")" in source
    assert "status = await send_quote(message, \"⏳ <b>Pausing...</b>\"" in source
    assert "status = await send_quote(message, \"⏳ <b>Resuming...</b>\"" in source
    assert 'await cb.answer("⏳ Pausing...")' in source
    assert 'await cb.answer("⏳ Resuming...")' in source
    assert 'spawn(\n        _callback_pause_resume' in source
    assert "spawn(\n        _finish_volume" in volume
    assert "spawn(\n        _finish_mute" in volume
    assert "spawn(\n        _finish_channel_transport" in channel


def test_permission_hot_path_uses_bounded_parallel_lookups():
    decorators = _source("utils/decorators.py")
    database = _source("utils/database.py")
    assert "asyncio.gather(is_banned(user_id), is_gbanned(user_id))" in decorators
    assert "timeout=_BAN_CHECK_TIMEOUT" in decorators
    assert "async def _auth_users_fast" in decorators
    assert "timeout=_AUTH_CHECK_TIMEOUT" in decorators
    assert "_SETTINGS_TTL = 45.0" in database
    assert "_settings_cache" in database


def test_local_playback_handoff_is_bounded():
    source = _source("melody/core/call.py")
    assert "_LOCAL_PLAY_TIMEOUT" in source
    assert "timeout=_LOCAL_PLAY_TIMEOUT" in source
    assert "asyncio.wait_for(" in source


def test_gridfs_cache_uploads_are_serialized_and_async():
    source = _source("utils/song_cache.py")
    assert "_gridfs_upload_lock" in source
    assert "async with lock:" in source
    assert "await upload.write(chunk)" in source
    assert "await upload.close()" in source


def test_local_proxy_failure_does_not_recreate_a_second_proxy():
    call = _source("melody/core/call.py")
    proxy = _source("utils/tg_media_proxy.py")
    assert "LOCAL_PROXY_PLAY_TIMEOUT" in call
    assert "if local_proxy_source:" in call
    assert "return False" in call
    assert "ClientConnectionResetError" in proxy
    assert "except asyncio.CancelledError:" in proxy
    assert "quote(name, safe='')" in proxy


def test_innertube_resolver_reuses_bounded_pool_and_cancels_queued_probes():
    source = _source("melody/core/ytdl.py")
    assert "YTDL_POOL.submit(_probe, entry)" in source
    assert "pool = ThreadPoolExecutor(max_workers=len(_CLIENTS))" not in source
    assert "future.cancel()" in source


def test_play_timing_log_labels_queued_requests():
    source = _source("melody/plugins/music/play.py")
    assert '"⏱ play timings | outcome=%s search=' in source
    assert '_outcome = "playing" if playing_now else "queued"' in source
    assert 'if playing_now else 0.0' in source
    assert "queued = track in get_queue(chat.id)" in source
    assert "if not playing_now and (force or not queued)" in source


def test_force_callbacks_report_background_handoff_failure():
    source = _source("melody/plugins/music/controls.py")
    assert "async def _force_and_report()" in source
    assert "Force play start nahi ho paya" in source
    assert "async def _replay_and_report()" in source


def test_cloud_warm_failure_leaves_fallback_to_stream_owner():
    source = _source("melody/plugins/music/play.py")
    assert "on_cloud_host," in source
    assert "if on_cloud_host():" in source
    assert "playback will own fallback download" in source
    assert "races direct resolution, inflates RSS, and can trigger R14" in source


def test_cold_audio_fallback_prefers_small_voice_chat_format():
    source = _source("melody/core/ytdl.py")
    assert 'YT_AUDIO_MAX_ABR' in source
    assert 'bestaudio[ext=webm][abr<=' in source
    assert 'bestaudio[ext=opus]/bestaudio[abr<=192]' in source
    assert '"concurrent_fragment_downloads": _env_int("YT_CONCURRENT_FRAGMENTS", 4)' in source


def test_tagged_media_duration_guard_runs_before_download():
    ytdl = _source("melody/core/ytdl.py")
    play = _source("melody/plugins/music/play.py")
    assert "_TAGGED_PROXY_THRESHOLD_MB = max(32, _env_int(\"TAGGED_PROXY_THRESHOLD_MB\", 256))" in ytdl
    assert "_TAGGED_MAX_DURATION = max(600, _env_int(\"TAGGED_MAX_DURATION_SECONDS\", 6 * 3600))" in ytdl
    assert "def tagged_media_limit_reason" in ytdl
    assert "tagged_media_limit_reason(replied, video=video)" in play
    assert "_TAGGED_PROXY_THRESHOLD_BYTES" in ytdl


def test_tagged_document_detection_uses_extension_fallback():
    source = _source("melody/core/ytdl.py")
    assert "application/octet-stream" in source
    assert 'ext in {' in source
    assert '"mp3", "mp4", "m4a", "aac"' in source
    assert '"opus", "webm", "mkv", "mov", "avi", "m4v"' in source


def test_large_tagged_media_uses_range_proxy_without_full_download():
    ytdl = _source("melody/core/ytdl.py")
    call = _source("melody/core/call.py")
    proxy = _source("utils/tg_media_proxy.py")
    assert "_TAGGED_PROXY_THRESHOLD_BYTES" in ytdl
    assert "create_media_proxy" in ytdl
    assert "tagged media using range proxy" in ytdl
    assert 'proxy_url.startswith(("http://127.0.0.1:", "http://localhost:"))' in call
    assert "stream_media(" in proxy
    assert '"Accept-Ranges": "bytes"' in proxy
    assert '"Content-Range"' in proxy


def test_range_proxy_range_parser_is_bounded():
    from utils.tg_media_proxy import _parse_range
    assert _parse_range(None, 100) is None
    assert _parse_range("bytes=0-9", 100) == (0, 9)
    assert _parse_range("bytes=90-", 100) == (90, 99)
    assert _parse_range("bytes=-10", 100) == (90, 99)


def test_telegram_loopback_proxy_is_classified_as_local_source():
    from utils.pytgcalls_patch import _is_local_proxy_url, _is_local_source

    proxy = "http://127.0.0.1:43127/tg/token/movie.mp4"
    assert _is_local_proxy_url(proxy)
    assert _is_local_source(proxy)
    assert not _is_local_proxy_url("https://cdn.example.com/movie.mp4")


def test_loopback_proxy_probe_path_does_not_require_remote_probe_error():
    source = _source("utils/pytgcalls_patch.py")
    assert "local = _is_local_source(path)" in source
    assert "if _is_local_file(path) and not _is_local_playable(path)" in source
    assert "raise StreamProbeUnavailable" in source


def test_local_telegram_proxy_bypasses_ffprobe_before_pytgcalls():
    source = _source("utils/pytgcalls_patch.py")
    assert "if _is_local_proxy_url(path):" in source
    assert "local Telegram proxy — skipping pre-probe" in source
    assert "return None" in source[source.index("if _is_local_proxy_url(path):"):source.index("# PERMANENT FIX", source.index("if _is_local_proxy_url(path):"))]


def test_large_movie_proxy_has_bounded_high_fanout_registry():
    source = _source("utils/tg_media_proxy.py")
    assert '_MAX_PROXIES = _env_int("TG_PROXY_MAX_ENTRIES", 1024, 64)' in source
    assert '_PROXY_CONCURRENCY = _env_int("TG_PROXY_CONCURRENCY", 32, 4)' in source
    assert "_source_tokens" in source
    assert "same movie" in source
    assert "_stream_slots = asyncio.Semaphore(_PROXY_CONCURRENCY)" in source


def test_command_registration_recovers_from_telegram_command_limit():
    source = _source("melody/__main__.py")
    assert "BOT_COMMANDS_TOO_MUCH" in source
    assert "retrying with 100-entry menu" in source
    assert "_set_commands_safely" in source
