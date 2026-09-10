"""
👑 Owner Panel — All owner control callbacks
   HTML blockquote formatting, plain-label buttons (no fake color emoji —
   Bot API buttons can't be colored; see strings/themes.py + strings/webmenu.py)
"""
import asyncio
import html
import importlib
import pkgutil
import sys
import time
import psutil
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, CallbackQuery
from utils.buttons import ikb  # premium-emoji + styled buttons (safe on every fork)
from melody import bot
from melody.config import Config
from utils.decorators import owner_only, error_handler
from utils.database import get_all_chats, get_chats_page, get_chat_count, get_stats
from strings.themes import BLUE, RED, GREEN, btn, fancy
from utils.melody_theme import headline

_start_time = time.time()

PANEL_TEXT = (
    f"<blockquote>👑 <b>{fancy('OWNER CONTROL PANEL')}</b></blockquote>\n\n"
    "Welcome back, <b>Master</b> 🫡\n"
    "<i>Choose an action below:</i>\n\n"
    "<blockquote>"
    "🖼️ <b>Set Start Pic</b> — Change welcome image\n"
    "🎉 <b>Set Welcome GC Pic</b> — Shown when added to a group\n"
    "📡 <b>Active VCs</b> — Live voice chat sessions\n"
    "📢 <b>Broadcast</b> — Message all chats\n"
    "📊 <b>Bot Stats</b> — CPU, RAM, uptime\n"
    "📋 <b>Chat List</b> — All served groups\n"
    "🔧 <b>Maintenance</b> — Toggle mode\n"
    "📚 <b>Commands</b> — Full admin/sudo command vault\n"
    "📣 <b>Promo Reminders</b> — Global promo ON / OFF\n"
    "🧑‍💻 <b>Source Code</b> — Set / remove repository URL\n"
    "🔄 <b>Reload</b> — Hot-reload plugins\n"
    "🔴 <b>Restart</b> — Full bot restart"
    "</blockquote>"
)

PANEL_MARKUP = InlineKeyboardMarkup([
    [
        ikb(btn("🖼️ Set Start Pic", BLUE),  callback_data="owner_setpic_info"),
        ikb(btn("🎉 Welcome GC Pic", BLUE),  callback_data="owner_setwelcomepic_info"),
    ],
    [
        ikb(btn("📡 Active VCs", GREEN),     callback_data="owner_activevc"),
        ikb(btn("📢 Broadcast", BLUE),       callback_data="owner_broadcast_info"),
    ],
    [
        ikb(btn("📊 Bot Stats", BLUE),       callback_data="owner_stats"),
        ikb(btn("📋 Chat List", BLUE),       callback_data="owner_chatlist"),
    ],
    [
        ikb(btn("📚 Commands", GREEN),       callback_data="ownercmd_index"),
        ikb(btn("🔧 Maintenance", RED),      callback_data="owner_maintenance"),
    ],
    [
        ikb(btn("📣 Promo Reminders", GREEN), callback_data="owner_promo"),
        ikb(btn("🧑‍💻 Source Code", BLUE),     callback_data="srccode_main"),
    ],
    [
        ikb(btn("🔄 Reload Plugins", GREEN), callback_data="owner_reload"),
    ],
    [
        ikb(btn("🔁 Restart Bot", RED),      callback_data="owner_restart"),
    ],
])

PANEL_MARKUP_WITH_BACK = InlineKeyboardMarkup([
    [
        ikb(btn("🖼️ Set Start Pic", BLUE),  callback_data="owner_setpic_info"),
        ikb(btn("🎉 Welcome GC Pic", BLUE),  callback_data="owner_setwelcomepic_info"),
    ],
    [
        ikb(btn("📡 Active VCs", GREEN),     callback_data="owner_activevc"),
        ikb(btn("📢 Broadcast", BLUE),       callback_data="owner_broadcast_info"),
    ],
    [
        ikb(btn("📊 Bot Stats", BLUE),       callback_data="owner_stats"),
        ikb(btn("📋 Chat List", BLUE),       callback_data="owner_chatlist"),
    ],
    [
        ikb(btn("📚 Commands", GREEN),       callback_data="ownercmd_index"),
        ikb(btn("🔧 Maintenance", RED),      callback_data="owner_maintenance"),
    ],
    [
        ikb(btn("📣 Promo Reminders", GREEN), callback_data="owner_promo"),
        ikb(btn("🧑‍💻 Source Code", BLUE),     callback_data="srccode_main"),
    ],
    [
        ikb(btn("🔄 Reload Plugins", GREEN), callback_data="owner_reload"),
    ],
    [
        ikb(btn("🔁 Restart Bot", RED),      callback_data="owner_restart"),
    ],
    [
        ikb(btn("✵ BACK ✵", RED),            callback_data="owner_back_home"),
    ],
])


