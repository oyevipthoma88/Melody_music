"""
🔒 Decorators for permission checks
BUG FIX: error_handler now also handles errors from admin_or_auth (DB failures etc.)

SPEED FIX ("commands bohot slow work krti hai"): every /skip, /pause, /stop
etc. used to make a Telegram API round-trip (get_chat_member) PLUS two
MongoDB round-trips (is_gbanned, is_banned) PLUS another MongoDB round-trip
(get_auth_users) — all serial, all before the command itself could run. On
Heroku that's easily 2-4 seconds of pure overhead per command.

Now:
  • Admin status is cached in-memory for 90 seconds per (chat, user) — the
    vast majority of repeated commands hit the cache and never touch the
    network.
  • The auth list (local DB) is checked BEFORE the slow Telegram API call,
    so authorized users skip get_chat_member entirely.
  • Ban lookups are cached too (30s) — they change rarely and were burning
    two Mongo queries on every command.
"""
import asyncio
import functools
import re
import time
from pyrogram import enums
from pyrogram import Client, ContinuePropagation, StopPropagation
from pyrogram.errors import FloodWait
from pyrogram.types import Message, CallbackQuery, LinkPreviewOptions
from melody.config import Config
from utils.formatters import quote_html
from melody.logging import LOGGER, send_error_log
from utils.database import is_banned, is_gbanned, get_auth_users
from utils import cmd_lock
from utils.error_translate import translate as translate_error
from utils.tasks import spawn

# ── In-memory caches (process-local, no network) ────────────────────────────
# (chat_id, user_id) → (is_admin, timestamp)
_admin_cache: dict = {}
_ADMIN_TTL = 90  # seconds — admin status rarely changes mid-session
_ADMIN_NEGATIVE_TTL = 5  # promotions must not remain hidden for 90 seconds

# user_id → (is_banned, is_gbanned, timestamp)
_ban_cache: dict = {}
_BAN_TTL = 30  # seconds — bans change rarely
_BAN_CHECK_TIMEOUT = 2.0
_AUTH_CHECK_TIMEOUT = 1.5


# chat_id → (expiry_monotonic, {admin_user_ids}). One `get_chat_members`
# call caches EVERY admin of the chat at once, so the first command from each
# admin no longer pays a separate get_chat_member round-trip. /reload rebuilds
# this on demand.
_chat_admins: dict = {}
_CHAT_ADMINS_TTL = 600  # seconds


async def refresh_chat_admins(client: Client, chat_id: int) -> set:
    """Fetch (and cache) the full admin id set for a chat. Returns the set."""
    admins: set = set()
    try:
        async for m in client.get_chat_members(
            chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS
        ):
            if m.user:
                admins.add(m.user.id)
    except Exception as exc:
        LOGGER.warning("Admin-list refresh failed for chat %s: %s", chat_id, exc)
        return _chat_admins.get(chat_id, (0, set()))[1]
    _chat_admins[chat_id] = (time.monotonic() + _CHAT_ADMINS_TTL, admins)
    # Keep the per-user cache consistent with the freshly fetched list.
    for (c, u) in [k for k in _admin_cache if k[0] == chat_id]:
        _admin_cache.pop((c, u), None)
    for uid in admins:
        _set_cached_admin(chat_id, uid, True)
    return admins


def _chat_admin_set(chat_id: int) -> "set | None":
    entry = _chat_admins.get(chat_id)
    if not entry or entry[0] <= time.monotonic():
        _chat_admins.pop(chat_id, None)
        return None
    return entry[1]


def invalidate_admin_cache(chat_id: int | None = None) -> None:
    """Drop cached admin data (used by /reload and by promote/demote)."""
    if chat_id is None:
        _admin_cache.clear()
        _chat_admins.clear()
        return
    _chat_admins.pop(chat_id, None)
    for key in [k for k in _admin_cache if k[0] == chat_id]:
        _admin_cache.pop(key, None)


def _is_cached_admin(chat_id: int, user_id: int) -> bool | None:
    """Return cached admin status or None if not cached / expired."""
    admins = _chat_admin_set(chat_id)
    if admins is not None and user_id in admins:
        return True
    entry = _admin_cache.get((chat_id, user_id))
    if entry is None:
        return None
    is_admin, ts = entry
    ttl = _ADMIN_TTL if is_admin else _ADMIN_NEGATIVE_TTL
    if time.monotonic() - ts > ttl:
        _admin_cache.pop((chat_id, user_id), None)
        return None
    return is_admin


