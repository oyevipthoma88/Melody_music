from pyrogram.types import LinkPreviewOptions
"""
🎙 Voice-chat event notifications.

Sends a pretty card in the group whenever:
  • a user (or the assistant account) JOINS the voice chat      → #JᴏɪɴVɪᴅᴇᴏCʜᴀᴛ
  • a user (or the assistant account) LEAVES the voice chat     → #LᴇғᴛVɪᴅᴇᴏCʜᴀᴛ
  • the voice chat is closed / the assistant gets kicked        → #EɴᴅᴇᴅVɪᴅᴇᴏCʜᴀᴛ
  • the assistant is invited to a voice chat                    → #VᴄIɴᴠɪᴛᴇ

Join / leave events are NOT Telegram service messages. They are detected
WITHOUT putting any account inside the call: `melody/core/vc_listener.py`
polls `phone.GetGroupParticipants` with the assistant account from outside the
call and diffs the roster. py-tgcalls' `UpdatedGroupCallParticipant` update is
still honoured (and deduped) for the times the assistant is in the call to
play music.
Video-chat *started* / *ended* / *members invited* ARE service messages and
are handled by `melody/plugins/misc/vc_events.py`.
"""
import asyncio
import html
import time

from pyrogram import enums

from melody.logging import LOGGER

# (chat_id, user_id, action) → last notification timestamp. py-tgcalls can
# emit several participant updates for the same person within milliseconds
# (mute state, video state, source change), and each of those arrives as a
# fresh "JOINED"/"LEFT" flag. Without this guard the group gets spammed.
_last_notice: dict = {}
# ROOT FIX ("VC left ka message 2 baar aata hai"): the same leave arrives from
# two independent sources — the raw MTProto participant update (assistant in
# call) and the roster poller in melody/core/vc_listener.py, which can be up
# to a poll interval apart. An 8s window was shorter than that gap, so both
# posted a card. The window now covers a full poll cycle AND announce() also
# checks the tracked roster, so a second LEFT for someone already gone is a
# no-op.
_DEDUPE_WINDOW = 45.0
_participant_state: dict = {}


def _should_notify(chat_id: int, user_id: int, action: str) -> bool:
    key = (chat_id, user_id, action)
    now = time.time()
    last = _last_notice.get(key, 0.0)
    if now - last < _DEDUPE_WINDOW:
        return False
    _last_notice[key] = now
    # Opposite action can fire again immediately (join right after a leave).
    _last_notice.pop((chat_id, user_id, "LEFT" if action == "JOINED" else "JOINED"), None)
    return True


_name_cache: dict = {}


def _full_name(user) -> str:
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    name = (f"{first} {last}".strip()
            or (getattr(user, "title", None) or "").strip()
            or (getattr(user, "username", None) or "").strip())
    return name


async def _resolve_user(user_id: int) -> tuple[str, str]:
    """Return a display name and username for a VC participant.

    BUG FIX ("kabhi kabhi Unknown show hota hai"): a single failed
    ``get_users`` (peer not cached on that client / FloodWait) made the card
    read "Unknown" forever. Now every source is tried — bot, assistant,
    ``get_chat_member`` on the group, and the roster we already built — and
    successful lookups are cached so the same person is never "Unknown"
    twice. Last resort is a readable ``User <id>`` instead of "Unknown".
    """
    from melody import bot, assistant

    cached = _name_cache.get(user_id)
    if cached:
        return cached

    for client in (bot, assistant):
        if client is None:
            continue
        try:
            user = await client.get_users(user_id)
        except Exception:
            continue
        name = _full_name(user)
        if name:
            username = getattr(user, "username", None)
            result = (str(name), f"@{username}" if username else "—")
            _name_cache[user_id] = result
            return result

    # Still nothing: ask every chat we are tracking this user in — a
    # group-member lookup resolves peers that get_users() cannot.
    for chat_id, roster in list(_vc_members.items()):
        if user_id not in roster:
            continue
        for client in (bot, assistant):
            if client is None:
                continue
            try:
                member = await client.get_chat_member(chat_id, user_id)
            except Exception:
                continue
            name = _full_name(getattr(member, "user", None) or member)
            if name:
                username = getattr(getattr(member, "user", None), "username", None)
                result = (str(name), f"@{username}" if username else "—")
                _name_cache[user_id] = result
                return result

    # Roster may still hold a name captured on JOIN.
    for roster in _vc_members.values():
        known = roster.get(user_id)
        if known and str(known) != str(user_id):
            return str(known), "—"

    return f"User {user_id}", "—"


