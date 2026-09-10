"""
🐍 /eval — evaluate Python code (owner only)
"""
import io
import sys
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from melody import bot
from utils.decorators import owner_only, error_handler
from utils.formatters import quote_html


@bot.on_message(filters.command(["eval", "py"]) & filters.private)
@owner_only
@error_handler
async def eval_cmd(client: Client, message: Message):
    code = " ".join(message.command[1:])
    if message.reply_to_message:
        code = message.reply_to_message.text or code
    if not code:
        return await message.reply(
            quote_html("**Usage:** `/eval <python code>`"), parse_mode=enums.ParseMode.HTML
        )

    msg = await message.reply(quote_html("⚙️ Evaluating..."), parse_mode=enums.ParseMode.HTML)

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()

    result = None
    try:
        result = eval(compile(code, "<string>", "eval"))
    except SyntaxError:
        try:
            exec(compile(code, "<string>", "exec"))
        except Exception as e:
            result = f"❌ {type(e).__name__}: {e}"
    except Exception as e:
        result = f"❌ {type(e).__name__}: {e}"
    finally:
        sys.stdout = old_stdout

    printed = buf.getvalue()
    output = ""
    if printed:
        output += f"**stdout:**\n```\n{printed[:1500]}\n```\n"
    if result is not None:
        output += f"**result:**\n```\n{str(result)[:1500]}\n```"
    if not output:
        output = "✅ Done (no output)"

    await msg.edit(quote_html(output[:4000], expandable=True), parse_mode=enums.ParseMode.HTML)
