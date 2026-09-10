"""Guards for the unified Melody card theme + perfect (accurate) premium ids."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from utils.emoji_map import EMOJI_ID_MAP, GLYPH_ALIASES, assign_pool_id  # noqa: E402
from utils.melody_theme import RIBBON, card, meter  # noqa: E402


def test_every_card_shares_the_tricolour_frame():
    text = card("Test", "body")
    assert text.startswith(f"<blockquote>{RIBBON}")
    assert "body" in text
    # admin_tools.card() must delegate here instead of keeping its own header
    # (importing it needs pyrogram, so assert on the source).
    source = (ROOT / "utils" / "admin_tools.py").read_text(encoding="utf-8")
    assert "from utils.melody_theme import card as themed_card" in source
    assert "themed_card(title, body, footer=footer)" in source


def test_card_footer_and_tags_are_quoted():
    text = card("T", "b", footer="foot", tags="#Tag")
    assert "#Tag" in text and "<blockquote>foot</blockquote>" in text


def test_alias_sources_all_exist_in_the_verified_map():
    for alias, source in GLYPH_ALIASES.items():
        assert source in EMOJI_ID_MAP, f"{alias} borrows unmapped {source}"


def test_alias_glyph_gets_its_source_id_not_a_random_one():
    assert EMOJI_ID_MAP["🧾"] == EMOJI_ID_MAP["📄"]
    assert EMOJI_ID_MAP["⏯"] == EMOJI_ID_MAP["▶"]


def test_unknown_glyph_stays_plain_instead_of_animating_wrong_emoji():
    # ROOT FIX: a glyph no pack depicts used to be handed a RANDOM pool id.
    assert assign_pool_id("\U0001FAE9") is None  # 🫩 (not in any pack)


def test_card_emoji_audit_passes():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "verify_card_emojis.py")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout


def test_meter_uses_mapped_glyphs_only():
    for glyph in set(meter(3)):
        assert glyph in EMOJI_ID_MAP
