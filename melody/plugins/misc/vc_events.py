"""
🎙 Voice-chat service-message notifications (bot side).

REQUESTED CHANGES
  • "screen / camera / hand" states removed from the participant card
    (handled in melody/core/vc_notify.py).
  • Added "last kab join kiya tha" (previous-join timestamp).
  • Every VC notification auto-deletes after 3 seconds.
  • Each card carries a CLOSE button that removes it instantly.
  • VC start / end now clearly names WHO started and WHO ended it.
"""
import html
import time

from pyrogram import Client, enums, filters
from pyrogram.errors import ChatWriteForbidden, ChatAdminRequired, Forbidden
from pyrogram.types import Message

from melody import bot
from melody.logging import LOGGER, log_activity
from melody.core.vc_theme import FLAGS, tag_list, vc_card
from utils.admin_tools import auto_delete, close_kb, human_delta
from utils.gc_db import get_global, set_global
from utils.tasks import spawn

VC_AUTO_DELETE = 3
# REQUESTED: the "members invited to VC" card must NEVER be auto-deleted.
INVITE_AUTO_DELETE = 0

# ── DUPLICATE GUARD (REQUESTED: "vc invite 2 baar aata hai", "vc start end bhi
# do baar aara"). Telegram can re-deliver the same service message (session
# resync / two update streams), and py-tgcalls raises its own ChatUpdate for
# the very same event. Every card below goes through this guard, so one real
# event = exactly one card.
_DEDUPE_WINDOW = 25.0
_recent: dict = {}


def _fresh(chat_id: int, kind: str, message_id=None) -> bool:
    now = time.time()
    for key, stamp in list(_recent.items()):
        if now - stamp > 300:
            _recent.pop(key, None)
    if message_id is not None and _recent.get((chat_id, kind, message_id)):
        return False
    last = _recent.get((chat_id, kind), 0.0)
    if now - last < _DEDUPE_WINDOW:
        return False
    _recent[(chat_id, kind)] = now
    if message_id is not None:
        _recent[(chat_id, kind, message_id)] = now
    return True


def _tag(user_id: int, name: str, username=None) -> str:
    """Clickable mention + @username + numeric id (REQUESTED: dono mention)."""
    handle = f"@{username}" if username else "—"
    return (
        f'<a href="tg://user?id={int(user_id)}">{html.escape(str(name or "User"))}</a> '
        f'· <code>{html.escape(handle)}</code> · <code>{int(user_id)}</code>'
    )


def _who(message: Message) -> str:
    user = message.from_user
    if user:
        return _tag(user.id, user.first_name or "User", user.username)
    sender = message.sender_chat
    if sender:
        handle = f"@{sender.username}" if sender.username else "—"
        return (
            f"<b>{html.escape(sender.title or 'Channel')}</b> "
            f"· <code>{html.escape(handle)}</code> · <code>{sender.id}</code>"
        )
    return "🕵️ <b>Unknown actor</b>"


async def _send(message: Message, text: str, seconds: int = VC_AUTO_DELETE):
    try:
        sent = await message.reply(
            text, parse_mode=enums.ParseMode.HTML, reply_markup=close_kb()
        )
    except (ChatWriteForbidden, ChatAdminRequired, Forbidden) as exc:
        # Service notifications are optional. A muted/readonly chat must not
        # turn a Telegram permission restriction into a dispatcher traceback.
        LOGGER.info("VC notification skipped in %s: %s", message.chat.id, exc)
        return None
    except Exception as exc:
        LOGGER.warning("VC notification failed in %s: %s", message.chat.id, exc)
        return None
    if seconds:
        await auto_delete(sent, seconds)
    return sent


