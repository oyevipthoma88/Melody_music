"""
🛡️ Resilient sender for "premium animated emoji" + native Telegram "quote"
(<blockquote>) formatted messages.

BUG BEING FIXED
----------------
A previous change wrapped several cards in <blockquote> and sprinkled
<emoji id="..."> "premium animated emoji" entities through them, but never
verified the emoji ids were real Telegram custom-emoji documents:

- One id (``95282968352862527007``) is 20 digits — outside the signed
  64-bit range Telegram document ids live in — so it can never be valid.
- The rest are plausible-looking but were hand-typed placeholders, not ids
  pulled from a real custom emoji pack the bot can see.

Telegram rejects a message outright when one of its entities points at a
custom emoji id it doesn't recognise (``CUSTOM_EMOJI_INVALID`` /
``MSG_ENTITIES_INVALID``-style errors). Because the whole request fails,
neither the emoji *nor* the surrounding <blockquote> "quote" render — the
user just sees the old message (or nothing), which is exactly the two
symptoms reported ("premium animated emoji" not working *and* the
Telegram "quote" not working): both are collateral damage of one bad id.

FIX
---
Before sending, verify every <emoji id="..."> against Telegram
(``get_custom_emoji_stickers``) and drop only the ones that don't resolve,
falling back to their plain glyph. The <blockquote> "quote" formatting
always survives. If the send still fails for an unrelated reason, we
retry with formatting stripped entirely so the user always gets *a*
reply instead of silence.
"""

import logging
import re
from typing import Awaitable, Callable, Dict

from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError

from utils.emoji_map import EMOJI_ID_MAP

log = logging.getLogger(__name__)

_EMOJI_TAG_RE = re.compile(r"<emoji id=[\"']?(-?\d+)[\"']?>(.*?)</emoji>", re.DOTALL)
_INT64_MAX = 2**63 - 1

# Per-client cache of {custom_emoji_id: is_valid}, so we only ever hit
# Telegram once per id instead of on every message.
_verified_cache: Dict[int, Dict[int, bool]] = {}

# ── Auto premium-emoji wrapping (ALL glyphs, not just headline cards) ──────
#
# Splits `text` into segments that must stay untouched (existing
# <emoji id="...">...</emoji> entities, and <pre>/<code> spans — Telegram's
# HTML parser does not allow nested tags inside those two) versus segments
# where a plain literal glyph can be safely auto-wrapped. Only glyphs present
# in EMOJI_ID_MAP (built from the owner's real custom-emoji ids) are wrapped;
# anything else is left as-is. A glyph optionally followed by the U+FE0F
# "emoji presentation" variation selector is matched and wrapped as one unit
# so e.g. "▶️" round-trips correctly.
_PROTECTED_RE = re.compile(
    r"<emoji id=[\"']?-?\d+[\"']?>.*?</emoji>"
    r"|<pre(?:\s[^>]*)?>.*?</pre>"
    r"|<code(?:\s[^>]*)?>.*?</code>",
    re.DOTALL,
)
_GLYPH_RE = None


def refresh_glyph_pattern() -> None:
    """(Re)build the glyph matcher from the CURRENT contents of EMOJI_ID_MAP.

    Called at import time and again by `utils.emoji_map.resolve_emoji_map()`
    once Telegram has told us which glyph each of the owner's premium
    custom-emoji ids really depicts.
    """
    global _GLYPH_RE
    if not EMOJI_ID_MAP:
        _GLYPH_RE = None
        return
    glyphs = sorted(EMOJI_ID_MAP, key=len, reverse=True)
    _GLYPH_RE = re.compile("(" + "|".join(re.escape(g) for g in glyphs) + ")(\ufe0f?)")


refresh_glyph_pattern()


