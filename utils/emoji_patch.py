"""
🌍 Global "premium emoji everywhere" patch.

WHY THIS EXISTS
----------------
`utils/formatters.premium_emoji()` + `PREMIUM_EMOJI_IDS` only ever covered
10 hand-picked headline strings (start/about/ping/stats/pause/resume/skip/
stop/nowplaying/queue). Every other plugin (~40 files, ~500+ raw glyphs —
help menus, admin commands, error messages, panel, ytdl errors, etc.) still
sent plain-text emoji, so premium emoji only ever "worked" in a few places.

Hand-editing every call site to wrap its glyphs is high-effort and high
regression-risk for a live bot we can't run end-to-end here. Instead we
intercept at the lowest common layer: EVERY outgoing text/caption in
Pyrogram ultimately flows through `Client.send_message`,
`Client.edit_message_text`, or `Client.send_photo` — `Message.reply`,
`Message.reply_text`, `Message.edit`, `Message.edit_text`, and
`Message.reply_photo` are all thin wrappers around these three. Patching
just these three functions therefore covers every send path in the bot
(present AND future — new plugins get this for free).

WHAT THE PATCH DOES
--------------------
For the text/caption argument only, it runs the existing
`verify_custom_emojis()` pipeline (which itself calls `auto_premium_emoji()`
first — see utils/telegram_html.py) — i.e. exactly the same fail-safe
wrap-then-verify-against-Telegram-then-fall-back-to-plain-glyph flow that
`send_quote()` already used for the original 10 headline strings, just
applied globally now. Nothing else about the call (parse_mode, markup,
media, etc.) is touched, so existing behavior for non-text arguments and
already-verified `<emoji>` tags is unchanged. Idempotent: text that has
already gone through `send_quote()`/`premium_emoji()` will not be
double-wrapped (auto_premium_emoji skips glyphs already inside an
<emoji>...</emoji> span).

Call `apply_emoji_patch()` exactly once, before the bot starts polling
(done in `melody/__init__.py`, right after `create_clients()`).
"""

import functools
import inspect
import logging
import re
import time

from pyrogram import Client

from utils.telegram_html import strip_all_emoji_tags, verify_custom_emojis

log = logging.getLogger(__name__)

_PATCHED_METHODS = (
    ("send_message", "text"),
    ("edit_message_text", "text"),
    # BUG FIX (reported: "Error in help_cb: [400 ENTITY_TEXT_INVALID]"):
    # photo-backed cards are edited through edit_message_caption, which was
    # NOT patched — so hand-written <emoji id="..."> tags in help/start cards
    # were never verified and the 400 was never retried without entities.
    ("edit_message_caption", "caption"),
    ("send_photo", "caption"),
    ("send_audio", "caption"),
    ("send_video", "caption"),
    ("send_animation", "caption"),
    ("send_document", "caption"),
    ("send_voice", "caption"),
)


_applied = False


