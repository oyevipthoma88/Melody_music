"""
🖼️ /setpic — Set custom start/welcome picture (owner only)
   /delpic  — Remove custom start picture (owner only)

FIX: After saving bg_start.png locally, the image is also pushed to the
     GitHub repository so it persists across fresh deployments/restarts.
     Set GITHUB_TOKEN + GITHUB_REPO in your .env to enable this. On the
     next startup, melody/__main__.py pulls it back down from GitHub if
     the local file is missing (see utils/github_assets.py) — the push
     alone isn't enough on ephemeral filesystems like Heroku's.
     The push is best-effort — a failure is reported but does NOT block
     the local save, so the pic works immediately regardless.
"""
import html
import os
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from utils.decorators import owner_only, error_handler
from utils.formatters import quote_html
from utils.picture_assets import save_picture_file_id, schedule_picture_backup

ASSETS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "assets")
BG_START = os.path.join(ASSETS, "bg_start.png")

# GitHub target path inside the repo
GH_START_PATH = "assets/bg_start.png"


def _ensure_assets():
    os.makedirs(ASSETS, exist_ok=True)


@bot.on_message(filters.command("setpic") & filters.private)
@owner_only
@error_handler
async def setpic_cmd(client: Client, message: Message):
    """
    Usage: Send a photo with caption /setpic
    OR reply to a photo with /setpic
    """
    photo = None

    # Case 1: Message itself has a photo
    if message.photo:
        photo = message.photo

    # Case 2: Reply to a photo message
    if not photo and message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo

    if not photo:
        return await message.reply(
            quote_html(
                "🖼️ <b>Set Start Picture</b>\n\n"
                "Send a photo with caption <code>/setpic</code>\n"
                "or reply to a photo with <code>/setpic</code>"
            ),
            parse_mode=enums.ParseMode.HTML,
        )

    msg = await message.reply(quote_html("⏳ Saving picture..."), parse_mode=enums.ParseMode.HTML)

    try:
        # Telegram file_id is the fast, reboot-proof source of truth. Do not
        # make the owner wait for the optional binary backup or GitHub/Mongo IO.
        await save_picture_file_id("start", photo.file_id)
    except Exception as e:
        await msg.edit(
            quote_html(f"❌ <b>Failed to save image.</b>\n<code>{html.escape(str(e))}</code>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    schedule_picture_backup(
        client,
        key="start",
        file_id=photo.file_id,
        local_path=BG_START,
        mongo_key=GH_START_PATH,
    )
    await msg.edit(
        quote_html(
            "✅ <b>Start picture updated!</b>\n\n"
            "The new image will be shown on the next <code>/start</code> command.\n"
            "📦 Background backup bhi start ho gaya hai."
        ),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command("delpic") & filters.private)
@owner_only
@error_handler
async def delpic_cmd(client: Client, message: Message):
    """Remove the custom start picture."""
    if os.path.exists(BG_START):
        os.remove(BG_START)
        await message.reply(
            quote_html(
                "🗑️ <b>Start picture removed.</b>\n"
                "The bot will now use text-only start messages."
            ),
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await message.reply(
            quote_html("ℹ️ No custom start picture is set."),
            parse_mode=enums.ParseMode.HTML,
        )
