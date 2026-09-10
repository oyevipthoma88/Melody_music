"""Melody Coins — a persistent, non-gambling group economy.

Commands:
    /balance, /coins, /profile
    /daily, /work
    /deposit <amount>, /withdraw <amount>
    /pay <user|reply> <amount>
    /leaderboard, /economy

All rewards are deterministic to the user but use small bounded random ranges
for variety. No betting, casino, or real-money value is involved.
"""
from __future__ import annotations

import html
import re
from pyrogram import Client, enums, filters
from pyrogram.types import Message

from melody import bot
from utils.admin_tools import card
from utils.decorators import error_handler
from utils.economy_db import (
    claim_daily,
    leaderboard,
    move_money,
    profile,
    transfer,
    work,
)


_AMOUNT_RE = re.compile(r"^[1-9]\d{0,8}$")


def _name(user) -> str:
    return " ".join(filter(None, [getattr(user, "first_name", ""), getattr(user, "last_name", "")])) or "Player"


def _fmt(n: int) -> str:
    return f"{int(n):,}"


def _cooldown(seconds: int) -> str:
    seconds = max(1, int(seconds))
    minutes, sec = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def _profile_card(user, doc: dict, title: str = "Mᴇʟᴏᴅʏ Cᴏɪɴs") -> str:
    wallet = int(doc.get("wallet") or 0)
    bank = int(doc.get("bank") or 0)
    total = wallet + bank
    level = int(doc.get("level") or 1)
    xp = int(doc.get("xp") or 0)
    streak = int(doc.get("streak") or 0)
    return card(
        title,
        f"👤 <b>{html.escape(_name(user))}</b>\n"
        f"🏦 Wallet: <b>🪙 {_fmt(wallet)}</b>\n"
        f"💳 Bank: <b>🪙 {_fmt(bank)}</b>\n"
        f"💎 Net worth: <b>🪙 {_fmt(total)}</b>\n\n"
        f"⭐ Level: <b>{level}</b>  ·  XP: <b>{_fmt(xp)}</b>\n"
        f"🔥 Daily streak: <b>{streak}</b>\n\n"
        f"<i>Earn coins, level up, and build your group legacy.</i>",
    )


def _amount(raw: str | None) -> int | None:
    raw = (raw or "").replace(",", "").strip()
    if not _AMOUNT_RE.fullmatch(raw):
        return None
    value = int(raw)
    return value if 1 <= value <= 100_000_000 else None


async def _target(client: Client, message: Message, index: int = 1):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user, index
    if len(message.command or []) <= index:
        return None, index
    ref = message.command[index]
    try:
        return await client.get_users(ref), index + 1
    except Exception:
        return None, index + 1


@bot.on_message(filters.command(["balance", "bal", "coins", "profile"]) & (filters.group | filters.private))
@error_handler
async def economy_balance(client: Client, message: Message):
    user = message.from_user
    if not user:
        return
    doc = await profile(user.id, _name(user))
    await message.reply(_profile_card(user, doc), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command(["daily", "claim"]) & (filters.group | filters.private))
@error_handler
async def economy_daily(client: Client, message: Message):
    user = message.from_user
    if not user:
        return
    ok, reward, extra, doc = await claim_daily(user.id, _name(user))
    if not ok:
        return await message.reply(
            card("Dᴀɪʟʏ Cᴏᴏʟᴅᴏᴡɴ", f"⏳ Agla daily reward <b>{_cooldown(extra)}</b> me available hoga.\n\n💡 Tab tak <code>/work</code> try karo."),
            parse_mode=enums.ParseMode.HTML,
        )
    await message.reply(
        card("Dᴀɪʟʏ Rᴇᴡᴀʀᴅ", f"🎁 <b>+🪙 {_fmt(reward)}</b> coins added!\n🔥 Streak: <b>{extra}</b> days\n💰 Wallet: <b>🪙 {_fmt(doc.get('wallet', 0))}</b>"),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command(["work", "job"]) & (filters.group | filters.private))
@error_handler
async def economy_work(client: Client, message: Message):
    user = message.from_user
    if not user:
        return
    ok, reward, wait, doc = await work(user.id, _name(user))
    if not ok:
        return await message.reply(
            card("Wᴏʀᴋ Cᴏᴏʟᴅᴏᴡɴ", f"⏳ Next shift <b>{_cooldown(wait)}</b> me available hogi."),
            parse_mode=enums.ParseMode.HTML,
        )
    await message.reply(
        card("Wᴏʀᴋ Cᴏᴍᴘʟᴇᴛᴇ", f"💼 Shift complete!\n\n💵 Earned: <b>🪙 {_fmt(reward)}</b>\n💰 Wallet: <b>🪙 {_fmt(doc.get('wallet', 0))}</b>\n⭐ XP: <b>+10</b>"),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command(["deposit", "dep"]) & (filters.group | filters.private))
