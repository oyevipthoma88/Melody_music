"""
🛡️ Shared group-administration helpers (Melody theme).

BUG FIX (the reported "ban / unban properly work na krti"):
    The old check was `member.status in ("administrator", "creator")`.
    Pyrogram **2.x** returns a `ChatMemberStatus` *enum*, not those legacy
    Pyrogram-1.x strings, so that comparison was ALWAYS False — every admin
    got "⚠️ Admins only." and /ban, /unban and friends silently did nothing.
    `is_admin()` below compares against the real enum members (with a string
    fallback) so it works on every Pyrogram build.
"""
from utils.buttons import ikb  # premium-emoji + styled buttons (safe on every fork)
import asyncio
import html
import inspect
import logging
import time

from pyrogram import enums
from pyrogram.errors import RPCError
from pyrogram.types import (
    ChatPermissions,
    ChatPrivileges,
    InlineKeyboardMarkup,
    Message,
)

from melody.config import Config
from utils.gc_db import is_sudo
from utils.client_cache import get_me_cached

ADMIN_STATUSES = (
    enums.ChatMemberStatus.OWNER,
    enums.ChatMemberStatus.ADMINISTRATOR,
)

log = logging.getLogger(__name__)

# Every permission revoked → a fully muted member.
MUTED_PERMS = ChatPermissions(can_send_messages=False)

# A sane "normal member" permission set used by /unmute + /unmuteall.
#
# BUG FIX (12 plugins failed to load: ban, gcmanage, massactions, protection,
# sudo, antiabuse, pics, ping, alive, promo, vc_events, channelplay):
#     this module was built with `ChatPermissions(can_send_other_messages=...)`,
#     a Pyrogram-1.x/Bot-API name that pyrofork 2.3.x does NOT accept — it
#     splits that flag into can_send_stickers / can_send_gifs / can_send_games
#     / can_send_inline. The keyword raised TypeError at MODULE IMPORT time, so
#     `utils.admin_tools` never imported and every plugin importing it was
#     dropped by load_plugins() — which is why /ban, /unban and the whole group
#     -management suite "did nothing": their handlers were never registered.
#
#     `_perms()` now filters kwargs against the installed ChatPermissions
#     signature, so this can never again turn a renamed flag into a
#     plugin-wide import failure on any Pyrogram/pyrofork build.
_PERM_FIELDS = set(inspect.signature(ChatPermissions.__init__).parameters)


def _perms(**kwargs) -> ChatPermissions:
    """Build ChatPermissions with only the flags this Pyrogram build knows."""
    unknown = [k for k in kwargs if k not in _PERM_FIELDS]
    if unknown:
        log.debug("admin_tools: ignoring unsupported ChatPermissions flags %s", unknown)
    return ChatPermissions(**{k: v for k, v in kwargs.items() if k in _PERM_FIELDS})


UNMUTED_PERMS = _perms(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_plain=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_audios=True,
    can_send_docs=True,
    can_send_voices=True,
    can_send_roundvideos=True,
    can_send_stickers=True,
    can_send_gifs=True,
    can_send_games=True,
    can_send_inline=True,
    can_send_other_messages=True,  # legacy alias, filtered out when absent
    can_send_polls=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
    can_change_info=False,
    can_pin_messages=False,
)


def mention(user) -> str:
    """HTML mention that never breaks the parser."""
    if user is None:
        return "<i>Unknown</i>"
    uid = getattr(user, "id", None) or 0
    name = getattr(user, "first_name", None) or getattr(user, "title", None) or "User"
    return f'<a href="tg://user?id={uid}">{html.escape(str(name))}</a>'


def mention_id(user_id: int, name: str = "User") -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(str(name))}</a>'


def close_kb(extra: list | None = None) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    rows.append([ikb("✵ CLOSE ✵", callback_data="gc_close")])
    return InlineKeyboardMarkup(rows)


def card(title: str, body: str, footer: str = "") -> str:
    """Standard Melody card — same Modi–Meloni tricolour frame as every other
    card in the bot (see utils/melody_theme.py). Admin/owner/misc cards used
    to render a different, plainer header; now there is exactly one look."""
    from utils.melody_theme import card as themed_card

    return themed_card(title, body, footer=footer)


async def get_status(client, chat_id: int, user_id: int):
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member
    except Exception:
        return None


