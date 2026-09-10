"""
🆔 /emojiid — Owner-only utility to extract REAL Telegram premium
animated custom-emoji ids.

WHY THIS EXISTS
----------------
"Premium animated emoji" only render when an <emoji id="..."> entity
points at a real Telegram custom-emoji document. A previous change broke
this (and took the surrounding <blockquote> "quote" down with it) by
hand-typing plausible-looking ids that were never real — see
utils/telegram_html.py and utils/formatters.py for the fix that makes bad
ids fail safe.

But failing safe just means the emoji quietly doesn't show up — to
actually get the animated emoji to appear, this bot needs *real* custom
emoji ids. The only legitimate source for those is Telegram itself: every
message that contains a premium animated emoji carries its real document
id in that message's entities.

USAGE
-----
Reply to any message containing one or more premium animated emoji with
/emojiid. The bot reads the real ids straight from Telegram's own message
entities (no guessing), double-checks each one is still resolvable via
get_custom_emoji_stickers, and returns copy-pastable
premium_emoji("<id>", "<glyph>") snippets for the ones that are.
"""
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import MessageEntityType
from melody import bot
from utils.decorators import owner_only, error_handler
from utils.formatters import send_quote


@bot.on_message(filters.command("emojiid"))
@error_handler
@owner_only
async def emoji_id_cmd(client: Client, message: Message):
    reply = message.reply_to_message
    if not reply or not (reply.text or reply.caption):
        await send_quote(
            message,
            "🆔 <b>Get a real premium emoji id</b>\n\n"
            "Reply to a message that contains a premium animated emoji "
            "with <code>/emojiid</code> — Telegram itself will tell us the "
            "real document id, so it's guaranteed to work.",
        )
        return

    text = reply.text or reply.caption
    entities = reply.entities or reply.caption_entities or []
    custom = [e for e in entities if e.type == MessageEntityType.CUSTOM_EMOJI]

    if not custom:
        await send_quote(
            message,
            "❌ That message doesn't contain any premium animated emoji "
            "(no <code>custom_emoji</code> entities found).",
        )
        return

    ids = [e.custom_emoji_id for e in custom]
    glyphs = [text[e.offset : e.offset + e.length] for e in custom]

    # Double-check each id still resolves right now — Telegram custom
    # emoji can be deleted/rotated, so "it was real a moment ago" isn't
    # quite the same guarantee as "it resolves right now".
    try:
        from utils.custom_emoji import resolve_custom_emoji

        resolvable = set(await resolve_custom_emoji(client, ids))
    except Exception:
        resolvable = set()

    lines = ["🆔 <b>Real premium emoji ids found:</b>\n"]
    for emoji_id, glyph in zip(ids, glyphs):
        status = "✅" if emoji_id in resolvable else "⚠️ not resolvable right now"
        lines.append(
            f"{status} <code>{glyph}</code> → "
            f"<code>premium_emoji(\"{emoji_id}\", \"{glyph}\")</code>"
        )
    lines.append(
        "\n<i>Paste the ✅ ones into utils/formatters.py or a message string "
        "and send it through send_quote() — invalid ids always fail safe.</i>"
    )

    await send_quote(message, "\n".join(lines), client=client)
