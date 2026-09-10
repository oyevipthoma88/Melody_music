"""
💬 Voice-chat (VC) chat log — ONLY real in-call chat.

Telegram delivers voice-chat text as `updateGroupCallMessage`, and only to
accounts sitting inside the call (the assistant, while it streams music).
Ordinary group chat carries no VC marker at all, so it is NEVER logged —
the group-chat mirror was removed on request ("gc chat log system hata de").

Output: a themed card in the group (2 buttons: ON/OFF + more settings) and a
permanent copy in `Config.VC_CHAT_LOG_CHANNEL_ID`. Never `LOG_GROUP_ID`.
"""
import asyncio
import html
import time

from melody.logging import LOGGER

# ─────────────────────────── call-id ↔ chat-id map ──────────────────────────
# InputGroupCall carries only (id, access_hash); the update has no peer, so we
# keep our own map. Populated from three places, cheapest first.
_call_chat: dict = {}          # call_id -> chat_id
_call_input: dict = {}         # call_id -> raw InputGroupCall (for API calls)
_resolve_lock = asyncio.Lock()

FLAG = "vc_chat_log"            # per-chat toggle in gc_db (default ON)
DEL_KEY = "vc_chat_log_del"     # per-chat auto-delete seconds (default 5)
RESUME_KEY = "vc_chat_log_resume"  # epoch when an OFF toggle flips back ON
DEFAULT_DELETE = 5              # seconds — REQUESTED default
AUTO_DELETE = 5                 # seconds — REQUESTED: VC chat card 5s me delete
OFF_DURATION = 30 * 60          # OFF lasts 30 minutes, then auto-ON again
DELETE_CHOICES = (0, 5, 10, 30, 60, 300)   # 0 = never delete
_FLUSH_DELAY = 2.5             # seconds of buffering before one card is sent
_MAX_LINES = 12                # lines per card (older ones spill to next card)

_buffers: dict = {}            # chat_id -> list[str]
_flush_tasks: dict = {}        # chat_id -> asyncio.Task
_seen: dict = {}               # (call_id, msg_id) -> ts   (dedupe)
_SEEN_TTL = 600.0

# STRICT DELIVERY (REQUESTED: "strictly all vc ki chat show").
# A VC message whose call id cannot be mapped to a chat yet is NOT thrown
# away any more — it waits here and is retried, and if it still cannot be
# placed it is written to the VC backup channel so nothing is ever lost.
_pending: list = []            # [ {call, line, tries, ts} ]
_pending_task = None
_PENDING_RETRY = 5.0           # seconds between retries
_PENDING_TRIES = 6             # ~30s of retries before the fallback dump


def remember_call(chat_id: int, call) -> None:
    """Public hook: record the live call object of a chat (used by call.py /
    vc_notify whenever a full-chat lookup already fetched it)."""
    call_id = getattr(call, "id", None)
    if isinstance(call_id, int):
        _call_chat[call_id] = chat_id
        _call_input[call_id] = call


def forget_chat(chat_id: int) -> None:
    for call_id, mapped in list(_call_chat.items()):
        if mapped == chat_id:
            _call_chat.pop(call_id, None)
            _call_input.pop(call_id, None)
    _buffers.pop(chat_id, None)
    task = _flush_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


async def _candidate_chats() -> list:
    """Chats that could own an unknown call id, best guess first."""
    chats: list = []
    try:
        from melody.core import call as call_core

        chats += [cid for cid, active in getattr(call_core, "_active", {}).items() if active]
    except Exception:
        pass
    try:
        from melody.core.vc_notify import _vc_members

        chats += list(_vc_members.keys())
    except Exception:
        pass
    try:
        from melody import assistant

        if assistant is not None:
            async for dialog in assistant.get_dialogs(limit=60):
                cid = getattr(getattr(dialog, "chat", None), "id", None)
                if isinstance(cid, int) and cid < 0:
                    chats.append(cid)
    except Exception as exc:
        LOGGER.debug("vc_chat_log dialog scan failed: %s", exc)

    seen = set()
    ordered = []
    for cid in chats:
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)
    return ordered


