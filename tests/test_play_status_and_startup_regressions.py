import asyncio
import os
from pathlib import Path

os.environ.setdefault("MONGO_DB_URI", "mongodb://localhost:27017/musicbot_test")
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_play_status_mutations_are_stale_message_safe():
    source = (ROOT / "melody/plugins/music/play.py").read_text(encoding="utf-8")
    assert "def _is_stale_message_error" in source
    assert "async def _safe_processing_edit" in source
    assert "async def _safe_processing_delete" in source
    assert "MESSAGE_ID_INVALID" in source
    assert source.count("await processing.edit(") == 1
    assert "await _safe_processing_edit(processing, message" in source


def test_safe_processing_edit_replies_when_status_message_is_invalidated():
    import melody

    class BotStub:
        def on_message(self, *_args, **_kwargs):
            return lambda function: function

    melody.bot = BotStub()
    from melody.plugins.music.play import _safe_processing_edit

    class MessageIdInvalid(Exception):
        pass

    class Processing:
        async def edit(self, *args, **kwargs):
            raise MessageIdInvalid("Telegram says: [400 MESSAGE_ID_INVALID]")

    class Source:
        chat = SimpleNamespace(id=-100)
        def __init__(self):
            self.replies = []

        async def reply(self, *args, **kwargs):
            self.replies.append((args, kwargs))
            return "replacement"

    source = Source()
    result = asyncio.run(_safe_processing_edit(Processing(), source, "status"))
    assert result == "replacement"
    assert source.replies[0][0] == ("status",)


def test_direct_negative_cache_does_not_trigger_an_immediate_second_resolve():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert 'cached_failure = "cached failure" in str(cached_exc).lower()' in source
    assert "direct source negative-cached" in source
    assert "force=True" in source


def test_stream_fallback_keeps_audio_early_handoff_and_bounded_probe():
    call_source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    ytdl_source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "allow_early=not video" in call_source
    assert "_PLAY_PROBE_TIMEOUT" in call_source
    assert "_EARLY_AUDIO_HANDOFF_ENABLED" in ytdl_source
    assert "_early_audio_path_is_safe" in ytdl_source
    assert "_download_futures" in ytdl_source


def test_queue_prefetch_starts_download_before_optional_direct_resolve():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    download_start = source.index("download_task = asyncio.create_task(")
    resolver_start = source.index("resolve_task = None", download_start)
    assert download_start < resolver_start
    assert "path = await download_task" in source
    assert "queued track wait" in source


def test_invidious_direct_route_is_present_and_bounded():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "def _resolve_stream_urls_invidious" in source
    assert "_INVIDIOUS_INSTANCES[:6]" in source
    assert "invidious_task = asyncio.ensure_future(" in source
    assert 'os.getenv("RESOLVE_TIMEOUT", "8.0")' in source
    assert "adaptiveFormats" in source
    assert "hlsUrl" in source


def test_resolver_attempts_alternate_direct_provider_after_primary_race():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    race_start = source.index("invidious_task = asyncio.ensure_future(")
    deadline = source.index("deadline =", race_start)
    assert race_start < deadline
    assert "tasks.append(invidious_task)" in source[race_start:deadline]
    assert "timeout=7.0" not in source


def test_invidious_direct_route_normalizes_adaptive_audio(monkeypatch):
    from melody.core import ytdl

    class Response:
        status_code = 200

        def json(self):
            return {
                "adaptiveFormats": [{
                    "url": "https://cdn.example/audio.webm?expire=4102444800",
                    "type": "audio/webm; codecs=opus",
                    "bitrate": 96000,
                }],
                "formatStreams": [],
            }

    class Client:
        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(ytdl, "get_http_sync_client", lambda: Client())
    monkeypatch.setattr(ytdl, "_INVIDIOUS_INSTANCES", ["https://example.invalid"])
    result = ytdl._resolve_stream_urls_invidious("iAIBF2ngbWY", False)
    assert result["audio"].startswith("https://cdn.example/audio.webm")
    assert result["video"] is None


def test_cloud_autoplay_prefetch_skips_only_video_on_small_hosts():
    source = (ROOT / "melody/core/autoplay.py").read_text(encoding="utf-8")
    assert "and not _cloud_prefetch_enabled() and want_video" in source
    assert "Audio prefetch is intentionally allowed" in source
    assert "cloud video pre-download skipped" in source
