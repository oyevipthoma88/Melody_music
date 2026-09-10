"""
🚪 Bulk join-request handling — /rapproveall, /rdeclineall, /rpending.

REQUESTED
---------
"/rapproveall /requestapproveall … gc me jitni join request hai wo gc me add ya
 decline hogi, aur koi random admin koi join request reject kare to notify ho."

WHY THE OLD BEHAVIOUR WAS BROKEN
--------------------------------
There was no bulk command at all — only the per-request ACCEPT/REJECT buttons
from `join_requests.py`, so a group sitting on 400 pending requests could not be
cleared. And Telegram never pushes an update when *another* admin resolves a
request from the native UI, so those requests silently stayed in Melody's
pending map forever with a dead button attached.

HOW IT IS FIXED
---------------
1. Bulk commands use Telegram's own bulk RPC when the installed Pyrogram fork
   exposes it (`approve_all_chat_join_requests` / `decline_all_…`) — one call,
   instant, no flood. If the fork lacks it we page through
   `get_chat_join_requests()` and resolve them one by one with flood-wait
   handling and a live progress card.
2. `melody/plugins/admin/join_requests.py` runs a reconciler that detects
   externally-resolved requests and posts the "kisne accept/reject kiya" card
   in the group (see `reconcile_pending` there).

Commands (group admins / sudo)
    /rapproveall  /requestapproveall  /approveallrequests   → accept everything
    /rdeclineall  /requestdeclineall  /declineallrequests   → decline everything
    /rpending     /joinrequests                             → how many pending
"""
from __future__ import annotations

import asyncio
import html
import time

from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from melody import bot
from melody.logging import LOGGER, log_activity
from utils.admin_tools import auto_delete, card, close_kb, is_admin, mention
from utils.buttons import ikb
from utils.decorators import cb_admin_or_auth, error_handler
from utils.tasks import spawn

_running: set[int] = set()
# chat_id -> (user_id, mode, expires_at)
_pending_confirm: dict[int, tuple[int, str, float]] = {}
_CONFIRM_TTL = 90


async def _gate(client: Client, message: Message) -> bool:
    if not message.from_user:
        return False
    if await is_admin(client, message.chat.id, message.from_user.id):
        return True
    m = await message.reply(
        card("Aᴄᴄᴇss Dᴇɴɪᴇᴅ", "⚠️ <b>Sɪʀғ ᴀᴅᴍɪɴs</b> join requests handle kar sakte hain."),
        parse_mode=enums.ParseMode.HTML,
    )
    await auto_delete(m, 20)
    return False


async def _list_pending(client: Client, chat_id: int, limit: int = 0) -> list:
    """Return the pending join requests (best-effort, never raises)."""
    out: list = []
    try:
        async for req in client.get_chat_join_requests(chat_id):
            out.append(req)
            if limit and len(out) >= limit:
                break
    except AttributeError:
        LOGGER.warning("get_chat_join_requests missing on this Pyrogram build")
    except Exception as exc:
        LOGGER.warning("get_chat_join_requests failed in %s: %s", chat_id, exc)
    return out


async def _bulk_native(client: Client, chat_id: int, approve: bool) -> bool:
    """Try Telegram's single-shot bulk RPC. True when it succeeded."""
    name = "approve_all_chat_join_requests" if approve else "decline_all_chat_join_requests"
    func = getattr(client, name, None)
    if func is None:
        return False
    try:
        await func(chat_id)
        return True
    except FloodWait as exc:
        await asyncio.sleep(int(getattr(exc, "value", 5)) + 1)
        try:
            await func(chat_id)
            return True
        except Exception:
            return False
    except Exception as exc:
        LOGGER.info("%s unavailable/failed in %s: %s", name, chat_id, exc)
        return False