@error_handler
async def economy_deposit(client: Client, message: Message):
    user = message.from_user
    amount = _amount(message.command[1] if len(message.command or []) > 1 else None)
    if not user or amount is None:
        return await message.reply("Usage: <code>/deposit 500</code>", parse_mode=enums.ParseMode.HTML)
    ok, doc = await move_money(user.id, amount, "wallet", "bank")
    if not ok:
        return await message.reply("❌ Wallet me itne coins nahi hain.")
    await message.reply(card("Bᴀɴᴋ Dᴇᴘᴏsɪᴛ", f"✅ Bank me <b>🪙 {_fmt(amount)}</b> deposit kiye.\n🏦 Bank: <b>🪙 {_fmt(doc.get('bank', 0))}</b>"), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command(["withdraw", "with"]) & (filters.group | filters.private))
@error_handler
async def economy_withdraw(client: Client, message: Message):
    user = message.from_user
    amount = _amount(message.command[1] if len(message.command or []) > 1 else None)
    if not user or amount is None:
        return await message.reply("Usage: <code>/withdraw 500</code>", parse_mode=enums.ParseMode.HTML)
    ok, doc = await move_money(user.id, amount, "bank", "wallet")
    if not ok:
        return await message.reply("❌ Bank me itne coins nahi hain.")
    await message.reply(card("Bᴀɴᴋ Wɪᴛʜᴅʀᴀᴡ", f"✅ Wallet me <b>🪙 {_fmt(amount)}</b> withdraw kiye.\n💰 Wallet: <b>🪙 {_fmt(doc.get('wallet', 0))}</b>"), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command(["pay", "give", "transfer"]) & (filters.group | filters.private))
@error_handler
async def economy_pay(client: Client, message: Message):
    sender = message.from_user
    if not sender:
        return
    target, amount_index = await _target(client, message, 1)
    amount = _amount(message.command[amount_index] if len(message.command or []) > amount_index else None)
    if not target or amount is None:
        return await message.reply("Usage: reply karke <code>/pay 500</code> ya <code>/pay @user 500</code>", parse_mode=enums.ParseMode.HTML)
    ok, reason, receiver = await transfer(sender.id, target.id, amount, _name(sender), _name(target))
    if not ok:
        return await message.reply(f"❌ {html.escape(reason)}")
    await message.reply(
        card("Cᴏɪɴ Tʀᴀɴsғᴇʀ", f"✅ <b>{html.escape(_name(sender))}</b> ne <b>{html.escape(_name(target))}</b> ko\n🪙 <b>{_fmt(amount)}</b> coins bheje.\n\n💰 Receiver wallet: <b>🪙 {_fmt(receiver.get('wallet', 0))}</b>"),
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_message(filters.command(["leaderboard", "rich", "topcoins"]) & (filters.group | filters.private))
@error_handler
async def economy_leaderboard(client: Client, message: Message):
    rows = await leaderboard(10)
    if not rows:
        return await message.reply(card("Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ", "Abhi koi player nahi hai. <code>/daily</code> se start karo!"), parse_mode=enums.ParseMode.HTML)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, row in enumerate(rows, 1):
        total = int(row.get("wallet") or 0) + int(row.get("bank") or 0)
        icon = medals[idx - 1] if idx <= 3 else f"<b>{idx:02d}</b>"
        lines.append(f"{icon} {html.escape(row.get('name') or 'Player')[:24]} · <b>🪙 {_fmt(total)}</b> · Lv.{int(row.get('level') or 1)}")
    await message.reply(card("Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ", "\n".join(lines) + "\n\n<i>Group legacy is built one coin at a time.</i>"), parse_mode=enums.ParseMode.HTML)


@bot.on_message(filters.command(["economy", "economyhelp", "coinhelp"]) & (filters.group | filters.private))
@error_handler
async def economy_help(client: Client, message: Message):
    await message.reply(
        card("Mᴇʟᴏᴅʏ Eᴄᴏɴᴏᴍʏ", "🪙 <code>/balance</code> — wallet, bank, level\n🎁 <code>/daily</code> — daily reward\n💼 <code>/work</code> — earn coins\n🏦 <code>/deposit 500</code> / <code>/withdraw 500</code>\n💸 <code>/pay @user 500</code> — transfer\n🏆 <code>/leaderboard</code> — top players\n\n<i>No betting, no real-money value — just a group game.</i>"),
        parse_mode=enums.ParseMode.HTML,
    )
