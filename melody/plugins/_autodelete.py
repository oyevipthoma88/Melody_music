"""
🧹 Global command auto-delete + 📋 A2Z activity logger
────────────────────────────────────────────────────────────────────────────
Requirement #2: the user's own command message must vanish instantly
(0.0 sec) after Telegram delivers it to the bot.

Requirement #3: EVERY command used anywhere (not just errors) must be
logged to LOG_GROUP_ID — a full "A2Z" activity trail.

Implementation notes
─────────────────────
• Registered in `group=-1` (Pyrogram dispatches groups in ascending order),
  so this runs BEFORE the real command handler in group 0 gets a chance.
• The delete is fired with `asyncio.create_task()` and NOT awaited before
  we hand off control — the coroutine that actually calls
  `message.delete()` starts executing on the very next event-loop tick,
  which is as close to "0.0 sec" as an async Telegram bot can get. We do
  not block command processing on the delete's network round-trip.
• `raise ContinuePropagation` lets Pyrogram continue on to the next group
  so the actual command handler (in group 0) still runs normally.
• Deletion requires the bot to have "Delete Messages" admin rights in the
  group. If it doesn't, the delete silently no-ops — we never surface a
  permission error to the chat, only to LOG_GROUP_ID (debug-level, so it
  doesn't spam the log group on every single message).
"""
import html
from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import Message
from melody import bot
from melody.logging import log_activity, LOGGER
from utils.tasks import spawn

ALL_COMMANDS = [
    "start", "help", "about", "ping", "stats",
    "play", "vplay", "playforce", "vplayforce", "playlist",
    "pause", "resume", "skip", "s", "stop",
    "queue", "q", "np", "volume", "mute", "unmute",
    "loop", "loopall", "noloop", "shuffle", "clearqueue", "remove",
    "seek", "seekback", "rewind", "speed", "search", "lyrics", "autoplay",
    "auth", "unauth", "authlist", "ban", "unban", "gban", "ungban",
    "broadcast", "chatlist", "maintenance",
    "reboot", "restart", "reload", "update", "logs", "panel",
    "activevc", "setpic", "delpic",
    "eval", "py", "shell", "sh", "bash", "exec",
    # GAP FIX: the group-management / protection / promo suites were added
    # later but never listed here, so their command messages were neither
    # auto-deleted nor written to the A2Z activity log.
    "gcban", "gcunban", "kick", "dkick", "punch", "gcmute", "gcunmute",
    "vcmute", "vcunmute", "tmute",
    "promote", "fpromote", "fullpromote", "demote", "dpromote",
    "purge", "spurge", "del", "delete",
    # NEW housekeeping suite (melody/plugins/admin/cleanall.py)
    "cleanall", "clearchat", "nuke", "purgeme", "kickme", "zombies",
    "pin", "unpin", "unpinall",
    "warn", "warns", "resetwarns", "rmwarns",
    "admins", "adminlist", "staff", "report", "reportuser",
    "tagall", "all", "mention", "cancel", "canceltag", "stoptag",
    "invitelink", "link", "info", "whois", "lock", "unlock",
    "muteall", "unmuteall", "banall", "unbanall",
    "protection", "guard", "antispam",
    "channelplay", "cplaylink",
    "alive", "setstartpic", "setwelcomepic", "delstartpic",
    # Greetings suite (melody/plugins/admin/greetings.py)
    "welcome", "greetings", "greeting", "goodbye", "leftmsg", "left",
    "setwelcome", "setgoodbye", "resetwelcome", "clearwelcome",
    "resetgoodbye", "cleargoodbye", "resetgreetings", "resetgreeting",
    "cleanwelcome", "cleangoodbye", "cleanservice", "cleanservicemsg",
    "welcomecard", "welcomethumb", "goodbyecard", "goodbyethumb",
    "setwelcomeimage", "setwelcomepicgc", "delwelcomeimage", "rmwelcomeimage",
    "welcometest", "testwelcome", "goodbyetest", "testgoodbye",
    # Bulk join-request suite (melody/plugins/admin/join_approve.py)
    "rapproveall", "requestapproveall", "approveallrequests",
    "rdeclineall", "requestdeclineall", "declineallrequests",
    "rpending", "joinrequests", "pendingrequests",
    # Owner Assistant (melody/plugins/admin/owner_assistant.py)
    "assistant", "oa", "ownerassistant", "oaset", "assistantset",
    "oareset", "assistantreset",
]


# Channel-play commands are authored via channel posts (filters.channel),
# which `_delete_and_log_command` below intentionally does not touch —
# channels don't let the bot "delete a channel post" the same way group
# messages work, and only admins could post it in the first place, so
# there's no spam to hide. Logged separately isn't needed either since
# _delete_and_log_command only runs on filters.group.
CHANNEL_COMMANDS = ["cplay", "cvplay", "cpause", "cresume", "cskip", "cs", "cstop"]


async def _safe_delete(message: Message):
    try:
        await message.delete()
    except Exception as exc:
        LOGGER.debug("auto-delete: could not remove message %s: %s", message.id, exc)


@bot.on_message(filters.command(ALL_COMMANDS) & filters.group, group=-1)
async def _delete_and_log_command(client: Client, message: Message):
    # Fire-and-forget deletion — do NOT await before continuing dispatch.
    spawn(_safe_delete(message))

    # A2Z logging — every command, everywhere, fire-and-forget too so the
    # log-group round trip never adds latency to the real command handler.
    user = message.from_user
    chat = message.chat
    user_label = (
        f"{html.escape(user.first_name or 'Unknown')} (<code>{user.id}</code>)"
        if user else "Unknown"
    )
    chat_label = f"{html.escape(chat.title or 'Private')} (<code>{chat.id}</code>)"
    cmd_text = html.escape(message.text or "")
    spawn(log_activity(
        f"🧾 <b>Command</b>\n"
        f"• Cmd: <code>{cmd_text}</code>\n"
        f"• User: {user_label}\n"
        f"• Chat: {chat_label}"
    ))

    raise ContinuePropagation
