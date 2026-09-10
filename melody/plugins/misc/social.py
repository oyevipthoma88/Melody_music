"""Lightweight social commands with public GIF fallbacks.

Commands include relationship, reaction, and celebration actions such as /couple,
/couples, /kiss, /hug, /slap, /pat, /dance, and /cheers. Use them as a reply or
pass a username: ``/kiss @username``.

Every resolved sender and target is rendered as a clickable Telegram mention.
The bot never downloads or stores the remote GIFs; Telegram fetches the URL when
possible. If a provider is unavailable, a friendly text reply is sent.
"""
from __future__ import annotations

import hashlib
import random

from pyrogram import Client, enums, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

from melody import bot
from utils.decorators import error_handler
from utils.melody_theme import tag
from utils.reply import reply_params


# Public CDN URLs; these are presentation assets only and are not persisted.
# Keep several choices per action so a chat does not see the same animation on
# every use. The text fallback keeps the command useful if a CDN is blocked.
_GIFS = {
    "couple": [
        "https://media.giphy.com/media/3o7TKq5M8t7wP3p7Jm/giphy.gif",
        "https://media.giphy.com/media/l0MYt5jPR6QX5pnqM/giphy.gif",
    ],
    "kiss": [
        "https://media.giphy.com/media/G3va34OqT4nQ4/giphy.gif",
        "https://media.giphy.com/media/zkppEMFvRX5FC/giphy.gif",
    ],
    "hug": [
        "https://media.giphy.com/media/od5H3PmEG5EVq/giphy.gif",
        "https://media.giphy.com/media/ArLxZ4PebH2Ug/giphy.gif",
    ],
    "cuddle": [
        "https://media.giphy.com/media/143v0Z4767T15e/giphy.gif",
        "https://media.giphy.com/media/PHZ7v9tfQu0o0/giphy.gif",
    ],
    "love": [
        "https://media.giphy.com/media/26FLdmIp6wJr91JAI/giphy.gif",
        "https://media.giphy.com/media/1R6cd7g9m7G7S/giphy.gif",
    ],
    "highfive": [
        "https://media.giphy.com/media/3oEjHV0z8S7WM4MwnK/giphy.gif",
        "https://media.giphy.com/media/26ufdipQqU2lhNA4g/giphy.gif",
    ],
    "ship": [
        "https://media.giphy.com/media/26FLdmIp6wJr91JAI/giphy.gif",
        "https://media.giphy.com/media/l41YgA9fR4W8s7QwM/giphy.gif",
    ],
    "slap": [
        "https://media.giphy.com/media/xT9IgG50Fb7Mi0prBC/giphy.gif",
        "https://media.giphy.com/media/arbHBoiUWUgmc/giphy.gif",
    ],
    "punch": [
        "https://media.giphy.com/media/arbHBoiUWUgmc/giphy.gif",
        "https://media.giphy.com/media/11HeubLHnQJSAU/giphy.gif",
    ],
    "bonk": [
        "https://media.giphy.com/media/qs4ll1FSxKnNHeSmom/giphy.gif",
        "https://media.giphy.com/media/5fBH6zodw7VMu/giphy.gif",
    ],
    "pat": [
        "https://media.giphy.com/media/ye7OTQgwmVuVy/giphy.gif",
        "https://media.giphy.com/media/ARspC2qYB3H8Q/giphy.gif",
    ],
    "poke": [
        "https://media.giphy.com/media/6aiIvESLiM2rS/giphy.gif",
        "https://media.giphy.com/media/6aiIvESLiM2rS/giphy.gif",
    ],
    "wink": [
        "https://media.giphy.com/media/6ra84Uso2hoir3YCgb/giphy.gif",
        "https://media.giphy.com/media/3oKIPdIsq5q3Y8Qe5a/giphy.gif",
    ],
    "dance": [
        "https://media.giphy.com/media/13CoXDiaCcCoyk/giphy.gif",
        "https://media.giphy.com/media/13CoXDiaCcCoyk/giphy.gif",
    ],
    "laugh": [
        "https://media.giphy.com/media/10tIjpzIu8fe0/giphy.gif",
        "https://media.giphy.com/media/En8fsYde6cqvhYBnAb/giphy.gif",
    ],
    "cry": [
        "https://media.giphy.com/media/ROF8OQvDmxytW/giphy.gif",
        "https://media.giphy.com/media/d2lcHJTG5Tscg/giphy.gif",
    ],
    "angry": [
        "https://media.giphy.com/media/11tTNkNy1SdXGg/giphy.gif",
        "https://media.giphy.com/media/l0Iy5jWZFjb60dBXa/giphy.gif",
    ],
    "cheers": [
        "https://media.giphy.com/media/g9582DNuQppxC/giphy.gif",
        "https://media.giphy.com/media/3o6Zt6D5sZqY9Q5qjC/giphy.gif",
    ],
    "brofist": [
        "https://media.giphy.com/media/3oEjHV0z8S7WM4MwnK/giphy.gif",
        "https://media.giphy.com/media/26ufdipQqU2lhNA4g/giphy.gif",
    ],
    "clap": [
        "https://media.giphy.com/media/3Gm15yD3P2f9r/giphy.gif",
        "https://media.giphy.com/media/26BRBKqUiq586bRVm/giphy.gif",
    ],
    "wave": [
        "https://media.giphy.com/media/xT9IgG50Fb7Mi0prBC/giphy.gif",
        "https://media.giphy.com/media/8m4R4pvViWtRzbloJ1/giphy.gif",
    ],
}