async def _activity_enabled(chat_id: int) -> bool:
    from utils.gc_db import get_setting_flag
    return bool(await get_setting_flag(chat_id, "vc_activity", True))


# Telegram does not expose a separate, distinguishable "voice-chat text"
# update. Only real group-call participant events are reported here.
JOIN_LEFT_AUTO_DELETE = 2   # REQUESTED: join/left cards vanish in 2s
# REQUESTED: "vc invite mesg delete mt Krna" — invite cards stay forever.
INVITE_AUTO_DELETE = 0


async def _send(chat_id: int, text: str, auto_delete_after: "int | None" = None,
                with_close: bool = True):
    from melody import bot

    if bot is None:
        return None
    keyboard = None
    if with_close:
        try:
            from utils.admin_tools import close_kb

            keyboard = close_kb()
        except Exception:
            keyboard = None
    try:
        sent = await bot.send_message(
            chat_id, text,
            parse_mode=enums.ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=keyboard,
        )
    except Exception as exc:  # group may forbid the bot from posting
        LOGGER.debug("VC notice not sent in %s: %s", chat_id, exc)
        return None

    if auto_delete_after:
        from utils.admin_tools import auto_delete

        await auto_delete(sent, auto_delete_after)
    return sent


# Live VC roster per chat — powers the "👥 In VC" counter on the cards and
# the VC chat log header.
_vc_members: dict = {}


def _track_presence(chat_id: int, user_id: int, name: str, joined: bool) -> None:
    roster = _vc_members.setdefault(chat_id, {})
    if joined:
        roster[user_id] = name
    else:
        roster.pop(user_id, None)
    # A live JOIN/LEFT update means our view of the call is current, so the
    # VC chat log can trust the roster without another network round-trip.
    _roster_stamp[chat_id] = time.monotonic()


def _listener_count(chat_id: int) -> int:
    return len(_vc_members.get(chat_id, {}))


def clear_roster(chat_id: int) -> None:
    _vc_members.pop(chat_id, None)
    _roster_stamp.pop(chat_id, None)


def roster_is_known(chat_id: int) -> bool:
    """True when we have a trustworthy roster for this chat (fetched or
    built from live participant updates within the freshness window)."""
    return (time.monotonic() - _roster_stamp.get(chat_id, 0.0)) < _ROSTER_TRUST


# chat_id -> monotonic timestamp of the last *successful* roster fetch, plus a
# per-chat lock so twenty simultaneous group messages trigger ONE lookup.
_roster_stamp: dict = {}
_roster_locks: dict = {}
_ROSTER_TTL = 8.0      # re-fetch at most this often
# The outside-the-call watcher re-polls every 10s, so a 25s trust window keeps
# the roster usable between polls without one lookup per group message.
_ROSTER_TRUST = 25.0

# "Is a voice chat running at all?" cache — this works with the BOT account, so
# it answers even when the assistant is not a member of the group and the
# participant list is impossible to read (bots cannot call phone.*).
_call_live: dict = {}
_call_stamp: dict = {}
_CALL_TTL = 30.0   # GetFullChannel is flood-prone: cache the answer longer


async def _full_chat(client, chat_id: int):
    from pyrogram.raw import functions, types as raw

    peer = await client.resolve_peer(chat_id)
    if isinstance(peer, raw.InputPeerChannel):
        return await client.invoke(functions.channels.GetFullChannel(channel=peer))
    return await client.invoke(
        functions.messages.GetFullChat(chat_id=getattr(peer, "chat_id", chat_id))
    )


