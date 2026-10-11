"""
💬 /q — chat ka screenshot (Quotly-style sticker) + 🧩 /kang sticker packs.

/q            reply kiye message ka quote sticker
/q 3          reply se aage ke 3 messages (max 10) ek hi sticker me
/q r          replied message jis message ka reply tha, wo bhi andar dikhe
/q 3 r        dono saath
/kang [emoji] sticker / photo / image file pe reply → aapke pack me add
              (pehle bot ko DM me /start karna zaroori hai)

Queue ab /queue · /que pe hai (/q quote ke liye free kiya gaya).
"""
import asyncio
import io
import re

from pyrogram import Client, filters, raw
from pyrogram.errors import RPCError
from pyrogram.file_id import FileId
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from melody import bot
from melody.logging import LOGGER
from utils.buttons import ikb
from utils.database import db
from utils.decorators import error_handler
from utils.quote_render import render_quote, to_sticker_webp

kang_col = db["kang_users"]

MAX_Q = 10
DEFAULT_EMOJI = "🎵"
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF]")
_AVATAR_CACHE: dict = {}


# ─── DM start tracking (kang needs it) ───────────────────────────────────────

@bot.on_message(filters.private & filters.incoming, group=-7)
async def _track_dm_user(_client: Client, message: Message):
    if message.from_user and not message.from_user.is_bot:
        try:
            await kang_col.update_one(
                {"_id": message.from_user.id},
                {"$set": {"started": True}},
                upsert=True,
            )
        except Exception:  # noqa: BLE001
            pass


async def _has_started(user_id: int) -> bool:
    try:
        doc = await kang_col.find_one({"_id": user_id})
        return bool(doc and doc.get("started"))
    except Exception:  # noqa: BLE001
        return False


def _start_kb(client: Client) -> InlineKeyboardMarkup:
    uname = getattr(client.me, "username", "") or ""
    return InlineKeyboardMarkup([[ikb("🤖 Sᴛᴀʀᴛ ʙᴏᴛ ɪɴ DM", url=f"https://t.me/{uname}?start=kang")]])


# ─── /q helpers ──────────────────────────────────────────────────────────────

def _sender(m: Message):
    """(id, display name, photo file_id) for a message sender."""
    fwd = getattr(m, "forward_from", None)
    if fwd:
        u = fwd
    elif m.from_user:
        u = m.from_user
    else:
        u = None
    if u is not None:
        name = " ".join(x for x in (u.first_name, u.last_name) if x) or (u.username or "User")
        return u.id, name, (u.photo.small_file_id if u.photo else None)
    c = m.sender_chat or getattr(m, "forward_from_chat", None) or m.chat
    name = getattr(m, "forward_sender_name", None) or getattr(c, "title", None) or "Anonymous"
    photo = getattr(c, "photo", None)
    return getattr(c, "id", 0) or 0, name, (photo.small_file_id if photo else None)


def _msg_text(m: Message) -> str:
    t = m.text or m.caption or ""
    if t:
        return t[:1500]
    if m.sticker:
        return "" if not (m.sticker.is_animated or m.sticker.is_video) else f"{m.sticker.emoji or ''} Sticker"
    for attr, label in (("voice", "🎤 Voice message"), ("audio", "🎵 Audio"),
                        ("video", "🎬 Video"), ("animation", "🎞 GIF"),
                        ("document", "📄 File"), ("poll", "📊 Poll"),
                        ("location", "📍 Location"), ("contact", "👤 Contact")):
        if getattr(m, attr, None):
            if attr == "audio" and m.audio.title:
                return f"🎵 {m.audio.title}"
            if attr == "document" and m.document.file_name:
                return f"📄 {m.document.file_name}"
            return label
    return ""


async def _dl(client: Client, file_id: str | None, limit: int = 8 * 1024 * 1024) -> bytes | None:
    if not file_id:
        return None
    try:
        bio = await asyncio.wait_for(client.download_media(file_id, in_memory=True), timeout=20)
        data = bio.getvalue() if bio else None
        return data if data and len(data) <= limit else None
    except Exception:  # noqa: BLE001
        return None


async def _avatar(client: Client, file_id: str | None) -> bytes | None:
    if not file_id:
        return None
    if file_id in _AVATAR_CACHE:
        return _AVATAR_CACHE[file_id]
    data = await _dl(client, file_id, 2 * 1024 * 1024)
    if len(_AVATAR_CACHE) > 300:
        _AVATAR_CACHE.clear()
    _AVATAR_CACHE[file_id] = data
    return data


async def _media(client: Client, m: Message) -> bytes | None:
    if m.photo:
        return await _dl(client, m.photo.file_id)
    if m.sticker and not (m.sticker.is_animated or m.sticker.is_video):
        return await _dl(client, m.sticker.file_id)
    thumbs = None
    for attr in ("video", "animation", "document"):
        obj = getattr(m, attr, None)
        if obj is not None and getattr(obj, "thumbs", None):
            thumbs = obj.thumbs
            break
    if thumbs:
        return await _dl(client, thumbs[-1].file_id)
    return None