_CAPTIONS = {
    "couple": "💞 <b>{sender}</b> × <b>{target}</b> — Melody ne couple energy approve kar di.",
    "kiss": "💋 <b>{sender}</b> ne <b>{target}</b> ko ek cute kiss bheja.",
    "hug": "🫂 <b>{sender}</b> ne <b>{target}</b> ko warm hug bheja.",
    "cuddle": "🤍 <b>{sender}</b> aur <b>{target}</b> ka comfort-cuddle moment.",
    "love": "❤️ <b>{sender}</b> is sending good vibes to <b>{target}</b>.",
    "highfive": "🙌 <b>{sender}</b> × <b>{target}</b> — high-five, team Melody!",
    "ship": "💞 <b>{sender}</b> + <b>{target}</b> — group ne inki vibe notice kar li.",
    "slap": "👋 <b>{sender}</b> ne <b>{target}</b> ko playful slap meme bheja.",
    "punch": "👊 <b>{sender}</b> ne <b>{target}</b> ko meme-style punch bheja — no hard feelings.",
    "bonk": "🔨 <b>{sender}</b> ne <b>{target}</b> ko bonk karke bola: drama kam.",
    "pat": "🤗 <b>{sender}</b> ne <b>{target}</b> ko gentle pat diya.",
    "poke": "👉 <b>{sender}</b> is poking <b>{target}</b> — hello, notice me!",
    "wink": "😉 <b>{sender}</b> ne <b>{target}</b> ko ek wink bheja.",
    "dance": "💃 <b>{sender}</b> aur <b>{target}</b> ka mini dance break.",
    "laugh": "😂 <b>{sender}</b> aur <b>{target}</b> ek saath has rahe hain.",
    "cry": "😢 <b>{sender}</b> aur <b>{target}</b> ka emotional moment — tissue please.",
    "angry": "😤 <b>{sender}</b> ka dramatic angry reaction <b>{target}</b> ke liye.",
    "cheers": "🥂 <b>{sender}</b> aur <b>{target}</b> ke naam ek virtual cheers.",
    "brofist": "👊 <b>{sender}</b> ne <b>{target}</b> ko bro-fist diya.",
    "clap": "👏 <b>{sender}</b> is clapping for <b>{target}</b>.",
    "wave": "👋 <b>{sender}</b> ne <b>{target}</b> ko wave kiya.",
}