async def call_is_live(chat_id: int) -> bool:
    """Is a voice/video chat currently running in this chat?

    JUGAD (requested): the participant list can only be read by the assistant
    account, and only when it is inside the group. But the *existence* of the
    call is visible to the bot itself via `full_chat.call`, so the VC chat log
    can still work (loose mode) whenever a VC is live."""
    now = time.monotonic()
    if chat_id in _call_live and now - _call_stamp.get(chat_id, 0.0) < _CALL_TTL:
        return _call_live[chat_id]

    from melody import bot, assistant

    live = False
    flood_for = 0
    for client in (bot, assistant):
        if client is None:
            continue
        try:
            full = await _full_chat(client, chat_id)
            call = getattr(getattr(full, "full_chat", None), "call", None)
            live = call is not None
            if live:
                try:
                    from melody.core.vc_chat_log import remember_call

                    remember_call(chat_id, call)
                except Exception:
                    pass
            break
        except Exception as exc:
            # BUG FIX (log: 'Waiting for N seconds … required by
            # "channels.GetFullChannel"'): polling dozens of chats made
            # Telegram rate-limit the lookup, and pyrogram then *slept*
            # inside the poll loop, stalling every other VC feature. Treat a
            # flood-wait as "unknown for now" and back this chat off instead.
            wait = getattr(exc, "value", None) or getattr(exc, "x", None)
            if type(exc).__name__ == "FloodWait" and isinstance(wait, int):
                flood_for = max(flood_for, wait)
                LOGGER.debug("call_is_live flood-wait %ss for %s", wait, chat_id)
                continue
            LOGGER.debug("call_is_live lookup failed for %s: %s", chat_id, exc)
            continue

    _call_live[chat_id] = live
    # Cache a flood-limited answer for as long as Telegram told us to wait, so
    # the watcher stops hammering the same chat.
    _call_stamp[chat_id] = now + max(0.0, flood_for - _CALL_TTL) if flood_for else now
    return live



async def _raw_participants(chat_id: int) -> "list[int] | None":
    """Read the voice-chat participant list straight from Telegram with the
    assistant account.

    ROOT FIX: the old implementation only asked py-tgcalls, which can answer
    ONLY while the assistant is connected and streaming. So whenever the VC
    was running without music (or right after a bot restart) the roster stayed
    empty and the whole VC chat log silently did nothing. This path works for
    any voice chat the assistant can see, playing or not.

    Returns None when the lookup itself failed (so callers keep the old
    roster instead of wrongly emptying it)."""
    from melody import assistant

    if assistant is None:
        return None
    try:
        from pyrogram.raw import functions, types as raw

        peer = await assistant.resolve_peer(chat_id)
        if isinstance(peer, raw.InputPeerChannel):
            full = await assistant.invoke(functions.channels.GetFullChannel(channel=peer))
        else:
            full = await assistant.invoke(
                functions.messages.GetFullChat(chat_id=getattr(peer, "chat_id", chat_id))
            )
        call = getattr(getattr(full, "full_chat", None), "call", None)
        if call is None:
            return []  # no voice chat running at all
        # Feed the in-call chat logger: it receives raw group-call updates that
        # carry only (call id, access_hash), so it needs this call -> chat map.
        try:
            from melody.core.vc_chat_log import remember_call

            remember_call(chat_id, call)
        except Exception:
            pass
        result = await assistant.invoke(
            functions.phone.GetGroupParticipants(
                call=call, ids=[], sources=[], offset="", limit=200
            )
        )
        ids = []
        for participant in getattr(result, "participants", None) or []:
            # `left=True` rows are tombstones Telegram keeps in the answer.
            # Counting them kept people "in VC" long after they hung up, which
            # is one way ordinary group chat leaked into the mirror.
            if getattr(participant, "left", False):
                continue
            uid = getattr(getattr(participant, "peer", None), "user_id", None)
            if isinstance(uid, int):
                ids.append(uid)
        return ids
    except Exception as exc:
        LOGGER.debug("raw participant lookup failed for %s: %s", chat_id, exc)
        return None


async def refresh_roster(chat_id: int, max_age: float = _ROSTER_TTL) -> dict:
    """Rebuild the roster straight from the call.

    The bot can miss JOIN updates (restart, late assistant join), which would
    silently disable the VC chat log. Anything that needs an accurate roster
    calls this first. Results are cached for `max_age` seconds and the fetch
    is de-duplicated with a per-chat lock."""
    if (time.monotonic() - _roster_stamp.get(chat_id, 0.0)) < max_age:
        return dict(_vc_members.get(chat_id, {}))

    lock = _roster_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        # Another waiter may have refreshed while we queued on the lock.
        if (time.monotonic() - _roster_stamp.get(chat_id, 0.0)) < max_age:
            return dict(_vc_members.get(chat_id, {}))

        # ROOT FIX ("vc pe 2 user ho to bhi chat/join-left kaam kare"):
        # py-tgcalls only knows the participants it is streaming with, so it
        # used to answer with a 1-entry list (just the assistant) and we
        # trusted it — everyone else looked "not in VC" and got LEFT cards.
        # Telegram's own phone.GetGroupParticipants is authoritative and works
        # from OUTSIDE the call, so it is tried first now.
        ids: "list[int] | None" = await _raw_participants(chat_id)

        if not ids:
            try:
                from melody.core.call import get_participants

                participants = await get_participants(chat_id)
                if participants:
                    fallback = [
                        uid for uid in (
                            getattr(p, "user_id", None) or getattr(p, "peer_id", None)
                            for p in participants
                        )
                        if isinstance(uid, int)
                    ]
                    if fallback:
                        ids = fallback
            except Exception as exc:
                LOGGER.debug("pytgcalls roster failed for %s: %s", chat_id, exc)

        if ids is None:  # lookup broke — keep whatever we had
            return dict(_vc_members.get(chat_id, {}))

        known = _vc_members.get(chat_id, {})
        _vc_members[chat_id] = {uid: known.get(uid, str(uid)) for uid in ids}
        _roster_stamp[chat_id] = time.monotonic()
        return dict(_vc_members[chat_id])



