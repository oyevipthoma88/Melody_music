"""
🎨 Theme strings — Modi-Meloni color palette
"""

# ─────────────────────────────────────────────────────────────────────────────
#  Stylized "fancy font" text — Mathematical Bold Italic Unicode block
#  (matches the requested style, e.g. "𝒔𝒋𝒌𝒔𝒔𝒔𝒃𝒔𝒔𝒔𝒏𝒔")
# ─────────────────────────────────────────────────────────────────────────────
_FANCY_MAP: dict[str, str] = {}
for _i, _c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    _FANCY_MAP[_c] = chr(0x1D468 + _i)
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _FANCY_MAP[_c] = chr(0x1D482 + _i)
for _i, _c in enumerate("0123456789"):
    _FANCY_MAP[_c] = chr(0x1D7CE + _i)


def fancy(text: str) -> str:
    """Render text in bold-italic Unicode "fancy font" (𝑴𝒆𝒍𝒐𝒅𝒚 style).

    Falls back to the original character for anything outside A-Z/a-z/0-9
    (spaces, emoji, punctuation) so formatting/links keep working.
    """
    return "".join(_FANCY_MAP.get(ch, ch) for ch in text)


# ─────────────────────────────────────────────────────────────────────────────
#  Inline button color-coding.
#
#  IMPORTANT: the Telegram Bot API has no "color" field on InlineKeyboardButton
#  or ReplyKeyboardMarkup — a bot can NEVER paint a real background color on a
#  normal chat button (this used to be faked here with a 🔵/🔴/🟢 circle emoji
#  prefix, which is not the same thing and was dropped on request).
#
#  The only way to get real colored button backgrounds (like the reference
#  screenshot) is Telegram's Mini App / Web App surface, which is plain HTML
#  and can be styled however you want. See `strings/webmenu.py` +
#  `docs/menu.html` for the reusable "colored menu" system built for this —
#  `melody/plugins/misc/help.py` is the reference implementation.
#
#  Rule for any NEW button going forward:
#    - If it needs a real color, add it as a row in a `strings.webmenu`
#      spec (pick "blue" / "red" / "green") instead of inventing a new emoji
#      trick.
#    - Otherwise keep it a plain, undecorated label — no color emoji.
# ─────────────────────────────────────────────────────────────────────────────
BLUE = "blue"
RED = "red"
GREEN = "green"


def btn(label: str, color: str = None) -> str:
    """Plain button label — color emoji prefixes are intentionally not used.

    Kept as a passthrough (instead of deleting it) so existing call sites
    across the plugins don't need to change; `color` is accepted but ignored
    for normal Bot API buttons. Use `strings.webmenu` for real colors.
    """
    return label


COLORS = {
    "saffron": "#FF6600",
    "gold":    "#FFD700",
    "green":   "#009246",
    "red":     "#CE2B37",
    "white":   "#FFFFFF",
    "dark":    "#1A0500",
}

# Emoji shortcuts
E = {
    "play":    "▶️",
    "pause":   "⏸",
    "skip":    "⏭",
    "stop":    "⏹",
    "loop":    "🔁",
    "shuffle": "🔀",
    "vol_up":  "🔊",
    "vol_dn":  "🔉",
    "mute":    "🔇",
    "lyrics":  "🎵",
    "queue":   "📋",
    "search":  "🔍",
    "owner":   "♛",
    "group":   "🏛",
    "user":    "🙋",
    "bot":     "🎶",
    "error":   "❌",
    "ok":      "✅",
    "warn":    "⚠️",
    "info":    "ℹ️",
    "fire":    "🔥",
    "star":    "⭐",
    "crown":   "👑",
}

# Common messages
STRINGS = {
    "error_generic":    "Something went wrong 🌸",
    "not_in_vc":       "Voice chat band hai, pehle VC kholo 📢",
    "no_song":         "Song nahi mili 🌸",
    "queue_empty":     "Queue empty hai 🎵",
    "nothing_playing": "Kuch nahi chal raha abhi ❌",
    "too_long":        "Song bahut lamba hai! Max {max} allowed ⏱",
    "banned":          "Aap banned hain is bot se 🚫",
    "admin_only":      "Sirf admins ya authorized users use kar sakte hain ⚠️",
}