async def _bulk_loop(client: Client, chat_id: int, approve: bool, status: Message | None):
    """Fallback: resolve requests one by one with progress + flood handling."""
    done = failed = 0
    last_edit = 0.0
    while True:
        batch = await _list_pending(client, chat_id, limit=200)
        if not batch:
            break
        for req in batch:
            user = getattr(req, "from_user", None)
            if not user:
                continue
            call = (client.approve_chat_join_request if approve
                    else client.decline_chat_join_request)
            for attempt in range(3):
                try:
                    await call(chat_id, user.id)
                    done += 1
                    break
                except FloodWait as exc:
                    await asyncio.sleep(int(getattr(exc, "value", 3)) + 1)
                except Exception:
                    if attempt == 2:
                        failed += 1
                    else:
                        await asyncio.sleep(0.5)
            if status and time.time() - last_edit > 4:
                last_edit = time.time()
                try:
                    await status.edit_text(
                        card(
                            "Jᴏɪɴ Rᴇϙᴜᴇsᴛs",
                            f"⚙️ <b>{'Aᴘᴘʀᴏᴠɪɴɢ' if approve else 'Dᴇᴄʟɪɴɪɴɢ'}…</b>\n\n"
                            f"✅ <b>Dᴏɴᴇ :</b> <code>{done}</code>\n"
                            f"⚠️ <b>Fᴀɪʟᴇᴅ :</b> <code>{failed}</code>",
                        ),
                        parse_mode=enums.ParseMode.HTML,
                    )
                except Exception:
                    pass
        if len(batch) < 200:
            break
    return done, failed


async def _run_bulk(client: Client, chat_id: int, actor, approve: bool, status: Message | None):
    if chat_id in _running:
        return None
    _running.add(chat_id)
    try:
        before = len(await _list_pending(client, chat_id, limit=0))
        if await _bulk_native(client, chat_id, approve):
            after = len(await _list_pending(client, chat_id, limit=0))
            done, failed = max(before - after, 0), after
        else:
            done, failed = await _bulk_loop(client, chat_id, approve, status)

        text = card(
            "Jᴏɪɴ Rᴇϙᴜᴇsᴛs " + ("Aᴘᴘʀᴏᴠᴇᴅ" if approve else "Dᴇᴄʟɪɴᴇᴅ"),
            f"{'✅' if approve else '❌'} <b>{'Add' if approve else 'Decline'} ho gaye :</b> "
            f"<code>{done}</code>\n"
            f"⚠️ <b>Bᴀᴄʜᴇ / ғᴀɪʟᴇᴅ :</b> <code>{failed}</code>\n"
            f"👮 <b>Bʏ :</b> {mention(actor)}",
            "Melody join-request manager 🚪",
        )
        try:
            if status:
                await status.edit_text(text, parse_mode=enums.ParseMode.HTML,
                                       reply_markup=close_kb())
            else:
                await client.send_message(chat_id, text, parse_mode=enums.ParseMode.HTML,
                                          reply_markup=close_kb())
        except Exception:
            pass

        spawn(log_activity(
            f"#joinrequest #bulk {'#approved' if approve else '#declined'}\n"
            f"🏠 <code>{chat_id}</code>\n"
            f"📊 <code>{done}</code> done · <code>{failed}</code> left\n"
            f"👮 <code>{actor.id}</code>"
        ))

        # Any pending card tracked by join_requests.py is now stale.
        try:
            from melody.plugins.admin.join_requests import forget_chat_pending

            forget_chat_pending(chat_id)
        except Exception:
            pass
        return done, failed
    finally:
        _running.discard(chat_id)


def _confirm_kb(mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        ikb("✅ Aᴘᴘʀᴏᴠᴇ Aʟʟ" if mode == "ok" else "❌ Dᴇᴄʟɪɴᴇ Aʟʟ",
            callback_data=f"rall_go_{mode}"),
        ikb("✖️ Cᴀɴᴄᴇʟ", callback_data="rall_no"),
    ]])