def _peer_uid(peer) -> "int | None":
    for attr in ("user_id", "chat_id", "channel_id"):
        value = getattr(peer, attr, None)
        if isinstance(value, int):
            return value if attr == "user_id" else -value
    return None


async def _is_assistant(user_id: int) -> bool:
    from melody import assistant

    try:
        me = await assistant.get_me()
        return me.id == user_id
    except Exception:
        return False


def _join_card(name: str, username: str, role: str, listeners: int,
               user_id: int = 0) -> str:
    """JOIN card — REQUESTED: same trimmed layout as the VC invite card."""
    return (
        "#JᴏɪɴᴇᴅVɪᴅᴇᴏCʜᴀᴛ ✅\n"
        f"🙋 <b>Jᴏɪɴᴇᴅ :</b> {name} · <code>{username}</code> · <code>{user_id}</code>\n"
        f"👥 <b>Iɴ VC :</b> <code>{listeners}</code>"
    )


def _left_card(name: str, username: str, role: str, listeners: int,
               user_id: int = 0) -> str:
    """LEFT card — same trimmed layout as JOIN / invite."""
    return (
        "#LᴇғᴛVɪᴅᴇᴏCʜᴀᴛ ❌\n"
        f"🙋 <b>Lᴇғᴛ :</b> {name} · <code>{username}</code> · <code>{user_id}</code>\n"
        f"👥 <b>Sᴛɪʟʟ ɪɴ VC :</b> <code>{listeners}</code>"
    )


async def announce(chat_id: int, user_id: int, action_name: str) -> None:
    """Post ONE join or leave card (deduped across both update sources)."""
    if action_name not in ("JOINED", "LEFT"):
        return
    if not await _activity_enabled(chat_id):
        return
    # State guard: ignore an event that does not change presence (duplicate
    # LEFT for someone already gone / duplicate JOIN for someone already in).
    roster = _vc_members.get(chat_id)
    if roster is not None:
        present = user_id in roster
        if action_name == "LEFT" and not present:
            return
        if action_name == "JOINED" and present:
            return
    if not _should_notify(chat_id, user_id, action_name):
        return

    plain, username = await _resolve_user(user_id)
    safe_name = (
        f'<a href="tg://user?id={int(user_id)}">{html.escape(plain)}</a>'
    )
    is_assistant = await _is_assistant(user_id)

    if is_assistant:
        # The assistant is only ever in a call to play music. Its presence is
        # tracked for the counter but never announced as a "user joined" card.
        _track_presence(chat_id, user_id, plain, action_name == "JOINED")
        return

    role = "🤖 <b>Aꜱꜱɪꜱᴛᴀɴᴛ</b>" if is_assistant else "👤 <b>Uꜱᴇʀ</b>"
    _track_presence(chat_id, user_id, plain, joined=action_name == "JOINED")
    listeners = _listener_count(chat_id)
    text = (_join_card if action_name == "JOINED" else _left_card)(
        safe_name, html.escape(username), role, listeners, user_id
    )
    LOGGER.debug("VC %s in %s: %s (%s)", action_name, chat_id, plain, user_id)
    await _send(chat_id, text, auto_delete_after=JOIN_LEFT_AUTO_DELETE)


async def notify_participant(update) -> None:
    """Handle a py-tgcalls `UpdatedGroupCallParticipant` update."""
    try:
        from pytgcalls.types import GroupCallParticipant

        participant = getattr(update, "participant", None)
        chat_id = getattr(update, "chat_id", None)
        if participant is None or chat_id is None:
            return

        action = getattr(participant, "action", None)
        action_text = str(getattr(action, "name", action)).upper()
        if action == GroupCallParticipant.Action.JOINED or "JOIN" in action_text:
            action_name = "JOINED"
        elif (action == GroupCallParticipant.Action.LEFT
              or "LEFT" in action_text or "LEAVE" in action_text):
            action_name = "LEFT"
        else:
            return  # mute/camera/hand changes are not join-leave events

        user_id = getattr(participant, "user_id", None) or getattr(participant, "peer_id", None)
        if not isinstance(user_id, int):
            return
        await announce(chat_id, user_id, action_name)
    except Exception as exc:
        LOGGER.debug("notify_participant failed: %s", exc)