async def _chat_of_call(call) -> "int | None":
    """Resolve InputGroupCall -> chat_id, caching every call we learn about.

    STRICT MODE (REQUESTED: "vc ki chat kabhi kabhi show nahi hoti — strictly
    all vc ki chat show"). The old version only scanned a small candidate
    list once; whenever the call id was not in that list the whole message was
    silently dropped. Now we:
      1. use the cache,
      2. ask `vc_notify.call_is_live()` for every watched/known chat, which
         itself calls `remember_call()` and therefore fills the map,
      3. fall back to a full MTProto scan of the candidate chats,
    and every step says in the log exactly what happened.
    """
    call_id = getattr(call, "id", None)
    if not isinstance(call_id, int):
        LOGGER.debug("vc_chat_log: update carried no usable call id (%r)", call)
        return None
    if call_id in _call_chat:
        return _call_chat[call_id]

    async with _resolve_lock:
        if call_id in _call_chat:      # another waiter resolved it
            return _call_chat[call_id]

        from melody import assistant

        if assistant is None:
            LOGGER.warning(
                "vc_chat_log: cannot map call %s — assistant client is None", call_id
            )
            return None

        candidates = await _candidate_chats()
        LOGGER.debug(
            "vc_chat_log: mapping call %s against %d candidate chat(s)",
            call_id, len(candidates),
        )

        # Step 1 — cheap: let vc_notify refresh the live call of watched chats.
        try:
            from melody.core.vc_notify import call_is_live

            for chat_id in candidates[:25]:
                try:
                    await call_is_live(chat_id)
                except Exception:
                    continue
                if call_id in _call_chat:
                    LOGGER.debug(
                        "vc_chat_log: call %s mapped to chat %s (live-check)",
                        call_id, _call_chat[call_id],
                    )
                    return _call_chat[call_id]
        except Exception as exc:
            LOGGER.debug("vc_chat_log: live-check pass failed: %s", exc)

        # Step 2 — full lookup per candidate chat.
        from pyrogram.raw import functions, types as raw

        for chat_id in candidates:
            try:
                peer = await assistant.resolve_peer(chat_id)
                if isinstance(peer, raw.InputPeerChannel):
                    full = await assistant.invoke(
                        functions.channels.GetFullChannel(channel=peer)
                    )
                else:
                    full = await assistant.invoke(
                        functions.messages.GetFullChat(
                            chat_id=getattr(peer, "chat_id", chat_id)
                        )
                    )
            except Exception as exc:
                LOGGER.debug(
                    "vc_chat_log: full-chat lookup failed for %s (%s: %s)",
                    chat_id, type(exc).__name__, exc,
                )
                continue
            live = getattr(getattr(full, "full_chat", None), "call", None)
            live_id = getattr(live, "id", None)
            if isinstance(live_id, int):
                _call_chat[live_id] = chat_id
                _call_input[live_id] = live
            if live_id == call_id:
                LOGGER.debug(
                    "vc_chat_log: call %s mapped to chat %s (full-chat scan)",
                    call_id, chat_id,
                )
                return chat_id

        LOGGER.warning(
            "vc_chat_log: could not map group call %s to a chat yet — "
            "message queued for retry (%d candidates tried)",
            call_id, len(candidates),
        )
        return None


# ───────────────────────────────── helpers ──────────────────────────────────
def _peer_id(peer) -> "int | None":
    for attr, sign in (("user_id", 1), ("chat_id", -1), ("channel_id", -1)):
        value = getattr(peer, attr, None)
        if isinstance(value, int):
            if sign == 1:
                return value
            return int(f"-100{value}") if attr == "channel_id" else -value
    return None


async def _name_of(user_id: int) -> str:
    from melody import bot, assistant

    for client in (assistant, bot):
        if client is None:
            continue
        try:
            user = await client.get_users(user_id)
            return str(
                getattr(user, "first_name", None) or getattr(user, "title", None) or user_id
            )
        except Exception:
            continue
    return str(user_id)


def _prune_seen() -> None:
    now = time.time()
    for key, stamp in list(_seen.items()):
        if now - stamp > _SEEN_TTL:
            _seen.pop(key, None)


