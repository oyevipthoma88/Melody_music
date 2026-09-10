from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_error_code_152_marker_is_classified_as_unavailable_content():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert '"video is unavailable"' in source
    assert '"error code 152"' in source


def test_unavailable_media_branch_precedes_generic_crash_logging():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert '"video is unavailable"' in source
    assert '"error code 152"' in source
    assert "if _is_unavailable_media_error(exc):" in source
    assert 'f"_stream_track failed in {chat_id}"' in source
