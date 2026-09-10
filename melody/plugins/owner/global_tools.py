"""
🌍 Owner/sudo global tooling that Melody was missing.

Every top music bot (Yukki, AnonXMusic, VIPMusic, TgMusicBot) ships the *list*
and *chat-level* half of the global moderation surface. Melody had /gban and
/botban but no way to audit or undo them, and no chat blacklist at all:

    /gbannedusers  /gbanlist       — who is globally banned
    /blockedusers  /blocklist      — who is bot-banned
    /blacklistchat <id>            — bot refuses to work in that chat
    /whitelistchat <id>            — undo it
    /blacklistedchats              — list them
    /speedtest                     — host network speed
    /sysinfo                       — CPU / RAM / disk / uptime
    /logger on|off                 — bot-wide activity logging switch

The blacklist is ENFORCED here too, in a group=-8 pre-handler that stops
propagation before any plugin sees the message — a list nobody enforces is
just decoration.
"""
import asyncio
import html
import platform
import shutil
import time

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from melody import bot
from melody.config import Config
from utils.database import (
    blacklist_chat,
    get_banned_users,
    get_blacklisted_chats,
    get_gbanned_users,
    get_global,
    is_blacklisted_chat,
    set_global,
    whitelist_chat,
)
from utils.decorators import error_handler, owner_only
from utils.formatters import quote_html

_START_TIME = time.time()

# chat_id -> bool, refreshed on every write. Keeps the hot path (every group
# message) off Mongo; a miss falls through to a single DB read.
_blacklist_cache: dict = {}


async def _chat_blocked(chat_id: int) -> bool:
    cached = _blacklist_cache.get(chat_id)
    if cached is not None:
        return cached
    try:
        blocked = await is_blacklisted_chat(chat_id)
    except Exception:
        return False
    _blacklist_cache[chat_id] = blocked
    return blocked


@bot.on_message(filters.group & ~filters.service, group=-8)
async def _blacklist_guard(client: Client, message: Message):
    """Silently ignore every command from a blacklisted chat."""
    from pyrogram import StopPropagation

    if not message.text and not message.caption:
        return
    if await _chat_blocked(message.chat.id):
        raise StopPropagation


def _target_chat(message: Message) -> "int | None":
    if len(message.command) > 1:
        try:
            return int(message.command[1])
        except ValueError:
            return None
    if message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        return message.chat.id
    return None


# ─── Global ban / block listings ─────────────────────────────────────────────