def back_to_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [ikb(btn("✵ BACK ✵", RED), callback_data="owner_panel")],
    ])


# ─── /panel command ───────────────────────────────────────────────────────────

@bot.on_message(filters.command("panel") & filters.private)
@owner_only
@error_handler
async def panel_cmd(client: Client, message: Message):
    await message.reply(
        PANEL_TEXT,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=PANEL_MARKUP,
    )


# ─── Owner panel callback ─────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^owner_panel$"))
@error_handler
async def cb_panel(client: Client, cb: CallbackQuery):
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)
    await cb.message.edit_text(
        PANEL_TEXT,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=PANEL_MARKUP_WITH_BACK,
    )
    await cb.answer()


# ─── Bot Stats ────────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^owner_stats$"))
@error_handler
async def cb_stats(client: Client, cb: CallbackQuery):
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    db_stats  = await get_stats()
    uptime_sec = int(time.time() - _start_time)
    h = uptime_sec // 3600
    m = (uptime_sec % 3600) // 60
    s = uptime_sec % 60

    cpu  = psutil.cpu_percent(interval=0.5)
    ram  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    text = (
        f"{headline('BOT STATISTICS')}\n\n"
        "<blockquote>"
        f"🕒 <b>Uptime:</b> <code>{h}h {m}m {s}s</code>\n\n"
        "💻 <b>System:</b>\n"
        f"  🔸 CPU:  <code>{cpu}%</code>\n"
        f"  🔸 RAM:  <code>{ram.percent}%</code> "
        f"<i>({ram.used // 1024 // 1024}MB / {ram.total // 1024 // 1024}MB)</i>\n"
        f"  🔸 Disk: <code>{disk.percent}%</code> "
        f"<i>({disk.used // 1024 // 1024 // 1024}GB / {disk.total // 1024 // 1024 // 1024}GB)</i>"
        "</blockquote>\n\n"
        "<blockquote>"
        "📈 <b>Database:</b>\n"
        f"  🔸 Total Chats:     <code>{db_stats['chats']}</code>\n"
        f"  🔸 Banned Users:    <code>{db_stats['banned']}</code>\n"
        f"  🔸 Globally Banned: <code>{db_stats['gbanned']}</code>"
        "</blockquote>"
    )
    await cb.message.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=back_to_panel())
    await cb.answer()


# ─── Chat List ────────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^owner_chatlist(?::[0-9]+)?$"))
@error_handler
async def cb_chatlist(client: Client, cb: CallbackQuery):
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    # A callback query expires while Mongo is slow unless Telegram receives an
    # acknowledgement first. The list itself is bounded and time-limited.
    await cb.answer("Loading chat list…")
    try:
        raw_offset = (cb.data or "").split(":", 1)[1]
        offset = max(0, int(raw_offset))
    except (IndexError, TypeError, ValueError):
        offset = 0
    page_size = 25
    try:
        chats = await asyncio.wait_for(
            get_chats_page(limit=page_size, offset=offset), timeout=4.0
        )
        try:
            total = await asyncio.wait_for(get_chat_count(), timeout=2.0)
        except Exception:
            total = len(chats)
    except Exception:
        await cb.message.edit_text(
            f"{headline('Chat List')}\n\n"
            "⚠️ Chat list abhi load nahi ho saki. Dobara button dabao.",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=back_to_panel(),
        )
        return

    if not chats:
        await cb.message.edit_text(
            f"{headline('Chat List')}\n\n"
            "❌ No chats in database yet.",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=back_to_panel(),
        )
        return

    lines = [
        f"<blockquote>📋 <b>Served Chats</b> ({total} total) · "
        f"Page {offset // page_size + 1}</blockquote>\n"
    ]
    for c in chats:
        title = html.escape(c.get("title", "Unknown")[:25])
        lines.append(f"  🔸 <code>{c['chat_id']}</code> — {title}")

    navigation = []
    if offset > 0:
        navigation.append(ikb(btn("◀ Previous", BLUE), callback_data=f"owner_chatlist:{max(0, offset - page_size)}"))
    if offset + len(chats) < total:
        navigation.append(ikb(btn("Next ▶", BLUE), callback_data=f"owner_chatlist:{offset + page_size}"))
    if navigation:
        markup = InlineKeyboardMarkup([navigation, [ikb(btn("✵ BACK ✵", RED), callback_data="owner_panel")]])
    else:
        markup = back_to_panel()

    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=markup,
    )


