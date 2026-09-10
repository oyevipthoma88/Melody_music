"""
🖼️ Bot pictures — set once, keep forever (reboot-proof).

Every picture is stored in MongoDB as a Telegram **file_id**, which stays
valid for the lifetime of the bot and needs no re-upload. That is what makes
"reboot karne pe dobara set na karna pade" true even on Heroku's ephemeral
filesystem — no file on disk, no GitHub push needed.

Commands (bot owner):
    /setpingpic   — picture shown by /ping
    /setalivepic  — picture shown by /alive
    /setplaypic   — fallback picture for /play /vplay /cplay
    /setstartpic  — picture shown by /start
    /setwelcomepic2 — picture for the "bot added to group" card
    /delpic <key> — remove one
    /piclist      — see everything that is set

Usage: send a photo with the command as caption, or reply to a photo.
"""
import html
import os

from pyrogram import Client, enums, filters
from pyrogram.types import Message
from pyrogram.types import LinkPreviewOptions
from utils.reply import reply_params


from melody import bot
from utils.admin_tools import card, close_kb
from utils.decorators import error_handler, owner_only
from utils.gc_db import all_pics, del_pic, get_pic
from utils.picture_assets import save_picture_file_id, schedule_picture_backup

# REQUESTED: "jitni bhi pic set hogi welcome, play etc all must mongo db +
# GitHub me save ho" — every picture is stored twice:
#   1. Mongo, as a Telegram file_id (instant re-send, no upload)
#   2. the bot's own GitHub repo, as a real file (survives even a fresh Mongo)
ASSETS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "assets")
GH_PIC_PATHS = {
    "start": "assets/bg_start.png",
    "welcome": "assets/bg_welcome.png",
    "ping": "assets/bg_ping.png",
    "alive": "assets/bg_alive.png",
    "play": "assets/bg_play.png",
}


PIC_KEYS = {
    "setpingpic": ("ping", "🏓 Ping card"),
    "setalivepic": ("alive", "💚 Alive card"),
    "setplaypic": ("play", "🎵 Play card fallback"),
    "setstartpic": ("start", "🚀 Start card"),
    "setwelcomepic2": ("welcome", "🎉 Group welcome card"),
}


def _photo_of(message: Message):
    if message.photo:
        return message.photo.file_id
    if message.reply_to_message and message.reply_to_message.photo:
        return message.reply_to_message.photo.file_id
    return None


@bot.on_message(filters.command(list(PIC_KEYS.keys())))
@owner_only
@error_handler
async def set_pic_cmd(client: Client, message: Message):
    key, label = PIC_KEYS[message.command[0].lower()]
    file_id = _photo_of(message)
    if not file_id:
        return await message.reply(
            card(
                "Sᴇᴛ Pɪᴄᴛᴜʀᴇ",
                f"🖼 <b>{html.escape(label)}</b>\n\n"
                f"<i>Photo bhejo caption me</i> <code>/{message.command[0]}</code> "
                f"<i>ya kisi photo pe reply karke command chalao.</i>",
            ),
            parse_mode=enums.ParseMode.HTML,
        )
    status = await message.reply(
        card("Pɪᴄᴛᴜʀᴇ Sᴀᴠɪɴɢ", "⏳ Telegram file ID save ho raha hai..."),
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        await save_picture_file_id(key, file_id)
    except Exception as exc:
        return await status.edit(
            card(
                "Pɪᴄᴛᴜʀᴇ Sᴀᴠᴇ Fᴀɪʟᴇᴅ",
                f"❌ <b>{html.escape(label)}</b> save nahi ho paya.\n"
                f"<code>{html.escape(str(exc))}</code>",
            ),
            parse_mode=enums.ParseMode.HTML,
        )

    gh_path = GH_PIC_PATHS[key]
    local = os.path.join(ASSETS, os.path.basename(gh_path))
    schedule_picture_backup(
        client,
        key=key,
        file_id=file_id,
        local_path=local,
        mongo_key=gh_path,
    )
    await status.edit(
        card(
            "Pɪᴄᴛᴜʀᴇ Sᴀᴠᴇᴅ",
            f"✅ <b>{html.escape(label)}</b> update ho gaya.\n"
            "💾 <i>Mongo me saved — reboot pe bhi rahega.</i>\n"
            "📦 Binary backup background me chal raha hai.",
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(filters.command("piclist"))
@owner_only
@error_handler
async def piclist_cmd(client: Client, message: Message):
    pics = await all_pics()
    lines = "\n".join(
        f"  🔸 <b>{k}</b> — {'✅ set' if k in pics else '➖ not set'}"
        for k in ("start", "ping", "alive", "play", "welcome")
    )
    await message.reply(
        card("Sᴀᴠᴇᴅ Pɪᴄᴛᴜʀᴇs", lines, "/setpingpic · /setalivepic · /setplaypic · /setstartpic"),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=close_kb(),
    )


@bot.on_message(filters.command("delpics"))
@owner_only
@error_handler
async def delpic_cmd(client: Client, message: Message):
    key = (message.command[1].lower() if len(message.command) > 1 else "")
    if key not in ("start", "ping", "alive", "play", "welcome"):
        return await message.reply(
            card("Rᴇᴍᴏᴠᴇ Pɪᴄᴛᴜʀᴇ", "🧾 <code>/delpics start|ping|alive|play|welcome</code>"),
            parse_mode=enums.ParseMode.HTML,
        )
    await del_pic(key)
    await message.reply(card("Rᴇᴍᴏᴠᴇᴅ", f"🗑 <code>{key}</code> picture hata di."), parse_mode=enums.ParseMode.HTML)


async def send_with_pic(client, chat_id: int, key: str, text: str, reply_markup=None, reply_to: int = None):
    """Send `text` as a photo caption when a picture is configured for `key`,
    otherwise as a normal message. Shared by /ping, /alive and the play cards."""
    file_id = await get_pic(key)
    if file_id:
        try:
            return await client.send_photo(
                chat_id,
                file_id,
                caption=text,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=reply_markup,
                reply_parameters=reply_params(reply_to),
            )
        except Exception:
            pass
    return await client.send_message(
        chat_id,
        text,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=reply_markup,
        reply_parameters=reply_params(reply_to),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
