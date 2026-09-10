"""
ℹ️ /about — About page with HTML blockquote formatting
"""
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup
from melody import bot
from utils.decorators import error_handler
from utils.formatters import send_quote, premium_emoji, PREMIUM_EMOJI_IDS
from strings.themes import RED, btn, fancy

ABOUT_TEXT = (
    f"<blockquote>{premium_emoji(PREMIUM_EMOJI_IDS['about'], '🎶')} <b>About {fancy('Melody')}</b></blockquote>\n\n"
    "<b>Melody</b> is a premium Telegram music bot that streams\n"
    "high-quality audio from YouTube.\n\n"
    "<blockquote>"
    "🔥 <b>Features:</b>\n"
    "  🔸 HD YouTube streaming\n"
    "  🔸 Smart queue management\n"
    "  🔸 AutoPlay with related songs\n"
    "  🔸 Genius lyrics integration\n"
    "  🔸 Beautiful now-playing cards\n"
    "  🔸 Group admin controls"
    "</blockquote>\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "Made with 💛 for music lovers\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━"
)


@bot.on_message(filters.command("about"))
@error_handler
async def about_cmd(client: Client, message: Message):
    await send_quote(
        message,
        ABOUT_TEXT,
        client=client,
        reply_markup=InlineKeyboardMarkup([
            [ikb(btn("✵ CLOSE ✵", RED), callback_data="about_close")],
        ]),
    )


from pyrogram.types import CallbackQuery
from utils.buttons import ikb  # premium-emoji + styled buttons (safe on every fork)

@bot.on_callback_query(filters.regex(r"^about_close$"))
@error_handler
async def about_close_cb(client: Client, cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer()