def _set_cached_admin(chat_id: int, user_id: int, is_admin: bool) -> None:
    _admin_cache[(chat_id, user_id)] = (is_admin, time.monotonic())


def _is_admin_status(status) -> bool:
    """Handle real Pyrogram enums plus compatible fork/string values."""
    return status in (
        enums.ChatMemberStatus.OWNER,
        enums.ChatMemberStatus.ADMINISTRATOR,
    ) or str(status).lower() in {
        "chatmemberstatus.owner",
        "chatmemberstatus.administrator",
        "owner",
        "creator",
        "administrator",
    }


async def _fetch_admin_status(client: Client, chat_id: int, user_id: int) -> "bool | None":
    """Return None on Telegram/RPC failure; never turn an outage into denial."""
    if _chat_admin_set(chat_id) is None:
        # Warm the whole admin list once, in the background, so the NEXT
        # command from any admin of this chat is answered from memory.
        spawn(refresh_chat_admins(client, chat_id))
    for attempt in range(2):
        try:
            member = await client.get_chat_member(chat_id, user_id)
            return _is_admin_status(member.status)
        except Exception as exc:
            if attempt == 0:
                await asyncio.sleep(0.15)
                continue
            LOGGER.warning(
                "Admin lookup failed for user %s in chat %s: %s",
                user_id, chat_id, exc,
            )
    return None


def _cached_ban_status(user_id: int) -> tuple[bool, bool] | None:
    """Return (is_banned, is_gbanned) from cache, or None if expired."""
    entry = _ban_cache.get(user_id)
    if entry is None:
        return None
    banned, gbanned, ts = entry
    if time.monotonic() - ts > _BAN_TTL:
        _ban_cache.pop(user_id, None)
        return None
    return banned, gbanned


def _set_cached_ban_status(user_id: int, banned: bool, gbanned: bool) -> None:
    _ban_cache[user_id] = (banned, gbanned, time.monotonic())


async def _auth_users_fast(chat_id: int) -> list | None:
    """Return cached/auth users quickly; None means DB was unavailable."""
    try:
        return await asyncio.wait_for(
            get_auth_users(chat_id), timeout=_AUTH_CHECK_TIMEOUT,
        )
    except Exception as exc:
        LOGGER.debug("auth lookup timed out for %s: %s", chat_id, type(exc).__name__)
        return None


async def _check_banned(user_id: int) -> tuple[bool, bool]:
    """Return (is_banned, is_gbanned) with caching and parallel I/O."""
    cached = _cached_ban_status(user_id)
    if cached is not None:
        return cached
    # These are independent collections. Running them together removes one
    # full Atlas round-trip from the first command while the bounded timeout
    # prevents a dead Mongo node from holding every handler indefinitely.
    banned, gbanned = await asyncio.wait_for(
        asyncio.gather(is_banned(user_id), is_gbanned(user_id)),
        timeout=_BAN_CHECK_TIMEOUT,
    )
    result = (bool(banned), bool(gbanned))
    _set_cached_ban_status(user_id, *result)
    return result


def owner_only(func):
    """Restrict command to bot owner only."""
    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        if message.from_user and message.from_user.id == Config.OWNER_ID:
            return await func(client, message, *args, **kwargs)
        # Silently ignore — owner commands are hidden
    return wrapper


def admin_or_auth(func):
    """Allow group admins and authorized users only."""
    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        if not message.from_user:
            return
        user_id = message.from_user.id
        chat_id = message.chat.id

        # Owner always allowed
        if user_id == Config.OWNER_ID:
            return await func(client, message, *args, **kwargs)

        # Check bans (cached)
        banned, gbanned = await _check_banned(user_id)
        if gbanned:
            return await message.reply(quote_html("❌ You are globally banned from using this bot."), parse_mode=enums.ParseMode.HTML)
        if banned:
            return await message.reply(quote_html("❌ You are banned from using this bot."), parse_mode=enums.ParseMode.HTML)

        # Check auth list FIRST — it's a fast local DB lookup, much cheaper
        # than the Telegram API call below. Authorized users skip the slow
        # get_chat_member round-trip entirely.
        auth_users = await _auth_users_fast(chat_id)
        if auth_users is not None and user_id in auth_users:
            return await func(client, message, *args, **kwargs)

        # Check cached admin status before hitting the Telegram API
        cached = _is_cached_admin(chat_id, user_id)
        if cached is True:
            return await func(client, message, *args, **kwargs)
        if cached is False:
            await message.reply(quote_html("⚠️ Only group admins or authorized users can use this command."), parse_mode=enums.ParseMode.HTML)
            return

        # Slow path: Telegram API call (cached afterward)
        status = await _fetch_admin_status(client, chat_id, user_id)
        if status is True:
            _set_cached_admin(chat_id, user_id, True)
            return await func(client, message, *args, **kwargs)
        if status is None:
            return await message.reply(
                quote_html("⚠️ Admin status abhi verify nahi ho paya. Ek baar phir try karo."),
                parse_mode=enums.ParseMode.HTML,
            )

        _set_cached_admin(chat_id, user_id, False)
        await message.reply(quote_html("⚠️ Only group admins or authorized users can use this command."), parse_mode=enums.ParseMode.HTML)

    return wrapper


