"""Regression tests for the real root cause of the forced-download bug.

Heroku log (worker.1):

    Direct CDN stream unusable for 0PyHEaoZE1c (StreamProbeUnavailable:
    ffprobe could not read source (NoAudioSourceFound: ...)) — falling back
    to download   ->  downloaded in 12.9s, total 17.27s

py-tgcalls builds its ffmpeg/ffprobe command with ONE `-headers` option per
header.  `-headers` is a single AVOption, so every occurrence overwrites the
previous one: the User-Agent never reached the socket, googlevideo answered
403, ffprobe printed `{}` and py-tgcalls reported "no audio source" for a
perfectly good audio itag.  The URL was then blacklisted for 15 minutes, so
every later /play of that song paid a full download.

These tests pin all three halves of the fix:
  * headers are merged into a single CRLF-joined value (verified end-to-end
    against a real ffprobe run when the binary is available),
  * a googlevideo URL always carries a User-Agent matching its `c=` client,
  * an unreadable source is NEVER reported as "no audio track".
"""

import asyncio
import http.server
import logging
import pathlib
import shutil
import subprocess
import threading
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_patch_module():
    """Import utils/pytgcalls_patch.py with its pyrogram-heavy deps stubbed."""
    import sys

    if "melody.logging" not in sys.modules:
        melody = types.ModuleType("melody")
        melody.__path__ = []  # type: ignore[attr-defined]
        logging_mod = types.ModuleType("melody.logging")
        logging_mod.redact_sensitive_text = lambda text: text  # type: ignore[attr-defined]
        sys.modules.setdefault("melody", melody)
        sys.modules["melody.logging"] = logging_mod
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import importlib

    return importlib.import_module("utils.pytgcalls_patch")


patch = _load_patch_module()


class _HeaderEcho(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):  # noqa: N802 - stdlib naming
        type(self).seen.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"abcd")

    def log_message(self, *args):  # silence the test output
        pass


def _serve():
    server = http.server.HTTPServer(("127.0.0.1", 0), _HeaderEcho)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ── the bug itself ───────────────────────────────────────────────────────────


def test_repeated_headers_are_merged_into_one_option():
    command = patch.merge_header_options([
        "ffprobe",
        "-headers", "User-Agent: UA-1\r\n",
        "-headers", "Referer: https://www.youtube.com/\r\n",
        "-i", "https://rr3---sn-x.googlevideo.com/videoplayback?itag=140&c=WEB",
    ])
    assert command.count("-headers") == 1
    blob = command[command.index("-headers") + 1]
    assert "User-Agent: UA-1\r\n" in blob
    assert "Referer: https://www.youtube.com/\r\n" in blob
    # the merged option must still precede the input url
    assert command.index("-headers") < command.index("-i")


def test_ffprobe_actually_receives_every_merged_header():
    """End-to-end proof: the unmerged form loses the User-Agent, ours keeps it."""
    if not shutil.which("ffprobe"):
        return  # binary-less CI: the unit assertions above still cover the fix
    server = _serve()
    port = server.server_port
    _HeaderEcho.seen = []
    try:
        unmerged = [
            "ffprobe", "-v", "error",
            "-headers", "User-Agent: MELODY-UA",
            "-headers", "Referer: https://www.youtube.com/",
            "-i", f"http://127.0.0.1:{port}/unmerged",
        ]
        subprocess.run(unmerged, capture_output=True, timeout=20)
        subprocess.run(
            patch.merge_header_options(
                ["ffprobe", "-v", "error"]
                + unmerged[3:7]
                + ["-i", f"http://127.0.0.1:{port}/merged"]
            ),
            capture_output=True,
            timeout=20,
        )
    finally:
        server.shutdown()

    unmerged_seen, merged_seen = _HeaderEcho.seen[0], _HeaderEcho.seen[1]
    # the bug: only the LAST -headers survived, ffmpeg sent its default UA
    assert unmerged_seen.get("User-Agent") != "MELODY-UA"
    # the fix: both headers arrive
    assert merged_seen.get("User-Agent") == "MELODY-UA"
    assert merged_seen.get("Referer") == "https://www.youtube.com/"


def test_googlevideo_url_without_a_user_agent_gets_its_client_ua():
    command = patch.merge_header_options([
        "ffprobe",
        "-i", "https://rr1---sn-x.googlevideo.com/videoplayback?c=ANDROID&itag=140",
    ])
    blob = command[command.index("-headers") + 1]
    assert "com.google.android.youtube/" in blob


