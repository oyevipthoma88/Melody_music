import asyncio
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_remote_reachability_probe_is_cached_for_camera_and_microphone(monkeypatch):
    from utils import pytgcalls_patch as patch

    url = "https://cdn.example/video?expire=4102444800"
    calls = []
    patch._remote_reach_cache.clear()

    def fake_probe(probe_url, timeout, headers):
        calls.append((probe_url, timeout, headers))
        return True

    monkeypatch.setattr(patch, "_http_reachable_sync", fake_probe)

    async def run():
        first = await patch._remote_reachable(url, {"User-Agent": "test"})
        second = await patch._remote_reachable(url, {"User-Agent": "test"})
        return first, second

    assert asyncio.run(run()) == (True, True)
    assert len(calls) == 1


def test_stream_check_cache_is_bounded_and_configurable():
    source = (ROOT / "utils/pytgcalls_patch.py").read_text(encoding="utf-8")
    assert "REMOTE_CHECK_CACHE_TTL" in source
    assert "_remote_reach_cache" in source
    assert "_REMOTE_REACH_CACHE_MAX = 256" in source
    assert "if cached_until > now" in source


def test_dead_remote_source_skips_slow_ffprobe_and_uses_download_fallback():
    source = (ROOT / "utils/pytgcalls_patch.py").read_text(encoding="utf-8")
    assert "remote source failed bounded ranged preflight" in source
    assert "raise StreamProbeUnavailable" in source
    assert "if \".m3u8\" in str(path).lower()" in source


def test_current_track_has_one_interactive_direct_resolver_owner():
    source = (ROOT / "melody/plugins/music/play.py").read_text(encoding="utf-8")
    assert "Do not resolve the current track here" in source
    assert "duplicate same-key resolve" in source
    assert "return []" in source
