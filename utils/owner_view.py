"""
👑 Group-owner display rules — one place for the whole bot.

REQUESTED
---------
"pure code me jaha pe bhi gc owner vagera hoga — agr gc ka owner hidden hai
ya anonymous hai to show mt karna, bs bolna hidden."

So: any card, alert or log line that would print the group owner must call
`owner_text()` / `owner_label()` from here instead of building its own
mention. If the owner is anonymous (Telegram's "remain anonymous" admin
switch), deleted, or simply not visible to us, we print a neutral
`🕵 Hidden` badge and NEVER a name, id or clickable mention.
"""
from __future__ import annotations

import html

from melody.logging import LOGGER

HIDDEN = "🕵 Hɪᴅᴅᴇɴ"
UNKNOWN = "🕵 Hɪᴅᴅᴇɴ"


def _status_str(status) -> str:
    return str(getattr(status, "value", status) or "").lower()


def is_anonymous_member(member) -> bool:
    """True when this chat member deliberately hides behind the group.

    ROOT-CAUSE FIX ("/staff pe anonymous owner ka naam dikh raha hai"):
    Pyrogram 2.x does NOT put an `is_anonymous` attribute on ChatMember — the
    flag lives on `member.privileges.is_anonymous` (ChatPrivileges). The old
    `getattr(member, "is_anonymous", False)` therefore evaluated to False for
    EVERY admin, including a fully anonymous group creator, so the owner's
    name/mention leaked into /staff, /info and every other owner line. We now
    read the flag from both places (plus the raw `_raw` admin rights that some
    forks expose) so the Hidden badge actually kicks in.
    """
    if member is None:
        return True

    # 1) Direct flag (older / patched forks) …
    if getattr(member, "is_anonymous", False):
        return True

    # 2) … Pyrogram 2.x location: ChatMember.privileges.is_anonymous
    privileges = getattr(member, "privileges", None)
    if privileges is not None and bool(getattr(privileges, "is_anonymous", False)):
        return True

    # 3) … and the raw MTProto participant, when a fork exposes it.
    raw = getattr(member, "_raw", None) or getattr(member, "raw", None)
    if raw is not None:
        if bool(getattr(raw, "anonymous", False)):
            return True
        admin_rights = getattr(raw, "admin_rights", None)
        if admin_rights is not None and bool(getattr(admin_rights, "anonymous", False)):
            return True

    user = getattr(member, "user", None)
    if user is None:
        return True
    if getattr(user, "is_deleted", False):
        return True
    # Anonymous admins are delivered as the group itself (sender_chat).
    if getattr(member, "sender_chat", None) is not None:
        return True
    return False



def is_anonymous_message(message) -> bool:
    """True when an admin acted as the group/channel instead of as a user."""
    if message is None:
        return True
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None:
        return True
    return getattr(message, "from_user", None) is None


def owner_label(member) -> str:
    """Rendered owner for a card: a mention, or `🕵 Hidden`."""
    if is_anonymous_member(member):
        LOGGER.info(
            "owner_view: group owner is anonymous/hidden — showing '%s' instead "
            "of any identity", HIDDEN,
        )
        return HIDDEN
    user = member.user
    name = html.escape(str(getattr(user, "first_name", None) or "Owner"))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def owner_text(owner_id: "int | None", owner_name: "str | None" = None,
               anonymous: bool = False) -> str:
    """Same rule for places that only kept an id/name (e.g. the database)."""
    if anonymous or not owner_id:
        return HIDDEN
    name = html.escape(str(owner_name or "Owner"))
    return f'<a href="tg://user?id={int(owner_id)}">{name}</a>'


def pick_owner(members) -> "object | None":
    """Find the OWNER/creator entry inside an admin list (may be anonymous)."""
    for m in members:
        if _status_str(getattr(m, "status", "")) in ("owner", "creator") or str(
            getattr(m, "status", "")
        ).endswith("OWNER"):
            return m
    return None
