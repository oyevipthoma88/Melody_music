"""
🎛 Central inline-keyboard factory for Melody's play cards and utility panels.

Two things the owner asked for, in one place:

1. **Premium emoji on every inline button** — a button *label* is plain text
   (Telegram never renders `<emoji id="...">` entities inside a button), so
   "premium emoji" here means the exact glyph set of the bot's premium pack,
   placed BOTH in front of and behind the label — including the close button.
   The glyphs come from `utils.emoji_map.EMOJI_ID_MAP`, i.e. the same pack the
   rest of the bot renders as animated premium emoji in message text, so the
   card and its buttons always look like one set.

2. **Real button colours** — Telegram's newer layer exposes
   `enums.ButtonStyle` (`PRIMARY` / `DANGER` / `SUCCESS` / `DEFAULT`) on
   `InlineKeyboardButton`, which is supported by newer Telegram client/library builds. Older Pyrogram
   builds have no `style=` kwarg at all, so `sbtn()` probes once and silently
   drops the colour instead of crashing the whole keyboard with
   `TypeError: unexpected keyword argument 'style'`.
"""

from __future__ import annotations


from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# ─────────────────────────────────────────────────────────────────────────────
#  Buttons are built through utils.buttons.ikb, which already handles
#  `style=` (colour) and `icon_custom_emoji_id=` (real animated premium emoji
#  on the button itself) with per-fork capability probing plus the
#  emoji_patch quarantine that stopped the ENTITY_TEXT_INVALID strikes.
# ─────────────────────────────────────────────────────────────────────────────
from utils.buttons import (  # noqa: E402
    STYLE_DANGER,
    STYLE_DEFAULT,
    STYLE_PRIMARY,
    STYLE_SUCCESS,
    ikb as sbtn,
)


class Style:
    """Colour names used across the bot (safe no-ops on old Pyrogram)."""

    DEFAULT = STYLE_DEFAULT
    PRIMARY = STYLE_PRIMARY
    DANGER = STYLE_DANGER
    SUCCESS = STYLE_SUCCESS


# ─────────────────────────────────────────────────────────────────────────────
#  Premium-emoji glyphs (front + back decoration)
# ─────────────────────────────────────────────────────────────────────────────
def _glyph(primary: str, fallback: str = "") -> str:
    """Only use a glyph that belongs to the bot's real premium pack."""
    try:
        from utils.emoji_map import EMOJI_ID_MAP
    except Exception:
        return primary
    if primary in EMOJI_ID_MAP or primary.rstrip("\ufe0f") in EMOJI_ID_MAP:
        return primary
    return fallback or primary


# ── Owner-supplied premium ids for specific play-card buttons ─────────────
# These are real ids from the owner's premium packs and are attached directly
# with `icon_custom_emoji_id=` (a button label is plain text, so this is the
# ONLY way a button can show an animated premium emoji).
QUEUE_ICON_ID = "6154222749691679144"  # 📋, Telegram-verified
AUTOPLAY_ON_ICON_ID = "5226702984204797593"  # 🔄, Telegram-verified
AUTOPLAY_OFF_ICON_ID = "5098211821999883164"  # 🚫, Telegram-verified
# Brand button that replaced "Add Bot" on the playing card.
BRAND_ICON_ID = "4916169087698076775"  # 🎵, Telegram-verified
BRAND_LABEL = "- 𝑴𝒆𝒍𝑜𝒅𝒊𝒙 𝑴𝒖𝒔𝒊𝒄 .ᐟ.ᐟ"


PE = {
    "play": "▶", "pause": "⏸", "replay": "🔄", "skip": "⏭",
    "stop": "⏹", "close": "❌", "queue": "📋", "auto": "🔁",
    "bot": "🤖", "music": "🎵", "note": "🎶", "live": "🔴",
    "fast": "⚡", "search": "🔍", "info": "ℹ", "panel": "🔧",
}


def deco(key: str, label: str = "", back_key: str | None = None) -> str:
    """`<glyph> label` — exactly ONE emoji per button.

    BUG FIX ("inline buttons ke normal emojis fix kro, sab premium hone
    chahiye"): this used to put a glyph in FRONT and BEHIND every label. On
    clients that render the premium `icon_custom_emoji_id` (attached by
    utils.buttons.ikb from the glyph found here) that produced up to three
    emoji on one button — the premium icon plus two plain unicode copies.

    Now the glyph is emitted once, purely as the *lookup key* for the premium
    icon: `ikb()` attaches the matching premium custom emoji and removes the
    plain unicode from the label, so supported clients show a real premium
    emoji and older ones fall back to the single plain glyph.
    """
    front = _glyph(PE.get(key, "")) or _glyph(PE.get(back_key or key, ""))
    if not label:
        # icon-only transport button: the glyph IS the label
        return front
    return f"{front} {label}".strip()