async def log_enabled(chat_id: int) -> bool:
    """Is the VC chat log ON for this chat?

    A manual OFF is deliberately temporary (REQUESTED): 30 minutes after the
    toggle the log turns itself back ON, so nobody can silence the backup
    forever by accident.
    """
    try:
        from utils.gc_db import get_setting_flag, set_setting_flag

        state = await get_setting_flag(chat_id, FLAG, True)
        if state:
            return True
        resume = float(await get_setting_flag(chat_id, RESUME_KEY, 0) or 0)
        if resume and time.time() >= resume:
            await set_setting_flag(chat_id, FLAG, True)
            await set_setting_flag(chat_id, RESUME_KEY, 0)
            LOGGER.debug("vc_chat_log auto re-enabled in %s after 30m", chat_id)
            return True
        return False
    except Exception:
        return True


# Backwards-compatible alias used inside this module.
_enabled = log_enabled


async def set_enabled(chat_id: int, state: bool) -> float:
    """Turn the log ON/OFF. Returns the epoch of the automatic re-enable."""
    from utils.gc_db import set_setting_flag

    resume = 0.0 if state else time.time() + OFF_DURATION
    await set_setting_flag(chat_id, FLAG, bool(state))
    await set_setting_flag(chat_id, RESUME_KEY, resume)
    if state:
        try:
            from melody.core import vc_listener

            await vc_listener.ensure(chat_id)   # poll only — never joins the VC
        except Exception:
            pass
    return resume


def log_keyboard(chat_id: int, state: bool):
    """Buttons on every VC chat card (REQUESTED: sirf 2 buttons, ZERO time).

    Row 1 : 🟢/🔴 VC chat log ON/OFF (OFF auto-returns after 30 min)
    Row 2 : ✵ Close ✵

    The old "⚙ More settings" panel only held auto-delete timers, and every
    kind of time was removed from the VC chat card on request — so the panel
    itself is gone too.
    """
    from pyrogram.types import InlineKeyboardMarkup

    # Use the shared factory so these buttons also get premium emoji icons and
    # colour styles like the rest of the bot (raw InlineKeyboardButton skipped
    # both).
    from utils.buttons import ikb, STYLE_DANGER, STYLE_SUCCESS

    toggle = "🔴 Vᴄ Cʜᴀᴛ Oғғ" if state else "🟢 Vᴄ Cʜᴀᴛ Oɴ"
    rows = [
        [ikb(toggle, style=STYLE_DANGER if state else STYLE_SUCCESS,
             callback_data=f"vclog:toggle:{chat_id}")],
        [ikb("✵ CLOSE ✵", style=STYLE_DANGER, callback_data="gc_close")],
    ]
    return InlineKeyboardMarkup(rows)



# ─────────────────────────────── the log card ───────────────────────────────
# chat_id set where posting the VC-chat card is permanently impossible (bot not
# a member / kicked / no send rights). Populated on the first permanent failure
# so the same warning is not repeated on every flush.
_group_post_blocked: set = set()


def unblock_group_post(chat_id: int) -> None:
    """Allow group posting again (e.g. after the bot is re-added)."""
    _group_post_blocked.discard(chat_id)


