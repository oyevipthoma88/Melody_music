"""Premium emoji must survive the FULL send pipeline, not just the map.

Guards the reported bug: "premium emoji / unicode abhi bhi normal aate hai".
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from utils import melody_theme as theme  # noqa: E402
from utils.emoji_ids import PREMIUM_EMOJI_ID_POOL  # noqa: E402
from utils.emoji_map import EMOJI_ID_MAP  # noqa: E402
from utils.emoji_order_pack import ORDER_EMOJI_MAP  # noqa: E402
from utils.emoji_patch import cap_emoji_entities, emoji_entity_budget  # noqa: E402
from utils.telegram_html import _EMOJI_SEQ_RE, auto_premium_emoji  # noqa: E402

TAG_RE = re.compile(r"<emoji id=[\"']?(-?\d+)[\"']?>(.*?)</emoji>", re.DOTALL)


def _plain_left(text: str) -> list:
    rendered = cap_emoji_entities(auto_premium_emoji(text))
    return _EMOJI_SEQ_RE.findall(TAG_RE.sub("", rendered))


def test_theme_frame_is_fully_premium():
    for sample in (
        theme.headline("Now Playing", note=True),
        theme.card("Melody", theme.FLAGS, theme.rule()),
        theme.rule(),
        theme.meter(4),
        theme.accent(theme.SPARK, "premium"),
        theme.BULLET + theme.SUB_BULLET + theme.ARROW + theme.NOTE + theme.SCORE,
    ):
        assert _plain_left(sample) == [], sample


def test_no_plain_dingbat_bullets_left_in_cards():
    # "▸" / "➜" can never be a custom-emoji entity — they must not come back.
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts or "tools" in path.parts:
            continue
        assert "▸" not in path.read_text(encoding="utf-8"), path
        assert "➜" not in path.read_text(encoding="utf-8"), path


def test_entity_budget_uses_telegram_limit():
    # A plain card gets far more than the old flat cap of 48.
    assert emoji_entity_budget("hello") >= 80
    # Heavy HTML formatting shrinks the budget instead of blowing the limit.
    assert emoji_entity_budget("<b>x</b>" * 60) >= 8
    assert emoji_entity_budget("<b>x</b>" * 60) < 60


def test_cap_keeps_the_first_ids_and_unwraps_the_rest():
    text = "".join(f'<emoji id="1234567890{i % 10}">🎶</emoji>' for i in range(200))
    out = cap_emoji_entities(text)
    assert 8 <= len(TAG_RE.findall(out)) <= 90


def test_every_mapped_id_comes_from_a_verified_pack():
    known = {str(i) for i in PREMIUM_EMOJI_ID_POOL}
    for value in ORDER_EMOJI_MAP.values():
        if isinstance(value, (list, tuple)):
            known.update(str(v) for v in value)
        else:
            known.add(str(value))
    unverified = {g: i for g, i in EMOJI_ID_MAP.items() if str(i) not in known}
    assert not unverified, unverified


def test_media_caption_paths_are_patched():
    from pyrogram import Client

    from utils.emoji_patch import apply_emoji_patch

    apply_emoji_patch()
    for name in ("send_message", "send_photo", "edit_message_caption",
                 "send_media_group", "edit_message_media"):
        assert getattr(getattr(Client, name), "_emoji_patched", False), name


def test_full_audit_script_passes():
    result = subprocess.run(
        [sys.executable, "tools/verify_premium_emoji.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
