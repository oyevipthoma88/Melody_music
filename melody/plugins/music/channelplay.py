"""
📺 Channel Play — two ways to stream into a channel's voice chat.

1) Directly inside the channel
   Add Melody to the channel as admin and post <code>/play</code>,
   <code>/vplay</code> or <code>/cplay</code> there — it joins the channel's
   own voice chat and plays. The command post itself is deleted instantly
   (0 sec) so the channel feed stays clean.

2) From a group ("/channelplay")
   <code>/channelplay @channelusername</code> or <code>/channelplay -100…</code>
   links this group to that channel. After that EVERY <code>/cplay</code> /
   <code>/cvplay</code> typed in the group streams into the linked channel's
   voice chat instead of the group's.

   <code>/channelplay off</code>     — unlink
   <code>/channelplay status</code>  — show current link
"""
import html

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from melody import bot
from melody.logging import log_activity
from utils.admin_tools import card, close_kb, is_admin, mention
from utils.decorators import admin_or_auth, channel_admin_or_auth, error_handler
from utils.gc_db import get_linked_channel, link_channel, unlink_channel
from melody.plugins.music.play import _play_core
from utils.tasks import spawn


# ─── 1) Commands posted inside the channel itself ────────────────────────────

@bot.on_message(filters.command(["play", "vplay"]) & filters.channel)
@error_handler
@channel_admin_or_auth
async def channel_direct_play(client: Client, message: Message):
    video = message.command[0].lower() == "vplay"
    # 0-second cleanup: drop the command post immediately.
    spawn(_silent_delete(message))
    await _play_core(client, message, video=video)


async def _silent_delete(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


# ─── 2) /channelplay link management (in a group) ────────────────────────────

@bot.on_message(filters.command(["channelplay", "cplaylink"]) & filters.group)
@error_handler
async def channelplay_cmd(client: Client, message: Message):
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        return await message.reply(
            card("Cʜᴀɴɴᴇʟ Pʟᴀʏ", "⚠️ Sirf group admins hi channel link kar sakte hain."),
            parse_mode=enums.ParseMode.HTML,
        )

    arg = (message.command[1] if len(message.command) > 1 else "status").strip()

    if arg.lower() in ("off", "disable", "remove", "unlink"):
        await unlink_channel(message.chat.id)
        return await message.reply(
            card("Cʜᴀɴɴᴇʟ Uɴʟɪɴᴋᴇᴅ", "📴 Ab <code>/cplay</code> wapas is group me hi bajega."),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=close_kb(),
        )

    if arg.lower() in ("status", "info"):
        link = await get_linked_channel(message.chat.id)
        body = (
            f"📺 <b>Lɪɴᴋᴇᴅ :</b> {html.escape(link.get('title') or str(link['channel_id']))}\n"
            f"🆔 <code>{link['channel_id']}</code>"
            if link else "➖ <b>Kᴏɪ ᴄʜᴀɴɴᴇʟ ʟɪɴᴋ ɴᴀʜɪ ʜᴀɪ.</b>"
        )
        return await message.reply(
            card("Cʜᴀɴɴᴇʟ Pʟᴀʏ Sᴛᴀᴛᴜs", body, "/channelplay @username · /channelplay off"),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=close_kb(),
        )

    ref = arg.lstrip("@")
    try:
        chat = await client.get_chat(int(ref) if ref.lstrip("-").isdigit() else ref)
    except Exception as exc:
        return await message.reply(
            card(
                "Cʜᴀɴɴᴇʟ Nᴏᴛ Fᴏᴜɴᴅ",
                f"❌ <code>{html.escape(str(exc))}</code>\n\n"
                "<i>Bot aur assistant dono ko us channel me admin banao, phir dobara try karo.</i>",
            ),
            parse_mode=enums.ParseMode.HTML,
        )

    if chat.type != enums.ChatType.CHANNEL:
        return await message.reply(
            card("Nᴏᴛ A Cʜᴀɴɴᴇʟ", "❌ Ye ID/username kisi channel ka nahi hai."), parse_mode=enums.ParseMode.HTML
        )

    await link_channel(message.chat.id, chat.id, chat.title or "")
    await message.reply(
        card(
            "Cʜᴀɴɴᴇʟ Lɪɴᴋᴇᴅ",
            f"📺 <b>Cʜᴀɴɴᴇʟ :</b> {html.escape(chat.title or '')}\n"
            f"🆔 <code>{chat.id}</code>\n"
            f"👮 <b>Bʏ :</b> {mention(message.from_user)}\n\n"
            "🎧 Ab is group me <code>/cplay &lt;song&gt;</code> karoge to gaana "
            "<b>us channel ke voice chat</b> me bajega.",
            "Assistant ko channel me add karna mat bhoolna.",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )
    spawn(log_activity(
        f"#channelplay #linked\n🏠 <code>{message.chat.id}</code> → 📺 <code>{chat.id}</code>"
    ))


# ─── 3) /cplay + /cvplay typed in a group → stream into the linked channel ───

async def _group_cplay(client: Client, message: Message, video: bool):
    link = await get_linked_channel(message.chat.id)
    if not link:
        return await message.reply(
            card(
                "Cʜᴀɴɴᴇʟ Nᴏᴛ Lɪɴᴋᴇᴅ",
                "📺 <b>Pehle channel connect karo :</b>\n"
                "<code>/channelplay @channelusername</code>\n"
                "<code>/channelplay -1001234567890</code>",
            ),
            parse_mode=enums.ParseMode.HTML,
        )
    try:
        target = await client.get_chat(link["channel_id"])
    except Exception:
        target = None
    if target is None:
        return await message.reply(
            card("Cʜᴀɴɴᴇʟ Uɴʀᴇᴀᴄʜᴀʙʟᴇ", "❌ Channel access nahi mila — bot ko admin banao."),
            parse_mode=enums.ParseMode.HTML,
        )
    await _play_core(client, message, video=video, stream_chat=target)


@bot.on_message(filters.command("cplay") & filters.group)
@error_handler
@admin_or_auth
async def group_cplay_cmd(client: Client, message: Message):
    await _group_cplay(client, message, video=False)


@bot.on_message(filters.command("cvplay") & filters.group)
@error_handler
@admin_or_auth
async def group_cvplay_cmd(client: Client, message: Message):
    await _group_cplay(client, message, video=True)
