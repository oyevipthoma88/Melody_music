"""
🎨 Modi–Meloni "Melody" theme — the ONE place every card in the bot is styled.

WHY THIS EXISTS
---------------
Cards were styled in two different places: `melody/core/vc_theme.py` (the
tricolour VC suite) and `utils/admin_tools.card()` (a plain "🎶 title" header
used by every admin / owner / misc plugin). Same bot, two looks — and any
restyle had to be done twice.

This module owns the theme. `vc_theme` and `admin_tools.card()` are now thin
re-exports of it, so ONE edit here restyles every card in the bot.

THEME
-----
India 🇮🇳 and Italy 🇮🇹 share the same tricolour family — saffron / white /
green — so the ribbon 🧡🤍💚 is the signature of every Melody card, framed
with Telegram blockquotes and the project's bold-italic `fancy()` headline
font.

Every glyph used here has a VERIFIED premium (animated) custom-emoji id in
`utils/emoji_map`, so the whole frame renders premium after
`auto_premium_emoji()` — no random ids, no wrong artwork.
"""
from __future__ import annotations

import html
import time

# Every glyph below is checked against EMOJI_ID_MAP by
# tools/verify_premium_emoji.py, so the whole frame renders as animated
# premium emoji — never plain unicode.
RIBBON = "🧡🤍💚"
FLAGS = "🇮🇳 🤝 🇮🇹"
BULLET = "🔸"          # replaces the old plain dingbat, which could never be premium
SUB_BULLET = "🔹"
NOTE = "🎶"
SCORE = "🎼"
SPARK = "✨"
ARROW = "➡️"
CROWN = "👑"
OK = "✅"
BAD = "❌"
LIVE = "🔊"


def fancy_title(title: str) -> str:
    from strings.themes import fancy

    return fancy(title)


def headline(title: str, note: bool = False) -> str:
    """Tricolour ribbon headline used at the top of every card."""
    lead = f"{NOTE} " if note else f"{SCORE} "
    return f"<blockquote>{RIBBON} {lead}<b>{fancy_title(title)}</b> {RIBBON}</blockquote>"


def card(title: str, body: str, footer: str = "", tags: str = "") -> str:
    """Standard Melody card: ribbon headline · optional tags · body · footer."""
    text = headline(title)
    if tags:
        text += f"\n{tags}"
    if body:
        text += f"\n{body}"
    if footer:
        text += f"\n<blockquote>{footer}</blockquote>"
    return text


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def tag(user_id: int, name: str) -> str:
    """Clickable mention."""
    return f'<a href="tg://user?id={int(user_id)}">{esc(name)}</a>'


def tag_list(users, limit: int = 12) -> str:
    """Bullet list of clickable mentions from Pyrogram `User` objects."""
    users = list(users)
    lines = []
    for user in users[:limit]:
        uid = getattr(user, "id", None)
        name = (
            getattr(user, "first_name", None)
            or getattr(user, "title", None)
            or "User"
        )
        lines.append(f"   {BULLET} {tag(uid, name) if uid else esc(name)}")
    extra = max(0, len(users) - limit)
    if extra:
        lines.append(f"   {BULLET} <i>+{extra} more</i>")
    return "\n".join(lines) or f"   {BULLET} ➖"


def rule() -> str:
    """Thin tricolour divider used inside the bigger cards."""
    return "🟧━━━━━⬜━━━━━🟩"


def accent(glyph: str, text: str) -> str:
    """One-line accent row: premium glyph + arrow + text."""
    return f"{glyph} {ARROW} {text}"


def stamp() -> str:
    """Local HH:MM:SS timestamp for card footers."""
    return time.strftime("%H:%M:%S", time.localtime())


def meter(count: int, cap: int = 10) -> str:
    """Tiny tricolour presence meter."""
    filled = max(0, min(cap, int(count or 0)))
    return "🟩" * filled + "⚪" * (cap - filled)