@bot.on_message(filters.video_chat_started & filters.group)
async def vc_started(client: Client, message: Message):
    chat_id = message.chat.id
    if not _fresh(chat_id, "started", message.id):
        return
    # 🛰 A voice chat just opened: start watching it from OUTSIDE the call
    # (roster polling) so chat log and join/left cards work with no music and
    # without the assistant ever joining the VC.
    try:
        from melody.core import vc_listener

        vc_listener.watch(chat_id)
        spawn(vc_listener.ensure(chat_id))
    except Exception:
        pass
    last = await get_global(f"vc_last_{chat_id}")
    await set_global(f"vc_last_{chat_id}", time.time())

    await _send(message, vc_card(
        "Started Video Chat",
        f"🟢 <b>Sᴛᴀᴛᴜs :</b> Voice chat live\n"
        f"🎙 <b>Sᴛᴀʀᴛᴇᴅ ʙʏ :</b> {_who(message)}\n"
        f"🏠 <b>Cʜᴀᴛ :</b> {html.escape((message.chat.title or 'Group')[:60])}\n"
        f"🕒 <b>Lᴀsᴛ VC :</b> {human_delta(last)}\n"
        f"💬 <b>Cʜᴀᴛ ʟᴏɢ :</b> ᴀᴜᴛᴏ-ᴀʀᴍᴇᴅ (ɴᴀ ᴍᴜsɪᴄ ᴄʜᴀʜɪᴇ, ɴᴀ ʙᴏᴛ ᴠᴄ ᴍᴇ)",
        footer=f"{FLAGS} · <code>/play &lt;song&gt;</code> ʙʜᴇᴊᴏ 🎧",
        tags="#SᴛᴀʀᴛᴇᴅVɪᴅᴇᴏCʜᴀᴛ 🟢",
    ))
    spawn(log_activity(
        f"#vcstarted\n🏠 {html.escape(message.chat.title or '')} (<code>{chat_id}</code>)\n"
        f"👤 Started by: {_who(message)}"
    ))


@bot.on_message(filters.video_chat_ended & filters.group)
async def vc_ended(client: Client, message: Message):
    if not _fresh(message.chat.id, "ended", message.id):
        return
    try:
        from melody.core.call import stop_stream
        await stop_stream(message.chat.id)
    except Exception:
        pass
    try:
        from melody.core.vc_notify import clear_roster
        clear_roster(message.chat.id)
    except Exception:
        pass
    try:
        from melody.core import vc_listener
        await vc_listener.release(message.chat.id)
    except Exception:
        pass

    started = await get_global(f"vc_last_{message.chat.id}")
    lasted = human_delta(started) if started else "—"

    await _send(message, vc_card(
        "Ended Video Chat",
        f"🔴 <b>Sᴛᴀᴛᴜs :</b> Voice chat ended\n"
        f"🙅 <b>Eɴᴅᴇᴅ ʙʏ :</b> {_who(message)}\n"
        f"⏳ <b>Dᴜʀᴀᴛɪᴏɴ :</b> {lasted}\n"
        f"🧹 <b>Qᴜᴇᴜᴇ :</b> Cleared ✅",
        footer=f"{FLAGS} · ᴅᴏʙᴀʀᴀ VC sᴛᴀʀᴛ ᴋᴀʀᴋᴇ <code>/play</code> ᴋᴀʀᴏ 🎵",
        tags="#EɴᴅᴇᴅVɪᴅᴇᴏCʜᴀᴛ 🔴",
    ))
    spawn(log_activity(
        f"#vcended\n🏠 {html.escape(message.chat.title or '')} (<code>{message.chat.id}</code>)\n"
        f"👤 Ended by: {_who(message)}"
    ))


@bot.on_message(filters.video_chat_members_invited & filters.group)
async def vc_invited(client: Client, message: Message):
    """VC invite card — REQUESTED: never deleted, and every invited user is
    tagged with a real clickable mention."""
    invited = getattr(message.video_chat_members_invited, "users", None) or []
    ids = ",".join(str(getattr(u, "id", "")) for u in invited)
    if not _fresh(message.chat.id, f"invited:{ids}", message.id):
        return
    # An invite means a VC is (about to be) live here: arm the outside watcher
    # so the chat log works even if no music is ever played.
    try:
        from melody.core import vc_listener

        vc_listener.watch(message.chat.id)
        spawn(vc_listener.ensure(message.chat.id))
    except Exception:
        pass

    mentions = "\n".join(
        f"   🔸 {_tag(u.id, u.first_name or 'User', getattr(u, 'username', None))}"
        for u in invited[:12] if getattr(u, "id", None)
    )
    # REQUESTED ("vc invite utna rakh bs"): no ribbon headline, no footer —
    # just the hashtag, who invited, and the invited list.
    await _send(
        message,
        "#IɴᴠɪᴛᴇᴅVɪᴅᴇᴏCʜᴀᴛ 📨\n"
        f"🙋 <b>Iɴᴠɪᴛᴇᴅ ʙʏ :</b> {_who(message)}\n"
        f"👥 <b>Iɴᴠɪᴛᴇᴅ ({len(invited)}) :</b>\n"
        + (mentions or tag_list(invited)),
        seconds=INVITE_AUTO_DELETE,
    )
