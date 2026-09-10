#!/usr/bin/env python3
"""🔎 End-to-end premium-emoji audit (runtime pipeline, not just the map).

`tools/verify_card_emojis.py` proves every glyph in the source has an
ACCURATE id. That is not the same question as "does the user actually see an
animated emoji?", because a card only renders premium when the whole runtime
pipeline keeps the tag:

    auto_premium_emoji()  ->  entity_text()  ->  cap_emoji_entities()

This script replays exactly that pipeline over every string literal the bot
can send, plus the shared Melody theme frame, and reports:

  * glyphs that come out the other end as PLAIN unicode (the reported bug),
  * ids that can never be valid (not 64-bit, not numeric, quarantine-shaped),
  * cards that would lose emoji to Telegram's 100-entity limit.

Run:  python3 tools/verify_premium_emoji.py
Exit code is non-zero when a real card would render plain unicode.
"""
from __future__ import annotations

import ast
import collections
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from utils.emoji_map import EMOJI_ID_MAP  # noqa: E402
from utils.emoji_patch import cap_emoji_entities, emoji_entity_budget  # noqa: E402
from utils.telegram_html import (  # noqa: E402
    _EMOJI_SEQ_RE,
    auto_premium_emoji,
)

INT64_MAX = 2**63 - 1
TAG_RE = re.compile(r"<emoji id=[\"']?(-?\d+)[\"']?>(.*?)</emoji>", re.DOTALL)

# Files whose emoji never travel through Telegram text entities.
SKIP = {
    "utils/thumbnails.py",       # drawn onto the image with PIL
    "utils/telegram_html.py",    # the matcher's own docstrings
    "utils/emoji_map.py",
    "utils/emoji_order_pack.py",
    "utils/emoji_ids.py",
}

# Telegram strips ALL entities from inline-button labels — a premium emoji is
# impossible there by design, so button-only decorations are not failures.
BUTTON_ONLY = {"✵", "✧", "✦", "❯", "❮"}


def is_button_label(source_line: str) -> bool:
    return "ikb(" in source_line or "InlineKeyboardButton" in source_line


def literals():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in SKIP or rel.startswith(("tools/", "tests/")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        # Character-class literals inside re.compile("[\U0001F000-...]") are
        # regex source, never card text — they must not count as "plain emoji".
        regex_literals = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == "compile":
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                            regex_literals.add(id(inner))
        for node in ast.walk(tree):
            if id(node) in regex_literals:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _EMOJI_SEQ_RE.search(node.value):
                    line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                    yield rel, node.lineno, line, node.value


def theme_samples():
    from utils import melody_theme as theme

    yield "utils/melody_theme.py", 0, "", theme.headline("Now Playing", note=True)
    yield "utils/melody_theme.py", 0, "", theme.card("Melody", theme.FLAGS, theme.rule())
    yield "utils/melody_theme.py", 0, "", theme.rule()
    yield "utils/melody_theme.py", 0, "", theme.meter(4)
    yield "utils/melody_theme.py", 0, "", theme.accent(theme.SPARK, "premium")


def main() -> int:
    plain = collections.Counter()
    plain_where: dict[str, str] = {}
    bad_ids = collections.Counter()
    capped = []
    scanned = wrapped = 0

    for rel, lineno, line, text in list(literals()) + list(theme_samples()):
        scanned += len(_EMOJI_SEQ_RE.findall(text))
        rendered = auto_premium_emoji(text)
        budget = emoji_entity_budget(rendered)
        before = len(TAG_RE.findall(rendered))
        rendered = cap_emoji_entities(rendered)
        after = len(TAG_RE.findall(rendered))
        wrapped += after
        if before > after:
            capped.append((rel, lineno, before, after, budget))

        for emoji_id, glyph in TAG_RE.findall(rendered):
            if not emoji_id.lstrip("-").isdigit() or abs(int(emoji_id)) > INT64_MAX:
                bad_ids[f"{emoji_id} ({glyph})"] += 1

        leftover = TAG_RE.sub("", rendered)
        for glyph in _EMOJI_SEQ_RE.findall(leftover):
            if glyph in BUTTON_ONLY or is_button_label(line):
                continue
            plain[glyph] += 1
            plain_where.setdefault(glyph, f"{rel}:{lineno}")

    print(f"emoji occurrences scanned .......... {scanned}")
    print(f"rendered as PREMIUM entities ....... {wrapped}")
    print(f"mapped glyphs in EMOJI_ID_MAP ...... {len(EMOJI_ID_MAP)}")
    print(f"cards clipped by entity budget ..... {len(capped)}")
    print(f"impossible / malformed ids ......... {len(bad_ids)}")
    print(f"glyphs still rendering PLAIN ....... {len(plain)}")

    for rel, lineno, before, after, budget in capped[:10]:
        print(f"  clipped {rel}:{lineno} {before} -> {after} (budget {budget})")
    for label, count in bad_ids.most_common(10):
        print(f"  BAD ID {label} x{count}")
    for glyph, count in plain.most_common(30):
        print(f"  PLAIN {glyph!r} U+{ord(glyph[0]):04X} x{count} @ {plain_where[glyph]}")

    return 1 if (plain or bad_ids) else 0


if __name__ == "__main__":
    raise SystemExit(main())