# ─── Active VCs ───────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^owner_activevc$"))
@error_handler
async def cb_activevc(client: Client, cb: CallbackQuery):
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    from melody.core.call import _active
    active_ids = [cid for cid, active in _active.items() if active]

    if not active_ids:
        await cb.message.edit_text(
            f"{headline('Active Voice Chats')}\n\n"
            "🔇 No active VCs right now.",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=back_to_panel(),
        )
        return await cb.answer()

    all_chats = await get_all_chats()
    chat_map  = {c["chat_id"]: c.get("title", "Unknown") for c in all_chats}

    from melody.core.queue import get_current

    lines = [f"<blockquote>📡 <b>Active Voice Chats</b> ({len(active_ids)} active)</blockquote>\n"]
    for cid in active_ids:
        title   = html.escape(chat_map.get(cid, "Unknown")[:25])
        current = get_current(cid)
        song    = f"<code>{html.escape(current.title[:20])}...</code>" if current else "<i>Unknown</i>"
        lines.append(f"🎵 <b>{title}</b>\n   ID: <code>{cid}</code> | Now: {song}\n")

    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=back_to_panel(),
    )
    await cb.answer()


# ─── Broadcast Info ───────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^owner_broadcast_info$"))
@error_handler
async def cb_broadcast_info(client: Client, cb: CallbackQuery):
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    chats = await get_all_chats()
    await cb.message.edit_text(
        f"{headline('Broadcast')}\n\n"
        f"📊 Total chats: <b>{len(chats)}</b>\n\n"
        "<blockquote>"
        "<b>How to broadcast:</b>\n"
        "1. Close this panel\n"
        "2. Write or forward your message\n"
        "3. Reply to it with <code>/broadcast</code>"
        "</blockquote>",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=back_to_panel(),
    )
    await cb.answer()


# ─── Set Start Pic Info ───────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^owner_setpic_info$"))
@error_handler
async def cb_setpic_info(client: Client, cb: CallbackQuery):
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    await cb.message.edit_text(
        f"{headline('Set Start Picture')}\n\n"
        "<blockquote>"
        "<b>How to use:</b>\n"
        "Send any photo with caption <code>/setpic</code>\n\n"
        "To remove: send <code>/delpic</code>"
        "</blockquote>",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=back_to_panel(),
    )
    await cb.answer()


# ─── Set Welcome (GC) Pic Info ─────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^owner_setwelcomepic_info$"))
@error_handler
async def cb_setwelcomepic_info(client: Client, cb: CallbackQuery):
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    await cb.message.edit_text(
        f"{headline('Set Welcome (GC) Picture')}\n\n"
        "Shown whenever the bot is added to a new group — separate from "
        "the /setpic start picture.\n\n"
        "<blockquote>"
        "<b>How to use:</b>\n"
        "Send any photo with caption <code>/setwelcomepic</code>\n\n"
        "To remove: send <code>/delwelcomepic</code>"
        "</blockquote>",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=back_to_panel(),
    )
    await cb.answer()


