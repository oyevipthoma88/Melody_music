"""Root fix: song end par last hissa atakta rehta tha, phir bot VC chhod deta tha.

ROOT CAUSE: _resume_if_premature_end() treats every StreamEnded before
(duration - 8s) as "file still downloading" and seeks back to elapsed-1.
YouTube metadata duration is often a few seconds longer than the real audio
(and the playback clock lags a little after startup), so at the GENUINE end
the bot re-played the last second up to 15 times (stuck loop) and only then
advanced / left the VC.

FIX: an end is genuine when
  * a previous resume made no real progress (< 5s played since), or
  * the file is fully downloaded / not growing and >= 80% was played.
Only a real mid-song EOF on a still-growing file is resumed now.
"""
import os
import sys
import threading
import time

_last_resume: dict = {}


def _patch(mod) -> None:
    orig = getattr(mod, "_resume_if_premature_end", None)
    if orig is None or getattr(orig, "_rootfix", False):
        return

    async def _resume_if_premature_end(chat_id):
        try:
            track = mod.get_current(chat_id)
            if not track:
                return False
            elapsed = int(mod.get_playback_position(chat_id) or 0)
            duration = int(getattr(track, "duration", 0) or 0)
            key = f"{chat_id}:{track.video_id}"
            last = _last_resume.get(key)
            if last is not None and 0 <= elapsed - last < 5:
                _last_resume.pop(key, None)
                mod.LOGGER.info("⏹ %s in %s: no progress after resume (%ss) — genuine end, advancing",
                                track.video_id, chat_id, elapsed)
                return False
            video = mod._is_video.get(chat_id, False)
            try:
                from melody.core.ytdl import is_download_in_progress
                growing = bool(is_download_in_progress(track.video_id, audio_only=not video))
            except Exception:  # noqa: BLE001
                growing = True
            if not growing and duration > 0 and elapsed >= duration * 0.8:
                _last_resume.pop(key, None)
                return False
        except Exception:  # noqa: BLE001
            return await orig(chat_id)
        resumed = await orig(chat_id)
        if resumed:
            _last_resume[key] = elapsed
        else:
            _last_resume.pop(key, None)
        return resumed

    _resume_if_premature_end._rootfix = True  # type: ignore[attr-defined]
    mod._resume_if_premature_end = _resume_if_premature_end
    try:
        mod.LOGGER.info("✅ song-end fix wired (no more stuck last-second loop)")
    except Exception:  # noqa: BLE001
        pass


def _watch() -> None:
    end = time.time() + 600
    while time.time() < end:
        m = sys.modules.get("melody.core.call")
        if m is not None and hasattr(m, "_resume_if_premature_end") and hasattr(m, "get_playback_position"):
            _patch(m)
            return
        time.sleep(0.05)


if os.environ.get("DISABLE_SONG_END_FIX", "0") not in {"1", "true"}:
    threading.Thread(target=_watch, name="songend-rootfix", daemon=True).start()
