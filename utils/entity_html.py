"""
🎨 Message → HTML converter that KEEPS premium (custom) emoji.

WHY THIS EXISTS
---------------
REQUESTED: "welcome mesg me premium emojis support add kr. Agr premium user
welcome me jo emojis set karega bs vahi emojis use ho — unki emoji id khud
nikal ke convert karna."

`/setwelcome <text>` used to store `message.text`, which is the PLAIN unicode
string: every `custom_emoji` entity (a premium emoji) collapses to its normal
fallback glyph and every bold/italic/link is lost. So a premium user could set
a beautiful animated-emoji welcome and the bot would greet people with plain
emoji instead.

`message_html()` walks the message entities itself and re-emits them as
Telegram HTML — including `<emoji id="12345">😀</emoji>` for every premium
emoji, so the exact ids the user picked are stored and re-sent verbatim.
Non-premium glyphs in the same text stay plain here and are upgraded later by
`utils.telegram_html.auto_premium_emoji()` (which never touches a glyph that
is already inside an `<emoji>` tag).
"""
from __future__ import annotations

import html as _html

from pyrogram import enums

# entity type -> (open tag, close tag)
_SIMPLE_TAGS = {
    enums.MessageEntityType.BOLD: ("<b>", "</b>"),
    enums.MessageEntityType.ITALIC: ("<i>", "</i>"),
    enums.MessageEntityType.UNDERLINE: ("<u>", "</u>"),
    enums.MessageEntityType.STRIKETHROUGH: ("<s>", "</s>"),
    enums.MessageEntityType.SPOILER: ('<span class="tg-spoiler">', "</span>"),
    enums.MessageEntityType.CODE: ("<code>", "</code>"),
    enums.MessageEntityType.BLOCKQUOTE: ("<blockquote>", "</blockquote>"),
}


def _open_close(entity) -> "tuple[str, str] | None":
    etype = entity.type
    simple = _SIMPLE_TAGS.get(etype)
    if simple:
        return simple
    if etype == enums.MessageEntityType.PRE:
        lang = getattr(entity, "language", None)
        if lang:
            return (f'<pre><code class="language-{_html.escape(str(lang))}">', "</code></pre>")
        return ("<pre>", "</pre>")
    if etype == enums.MessageEntityType.TEXT_LINK:
        url = _html.escape(str(getattr(entity, "url", "") or ""))
        return (f'<a href="{url}">', "</a>")
    if etype == enums.MessageEntityType.TEXT_MENTION:
        user = getattr(entity, "user", None)
        uid = getattr(user, "id", None)
        if uid:
            return (f'<a href="tg://user?id={uid}">', "</a>")
        return None
    if etype == enums.MessageEntityType.CUSTOM_EMOJI:
        emoji_id = getattr(entity, "custom_emoji_id", None)
        if emoji_id:
            return (f'<emoji id="{int(emoji_id)}">', "</emoji>")
        return None
    # URL / mention / hashtag / bot_command … need no markup at all.
    return None


def entities_to_html(text: str, entities, offset: int = 0) -> str:
    """Render `text[offset:]` as Telegram HTML using `entities`.

    Telegram entity offsets are counted in UTF-16 code units, so the whole
    conversion is done on the UTF-16 representation and decoded back at the
    end — otherwise every emoji (a surrogate pair) shifts the following
    entities by one and the markup lands on the wrong characters.
    """
    if not text:
        return ""
    utf16 = text.encode("utf-16-le")
    total = len(utf16) // 2

    # position (in UTF-16 units) -> list of tag strings to insert there
    opens: dict[int, list[str]] = {}
    closes: dict[int, list[str]] = {}

    for entity in sorted(entities or [], key=lambda e: (e.offset, -e.length)):
        start, end = entity.offset, entity.offset + entity.length
        if end <= offset or start > total:
            continue
        pair = _open_close(entity)
        if not pair:
            continue
        open_tag, close_tag = pair
        start = max(start, offset)
        end = min(end, total)
        if end <= start:
            continue
        opens.setdefault(start, []).append(open_tag)
        # Closing tags must come out innermost-first.
        closes.setdefault(end, []).insert(0, close_tag)

    # Tags may only be inserted at these boundaries; the text between two
    # boundaries is decoded as ONE slice so surrogate pairs (every emoji is
    # one) survive instead of decoding to nothing.
    points = sorted({offset, total} | {p for p in opens if p >= offset}
                    | {p for p in closes if p >= offset})
    out: list[str] = []
    for index, pos in enumerate(points):
        for tag in closes.get(pos, []):
            out.append(tag)
        for tag in opens.get(pos, []):
            out.append(tag)
        if index + 1 < len(points):
            chunk = utf16[pos * 2:points[index + 1] * 2].decode("utf-16-le", errors="ignore")
            out.append(_html.escape(chunk, quote=False))
    return "".join(out)


def message_html(message, skip_command: bool = False) -> str:
    """HTML body of `message` (text or caption), premium emoji preserved.

    `skip_command=True` drops the leading `/command` token, which is what the
    `/setwelcome …` style setters need.
    """
    if message is None:
        return ""
    raw = message.text or message.caption or ""
    if not raw:
        return ""
    entities = message.entities or message.caption_entities or []

    offset = 0
    if skip_command and raw.startswith("/"):
        head, _, _ = raw.partition("\n")
        first = raw.split(None, 1)
        if len(first) < 2:
            return ""
        # UTF-16 offset of the argument start.
        consumed = raw.index(first[1])
        offset = len(raw[:consumed].encode("utf-16-le")) // 2
        del head
    return entities_to_html(raw, entities, offset=offset).strip()
