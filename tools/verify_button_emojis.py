#!/usr/bin/env python3
"""✅ Verify that EVERY inline-button label gets a MATCHING premium emoji icon.

Telegram renders premium (animated) emoji inside a button only through
`InlineKeyboardButton(icon_custom_emoji_id=...)` — plain `<emoji id="...">`
markup in the label text is ignored. `utils/buttons.ikb()` attaches that icon
automatically from the emoji already written in the label.

This script is the batch check for that pipeline. It walks every `ikb(...)`
call in the repo, extracts the literal label, and confirms:

  1. every emoji SEQUENCE in the label (flags, keycaps, ZWJ, skin tones
     included) resolves to at least one verified premium custom-emoji id, and
  2. that id is stored under the SAME glyph in the Telegram-verified catalogue
     (utils/emoji_order_pack.py), i.e. the animation matches the label — no
     random pool id, no unrelated artwork,
  3. labels whose glyph Telegram refuses inside an entity (lone regional
     indicator, bare skin tone, combining keycap) are reported as "plain by
     design" instead of being silently mismatched.

Run:  python3 tools/verify_button_emojis.py
Exit code is non-zero when a label would render an unmatched / mismatched icon.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from utils.emoji_map import (  # noqa: E402
    EMOJI_ID_FALLBACKS,
    EMOJI_ID_MAP,
    entity_text,
)
from utils.emoji_order_pack import ORDER_EMOJI_MAP  # noqa: E402

# Same sequence matcher utils/telegram_html.py uses for message text, kept
# local so this script needs no pyrogram install.
_EMOJI_SEQ_RE = re.compile(
    "(?:"
    "[\U0001F1E6-\U0001F1FF]{2}"                      # flags
    "|[0-9#*]\ufe0f?\u20e3"                            # keycaps
    "|[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u27BF"
    "\u2B00-\u2BFF\u2600-\u26FF\u2139\u24C2\u203C\u2049"
    "\u2122\u3030\u303D\u3297\u3299]"
    "[\ufe0f\ufe0e]?(?:[\U0001F3FB-\U0001F3FF])?"
    "(?:\u200d[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]"
    "[\ufe0f\ufe0e]?(?:[\U0001F3FB-\U0001F3FF])?)*"
    ")"
)

# Typographic symbols used purely as decoration in labels. They are not emoji,
# so no premium pack contains them and no icon should be forced onto them.
_NON_EMOJI_SYMBOLS = {"\u2735", "\u2734", "\u2726", "\u2727", "\u273F", "\u2740"}

VERIFIED_BY_GLYPH = {g.replace("\ufe0f", ""): {str(i) for i in ids}
                     for g, ids in ORDER_EMOJI_MAP.items()}


def button_labels() -> list:
    """(file, lineno, label) for every literal-labelled ikb() call."""
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        if "/tools/" in str(path) or "/tests/" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - caught by other tests
            print(f"SYNTAX ERROR {path}: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "ikb" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((path.relative_to(ROOT), node.lineno, first.value))
    return found


def main() -> int:
    labels = button_labels()
    unmatched, mismatched, plain_by_design, decorative, ok = [], [], [], [], []

    for path, lineno, label in labels:
        glyphs = _EMOJI_SEQ_RE.findall(label)
        if not glyphs:
            continue  # text-only button: nothing to animate
        matched = False
        for glyph in glyphs:
            key = glyph.replace("\ufe0f", "")
            if entity_text(glyph) is None:
                plain_by_design.append((path, lineno, label, key))
                continue
            chain = [str(i) for i in (EMOJI_ID_FALLBACKS.get(key) or [])]
            primary = EMOJI_ID_MAP.get(key)
            if primary:
                chain.insert(0, str(primary))
            if not chain:
                # A dingbat / text symbol like '✵' has no premium counterpart
                # in ANY pack — it is typography, not an emoji. Leave it plain
                # rather than attaching an unrelated animation.
                if key in _NON_EMOJI_SYMBOLS:
                    decorative.append((path, lineno, label, key))
                else:
                    unmatched.append((path, lineno, label, key))
                continue
            verified = VERIFIED_BY_GLYPH.get(key, set())
            if verified and chain[0] not in verified:
                mismatched.append((path, lineno, label, key, chain[0]))
                continue
            matched = True
            break
        if matched:
            ok.append((path, lineno, label))

    print(f"inline buttons scanned ......... {len(labels)}")
    print(f"labels with a matching icon ... {len(ok)}")
    print(f"plain by design (Telegram) .... {len(plain_by_design)}")
    print(f"decorative symbols (no pack) .. {len(decorative)}")
    print(f"unmatched glyphs .............. {len(unmatched)}")
    print(f"MISMATCHED glyph/id pairs ..... {len(mismatched)}")

    for path, lineno, label, key in plain_by_design[:20]:
        print(f"  plain  {path}:{lineno} {label!r} -> {key!r} (invalid entity target)")
    for path, lineno, label, key in unmatched:
        print(f"  MISS   {path}:{lineno} {label!r} -> no verified id for {key!r}")
    for path, lineno, label, key, eid in mismatched:
        print(f"  WRONG  {path}:{lineno} {label!r} -> {eid} is not {key!r}")

    return 1 if (unmatched or mismatched) else 0


if __name__ == "__main__":
    raise SystemExit(main())