def test_client_user_agent_matches_the_signing_client():
    base = "https://rr1---sn-x.googlevideo.com/videoplayback?itag=140&c="
    assert "android.apps.youtube.music" in patch.client_user_agent(base + "ANDROID_MUSIC")
    assert "com.google.ios.youtube/" in patch.client_user_agent(base + "IOS")
    assert "Chrome/" in patch.client_user_agent(base + "WEB")
    assert "Chrome/" in patch.client_user_agent("https://example.com/a.m4a")


def test_non_googlevideo_command_without_headers_is_left_alone():
    command = ["ffprobe", "-i", "/tmp/song.m4a"]
    assert patch.merge_header_options(list(command)) == command


# ── the poisoned blacklist ───────────────────────────────────────────────────


def test_unreadable_source_is_not_reported_as_missing_audio():
    """A refused/dead URL must return None (inconclusive), never False."""
    if not shutil.which("ffprobe"):
        return
    verdict = asyncio.run(
        patch.probe_audio_streams("http://127.0.0.1:9/dead.m4a", {})
    )
    assert verdict is None


def test_real_audio_file_is_reported_as_having_audio():
    if not (shutil.which("ffprobe") and shutil.which("ffmpeg")):
        return
    sample = pathlib.Path("/tmp/melody_probe_sample.m4a")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=1", str(sample)],
        capture_output=True, timeout=60,
    )
    if not sample.exists():
        return
    assert asyncio.run(patch.probe_audio_streams(str(sample), {})) is True


def test_video_only_file_is_confirmed_as_having_no_audio():
    if not (shutil.which("ffprobe") and shutil.which("ffmpeg")):
        return
    sample = pathlib.Path("/tmp/melody_probe_silent.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "testsrc=size=64x64:rate=10:duration=1", str(sample)],
        capture_output=True, timeout=60,
    )
    if not sample.exists():
        return
    assert asyncio.run(patch.probe_audio_streams(str(sample), {})) is False


def test_no_audio_track_is_a_probe_failure_subclass():
    assert issubclass(patch.NoAudioTrack, patch.StreamProbeUnavailable)


# ── only a CONFIRMED audio-less url may be blacklisted ───────────────────────


def _load_call_blacklist_guard() -> str:
    return (ROOT / "melody/core/call.py").read_text(encoding="utf-8")


def test_call_py_blacklists_only_confirmed_no_audio_sources():
    src = _load_call_blacklist_guard()
    assert 'type(play_exc).__name__ == "NoAudioTrack"' in src
    assert 'type(alt_exc).__name__ == "NoAudioTrack"' in src
    # the old string-sniffing guard must be gone: it fired on every refused URL
    assert '"no audio source" in str(play_exc).lower()' not in src
    assert '"no audio source" in str(alt_exc).lower()' not in src
    assert '"NoAudioTrack",' in src  # routed to the download fallback, not a crash


def test_resolvers_never_ship_a_bot_user_agent():
    src = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    # No bot UA anywhere: googlevideo 403s it, and several Invidious mirrors
    # rate-limit it too.
    assert "MelodyBot" not in src
    assert src.count("picked[\"headers\"] = _stream_headers(") == 3


def test_stream_headers_fills_in_a_matching_user_agent():
    src = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    start = src.index("def _stream_headers(")
    end = src.index("def _resolve_stream_urls_innertube")
    namespace: dict = {}
    exec(compile(src[start:end], "stream_headers", "exec"), namespace)  # noqa: S102
    stream_headers = namespace["_stream_headers"]

    url = "https://rr1---sn-x.googlevideo.com/videoplayback?itag=140&c=IOS"
    assert "com.google.ios.youtube/" in stream_headers(None, url)["User-Agent"]
    assert "com.google.ios.youtube/" in stream_headers({"Referer": "x"}, url)["User-Agent"]
    kept = stream_headers({"User-Agent": "UA-X", "Cookie": "secret"}, url)
    assert kept["User-Agent"] == "UA-X"
    assert "Cookie" not in kept  # cookies must never leak into the ffmpeg command


def test_cloud_reachability_budget_leaves_room_for_a_slow_pop():
    src = (ROOT / "utils/pytgcalls_patch.py").read_text(encoding="utf-8")
    assert 'os.getenv("REMOTE_CHECK_CLOUD_BUDGET", "1.5")' in src


def test_build_command_patch_is_installed():
    src = (ROOT / "utils/pytgcalls_patch.py").read_text(encoding="utf-8")
    assert "_patch_build_command(_ffmpeg_mod, _media_stream_mod)" in src
    logging.getLogger(__name__).info("build_command patch wired into the probe patch")
