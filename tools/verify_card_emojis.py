#!/usr/bin/env python3
"""🎯 Verify every emoji printed on a card has an ACCURATE premium id.

`auto_premium_emoji()` wraps each emoji in outgoing card text with a premium
custom-emoji entity. That is only "perfect" when the id really depicts that
glyph — a random id animates as a completely different emoji.

This script walks every `.py` file in the bot, extracts each emoji sequence
from string literals, and checks it resolves through:

  1. EMOJI_ID_MAP / EMOJI_ID_FALLBACKS (verified ids), or
  2. utils.emoji_order_pack.ORDER_EMOJI_MAP (the owner's harvested catalogue), or
  3. utils.emoji_map.GLYPH_ALIASES (glyph borrows the closest verified glyph).

Anything left over renders as PLAIN unicode by design (never a wrong
animation). Exit code is non-zero only when such glyphs exist, so the owner
can decide to map them.

Run:  python3 tools/verify_card_emojis.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from utils.emoji_map import (  # noqa: E402
    EMOJI_ID_FALLBACKS,
    EMOJI_ID_MAP,
    GLYPH_ALIASES,
    entity_text,
)
from utils.emoji_order_pack import ORDER_EMOJI_MAP  # noqa: E402

EMOJI_SEQ_RE = re.compile(
    "(?:"
    r"[#*0-9]\ufe0f?\u20e3"
    "|[\U0001F1E6-\U0001F1FF]{2}"
    "|(?:[\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u21aa"
    "\u231a-\u231b\u2328\u23cf\u23e9-\u23fa\u24c2\u25aa-\u25fe"
    "\u2600-\u27bf\u2934-\u2935\u2b00-\u2bff\u3030\u303d\u3297\u3299"
    "\U0001F000-\U0001FAFF]"
    "[\U0001F3FB-\U0001F3FF]?\ufe0f?"
    "(?:\u200d[\u2600-\u27bf\U0001F000-\U0001FAFF]"
    "[\U0001F3FB-\U0001F3FF]?\ufe0f?)*"
    ")"
    ")"
)

# Drawn onto the thumbnail image with PIL (not Telegram text) or used as a
# docstring example of the matcher itself — no entity is ever sent for these.
SKIP_FILES = {"utils/thumbnails.py", "utils/telegram_html.py",
              "utils/emoji_map.py", "utils/emoji_order_pack.py",
              "utils/emoji_ids.py", "tools/verify_card_emojis.py",
              "tools/verify_button_emojis.py", "tools/termux_emoji_harvest.py",
              "tools/verify_premium_emoji.py"}


# Typographic decoration, not emoji: no premium pack draws them and Telegram
# renders them as text, so they are correct exactly as written.
DECORATIVE = {"\u25b8", "\u2735", "\u2734", "\u2726", "\u2727", "\u273f",
              "\u2740", "\u279c", "\u266a", "\u25cf", "\u25ac", "\u25ad",
              "\u25ab", "\u1f130"}
SKIN_TONES = "\U0001F3FB\U0001F3FC\U0001F3FD\U0001F3FE\U0001F3FF"


def has_accurate_id(sequence: str) -> bool:
    glyph = sequence.replace("\ufe0f", "")
    if glyph in DECORATIVE or glyph == "\U0001F130":
        return True
    base = glyph.rstrip(SKIN_TONES)
    if base != glyph and (base in EMOJI_ID_MAP or base in GLYPH_ALIASES
                          or base in ORDER_EMOJI_MAP):
        return True
    if glyph in EMOJI_ID_MAP or glyph in EMOJI_ID_FALLBACKS:
        return True
    if glyph in ORDER_EMOJI_MAP or sequence in ORDER_EMOJI_MAP:
        return True
    return glyph in GLYPH_ALIASES


def main() -> int:
    plain: dict[str, set] = {}
    total = 0
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(".git") or rel in SKIP_FILES or rel.startswith("tests/"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for sequence in EMOJI_SEQ_RE.findall(text):
            total += 1
            if has_accurate_id(sequence):
                continue
            if entity_text(sequence) is None:
                continue  # Telegram would reject it as an entity anyway.
            plain.setdefault(sequence.replace("\ufe0f", ""), set()).add(rel)

    print(f"emoji occurrences scanned .......... {total}")
    print(f"glyph aliases in use ............... {len(GLYPH_ALIASES)}")
    print(f"glyphs WITHOUT an accurate id ...... {len(plain)}")
    for glyph, files in sorted(plain.items(), key=lambda kv: -len(kv[1])):
        print(f"  {glyph!r:12} in {len(files)} file(s): {', '.join(sorted(files)[:3])}")
    return 1 if plain else 0


if __name__ == "__main__":
    raise SystemExit(main())