def channel_admin_or_auth(func):
    """Permission gate for commands usable directly inside a channel
    (channel play/controls), where the invoking message may be a genuine
    channel post instead of a normal user message.

    Telegram only allows channel admins to author a post that appears as
    coming from the channel itself (message.from_user is None in that case,
    message.sender_chat is the channel) — so any such message is implicitly
    admin-authored and safe to allow. If the message DOES have a from_user
    (e.g. sent from a discussion group linked to the channel, or Telegram
    surfaces the real author), fall back to the same checks as
    admin_or_auth.
    """
    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        chat_id = message.chat.id

        if message.from_user is None:
            # Genuine channel post — only channel admins can author these.
            return await func(client, message, *args, **kwargs)

        user_id = message.from_user.id

        if user_id == Config.OWNER_ID:
            return await func(client, message, *args, **kwargs)

        banned, gbanned = await _check_banned(user_id)
        if gbanned:
            return await message.reply(quote_html("❌ You are globally banned from using this bot."), parse_mode=enums.ParseMode.HTML)
        if banned:
            return await message.reply(quote_html("❌ You are banned from using this bot."), parse_mode=enums.ParseMode.HTML)

        auth_users = await _auth_users_fast(chat_id)
        if auth_users is not None and user_id in auth_users:
            return await func(client, message, *args, **kwargs)

        cached = _is_cached_admin(chat_id, user_id)
        if cached is True:
            return await func(client, message, *args, **kwargs)
        if cached is False:
            await message.reply(quote_html("⚠️ Only channel admins or authorized users can use this command."), parse_mode=enums.ParseMode.HTML)
            return

        status = await _fetch_admin_status(client, chat_id, user_id)
        if status is True:
            _set_cached_admin(chat_id, user_id, True)
            return await func(client, message, *args, **kwargs)
        if status is None:
            return await message.reply(
                quote_html("⚠️ Admin status abhi verify nahi ho paya. Ek baar phir try karo."),
                parse_mode=enums.ParseMode.HTML,
            )

        _set_cached_admin(chat_id, user_id, False)
        await message.reply(quote_html("⚠️ Only channel admins or authorized users can use this command."), parse_mode=enums.ParseMode.HTML)

    return wrapper


def playmode_gate(func):
    """Permission gate for /play and /vplay that honours the chat's playmode.

    MISSING-FEATURE FIX: /play was wrapped in `channel_admin_or_auth`, so in
    every normal group only admins could ever queue a song — none of the top
    music bots behave that way. Now the chat decides:

      • playmode access = "everyone" (default) → any non-banned member plays.
      • playmode access = "admin"              → falls back to the old
                                                 channel_admin_or_auth checks.

    Global/bot bans are still enforced in BOTH modes.
    """
    strict = channel_admin_or_auth(func)

    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        try:
            from melody.plugins.music.playmode import get_access_mode
            mode = await asyncio.wait_for(
                get_access_mode(message.chat.id), timeout=1.0,
            )
        except Exception:
            mode = "everyone"

        if mode != "everyone" or message.from_user is None:
            return await strict(client, message, *args, **kwargs)

        user_id = message.from_user.id
        if user_id != Config.OWNER_ID:
            banned, gbanned = await _check_banned(user_id)
            if gbanned:
                return await message.reply(
                    quote_html("❌ You are globally banned from using this bot."),
                    parse_mode=enums.ParseMode.HTML,
                )
            if banned:
                return await message.reply(
                    quote_html("❌ You are banned from using this bot."),
                    parse_mode=enums.ParseMode.HTML,
                )
        return await func(client, message, *args, **kwargs)

    return wrapper