_ACTION_NAMES = {
    "couple": "COUPLE MATCH",
    "kiss": "KISS DROP",
    "hug": "WARM HUG",
    "cuddle": "CUDDLE MOMENT",
    "love": "LOVE VIBES",
    "highfive": "HIGH-FIVE",
    "ship": "SHIP CHECK",
    "slap": "PLAYFUL SLAP",
    "punch": "MEME PUNCH",
    "bonk": "BONK ALERT",
    "pat": "GENTLE PAT",
    "poke": "POKE ALERT",
    "wink": "WINK DROP",
    "dance": "DANCE BREAK",
    "laugh": "LAUGH MODE",
    "cry": "EMOTIONAL MODE",
    "angry": "DRAMA MODE",
    "cheers": "CHEERS",
    "brofist": "BRO-FIST",
    "clap": "APPLAUSE",
    "wave": "WAVE",
}


def _mention(user) -> str:
    """Return a safe, clickable Telegram mention for a Pyrogram user."""
    name = " ".join(filter(None, [getattr(user, "first_name", None), getattr(user, "last_name", None)]))
    return tag(int(user.id), name or "User")


def _compatibility(sender, target) -> int:
    """Return a stable pair score so repeated cards do not jump randomly."""
    raw = f"{int(sender.id)}:{int(target.id)}".encode()
    return 70 + (int(hashlib.sha256(raw).hexdigest()[:4], 16) % 30)


def _meter(score: int) -> str:
    filled = max(1, min(10, round(score / 10)))
    return "=" * filled + "-" * (10 - filled)


def _caption(action: str, sender, target) -> str:
    sender_mention = _mention(sender)
    target_mention = _mention(target)
    title = _ACTION_NAMES[action]
    body = _CAPTIONS[action].format(sender=sender_mention, target=target_mention)
    if action != "couple":
        return f"<b>✨ MELODY {title}</b>\n\n{body}\n\n<i>Reply to a user or use @{getattr(target, 'username', None) or 'username'} for another round.</i>"
    score = _compatibility(sender, target)
    return (
        f"<b>✨ MELODY {title}</b>\n\n"
        f"{body}\n\n"
        f"<b>Compatibility</b>  <code>{_meter(score)} {score}%</code>\n"
        f"<i>Premium pair card • Reply karke ya /couples @username se phir try karo.</i>"
    )


def _media_markup(action: str) -> InlineKeyboardMarkup:
    """Add a harmless discovery button; it does not create a dead callback."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("💞 Try /couples", switch_inline_query_current_chat="/couples ")]]
    ) if action == "couple" else InlineKeyboardMarkup(
        [[InlineKeyboardButton("💫 Try /couple", switch_inline_query_current_chat="/couple ")]]
    )


async def _target(client: Client, message: Message):
    """Resolve text mention entity, reply target, then username or numeric ID."""
    for entity in (message.entities or []):
        if entity.type == enums.MessageEntityType.TEXT_MENTION and entity.user:
            return entity.user
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    if len(message.command or []) > 1:
        raw = (message.command[1] or "").lstrip("@")
        try:
            return await client.get_users(int(raw) if raw.isdigit() else raw)
        except Exception:
            return None
    return None


@bot.on_message(filters.command(list(_GIFS) + ["couples"]) & (filters.group | filters.private))
@error_handler
async def social_gif(client: Client, message: Message):
    requested = (message.command[0] if message.command else "hug").lower()
    action = "couple" if requested == "couples" else requested
    sender = message.from_user
    if not sender or action not in _GIFS:
        return
    target = await _target(client, message)
    if target is None:
        # /love, /couple and /couples without a target remain usable as a self/mood card;
        # targeted actions ask for a reply to avoid random or ambiguous tagging.
        if action in ("love", "couple"):
            target = sender
        else:
            return await message.reply(
                f"Usage: reply to someone with <code>/{requested}</code> ya <code>/{requested} @username</code>",
                parse_mode=enums.ParseMode.HTML,
            )

    caption = _caption(action, sender, target)
    gif_url = random.choice(_GIFS[action])
    try:
        await client.send_animation(
            message.chat.id,
            gif_url,
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_media_markup(action),
            reply_parameters=reply_params(message.id),
        )
    except Exception:
        await message.reply(caption, parse_mode=enums.ParseMode.HTML)
