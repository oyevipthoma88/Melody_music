from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]


def _load_classifier():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    namespace = {}
    start = source.index("_PERMANENT_DOWNLOAD_MARKERS =")
    end = source.index("\n\ndef _extract_with_retries", start)
    exec(compile(source[start:end], "melody/core/ytdl.py", "exec"), namespace)
    return namespace["_is_permanent_download_error"]


def _source():
    return (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")


def test_mobile_browser_restriction_is_retryable():
    fn = _load_classifier()
    error = RuntimeError(
        "ERROR: [youtube] IZLDEWg9JkU: Watch on the YouTube app\n"
        "This content isn’t available on your mobile browser"
    )
    assert fn(error) is False


def test_removed_private_and_geo_content_stays_terminal():
    fn = _load_classifier()
    assert fn(RuntimeError("This video is unavailable")) is True
    assert fn(RuntimeError("This is a private video")) is True
    assert fn(RuntimeError("Not available in your country")) is True


def test_retry_ladder_contains_mobile_compatible_profiles():
    source = _source()
    assert '["android_vr", "web_safari"]' in source
    assert '["ios", "mweb"]' in source
    assert '["tv", "web"]' in source
