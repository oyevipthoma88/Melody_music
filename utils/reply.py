"""Small helper for Telegram reply targets.

Pyrogram/kurigram deprecated `reply_to_message_id=` (it logged a WARNING on
every single send). The replacement is `reply_parameters=ReplyParameters(...)`,
but that object must be omitted entirely when there is nothing to reply to —
`ReplyParameters(message_id=None)` is invalid. `reply_params()` returns None in
that case, which Pyrogram treats as "no reply".
"""
from __future__ import annotations

from pyrogram.types import ReplyParameters


def reply_params(message_id: "int | None"):
    """ReplyParameters for a valid message id, else None."""
    if not message_id:
        return None
    try:
        return ReplyParameters(message_id=int(message_id))
    except Exception:
        return None