def _is_admin_status(status) -> bool:
    if status in ADMIN_STATUSES:
        return True
    return str(status) in (
        "ChatMemberStatus.OWNER",
        "ChatMemberStatus.ADMINISTRATOR",
        "creator",
        "administrator",
    )


async def is_admin(client, chat_id: int, user_id: int) -> bool:
    """True for group owner, admins, bot owner and sudo users."""
    if user_id == Config.OWNER_ID or await is_sudo(user_id):
        return True
    member = await get_status(client, chat_id, user_id)
    return bool(member) and _is_admin_status(member.status)


async def is_real_admin(client, chat_id: int, user_id: int) -> bool:
    """True ONLY for actual Telegram group admins/owner — not sudo/owner override.

    Used by /promote to avoid the false "already admin" when a sudo user
    who is NOT a group admin is targeted for promotion.
    """
    member = await get_status(client, chat_id, user_id)
    return bool(member) and _is_admin_status(member.status)


async def is_group_owner(client, chat_id: int, user_id: int) -> bool:
    """True ONLY for the actual group creator (plus bot owner / sudo, who are
    intentionally allowed everywhere)."""
    if user_id == Config.OWNER_ID or await is_sudo(user_id):
        return True
    member = await get_status(client, chat_id, user_id)
    if not member:
        return False
    return member.status == enums.ChatMemberStatus.OWNER or str(member.status) in (
        "ChatMemberStatus.OWNER",
        "creator",
    )


