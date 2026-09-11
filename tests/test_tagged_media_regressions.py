from types import SimpleNamespace
from pathlib import Path

from melody.core.ytdl import has_playable_media


ROOT = Path(__file__).resolve().parents[1]


def _message(**media):
    fields = {"audio": None, "video": None, "voice": None, "video_note": None, "document": None}
    fields.update(media)
    return SimpleNamespace(**fields)


def test_tagged_audio_voice_and_video_are_detected():
    assert has_playable_media(_message(audio=SimpleNamespace(file_name="song.mp3")))
    assert has_playable_media(_message(voice=SimpleNamespace(file_name="voice.ogg")))
    assert has_playable_media(_message(video=SimpleNamespace(file_name="clip.mp4")))
    assert has_playable_media(_message(video_note=SimpleNamespace(file_name="note.mp4")))


def test_tagged_documents_use_mime_or_safe_filename_extension():
    assert has_playable_media(_message(document=SimpleNamespace(
        mime_type="application/octet-stream", file_name="song.mp3"
    )))
    assert has_playable_media(_message(document=SimpleNamespace(
        mime_type="video/mp4", file_name="uploaded.bin"
    )))
    assert not has_playable_media(_message(document=SimpleNamespace(
        mime_type="application/octet-stream", file_name="notes.txt"
    )))


def test_proxy_is_optional_and_tagged_media_has_download_fallback():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    marker = '"tagged range proxy unavailable id=%s error=%s"'
    start = source.index(marker)
    tail = source[start:source.index("            ext = file_name", start)]
    assert "return None" not in tail


def test_drm_download_error_is_treated_as_unavailable_content():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert '"this video is drm protected"' in source
    assert '"drm protected"' in source
