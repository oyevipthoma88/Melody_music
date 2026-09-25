"""
🛡️ Group management — the full Melody admin suite.

Commands (group admins / sudo unless stated):
    /ban /unban /kick /dkick        — remove members
    /mute /unmute /tmute            — restrict members
    /promote /fpromote /demote      — admin rights (fpromote = full rights)
    /purge /spurge /del             — delete messages (any age)
    /pin /unpin /unpinall           — pinning
    /warn /warns /resetwarns        — warn system (3 warns → mute)
    /admins /@admins /report        — tag the admin team
    /tagall /cancel                 — tag everyone
    /invitelink /adminlist /info    — utilities
    /lock /unlock                   — lock the chat (default permissions)

All of them target: a reply, an @username, a user id, or a text mention.
"""
import asyncio
import html

from pyrogram import Client, enums, filters
from pyrogram.types import CallbackQuery, ChatPermissions, Message

from melody import bot
from melody.config import Config
from utils.client_cache import get_me_cached
from melody.logging import log_activity
from utils.admin_tools import (
    BASIC_PRIVILEGES,
    FULL_PRIVILEGES,
    MUTED_PERMS,
    UNMUTED_PERMS,
    auto_delete,
    card,
    close_kb,
    extract_reason,
    extract_target,
    is_admin,
    is_real_admin,
    mention,
    protected_target,
    safe_call,
)
from utils.decorators import error_handler, invalidate_admin_cache
from utils.admin_tools import is_full_rights, is_group_owner, privileges_of
from utils.gc_db import (
    add_warn,
    is_sudo,
    get_bot_promoted,
    get_warns,
    mark_bot_promoted,
    reset_warns,
    unmark_bot_promoted,
)
from utils.tasks import spawn

WARN_LIMIT = 3
_tagall_cancel: set = set()


async def _deny(message: Message):
    m = await message.reply(
        card("Aᴄᴄᴇss Dᴇɴɪᴇᴅ", "⚠️ <b>Sɪʀғ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴs</b> ye command chala sakte hain."),
        parse_mode=enums.ParseMode.HTML,
    )
    await auto_delete(m, 30)


async def _need_target(message: Message, usage: str):
    m = await message.reply(
        card("Usᴀɢᴇ", f"🧾 <code>{html.escape(usage)}</code>\n\n<i>Reply, @username, user id ya mention — teeno chalte hain.</i>"),
        parse_mode=enums.ParseMode.HTML,
    )
    await auto_delete(m, 45)


def _gate(func):
    """Admin gate shared by every command in this module."""
    async def wrapper(client: Client, message: Message):
        if not message.from_user:
            return
        if not await is_admin(client, message.chat.id, message.from_user.id):
            return await _deny(message)
        return await func(client, message)

    wrapper.__name__ = func.__name__
    return wrapper


# ─── Ban / unban / kick ──────────────────────────────────────────────────────