# ── Runtime capability gate ───────────────────────────────────────────────
#
# BUG (reported): "/start all broken" + "help_cb -> [400 ENTITY_TEXT_INVALID]".
#
# Telegram only lets a BOT send `custom_emoji` entities when that bot's owner
# has bought an extra username on Fragment (premium-emoji rights). Every other
# bot gets `400 ENTITY_TEXT_INVALID` for the WHOLE message the moment a single
# <emoji id> entity is present — even though `messages.GetCustomEmojiDocuments`
# happily resolves the ids (so the old `verify_custom_emojis()` check always
# said "valid" and never protected us).
#
# Worse: `safe_html_action()`'s degrade path stripped the tags and re-sent, but
# the re-send went through THIS patch again, which re-wrapped the glyphs -> the
# same 400. Its last resort then sent with ParseMode.DISABLED while the text
# was still full of freshly injected `<emoji id="...">` markup, which is
# exactly the raw-markup /start message the owner pasted.
#
# Fix: (1) never wrap when the caller asked for no parse mode, (2) retry the
# send with the entities stripped when Telegram rejects them, and (3) latch a
# process-wide kill switch the first time that happens, so from then on the
# whole bot silently uses plain glyphs instead of paying a failed round-trip
# (and a broken card) on every single message.
# BUG FIX (reported: "premium emojis not working properly — permanent fix, ALL
# emojis must be premium"):
# the previous code latched a PROCESS-WIDE kill switch the very first time
# Telegram returned any entity-ish error. One single bad/rotated id (or one
# transient 400 on one message) therefore turned premium emoji off for the
# WHOLE bot until the next restart — which is exactly the "sometimes premium,
# mostly plain" behaviour that was reported.
#
# New behaviour:
#   * a rejected send NEVER disables premium emoji globally; the offending
#     message alone is retried with plain glyphs,
#   * the ids that were actually in the rejected message are quarantined so
#     the same broken id can't poison later messages,
#   * only a *sustained* run of rejections (>= _MAX_STRIKES in a row, i.e. the
#     bot genuinely has no premium-emoji rights) pauses wrapping, and even then
#     only for _COOLDOWN seconds, after which it automatically tries again.
QUARANTINED_IDS: set = set()
_quarantined_at: dict = {}
_QUARANTINE_TTL = 1800.0  # 30 min

# Telegram accepts at most 100 message entities; a long card auto-wrapped
# glyph-by-glyph can blow past that, and Telegram then rejects the WHOLE
# message (which read as "emoji stopped working half way through"). Cap the
# number of custom-emoji entities we inject per message well below the limit.
# ROOT-CAUSE FIX ("lambe card me aadhe emoji normal unicode aate hai"):
# the old flat cap of 48 unwrapped EVERY emoji after the 48th on big cards
# (help / panel / start), which is exactly the "premium emoji beech me se
# normal ho jaate hai" report. Telegram's real limit is 100 entities PER
# MESSAGE — counting <b>, <i>, <a>, <code>, <blockquote> too — so instead of
# a pessimistic constant we now spend the whole remaining entity budget on
# premium emoji.
TELEGRAM_ENTITY_LIMIT = 100
ENTITY_SAFETY_MARGIN = 4
MAX_EMOJI_ENTITIES = 90

_OTHER_TAG_RE = re.compile(
    r"<(b|strong|i|em|u|ins|s|strike|del|code|pre|a|blockquote|spoiler|tg-spoiler)"
    r"(?:\s[^>]*)?>",
    re.IGNORECASE,
)


def emoji_entity_budget(text: str) -> int:
    """How many <emoji> entities this specific message can still afford."""
    others = len(_OTHER_TAG_RE.findall(text or ""))
    return max(8, min(MAX_EMOJI_ENTITIES,
                      TELEGRAM_ENTITY_LIMIT - ENTITY_SAFETY_MARGIN - others))


def _quarantine(emoji_id: str) -> None:
    QUARANTINED_IDS.add(str(emoji_id))
    _quarantined_at[str(emoji_id)] = time.time()


def _expire_quarantine() -> None:
    now = time.time()
    for emoji_id in [i for i, ts in _quarantined_at.items() if now - ts >= _QUARANTINE_TTL]:
        QUARANTINED_IDS.discard(emoji_id)
        _quarantined_at.pop(emoji_id, None)


def cap_emoji_entities(text: str, limit: "int | None" = None) -> str:
    """Keep at most `limit` <emoji> entities in `text`; unwrap the rest to
    their plain glyph so Telegram never rejects the message for having too
    many entities."""
    if not text or "<emoji" not in text:
        return text
    if limit is None:
        limit = emoji_entity_budget(text)
    seen = 0

    def _sub(match):
        nonlocal seen
        seen += 1
        return match.group(0) if seen <= limit else match.group(2)

    return re.compile(
        r"<emoji id=[\"']?(-?\d+)[\"']?>(.*?)</emoji>", re.DOTALL
    ).sub(_sub, text)


_ENTITY_ERRORS = (
    "ENTITY_TEXT_INVALID",
    "CUSTOM_EMOJI_INVALID",
    "MSG_ENTITIES_INVALID",
    "ENTITY_BOUNDS_INVALID",
    "EMOJI_INVALID",
    "EMOJI_NOT_MODIFIED",
    "DOCUMENT_INVALID",
    "PREMIUM_ACCOUNT_REQUIRED",
)

