"""
👑 Owner Assistant — Melody works like the group owner's private bodyguard.

REQUESTED
---------
"Gc security keliye owner assistant ki trh kaam kare … koi bhi admin is bot se
 promote hua ho aur 5 sec me 3+ user ban kare to bot uski demote kare, owner ko
 tag kare aur reason bataye. Aur ye custom set ho sake. Aur isi tarah top 5 best
 features add kar."

THE FIVE GUARDS (all per-group configurable via /oaset)
-------------------------------------------------------
1. 🚨 Mass-Ban Guard      — N bans inside T seconds → demote + mute the admin,
                            revoke the bot-promotion, tag the owner with reason.
2. 🚪 Mass-Kick Guard     — same engine for kicks (separate limit/window).
3. 🔇 Mass-Mute Guard     — same engine for restrictions/mutes.
4. ⬆️ Promote Guard       — a bot-promoted admin promoting *more* admins gets the
                            new admin demoted instantly + an owner alert (this is
                            the classic "admin ne apna gang bana liya" takeover).
5. 🛡 Owner Shield        — any ban/mute aimed at the group owner, a sudo user or
                            Melody itself is auto-reverted and the attacker is
                            stripped of powers.
   ➕ 📜 Audit Trail       — optional DM to the group owner for every admin action.

Commands (group owner / sudo)
    /assistant  /oa                 → the status panel
    /assistant on|off               → master switch
    /oaset banlimit 3 5             → 3 bans per 5 seconds
    /oaset kicklimit 5 10
    /oaset mutelimit 5 10
    /oaset action demote|mute|both
    /oaset promoteguard on|off
    /oaset ownershield on|off
    /oaset settingsguard on|off
    /oaset audit on|off
    /oaset tagowner on|off
    /oareset                        → back to defaults

This module replaces the old fixed-threshold `antiabuse.py` watchdog (10s / 5
bans, hardcoded, no config, no other guard).
"""
from __future__ import annotations

import html
import time
from collections import defaultdict, deque

from pyrogram import Client, enums, filters
from pyrogram.types import (
    ChatMemberUpdated,
    ChatPrivileges,
    InlineKeyboardMarkup,
    Message,
)

from melody import bot
from melody.config import Config
from utils.client_cache import get_me_cached
from melody.logging import LOGGER, log_activity
from utils.admin_tools import (
    MUTED_PERMS,
    UNMUTED_PERMS,
    card,
    close_kb,
    is_group_owner,
    mention_id,
    safe_call,
)
from utils.assistant_db import BOOL_KEYS, DEFAULTS, get_cfg, reset_cfg, set_cfg
from utils.buttons import ikb
from utils.database import get_chat_owner
from utils.decorators import error_handler
from utils.gc_db import is_bot_promoted, is_sudo, unmark_bot_promoted
from utils.tasks import spawn

# (chat_id, actor_id, kind) → deque[timestamps]
_events: dict = defaultdict(lambda: deque(maxlen=64))
_punished: dict = {}
_owner_cache: dict[int, tuple[float, tuple[int, str] | None]] = {}
_OWNER_TTL = 600


def _status_str(status) -> str:
    return str(getattr(status, "value", status) or "").lower()


def _record(chat_id: int, actor_id: int, kind: str, window: int) -> int:
    now = time.time()
    events = _events[(chat_id, actor_id, kind)]
    events.append(now)
    while events and now - events[0] > window:
        events.popleft()
    return len(events)