# ── STRICT emoji coverage (root fix: "kuch emojis abhi bhi normal me ja rahe hai") ──
#
# The old wrapper only matched glyphs that were literally present as KEYS in
# EMOJI_ID_MAP, one codepoint at a time. Everything else stayed plain unicode:
#   • multi-codepoint sequences (👨\u200d💻, 1️⃣, 🇮🇳, 🙋🏻)
#   • text-presentation symbols written without U+FE0F (🕊, ⛱, ✍, ❤)
#   • any brand new glyph nobody had added to the map yet
# Now EVERY emoji grapheme in the text is matched by a full emoji-sequence
# regex, given a stable premium id (auto-assigned from the owner's verified
# pool when the map has none), and emitted with U+FE0F when Telegram needs it
# for the entity to be accepted. Nothing is left plain unless Telegram cannot
# accept it as a custom-emoji entity at all.
_EMOJI_SEQ_RE = re.compile(
    "(?:"
    r"[#*0-9]\ufe0f?\u20e3"                       # keycaps  1️⃣
    "|[\U0001F1E6-\U0001F1FF]{2}"                 # flags    🇮🇳
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


def _premium_tag(sequence: str) -> str:
    """Return the <emoji> markup for one emoji grapheme (or it unchanged)."""
    from utils.emoji_map import assign_pool_id, entity_text

    emoji_id = assign_pool_id(sequence)
    if not emoji_id:
        return sequence
    display = entity_text(sequence) or sequence
    return f'<emoji id="{emoji_id}">{display}</emoji>'


def auto_premium_emoji(text: str) -> str:
    """Wrap EVERY plain emoji grapheme in `text` with a premium custom-emoji
    entity, skipping graphemes already inside an <emoji> tag or inside a
    <pre>/<code> span (where nested tags aren't allowed)."""
    if not text:
        return text

    report_unmapped(text)

    def _wrap_glyphs(segment: str) -> str:
        if not segment:
            return segment
        return _EMOJI_SEQ_RE.sub(lambda m: _premium_tag(m.group(0)), segment)

    parts = _PROTECTED_RE.split(text)
    protected = _PROTECTED_RE.findall(text)
    out = [_wrap_glyphs(parts[0])]
    for chunk, tail in zip(protected, parts[1:]):
        out.append(chunk)
        out.append(_wrap_glyphs(tail))
    return "".join(out)


# ── Strict coverage reporting ──────────────────────────────────────────────
#
# REQUESTED: "strictly ALL emojis must be premium". A glyph can only become a
# premium animated emoji if an id in the owner's pack depicts it, so instead of
# silently leaving unknown glyphs plain we now record them once and log them —
# the owner can add the missing ids to utils/emoji_ids.py and they are picked
# up on the next start.
_ANY_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U0001F000-\U0001F2FF"
    "\u2600-\u27BF" "\u2190-\u21FF" "\u2B00-\u2BFF" "\uFE0F" "]"
)
UNMAPPED_GLYPHS: set = set()


def report_unmapped(text: str) -> None:
    for glyph in _ANY_EMOJI_RE.findall(text or ""):
        if glyph == "\ufe0f" or glyph in EMOJI_ID_MAP:
            continue
        if glyph in UNMAPPED_GLYPHS:
            continue
        UNMAPPED_GLYPHS.add(glyph)
        # REQUESTED: every non-premium emoji must still render premium — give
        # the glyph a (stable) random id out of the owner's verified pool
        # instead of leaving it as plain unicode.
        try:
            from utils.emoji_map import assign_pool_id
            if assign_pool_id(glyph):
                continue
        except Exception:
            pass
        log.debug(
            "premium-emoji: no custom-emoji id for %r — add one to "
            "utils/emoji_ids.py to make it premium too.", glyph,
        )


def strip_out_of_range_emoji(text: str) -> str:
    """Drop <emoji> tags whose id can't possibly be a real Telegram document
    id (those are signed 64-bit integers) without needing any API call."""

    def _sub(match: "re.Match[str]") -> str:
        emoji_id, inner = match.group(1), match.group(2)
        if abs(int(emoji_id)) > _INT64_MAX:
            return inner
        return match.group(0)

    return _EMOJI_TAG_RE.sub(_sub, text)


def strip_all_emoji_tags(text: str) -> str:
    """Unwrap every <emoji id="...">X</emoji> down to the plain glyph X."""
    return _EMOJI_TAG_RE.sub(lambda m: m.group(2), text)


_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_UNESCAPE = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&"))


def strip_html_tags(text: str) -> str:
    """Last-resort: turn HTML into readable plain text.

    BUG FIX (raw markup leak): the final fallback sent the text with
    ParseMode.DISABLED but still full of <b>/<code>/<blockquote> tags, so the
    user saw literal markup. Now every tag is removed and HTML entities are
    decoded before the plain-text send.
    """
    out = _TAG_RE.sub("", text)
    for src, dst in _ENTITY_UNESCAPE:
        out = out.replace(src, dst)
    return out


def strip_blockquote(text: str) -> str:
    """Remove <blockquote>/<blockquote expandable> wrapping, last-resort
    fallback when even the sanitized HTML fails to send."""
    return (
        text.replace("<blockquote expandable>", "")
        .replace("<blockquote>", "")
        .replace("</blockquote>", "")
    )


