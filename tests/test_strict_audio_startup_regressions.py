from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_play_entrypoints_have_unambiguous_media_intent():
    source = (ROOT / "melody/plugins/music/play.py").read_text(encoding="utf-8")
    assert "await _play_core(client, message, video=False)" in source
    assert "await _play_core(client, message, video=True)" in source
    assert "await _play_core(client, message, video=False, force=True)" in source
    assert "await _play_core(client, message, video=True, force=True)" in source


def test_audio_api_clears_stale_video_track_state_at_core_boundary():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "Hard media invariant" in source
    assert "if not video:" in source
    assert "track.video = False" in source
    assert "video_flags=MediaStream.Flags.IGNORE" in source


def test_direct_and_download_paths_share_one_absolute_deadline():
    ytdl = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert 'os.getenv("RESOLVE_TIMEOUT", "8.0")' in ytdl
    assert "invidious_task = asyncio.ensure_future(" in ytdl
    assert "tasks.append(invidious_task)" in ytdl
    assert "timeout=7.0" not in ytdl
    assert "download_task = asyncio.create_task(" in call
    assert "return_when=asyncio.FIRST_COMPLETED" in call


def test_video_direct_stream_is_default_for_large_media():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert 'os.getenv("DIRECT_VIDEO_STREAM", "true")' in source
    assert "and (not video or _DIRECT_VIDEO_STREAM)" in source



def test_proxy_and_alternate_fallbacks_share_the_absolute_startup_deadline():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "fallback_path = await asyncio.wait_for(" in source
    assert "wait_for_download(" in source
    assert "min(_LOCAL_PLAY_TIMEOUT, _startup_remaining())" in source
    assert "min(_PLAY_PROBE_TIMEOUT, _startup_remaining())" in source
