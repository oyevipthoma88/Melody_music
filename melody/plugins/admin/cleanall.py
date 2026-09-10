"""
🧨 /cleanall + extra housekeeping commands.

REQUESTED: "ek command aisi jo chat ki puri chat clear kar de — bot ki bhi aur
non-bot ki bhi, kitni bhi old chat ho".

HOW IT WORKS (and why it works on arbitrarily old messages):
Telegram's `get_chat_history` is paginated, heavily rate-limited and not even
available to bot accounts, so walking history is hopeless for a full wipe.
Instead we delete the raw message-id RANGE (1 … current id) in chunks of 100 —
the same trick /purge already uses. A supergroup admin with delete rights can
delete ANY message regardless of age or author, so a single pass removes the
bot's messages, members' messages and service messages alike.

Only the group CREATOR, the bot owner and sudo users can run it — it is
irreversible, so a normal admin is refused and every run needs a button
confirmation first.

Extra commands added here (they were missing from the suite):
    /cleanall  — wipe the entire chat (owner/sudo only, confirm required)
    /purgeme   — delete your own recent messages in this chat
    /kickme    — leave the group politely
    /zombies   — find & remove deleted accounts
"""
from utils.buttons import ikb  # premium-emoji + styled buttons (safe on every fork)
import asyncio
import time

from pyrogram import Client, enums, filters
from pyrogram.errors import ChatAdminRequired, FloodWait, MessageDeleteForbidden
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)


from melody import bot
from melody.config import Config
from melody.logging import log_activity
from utils.admin_tools import auto_delete, card, close_kb, is_admin, is_group_owner, mention
from utils.decorators import error_handler
from utils.tasks import spawn

# chat_id -> (user_id, upto_message_id, expires_at) for a pending confirmation
_pending: dict = {}
_CONFIRM_TTL = 120
_running: set = set()


async def _is_owner_or_sudo(client: Client, message: Message) -> bool:
    if not message.from_user:
        return False
    uid = message.from_user.id
    if uid == Config.OWNER_ID or uid in (getattr(Config, "SUDO_USERS", None) or []):
        return True
    try:
        from utils.gc_db import is_sudo

        if await is_sudo(uid):
            return True
    except Exception:
        pass
    return await is_group_owner(client, message.chat.id, uid)


async def _delete_chunk(client: Client, chat_id: int, ids: list[int], depth: int = 0) -> int:
    """Delete a list of ids, splitting on failure so ONE bad id can't skip 99 good ones."""
    if not ids:
        return 0
    for _attempt in range(3):
        try:
            removed = await client.delete_messages(chat_id, ids)
            # Some forks return a bool / None instead of a count.
            if isinstance(removed, bool) or removed is None:
                return len(ids) if removed is not False else 0
            if isinstance(removed, int):
                return removed or len(ids)
            return len(ids)
        except FloodWait as fw:
            await asyncio.sleep(int(getattr(fw, "value", getattr(fw, "x", 2))) + 1)
        except (MessageDeleteForbidden, ChatAdminRequired):
            return 0
        except Exception:
            break
    if len(ids) == 1 or depth >= 6:
        return 0
    mid = len(ids) // 2
    left = await _delete_chunk(client, chat_id, ids[:mid], depth + 1)
    right = await _delete_chunk(client, chat_id, ids[mid:], depth + 1)
    return left + right