async def _item(client: Client, m: Message, with_reply: bool) -> dict:
    uid, name, photo = _sender(m)
    avatar, media = await asyncio.gather(_avatar(client, photo), _media(client, m))
    it = {"user_id": uid, "name": name, "avatar": avatar, "text": _msg_text(m), "media": media}
    r = m.reply_to_message if with_reply else None
    if r is not None and not getattr(r, "empty", False):
        rid, rname, _ = _sender(r)
        it.update(reply_user_id=rid, reply_name=rname, reply_text=_msg_text(r) or "📎 Media")
    return it


def _parse_q_args(message: Message) -> tuple[int, bool]:
    count, with_reply = 1, False
    for a in (message.command or [])[1:]:
        a = a.lower().strip()
        if a in ("r", "reply", "-r"):
            with_reply = True
        elif a.isdigit():
            count = max(1, min(MAX_Q, int(a)))
    return count, with_reply


# ─── /q ──────────────────────────────────────────────────────────────────────

@bot.on_message(filters.command(["q", "quote"]))
@error_handler
async def quote_cmd(client: Client, message: Message):
    target = message.reply_to_message
    if not target:
        await message.reply_text(
            "💬 <b>Kisi message pe reply karke</b> <code>/q</code> bhejo.\n"
            "• <code>/q 3</code> — 3 messages ek saath\n"
            "• <code>/q r</code> — reply wala message bhi dikhao\n"
            "• Queue ke liye ab <code>/queue</code> ya <code>/que</code>"
        )
        return
    count, with_reply = _parse_q_args(message)
    wait = await message.reply_text("🎨 <i>Screenshot ban raha hai…</i>")
    try:
        if count > 1:
            ids = list(range(target.id, target.id + count + 5))
            got = await client.get_messages(message.chat.id, ids)
            msgs = [m for m in (got if isinstance(got, list) else [got])
                    if m and not getattr(m, "empty", False) and not m.service
                    and m.id != message.id and m.id != wait.id
                    and not (m.text or "").startswith(("/q", "/quote"))]
            msgs = msgs[:count] or [target]
        else:
            msgs = [target]
        items = await asyncio.gather(*(_item(client, m, with_reply) for m in msgs))
        items = [i for i in items if i["text"] or i["media"]]
        if not items:
            await wait.edit_text("❌ Is message me quote karne layak kuch nahi mila.")
            return
        webp = await asyncio.to_thread(render_quote, items)
        bio = io.BytesIO(webp)
        bio.name = "quote.webp"
        await message.reply_sticker(
            bio,
            reply_markup=InlineKeyboardMarkup([[ikb("➕ Kᴀɴɢ", callback_data="kangq")]]),
        )
    finally:
        try:
            await wait.delete()
        except Exception:  # noqa: BLE001
            pass


# ─── /kang ───────────────────────────────────────────────────────────────────

def _pack_name(client: Client, user_id: int, idx: int) -> str:
    uname = (getattr(client.me, "username", "") or "bot").lower()
    base = f"m{user_id}_{idx}" if idx else f"m{user_id}"
    return f"{base}_by_{uname}"[:64]


async def _input_document(client: Client, src: Message, user_id: int):
    """InputDocument for the source media (reused as-is for stickers)."""
    st = src.sticker
    if st is not None:
        fid = FileId.decode(st.file_id)
        return raw.types.InputDocument(
            id=fid.media_id, access_hash=fid.access_hash, file_reference=fid.file_reference,
        ), (st.emoji or None)
    file_id = None
    if src.photo:
        file_id = src.photo.file_id
    elif src.document and (src.document.mime_type or "").startswith("image/"):
        file_id = src.document.file_id
    if not file_id:
        return None, None
    data = await _dl(client, file_id, 10 * 1024 * 1024)
    if not data:
        raise ValueError("download failed")
    webp = await asyncio.to_thread(to_sticker_webp, data)
    bio = io.BytesIO(webp)
    bio.name = "sticker.webp"
    up = await client.save_file(bio)
    media = await client.invoke(raw.functions.messages.UploadMedia(
        peer=await client.resolve_peer(user_id),
        media=raw.types.InputMediaUploadedDocument(
            file=up, mime_type="image/webp",
            attributes=[raw.types.DocumentAttributeFilename(file_name="sticker.webp")],
        ),
    ))
    doc = media.document
    return raw.types.InputDocument(
        id=doc.id, access_hash=doc.access_hash, file_reference=doc.file_reference,
    ), None


