from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_innertube_search_initializes_empty_response_before_client_race():
    """All failed client contexts must return None, not raise NameError."""
    source = _source("melody/core/ytdl.py")
    start = source.index("def _innertube_search_sync")
    end = source.index("# Keep Invidious", start)
    function = source[start:end]
    assert "data = None" in function
    assert "if not data:" in function


def test_search_failure_keeps_fallback_chain_alive():
    source = _source("melody/core/ytdl.py")
    start = source.index("async def _get_video_info_once")
    end = source.index("# ─────────────────────────────────────────────────────────────────────────────\n#  Full file download", start)
    function = source[start:end]
    assert "_innertube_search_sync(url_or_query)" in function
    assert "_invidious_search_sync" in function
    assert "_ytdlp_info" in function


def test_non_video_search_playlist_ids_are_rejected():
    source = _source("melody/core/ytdl.py")
    start = source.index("def _normalize_info")
    end = source.index("def _innertube_next_sync", start)
    function = source[start:end]
    assert 'info.get("_type") in ("playlist", "multi_video", "url_transparent")' in function
    assert "is_valid_video_id(vid)" in function
