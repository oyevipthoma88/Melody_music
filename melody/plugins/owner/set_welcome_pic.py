"""
🖼️ /setwelcomepic — Set the picture shown when the bot joins a NEW GROUP
   /delwelcomepic  — Remove the custom group-welcome picture (owner only)

This is a separate image from the /setpic start picture: /setpic controls
what /start shows in DM, this controls the "thanks for adding me" card
posted in a group the moment the bot joins it (see
melody/plugins/misc/start.py::new_group_handler).

Same GitHub persistence pattern as /setpic — pushed on save, pulled back
on the next startup if missing locally (see utils/github_assets.py) so it
survives Heroku's ephemeral filesystem.
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
BG_WELCOME = os.path.join(ASSETS, "bg_welcome.png")

# GitHub target path inside the repo
GH_WELCOME_PATH = "assets/bg_welcome.png"


def _ensure_assets():
    os.makedirs(ASSETS, exist_ok=True)


@bot.on_message(filters.command("setwelcomepic") & filters.private)
@owner_only
@error_handler
async def setwelcomepic_cmd(client: Client, message: Message):
    """
    Usage: Send a photo with caption /setwelcomepic
    OR reply to a photo with /setwelcomepic
    """
    photo = None

    if message.photo:
        photo = message.photo
    if not photo and message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo

    if not photo:
        return await message.reply(
            quote_html(
                "🖼️ <b>Set Welcome (GC) Picture</b>\n\n"
                "Shown when the bot is added to a new group.\n\n"
                "Send a photo with caption <code>/setwelcomepic</code>\n"
                "or reply to a photo with <code>/setwelcomepic</code>"
            ),
            parse_mode=enums.ParseMode.HTML,
        )

    msg = await message.reply(quote_html("⏳ Saving picture..."), parse_mode=enums.ParseMode.HTML)

    try:
        await save_picture_file_id("welcome", photo.file_id)
    except Exception as e:
        await msg.edit(
            quote_html(f"❌ <b>Failed to save image.</b>\n<code>{html.escape(str(e))}</code>"),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    schedule_picture_backup(
        client,
        key="welcome",
        file_id=photo.file_id,
        local_path=BG_WELCOME,
        mongo_key=GH_WELCOME_PATH,
    )
    await msg.edit(
        quote_html(
            "✅ <b>Welcome (GC) picture updated!</b>\n\n"
            "It will be shown the next time the bot is added to a group.\n"
            "📦 Background backup bhi start ho gaya hai."
        ),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command("delwelcomepic") & filters.private)
@owner_only
@error_handler
async def delwelcomepic_cmd(client: Client, message: Message):
    """Remove the custom group-welcome picture."""
    if os.path.exists(BG_WELCOME):
        os.remove(BG_WELCOME)
        await message.reply(
            quote_html(
                "🗑️ <b>Welcome (GC) picture removed.</b>\n"
                "New groups will fall back to the start picture, or a text-only card."
            ),
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await message.reply(
            quote_html("ℹ️ No custom welcome (GC) picture is set."),
            parse_mode=enums.ParseMode.HTML,
        )
