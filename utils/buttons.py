"""🎛️ Safe inline-button factory (premium emoji icons + button styles).

WHY THIS FILE EXISTS
--------------------
Two separate crashes/bugs came from building `InlineKeyboardButton(...)` by
hand all over the codebase:

1. `NameError: name 'ButtonStyle' is not defined`
   (melody/plugins/misc/start.py -> user_buttons()). The name was used but
   never imported, and — more importantly — `ButtonStyle` does not exist in
   every Pyrogram fork, so a plain `from pyrogram.enums import ButtonStyle`
   would only move the crash to an `ImportError` on some installs.

2. `icon_custom_emoji_id=` (premium emoji inside a button) is only accepted
   by newer forks. Passing it to an older one raises
   `TypeError: __init__() got an unexpected keyword argument`.

So instead of guessing, this module *inspects* the installed
`InlineKeyboardButton` signature once at import time and silently drops any
keyword the running library cannot accept. Buttons therefore render with
premium emoji + colour styles where supported, and render as normal buttons
everywhere else — never as a crash.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Optional

from pyrogram.types import InlineKeyboardButton as _RawButton

# ── capability detection ──────────────────────────────────────────────────
try:
    _PARAMS = set(inspect.signature(_RawButton.__init__).parameters)
except (TypeError, ValueError):  # pragma: no cover - exotic builds
    _PARAMS = set()

SUPPORTS_ICON: bool = "icon_custom_emoji_id" in _PARAMS
SUPPORTS_STYLE: bool = "style" in _PARAMS


class _StyleFallback(str):
    """Stand-in for `ButtonStyle` on forks that do not ship it."""


def _load_styles():
    try:
        from pyrogram.enums import ButtonStyle as _BS  # type: ignore
        return _BS
    except Exception:
        class _BS:  # noqa: N801 - mimics the enum API
            DEFAULT = _StyleFallback("default")
            PRIMARY = _StyleFallback("primary")
            SUCCESS = _StyleFallback("success")
            DANGER = _StyleFallback("danger")
            WARNING = _StyleFallback("warning")
            SECONDARY = _StyleFallback("secondary")
        return _BS


ButtonStyle = _load_styles()

# Style aliases so call sites never touch the enum directly.
STYLE_DEFAULT = getattr(ButtonStyle, "DEFAULT", None)
STYLE_PRIMARY = getattr(ButtonStyle, "PRIMARY", STYLE_DEFAULT)
STYLE_SUCCESS = getattr(ButtonStyle, "SUCCESS", STYLE_DEFAULT)
STYLE_DANGER = getattr(ButtonStyle, "DANGER", STYLE_DEFAULT)
STYLE_WARNING = getattr(ButtonStyle, "WARNING", STYLE_DEFAULT)


# ── premium emoji lookup ──────────────────────────────────────────────────
def label_glyphs(label: str) -> list:
    """Every full emoji SEQUENCE in a button label, in order.

    BUG FIX ("perfect matching chahiye"): the old lookup walked the label one
    CODEPOINT at a time, so a flag (🇮🇳), keycap (1️⃣), ZWJ family or
    skin-toned emoji never matched the map — its first codepoint is a bare
    regional indicator / digit / base glyph. Those misses then fell through to
    a pseudo-random pool id, which is exactly how a button ended up animating
    an emoji that had nothing to do with its label. Matching the SAME sequence
    regex the text pipeline uses (utils.telegram_html) keeps buttons and text
    perfectly in sync.
    """
    if not label:
        return []
    try:
        from utils.telegram_html import _EMOJI_SEQ_RE

        return [m.group(0) for m in _EMOJI_SEQ_RE.finditer(label)]
    except Exception:
        return [ch for ch in label if not (ch.isspace() or ch.isalnum())]


def icon_ids_for(label: str) -> list:
    """All verified premium ids that depict the label's own emoji, best first.

    Every id comes from a glyph-keyed chain that was verified against Telegram
    (`getCustomEmojiStickers` / `getStickerSet`, see utils/emoji_order_pack.py
    and the startup resolution in utils.emoji_map.resolve_emoji_map), so the
    icon animation ALWAYS matches the glyph written in the label.

    The list is a fallback chain for the SAME glyph: when Telegram rejects /
    quarantines one document the caller can try the next one and still show a
    matching premium icon instead of dropping to plain unicode. An empty list
    means "no verified match" — the plain glyph must stay in the label rather
    than the button animating an unrelated emoji.
    """
    if not label:
        return []
    try:
        from utils.emoji_map import (
            EMOJI_ID_FALLBACKS,
            EMOJI_ID_MAP,
            _strip_vs,
            entity_text,
        )
    except Exception:
        return []

    try:
        from utils.emoji_map import _dead_ids

        dead = {str(i) for i in _dead_ids()}
    except Exception:
        dead = set()

    for glyph in label_glyphs(label):
        if entity_text(glyph) is None:
            # Telegram rejects this fragment as a custom-emoji entity.
            continue
        key = _strip_vs(glyph)
        candidates = [str(i) for i in (EMOJI_ID_FALLBACKS.get(key) or [])]
        primary = EMOJI_ID_MAP.get(key)
        if primary and str(primary) in candidates:
            candidates.remove(str(primary))
        if primary:
            candidates.insert(0, str(primary))
        # Telegram-resolved match for glyphs that are not in the static map.
        if not candidates:
            try:
                from utils import emoji_search

                looked_up = emoji_search.cached_id(key)
                if looked_up:
                    candidates = [str(looked_up)]
                else:
                    emoji_search.request(key)
            except Exception:
                pass
        alive = [c for c in candidates if c not in dead]
        if alive:
            return alive
    return []


def icon_id_for(label: str) -> Optional[str]:
    """Primary verified premium id for a button label (None when unmatched)."""
    ids = icon_ids_for(label)
    return ids[0] if ids else None




# ── plain-unicode glyph removal ───────────────────────────────────────────
#
# BUG FIX (reported: "inline buttons me jo normal emojis hai unko sahi se fix
# kr, must premium chahiye, all unicode replace kr premium emojis se").
#
# A button label is PLAIN TEXT — Telegram never renders <emoji id="..."> in it.
# The only real premium emoji a button can show is the one attached through
# `icon_custom_emoji_id=`. The old code left the raw unicode glyph inside the
# label as well, so a supported client showed the premium icon AND a plain
# unicode copy of the same emoji next to it (and on labels with a glyph in
# front *and* behind, three emoji per button). That is what looked broken.
#
# So: whenever a premium icon is actually attached, strip every plain glyph out
# of the label — the premium icon replaces the unicode. When no icon can be
# attached (old fork, quarantined id, or an icon-only button with no words),
# the plain glyph is kept so the button is never blank/unlabelled.
_GLYPH_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # pictographs, emoticons, symbols, extended
    "\u2190-\u21FF"           # arrows
    "\u2139\u24C2\u203C\u2049\u2122"  # info / Ⓜ / ‼ / ⁉ / ™
    "\u3030\u303D\u3297\u3299"  # CJK-block emoji
    "\u2300-\u27BF"           # misc technical / dingbats
    "\u2B00-\u2BFF"           # extra arrows & shapes
    "\u2600-\u26FF"           # misc symbols
    "\uFE0F\uFE0E\u200D\u20E3"  # variation selectors / ZWJ / keycap
    "]+"
)


def strip_glyphs(text: str) -> str:
    """Label text with plain unicode emoji/pictographs removed."""
    cleaned = _GLYPH_RE.sub(" ", text or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


_DANGER_WORDS = ("close", "stop", "cancel", "delete", "remove", "ban", "back", "off", "end", "✖", "❌", "⏹", "🗑")
_SUCCESS_WORDS = ("play", "resume", "start", "add", "on", "approve", "enable", "yes", "▶", "✅", "🟢")


def infer_style(text: str) -> Any:
    """Pick a sensible button colour from the label when none was given.

    REQUEST: "inline buttons me premium emojis + button colour style chaiye."
    Doing it here means every menu in the bot gets coloured buttons without
    hand-editing each plugin, and forks without `style=` still work.
    """
    low = (text or "").lower()
    if any(w in low for w in _DANGER_WORDS):
        return STYLE_DANGER
    if any(w in low for w in _SUCCESS_WORDS):
        return STYLE_SUCCESS
    return STYLE_PRIMARY


def ikb(
    text: str,
    *,
    style: Any = None,
    icon: Optional[str] = None,
    auto_icon: bool = True,
    **kwargs: Any,
) -> _RawButton:
    """Build an InlineKeyboardButton that works on every Pyrogram fork.

    `style`  -> applied when the installed fork supports `style=`; when the
                caller passes none, a colour is inferred from the label so no
                button is left plain/grey.
    `icon`   -> premium custom-emoji id; when omitted and `auto_icon` is on,
                it is looked up from the emoji already present in `text`.
    Any other keyword (callback_data, url, switch_inline_query, …) is passed
    straight through.
    """
    if SUPPORTS_STYLE:
        chosen = style if style is not None else infer_style(text)
        if chosen is not None:
            kwargs["style"] = chosen
    if SUPPORTS_ICON:
        chain = [str(icon)] if icon else (icon_ids_for(text) if auto_icon else [])
        # BUG FIX (log: repeated 400 ENTITY_TEXT_INVALID strikes): button icons
        # live in reply_markup, so utils/emoji_patch.py's text sanitiser never
        # saw them and a single bad/blocked id kept poisoning whole menus.
        # Honour the SAME quarantine + pause state here — and walk the glyph's
        # fallback chain, so one quarantined document no longer knocks the
        # button back to plain unicode when another verified variant exists.
        emoji_id = chain[0] if chain else None
        if chain:
            try:
                from utils.emoji_patch import (
                    QUARANTINED_IDS, custom_emoji_allowed, _expire_quarantine,
                )
                # Let stale quarantines lapse first, otherwise button icons
                # stay plain long after the transient rejection is over.
                _expire_quarantine()
                if not custom_emoji_allowed():
                    emoji_id = None
                else:
                    blocked = {str(i) for i in QUARANTINED_IDS}
                    emoji_id = next((c for c in chain if c not in blocked), None)
            except Exception:
                pass
        if emoji_id:
            kwargs["icon_custom_emoji_id"] = emoji_id
            # The premium icon now carries the emoji — drop the duplicate plain
            # unicode copy from the label (but never leave the label empty).
            cleaned = strip_glyphs(text)
            if cleaned:
                text = cleaned

    try:
        return _RawButton(text, **kwargs)
    except TypeError:
        # Last-resort guard: strip the optional extras and never crash a menu.
        kwargs.pop("style", None)
        kwargs.pop("icon_custom_emoji_id", None)
        return _RawButton(text, **kwargs)


__all__ = [
    "ButtonStyle",
    "SUPPORTS_ICON",
    "SUPPORTS_STYLE",
    "STYLE_DEFAULT",
    "STYLE_PRIMARY",
    "STYLE_SUCCESS",
    "STYLE_DANGER",
    "STYLE_WARNING",
    "icon_id_for",
    "icon_ids_for",
    "label_glyphs",

    "ikb",
    "strip_glyphs",
]