def error_handler(func):
    """
    Catch ALL exceptions (including from nested decorators like admin_or_auth).
    Send traceback to LOG_GROUP and show friendly message to user.
    Works for both Message handlers and CallbackQuery handlers.

    BUG FIX (⚠️ Melody Error Log spam / crash on FloodWait):
    A FloodWait raised by Telegram (420 FLOOD_WAIT_X) is normal rate-limit
    back-pressure, not a bug — it used to be treated exactly like any other
    exception here: logged to LOG_GROUP as an "error" AND immediately
    retried by sending yet another message ("Something went wrong 🌸") to
    the same flood-controlled chat/method, which just as easily hits the
    SAME flood wait again (or a longer one) and raises a second, uncaught
    FloodWait right out of this handler. Client.sleep_threshold (see
    melody/__init__.py) now makes Pyrogram auto-sleep-and-retry any
    FLOOD_WAIT_X up to 60s transparently, so this branch only ever fires
    for waits longer than that. In that rarer case: sleep out the wait
    once and retry the original command a single time instead of sending
    a second message into the same flood window; never re-log FloodWait
    as an "error" (it isn't one) and never let a retry's own FloodWait
    cascade into more exceptions.
    """
    @functools.wraps(func)
    async def wrapper(client, update, *args, **kwargs):
        # STRICT COMMAND LOCK + INSTANT RESPONSE (REQUESTED: "all cmnds
        # strictly lock krde, skip/end/play instant response aaye"). Zero
        # network cost, so it can sit in front of every single handler.
        lock_key = None
        if isinstance(update, Message):
            try:
                allowed, lock_key = await cmd_lock.acquire(update)
                if not allowed:
                    return
            except Exception:
                lock_key = None
        try:
            return await func(client, update, *args, **kwargs)
        except (StopPropagation, ContinuePropagation):
            # BUG FIX (log: "Error in capture_source_url: ... pyrogram.StopPropagation"):
            # StopPropagation / ContinuePropagation are Pyrogram CONTROL-FLOW
            # signals, not errors. `message.stop_propagation()` raises
            # StopPropagation on purpose to stop later handler groups from
            # also seeing the message. The broad `except Exception` below used
            # to swallow it, report a fake traceback to the owner's error log
            # AND reply "Something went wrong 🌸" to the user on a completely
            # successful command. Re-raise so Pyrogram's dispatcher handles it.
            raise
        except FloodWait as e:
            LOGGER.warning("FloodWait %ss in %s — sleeping once and retrying.", e.value, func.__name__)
            try:
                await asyncio.sleep(e.value)
                return await func(client, update, *args, **kwargs)
            except Exception:
                # Retry itself failed (likely flooded again) — give up quietly.
                # No user-facing message and no error-log spam for flood control.
                pass
        except Exception as e:
            # Build structured context from the update so the log channel
            # shows which command, which chat, and which user triggered
            # the error — not just a bare function name and traceback.
            ctx = {"command": func.__name__}
            try:
                if isinstance(update, CallbackQuery):
                    if update.message and update.message.chat:
                        ctx["chat_id"] = update.message.chat.id
                        ctx["chat_title"] = update.message.chat.title
                        ctx["chat_username"] = getattr(update.message.chat, "username", None)
                    if update.from_user:
                        ctx["user_id"] = update.from_user.id
                        ctx["user_name"] = update.from_user.first_name or ""
                elif hasattr(update, "chat") and update.chat:
                    ctx["chat_id"] = update.chat.id
                    ctx["chat_title"] = update.chat.title
                    ctx["chat_username"] = getattr(update.chat, "username", None)
                if hasattr(update, "from_user") and update.from_user:
                    ctx["user_id"] = update.from_user.id
                    ctx["user_name"] = update.from_user.first_name or ""
            except Exception:
                pass
            # SMART ERROR HANDLING (REQUESTED: "bug mt show kr, text bhejna
            # kya issue ho rahi hai"). The user never sees a traceback or a
            # useless "Something went wrong" — they get a short human message
            # that says what broke and exactly what to do about it (for an
            # assistant ban: the unban command + the assistant's user id + a
            # convincing request). The log group still gets everything.
            try:
                info = await translate_error(e)
            except Exception:
                info = {
                    "human": "An unexpected problem interrupted this command.",
                    "user": "🌸 <b>Ye command puri nahi ho payi.</b> Thoda ruk kar dubara bhejo.",
                }
            ctx["human_reason"] = info.get("human", "")
            await send_error_log(
                f"Error in {func.__name__}: {e}",
                exc=e,
                context=ctx,
                human=info.get("human"),
            )
            try:
                if isinstance(update, CallbackQuery):
                    # An alert has a hard length limit and no HTML.
                    plain = re.sub(r"<[^>]+>", "", info["user"]).strip()
                    await update.answer(plain[:195], show_alert=True)
                else:
                    await update.reply(
                        info["user"],
                        parse_mode=enums.ParseMode.HTML,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
            except Exception:
                pass
        finally:
            cmd_lock.release(lock_key, update)
    return wrapper


async def cb_admin_or_auth(client: Client, cb, chat_id: int | None = None) -> bool:
    """Callback-query twin of :func:`admin_or_auth`.

    SECURITY FIX: the inline player buttons (pause / resume / skip / stop)
    had NO permission check at all, while the identical `/pause`, `/skip`,
    `/stop` commands were admin-gated. Any member of any group could stop
    everybody else's music by tapping the card. This helper applies the same
    rule to a CallbackQuery and answers the query itself when access is
    denied, so handlers stay one line.
    """
    user = getattr(cb, "from_user", None)
    if not user:
        return False
    user_id = user.id
    chat = getattr(getattr(cb, "message", None), "chat", None)
    # `chat_id` may be supplied by the caller when it comes from callback data
    # (the play card embeds the target chat), so the permission check runs
    # against the chat that is actually being controlled.
    if chat_id is None:
        chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return False
    if getattr(chat, "id", None) != chat_id:
        chat = None

    if user_id == Config.OWNER_ID:
        return True

    banned, gbanned = await _check_banned(user_id)
    if gbanned or banned:
        await cb.answer("❌ You are banned from using this bot.", show_alert=True)
        return False

    # Private chat / channel post buttons have no group admin concept.
    if chat is not None and getattr(chat, "type", None) is not None and str(
        chat.type
    ) in ("ChatType.PRIVATE", "private"):
        return True

    # Check auth list before the slow Telegram API call
    auth_users = await _auth_users_fast(chat_id)
    if auth_users is not None and user_id in auth_users:
        return True

    # Check cached admin status
    cached = _is_cached_admin(chat_id, user_id)
    if cached is True:
        return True
    if cached is False:
        await cb.answer(
            "⚠️ Sirf group admins ya authorized users ye control kar sakte hain.",
            show_alert=True,
        )
        return False

    try:
        member = await client.get_chat_member(chat_id, user_id)
        if str(member.status) in ("ChatMemberStatus.OWNER",
                                  "ChatMemberStatus.ADMINISTRATOR",
                                  "creator", "administrator"):
            _set_cached_admin(chat_id, user_id, True)
            return True
    except Exception:
        pass

    _set_cached_admin(chat_id, user_id, False)
    await cb.answer(
        "⚠️ Sirf group admins ya authorized users ye control kar sakte hain.",
        show_alert=True,
    )
    return False


async def is_admin_or_auth(client: Client, chat_id: int, user_id: int) -> bool:
    """Return whether a user may use an admin-gated group feature.

    This is the non-message counterpart of :func:`admin_or_auth`, intended for
    handlers that need to resolve a secondary target (for example, a linked
    channel voice chat) before choosing their reply. It deliberately reuses
    the same ban, authorization, and admin caches as the decorator so access
    decisions cannot drift between command families.
    """
    if not user_id:
        return False
    if user_id == Config.OWNER_ID:
        return True

    banned, gbanned = await _check_banned(user_id)
    if banned or gbanned:
        return False

    auth_users = await _auth_users_fast(chat_id)
    if auth_users is not None and user_id in auth_users:
        return True

    cached = _is_cached_admin(chat_id, user_id)
    if cached is not None:
        return cached

    status = await _fetch_admin_status(client, chat_id, user_id)
    if status is None:
        return False
    _set_cached_admin(chat_id, user_id, status)
    return status


async def cb_playmode_gate(client: Client, cb, chat_id: int | None = None) -> bool:
    """Apply the same access policy as :func:`playmode_gate` to callbacks."""
    user = getattr(cb, "from_user", None)
    if not user:
        return False
    if chat_id is None:
        chat = getattr(getattr(cb, "message", None), "chat", None)
        chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return False

    if user.id != Config.OWNER_ID:
        banned, gbanned = await _check_banned(user.id)
        if gbanned or banned:
            await cb.answer("❌ You are banned from using this bot.", show_alert=True)
            return False

    try:
        from melody.plugins.music.playmode import get_access_mode
        mode = await asyncio.wait_for(
            get_access_mode(chat_id), timeout=1.0,
        )
    except Exception:
        mode = "everyone"

    if mode == "everyone":
        return True
    return await cb_admin_or_auth(client, cb, chat_id=chat_id)