async def _group_owner(client: Client, chat_id: int) -> tuple[int, str] | None:
    cached = _owner_cache.get(chat_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    found = None
    try:
        async for m in client.get_chat_members(
            chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS
        ):
            if _status_str(m.status) in ("owner", "creator"):
                from utils.owner_view import is_anonymous_member

                if is_anonymous_member(m):
                    # REQUESTED: hidden/anonymous owner ki identity kabhi
                    # reveal nahi karni — sirf "hidden" bolna hai.
                    LOGGER.info(
                        "owner_assistant: group %s ka owner anonymous hai — "
                        "identity hide ki gayi", chat_id,
                    )
                    found = (0, None)
                else:
                    found = (m.user.id, m.user.first_name or "Owner")
                break
    except Exception:
        found = None
    _owner_cache[chat_id] = (time.monotonic() + _OWNER_TTL, found)
    return found


async def _owner_tags(client: Client, chat_id: int, cfg: dict) -> str:
    if not cfg.get("tag_owner", True):
        return ""
    tags = []
    owner = await _group_owner(client, chat_id)
    if owner and owner[0]:
        tags.append(f"👑 <b>Gʀᴏᴜᴘ Oᴡɴᴇʀ :</b> {mention_id(owner[0], owner[1])}")
    elif owner:
        from utils.owner_view import HIDDEN

        tags.append(f"👑 <b>Gʀᴏᴜᴘ Oᴡɴᴇʀ :</b> {HIDDEN}")
    adder = await get_chat_owner(chat_id)
    if adder and adder.get("owner_id") and (not owner or adder["owner_id"] != owner[0]):
        tags.append(
            f"➕ <b>Bᴏᴛ Aᴅᴅᴇᴅ Bʏ :</b> "
            f"{mention_id(adder['owner_id'], adder.get('owner_name') or 'User')}"
        )
    return ("\n" + "\n".join(tags)) if tags else ""


async def _is_protected(client: Client, chat_id: int, user_id: int) -> bool:
    """Owner / sudo / Melody itself — never a valid target."""
    if user_id == Config.OWNER_ID:
        return True
    try:
        if await is_sudo(user_id):
            return True
    except Exception:
        pass
    try:
        me = await get_me_cached(client)
        if user_id == me.id:
            return True
    except Exception:
        pass
    return await is_group_owner(client, chat_id, user_id)


async def _punish(client: Client, chat_id: int, actor, cfg: dict, reason: str,
                  detail: str, guard: str):
    """Demote / mute the offending admin and report to the group + owner."""
    if time.time() - _punished.get((chat_id, actor.id), 0) < 45:
        return
    _punished[(chat_id, actor.id)] = time.time()

    action = str(cfg.get("action", "both")).lower()
    steps = []
    if action in ("demote", "both"):
        ok, _ = await safe_call(client.promote_chat_member(
            chat_id, actor.id, privileges=ChatPrivileges(can_manage_chat=False)))
        steps.append("📉 Demoted" if ok else "📉 Demote failed (mujhe rights nahi mile)")
        await unmark_bot_promoted(chat_id, actor.id)
    if action in ("mute", "both"):
        ok, _ = await safe_call(client.restrict_chat_member(chat_id, actor.id, MUTED_PERMS))
        steps.append("🔇 Muted" if ok else "🔇 Mute failed")

    tags = await _owner_tags(client, chat_id, cfg)
    text = card(
        "Oᴡɴᴇʀ Assɪsᴛᴀɴᴛ Aʟᴇʀᴛ",
        f"🚨 <b>{html.escape(guard)}</b>\n\n"
        f"👤 <b>Aᴅᴍɪɴ :</b> {mention_id(actor.id, actor.first_name or 'Admin')}\n"
        f"🆔 <b>ID :</b> <code>{actor.id}</code>\n"
        f"📊 <b>Dᴇᴛᴀɪʟ :</b> {html.escape(detail)}\n"
        f"📝 <b>Rᴇᴀsᴏɴ :</b> {html.escape(reason)}\n\n"
        "⚡ <b>Aᴄᴛɪᴏɴ ᴛᴀᴋᴇɴ :</b>\n" + "\n".join(f"  🔸 {s}" for s in steps) + "\n"
        f"  🔸 🗑 Bot-promotion revoked{tags}",
        "Melody apne groups ki hifazat karta hai 🎶",
    )
    try:
        await client.send_message(chat_id, text, parse_mode=enums.ParseMode.HTML,
                                  reply_markup=close_kb([[
                                      ikb("🔙 Rᴇsᴛᴏʀᴇ Aᴅᴍɪɴ",

                                          callback_data=f"oa_restore_{chat_id}_{actor.id}")]]))
    except Exception as exc:
        LOGGER.warning("owner-assistant report failed in %s: %s", chat_id, exc)

    spawn(log_activity(
        f"#ownerassistant #{guard.split()[0].lower().strip('🚨🚪🔇⬆️🛡')}\n"
        f"👤 <code>{actor.id}</code>\n🏠 <code>{chat_id}</code>\n"
        f"📊 {html.escape(detail)}"
    ))


async def _audit(client: Client, chat_id: int, cfg: dict, line: str):
    if not cfg.get("audit"):
        return
    owner = await _group_owner(client, chat_id)
    if not owner or not owner[0]:
        # Anonymous owner: no id to DM, and we never expose the identity.
        LOGGER.info(
            "owner_assistant[%s]: audit DM skipped — group owner hidden/anonymous",
            chat_id,
        )
        return
    try:
        await client.send_message(
            owner[0],
            card("Aᴜᴅɪᴛ Tʀᴀɪʟ", f"📜 <b>Gʀᴏᴜᴘ :</b> <code>{chat_id}</code>\n{line}"),
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass


# ── the watchdog ─────────────────────────────────────────────────────────────

@bot.on_chat_member_updated(group=9)
async def assistant_watchdog(client: Client, update: ChatMemberUpdated):
    try:
        new = update.new_chat_member
        old = update.old_chat_member
        actor = update.from_user
        if not new or not actor:
            return
        chat_id = update.chat.id
        victim = getattr(new, "user", None)
        if not victim or actor.id == victim.id:
            return  # self-leave / self-join is not an admin action

        cfg = await get_cfg(chat_id)
        if not cfg.get("enabled", True):
            return
        if await _is_protected(client, chat_id, actor.id):
            return  # the owner / sudo / bot can do whatever they want

        new_status = _status_str(new.status)
        old_status = _status_str(getattr(old, "status", "") or "")

        # ── Guard 5: Owner Shield ────────────────────────────────────────────
        if cfg.get("owner_shield", True) and new_status in ("banned", "kicked", "restricted"):
            if await _is_protected(client, chat_id, victim.id):
                if new_status in ("banned", "kicked"):
                    await safe_call(client.unban_chat_member(chat_id, victim.id))
                else:
                    await safe_call(client.restrict_chat_member(chat_id, victim.id, UNMUTED_PERMS))
                return await _punish(
                    client, chat_id, actor, cfg,
                    reason="Owner / sudo / Melody pe action lene ki koshish ki.",
                    detail=f"target <code>{victim.id}</code> ({new_status})",
                    guard="🛡 Oᴡɴᴇʀ Sʜɪᴇʟᴅ Tʀɪɢɢᴇʀᴇᴅ",
                )

        bot_promoted = await is_bot_promoted(chat_id, actor.id)

        # ── Guard 4: Promote Guard ───────────────────────────────────────────
        if (cfg.get("promote_guard", True) and bot_promoted
                and new_status == "administrator" and old_status not in ("administrator", "owner")):
            await safe_call(client.promote_chat_member(
                chat_id, victim.id, privileges=ChatPrivileges(can_manage_chat=False)))
            await unmark_bot_promoted(chat_id, victim.id)
            return await _punish(
                client, chat_id, actor, cfg,
                reason="Bot se promote hua admin khud naye admins bana raha tha "
                       "(takeover attempt).",
                detail=f"naya admin <code>{victim.id}</code> demote kar diya",
                guard="⬆️ Pʀᴏᴍᴏᴛᴇ Gᴜᴀʀᴅ Tʀɪɢɢᴇʀᴇᴅ",
            )

        # Burst guards only police admins that got their powers FROM Melody —
        # a human-promoted co-owner is the group owner's own responsibility.
        if not bot_promoted:
            return

        kinds = {
            "ban": (new_status in ("banned", "kicked") and old_status not in ("banned", "kicked"),
                    int(cfg.get("ban_limit", 3)), int(cfg.get("ban_window", 5)),
                    "🚨 Mᴀss-Bᴀɴ Dᴇᴛᴇᴄᴛᴇᴅ", "bans"),
            "kick": (new_status == "left" and old_status in ("member", "restricted", "administrator"),
                     int(cfg.get("kick_limit", 5)), int(cfg.get("kick_window", 10)),
                     "🚪 Mᴀss-Kɪᴄᴋ Dᴇᴛᴇᴄᴛᴇᴅ", "kicks"),
            "mute": (new_status == "restricted" and old_status != "restricted",
                     int(cfg.get("mute_limit", 5)), int(cfg.get("mute_window", 10)),
                     "🔇 Mᴀss-Mᴜᴛᴇ Dᴇᴛᴇᴄᴛᴇᴅ", "mutes"),
        }
        for kind, (matched, limit, window, guard, noun) in kinds.items():
            if not matched:
                continue
            count = _record(chat_id, actor.id, kind, max(window, 1))
            await _audit(
                client, chat_id, cfg,
                f"👮 <code>{actor.id}</code> → {kind} <code>{victim.id}</code>",
            )
            if count < max(limit, 1):
                return
            return await _punish(
                client, chat_id, actor, cfg,
                reason=f"{count} {noun} sirf {window} second me — mass abuse.",
                detail=f"<code>{count}</code> {noun} / <code>{window}s</code> "
                       f"(limit <code>{limit}</code>)",
                guard=guard,
            )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("owner-assistant watchdog: %s", exc)


# ── Guard 3b: chat settings changed ──────────────────────────────────────────

@bot.on_message((filters.new_chat_title | filters.new_chat_photo) & filters.group, group=9)
@error_handler
async def settings_guard(client: Client, message: Message):
    actor = message.from_user
    if not actor:
        return
    cfg = await get_cfg(message.chat.id)
    if not cfg.get("enabled", True) or not cfg.get("settings_guard", True):
        return
    if await _is_protected(client, message.chat.id, actor.id):
        return
    what = "title" if message.new_chat_title else "photo"
    tags = await _owner_tags(client, message.chat.id, cfg)
    await client.send_message(
        message.chat.id,
        card(
            "Sᴇᴛᴛɪɴɢs Cʜᴀɴɢᴇ Aʟᴇʀᴛ",
            f"⚠️ <b>Gʀᴏᴜᴘ {what} ʙᴀᴅᴀʟ ᴅɪʏᴀ ɢᴀʏᴀ.</b>\n\n"
            f"👤 <b>Bʏ :</b> {mention_id(actor.id, actor.first_name or 'Admin')}\n"
            f"🆔 <b>ID :</b> <code>{actor.id}</code>"
            + (f"\n🏷 <b>Nᴇᴡ Tɪᴛʟᴇ :</b> {html.escape(message.new_chat_title)}"
               if message.new_chat_title else "")
            + tags,
            "Owner Assistant · settings guard 🛡",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    await _audit(client, message.chat.id, cfg,
                 f"⚙️ <code>{actor.id}</code> changed the group {what}")


# ── restore button ───────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^oa_restore_(-?\d+)_(\d+)$"))
@error_handler
async def oa_restore_cb(client: Client, cb):
    chat_id, user_id = cb.data.split("_")[2:]
    chat_id, user_id = int(chat_id), int(user_id)
    if not (cb.from_user.id == Config.OWNER_ID
            or await is_sudo(cb.from_user.id)
            or await is_group_owner(client, chat_id, cb.from_user.id)):
        return await cb.answer("❌ Sirf group owner / sudo restore kar sakte hain.",
                              show_alert=True)
    await safe_call(client.restrict_chat_member(chat_id, user_id, UNMUTED_PERMS))
    from utils.admin_tools import BASIC_PRIVILEGES

    ok, err = await safe_call(client.promote_chat_member(chat_id, user_id,
                                                        privileges=BASIC_PRIVILEGES))
    _punished.pop((chat_id, user_id), None)
    for kind in ("ban", "kick", "mute"):
        _events.pop((chat_id, user_id, kind), None)
    await cb.answer("✅ Restore ho gaya." if ok else f"⚠️ {err[:150]}", show_alert=True)


# ── configuration commands ───────────────────────────────────────────────────

async def _owner_gate(client: Client, message: Message) -> bool:
    uid = message.from_user.id if message.from_user else 0
    if uid == Config.OWNER_ID or await is_sudo(uid) or await is_group_owner(
        client, message.chat.id, uid
    ):
        return True
    await message.reply(
        card("Aᴄᴄᴇss Dᴇɴɪᴇᴅ",
             "⚠️ <b>Owner Assistant sirf ɢʀᴏᴜᴘ ᴏᴡɴᴇʀ / sᴜᴅᴏ configure kar sakte hain.</b>"),
        parse_mode=enums.ParseMode.HTML,
    )
    return False


def _panel(cfg: dict) -> str:
    def flag(key: str) -> str:
        return "✅ ON" if cfg.get(key) else "❌ OFF"

    return card(
        "Oᴡɴᴇʀ Assɪsᴛᴀɴᴛ",
        f"🛡 <b>Mᴀsᴛᴇʀ :</b> {flag('enabled')}\n\n"
        "<b>Bᴜʀsᴛ Gᴜᴀʀᴅs</b>\n"
        f"┌ 🚨 Ban : <code>{cfg['ban_limit']}</code> / <code>{cfg['ban_window']}s</code>\n"
        f"├ 🚪 Kick : <code>{cfg['kick_limit']}</code> / <code>{cfg['kick_window']}s</code>\n"
        f"└ 🔇 Mute : <code>{cfg['mute_limit']}</code> / <code>{cfg['mute_window']}s</code>\n"
        f"⚡ <b>Pᴜɴɪsʜᴍᴇɴᴛ :</b> <code>{cfg['action']}</code>\n\n"
        "<b>Oᴛʜᴇʀ Gᴜᴀʀᴅs</b>\n"
        f"┌ ⬆️ Promote Guard : {flag('promote_guard')}\n"
        f"├ 🛡 Owner Shield : {flag('owner_shield')}\n"
        f"├ ⚙️ Settings Guard : {flag('settings_guard')}\n"
        f"├ 📜 Audit Trail (owner DM) : {flag('audit')}\n"
        f"└ 🔔 Tag Owner in alerts : {flag('tag_owner')}\n\n"
        "<blockquote expandable>"
        "🧾 <b>Cᴏᴍᴍᴀɴᴅs</b>\n"
        "┌ <code>/assistant on|off</code>\n"
        "├ <code>/oaset banlimit 3 5</code>\n"
        "├ <code>/oaset kicklimit 5 10</code> · <code>/oaset mutelimit 5 10</code>\n"
        "├ <code>/oaset action demote|mute|both</code>\n"
        "├ <code>/oaset promoteguard on|off</code>\n"
        "├ <code>/oaset ownershield on|off</code>\n"
        "├ <code>/oaset settingsguard on|off</code>\n"
        "├ <code>/oaset audit on|off</code> · <code>/oaset tagowner on|off</code>\n"
        "└ <code>/oareset</code>"
        "</blockquote>",
        "Sirf bot se promote hue admins burst-guards ke andar aate hain 🎶",
    )


def _panel_kb(cfg: dict) -> InlineKeyboardMarkup:
    def mark(key: str, label: str) -> str:
        return f"{'✅' if cfg.get(key) else '❌'} {label}"

    return InlineKeyboardMarkup([
        [ikb(mark("enabled", "Assistant"), callback_data="oa_t_enabled")],
        [ikb(mark("promote_guard", "Promote Guard"), callback_data="oa_t_promote_guard"),
         ikb(mark("owner_shield", "Owner Shield"), callback_data="oa_t_owner_shield")],
        [ikb(mark("settings_guard", "Settings Guard"), callback_data="oa_t_settings_guard"),
         ikb(mark("audit", "Audit DM"), callback_data="oa_t_audit")],
        [ikb(mark("tag_owner", "Tag Owner"), callback_data="oa_t_tag_owner")],
        [ikb("✖️ Cʟᴏsᴇ", callback_data="gc_close")],
    ])


@bot.on_message(filters.command(["assistant", "oa", "ownerassistant"]) & filters.group)
@error_handler
async def assistant_cmd(client: Client, message: Message):
    if not await _owner_gate(client, message):
        return
    arg = (message.command[1].lower() if len(message.command) > 1 else "")
    if arg in ("on", "off"):
        await set_cfg(message.chat.id, "enabled", arg == "on")
    cfg = await get_cfg(message.chat.id)
    await message.reply(_panel(cfg), parse_mode=enums.ParseMode.HTML,
                        reply_markup=_panel_kb(cfg))


_KEY_ALIASES = {
    "banlimit": ("ban_limit", "ban_window"),
    "kicklimit": ("kick_limit", "kick_window"),
    "mutelimit": ("mute_limit", "mute_window"),
    "promoteguard": ("promote_guard",),
    "ownershield": ("owner_shield",),
    "settingsguard": ("settings_guard",),
    "audit": ("audit",),
    "tagowner": ("tag_owner",),
    "action": ("action",),
    "enabled": ("enabled",),
}


@bot.on_message(filters.command(["oaset", "assistantset"]) & filters.group)
@error_handler
async def oaset_cmd(client: Client, message: Message):
    if not await _owner_gate(client, message):
        return
    args = message.command[1:]
    if not args or args[0].lower() not in _KEY_ALIASES:
        cfg = await get_cfg(message.chat.id)
        return await message.reply(_panel(cfg), parse_mode=enums.ParseMode.HTML,
                                  reply_markup=_panel_kb(cfg))

    alias = args[0].lower()
    keys = _KEY_ALIASES[alias]
    values = args[1:]

    if alias == "action":
        value = (values[0].lower() if values else "")
        if value not in ("demote", "mute", "both"):
            return await message.reply(
                "🧾 <code>/oaset action demote|mute|both</code>",
                parse_mode=enums.ParseMode.HTML)
        await set_cfg(message.chat.id, "action", value)
    elif keys[0] in BOOL_KEYS:
        value = (values[0].lower() if values else "")
        if value not in ("on", "off", "yes", "no", "true", "false"):
            return await message.reply(
                f"🧾 <code>/oaset {alias} on|off</code>", parse_mode=enums.ParseMode.HTML)
        await set_cfg(message.chat.id, keys[0], value in ("on", "yes", "true"))
    else:
        if len(values) < 2 or not values[0].isdigit() or not values[1].isdigit():
            return await message.reply(
                f"🧾 <code>/oaset {alias} &lt;count&gt; &lt;seconds&gt;</code>\n"
                f"<i>Example :</i> <code>/oaset {alias} 3 5</code>",
                parse_mode=enums.ParseMode.HTML)
        count = max(1, min(int(values[0]), 100))
        window = max(1, min(int(values[1]), 3600))
        await set_cfg(message.chat.id, keys[0], count)
        await set_cfg(message.chat.id, keys[1], window)

    cfg = await get_cfg(message.chat.id)
    await message.reply(
        card("Sᴀᴠᴇᴅ", f"✅ <b>{alias} update ho gaya.</b>") + "\n\n" + _panel(cfg),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_panel_kb(cfg),
    )


@bot.on_message(filters.command(["oareset", "assistantreset"]) & filters.group)
@error_handler
async def oareset_cmd(client: Client, message: Message):
    if not await _owner_gate(client, message):
        return
    await reset_cfg(message.chat.id)
    cfg = await get_cfg(message.chat.id)
    await message.reply(
        card("Rᴇsᴇᴛ", "♻️ <b>Owner Assistant default settings pe aa gaya.</b>\n\n"
                      f"<i>Defaults :</i> <code>{DEFAULTS['ban_limit']} bans / "
                      f"{DEFAULTS['ban_window']}s</code>")
        + "\n\n" + _panel(cfg),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_panel_kb(cfg),
    )


@bot.on_callback_query(filters.regex(r"^oa_t_([a-z_]+)$"))
@error_handler
async def oa_toggle_cb(client: Client, cb):
    key = cb.data.split("oa_t_", 1)[1]
    chat_id = cb.message.chat.id
    if not (cb.from_user.id == Config.OWNER_ID
            or await is_sudo(cb.from_user.id)
            or await is_group_owner(client, chat_id, cb.from_user.id)):
        return await cb.answer("❌ Sirf group owner / sudo.", show_alert=True)
    if key not in BOOL_KEYS:
        return await cb.answer("Unknown toggle", show_alert=True)
    cfg = await get_cfg(chat_id)
    await set_cfg(chat_id, key, not bool(cfg.get(key)))
    cfg = await get_cfg(chat_id)
    await cb.answer(f"{key.replace('_', ' ')} → {'ON' if cfg.get(key) else 'OFF'}")
    try:
        await cb.message.edit_text(_panel(cfg), parse_mode=enums.ParseMode.HTML,
                                   reply_markup=_panel_kb(cfg))
    except Exception:
        pass