CLOSE_LABEL = deco("close", "Close")


# ─────────────────────────────────────────────────────────────────────────────
#  Keyboards
# ─────────────────────────────────────────────────────────────────────────────
class Inline:
    """Melody's centralized inline keyboard collection."""

    ikm = InlineKeyboardMarkup

    # -- player -------------------------------------------------------------
    def controls(
        self,
        chat_id: int,
        paused: bool = False,
        status: str | None = None,
        remove: bool = False,
    ) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if status:
            rows.append([
                sbtn(
                    deco("live", status, "live"),
                    style=Style.DANGER if paused else Style.PRIMARY,
                    callback_data=f"controls status {chat_id}",
                )
            ])
        if not remove:
            rows.append([
                sbtn(
                    deco("play" if paused else "pause"),
                    style=Style.SUCCESS if paused else Style.PRIMARY,
                    callback_data=f"controls {'resume' if paused else 'pause'} {chat_id}",
                ),
                sbtn(deco("replay"), style=Style.PRIMARY,
                     callback_data=f"controls replay {chat_id}"),
                sbtn(deco("skip"), style=Style.PRIMARY,
                     callback_data=f"controls skip {chat_id}"),
                sbtn(deco("stop"), style=Style.DANGER,
                     callback_data=f"controls stop {chat_id}"),
            ])
        return self.ikm(rows)

    def play_card(
        self,
        chat_id: int,
        chat_title: str,
        autoplay_on: bool = False,
        paused: bool = False,
        bot_username: str | None = None,
        bot_name: str = "Melody",
    ) -> InlineKeyboardMarkup:
        """Four compact rows aligned beneath the play-card thumbnail."""
        rows: list[list[InlineKeyboardButton]] = list(
            self.controls(chat_id, paused=paused).inline_keyboard
        )

        # Keep labels intentionally short: Telegram decides button widths from
        # their text, so compact two-column rows line up most closely with the
        # thumbnail and make the complete card read as one rectangle.
        rows.append([
            sbtn("Queue", style=Style.PRIMARY, icon=QUEUE_ICON_ID,
                 callback_data="queue"),
            sbtn(
                "Auto On" if autoplay_on else "Auto Off",
                style=Style.SUCCESS if autoplay_on else Style.DEFAULT,
                icon=AUTOPLAY_ON_ICON_ID if autoplay_on else AUTOPLAY_OFF_ICON_ID,
                callback_data="autoplay_toggle",
            ),
        ])

        utility_row: list[InlineKeyboardButton] = []

        # REQUESTED: the play-card "Add Bot" button now carries the brand
        # label with a premium animated icon (it still adds the bot to a group).
        utility_row.append(
            sbtn(BRAND_LABEL, style=Style.SUCCESS, icon=BRAND_ICON_ID,
                 url=f"https://t.me/{bot_username}?startgroup=true")
            if bot_username else
            sbtn(BRAND_LABEL, style=Style.PRIMARY, icon=BRAND_ICON_ID,
                 callback_data="noop")
        )
        rows.append(utility_row)
        rows.append([self.close_button()])
        return self.ikm(rows)

    # -- small helpers ------------------------------------------------------
    def close_button(self) -> InlineKeyboardButton:
        return sbtn(CLOSE_LABEL, style=Style.DANGER, callback_data="close")

    def close_markup(self) -> InlineKeyboardMarkup:
        return self.ikm([[self.close_button()]])

    def queued(self, chat_id: int, item_id: str | int, label: str = "Play Now") -> InlineKeyboardMarkup:
        return self.ikm([
            [sbtn(deco("fast", label, "play"), style=Style.PRIMARY,
                  callback_data=f"controls force {chat_id} {item_id}")],
            [self.close_button()],
        ])

    def yt_key(self, link: str) -> InlineKeyboardMarkup:
        return self.ikm([[
            sbtn(deco("search", "YouTube", "music"), style=Style.PRIMARY, url=link),
            self.close_button(),
        ]])


inline = Inline()