@bot.on_message(filters.command(["gbannedusers", "gbanlist", "gbanned"]))
@error_handler
@owner_only
async def gbanned_cmd(client: Client, message: Message):
    users = await get_gbanned_users()
    if not users:
        await message.reply(
            quote_html("✅ <b>Koi bhi globally banned nahi hai.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return
    lines = [f"🌍 <b>Globally banned — {len(users)}</b>\n"]
    for i, doc in enumerate(users[:50], 1):
        reason = html.escape(str(doc.get("reason") or "no reason")[:40])
        lines.append(f"🔸 <code>{i}.</code> <code>{doc['user_id']}</code> — <i>{reason}</i>")
    if len(users) > 50:
        lines.append(f"\n<i>…aur {len(users) - 50} aur.</i>")
    await message.reply(quote_html("\n".join(lines)), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command(["blockedusers", "blocklist", "bannedusers"]))
@error_handler
@owner_only
async def blocked_cmd(client: Client, message: Message):
    users = await get_banned_users()
    if not users:
        await message.reply(
            quote_html("✅ <b>Koi bhi bot-banned nahi hai.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return
    lines = [f"🚫 <b>Bot-banned users — {len(users)}</b>\n"]
    for i, doc in enumerate(users[:50], 1):
        reason = html.escape(str(doc.get("reason") or "no reason")[:40])
        lines.append(f"🔸 <code>{i}.</code> <code>{doc['user_id']}</code> — <i>{reason}</i>")
    if len(users) > 50:
        lines.append(f"\n<i>…aur {len(users) - 50} aur.</i>")
    await message.reply(quote_html("\n".join(lines)), parse_mode=enums.ParseMode.HTML)


# ─── Chat blacklist ──────────────────────────────────────────────────────────

@bot.on_message(filters.command(["blacklistchat", "blchat"]))
@error_handler
@owner_only
async def blacklistchat_cmd(client: Client, message: Message):
    chat_id = _target_chat(message)
    if chat_id is None:
        await message.reply(
            quote_html("<b>Usage:</b> <code>/blacklistchat &lt;chat_id&gt;</code>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return
    reason = " ".join(message.command[2:]) if len(message.command) > 2 else ""
    await blacklist_chat(chat_id, reason)
    _blacklist_cache[chat_id] = True
    await message.reply(
        quote_html(f"🚷 <b>Chat blacklisted</b> — <code>{chat_id}</code>\nBot ab wahan kuch nahi karega."),
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        await client.leave_chat(chat_id)
    except Exception:
        pass


@bot.on_message(filters.command(["whitelistchat", "unblacklistchat", "wlchat"]))
@error_handler
@owner_only
async def whitelistchat_cmd(client: Client, message: Message):
    chat_id = _target_chat(message)
    if chat_id is None:
        await message.reply(
            quote_html("<b>Usage:</b> <code>/whitelistchat &lt;chat_id&gt;</code>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return
    await whitelist_chat(chat_id)
    _blacklist_cache[chat_id] = False
    await message.reply(
        quote_html(f"✅ <b>Chat whitelisted</b> — <code>{chat_id}</code>"),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command(["blacklistedchats", "blchats"]))
@error_handler
@owner_only
async def blacklisted_chats_cmd(client: Client, message: Message):
    chats = await get_blacklisted_chats()
    if not chats:
        await message.reply(
            quote_html("✅ <b>Koi chat blacklist me nahi hai.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return
    lines = [f"🚷 <b>Blacklisted chats — {len(chats)}</b>\n"]
    for i, doc in enumerate(chats[:50], 1):
        reason = html.escape(str(doc.get("reason") or "no reason")[:40])
        lines.append(f"🔸 <code>{i}.</code> <code>{doc['chat_id']}</code> — <i>{reason}</i>")
    await message.reply(quote_html("\n".join(lines)), parse_mode=enums.ParseMode.HTML)


# ─── Host diagnostics ────────────────────────────────────────────────────────

def _fmt_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _speedtest_sync() -> dict:
    """Download-only probe using a known CDN file — no speedtest-cli needed
    (that package pulls a heavy dependency and breaks on slim containers)."""
    import time as _t

    import httpx

    url = "https://speed.cloudflare.com/__down?bytes=25000000"
    started = _t.monotonic()
    total = 0
    with httpx.stream("GET", url, timeout=45.0) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(chunk_size=65536):
            total += len(chunk)
            if _t.monotonic() - started > 25:
                break
    elapsed = max(_t.monotonic() - started, 0.001)
    return {"bytes": total, "seconds": elapsed, "mbps": (total * 8) / elapsed / 1_000_000}


@bot.on_message(filters.command(["speedtest", "spt"]))
@error_handler
@owner_only
async def speedtest_cmd(client: Client, message: Message):
    msg = await message.reply(
        quote_html("📡 <b>Speedtest chal raha hai...</b>"), parse_mode=enums.ParseMode.HTML
    )
    try:
        from melody.core.pools import IO_POOL

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(IO_POOL, _speedtest_sync)
    except Exception as exc:
        await msg.edit(
            quote_html(f"❌ <b>Speedtest fail:</b> <code>{html.escape(str(exc)[:120])}</code>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    await msg.edit(
        quote_html(
            "📡 <b>Speedtest</b>\n\n"
            f"🔸 Download : <b>{result['mbps']:.2f} Mbps</b>\n"
            f"🔸 Data     : <code>{_fmt_bytes(result['bytes'])}</code>\n"
            f"🔸 Time     : <code>{result['seconds']:.2f}s</code>"
        ),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command(["sysinfo", "serverstats", "sys"]))
@error_handler
@owner_only
async def sysinfo_cmd(client: Client, message: Message):
    lines = ["🖥 <b>System</b>\n"]
    lines.append(f"🔸 Python : <code>{platform.python_version()}</code>")
    lines.append(f"🔸 OS     : <code>{html.escape(platform.platform()[:50])}</code>")

    try:
        import psutil

        mem = psutil.virtual_memory()
        lines.append(f"🔸 CPU    : <code>{psutil.cpu_percent(interval=0.4)}%</code>")
        lines.append(f"🔸 RAM    : <code>{_fmt_bytes(mem.used)} / {_fmt_bytes(mem.total)}</code>")
    except Exception:
        lines.append("🔸 CPU/RAM: <i>psutil unavailable</i>")

    try:
        usage = shutil.disk_usage("/")
        lines.append(f"🔸 Disk   : <code>{_fmt_bytes(usage.used)} / {_fmt_bytes(usage.total)}</code>")
    except Exception:
        pass

    uptime = int(time.time() - _START_TIME)
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    lines.append(f"🔸 Uptime : <code>{hours}h {minutes}m {seconds}s</code>")
    await message.reply(quote_html("\n".join(lines)), parse_mode=enums.ParseMode.HTML)


# ─── Logger switch ───────────────────────────────────────────────────────────

@bot.on_message(filters.command(["logger", "logging"]))
@error_handler
@owner_only
async def logger_cmd(client: Client, message: Message):
    args = message.command[1:]
    current = await get_global("logger_enabled", True)
    if not args:
        state = "🟢 ON" if current else "🔴 OFF"
        await message.reply(
            quote_html(
                f"🧾 <b>Activity logger:</b> {state}\n"
                f"<code>/logger on</code> · <code>/logger off</code>\n"
                f"Log group: <code>{Config.LOG_GROUP_ID}</code>"
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    want = args[0].lower() in ("on", "enable", "true", "yes", "1")
    await set_global("logger_enabled", want)
    await message.reply(
        quote_html(f"🧾 <b>Activity logger {'ON' if want else 'OFF'}.</b>"),
        parse_mode=enums.ParseMode.HTML,
    )
