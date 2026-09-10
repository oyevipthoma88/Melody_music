"""
🔁 /reboot + /reload — like every top music bot.

REQUESTED ("/reload, /reboot cmnd add kr baki music bot ka dekh ke"):

  /reload   (group, admins)   → refresh this chat's admin list + auth list +
                                settings cache. Use it right after promoting
                                someone so the bot sees them instantly.
  /reload   (owner, private)  → additionally hot-reloads every plugin module
                                and re-warms the assistant's peer cache.
  /reboot   (sudo / owner)    → full process restart, works in groups too.
  /restart                    → alias handled in restart.py (private only).

Both commands answer FIRST and do the slow work afterwards, so the user
never stares at a dead chat.
"""
import asyncio
import importlib
import os
import pkgutil
import sys

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from melody import bot
from melody.logging import LOGGER
from utils.decorators import error_handler, owner_only, refresh_chat_admins
from utils.database import invalidate_auth_cache, get_auth_users
from utils.formatters import quote_html
from utils.gc_db import is_sudo


# ── /reload ─────────────────────────────────────────────────────────────────

@bot.on_message(filters.command(["reload", "admincache", "refresh"]) & filters.group)
@error_handler
async def reload_group_cmd(client: Client, message: Message):
    """Refresh cached admins / auth users for THIS chat (any admin can run)."""
    chat_id = message.chat.id
    msg = await message.reply(
        quote_html("🔄 <b>Rᴇʟᴏᴀᴅɪɴɢ ᴀᴅᴍɪɴ ᴄᴀᴄʜᴇ…</b>"),
        parse_mode=enums.ParseMode.HTML,
    )

    admins = await refresh_chat_admins(client, chat_id)

    invalidate_auth_cache(chat_id)
    try:
        auth = await get_auth_users(chat_id)
    except Exception:
        auth = []

    try:
        from utils.gc_db import invalidate_flag_cache

        invalidate_flag_cache(chat_id)
    except Exception:
        pass

    await msg.edit(
        quote_html(
            "<blockquote>✅ <b>Rᴇʟᴏᴀᴅᴇᴅ</b></blockquote>\n\n"
            f"👮 <b>Admins:</b> <code>{len(admins)}</code>\n"
            f"🔑 <b>Auth users:</b> <code>{len(auth)}</code>\n"
            "⚙️ <b>Settings cache:</b> cleared\n\n"
            "<i>Naye admins ab turant recognise honge.</i>"
        ),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command(["reload", "admincache"]) & filters.private)
@owner_only
@error_handler
async def reload_cmd(client: Client, message: Message):
    """Hot-reload all plugins without a full restart (owner, private)."""
    msg = await message.reply(
        quote_html("🔄 <b>Reloading plugins…</b>"), parse_mode=enums.ParseMode.HTML
    )
    reloaded = []
    failed = []

    import melody.plugins as plugins_pkg

    for _finder, name, _ispkg in pkgutil.walk_packages(
        plugins_pkg.__path__, plugins_pkg.__name__ + "."
    ):
        try:
            mod = sys.modules.get(name)
            if mod:
                importlib.reload(mod)
                reloaded.append(name.split(".")[-1])
            else:
                importlib.import_module(name)
                reloaded.append(name.split(".")[-1])
        except Exception as e:
            LOGGER.error("Reload failed for %s: %s", name, e)
            failed.append(name.split(".")[-1])

    # Also refresh the assistant's peer cache — a stale/empty peer table is
    # what makes the first /play after a restart fail with "ID not found".
    warmed = 0
    try:
        from melody.core.call import warm_assistant_peers

        warmed = await warm_assistant_peers()
    except Exception as e:
        LOGGER.warning("Peer warm-up during /reload failed: %s", e)

    # Drop every cached permission/auth entry so promotions apply at once.
    try:
        from utils.decorators import invalidate_admin_cache

        invalidate_admin_cache()
        invalidate_auth_cache()
    except Exception:
        pass

    text = f"✅ <b>Reloaded {len(reloaded)} plugins</b> · 🎙 {warmed} VC peers warmed\n"
    if reloaded:
        text += f"<code>{'</code>, <code>'.join(reloaded[:20])}</code>\n"
    if failed:
        text += f"\n❌ <b>Failed ({len(failed)}):</b> <code>{'</code>, <code>'.join(failed)}</code>"

    await msg.edit(quote_html(text), parse_mode=enums.ParseMode.HTML)


# ── /reboot ─────────────────────────────────────────────────────────────────

async def _do_reboot():
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable, "-m", "melody"])


@bot.on_message(filters.command(["reboot"]))
@error_handler
async def reboot_cmd(client: Client, message: Message):
    """Full process restart — sudo users and the owner, anywhere."""
    user = message.from_user
    if not user or not await is_sudo(user.id):
        return  # hidden command for everyone else

    await message.reply(
        quote_html(
            "<blockquote>🔁 <b>Rᴇʙᴏᴏᴛɪɴɢ Mᴇʟᴏᴅʏ…</b></blockquote>\n"
            "<i>Kuch hi seconds mein wapas online.</i>"
        ),
        parse_mode=enums.ParseMode.HTML,
    )
    LOGGER.info("Reboot requested by %s", user.id)
    await _do_reboot()
