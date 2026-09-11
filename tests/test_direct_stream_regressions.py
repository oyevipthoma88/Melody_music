from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_direct_resolver_rotates_client_profiles_before_giving_up():
    """A transient/default client block must not force a full download."""
    from melody.core import ytdl

    calls = []
    formats = [{
        "url": "https://cdn.example/audio.webm?expire=4102444800",
        "protocol": "https",
        "vcodec": "none",
        "acodec": "opus",
        "abr": 96,
    }]

    class FakeYDL:
        def __init__(self, profile):
            self.profile = profile

        def extract_info(self, target, download=False):
            calls.append(self.profile)
            if self.profile in (None, ["default"]):
                raise RuntimeError("default client blocked")
            return {"formats": formats, "http_headers": {"User-Agent": "test"}}

    @contextmanager
    def fake_locked(opts):
        profile = ((opts.get("extractor_args") or {}).get("youtube") or {}).get("player_client")
        yield FakeYDL(profile)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ytdl, "_ydl_opts", lambda audio_only=True: {
        "format": "bestaudio",
        "extractor_args": {"youtube": {"player_client": ["default"]}},
    })
    monkeypatch.setattr(ytdl, "_locked_ytdl", fake_locked)
    try:
        result = ytdl._resolve_stream_urls_sync("https://www.youtube.com/watch?v=iAIBF2ngbWY", False)
    finally:
        monkeypatch.undo()

    assert result["audio"] == formats[0]["url"]
    assert calls[0] == ["default"]
    assert calls[1] == ["ios", "android_vr"]


def test_direct_resolver_keeps_hls_as_a_valid_last_resort():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "direct_profiles = (" in source
    assert '["tv_simply", "tv"]' in source
    assert "hlsManifestUrl" in source
    assert "download fallback engaged" in (ROOT / "melody/core/call.py").read_text(encoding="utf-8")


def test_audio_picker_prefers_audio_only_source_over_hls_manifest():
    from melody.core.ytdl import _pick_stream_formats

    result = _pick_stream_formats({
        "hlsManifestUrl": "https://cdn.example/live.m3u8",
        "formats": [
            {
                "url": "https://cdn.example/audio.webm",
                "protocol": "https",
                "vcodec": "none",
                "acodec": "opus",
                "abr": 96,
            },
            {
                "url": "https://cdn.example/muxed.mp4",
                "protocol": "https",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": 360,
                "tbr": 450,
            },
            {
                "url": "https://cdn.example/live.m3u8",
                "protocol": "m3u8_native",
                "vcodec": "none",
                "acodec": "aac",
            },
        ],
    }, False)
    assert result["audio"] == "https://cdn.example/audio.webm"


def test_audio_picker_keeps_hls_alternate_for_signed_url_recovery():
    from melody.core.ytdl import _pick_stream_formats

    result = _pick_stream_formats({
        "hlsManifestUrl": "https://cdn.example/fallback.m3u8",
        "formats": [{
            "url": "https://cdn.example/signed.webm",
            "protocol": "https",
            "vcodec": "none",
            "acodec": "opus",
            "abr": 96,
        }],
    }, False)
    assert result["audio"] == "https://cdn.example/signed.webm"
    assert result["fallback_audio"].endswith("fallback.m3u8")


def test_fallback_download_races_without_an_avoidable_cloud_delay():
    call = ROOT / "melody/core/call.py"
    source = call.read_text(encoding="utf-8")
    assert 'float(os.getenv("DOWNLOAD_START_DELAY", "0.25"))' in source
    assert "_DOWNLOAD_START_DELAY = 0.25" in source


def test_startup_timeout_preserves_parallel_download_fallback():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert 'os.getenv("DOWNLOAD_HANDOFF_GRACE", "12")' in source
    assert "asyncio.shield(download_task)" in source
    assert "startup deadline recovered via parallel" in source


def test_transient_stream_routes_do_not_emit_crash_cards():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "def _is_transient_playback_error" in source
    assert "playback routes exhausted" in source
    assert "suppressed transient _stream_track failure" in source


def test_active_download_suppresses_duplicate_forced_direct_resolve():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "is_download_inflight" in source
    assert "download_already_running" in source
    assert "no duplicate resolve" in source