async def _flush(chat_id: int) -> None:
    try:
        await asyncio.sleep(_FLUSH_DELAY)
    except asyncio.CancelledError:
        return
    _flush_tasks.pop(chat_id, None)
    lines = _buffers.pop(chat_id, [])
    if not lines:
        return

    from melody import bot
    from melody.config import Config

    head_lines, spill = lines[:_MAX_LINES], lines[_MAX_LINES:]
    if spill:
        _buffers.setdefault(chat_id, []).extend(spill)
        _schedule(chat_id)
        LOGGER.debug(
            "vc_chat_log[%s]: %d line(s) spilled to the next card", chat_id, len(spill)
        )

    body = "\n".join(head_lines)

    title = str(chat_id)
    try:
        if bot is not None:
            chat = await bot.get_chat(chat_id)
            title = html.escape(str(getattr(chat, "title", chat_id) or chat_id))
    except Exception as exc:
        LOGGER.debug("vc_chat_log[%s]: title lookup failed: %s", chat_id, exc)

    from melody.core.vc_theme import FLAGS, vc_card

    state = await log_enabled(chat_id)
    # REQUESTED: "vc chat card se all typ ka time hata de" — no clock, no
    # countdown, no auto-delete window. Only the flags ribbon stays.
    group_text = vc_card(
        "Vc Live Chat",
        body,
        footer=f"{FLAGS}",
        tags="#VᴄCʜᴀᴛ 💬",
    )

    if bot is not None and chat_id not in _group_post_blocked:
        try:
            from pyrogram import enums
            from pyrogram.types import LinkPreviewOptions

            sent = await bot.send_message(
                chat_id,
                group_text,
                parse_mode=enums.ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=log_keyboard(chat_id, state),
            )
            # REQUESTED: "vc chat auto delete kr 5sec me" — the card shows no
            # timer text anywhere, it simply disappears after AUTO_DELETE
            # seconds. The channel copy below stays permanent.
            from utils.admin_tools import auto_delete

            await auto_delete(sent, AUTO_DELETE)
            LOGGER.debug(
                "vc_chat_log[%s] «%s»: card posted with %d line(s) "
                "(msg_id=%s, auto-delete in %ss, channel copy kept)",
                chat_id, title, len(head_lines),
                getattr(sent, "id", "?"), AUTO_DELETE,
            )
        except Exception as exc:
            # LOG-SPAM FIX (Heroku logs repeated the same CHANNEL_INVALID
            # warning for every single flush): a permanent delivery failure
            # (bot not in the chat / kicked / chat deleted / no send rights)
            # can never fix itself by retrying a few seconds later. Remember
            # it once and keep only the permanent channel backup for that
            # chat, instead of warning on every card.
            text = f"{type(exc).__name__}: {exc}".lower()
            permanent = any(
                marker in text
                for marker in (
                    "channel_invalid", "channel_private", "peer_id_invalid",
                    "chat_write_forbidden", "chat_admin_required",
                    "user_banned_in_channel", "chat not found", "forbidden",
                    "chatwriteforbidden", "channelinvalid", "channelprivate",
                    "peeridinvalid", "userbannedinchannel",
                )
            )
            if permanent:
                _group_post_blocked.add(chat_id)
                LOGGER.debug(
                    "vc_chat_log[%s] «%s»: group posting disabled (%s: %s) — "
                    "keeping the channel backup only",
                    chat_id, title, type(exc).__name__, exc,
                )
            else:
                LOGGER.warning(
                    "vc_chat_log[%s] «%s»: group post FAILED (%s: %s) — "
                    "check that the bot is in the chat and can send messages",
                    chat_id, title, type(exc).__name__, exc,
                )

    # PERMANENT copy — dedicated VC-chat channel ONLY.
    # REQUESTED: the normal LOG_GROUP_ID must never receive VC chat text.
    channel_id = getattr(Config, "VC_CHAT_LOG_CHANNEL_ID", 0)
    if bot is not None and channel_id:
        try:
            from pyrogram import enums
            from pyrogram.types import LinkPreviewOptions

            await bot.send_message(
                channel_id,
                vc_card(
                    "Vc Chat Backup",
                    f"🏠 <b>{title}</b> (<code>{chat_id}</code>)\n\n{body}",
                    footer=f"{FLAGS}",
                    tags="#VᴄCʜᴀᴛBᴀᴄᴋᴜᴘ 🗂",
                ),
                parse_mode=enums.ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            LOGGER.debug(
                "vc_chat_log[%s]: backup copy stored in channel %s", chat_id, channel_id
            )
        except Exception as exc:
            LOGGER.warning(
                "vc_chat_log[%s]: backup channel %s post FAILED (%s: %s)",
                chat_id, channel_id, type(exc).__name__, exc,
            )
    elif bot is not None and not channel_id:
        LOGGER.debug(
            "vc_chat_log[%s]: VC_CHAT_LOG_CHANNEL_ID not set — no permanent backup",
            chat_id,
        )


async def _pending_worker() -> None:
    """Retry every VC line whose call could not be mapped to a chat yet."""
    global _pending_task
    try:
        while _pending:
            await asyncio.sleep(_PENDING_RETRY)
            for item in list(_pending):
                item["tries"] += 1
                chat_id = await _chat_of_call(item["call"])
                if chat_id is not None:
                    _pending.remove(item)
                    if not await _enabled(chat_id):
                        LOGGER.debug(
                            "vc_chat_log[%s]: queued line dropped — log is OFF here",
                            chat_id,
                        )
                        continue
                    _buffers.setdefault(chat_id, []).append(item["line"])
                    _schedule(chat_id)
                    LOGGER.debug(
                        "vc_chat_log[%s]: queued VC line recovered after %d retry(ies)",
                        chat_id, item["tries"],
                    )
                elif item["tries"] >= _PENDING_TRIES:
                    _pending.remove(item)
                    await _orphan_dump(item)
    finally:
        _pending_task = None


async def _orphan_dump(item: dict) -> None:
    """Last resort: a VC line we could not attribute to a group still gets
    stored in the VC backup channel — never silently dropped."""
    from melody import bot
    from melody.config import Config
    from melody.core.vc_theme import FLAGS, vc_card

    call_id = getattr(item.get("call"), "id", "?")
    channel_id = getattr(Config, "VC_CHAT_LOG_CHANNEL_ID", 0)
    LOGGER.warning(
        "vc_chat_log: call %s never mapped to a chat after %d tries — "
        "writing the line to the backup channel instead",
        call_id, item.get("tries", 0),
    )
    if bot is None or not channel_id:
        return
    try:
        from pyrogram import enums
        from pyrogram.types import LinkPreviewOptions

        await bot.send_message(
            channel_id,
            vc_card(
                "Vc Chat Backup",
                f"❓ <b>Uɴᴋɴᴏᴡɴ ɢʀᴏᴜᴘ</b> (call <code>{call_id}</code>)\n\n"
                + item["line"],
                footer=f"{FLAGS}",
                tags="#VᴄCʜᴀᴛBᴀᴄᴋᴜᴘ 🗂 #Uɴᴍᴀᴘᴘᴇᴅ",
            ),
            parse_mode=enums.ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as exc:
        LOGGER.warning(
            "vc_chat_log: orphan backup post failed (%s: %s)", type(exc).__name__, exc
        )


def _queue_pending(call, line: str) -> None:
    global _pending_task
    _pending.append({"call": call, "line": line, "tries": 0, "ts": time.time()})
    if _pending_task is None or _pending_task.done():
        _pending_task = asyncio.create_task(_pending_worker())


def _schedule(chat_id: int) -> None:
    if chat_id in _flush_tasks:
        return
    _flush_tasks[chat_id] = asyncio.create_task(_flush(chat_id))


# ────────────────────────────── raw update entry ────────────────────────────
async def handle_raw_update(client, update, users, chats) -> None:
    """Registered on the ASSISTANT client (the account inside the call)."""
    try:
        from pyrogram.raw import types as raw

        new_msg = getattr(raw, "UpdateGroupCallMessage", None)
        del_msg = getattr(raw, "UpdateDeleteGroupCallMessages", None)
        parts = getattr(raw, "UpdateGroupCallParticipants", None)
        call_upd = getattr(raw, "UpdateGroupCall", None)

        if new_msg is not None and isinstance(update, new_msg):
            await _on_call_message(update)
        elif del_msg is not None and isinstance(update, del_msg):
            await _on_call_delete(update)
        elif parts is not None and isinstance(update, parts):
            from melody.core.vc_notify import handle_raw_participants

            await handle_raw_participants(update)
        elif call_upd is not None and isinstance(update, call_upd):
            call = getattr(update, "call", None)
            call_id = getattr(call, "id", None)
            if isinstance(call_id, int):
                # Cache EVERY call object we ever see: the next in-call
                # message can then be mapped instantly instead of dropped.
                _call_input[call_id] = call
    except Exception as exc:
        LOGGER.warning(
            "vc_chat_log raw handler error on %s (%s: %s)",
            type(update).__name__, type(exc).__name__, exc, exc_info=True,
        )


async def _on_call_message(update) -> None:
    message = getattr(update, "message", None)
    call = getattr(update, "call", None)
    if message is None or call is None:
        LOGGER.debug("vc_chat_log: in-call update without message/call — ignored")
        return

    call_id = getattr(call, "id", None)
    msg_id = getattr(message, "id", None)
    key = (call_id, msg_id)
    _prune_seen()
    if key in _seen:
        LOGGER.debug("vc_chat_log: duplicate in-call message %s skipped", key)
        return
    _seen[key] = time.time()

    text_obj = getattr(message, "message", None)
    text = getattr(text_obj, "text", None) or str(text_obj or "")
    if not text.strip():
        LOGGER.debug(
            "vc_chat_log: in-call message %s in call %s had no text (media/service)",
            msg_id, call_id,
        )
        return

    user_id = _peer_id(getattr(message, "from_id", None))
    name = await _name_of(user_id) if isinstance(user_id, int) else "Unknown"
    who = (
        f'<a href="tg://user?id={user_id}">{html.escape(str(name))}</a> '
        f'(<code>{user_id}</code>)'
        if isinstance(user_id, int) else f"<b>{html.escape(str(name))}</b>"
    )
    stars = getattr(message, "paid_message_stars", None)
    badge = " 👑" if getattr(message, "from_admin", False) else ""
    paid = f" ⭐️<code>{stars}</code>" if stars else ""
    line = (
        f"🎙 {who}{badge}{paid}:\n<blockquote>{html.escape(text.strip())}"
        f"</blockquote>"
    )

    chat_id = await _chat_of_call(call)
    if chat_id is None:
        # STRICT: never drop it — retry until the call can be mapped.
        _queue_pending(call, line)
        LOGGER.debug(
            "vc_chat_log: call %s not mapped yet — message from %s queued "
            "(%d waiting)", call_id, user_id, len(_pending),
        )
        return

    if not await _enabled(chat_id):
        LOGGER.debug(
            "vc_chat_log[%s]: message from %s ignored — log is OFF in this chat",
            chat_id, user_id,
        )
        return

    _buffers.setdefault(chat_id, []).append(line)
    LOGGER.debug(
        "vc_chat_log[%s]: captured in-call message %s from %s (%s) — %d char(s), "
        "%d line(s) buffered",
        chat_id, msg_id, name, user_id, len(text.strip()),
        len(_buffers.get(chat_id, [])),
    )
    _schedule(chat_id)


async def _on_call_delete(update) -> None:
    call = getattr(update, "call", None)
    ids = list(getattr(update, "messages", None) or [])
    if call is None or not ids:
        return
    line = (
        f"🗑 <i>{len(ids)} ɪɴ-ᴄᴀʟʟ ᴍᴇssᴀɢᴇ(s) ᴅᴇʟᴇᴛᴇᴅ ɪɴ ᴛʜᴇ ᴄᴀʟʟ — "
        f"ʙᴀᴄᴋᴜᴘ ᴋᴇᴘᴛ</i>"
    )
    chat_id = await _chat_of_call(call)
    if chat_id is None:
        _queue_pending(call, line)
        return
    if not await _enabled(chat_id):
        return
    LOGGER.debug(
        "vc_chat_log[%s]: %d in-call message(s) deleted by their sender — "
        "backup kept", chat_id, len(ids),
    )
    _buffers.setdefault(chat_id, []).append(line)
    _schedule(chat_id)


def register(assistant) -> bool:
    """Attach the raw handler to the assistant client. Safe to call twice."""
    if assistant is None:
        LOGGER.warning("vc_chat_log: assistant is None — in-call chat log disabled")
        return False
    if getattr(assistant, "_melody_vc_chat_log", False):
        return True
    from pyrogram.handlers import RawUpdateHandler

    # High group number: never competes with playback/command handlers.
    assistant.add_handler(RawUpdateHandler(handle_raw_update), group=20)
    assistant._melody_vc_chat_log = True
    LOGGER.info("vc_chat_log: in-call (VC) chat mirroring active")
    return True
