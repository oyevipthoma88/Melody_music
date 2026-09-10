from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_direct_resolver_supports_forced_fresh_retry():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "want_video: bool = False, *, force: bool = False" in source
    assert "cached = None if force else _stream_url_cache.get(key)" in source
    assert "failure_until = 0.0 if force else _stream_url_failures.get(key, 0.0)" in source


def test_mobile_app_restriction_is_not_treated_as_terminal_content_failure():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "Watch on the YouTube app" not in source or "mobile browser" in source
    # The downloader must keep walking the client ladder after this exact error.
    assert "This content isn’t available on your mobile browser" not in source
    assert "_DOWNLOAD_LADDER" in source
    assert "android_vr" in source
    assert "ios" in source
    assert "mweb" in source


def test_related_403s_have_bounded_negative_cache_and_no_duplicate_tail_call():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "_RELATED_FAILURE_TTL" in source
    assert "_RELATED_FAILURES" in source
    assert "return []" in source
    assert "timeout=3.0" in source


def test_stream_path_retries_fresh_direct_before_waiting_for_download():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "fresh_stream = await _build_direct_stream" in source
    assert "force=True" in source
    assert "fresh direct retry recovered" in source


def test_audio_fallback_uses_bounded_prefix_not_sixty_percent_gate():
    from melody.core.ytdl import _early_handoff_ready

    assert _early_handoff_ready(256_000, 4_000_000)
    assert not _early_handoff_ready(32_000, 4_000_000)


def test_large_audio_fallback_keeps_fixed_prefix_bound():
    from melody.core.ytdl import _early_handoff_ready

    assert not _early_handoff_ready(3_999_000, 50_000_000)
    assert _early_handoff_ready(4_000_000, 50_000_000)
