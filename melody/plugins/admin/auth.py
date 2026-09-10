"""
👑 Auth management — authorize/unauthorize users in groups
"""
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from utils.database import auth_user, unauth_user, get_auth_users
from utils.decorators import error_handler
from utils.formatters import quote_html, mention_html


@bot.on_message(filters.command("auth") & filters.group)
@error_handler
async def auth_cmd(client: Client, message: Message):
    # Only group admins or creator
    if not await _is_admin(client, message):
        return await message.reply(quote_html("⚠️ Admins only."), parse_mode=enums.ParseMode.HTML)

    user = await _get_target_user(message)
    if not user:
        return await message.reply(
            quote_html("**Usage:** `/auth @user` or reply to a user"), parse_mode=enums.ParseMode.HTML
        )

    await auth_user(message.chat.id, user.id)
    await message.reply(
        quote_html(f"✅ {mention_html(user.id, user.first_name)} authorized to use bot commands."),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command("unauth") & filters.group)
@error_handler
async def unauth_cmd(client: Client, message: Message):
    if not await _is_admin(client, message):
        return await message.reply(quote_html("⚠️ Admins only."), parse_mode=enums.ParseMode.HTML)

    user = await _get_target_user(message)
    if not user:
        return await message.reply(
            quote_html("**Usage:** `/unauth @user` or reply to a user"), parse_mode=enums.ParseMode.HTML
        )

    await unauth_user(message.chat.id, user.id)
    await message.reply(
        quote_html(f"🚫 {mention_html(user.id, user.first_name)} unauthorized."),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command("authlist") & filters.group)
@error_handler
async def authlist_cmd(client: Client, message: Message):
    auth_ids = await get_auth_users(message.chat.id)
    if not auth_ids:
        return await message.reply(
            quote_html("📋 No authorized users in this group."), parse_mode=enums.ParseMode.HTML
        )

    lines = ["<b>📋 Authorized Users:</b>\n"]
    for uid in auth_ids:
        try:
            user = await client.get_users(uid)
            lines.append(f"• {mention_html(user.id, user.first_name)} (<code>{uid}</code>)")
        except Exception:
            lines.append(f"• <code>{uid}</code>")
    await message.reply(quote_html("\n".join(lines)), parse_mode=enums.ParseMode.HTML)


async def _is_admin(client: Client, message: Message) -> bool:
    try:
        member = await client.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def _get_target_user(message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    if len(message.command) > 1:
        try:
            return await message._client.get_users(message.command[1])
        except Exception:
            pass
    return None