async def extract_target(client, message: Message, arg_index: int = 1):
    """Resolve the target user from: a reply, @username, a user id, or a
    text_mention entity. Returns a `User`-like object or None."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user

    # tg://user?id= text mentions
    for entity in (message.entities or []):
        if entity.type == enums.MessageEntityType.TEXT_MENTION and entity.user:
            return entity.user

    parts = (message.command or [])[arg_index:]
    if not parts:
        return None
    ref = parts[0].strip()
    if ref.startswith("@"):
        ref = ref[1:]
    try:
        return await client.get_users(int(ref) if ref.lstrip("-").isdigit() else ref)
    except Exception:
        return None


def extract_reason(message: Message, start: int = 1) -> str:
    """Everything after the target (or after the command when replying)."""
    parts = (message.command or [])[start:]
    if message.reply_to_message:
        return " ".join(parts).strip()
    return " ".join(parts[1:]).strip()


async def protected_target(client, message: Message, user, allow_admin: bool = False,
                           positive: bool = False) -> "str | None":
    """Return a refusal reason if `user` must never be acted on, else None.

    BUG FIX ("bot demote nahi krta admin ko"): /demote's whole job is to act
    ON an admin, but this guard unconditionally answered "target is an admin —
    demote them first", so /demote refused every single target and the command
    was dead. Callers that legitimately target admins (demote) pass
    `allow_admin=True`; everyone else keeps the old protection.
    """
    if user is None:
        return None
    # BUG FIX ("🎼 Pʀᴏᴍᴏᴛᴇ Fᴀɪʟᴇᴅ — Yᴇ ᴍᴇʀᴇ ᴏᴡɴᴇʀ ʜᴀɪ, action denied"):
    # this guard exists to stop PUNITIVE actions (ban/mute/kick/demote) from
    # hitting the bot owner, sudo users or the bot itself. /promote is the
    # opposite of punitive — refusing to promote the owner made the command
    # useless for exactly the people who own the bot. Callers doing a
    # non-punitive action pass positive=True and skip the whole shield;
    # their own follow-up checks ("already admin", rights model) still apply.
    if positive:
        return None
    if user.id == Config.OWNER_ID:
        return "👑 <b>Yᴇ ᴍᴇʀᴇ ᴏᴡɴᴇʀ ʜᴀɪ</b> — action denied."
    if await is_sudo(user.id):
        return "⚡ <b>Sᴜᴅᴏ ᴜsᴇʀ</b> — action denied."
    me = await get_me_cached(client)
    if user.id == me.id:
        return "🎶 <b>Mᴜᴊʜ ᴘᴇ ʜɪ ?</b> Nice try 😌"
    member = await get_status(client, message.chat.id, user.id)
    if member and _is_admin_status(member.status):
        if not allow_admin:
            return "🛡 <b>Tᴀʀɢᴇᴛ ɪs ᴀɴ ᴀᴅᴍɪɴ</b> — demote them first."
        if member.status == enums.ChatMemberStatus.OWNER or str(member.status) in (
            "ChatMemberStatus.OWNER", "creator",
        ):
            return "👑 <b>Yᴇ ɢʀᴏᴜᴘ ᴋᴀ ᴏᴡɴᴇʀ ʜᴀɪ</b> — inhe koi demote nahi kar sakta."
    return None


# ── Rights model (promote / demote) ──────────────────────────────────────────
#
# REQUESTED
#   • "promote ke tym pe new admin ryt + jitni ryt hogi bs utni hi ryt new
#     admin ko jayegi"  → a promoter can never hand out a right they do not
#     hold themselves (nor one the bot lacks).
#   • "users ek dusre ko tbhi demote kr skte hai jb unke pass full ryts ho"
#     → demoting another admin needs the FULL right set (or group owner /
#     sudo / bot owner), except when you are demoting someone you yourself
#     promoted through this bot.
RIGHT_KEYS = (
    "can_manage_chat",
    "can_delete_messages",
    "can_manage_video_chats",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
    "can_invite_users",
    "can_pin_messages",
    "can_manage_topics",
    "can_post_messages",
    "can_edit_messages",
    "can_post_stories",
    "can_edit_stories",
    "can_delete_stories",
)

# The subset that actually defines "full rights" — story/topic/channel-only
# flags are not present in every group, so requiring them would make "full
# rights" unreachable for a normal supergroup admin.
CORE_RIGHT_KEYS = (
    "can_delete_messages",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
    "can_invite_users",
    "can_pin_messages",
    "can_manage_video_chats",
)


async def privileges_of(client, chat_id: int, user_id: int):
    """The ChatPrivileges of a member, or None when they are not an admin."""
    member = await get_status(client, chat_id, user_id)
    if not member or not _is_admin_status(member.status):
        return None
    return getattr(member, "privileges", None)


async def is_full_rights(client, chat_id: int, user_id: int) -> bool:
    """True for the group owner, bot owner, sudo, or an admin holding every
    core right (including 'add new admins')."""
    if user_id == Config.OWNER_ID or await is_sudo(user_id):
        return True
    if await is_group_owner(client, chat_id, user_id):
        return True
    privileges = await privileges_of(client, chat_id, user_id)
    if privileges is None:
        return False
    return all(bool(getattr(privileges, key, False)) for key in CORE_RIGHT_KEYS)


_BG_TASKS: set = set()


async def auto_delete(msg, seconds: int = 120) -> None:
    """Delete a sent message after `seconds` (fire-and-forget, never raises).

    BUG FIX: the task was created without keeping a reference, so CPython was
    free to garbage-collect it mid-sleep and the message would never be
    deleted. We now hold a strong ref until the task finishes.
    """
    async def _worker():
        try:
            await asyncio.sleep(seconds)
        except Exception:
            return
        # REQUESTED ("auto 3 sec me MUST delete"): one failed delete used to
        # leave the card in the group forever (flood-wait, transient RPC
        # error). Retry a few times, then fall back to the raw client call.
        for attempt in range(4):
            try:
                await msg.delete()
                return
            except Exception:
                try:
                    await asyncio.sleep(1.5 * (attempt + 1))
                except Exception:
                    return
        try:
            from melody import bot
            await bot.delete_messages(msg.chat.id, msg.id)
        except Exception:
            pass

    task = asyncio.create_task(_worker())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)



def human_delta(ts: "float | int | None") -> str:
    """'2h 14m ago' style relative time; '—' when unknown."""
    if not ts:
        return "—"
    delta = max(0, int(time.time() - float(ts)))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h {(delta % 3600) // 60}m ago"
    return f"{delta // 86400}d ago"


FULL_PRIVILEGES = ChatPrivileges(
    can_manage_chat=True,
    can_delete_messages=True,
    can_manage_video_chats=True,
    can_restrict_members=True,
    can_promote_members=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
)

BASIC_PRIVILEGES = ChatPrivileges(
    can_manage_chat=True,
    can_delete_messages=True,
    can_manage_video_chats=True,
    can_restrict_members=True,
    can_promote_members=False,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=True,
)


async def safe_call(coro):
    """Await a Telegram call, returning (ok, error_text)."""
    try:
        await coro
        return True, ""
    except RPCError as exc:
        return False, html.escape(str(exc))
    except Exception as exc:  # noqa: BLE001
        return False, html.escape(str(exc))
