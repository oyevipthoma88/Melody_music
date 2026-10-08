"""melody.core package boot.

⚡ ShrutiBots YT API integration (active only when SHRUTI_API_KEY is set).

ROOT CAUSE: YouTube blocks Heroku IPs and burns cookies, so yt-dlp hits the
"Sign in to confirm you're not a bot" wall and /play + /vplay become slow or
fail. ShrutiBots downloads on ITS servers and returns the file directly.

What this does (no edits to the huge ytdl.py / call.py needed):
  1. /play + /vplay: every download tries ShrutiAPI FIRST (audio or video);
     any miss falls back to the old yt-dlp/JioSaavn chain unchanged.
  2. Skips the blocked direct-CDN yt-dlp race (5-7s wasted per /play on
     Heroku). Set FORCE_DIRECT_STREAM=1 to re-enable it.
  3. Long /vplay (>15 min) was direct-CDN only -> always failed on Heroku;
     now allowed through the API (up to 60 min).
  4. Startup + every-30-min "🟢 ShrutiAPI ACTIVE / 🔴 INACTIVE" log lines and
     a status message in LOG_GROUP_ID.
"""
import os as _os


def _shruti_on() -> bool:
    return bool((_os.environ.get("SHRUTI_API_KEY") or _os.environ.get("SHRUTI_API_KEYS") or "").strip())


if _shruti_on():
    try:
        if float(_os.environ.get("VIDEO_DIRECT_ONLY_SECONDS") or 0) < 3600:
            _os.environ["VIDEO_DIRECT_ONLY_SECONDS"] = "3600"
    except ValueError:
        _os.environ["VIDEO_DIRECT_ONLY_SECONDS"] = "3600"

    import asyncio as _asyncio
    import sys as _sys
    import threading as _th
    import time as _time

    def _log(level: str, msg: str, *args) -> None:
        try:
            from melody.logging import LOGGER
            getattr(LOGGER, level)(msg, *args)
        except Exception:  # noqa: BLE001
            pass

    def _patch_ytdl(mod) -> None:
        orig_direct = getattr(mod, "should_try_direct_stream", None)
        if orig_direct is not None and not getattr(orig_direct, "_shruti", False):
            def should_try_direct_stream() -> bool:
                if _os.getenv("FORCE_DIRECT_STREAM", "0").strip().lower() in {"1", "true", "yes", "on"}:
                    return orig_direct()
                return False
            should_try_direct_stream._shruti = True  # type: ignore[attr-defined]
            mod.should_try_direct_stream = should_try_direct_stream

        orig_dl = getattr(mod, "_download_audio_locked", None)
        if orig_dl is not None and not getattr(orig_dl, "_shruti", False):
            async def _download_audio_locked(video_id, audio_only, tag, cancel_event=None, early_state=None):
                from melody.core import shruti_api as _sh
                valid = getattr(mod, "is_valid_video_id", lambda v: bool(v) and len(str(v)) == 11)
                if _sh.enabled() and valid(video_id):
                    try:
                        sp = await _sh.download(video_id, tag, audio_only=audio_only,
                                                cancel_event=cancel_event)
                    except _asyncio.CancelledError:
                        exc_cls = getattr(mod, "_DownloadCancelled", None)
                        if exc_cls is not None and cancel_event is not None and cancel_event.is_set():
                            raise exc_cls("shruti download superseded")
                        raise
                    if sp:
                        if early_state is not None:
                            try:
                                if not early_state.ready.is_set():
                                    early_state.path = sp
                                    early_state.ready.set()
                            except Exception:  # noqa: BLE001
                                pass
                        return sp
                    _log("info", "ShrutiAPI miss for %s — falling back to yt-dlp/JioSaavn chain", video_id)
                return await orig_dl(video_id, audio_only, tag,
                                     cancel_event=cancel_event, early_state=early_state)
            _download_audio_locked._shruti = True  # type: ignore[attr-defined]
            mod._download_audio_locked = _download_audio_locked
        _log("info", "⚡ ShrutiAPI wired into /play + /vplay (API first, yt-dlp fallback)")

    def _patch_main(mod) -> None:
        orig = getattr(mod, "send_startup_log", None)
        if orig is None or getattr(orig, "_shruti", False):
            return

        async def send_startup_log(bot, assistant, *a, **k):
            try:
                await orig(bot, assistant, *a, **k)
            finally:
                try:
                    from melody.core import shruti_api as _sh
                    await _sh.health_check()
                    _sh.start_monitor()
                    await _sh.send_status(bot, assistant)
                except Exception as exc:  # noqa: BLE001
                    _log("warning", "ShrutiAPI status log failed: %r", exc)

        send_startup_log._shruti = True  # type: ignore[attr-defined]
        mod.send_startup_log = send_startup_log

    def _watch() -> None:
        done_y = done_m = False
        end = _time.time() + 600
        while _time.time() < end and not (done_y and done_m):
            y = _sys.modules.get("melody.core.ytdl")
            if (not done_y and y is not None and hasattr(y, "_download_audio_locked")
                    and hasattr(y, "should_try_direct_stream")):
                _patch_ytdl(y)
                done_y = True
            m = _sys.modules.get("__main__")
            if not done_m and m is not None and hasattr(m, "send_startup_log"):
                _patch_main(m)
                done_m = True
            _time.sleep(0.02)

    _th.Thread(target=_watch, name="shruti-patch", daemon=True).start()


from melody import _rootfix as _rf  # noqa: E402,F401  (song-end stuck-loop fix)
