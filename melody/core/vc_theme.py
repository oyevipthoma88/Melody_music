"""
🎨 VC card theme — thin re-export of the single project-wide Melody theme.

Every VC surface (join, left, invite, started, ended, chat log) keeps calling
`vc_card()` / `headline()` / `tag()` exactly as before; the styling itself now
lives in `utils/melody_theme.py` so admin, owner, misc and music cards share
one identical Modi–Meloni tricolour look.
"""
from __future__ import annotations

from utils.melody_theme import (  # noqa: F401
    BULLET,
    FLAGS,
    RIBBON,
    card as vc_card,
    esc,
    headline,
    meter,
    rule,
    stamp,
    tag,
    tag_list,
)

__all__ = [
    "RIBBON", "FLAGS", "BULLET", "vc_card", "headline", "esc", "tag",
    "tag_list", "rule", "stamp", "meter",
]
