import asyncio
from pathlib import Path

from melody.core import ytdl


def test_attached_log_message_is_classified_as_unavailable():
    """The curly-apostrophe yt-dlp error must use alternate-track recovery."""
    source = Path("melody/core/call.py").read_text(encoding="utf-8")
    assert '"this content isn’t available"' in source
    assert '"this content isn\'t available"' in source


async def _run():
    original_search = ytdl.search_youtube
    original_resolve = ytdl.resolve_stream_urls
    try:
        async def fake_search(query, limit=5):
            return [
                {"id": "dead0000001", "title": "Unavailable result", "duration": 200,
                 "url": "https://www.youtube.com/watch?v=dead0000001", "thumbnail": "", "uploader": "x"},
                {"id": "good0000002", "title": "Playable result", "duration": 210,
                 "url": "https://www.youtube.com/watch?v=good0000002", "thumbnail": "", "uploader": "y"},
            ]

        async def fake_resolve(video_id, want_video=False):
            if video_id == "dead0000001":
                raise ValueError("Video unavailable. This content isn't available.")
            return {"audio": "https://cdn.example/good.webm"}

        ytdl.search_youtube = fake_search
        ytdl.resolve_stream_urls = fake_resolve
        result = await ytdl.find_playable_candidate("Agar Tum Saath Ho", "dead0000001")
        assert result["id"] == "good0000002"
        assert result["stream_url"] == "https://cdn.example/good.webm"
    finally:
        ytdl.search_youtube = original_search
        ytdl.resolve_stream_urls = original_resolve


if __name__ == "__main__":
    asyncio.run(_run())
    print("unavailable candidate recovery: PASS")
