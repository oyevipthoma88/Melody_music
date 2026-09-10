"""
🔥 Animated status messages (requirement #5)

Replaces the old static "🔍 Searching..." placeholder with a message that
cycles through random fire/celebration emojis while real work (VC join +
yt-dlp search) happens in the background — purely cosmetic, never blocks
or slows down the actual command.

─── WHY THIS FILE IS RATE-LIMITED (Heroku log fix) ──────────────────────────
The logs were full of:

    WARNING pyrogram.session.session: [MelodyBot] Waiting for 40 seconds
    before continuing (required by "messages.EditMessage")

Telegram FloodWait on `messages.EditMessage`. Pyrogram handles a FloodWait by
sleeping — and that sleep blocks the WHOLE bot session, not just the animation.
So a cosmetic emoji animation editing a message every 2.5 s was stalling real
work (VC join, /play replies) for up to 40 s at a time. That is the actual
reason "gana play hone me bohot time lagta hai".

Fixes applied here:
  • slower interval (6 s instead of 2.5 s)
  • a hard cap on frames per animation (no endless editing)
  • a process-wide token bucket so ALL animations together stay well under
    Telegram's edit budget
  • a FloodWait trip-switch: the first FloodWait stops the animation (and
    pauses every animation globally for the flood window) instead of
    re-triggering it forever
"""
import asyncio
import random
import time

from pyrogram.errors import FloodWait, RPCError

FIRE_EMOJIS = [
    "🔥", "💥", "⚡", "✨", "🎆", "🎇", "🌟", "💫", "🎉", "🚀", "🎶", "🪩", "🎵", "💃",
]

# Cosmetic edits are allowed at most this often, process-wide (seconds).
_GLOBAL_MIN_GAP = 2.0
_last_edit_at = 0.0
# While a FloodWait is in effect no animation edits are attempted at all.
_paused_until = 0.0
_lock = asyncio.Lock()


async def _may_edit() -> bool:
    """Token bucket shared by every AnimatedStatus in the process."""
    global _last_edit_at
    now = time.monotonic()
    if now < _paused_until:
        return False
    async with _lock:
        if time.monotonic() - _last_edit_at < _GLOBAL_MIN_GAP:
            return False
        _last_edit_at = time.monotonic()
    return True


def _pause(seconds: float) -> None:
    global _paused_until
    _paused_until = max(_paused_until, time.monotonic() + max(seconds, 5.0))


class AnimatedStatus:
    """
    Edits `message` every `interval` seconds with a random emoji frame until
    `.stop()` is called. Runs as its own background asyncio task, so
    starting/stopping it never adds latency to the caller's real work.

    `max_frames` caps how many edits a single animation may ever perform —
    playback normally starts long before that, and an un-capped animation is
    what earned the bot its FloodWaits.
    """

    def __init__(
        self,
        message,
        label: str = "Getting your vibe ready",
        interval: float = 6.0,
        max_frames: int = 6,
    ):
        self._message = message
        self._label = label
        self._interval = max(interval, 4.0)
        self._max_frames = max_frames
        self._task: "asyncio.Task | None" = None
        self._stopped = asyncio.Event()

    def start(self) -> "AnimatedStatus":
        self._task = asyncio.create_task(self._loop())
        return self

    async def _loop(self):
        last = None
        frames = 0
        while not self._stopped.is_set() and frames < self._max_frames:
            emoji = random.choice(FIRE_EMOJIS)
            while emoji == last and len(FIRE_EMOJIS) > 1:
                emoji = random.choice(FIRE_EMOJIS)
            last = emoji
            if await _may_edit():
                frames += 1
                try:
                    await self._message.edit(f"{emoji} {self._label} {emoji}")
                except FloodWait as exc:
                    # Never retry a cosmetic edit through a flood window: that
                    # is what froze the whole session for 40 s at a time.
                    _pause(float(getattr(exc, "value", 0) or 0))
                    return
                except RPCError:
                    return
                except Exception:
                    return
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        self._stopped.set()
        if self._task:
            try:
                await self._task
            except Exception:
                pass
            self._task = None