# ─── Maintenance Toggle ───────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^owner_maintenance$"))
@error_handler
async def cb_maintenance(client: Client, cb: CallbackQuery):
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    import melody
    current   = getattr(melody, "_maintenance", False)
    new_state = not current
    melody._maintenance = new_state

    status = (
        "🔧 <b>ON</b> — <i>Regular users cannot use the bot.</i>"
        if new_state else
        "✅ <b>OFF</b> — <i>Bot is available for everyone.</i>"
    )
    await cb.message.edit_text(
        f"{headline('Maintenance Mode')}\n\n"
        f"Status: {status}",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [ikb(
                btn("🔧 Toggle Again", RED) if not new_state else btn("✅ Turn Off", GREEN),
                callback_data="owner_maintenance",
            )],
            [ikb(btn("✵ BACK ✵", RED), callback_data="owner_panel")],
        ]),
    )
    await cb.answer(f"Maintenance {'ON ⚠️' if new_state else 'OFF ✅'}", show_alert=True)


# ─── Reload Plugins ───────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^owner_reload$"))
@error_handler
async def cb_reload(client: Client, cb: CallbackQuery):
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    await cb.message.edit_text(
        f"{headline('Reloading plugins...')}\n<i>Please wait...</i>",
        parse_mode=enums.ParseMode.HTML,
    )
    await cb.answer()

    reloaded, failed = [], []
    import melody.plugins as plugins_pkg
    for _finder, name, _ispkg in pkgutil.walk_packages(
        plugins_pkg.__path__, plugins_pkg.__name__ + "."
    ):
        try:
            mod = sys.modules.get(name)
            if mod:
                importlib.reload(mod)
            else:
                importlib.import_module(name)
            reloaded.append(name.split(".")[-1])
        except Exception:
            failed.append(name.split(".")[-1])

    text = (
        f"{headline('Reload Complete')}\n\n"
        f"✅ Reloaded: <code>{len(reloaded)}</code> plugins\n"
    )
    if failed:
        text += f"❌ Failed: <code>{len(failed)}</code> — <code>{'</code>, <code>'.join(failed[:8])}</code>\n"

    await cb.message.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=back_to_panel())


# ─── Restart Bot ──────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^owner_restart$"))
@error_handler
async def cb_restart(client: Client, cb: CallbackQuery):
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    await cb.message.edit_text(
        f"{headline('Restarting Melody...')}\n"
        "<i>Thodi der mein wapas aaunga! 👋</i>",
        parse_mode=enums.ParseMode.HTML,
    )
    await cb.answer("Restarting... 🔁", show_alert=True)
    await asyncio.sleep(1)
    import os
    os.execv(sys.executable, [sys.executable, "-m", "melody"])


# ─── Promo Reminders (owner-only; the user-facing /promo command was removed) ──

async def _promo_card():
    from utils.gc_db import get_global
    state = await get_global("promo", True)
    text = (
        f"{headline('Pʀᴏᴍᴏ Rᴇᴍɪɴᴅᴇʀs')}\n\n"
        "Occasional \"add me to your group\" cards posted in served groups.\n\n"
        f"<blockquote>Sᴛᴀᴛᴜs : {'🟢 <b>ON</b>' if state else '🔴 <b>OFF</b>'}</blockquote>"
    )
    markup = InlineKeyboardMarkup([
        [
            ikb(btn("🟢 Turn ON", GREEN),  callback_data="owner_promo_on"),
            ikb(btn("🔴 Turn OFF", RED),   callback_data="owner_promo_off"),
        ],
        [ikb(btn("✵ BACK ✵", RED), callback_data="owner_panel")],
    ])
    return text, markup


@bot.on_callback_query(filters.regex(r"^owner_promo(_on|_off)?$"))
@error_handler
async def cb_promo(client: Client, cb: CallbackQuery):
    # server-side authorisation — never trust callback_data alone
    if cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("❌ Owner only!", show_alert=True)

    from utils.gc_db import set_global
    action = cb.data
    if action == "owner_promo_on":
        await set_global("promo", True)
    elif action == "owner_promo_off":
        await set_global("promo", False)

    text, markup = await _promo_card()
    try:
        await cb.message.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=markup)
    except Exception:
        pass
    await cb.answer("Updated ✅" if action != "owner_promo" else None)