async def _wipe(client: Client, chat_id: int, upto: int, status: Message | None):
    """Wipe ids 1..upto.

    Deletes the raw id range so EVERYTHING goes: old messages, members' texts,
    other bots' messages, forwarded/linked-channel posts, service messages and
    Melody's own cards. Chunks of 100 are fired concurrently (bounded) instead
    of one-at-a-time-with-a-sleep, which is what made the old version crawl on
    big groups; a failing chunk is binary-split so a single undeletable id no
    longer costs the other 99.
    """
    total = max(upto, 1)
    chunks = [list(range(s, min(s + 100, upto + 1))) for s in range(1, upto + 1, 100)]
    deleted = 0
    done_chunks = 0
    sem = asyncio.Semaphore(12)
    stop = asyncio.Event()

    async def worker(chunk: list[int]):
        nonlocal deleted, done_chunks
        async with sem:
            deleted += await _delete_chunk(client, chat_id, chunk)
            done_chunks += 1

    async def reporter():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
                return
            except asyncio.TimeoutError:
                pass
            if not status:
                continue
            pct = int(done_chunks / len(chunks) * 100) if chunks else 100
            try:
                await status.edit_text(
                    card(
                        "Cʟᴇᴀɴɪɴɢ Cʜᴀᴛ",
                        f"🧹 <b>Pʀᴏɢʀᴇss :</b> <code>{pct}%</code>\n"
                        f"🗑️ <b>Dᴇʟᴇᴛᴇᴅ :</b> <code>{deleted}</code> messages\n"
                        f"📦 <b>Rᴀɴɢᴇ :</b> <code>1 - {total}</code>",
                    ),
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                pass

    report_task = asyncio.create_task(reporter())
    try:
        await asyncio.gather(*(worker(c) for c in chunks))
    finally:
        stop.set()
        report_task.cancel()
    return deleted



@bot.on_message(filters.command(["cleanall", "clearchat", "nuke"]) & filters.group)
@error_handler
async def cleanall_cmd(client: Client, message: Message):
    if not await _is_owner_or_sudo(client, message):
        m = await message.reply(
            card(
                "Aᴄᴄᴇss Dᴇɴɪᴇᴅ",
                "⚠️ <b>/cleanall sirf ɢʀᴏᴜᴘ ᴏᴡɴᴇʀ ᴀᴜʀ sᴜᴅᴏ ᴋᴇ ʟɪʏᴇ ʜᴀɪ.</b>\n\n"
                "<i>Ye poori chat delete kar deta hai — wapas nahi aayegi.</i>",
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return await auto_delete(m, 30)

    if message.chat.id in _running:
        return await message.reply("🧹 <i>Pehle wali cleaning chal rahi hai…</i>", parse_mode=enums.ParseMode.HTML)

    _pending[message.chat.id] = (message.from_user.id, message.id, time.time() + _CONFIRM_TTL)
    await client.send_message(
        message.chat.id,
        card(
            "Cʟᴇᴀɴ Eɴᴛɪʀᴇ Cʜᴀᴛ ?",
            "🧨 <b>Ye chat ki <u>saari</u> messages delete kar dega</b> — bot ki bhi, "
            "members ki bhi, chahe kitni purani ho.\n\n"
            "⚠️ <b>Ye undo nahi ho sakta.</b>",
            footer="confirm within 2 minutes",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    ikb("🧹 Yᴇs, Cʟᴇᴀɴ Aʟʟ", callback_data="cleanall_go"),
                    ikb("✖️ Cᴀɴᴄᴇʟ", callback_data="cleanall_no"),
                ]
            ]
        ),
    )


@bot.on_callback_query(filters.regex(r"^cleanall_(go|no)$"))
@error_handler
async def cleanall_cb(client: Client, query: CallbackQuery):
    chat_id = query.message.chat.id
    pending = _pending.get(chat_id)
    if not pending or pending[2] < time.time():
        _pending.pop(chat_id, None)
        return await query.answer("Confirmation expire ho gaya — /cleanall dobara chalao.", show_alert=True)
    if query.from_user.id != pending[0]:
        return await query.answer("Ye confirmation tumhare liye nahi hai.", show_alert=True)

    if query.data == "cleanall_no":
        _pending.pop(chat_id, None)
        await query.answer("Cancelled")
        return await query.message.edit_text(
            card("Cᴀɴᴄᴇʟʟᴇᴅ", "✖️ <b>Kuch delete nahi kiya.</b>"),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=close_kb(),
        )

    _pending.pop(chat_id, None)
    if chat_id in _running:
        return await query.answer("Already running…", show_alert=True)
    _running.add(chat_id)
    await query.answer("Cleaning…")

    upto = query.message.id
    status = query.message
    try:
        await status.edit_text(
            card("Cʟᴇᴀɴɪɴɢ Cʜᴀᴛ", "🧹 <b>Sᴛᴀʀᴛɪɴɢ…</b>"),
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass

    try:
        deleted = await _wipe(client, chat_id, upto, status)
    finally:
        _running.discard(chat_id)

    # The status card itself is inside the wiped range, so send a fresh one.
    done = await client.send_message(
        chat_id,
        card(
            "Cʜᴀᴛ Cʟᴇᴀɴᴇᴅ",
            f"🧹 <b>Dᴇʟᴇᴛᴇᴅ :</b> <code>{deleted}</code> messages\n"
            f"👮 <b>Bʏ :</b> {mention(query.from_user)}",
        ),
        parse_mode=enums.ParseMode.HTML,
    )
    await auto_delete(done, 20)
    spawn(
        log_activity(f"#cleanall #admin\n🧨 <code>{deleted}</code> msgs wiped in <code>{chat_id}</code>")
    )


@bot.on_message(filters.command("purgeme") & filters.group)
@error_handler
async def purgeme_cmd(client: Client, message: Message):
    """Delete your own recent messages (default 50, /purgeme 200 for more)."""
    limit = 50
    if len(message.command) > 1 and message.command[1].isdigit():
        limit = max(1, min(int(message.command[1]), 1000))

    uid = message.from_user.id if message.from_user else 0
    ids = list(range(max(message.id - limit, 1), message.id + 1))
    mine: list[int] = []
    # Batched lookups (200 ids per call) instead of one round-trip per message.
    for start in range(0, len(ids), 200):
        batch = ids[start:start + 200]
        try:
            msgs = await client.get_messages(message.chat.id, batch)
        except FloodWait as fw:
            await asyncio.sleep(int(getattr(fw, "value", getattr(fw, "x", 2))) + 1)
            continue
        except Exception:
            continue
        for msg in msgs or []:
            if msg and not getattr(msg, "empty", False) and msg.from_user and msg.from_user.id == uid:
                mine.append(msg.id)

    deleted = 0
    for start in range(0, len(mine), 100):
        deleted += await _delete_chunk(client, message.chat.id, mine[start:start + 100])

    done = await client.send_message(
        message.chat.id,
        card("Pᴜʀɢᴇᴍᴇ", f"🧹 <b>Tumhaari</b> <code>{deleted}</code> <b>messages delete ho gayi.</b>"),
        parse_mode=enums.ParseMode.HTML,
    )
    await auto_delete(done, 8)


@bot.on_message(filters.command("kickme") & filters.group)
@error_handler
async def kickme_cmd(client: Client, message: Message):
    if not message.from_user:
        return
    if await is_admin(client, message.chat.id, message.from_user.id):
        return await message.reply(
            card("Kɪᴄᴋᴍᴇ", "😌 <b>Admin ko kaise nikaalu?</b> Pehle demote karwao."),
            parse_mode=enums.ParseMode.HTML,
        )
    await message.reply(
        card("Bʏᴇ 👋", f"🚪 <b>{mention(message.from_user)} apni marzi se ja rahe hain.</b>"),
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        await client.ban_chat_member(message.chat.id, message.from_user.id)
        await asyncio.sleep(1)
        await client.unban_chat_member(message.chat.id, message.from_user.id)
    except Exception:
        await message.reply("⚠️ <i>Nikaal nahi paaya — mujhe ban rights chahiye.</i>", parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command("zombies") & filters.group)
@error_handler
async def zombies_cmd(client: Client, message: Message):
    """Count deleted accounts; /zombies clean removes them."""
    clean = len(message.command) > 1 and message.command[1].lower() in ("clean", "kick", "remove")
    if clean and not await is_admin(client, message.chat.id, message.from_user.id):
        return await message.reply("⚠️ <i>Sirf admins zombies clean kar sakte hain.</i>", parse_mode=enums.ParseMode.HTML)

    status = await message.reply("🧟 <i>Scanning…</i>", parse_mode=enums.ParseMode.HTML)
    zombies, removed = 0, 0
    try:
        async for member in client.get_chat_members(message.chat.id):
            if not member.user.is_deleted:
                continue
            zombies += 1
            if clean:
                try:
                    await client.ban_chat_member(message.chat.id, member.user.id)
                    removed += 1
                except Exception:
                    pass
    except Exception:
        return await status.edit_text("⚠️ <i>Member list nahi mili — mujhe admin banao.</i>", parse_mode=enums.ParseMode.HTML)

    body = f"🧟 <b>Zᴏᴍʙɪᴇs ғᴏᴜɴᴅ :</b> <code>{zombies}</code>"
    if clean:
        body += f"\n🧹 <b>Rᴇᴍᴏᴠᴇᴅ :</b> <code>{removed}</code>"
    elif zombies:
        body += "\n\n<i>Hataane ke liye</i> <code>/zombies clean</code>"
    await status.edit_text(card("Zᴏᴍʙɪᴇ Sᴄᴀɴ", body), parse_mode=enums.ParseMode.HTML, reply_markup=close_kb())


# ─────────────────────────────────────────────────────────────────────────────
#  /clean — the missing "junk cleaner"
# ─────────────────────────────────────────────────────────────────────────────
#
# BUG ("clean cmnd work nahi krti hai"): there simply was NO /clean handler in
# the whole codebase — only /cleanall (full nuke), /cleanwelcome, and
# /cleanservice. So typing /clean did nothing at all.
#
# Proper logic, three modes:
#     /clean            → last 200 messages me se bots ki messages + commands
#     /clean 500        → same, bigger window (max 1000)
#     /clean bots|cmd|service|all [n]
#
# • Group admins with delete rights (or owner/sudo) only.
# • Ids are inspected in batches of 200 and deleted in chunks of 100 through
#   the same FloodWait-safe, binary-splitting `_delete_chunk()` used by
#   /cleanall, so one undeletable message never aborts the run.
# • The /clean command itself and the result card are cleaned up too.

_CLEAN_MODES = {
    "bots": "bots",
    "bot": "bots",
    "cmd": "cmd",
    "cmds": "cmd",
    "commands": "cmd",
    "service": "service",
    "svc": "service",
    "all": "all",
    "junk": "junk",
}


def _is_command_msg(msg) -> bool:
    text = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
    return bool(text) and text[0] in "/!." and len(text) > 1


def _is_service_msg(msg) -> bool:
    for attr in (
        "new_chat_members", "left_chat_member", "new_chat_title",
        "new_chat_photo", "delete_chat_photo", "pinned_message",
        "group_chat_created", "supergroup_chat_created", "video_chat_started",
        "video_chat_ended", "video_chat_members_invited",
    ):
        if getattr(msg, attr, None):
            return True
    return bool(getattr(msg, "service", None))


def _matches_mode(msg, mode: str) -> bool:
    from_user = getattr(msg, "from_user", None)
    is_bot = bool(from_user and getattr(from_user, "is_bot", False))
    if mode == "all":
        return True
    if mode == "bots":
        return is_bot
    if mode == "cmd":
        return _is_command_msg(msg)
    if mode == "service":
        return _is_service_msg(msg)
    # "junk" (default): every bot's own message + every command + service msgs
    return is_bot or _is_command_msg(msg) or _is_service_msg(msg)


@bot.on_message(filters.command(["clean", "cleanchat", "cleanjunk"]) & filters.group)
@error_handler
async def clean_cmd(client: Client, message: Message):
    if not message.from_user or not (
        await is_admin(client, message.chat.id, message.from_user.id)
        or await _is_owner_or_sudo(client, message)
    ):
        m = await message.reply(
            card("Cʟᴇᴀɴ", "🔒 <b>Sirf ɢʀᴏᴜᴘ ᴀᴅᴍɪɴs /clean ᴄʜᴀʟᴀ sᴀᴋᴛᴇ ʜᴀɪɴ.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return await auto_delete(m, 15)

    args = [a.lower() for a in message.command[1:]]
    mode = "junk"
    limit = 200
    for arg in args:
        if arg in _CLEAN_MODES:
            mode = _CLEAN_MODES[arg]
        elif arg.isdigit():
            limit = max(10, min(int(arg), 1000))

    if message.chat.id in _running:
        m = await message.reply(
            card("Cʟᴇᴀɴ", "🧹 <i>Pehle wali cleaning chal rahi hai…</i>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return await auto_delete(m, 10)

    _running.add(message.chat.id)
    status = await message.reply(
        card("Cʟᴇᴀɴɪɴɢ", f"🧹 <b>Sᴄᴀɴɴɪɴɢ ʟᴀsᴛ</b> <code>{limit}</code> <b>messages…</b>"),
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        ids = list(range(max(message.id - limit, 1), message.id + 1))
        targets: list[int] = [message.id]  # the /clean command itself
        scanned = 0

        for start in range(0, len(ids), 200):
            batch = ids[start:start + 200]
            try:
                msgs = await client.get_messages(message.chat.id, batch)
            except FloodWait as fw:
                await asyncio.sleep(int(getattr(fw, "value", getattr(fw, "x", 2))) + 1)
                try:
                    msgs = await client.get_messages(message.chat.id, batch)
                except Exception:
                    continue
            except Exception:
                continue
            for msg in msgs or []:
                if not msg or getattr(msg, "empty", False):
                    continue
                if msg.id in (status.id, message.id):
                    continue
                scanned += 1
                if _matches_mode(msg, mode):
                    targets.append(msg.id)

        deleted = 0
        for start in range(0, len(targets), 100):
            deleted += await _delete_chunk(client, message.chat.id, targets[start:start + 100])
    finally:
        _running.discard(message.chat.id)

    label = {
        "junk": "bots + commands + service msgs",
        "bots": "bots ki messages",
        "cmd": "commands",
        "service": "service messages",
        "all": "sab messages",
    }[mode]

    try:
        await status.delete()
    except Exception:
        pass

    done = await client.send_message(
        message.chat.id,
        card(
            "Cʜᴀᴛ Cʟᴇᴀɴᴇᴅ",
            f"🧹 <b>Dᴇʟᴇᴛᴇᴅ :</b> <code>{deleted}</code>\n"
            f"🔎 <b>Sᴄᴀɴɴᴇᴅ :</b> <code>{scanned}</code> (last <code>{limit}</code>)\n"
            f"🧾 <b>Mᴏᴅᴇ :</b> {label}\n"
            f"👮 <b>Bʏ :</b> {mention(message.from_user)}",
            footer="/clean bots | cmd | service | all [count]",
        ),
        parse_mode=enums.ParseMode.HTML,
    )
    await auto_delete(done, 15)
    spawn(
        log_activity(f"#clean #admin\n🧹 <code>{deleted}</code> msgs ({mode}) in <code>{message.chat.id}</code>")
    )
