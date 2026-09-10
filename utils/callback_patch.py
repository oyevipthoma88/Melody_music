"""🛡 Safe `CallbackQuery.answer()` patch.

BUG (reported log):
    Error in close_callback: Telegram says: [400 QUERY_ID_INVALID]
    ... pyrogram.errors.exceptions.bad_request_400.QueryIdInvalid

WHY IT HAPPENS
--------------
A callback query id is only valid for a short window (and only ONCE). It goes
stale whenever:
  * the user taps a button on an old card and the bot answers late,
  * the same query is answered twice (handler + decorator + retry),
  * the bot restarted between the tap and the answer (Heroku dyno cycling),
  * two handlers match the same callback_data.

Answering a stale/duplicate query is *never* actionable — the tap was already
handled by Telegram's client — but the raised `QueryIdInvalid` propagated up
through `utils/decorators.error_handler`, which then spammed the owner's error
log channel (and, for `close_callback`, aborted the handler BEFORE the card was
actually deleted, so the Close button looked broken).

THE FIX
-------
Patch `CallbackQuery.answer` once at startup so these expected, non-actionable
Telegram errors are swallowed (debug-logged) instead of raised. Every callback
handler in the bot benefits without touching ~85 call sites, and real errors
(flood waits, network failures, anything else) still propagate normally.
"""

from __future__ import annotations

import functools
import logging

from pyrogram.types import CallbackQuery

log = logging.getLogger(__name__)

# Expected, non-actionable answer failures: the query is simply gone.
_STALE_ERRORS = (
    "QUERY_ID_INVALID",
    "QUERY_ID_EMPTY",
    "MESSAGE_ID_INVALID",
    "BOT_RESPONSE_TIMEOUT",
)

_applied = False


def _is_stale(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".upper()
    return any(marker in text for marker in _STALE_ERRORS)


def apply_callback_patch() -> None:
    global _applied
    if _applied or getattr(CallbackQuery.answer, "_melody_patched", False):
        return

    original = CallbackQuery.answer

    @functools.wraps(original)
    async def answer(self, *args, **kwargs):
        try:
            return await original(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - intentionally broad
            if _is_stale(exc):
                log.debug("Ignoring stale callback answer: %s", exc)
                return None
            raise

    answer._melody_patched = True  # type: ignore[attr-defined]
    CallbackQuery.answer = answer  # type: ignore[assignment]
    _applied = True
    log.info("🛡 CallbackQuery.answer patched (stale QUERY_ID_INVALID ignored).")


__all__ = ["apply_callback_patch"]
