"""
🔊 Volume commands
BUG FIX: @error_handler moved OUTSIDE @admin_or_auth
"""
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from melody.core.call import change_volume, mute_stream, unmute_stream
from melody.core.queue import get_volume
from utils.decorators import admin_or_auth, error_handler
from utils.formatters import quote_html, send_quote
from utils.tasks import spawn


async def _finish_volume(status, chat_id: int, vol: int, client):
    try:
        ok = await change_volume(chat_id, vol)
        text = (
            f"🔊 **Volume set to** `{vol}`" if ok else
            "⚠️ **Volume change nahi ho paya.** Voice chat me assistant ka connection issue lag raha hai — `/play` dobara try karo."
        )
        await send_quote(status, text, client=client, edit=True)
    except Exception:
        try:
            await send_quote(status, "⚠️ **Volume change nahi ho paya.**", client=client, edit=True)
        except Exception:
            pass


@bot.on_message(filters.command("volume") & filters.group)
@error_handler
@admin_or_auth
async def volume_cmd(client: Client, message: Message):
    args = message.command
    if len(args) < 2 or not args[1].isdigit():
        vol = get_volume(message.chat.id)
        await message.reply(
            quote_html(f"🔊 **Current Volume:** `{vol}/200`\n\n**Usage:** `/volume <1-200>`"),
            parse_mode=enums.ParseMode.HTML,
        )
        return
    vol = int(args[1])
    if not (1 <= vol <= 200):
        await message.reply(
            quote_html("❌ Volume must be between 1 and 200."), parse_mode=enums.ParseMode.HTML
        )
        return
    status = await message.reply(
        quote_html(f"⏳ **Setting volume to** `{vol}`..."),
        parse_mode=enums.ParseMode.HTML,
    )
    spawn(
        _finish_volume(status, message.chat.id, vol, client),
        name=f"volume-{message.chat.id}",
    )


async def _finish_mute(status, chat_id: int, unmute: bool, client):
    try:
        ok = await (unmute_stream(chat_id) if unmute else mute_stream(chat_id))
        text = (
            f"🔊 **Unmuted.** Volume: `{get_volume(chat_id)}`" if ok and unmute else
            "🔇 **Muted.**" if ok else
            "⚠️ **Unmute nahi ho paya.** Voice chat me assistant ka connection issue lag raha hai — `/play` dobara try karo." if unmute else
            "⚠️ **Mute nahi ho paya.** Voice chat me assistant ka connection issue lag raha hai — `/play` dobara try karo."
        )
        await send_quote(status, text, client=client, edit=True)
    except Exception:
        try:
            await send_quote(status, "⚠️ **Playback control complete nahi ho paya.**", client=client, edit=True)
        except Exception:
            pass


# HANDLER-GROUP FIX (/mute and /unmute did nothing for one of their two
# meanings): `melody/plugins/admin/gcmanage.py` also registers ["mute",
# "gcmute"] / ["unmute", "gcunmute"], and Pyrogram only ever runs the FIRST
# matching handler *within a handler group*. Both files registered in the
# default group 0, so whichever module happened to be imported first won and
# the other one's early-return "let the other handler take it" logic was
# never reached at all — GC mute OR stream mute was permanently dead
# depending on import order. Registering the stream versions in group=1
# makes Pyrogram dispatch both (groups run in ascending order), so the
# target/no-target split below actually decides which one replies.
@bot.on_message(filters.command(["mute", "vcmute"]) & filters.group, group=1)
@error_handler
@admin_or_auth
async def mute_cmd(client: Client, message: Message):
    # A target (reply or @user/id argument) means the user wants the GROUP
    # mute from melody/plugins/admin/gcmanage.py, not the stream mute.
    if message.reply_to_message or len(message.command) > 1:
        return
    # BUG FIX: was change_volume(chat_id, 0), which overwrote (and persisted)
    # the chat's real volume setting. py-tgcalls has a native mute().
    status = await message.reply(quote_html("⏳ **Muting...**"), parse_mode=enums.ParseMode.HTML)
    spawn(
        _finish_mute(status, message.chat.id, False, client),
        name=f"mute-{message.chat.id}",
    )


@bot.on_message(filters.command(["unmute", "vcunmute"]) & filters.group, group=1)
@error_handler
@admin_or_auth
async def unmute_cmd(client: Client, message: Message):
    # A target (reply or @user/id argument) means the user wants the GROUP
    # mute from melody/plugins/admin/gcmanage.py, not the stream mute.
    if message.reply_to_message or len(message.command) > 1:
        return
    # BUG FIX: was change_volume(chat_id, 100), which threw away whatever the
    # chat had set with /volume. Native unmute() restores it untouched.
    status = await message.reply(quote_html("⏳ **Unmuting...**"), parse_mode=enums.ParseMode.HTML)
    spawn(
        _finish_mute(status, message.chat.id, True, client),
        name=f"unmute-{message.chat.id}",
    )