_ID_RE = re.compile(r"<emoji id=[\"']?(-?\d+)[\"']?>")


def custom_emoji_allowed() -> bool:
    """Always True — premium emoji are never globally disabled.

    ROOT-CAUSE FIX ("bich me hi premium emojis kaam karna band kar dete hai"):
    the old strike/cooldown system disabled ALL premium emoji for 15 minutes
    after 8 rejections. Transient 400s (one bad id, one too-long message)
    were enough to trigger it, and once paused EVERY message fell back to
    plain glyphs until restart. Per-message retry (drop_quarantined) already
    handles bad ids gracefully, so a global kill switch only causes harm.
    """
    return True


def _is_entity_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".upper()
    return any(code in text for code in _ENTITY_ERRORS)


def _register_failure(exc: BaseException, text: str) -> None:
    """Quarantine ONLY the single offending id when unambiguous; never disable
    premium emoji globally.

    ROOT-CAUSE FIX ("premium emojis bich me hi kaam karna band kar dete hai"):
    the old code quarantined every id in a rejected message that was not in
    RESOLVED_IDS — but auto-assigned ids (from emoji_search background
    resolution or assign_pool_id) are NOT in RESOLVED_IDS, so valid working
    ids were wrongly quarantined. One rejection then cascaded: the retry
    unwrapped those 'bad' ids, the next message used different ids, and
    after 8 rejections the strike system disabled ALL premium emoji for
    15 minutes. Now we only quarantine when there is exactly one id in the
    message (unambiguous), and we NEVER disable premium emoji globally.
    """
    err = str(exc).upper()
    tagged = re.findall(r"<emoji id=[\"']?(-?\d+)[\"']?>(.*?)</emoji>", text or "", re.DOTALL)

    # A too-many/too-long-entities rejection says nothing about the ids
    # themselves — quarantining them here is what silently stripped premium
    # emoji from every later message. Just shrink the message instead.
    if "TOO_MUCH" in err or "TOO_LONG" in err or "MESSAGE_TOO_LONG" in err:
        log.debug("emoji_patch: entity-count rejection (%s) — ids kept", exc)
        return

    # Only quarantine when there is exactly one id — then we know it is the
    # culprit. With multiple ids we cannot tell which one failed, so
    # quarantining any of them risks killing valid ids (the exact bug that
    # made premium emoji stop working midway through a card).
    if len(tagged) == 1:
        _quarantine(tagged[0][0])
    else:
        log.debug(
            "emoji_patch: rejection with %d ids — not quarantining any to "
            "avoid killing valid premium emoji (%s)", len(tagged), exc,
        )



def _register_success() -> None:
    """No-op — premium emoji are never globally disabled now."""
    pass