async def kang(client: Client, user, src: Message, emoji: str | None = None) -> str:
    """Add `src` to user's pack. Returns the pack short name."""
    doc, src_emoji = await _input_document(client, src, user.id)
    if doc is None:
        raise ValueError("unsupported")
    item = raw.types.InputStickerSetItem(document=doc, emoji=emoji or src_emoji or DEFAULT_EMOJI)
    rec = await kang_col.find_one({"_id": user.id}) or {}
    idx = int(rec.get("pack_idx") or 0)
    title_base = (user.first_name or "User")[:40]
    for _ in range(10):
        name = _pack_name(client, user.id, idx)
        try:
            await client.invoke(raw.functions.stickers.AddStickerToSet(
                stickerset=raw.types.InputStickerSetShortName(short_name=name), sticker=item,
            ))
            break
        except RPCError as e:
            err = str(getattr(e, "ID", "") or e).upper()
            if "STICKERS_TOO_MUCH" in err:
                idx += 1
                continue
            if "STICKERSET_INVALID" in err or "STICKERSET_NOT_FOUND" in str(e).upper():
                await client.invoke(raw.functions.stickers.CreateStickerSet(
                    user_id=await client.resolve_peer(user.id),
                    title=f"{title_base}'s Melody Pack" + (f" {idx + 1}" if idx else ""),
                    short_name=name,
                    stickers=[item],
                ))
                break
            raise
    else:
        raise ValueError("no free pack")
    await kang_col.update_one({"_id": user.id}, {"$set": {"pack_idx": idx}}, upsert=True)
    return name


def _kang_error_text(e: Exception) -> str:
    s = str(e).upper()
    if "PEER_ID_INVALID" in s or "USER_IS_BLOCKED" in s or "INPUT_USER_DEACTIVATED" in s:
        return "🤖 Pehle bot ko DM me <b>/start</b> karo, phir dubara /kang bhejo."
    if "unsupported" in str(e):
        return "❌ Sirf <b>sticker, photo ya image file</b> kang ho sakti hai."
    if "STICKER_PNG_DIMENSIONS" in s or "STICKER_FILE_INVALID" in s:
        return "❌ Ye image sticker format me convert nahi ho payi."
    if "STICKER_VIDEO" in s or "STICKER_TGS" in s:
        return "❌ Ye animated sticker is pack me add nahi ho paya."
    return f"❌ Kang fail: <code>{str(e)[:150]}</code>"


@bot.on_message(filters.command(["kang", "steal"]))
@error_handler
async def kang_cmd(client: Client, message: Message):
    user = message.from_user
    if not user:
        await message.reply_text("❌ Anonymous admin se kang nahi ho sakta.")
        return
    src = message.reply_to_message
    if not src or not (src.sticker or src.photo or (src.document and (src.document.mime_type or "").startswith("image/"))):
        await message.reply_text("🧩 Kisi <b>sticker / photo</b> pe reply karke <code>/kang</code> bhejo.\n"
                                 "Emoji bhi de sakte ho: <code>/kang 😎</code>")
        return
    if not await _has_started(user.id):
        await message.reply_text("🤖 <b>Kang ke liye pehle bot ko DM me start karo</b>, phir dubara /kang bhejo.",
                                 reply_markup=_start_kb(client))
        return
    emoji = None
    m = _EMOJI_RE.search(" ".join((message.command or [])[1:]))
    if m:
        emoji = m.group(0)
    wait = await message.reply_text("🧩 <i>Kang ho raha hai…</i>")
    try:
        name = await kang(client, user, src, emoji)
    except Exception as e:  # noqa: BLE001
        LOGGER.info("kang failed for %s: %s", user.id, e)
        await wait.edit_text(_kang_error_text(e), reply_markup=_start_kb(client)
                             if "PEER_ID" in str(e).upper() else None)
        return
    await wait.edit_text(
        "✅ <b>Sticker kang ho gaya!</b>",
        reply_markup=InlineKeyboardMarkup([[ikb("📦 Pᴀᴄᴋ ᴅᴇᴋʜᴏ", url=f"https://t.me/addstickers/{name}")]]),
    )


@bot.on_callback_query(filters.regex(r"^kangq$"))
async def kang_quote_cb(client: Client, cq: CallbackQuery):
    user = cq.from_user
    if not cq.message or not cq.message.sticker:
        await cq.answer("Sticker nahi mila.", show_alert=True)
        return
    if not await _has_started(user.id):
        await cq.answer("Pehle bot ko DM me /start karo, phir Kang dabao.", show_alert=True)
        return
    try:
        name = await kang(client, user, cq.message)
    except Exception as e:  # noqa: BLE001
        msg = re.sub(r"<[^>]+>", "", _kang_error_text(e))
        await cq.answer(msg[:190], show_alert=True)
        return
    await cq.answer("✅ Aapke pack me add ho gaya!", show_alert=False)
    try:
        await client.send_message(user.id, f"✅ Quote sticker add ho gaya: https://t.me/addstickers/{name}")
    except Exception:  # noqa: BLE001
        pass