async def verify_custom_emojis(client, text: str) -> str:
    """Check every <emoji id> referenced in `text` against Telegram and
    silently fall back to the plain glyph for any id that isn't a real,
    resolvable custom emoji — so one bad/fake id can never sink the whole
    message (and the <blockquote> quote around it)."""
    # Process-wide kill switch: if Telegram has already told us this bot may
    # not send custom-emoji entities, never inject them again (and unwrap any
    # the caller hand-wrote) — otherwise EVERY send costs a failed round-trip.
    from utils import emoji_patch  # local import: avoids a circular import

    if not emoji_patch.custom_emoji_allowed():
        return strip_all_emoji_tags(text)

    text = auto_premium_emoji(text)
    text = strip_out_of_range_emoji(text)

    if client is None:
        return text

    ids = {int(m.group(1)) for m in _EMOJI_TAG_RE.finditer(text)}
    if not ids:
        return text

    cache = _verified_cache.setdefault(id(client), {})
    unknown = [i for i in ids if i not in cache]
    if unknown:
        from utils.custom_emoji import (
            EmojiResolutionUnavailable,
            resolve_custom_emoji,
        )

        try:
            found_ids = set(await resolve_custom_emoji(client, unknown))
        except EmojiResolutionUnavailable as e:
            # FAIL OPEN. Telegram could not be asked (bot accounts may not call
            # messages.GetCustomEmojiDocuments at all), so we must NOT treat
            # these ids as invalid — doing that unwrapped every <emoji> tag and
            # made the whole bot send plain unicode emoji. Send them as-is; if
            # Telegram really rejects one, emoji_patch retries without it.
            log.debug("Skipping custom-emoji verification: %s", e)
            return text
        except Exception as e:  # defensive: never let verification break a send
            log.warning("Unexpected error verifying custom emoji ids %s: %s", unknown, e)
            return text
        for i in unknown:
            cache[i] = i in found_ids

    def _sub(match: "re.Match[str]") -> str:
        emoji_id, inner = int(match.group(1)), match.group(2)
        # Default True: an id we could not check stays premium.
        return match.group(0) if cache.get(emoji_id, True) else inner

    return _EMOJI_TAG_RE.sub(_sub, text)


async def safe_html_action(
    action: Callable[..., Awaitable],
    text: str,
    *,
    client=None,
    **kwargs,
) -> object:
    """Run `action(text, parse_mode=ParseMode.HTML, **kwargs)` after
    sanitizing premium-emoji entities, degrading gracefully instead of
    letting a bad entity kill the whole message:

    1. Verify <emoji id> tags against Telegram; drop the invalid ones
       (the <blockquote> "quote" formatting is preserved).
    2. If the send still fails, strip every <emoji> tag and retry.
    3. If it still fails, strip <blockquote> too and send as plain text.

    `action` should be a callable such as
    ``lambda t, **kw: message.reply_text(t, **kw)`` — pass through only the
    text and formatting-related kwargs (parse_mode is set here).
    """
    # BUG FIX: MessageNotModified is NOT a failure — it means Telegram already
    # shows exactly this card (user tapped the same help button twice). The old
    # code treated it as an entity failure, retried without emoji, failed again
    # and finally re-sent the card as PLAIN text (ParseMode.DISABLED) — that is
    # why help pages sometimes suddenly lost all their formatting/emoji.
    from pyrogram.errors import MessageNotModified

    prepared = await verify_custom_emojis(client, text)
    try:
        return await action(prepared, parse_mode=ParseMode.HTML, **kwargs)
    except MessageNotModified:
        return None
    except RPCError as e:
        log.warning("safe_html_action: send failed with emoji entities (%s), retrying without them", e)
        from utils import emoji_patch  # local import: avoids a circular import

        if emoji_patch._is_entity_error(e):
            emoji_patch._register_failure(e, prepared)

    # Strip from the ORIGINAL text, not from `prepared`: stripping `prepared`
    # would leave the auto-injected entities out but the retry would still be
    # re-wrapped by emoji_patch. Both layers are now gated by the kill switch.
    stripped = strip_all_emoji_tags(text)
    try:
        return await action(stripped, parse_mode=ParseMode.HTML, **kwargs)
    except MessageNotModified:
        return None
    except RPCError as e:
        log.error("safe_html_action: send failed without emoji entities too (%s), falling back to plain text", e)


    plain = strip_html_tags(strip_blockquote(strip_all_emoji_tags(stripped)))
    return await action(plain, parse_mode=ParseMode.DISABLED, **kwargs)