def drop_quarantined(text: str) -> str:
    """Unwrap the ids Telegram cannot render, keeping every other premium
    emoji in the message intact (previously the whole message was degraded to
    plain glyphs because of one bad id).

    ROOT-CAUSE FIX ("bot ko DM me /start karne pe non-premium (plain) emoji
    aate hai"): the /start, /help and /about cards ship HAND-WRITTEN
    <emoji id="..."> tags in their source (see utils/formatters.
    PREMIUM_EMOJI_IDS). Those ids were never checked against the owner's real
    pack, so at least one of them does not exist for this bot. Telegram
    rejects the WHOLE message for a single unknown custom-emoji id, and the
    wrapper's retry path then strips EVERY tag — which is why the very first
    message a user sees in DM came back completely plain while group messages
    (built from auto-wrapped glyphs, all of them resolved ids) looked fine.
    Now any id that startup resolution did not confirm is unwrapped up front,
    so the rest of the card still renders as premium animated emoji.
    """
    if not text or "<emoji" not in text:
        return text

    # Let stale quarantines lapse before judging any id, otherwise one bad
    # minute permanently downgrades the bot to plain emoji.
    _expire_quarantine()


    def _bad(emoji_id: str) -> bool:
        # Only treat an id as bad if Telegram explicitly rejected it (it is in
        # QUARANTINED_IDS). The old code also treated any id not in RESOLVED_IDS
        # as bad — but auto-assigned ids (from emoji_search or assign_pool_id)
        # are NOT in RESOLVED_IDS, so valid working ids were wrongly stripped.
        # This was the root cause of "premium emojis change midway / stop working".
        return emoji_id in QUARANTINED_IDS

    from utils.emoji_map import EMOJI_ID_FALLBACKS, EMOJI_ID_MAP

    def _remap(glyph: str) -> "str | None":
        """A hand-written id can be dead while the SAME glyph has a perfectly
        valid resolved id in EMOJI_ID_MAP. ROOT-CAUSE of "DM /start me premium
        emoji nahi aate": the card's hand-written ids were simply unwrapped, so
        the very first message a user sees came back plain. Now the glyph is
        re-pointed at its resolved id and stays premium."""
        base = glyph.replace("\ufe0f", "")
        for candidate in EMOJI_ID_FALLBACKS.get(base, [EMOJI_ID_MAP.get(base)]):
            if candidate and not _bad(str(candidate)):
                return str(candidate)
        return None

    def _sub(match):
        emoji_id, glyph = match.group(1), match.group(2)
        if not _bad(emoji_id):
            return match.group(0)
        replacement = _remap(glyph)
        if replacement:
            return f'<emoji id="{replacement}">{glyph}</emoji>'
        return glyph

    return cap_emoji_entities(
        re.compile(r"<emoji id=[\"']?(-?\d+)[\"']?>(.*?)</emoji>", re.DOTALL).sub(_sub, text)
    )



def _wants_html(bound_arguments) -> bool:
    """True unless the caller explicitly disabled parse mode. Injecting
    <emoji> tags into a ParseMode.DISABLED send would show raw markup."""
    parse_mode = bound_arguments.get("parse_mode")
    name = getattr(parse_mode, "name", None) or str(parse_mode or "").upper()
    return "DISABLED" not in name and "MARKDOWN" not in name


def _wrap(method_name: str, param_name: str) -> None:
    original = getattr(Client, method_name)
    if getattr(original, "_emoji_patched", False):
        return  # already patched (e.g. apply_emoji_patch called twice)

    sig = inspect.signature(original)

    @functools.wraps(original)
    async def wrapper(self, *args, **kwargs):
        bound = sig.bind_partial(self, *args, **kwargs)
        bound.apply_defaults()
        value = bound.arguments.get(param_name)
        plain_value = value

        if isinstance(value, str) and value:
            if not _wants_html(bound.arguments):
                # ParseMode.DISABLED/MARKDOWN: an <emoji id="..."> tag would be
                # shown to the user as literal markup, so always unwrap it to
                # the plain glyph — even when the CALLER hand-wrote the tag.
                bound.arguments[param_name] = strip_all_emoji_tags(value)
            elif custom_emoji_allowed():
                try:
                    prepared = await verify_custom_emojis(self, value)
                    bound.arguments[param_name] = drop_quarantined(prepared)
                except Exception:
                    # Never let emoji wrapping itself break a real send.
                    log.exception("emoji_patch: falling back to plain %s()", method_name)
            else:
                bound.arguments[param_name] = strip_all_emoji_tags(value)

        # BUG FIX (help_cb -> [400 ENTITY_TEXT_INVALID] escaped this wrapper):
        # the retry below used to be gated on "did WE add an <emoji> tag?".
        # Many plugin strings (start/help/about cards) ship hand-written
        # <emoji id="..."> tags in the source, so `verify_custom_emojis()`
        # returned them unchanged, the "wrapped" flag stayed False, and the
        # 400 was re-raised straight into the plugin instead of being retried
        # without entities. Gate on what is ACTUALLY being sent instead.
        final_value = bound.arguments.get(param_name)
        has_entities = isinstance(final_value, str) and "<emoji" in final_value

        try:
            result = await original(*bound.args, **bound.kwargs)
            if has_entities:
                _register_success()
            return result
        except Exception as exc:
            if not (has_entities and _is_entity_error(exc)):
                raise
            _register_failure(exc, final_value)

        # GRANULAR RETRY (fixes "ek bad id ki wajah se poora card plain"):
        # _register_failure() has just quarantined every id that was in the
        # rejected message, so re-running drop_quarantined() now unwraps ONLY
        # the offending ids (and re-points glyphs at a good id when one
        # exists). The rest of the card keeps its premium animated emoji.
        try:
            retried = drop_quarantined(final_value)
            if retried != final_value and "<emoji" in retried:
                bound.arguments[param_name] = retried
                result = await original(*bound.args, **bound.kwargs)
                _register_success()
                return result
        except Exception as exc2:
            if _is_entity_error(exc2):
                _register_failure(exc2, bound.arguments.get(param_name) or "")
            else:
                raise

        # Last resort: strip every custom-emoji entity. All other formatting
        # (<b>, <code>, <blockquote> "quote", ...) is preserved, so the card
        # still renders — just with plain glyphs.
        bound.arguments[param_name] = strip_all_emoji_tags(plain_value)
        return await original(*bound.args, **bound.kwargs)

    wrapper._emoji_patched = True
    setattr(Client, method_name, wrapper)