async def _ask(client: Client, message: Message, approve: bool):
    if not await _gate(client, message):
        return
    pending = await _list_pending(client, message.chat.id, limit=0)
    if not pending:
        return await message.reply(
            card("Jᴏɪɴ Rᴇϙᴜᴇsᴛs", "📭 <b>Koi pending join request nahi hai.</b>"),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=close_kb(),
        )
    mode = "ok" if approve else "no"
    _pending_confirm[message.chat.id] = (message.from_user.id, mode, time.time() + _CONFIRM_TTL)
    preview = "\n".join(
        f"  🔸 <a href=\"tg://user?id={r.from_user.id}\">"
        f"{html.escape(r.from_user.first_name or 'User')}</a>"
        for r in pending[:5] if getattr(r, "from_user", None)
    )
    await message.reply(
        card(
            "Bᴜʟᴋ " + ("Aᴘᴘʀᴏᴠᴇ" if approve else "Dᴇᴄʟɪɴᴇ"),
            f"🚪 <b>Pᴇɴᴅɪɴɢ ʀᴇϙᴜᴇsᴛs :</b> <code>{len(pending)}</code>\n"
            + (f"\n{preview}\n" if preview else "")
            + f"\n{'✅ Sabko group me add kar dun?' if approve else '❌ Sabki request decline kar dun?'}",
            "confirm within 90 seconds",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_confirm_kb(mode),
    )


@bot.on_message(
    filters.command(["rapproveall", "requestapproveall", "approveallrequests",
                     "joinapproveall"]) & filters.group
)
@error_handler
async def rapproveall_cmd(client: Client, message: Message):
    await _ask(client, message, approve=True)


@bot.on_message(
    filters.command(["rdeclineall", "requestdeclineall", "declineallrequests",
                     "rrejectall", "joindeclineall"]) & filters.group
)
@error_handler
async def rdeclineall_cmd(client: Client, message: Message):
    await _ask(client, message, approve=False)


@bot.on_message(filters.command(["rpending", "joinrequests", "pendingrequests"]) & filters.group)
@error_handler
async def rpending_cmd(client: Client, message: Message):
    if not await _gate(client, message):
        return
    pending = await _list_pending(client, message.chat.id, limit=0)
    listing = "\n".join(
        f"  🔸 <a href=\"tg://user?id={r.from_user.id}\">"
        f"{html.escape(r.from_user.first_name or 'User')}</a> "
        f"(<code>{r.from_user.id}</code>)"
        for r in pending[:15] if getattr(r, "from_user", None)
    )
    await message.reply(
        card(
            "Pᴇɴᴅɪɴɢ Jᴏɪɴ Rᴇϙᴜᴇsᴛs",
            f"🚪 <b>Tᴏᴛᴀʟ :</b> <code>{len(pending)}</code>\n"
            + (f"\n{listing}\n" if listing else "\n📭 <i>Kuch bhi pending nahi.</i>\n")
            + "\n<code>/rapproveall</code> · <code>/rdeclineall</code>",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_callback_query(filters.regex(r"^rall_(go_ok|go_no|no)$"))
@error_handler
async def rall_cb(client: Client, cb: CallbackQuery):
    chat_id = cb.message.chat.id
    entry = _pending_confirm.get(chat_id)
    if not entry or entry[2] < time.time():
        _pending_confirm.pop(chat_id, None)
        return await cb.answer("Confirmation expire ho gaya — command dobara chalao.",
                              show_alert=True)
    if cb.from_user.id != entry[0] and not await cb_admin_or_auth(client, cb, chat_id=chat_id):
        return await cb.answer("Ye confirmation tumhare liye nahi hai.", show_alert=True)

    if cb.data == "rall_no":
        _pending_confirm.pop(chat_id, None)
        await cb.answer("Cancelled")
        return await cb.message.edit_text(
            card("Cᴀɴᴄᴇʟʟᴇᴅ", "✖️ <b>Kuch nahi kiya.</b>"),
            parse_mode=enums.ParseMode.HTML, reply_markup=close_kb(),
        )

    _pending_confirm.pop(chat_id, None)
    approve = cb.data.endswith("ok")
    await cb.answer("Chal raha hai…")
    try:
        await cb.message.edit_text(
            card("Jᴏɪɴ Rᴇϙᴜᴇsᴛs", "⚙️ <b>Sᴛᴀʀᴛɪɴɢ…</b>"),
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass
    await _run_bulk(client, chat_id, cb.from_user, approve, cb.message)