@bot.on_message(filters.command(["ban", "gcban"]) & filters.group)
@error_handler
@_gate
async def ban_cmd(client: Client, message: Message):
    user = await extract_target(client, message)
    if not user:
        return await _need_target(message, "/ban @user [reason]")
    blocked = await protected_target(client, message, user)
    if blocked:
        return await message.reply(card("Bᴀɴ", blocked), parse_mode=enums.ParseMode.HTML)

    reason = extract_reason(message) or "Not specified"
    ok, err = await safe_call(client.ban_chat_member(message.chat.id, user.id))
    if not ok:
        return await message.reply(
            card("Bᴀɴ Fᴀɪʟᴇᴅ", f"❌ <code>{err}</code>\n\n<i>Kya mujhe ban karne ka right diya hai?</i>"),
            parse_mode=enums.ParseMode.HTML,
        )
    await message.reply(
        card(
            "Usᴇʀ Bᴀɴɴᴇᴅ",
            f"🔨 <b>Usᴇʀ :</b> {mention(user)}\n"
            f"🆔 <b>ID :</b> <code>{user.id}</code>\n"
            f"👮 <b>Bʏ :</b> {mention(message.from_user)}\n"
            f"📝 <b>Rᴇᴀsᴏɴ :</b> {html.escape(reason)}",
            "Melody keeps this group clean 🎶",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(
        f"#ban #admin\n👤 {mention(user)} (<code>{user.id}</code>)\n"
        f"👮 By {mention(message.from_user)}\n🏠 <code>{message.chat.id}</code>\n📝 {html.escape(reason)}"
    ))


@bot.on_message(filters.command(["unban", "gcunban"]) & filters.group)
@error_handler
@_gate
async def unban_cmd(client: Client, message: Message):
    user = await extract_target(client, message)

    # BUG FIX (requested "/unban numeric id"): a banned account is often not
    # resolvable via get_users() (no shared chat / privacy), so extract_target
    # returned None and the command just printed usage. Fall back to using the
    # raw numeric id — unban_chat_member() accepts it directly.
    raw_id = None
    if user is None:
        arg = (message.command or [None, None])[1]
        if arg and arg.lstrip("-").isdigit():
            raw_id = int(arg)
        else:
            return await _need_target(message, "/unban @user | /unban 123456789")

    target_id = user.id if user else raw_id
    ok, err = await safe_call(client.unban_chat_member(message.chat.id, target_id))
    if not ok:
        return await message.reply(card("Uɴʙᴀɴ Fᴀɪʟᴇᴅ", f"❌ <code>{err}</code>"), parse_mode=enums.ParseMode.HTML)

    # REQUESTED: if the unbanned account is Melody's voice assistant, bring it
    # straight back into the group + voice chat instead of making the user
    # re-run /play.
    try:
        from melody.core.call import _get_assistant_id, _auto_join_assistant

        if target_id == await _get_assistant_id():
            spawn(_auto_join_assistant(message.chat.id))
    except Exception:
        pass

    if user is None:
        return await message.reply(
            card("Usᴇʀ Uɴʙᴀɴɴᴇᴅ", f"✅ <b>Iᴅ :</b> <code>{target_id}</code>\n👮 <b>Bʏ :</b> {mention(message.from_user)}"),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=close_kb(),
        )
    await message.reply(
        card(
            "Usᴇʀ Uɴʙᴀɴɴᴇᴅ",
            f"✅ <b>Usᴇʀ :</b> {mention(user)}\n👮 <b>Bʏ :</b> {mention(message.from_user)}",
            "Wapas aa jao, gaane sunte hain 🎧",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(
        f"#unban #admin\n👤 {mention(user)} (<code>{user.id}</code>)\n👮 By {mention(message.from_user)}"
    ))


@bot.on_message(filters.command(["kick", "dkick", "punch"]) & filters.group)
@error_handler
@_gate
async def kick_cmd(client: Client, message: Message):
    user = await extract_target(client, message)
    if not user:
        return await _need_target(message, "/kick @user")
    blocked = await protected_target(client, message, user)
    if blocked:
        return await message.reply(card("Kɪᴄᴋ", blocked), parse_mode=enums.ParseMode.HTML)
    ok, err = await safe_call(client.ban_chat_member(message.chat.id, user.id))
    if ok:
        await asyncio.sleep(1)
        await safe_call(client.unban_chat_member(message.chat.id, user.id))
    if message.command[0] == "dkick" and message.reply_to_message:
        await safe_call(message.reply_to_message.delete())
    if not ok:
        return await message.reply(card("Kɪᴄᴋ Fᴀɪʟᴇᴅ", f"❌ <code>{err}</code>"), parse_mode=enums.ParseMode.HTML)
    await message.reply(
        card("Usᴇʀ Kɪᴄᴋᴇᴅ", f"👢 {mention(user)} ko group se nikal diya.\n👮 <b>Bʏ :</b> {mention(message.from_user)}"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(f"#kick #admin\n👤 <code>{user.id}</code> by <code>{message.from_user.id}</code>"))


# ─── Mute / unmute ───────────────────────────────────────────────────────────

@bot.on_message(filters.command(["mute", "gcmute"]) & filters.group)
@error_handler
@_gate
async def mute_cmd(client: Client, message: Message):
    user = await extract_target(client, message)
    if not user:
        # No target → this is the MUSIC /mute (stream mute), handled by
        # melody/plugins/music/volume.py. Stay silent so the two commands
        # can share one name without double-replying.
        return
    blocked = await protected_target(client, message, user)
    if blocked:
        return await message.reply(card("Mᴜᴛᴇ", blocked), parse_mode=enums.ParseMode.HTML)
    ok, err = await safe_call(client.restrict_chat_member(message.chat.id, user.id, MUTED_PERMS))
    if not ok:
        return await message.reply(card("Mᴜᴛᴇ Fᴀɪʟᴇᴅ", f"❌ <code>{err}</code>"), parse_mode=enums.ParseMode.HTML)
    await message.reply(
        card(
            "Usᴇʀ Mᴜᴛᴇᴅ",
            f"🔇 <b>Usᴇʀ :</b> {mention(user)}\n👮 <b>Bʏ :</b> {mention(message.from_user)}\n"
            f"📝 <b>Rᴇᴀsᴏɴ :</b> {html.escape(extract_reason(message) or 'Not specified')}",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(f"#mute #admin\n👤 <code>{user.id}</code> by <code>{message.from_user.id}</code>"))


@bot.on_message(filters.command(["unmute", "gcunmute"]) & filters.group)
@error_handler
@_gate
async def unmute_cmd(client: Client, message: Message):
    user = await extract_target(client, message)
    if not user:
        # No target → this is the MUSIC /unmute (stream unmute), handled by
        # melody/plugins/music/volume.py. Stay silent so the two commands
        # can share one name without double-replying.
        return
    ok, err = await safe_call(client.restrict_chat_member(message.chat.id, user.id, UNMUTED_PERMS))
    if not ok:
        return await message.reply(card("Uɴᴍᴜᴛᴇ Fᴀɪʟᴇᴅ", f"❌ <code>{err}</code>"), parse_mode=enums.ParseMode.HTML)
    await message.reply(
        card("Usᴇʀ Uɴᴍᴜᴛᴇᴅ", f"🔈 {mention(user)} ab bol sakta hai.\n👮 <b>Bʏ :</b> {mention(message.from_user)}"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(f"#unmute #admin\n👤 <code>{user.id}</code>"))


@bot.on_message(filters.command("tmute") & filters.group)
@error_handler
@_gate
async def tmute_cmd(client: Client, message: Message):
    """/tmute @user 10m|2h|1d — temporary mute."""
    import time as _time

    user = await extract_target(client, message)
    if not user:
        return await _need_target(message, "/tmute @user 10m")
    raw = extract_reason(message).split(" ")[0] if extract_reason(message) else "10m"
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        seconds = int(raw[:-1]) * units[raw[-1].lower()]
    except Exception:
        seconds = 600
    until = int(_time.time()) + seconds
    ok, err = await safe_call(client.restrict_chat_member(message.chat.id, user.id, MUTED_PERMS, until_date=until))
    if not ok:
        return await message.reply(card("Tᴍᴜᴛᴇ Fᴀɪʟᴇᴅ", f"❌ <code>{err}</code>"), parse_mode=enums.ParseMode.HTML)
    await message.reply(
        card("Tᴇᴍᴘ Mᴜᴛᴇ", f"⏳ {mention(user)} muted for <code>{html.escape(raw)}</code>."),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )



# ─── Promote/demote sanity guards ────────────────────────────────────────────
#
# BUG ("bot ka owner khud bot ke through promote ho skta hun" + "bot, bot ko
# promote/demote kar deta hai"): nothing stopped a user from targeting
# THEMSELVES (self-promotion — anyone with the command could hand themselves
# rights they were never given) and nothing stopped bot accounts from being
# promoted/demoted by any admin.
#
# Rules now enforced for /promote, /fpromote and /demote:
#   1. Self-target is ALWAYS refused — bot owner and sudo included. Rights must
#      come from someone else, otherwise the bot is a privilege-escalation tool.
#   2. Melody itself can never be promoted/demoted through its own commands.
#   3. Other BOT accounts can only be promoted/demoted by the group owner, the
#      bot owner or a sudo user — and a bot never receives "can_promote_members"
#      (a bot that can promote can rebuild any admin it just lost).
async def _promote_sanity(client: Client, message: Message, user, action: str) -> "str | None":
    actor = message.from_user.id if message.from_user else 0

    if user.id == actor:
        # FIX (reported): the blanket self-target refusal also blocked the
        # people who already hold every right by definition — the group owner,
        # the bot owner and sudo users. For them a self-promote is not a
        # privilege escalation (they cannot gain anything they do not have),
        # so it is allowed. Normal admins are still refused.
        privileged = (
            actor == Config.OWNER_ID
            or await is_sudo(actor)
            or await is_group_owner(client, message.chat.id, actor)
        )
        if not privileged:
            return (
                f"🙅 <b>Khud ko {action} nahi kar sakte.</b>\n\n"
                "<i>Rights kisi dusre admin se lo — apne aap ko rights dena "
                "sirf group owner / bot owner / sudo kar sakte hain.</i>"
            )

    try:
        me = await get_me_cached(client)
        if user.id == me.id:
            return "🎶 <b>Mujhe hi ?</b> Apne aap ko promote/demote nahi karta 😌"
    except Exception:
        pass

    if getattr(user, "is_bot", False):
        privileged = (
            actor == Config.OWNER_ID
            or await is_sudo(actor)
            or await is_group_owner(client, message.chat.id, actor)
        )
        if not privileged:
            return (
                "🤖 <b>Bot ko " + action + " karna sirf group owner / sudo kar sakte hain.</b>\n\n"
                "<i>Normal admins bots ke admin rights nahi badal sakte.</i>"
            )
    return None


# ─── Promote / fpromote / demote ─────────────────────────────────────────────

# Every right Telegram can hand to a supergroup admin. Kept as a plain tuple so
# the code survives Pyrogram forks that add/remove fields.
_RIGHT_KEYS = (
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
    "is_anonymous",
)

# Auto titles (Telegram hard-limits custom titles to 16 characters).
_AUTO_TITLE_FULL = "👑 Co-Owner"
_AUTO_TITLE_BASIC = "⭐ Admin"


async def _grantable(client: Client, chat_id: int, wanted,
                     promoter_id: "int | None" = None) -> "tuple[object, list[str], list[str]]":
    """Intersect the requested rights with what Melody AND the promoter hold.

    Telegram silently refuses (or errors on) any right the promoting bot does
    not own, so we compute the real set first and report exactly what was given
    and what was skipped instead of promising powers the admin never got.

    REQUESTED ("promote ke tym pe new admin ryt + jitni ryt hogi bs utni hi
    ryt new admin ko jayegi"): the human running /promote can never hand out a
    right they do not hold themselves either. The group owner, bot owner and
    sudo users are exempt — they already hold everything by definition.
    """
    from pyrogram.types import ChatPrivileges

    mine = None
    try:
        me = await get_me_cached(client)
        member = await client.get_chat_member(chat_id, me.id)
        mine = getattr(member, "privileges", None)
    except Exception:
        mine = None

    # The promoter's own ceiling (None = unlimited: owner / sudo / bot owner).
    promoter = None
    if promoter_id is not None:
        try:
            if not await is_full_rights(client, chat_id, promoter_id):
                promoter = await privileges_of(client, chat_id, promoter_id)
        except Exception:
            promoter = None

    granted, skipped, kwargs = [], [], {}
    for key in _RIGHT_KEYS:
        want = getattr(wanted, key, None)
        if want is None:
            continue
        if key == "is_anonymous":
            kwargs[key] = bool(want)
            continue
        if not want:
            kwargs[key] = False
            continue
        have = True if mine is None else bool(getattr(mine, key, False))
        if have and promoter is not None and not bool(getattr(promoter, key, False)):
            # The promoter does not hold this right — it cannot be passed on.
            have = False
        kwargs[key] = have
        (granted if have else skipped).append(key)

    # Never leave the admin with literally nothing.
    kwargs["can_manage_chat"] = True
    try:
        privileges = ChatPrivileges(**kwargs)
    except TypeError:
        # Fork without one of the newer fields — drop unknown keys and retry.
        safe = {}
        for key, value in kwargs.items():
            try:
                ChatPrivileges(**{key: value})
                safe[key] = value
            except TypeError:
                continue
        privileges = ChatPrivileges(**safe)
    return privileges, granted, skipped


def _pretty_right(key: str) -> str:
    return key.replace("can_", "").replace("_", " ").title()


async def _do_promote(client: Client, message: Message, full: bool):
    user = await extract_target(client, message)
    if not user:
        return await _need_target(message, "/promote @user | userid | reply")

    sanity = await _promote_sanity(client, message, user, "promote")
    if sanity:
        return await message.reply(
            card("Pʀᴏᴍᴏᴛᴇ Fᴀɪʟᴇᴅ", f"⚠️ {sanity}"), parse_mode=enums.ParseMode.HTML
        )

    # positive=True: promoting is not a punitive action, so the owner/sudo/
    # admin shield must not block it (that shield was rejecting /promote with
    # "Yᴇ ᴍᴇʀᴇ ᴏᴡɴᴇʀ ʜᴀɪ — action denied"). Only the bot itself stays out.
    guard = await protected_target(client, message, user, positive=True)
    if guard:
        return await message.reply(
            card("Pʀᴏᴍᴏᴛᴇ Fᴀɪʟᴇᴅ", f"⚠️ {guard}"), parse_mode=enums.ParseMode.HTML
        )
    if await is_real_admin(client, message.chat.id, user.id):
        return await message.reply(
            card("Aʟʀᴇᴀᴅʏ Aᴅᴍɪɴ",
                 f"ℹ️ {mention(user)} <b>pehle se admin hai.</b>\n\n"
                 "<i>Rights badalne ke liye pehle</i> <code>/demote</code> <i>karo.</i>"),
            parse_mode=enums.ParseMode.HTML,
        )

    title = extract_reason(message)[:16].strip() or (_AUTO_TITLE_FULL if full else _AUTO_TITLE_BASIC)
    privileges, granted, skipped = await _grantable(
        client, message.chat.id, FULL_PRIVILEGES if full else BASIC_PRIVILEGES,
        promoter_id=message.from_user.id,
    )
    # A bot with "add new admins" can re-promote itself and anyone else — never
    # hand that right to a bot account, not even on /fpromote.
    if getattr(user, "is_bot", False):
        try:
            privileges.can_promote_members = False
        except Exception:
            pass
        if "can_promote_members" in granted:
            granted.remove("can_promote_members")
            skipped.append("can_promote_members")
    ok, err = await safe_call(client.promote_chat_member(message.chat.id, user.id, privileges=privileges))
    if not ok:
        return await message.reply(
            card("Pʀᴏᴍᴏᴛᴇ Fᴀɪʟᴇᴅ", f"❌ <code>{err}</code>\n\n<i>Bot ko 'Add new admins' right chahiye.</i>"),
            parse_mode=enums.ParseMode.HTML,
        )
    title_ok, _ = await safe_call(client.set_administrator_title(message.chat.id, user.id, title))
    await mark_bot_promoted(message.chat.id, user.id, message.from_user.id, full=full)
    # Cached permissions would otherwise hide the new admin for up to 10 min.
    invalidate_admin_cache(message.chat.id)

    rights_line = ", ".join(_pretty_right(k) for k in granted) or "Manage Chat"
    body = (
        f"{'👑' if full else '⭐'} <b>Usᴇʀ :</b> {mention(user)}\n"
        f"🆔 <b>ID :</b> <code>{user.id}</code>\n"
        f"👮 <b>Bʏ :</b> {mention(message.from_user)}\n"
        f"🏷 <b>Tɪᴛʟᴇ :</b> {html.escape(title) if title_ok else '—'}\n"
        f"🔐 <b>Rɪɢʜᴛs :</b> {html.escape(rights_line)}"
    )
    if skipped:
        body += (
            "\n\n⚠️ <b>Nᴏᴛ ɢʀᴀɴᴛᴇᴅ :</b> "
            f"{html.escape(', '.join(_pretty_right(k) for k in skipped))}\n"
            "<i>Ye rights aapke ya mere paas nahi hain — jitne rights aapke "
            "paas hain, sirf utne hi aage diye ja sakte hain.</i>"
        )
    body += "\n\n🛡 <i>Owner Assistant is admin ko watch karega (/assistant).</i>"

    await message.reply(
        card("Fᴜʟʟ Pʀᴏᴍᴏᴛᴇᴅ" if full else "Pʀᴏᴍᴏᴛᴇᴅ", body, "Power aayi hai — responsibility bhi 😌"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(
        f"#promote{' #full' if full else ''} #admin\n👤 {mention(user)} (<code>{user.id}</code>)\n"
        f"👮 By {mention(message.from_user)}\n🏷 {html.escape(title)}\n🏠 <code>{message.chat.id}</code>"
    ))



@bot.on_message(filters.command("promote") & filters.group)
@error_handler
@_gate
async def promote_cmd(client: Client, message: Message):
    await _do_promote(client, message, full=False)


@bot.on_message(filters.command(["fpromote", "fullpromote"]) & filters.group)
@error_handler
@_gate
async def fpromote_cmd(client: Client, message: Message):
    await _do_promote(client, message, full=True)


@bot.on_message(filters.command(["demote", "dpromote"]) & filters.group)
@error_handler
@_gate
async def demote_cmd(client: Client, message: Message):
    from pyrogram.types import ChatPrivileges

    user = await extract_target(client, message)
    if not user:
        return await _need_target(message, "/demote @user")

    # allow_admin=True: demote's entire purpose is to act on an admin. Without
    # it protected_target() answered "target is an admin — demote them first"
    # for EVERY target, which is exactly why /demote never worked.
    sanity = await _promote_sanity(client, message, user, "demote")
    if sanity:
        return await message.reply(
            card("Dᴇᴍᴏᴛᴇ Fᴀɪʟᴇᴅ", f"⚠️ {sanity}"), parse_mode=enums.ParseMode.HTML
        )

    guard = await protected_target(client, message, user, allow_admin=True)
    if guard:
        return await message.reply(
            card("Dᴇᴍᴏᴛᴇ Fᴀɪʟᴇᴅ", f"⚠️ {guard}"), parse_mode=enums.ParseMode.HTML
        )
    if not await is_admin(client, message.chat.id, user.id):
        return await message.reply(
            card("Nᴏᴛ Aɴ Aᴅᴍɪɴ", f"ℹ️ {mention(user)} <b>admin hi nahi hai.</b>"),
            parse_mode=enums.ParseMode.HTML,
        )

    # REQUESTED: "users ek dusre ko tbhi demote kr skte hai jb unke pass full
    # ryts ho" — plus the always-allowed case "jise bot ke through aapne khud
    # promote kiya hai, use aap demote kar sakte ho".
    actor = message.from_user.id
    record = await get_bot_promoted(message.chat.id, user.id)
    promoted_by_actor = bool(record) and record.get("by_id") == actor
    if not promoted_by_actor and not await is_full_rights(client, message.chat.id, actor):
        return await message.reply(
            card("Nᴏᴛ Eɴᴏᴜɢʜ Rɪɢʜᴛs",
                 "🔐 <b>Kisi dusre admin ko demote karne ke liye aapke paas "
                 "<u>full rights</u> hone chahiye.</b>\n\n"
                 "<i>Ya phir sirf unhe demote kar sakte ho jinhe aapne khud "
                 "is bot ke through promote kiya tha.</i>"),
            parse_mode=enums.ParseMode.HTML,
        )

    ok, err = await safe_call(
        client.promote_chat_member(
            message.chat.id, user.id,
            privileges=ChatPrivileges(**{key: False for key in _RIGHT_KEYS}),
        )
    )
    if not ok:
        return await message.reply(
            card("Dᴇᴍᴏᴛᴇ Fᴀɪʟᴇᴅ",
                 f"❌ <code>{err}</code>\n\n"
                 "<i>Telegram sirf unhe demote karne deta hai jinhe maine "
                 "(ya mere through kisi ne) promote kiya ho — kisi aur admin "
                 "ke promote kiye admins ko Telegram khud block karta hai.</i>"),
            parse_mode=enums.ParseMode.HTML,
        )
    await unmark_bot_promoted(message.chat.id, user.id)
    invalidate_admin_cache(message.chat.id)
    await message.reply(
        card("Dᴇᴍᴏᴛᴇᴅ",
             f"📉 {mention(user)} ab normal member hai.\n"
             f"🆔 <b>ID :</b> <code>{user.id}</code>\n"
             f"👮 <b>Bʏ :</b> {mention(message.from_user)}"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(
        f"#demote #admin\n👤 <code>{user.id}</code>\n"
        f"👮 By {mention(message.from_user)}\n🏠 <code>{message.chat.id}</code>"
    ))



# ─── Purge / delete ──────────────────────────────────────────────────────────

@bot.on_message(filters.command(["purge", "spurge"]) & filters.group)
@error_handler
@_gate
async def purge_cmd(client: Client, message: Message):
    """Delete every message between the replied one and this one.

    Works on ARBITRARILY OLD messages: instead of walking chat history (which
    Telegram paginates and rate-limits), we delete the raw message-id range in
    chunks of 100. Supergroup admins may delete any message regardless of age.
    """
    if not message.reply_to_message:
        return await _need_target(message, "reply to a message → /purge")

    silent = message.command[0] == "spurge"
    start_id = message.reply_to_message.id
    end_id = message.id
    ids = list(range(start_id, end_id + 1))

    deleted = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            deleted += await client.delete_messages(message.chat.id, chunk) or 0
        except Exception:
            # Fall back to one-by-one for the chunk (some ids are service msgs)
            for mid in chunk:
                try:
                    await client.delete_messages(message.chat.id, mid)
                    deleted += 1
                except Exception:
                    pass
        await asyncio.sleep(0.2)

    if silent:
        return
    done = await client.send_message(
        message.chat.id,
        card("Pᴜʀɢᴇ Cᴏᴍᴘʟᴇᴛᴇ", f"🧹 <b>Dᴇʟᴇᴛᴇᴅ :</b> <code>{deleted}</code> messages\n👮 <b>Bʏ :</b> {mention(message.from_user)}"),
        parse_mode=enums.ParseMode.HTML,
    )
    await auto_delete(done, 8)
    spawn(log_activity(f"#purge #admin\n🧹 <code>{deleted}</code> msgs in <code>{message.chat.id}</code>"))


# GAP FIX: /title, /purgefrom and /purgeto were advertised in the help menu
# (melody/plugins/misc/help.py -> "gcmanage" page) but no handler existed, so
# they silently did nothing. Implemented here with the same gate/card style as
# the rest of the suite.

# chat_id -> message id marked by /purgefrom
_purge_from: dict = {}


@bot.on_message(filters.command("title") & filters.group)
@error_handler
@_gate
async def title_cmd(client: Client, message: Message):
    user = await extract_target(client, message)
    if not user:
        return await _need_target(message, "/title @user <custom title>")
    title = (extract_reason(message) or "").strip()[:16]
    if not title:
        return await _need_target(message, "/title @user <custom title>")
    ok, err = await safe_call(client.set_administrator_title(message.chat.id, user.id, title))
    if not ok:
        return await message.reply(
            card("Tɪᴛʟᴇ Fᴀɪʟᴇᴅ", f"❌ <code>{err}</code>\n\n<i>User ko pehle /promote karo — title sirf admins ka set hota hai.</i>"),
            parse_mode=enums.ParseMode.HTML,
        )
    await message.reply(
        card("Tɪᴛʟᴇ Sᴇᴛ", f"🏷 {mention(user)} → <code>{html.escape(title)}</code>\n👮 <b>Bʏ :</b> {mention(message.from_user)}"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(f"#title #admin\n👤 <code>{user.id}</code> → <code>{html.escape(title)}</code>"))


async def _delete_range(client: Client, chat_id: int, start_id: int, end_id: int) -> int:
    """Delete an inclusive message-id range in chunks of 100 (same technique
    as /purge — works on arbitrarily old messages)."""
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    ids = list(range(start_id, end_id + 1))
    deleted = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            deleted += await client.delete_messages(chat_id, chunk) or 0
        except Exception:
            for mid in chunk:
                try:
                    await client.delete_messages(chat_id, mid)
                    deleted += 1
                except Exception:
                    pass
        await asyncio.sleep(0.2)
    return deleted


@bot.on_message(filters.command("purgefrom") & filters.group)
@error_handler
@_gate
async def purgefrom_cmd(client: Client, message: Message):
    if not message.reply_to_message:
        return await _need_target(message, "reply to the FIRST message → /purgefrom")
    _purge_from[message.chat.id] = message.reply_to_message.id
    m = await message.reply(
        card("Pᴜʀɢᴇ Sᴛᴀʀᴛ Mᴀʀᴋᴇᴅ", "📍 <b>Sᴛᴀʀᴛ sᴇᴛ.</b>\n\n<i>Ab last message pe reply karke</i> <code>/purgeto</code> <i>chalao.</i>"),
        parse_mode=enums.ParseMode.HTML,
    )
    await auto_delete(m, 30)


@bot.on_message(filters.command("purgeto") & filters.group)
@error_handler
@_gate
async def purgeto_cmd(client: Client, message: Message):
    start_id = _purge_from.pop(message.chat.id, None)
    if start_id is None:
        return await _need_target(message, "pehle /purgefrom se start mark karo")
    if not message.reply_to_message:
        return await _need_target(message, "reply to the LAST message → /purgeto")
    deleted = await _delete_range(client, message.chat.id, start_id, message.reply_to_message.id)
    await safe_call(message.delete())
    done = await client.send_message(
        message.chat.id,
        card("Rᴀɴɢᴇ Pᴜʀɢᴇ Cᴏᴍᴘʟᴇᴛᴇ", f"🧹 <b>Dᴇʟᴇᴛᴇᴅ :</b> <code>{deleted}</code> messages\n👮 <b>Bʏ :</b> {mention(message.from_user)}"),
        parse_mode=enums.ParseMode.HTML,
    )
    await auto_delete(done, 8)
    spawn(log_activity(f"#purgeto #admin\n🧹 <code>{deleted}</code> msgs in <code>{message.chat.id}</code>"))


@bot.on_message(filters.command(["del", "delete"]) & filters.group)
@error_handler
@_gate
async def del_cmd(client: Client, message: Message):
    if message.reply_to_message:
        await safe_call(message.reply_to_message.delete())
    await safe_call(message.delete())


# ─── Pin ─────────────────────────────────────────────────────────────────────

@bot.on_message(filters.command("pin") & filters.group)
@error_handler
@_gate
async def pin_cmd(client: Client, message: Message):
    if not message.reply_to_message:
        return await _need_target(message, "reply to a message → /pin")
    loud = "loud" in (message.command[1:] or [])
    ok, err = await safe_call(message.reply_to_message.pin(disable_notification=not loud))
    if not ok:
        return await message.reply(card("Pɪɴ Fᴀɪʟᴇᴅ", f"❌ <code>{err}</code>"), parse_mode=enums.ParseMode.HTML)
    await message.reply(card("Pɪɴɴᴇᴅ", "📌 Message pin ho gaya."), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command("unpin") & filters.group)
@error_handler
@_gate
async def unpin_cmd(client: Client, message: Message):
    if message.reply_to_message:
        await safe_call(message.reply_to_message.unpin())
    else:
        await safe_call(client.unpin_chat_message(message.chat.id))
    await message.reply(card("Uɴᴘɪɴɴᴇᴅ", "📍 Ho gaya."), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command("unpinall") & filters.group)
@error_handler
@_gate
async def unpinall_cmd(client: Client, message: Message):
    ok, err = await safe_call(client.unpin_all_chat_messages(message.chat.id))
    await message.reply(
        card("Uɴᴘɪɴ Aʟʟ", "🧹 Saare pins hata diye." if ok else f"❌ <code>{err}</code>"),
        parse_mode=enums.ParseMode.HTML,
    )


# ─── Warns ───────────────────────────────────────────────────────────────────

@bot.on_message(filters.command("warn") & filters.group)
@error_handler
@_gate
async def warn_cmd(client: Client, message: Message):
    user = await extract_target(client, message)
    if not user:
        return await _need_target(message, "/warn @user [reason]")
    blocked = await protected_target(client, message, user)
    if blocked:
        return await message.reply(card("Wᴀʀɴ", blocked), parse_mode=enums.ParseMode.HTML)
    reason = extract_reason(message) or "Not specified"
    count = await add_warn(message.chat.id, user.id, reason)
    extra = ""
    if count >= WARN_LIMIT:
        await safe_call(client.restrict_chat_member(message.chat.id, user.id, MUTED_PERMS))
        await reset_warns(message.chat.id, user.id)
        extra = "\n\n🔇 <b>Wᴀʀɴ ʟɪᴍɪᴛ ʀᴇᴀᴄʜᴇᴅ — user muted.</b>"
    await message.reply(
        card(
            "Wᴀʀɴɪɴɢ",
            f"⚠️ <b>Usᴇʀ :</b> {mention(user)}\n"
            f"📊 <b>Wᴀʀɴs :</b> <code>{min(count, WARN_LIMIT)}/{WARN_LIMIT}</code>\n"
            f"📝 <b>Rᴇᴀsᴏɴ :</b> {html.escape(reason)}{extra}",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(filters.command("warns") & filters.group)
@error_handler
async def warns_cmd(client: Client, message: Message):
    user = await extract_target(client, message) or message.from_user
    data = await get_warns(message.chat.id, user.id)
    reasons = "\n".join(f"  🔸 {html.escape(str(r))}" for r in (data.get("reasons") or [])) or "  🔸 —"
    await message.reply(
        card("Wᴀʀɴ Rᴇᴄᴏʀᴅ", f"👤 {mention(user)}\n📊 <b>Cᴏᴜɴᴛ :</b> <code>{data.get('count', 0)}/{WARN_LIMIT}</code>\n\n{reasons}"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(filters.command(["resetwarns", "rmwarns", "unwarn"]) & filters.group)
@error_handler
@_gate
async def resetwarns_cmd(client: Client, message: Message):
    user = await extract_target(client, message)
    if not user:
        return await _need_target(message, "/resetwarns @user")
    await reset_warns(message.chat.id, user.id)
    await message.reply(card("Wᴀʀɴs Cʟᴇᴀʀᴇᴅ", f"✅ {mention(user)} ka record saaf."), parse_mode=enums.ParseMode.HTML)


# ─── Admin tagging / info ────────────────────────────────────────────────────

async def _admin_list(client: Client, chat_id: int) -> list:
    admins = []
    async for m in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        if m.user and not m.user.is_bot:
            admins.append(m)
    return admins


@bot.on_message(filters.command(["admins", "adminlist", "staff"]) & filters.group)
@error_handler
async def adminlist_cmd(client: Client, message: Message):
    admins = await _admin_list(client, message.chat.id)
    from utils.owner_view import is_anonymous_member, owner_label, pick_owner

    owner_member = pick_owner(admins)
    # Never leak an anonymous owner through the ordinary admin list after
    # correctly rendering the owner row as Hidden.
    others = [
        a for a in admins
        if a is not owner_member and not is_anonymous_member(a)
    ]
    # REQUESTED: anonymous / hidden owner ka naam kabhi show nahi karna.
    body = "👑 <b>Oᴡɴᴇʀ :</b> " + owner_label(owner_member) + "\n\n🛡 <b>Aᴅᴍɪɴs :</b>\n"
    body += "\n".join(f"  🔸 {mention(a.user)}" for a in others) or "  🔸 —"
    await message.reply(
        card("Aᴅᴍɪɴ Tᴇᴀᴍ", body, f"Total: {len(admins)}"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(
    (filters.command(["report", "admin", "admins@", "reportuser"]) | filters.regex(r"^@admins?\b"))
    & filters.group
)
@error_handler
async def report_cmd(client: Client, message: Message):
    """`@admins` / `/report` (reply) — silently tag the whole admin team."""
    admins = await _admin_list(client, message.chat.id)
    if not admins:
        return
    target = message.reply_to_message.from_user if message.reply_to_message else None
    from utils.owner_view import is_anonymous_member

    visible_admins = [a for a in admins if not is_anonymous_member(a)]
    tags = "".join(
        f'<a href="tg://user?id={a.user.id}">\u2063</a>' for a in visible_admins[:40]
    )
    body = (
        f"🚨 <b>Rᴇᴘᴏʀᴛ ʙʏ :</b> {mention(message.from_user)}\n"
        + (f"🎯 <b>Rᴇᴘᴏʀᴛᴇᴅ :</b> {mention(target)}\n" if target else "")
        + f"💬 <b>Nᴏᴛᴇ :</b> {html.escape(extract_reason(message, 0) or 'Admins needed here')}\n\n"
        f"👮 <b>Tᴀɢɢᴇᴅ :</b> <code>{len(visible_admins)}</code> admins{tags}"
    )
    await message.reply(card("Aᴅᴍɪɴs Cᴀʟʟᴇᴅ", body), parse_mode=enums.ParseMode.HTML, reply_markup=close_kb())


@bot.on_message(filters.command(["tagall", "all", "mention", "utag"]) & filters.group)
@error_handler
@_gate
async def tagall_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    _tagall_cancel.discard(chat_id)
    note = html.escape(extract_reason(message, 0) or "Sab yahan aa jao 🎶")
    batch, sent = [], 0
    async for m in client.get_chat_members(chat_id):
        if chat_id in _tagall_cancel:
            break
        if m.user and not m.user.is_bot and not m.user.is_deleted:
            batch.append(mention(m.user))
        if len(batch) >= 6:
            await client.send_message(chat_id, f"💬 <b>{note}</b>\n" + " • ".join(batch), parse_mode=enums.ParseMode.HTML)
            sent += len(batch)
            batch = []
            await asyncio.sleep(2.5)
    if batch and chat_id not in _tagall_cancel:
        await client.send_message(chat_id, f"💬 <b>{note}</b>\n" + " • ".join(batch), parse_mode=enums.ParseMode.HTML)
    _tagall_cancel.discard(chat_id)


@bot.on_message(filters.command(["cancel", "canceltag", "stoptag"]) & filters.group)
@error_handler
@_gate
async def cancel_tag_cmd(client: Client, message: Message):
    _tagall_cancel.add(message.chat.id)
    await message.reply(card("Tᴀɢɢɪɴɢ Sᴛᴏᴘᴘᴇᴅ", "🛑 Tagall band kar diya."), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command(["invitelink", "link"]) & filters.group)
@error_handler
@_gate
async def invitelink_cmd(client: Client, message: Message):
    try:
        link = await client.export_chat_invite_link(message.chat.id)
    except Exception as exc:
        return await message.reply(card("Iɴᴠɪᴛᴇ Lɪɴᴋ", f"❌ <code>{html.escape(str(exc))}</code>"), parse_mode=enums.ParseMode.HTML)
    await message.reply(card("Iɴᴠɪᴛᴇ Lɪɴᴋ", f"🔗 <code>{html.escape(link)}</code>"), parse_mode=enums.ParseMode.HTML, reply_markup=close_kb())


@bot.on_message(filters.command(["info", "whois"]) & filters.group)
@error_handler
async def info_cmd(client: Client, message: Message):
    user = await extract_target(client, message) or message.from_user
    member = None
    try:
        member = await client.get_chat_member(message.chat.id, user.id)
    except Exception:
        pass
    warns = await get_warns(message.chat.id, user.id)

    # REQUESTED: "/staff etc kuch bhi krne pe Owner agr hidden hai to hide
    # rakho." /info still printed the anonymous owner's name, id and a
    # clickable mention — the one place the privacy rule leaked.
    from utils.owner_view import HIDDEN, is_anonymous_member

    hide_owner = False
    if member is not None and str(getattr(member, "status", "")).lower().endswith("owner"):
        hide_owner = is_anonymous_member(member)

    name_line = HIDDEN if hide_owner else mention(user)
    id_line = "<code>—</code>" if hide_owner else f"<code>{user.id}</code>"
    uname_line = (
        "—" if hide_owner
        else ("@" + user.username if user.username else "—")
    )
    await message.reply(
        card(
            "Usᴇʀ Iɴғᴏ",
            f"👤 <b>Nᴀᴍᴇ :</b> {name_line}\n"
            f"🆔 <b>ID :</b> {id_line}\n"
            f"🔗 <b>Usᴇʀɴᴀᴍᴇ :</b> {uname_line}\n"
            f"🛡 <b>Sᴛᴀᴛᴜs :</b> <code>{str(getattr(member, 'status', 'unknown')).split('.')[-1]}</code>\n"
            f"⚠️ <b>Wᴀʀɴs :</b> <code>{warns.get('count', 0)}/{WARN_LIMIT}</code>",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


# ─── Chat lock / unlock ──────────────────────────────────────────────────────

@bot.on_message(filters.command("lock") & filters.group)
@error_handler
@_gate
async def lock_cmd(client: Client, message: Message):
    what = (message.command[1].lower() if len(message.command) > 1 else "all")
    perms = {
        "all": ChatPermissions(can_send_messages=False),
        "media": ChatPermissions(can_send_messages=True, can_send_media_messages=False),
        "links": ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_add_web_page_previews=False),
        "polls": ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=False),
    }.get(what, ChatPermissions(can_send_messages=False))
    ok, err = await safe_call(client.set_chat_permissions(message.chat.id, perms))
    await message.reply(
        card("Cʜᴀᴛ Lᴏᴄᴋᴇᴅ", f"🔒 <b>Lᴏᴄᴋᴇᴅ :</b> <code>{html.escape(what)}</code>" if ok else f"❌ <code>{err}</code>"),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command("unlock") & filters.group)
@error_handler
@_gate
async def unlock_cmd(client: Client, message: Message):
    ok, err = await safe_call(client.set_chat_permissions(message.chat.id, UNMUTED_PERMS))
    await message.reply(
        card("Cʜᴀᴛ Uɴʟᴏᴄᴋᴇᴅ", "🔓 Sab bol sakte hain ab." if ok else f"❌ <code>{err}</code>"),
        parse_mode=enums.ParseMode.HTML,
    )


# ─── Shared close button ─────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^gc_close$"))
@error_handler
async def gc_close_cb(client: Client, cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("Closed 🎶")