_MEDIA_METHODS = (
    # BUG ("album / media edit ke caption me abhi bhi normal emoji"):
    # these two never touched Client.send_photo & co — the caption lives on an
    # InputMedia* object — so every album caption and every edited media card
    # was sent with plain unicode emoji.
    ("send_media_group", "media"),
    ("edit_message_media", "media"),
)


def _wants_html_media(media) -> bool:
    parse_mode = getattr(media, "parse_mode", None)
    name = getattr(parse_mode, "name", None) or str(parse_mode or "").upper()
    if parse_mode is None:
        return True
    return "DISABLED" not in name and "MARKDOWN" not in name


async def _prepare_media_caption(client, media) -> None:
    caption = getattr(media, "caption", None)
    if not isinstance(caption, str) or not caption:
        return
    if not _wants_html_media(media):
        try:
            media.caption = strip_all_emoji_tags(caption)
        except Exception:
            pass
        return
    try:
        media.caption = drop_quarantined(await verify_custom_emojis(client, caption))
    except Exception:
        log.exception("emoji_patch: could not premium-wrap a media caption")


def _wrap_media(method_name: str, param_name: str) -> None:
    original = getattr(Client, method_name, None)
    if original is None or getattr(original, "_emoji_patched", False):
        return

    sig = inspect.signature(original)

    @functools.wraps(original)
    async def wrapper(self, *args, **kwargs):
        bound = sig.bind_partial(self, *args, **kwargs)
        bound.apply_defaults()
        value = bound.arguments.get(param_name)
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            if item is not None and hasattr(item, "caption"):
                await _prepare_media_caption(self, item)
        try:
            return await original(*bound.args, **bound.kwargs)
        except Exception as exc:
            if not _is_entity_error(exc):
                raise
            for item in items:
                if item is not None and isinstance(getattr(item, "caption", None), str):
                    _register_failure(exc, item.caption)
                    item.caption = strip_all_emoji_tags(item.caption)
            return await original(*bound.args, **bound.kwargs)

    wrapper._emoji_patched = True
    setattr(Client, method_name, wrapper)


def apply_emoji_patch() -> None:
    """Patch pyrogram.Client so every outgoing message/caption is run
    through the premium-emoji auto-wrap + verify pipeline. Idempotent —
    safe to call more than once."""
    global _applied
    if _applied:
        return
    for method_name, param_name in _PATCHED_METHODS:
        _wrap(method_name, param_name)
    for method_name, param_name in _MEDIA_METHODS:
        _wrap_media(method_name, param_name)
    _applied = True
    log.info("emoji_patch: premium-emoji auto-wrap active on %s",
              ", ".join(m for m, _ in _PATCHED_METHODS))
