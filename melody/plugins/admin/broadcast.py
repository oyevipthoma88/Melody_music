"""Owner-only broadcast with true forwards and permission-aware pinning.

A replied message is forwarded with Telegram's forward semantics, preserving
its original author/source attribution. We never reconstruct it with copy().
Each destination is isolated: a failure, revoked membership, missing pin
rights, or FloodWait cannot abort the remaining broadcast.
"""
from __future__ import annotations

import asyncio

from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from melody import bot
from utils.database import get_all_chats
from utils.decorators import error_handler, owner_only
from utils.formatters import quote_html


async def _bot_can_pin(client: Client, chat_id: int, bot_id: int) -> bool:
    """Return whether this bot currently has the group pin privilege."""
    try:
        member = await client.get_chat_member(chat_id, bot_id)
        status = getattr(member, "status", None)
        if str(status).lower().endswith("owner"):
            return True
        if not str(status).lower().endswith("administrator"):
            return False
        privileges = getattr(member, "privileges", None)
        return bool(getattr(privileges, "can_pin_messages", False))
    except Exception:
        return False


async def _forward_one(client: Client, source: Message, chat_id: int, bot_id: int) -> tuple[bool, bool]:
    """Forward one source message and pin it when the bot is allowed.

    Returns ``(sent, pinned)``. Pin failure never turns a successful forward
    into a failed delivery; Telegram permissions can change between checks.
    """
    while True:
        try:
            sent = await client.forward_messages(
                chat_id=chat_id,
                from_chat_id=source.chat.id,
                message_ids=source.id,
                disable_notification=False,
            )
            if isinstance(sent, list):
                sent = sent[0] if sent else None
            if sent is None:
                return False, False
            if await _bot_can_pin(client, chat_id, bot_id):
                try:
                    await client.pin_chat_message(
                        chat_id, sent.id, disable_notification=True
                    )
                    return True, True
                except FloodWait:
                    # A pin flood wait should not duplicate the forward. Treat
                    # it as an unpinned success and continue delivery.
                    return True, False
                except Exception:
                    return True, False
            return True, False
        except FloodWait as exc:
            await asyncio.sleep(max(1, int(exc.value)))
        except Exception:
            return False, False


@bot.on_message(filters.command("broadcast") & filters.private)
@error_handler
@owner_only
async def broadcast_cmd(client: Client, message: Message):
    if not message.reply_to_message:
        return await message.reply(
            quote_html("<b>Reply to a message to broadcast it.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )

    chats = await get_all_chats()
    if not chats:
        return await message.reply(
            quote_html("❌ <b>No chats in database.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )

    status_msg = await message.reply(
        quote_html(f"📢 <b>Broadcasting to {len(chats)} chats…</b>"),
        parse_mode=enums.ParseMode.HTML,
    )
    bot_user = await client.get_me()
    sent_count = 0
    pinned_count = 0
    failed_count = 0
    source = message.reply_to_message

    for chat in chats:
        chat_id = chat.get("chat_id") if isinstance(chat, dict) else chat
        if not chat_id:
            failed_count += 1
            continue
        sent, pinned = await _forward_one(client, source, chat_id, bot_user.id)
        if sent:
            sent_count += 1
            pinned_count += int(pinned)
        else:
            failed_count += 1
        # Keep a small gap so a large database does not trigger a burst flood.
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        quote_html(
            "✅ <b>Broadcast complete</b>\n\n"
            f"📨 <b>Forwarded:</b> <code>{sent_count}</code>\n"
            f"📌 <b>Pinned:</b> <code>{pinned_count}</code>\n"
            f"❌ <b>Failed:</b> <code>{failed_count}</code>"
        ),
        parse_mode=enums.ParseMode.HTML,
    )
