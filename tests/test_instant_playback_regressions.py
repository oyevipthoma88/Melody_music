from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_fallback_download_is_not_delayed_by_direct_resolver():
    call = source("melody/core/call.py")
    assert 'configured_download_delay = float(os.getenv("DOWNLOAD_START_DELAY", "0.0"))' in call
    assert "download_task = asyncio.create_task(" in call
    assert "_DOWNLOAD_START_DELAY if direct_first else 0.0" in call
    assert "wait_for_early_download" in call


def test_completed_fallback_task_is_used_before_startup_timeout_is_reported():
    call = source("melody/core/call.py")
    assert "if download_task.done():" in call
    assert "return download_task.result()" in call
    assert "fallback download exceeded startup budget; startup grace window expired" in call


def test_early_download_waiter_is_public_and_never_required_for_video():
    ytdl = source("melody/core/ytdl.py")
    assert "async def wait_for_early_download(" in ytdl
    assert "never returning a partial path" in ytdl
    assert "_download_early_states.get(key)" in ytdl


def test_direct_resolution_has_a_short_default_budget_and_bounded_rescue():
    ytdl = source("melody/core/ytdl.py")
    assert 'os.getenv("RESOLVE_TIMEOUT", "4.0")' in ytdl
    assert 'timeout=_env_float("DIRECT_RESCUE_TIMEOUT", 1.5)' in ytdl
    assert "deadline = max(_t0 + _RESOLVE_TIMEOUT" in ytdl


def test_audio_early_handoff_remains_enabled_on_cloud_by_default():
    ytdl = source("melody/core/ytdl.py")
    # Cloud deployments must not silently disable the growing WebM/Opus path;
    # video and MP4/M4A remain completion-only through _early_handoff_allowed.
    assert '_EARLY_AUDIO_HANDOFF_ENABLED = _env_flag("EARLY_AUDIO_HANDOFF", True)' in ytdl
    assert "return bool(_EARLY_HANDOFF_ENABLED or (audio_only and _EARLY_AUDIO_HANDOFF_ENABLED))" in ytdl


def test_stream_end_handler_catches_failures_and_does_not_advance_during_leave():
    call = source("melody/core/call.py")
    assert "if _is_leaving(chat_id):" in call
    assert "if await _resume_if_premature_end(chat_id):" in call
    assert 'LOGGER.exception("‼️ _on_stream_end crashed for chat %s", chat_id)' in call


def test_download_deadline_prevents_retry_ladder_hanging_a_chat():
    ytdl = source("melody/core/ytdl.py")
    assert 'deadline = _time_mod.monotonic() + _env_float("DOWNLOAD_DEADLINE", 45.0)' in ytdl
    assert '"socket_timeout": _env_int("YT_SOCKET_TIMEOUT", 8)' in ytdl
    assert '"fragment_retries": _env_int("YT_FRAGMENT_RETRIES", 5)' in ytdl


def test_adaptive_video_pair_does_not_force_a_multi_gb_local_download():
    from melody.core.ytdl import _pick_stream_formats

    result = _pick_stream_formats(
        {
            "formats": [
                {
                    "url": "https://cdn.example/video-480.mp4",
                    "protocol": "https",
                    "vcodec": "avc1",
                    "acodec": "none",
                    "height": 480,
                    "tbr": 900,
                },
                {
                    "url": "https://cdn.example/audio.webm",
                    "protocol": "https",
                    "vcodec": "none",
                    "acodec": "opus",
                    "abr": 128,
                },
            ]
        },
        True,
    )
    assert result["video"].endswith("video-480.mp4")
    assert result["audio"].endswith("audio.webm")


def test_long_video_path_is_direct_only_and_skips_download_task():
    call = source("melody/core/call.py")
    assert 'VIDEO_DIRECT_ONLY_SECONDS", "900"' in call
    assert "direct_only_video = bool(" in call
    assert "if not live_source and not direct_only_video:" in call
    assert "long video direct stream unavailable; local fallback disabled" in call


def test_known_id_metadata_has_oembed_fast_fallback():
    ytdl = source("melody/core/ytdl.py")
    assert "def _youtube_oembed_sync(video_id: str)" in ytdl
    assert 'fast_jobs["oembed"]' in ytdl
    assert "oEmbed is tiny" in ytdl


def test_public_provider_failover_is_strictly_bounded():
    ytdl = source("melody/core/ytdl.py")
    assert 'INVIDIOUS_MAX_INSTANCES", "3"' in ytdl
    assert "_INVIDIOUS_INSTANCES[:_INVIDIOUS_MAX_INSTANCES]" in ytdl
    assert "timeout=0.75" in ytdl
    assert 'DIRECT_RESCUE_TIMEOUT", 1.5' in ytdl


def test_cookie_stream_fast_path_uses_one_direct_web_client():
    ytdl = source("melody/core/ytdl.py")
    assert '["web_safari"]\n            if has_cookies' in ytdl
    assert '_CLIENTS = [("WEB", _IT_WEB_VERSION, _IT_WEB_UA, {})]' in ytdl
    assert 'if cookie_header and _env_flag("INNERTUBE_WEB_CLIENTS", False)' not in ytdl