async def handle_raw_participants(update) -> None:
    """ROOT FIX for "VC join/left kaam nahi kar raha".

    py-tgcalls only forwards participant updates while it owns an active
    stream, so join/left cards silently stopped whenever no music was playing
    (and on some py-tgcalls builds, entirely). Telegram itself sends
    `UpdateGroupCallParticipants` to every account inside the call, so the
    assistant reports join/leave whenever it IS in the call for music, and
    `melody/core/vc_listener.py` polls the roster from outside the call the
    rest of the time. `_should_notify()` dedupes both paths.
    """
    try:
        from melody.core.vc_chat_log import _chat_of_call

        call = getattr(update, "call", None)
        participants = list(getattr(update, "participants", None) or [])
        if call is None or not participants:
            return
        chat_id = await _chat_of_call(call)
        if chat_id is None:
            return
        for participant in participants:
            user_id = _peer_uid(getattr(participant, "peer", None))
            if not isinstance(user_id, int):
                continue
            if getattr(participant, "left", False):
                await announce(chat_id, user_id, "LEFT")
            elif getattr(participant, "just_joined", False):
                await announce(chat_id, user_id, "JOINED")
    except Exception as exc:
        LOGGER.debug("handle_raw_participants failed: %s", exc)


def _ended_card(reason: str) -> str:
    from melody.core.vc_theme import FLAGS, vc_card

    return vc_card(
        "Ended Video Chat",
        f"{reason}\n"
        "🎵 Sᴛʀᴇᴀᴍ ʀᴏᴋ ᴅɪʏᴀ ɢᴀʏᴀ — ᴅᴏʙᴀʀᴀ VC ꜱᴛᴀʀᴛ ᴋᴀʀ ᴋᴇ "
        "<code>/play</code> ᴋᴀʀᴏ 🎧",
        footer=FLAGS,
        tags="#EɴᴅᴇᴅVɪᴅᴇᴏCʜᴀᴛ 🛑",
    )


async def notify_chat_update(update) -> None:
    """Handle a py-tgcalls `ChatUpdate` (VC closed / kicked / invited)."""
    try:
        from pytgcalls.types import ChatUpdate

        chat_id = getattr(update, "chat_id", None)
        status = getattr(update, "status", None)
        if chat_id is None or status is None:
            return

        # DUPLICATE FIX (REQUESTED: "vc invite 2 baar aata hai"):
        # Telegram already sends a `video_chat_members_invited` service
        # message, and melody/plugins/misc/vc_events.py posts the one and only
        # invite card for it. py-tgcalls' own INVITED_VOICE_CHAT update is the
        # exact same event, so it must stay silent here.
        if status & ChatUpdate.Status.INVITED_VOICE_CHAT:
            return

        # DUPLICATE FIX (REQUESTED: "vc start end bhi do baar aara"):
        # a closed voice chat also arrives as the `video_chat_ended` service
        # message, which vc_events.py answers with the proper "ended by" card.
        if status & ChatUpdate.Status.CLOSED_VOICE_CHAT:
            return
        if status & ChatUpdate.Status.KICKED:
            reason = "🚫 Aꜱꜱɪꜱᴛᴀɴᴛ ᴋᴏ ᴋɪᴄᴋ ᴋᴀʀ ᴅɪʏᴀ ɢᴀʏᴀ"
        elif status & ChatUpdate.Status.LEFT_GROUP:
            reason = "👋 Aꜱꜱɪꜱᴛᴀɴᴛ ɢʀᴏᴜᴘ ꜱᴇ ʟᴇғᴛ ʜᴏ ɢᴀʏᴀ"
        elif status & ChatUpdate.Status.DISCARDED_CALL:
            reason = "📞 Cᴀʟʟ ᴅɪꜱᴄᴀʀᴅ ʜᴏ ɢᴀʏɪ"
        else:
            return

        await _send(
            chat_id,
            _ended_card(reason),
            auto_delete_after=3,
        )
    except Exception as exc:
        LOGGER.debug("notify_chat_update failed: %s", exc)