def test_cloud_audio_fallback_can_start_from_validated_prefix():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert '"EARLY_AUDIO_HANDOFF", True' in source
    assert "_resume_if_premature_end()" in source
    assert '_EARLY_HANDOFF_RATIO = _env_float("EARLY_HANDOFF_RATIO", 0.02)' in source
    assert '_EARLY_HANDOFF_LARGE_FILE_PREFIX = _env_int("EARLY_HANDOFF_LARGE_FILE_PREFIX", 512_000)' in source


def test_failed_direct_url_is_quarantined_for_later_play_requests():
    ytdl = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "def invalidate_stream_url" in ytdl
    assert "_STREAM_URL_PLAY_FAILURE_TTL = 300.0" in ytdl
    assert "invalidate_stream_url(track.video_id, want_video=video)" in call
    assert "fallback circuit open" in call


def test_audio_early_handoff_is_enabled_with_small_prefix_and_long_metadata_budget():
    ytdl = ROOT / "melody/core/ytdl.py"
    source = ytdl.read_text(encoding="utf-8")
    assert '_EARLY_HANDOFF_BYTES = _env_int("EARLY_HANDOFF_BYTES", 128_000)' in source
    assert '_EARLY_HANDOFF_TIMEOUT = _env_float("EARLY_HANDOFF_TIMEOUT", 12.0)' in source
    assert '"⚡ #download early audio handoff %s variant=%s bytes=%d path=%s"' in source
    assert '_EARLY_AUDIO_HANDOFF_ENABLED = _env_flag(' in source
    assert 'False if _ON_CLOUD_HOST else True' in source
    assert '"webm", "ogg", "oga", "opus"' in source
    assert '"mp3", "flac", "wav"' in source


def test_download_audio_requests_early_handoff_for_interactive_audio_fallback():
    call = ROOT / "melody/core/call.py"
    source = call.read_text(encoding="utf-8")
    assert 'allow_early=not video' in source
    assert 'return_when=asyncio.FIRST_COMPLETED' in source
    assert 'while pending and stream is None' in source


def test_search_selection_does_not_await_speculative_direct_resolver():
    search = (ROOT / "melody/plugins/music/search.py").read_text(encoding="utf-8")
    assert "warm_stream = asyncio.create_task(" in search
    assert "warm_stream.add_done_callback(_consume_warm_result)" in search
    assert "await warm_stream" not in search
    assert "play_stream(chat.id, track)" in search


def test_early_audio_accepts_yt_dlp_fragment_suffixes():
    from melody.core.ytdl import _early_audio_path_is_safe

    assert _early_audio_path_is_safe("/tmp/file.webm.part")
    assert _early_audio_path_is_safe("/tmp/file.webm.part-Frag159")
    assert _early_audio_path_is_safe("/tmp/file.opus.part_frag_2")
    assert not _early_audio_path_is_safe("/tmp/file.mp4.part-Frag159")


def test_early_audio_handoff_does_not_wait_for_most_of_small_file():
    from melody.core.ytdl import _early_handoff_ready

    # A 4 MB song should be playable after the bounded prefix, not after the
    # old 60%-download gate that made direct-stream failures take 15-20s.
    assert _early_handoff_ready(128_000, 4_000_000)
    assert not _early_handoff_ready(32_000, 4_000_000)


def test_early_audio_handoff_uses_fixed_prefix_for_large_files():
    from melody.core.ytdl import _early_handoff_ready

    assert not _early_handoff_ready(400_000, 50_000_000)
    assert _early_handoff_ready(512_000, 50_000_000)


def test_play_and_playforce_activity_log_omits_requester_username():
    source = (ROOT / "melody/plugins/music/play.py").read_text(encoding="utf-8")
    assert "• Requested by:" in source
    assert "requester_name or 'Unknown'" in source
    assert "user.username" not in source


def test_cloud_audio_handoff_is_opt_in_to_prevent_growing_file_eof():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert 'False if _ON_CLOUD_HOST else True' in source
    assert "regular file" in source


def test_mid_track_stream_end_retries_same_track_before_queue_advance():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "async def _recover_interrupted_stream" in source
    assert "if await _recover_interrupted_stream(chat_id):" in source
    assert "_interrupted_retries" in source
    assert "start_at=resume_at" in source
