"""Regression tests for the NoAudioSourceFound direct-stream bug.

Heroku log (root cause):

    Direct CDN stream unusable for S7VvmNz9Emk (StreamProbeUnavailable:
    ffprobe could not read source (NoAudioSourceFound: No audio source found
    on "https://rr3---sn-p5qs7nzr.googlevideo.com/videoplayback")) —
    falling back to download.

`_pick_stream_formats()`'s metadata-light last resort accepted ANY http(s)
format for an audio request, including adaptive VIDEO-ONLY itags — and since
it picks the cheapest by (height, tbr), a video-only itag won. ffprobe found
no audio track, the forced retry re-picked the identical itag, and the song
fell all the way back to a full 13-20s download.

These tests exercise the picker directly (the surrounding module needs
pyrogram, which the test environment does not install).
"""

import logging
import os
import pathlib
import time as _time_mod

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_picker():
    src = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    start = src.index("_NO_AUDIO_URLS: dict[str, float] = {}")
    end = src.index("def _invidious_streams_sync")
    ns = {
        "os": os,
        "_time_mod": _time_mod,
        "LOGGER": logging.getLogger("test_no_audio"),
        "_stream_url_cache": {},
    }
    exec(compile(src[start:end], "picker", "exec"), ns)  # noqa: S102
    return ns


VIDEO_ONLY = {
    "url": "https://rr3---sn-p5qs7nzr.googlevideo.com/videoplayback?itag=136",
    "vcodec": "avc1.4d401f",
    "acodec": "none",
    "protocol": "https",
    "height": 720,
    "tbr": 1200,
}
AUDIO_ONLY = {
    "url": "https://rr3---sn-p5qs7nzr.googlevideo.com/videoplayback?itag=140",
    "vcodec": "none",
    "acodec": "mp4a.40.2",
    "protocol": "https",
    "abr": 128,
}


def test_video_only_format_is_never_returned_as_the_audio_source():
    ns = _load_picker()
    assert ns["_pick_stream_formats"]({"formats": [VIDEO_ONLY]}, False) == {}


def test_audio_only_format_still_wins_when_present():
    ns = _load_picker()
    picked = ns["_pick_stream_formats"]({"formats": [VIDEO_ONLY, AUDIO_ONLY]}, False)
    assert picked["audio"] == AUDIO_ONLY["url"]
    assert picked["video"] is None


def test_url_that_probed_without_audio_is_not_picked_again():
    ns = _load_picker()
    ns["note_no_audio_url"](AUDIO_ONLY["url"])
    assert ns["_pick_stream_formats"]({"formats": [VIDEO_ONLY, AUDIO_ONLY]}, False) == {}


def test_metadata_light_url_without_codec_info_is_still_accepted():
    ns = _load_picker()
    light = {
        "url": "https://rr3---sn-p5qs7nzr.googlevideo.com/videoplayback?itag=18",
        "protocol": "https",
    }
    picked = ns["_pick_stream_formats"]({"formats": [light]}, False)
    assert picked["audio"] == light["url"]
