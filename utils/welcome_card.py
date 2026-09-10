"""
🖼️ Welcome / Goodbye thumbnail generator — Modi–Meloni "Melody" theme.

Renders a 1280×640 card:
  • blurred group photo (or the tri-colour gradient) as the backdrop
  • saffron → white → green light streaks + soft vignette
  • the member's DP in a circular saffron/white/green triple ring
  • big WELCOME / GOODBYE headline, name, @username, member number, group name

Everything is pure PIL (already a dependency via utils/thumbnails.py) and the
heavy raster work runs in a worker thread, so a raid of 50 joins never blocks
the event loop. Every failure path returns None — the caller then sends a
text-only card instead of crashing the join handler.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

from PIL import Image, ImageDraw, ImageFilter

from utils.thumbnails import (  # reuse the audited helpers
    GOLD,
    GREEN,
    SAFFRON,
    WHITE,
    _circle_crop,
    _get_font,
    _initials_avatar,
    _load_image_any,
    _truncate_to_width,
    fetch_dp,
)

W, H = 1280, 640
CACHE_DIR = os.path.join(tempfile.gettempdir(), "melody_greet")
os.makedirs(CACHE_DIR, exist_ok=True)

_BOLD = "Poppins-Bold.ttf"
_REG = "Poppins-Regular.ttf"


# ── background ────────────────────────────────────────────────────────────────

def _tricolour_backdrop(photo: "Image.Image | None") -> "Image.Image":
    """Blurred group photo under a saffron→white→green wash."""
    if photo is not None:
        base = photo.convert("RGB").resize((W, H), Image.LANCZOS)
        base = base.filter(ImageFilter.GaussianBlur(22))
    else:
        base = Image.new("RGB", (W, H), (16, 8, 2))

    # Darken so text always stays readable, whatever the group photo is.
    base = Image.blend(base, Image.new("RGB", (W, H), (12, 6, 2)), 0.55)

    # Diagonal tri-colour wash.
    wash = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    wd = ImageDraw.Draw(wash)
    for x in range(0, W, 4):
        ratio = x / W
        if ratio < 0.5:
            t = ratio / 0.5
            col = (
                int(255 + (255 - 255) * t),
                int(102 + (255 - 102) * t),
                int(0 + (255 - 0) * t),
            )
        else:
            t = (ratio - 0.5) / 0.5
            col = (
                int(255 + (0 - 255) * t),
                int(255 + (146 - 255) * t),
                int(255 + (70 - 255) * t),
            )
        wd.rectangle([x, 0, x + 4, H], fill=col + (46,))
    base = Image.alpha_composite(base.convert("RGBA"), wash)

    # Top / bottom accent bars.
    d = ImageDraw.Draw(base)
    d.rectangle([0, 0, W, 8], fill=SAFFRON)
    d.rectangle([0, H - 8, W, H], fill=GREEN)
    return base


def _ring_avatar(avatar: "Image.Image", size: int = 300) -> "Image.Image":
    """Circle-cropped DP with a saffron / white / green triple ring."""
    pad = 22
    canvas = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    box = [2, 2, size + pad * 2 - 2, size + pad * 2 - 2]
    d.ellipse(box, outline=SAFFRON, width=8)
    d.ellipse([b + 8 if i < 2 else b - 8 for i, b in enumerate(box)],
              outline=WHITE, width=5)
    d.ellipse([b + 14 if i < 2 else b - 14 for i, b in enumerate(box)],
              outline=GREEN, width=6)
    canvas.paste(_circle_crop(avatar, size), (pad, pad), _circle_crop(avatar, size))
    return canvas


def _render_sync(
    out_path: str,
    kind: str,
    name: str,
    username: str,
    chat_title: str,
    member_no: "int | None",
    avatar_path: "str | None",
    chat_photo_path: "str | None",
) -> "str | None":
    try:
        card = _tricolour_backdrop(_load_image_any(chat_photo_path or ""))
        d = ImageDraw.Draw(card)

        avatar = _load_image_any(avatar_path or "") or _initials_avatar(name, 300, SAFFRON)
        ring = _ring_avatar(avatar, 300)
        card.paste(ring, (70, (H - ring.height) // 2), ring)

        x = 470
        headline = "WELCOME" if kind == "welcome" else "GOODBYE"
        sub = "ɢʀᴏᴜᴘ ᴍᴇɪɴ sᴡᴀᴀɢᴀᴛ ʜᴀɪ 🎶" if kind == "welcome" else "ᴘʜɪʀ ᴍɪʟᴇɴɢᴇ 🎶"

        f_head = _get_font(_BOLD, 92)
        f_name = _get_font(_BOLD, 54)
        f_meta = _get_font(_REG, 34)
        f_small = _get_font(_REG, 30)

        # Headline with a soft shadow so it pops on any backdrop.
        d.text((x + 3, 103), headline, font=f_head, fill=(0, 0, 0, 160))
        d.text((x, 100), headline, font=f_head, fill=SAFFRON if kind == "welcome" else "#FF3B3B")

        d.text((x, 205), sub, font=f_small, fill=GOLD)

        max_w = W - x - 60
        d.text((x, 262), _truncate_to_width(d, name, f_name, max_w), font=f_name, fill=WHITE)

        line = username if username else "—"
        d.text((x, 336), _truncate_to_width(d, line, f_meta, max_w), font=f_meta, fill="#DDDDDD")

        if member_no:
            label = f"Mᴇᴍʙᴇʀ #{member_no}" if kind == "welcome" else f"{member_no} ᴍᴇᴍʙᴇʀs ʟᴇғᴛ"
            d.text((x, 386), label, font=f_meta, fill=GREEN if kind == "welcome" else "#FF9A9A")

        d.text(
            (x, 452),
            _truncate_to_width(d, chat_title, f_small, max_w),
            font=f_small,
            fill="#F0F0F0",
        )
        d.text((x, 500), "🎶 ᴍᴇʟᴏᴅʏ ᴍᴜsɪᴄ", font=_get_font(_BOLD, 30), fill=GOLD)

        card.convert("RGB").save(out_path, "PNG", optimize=True)
        return out_path
    except Exception:
        return None


async def render_greet_card(
    client,
    user,
    chat,
    kind: str = "welcome",
    member_no: "int | None" = None,
) -> "str | None":
    """Build the greeting card and return its path (None on any failure)."""
    try:
        name = (getattr(user, "first_name", None) or "User")[:28]
        username = f"@{user.username}" if getattr(user, "username", None) else ""

        avatar_path = None
        try:
            avatar_path = await fetch_dp(client, user.id)
        except Exception:
            avatar_path = None

        chat_photo_path = None
        try:
            photo = getattr(chat, "photo", None)
            if photo and getattr(photo, "big_file_id", None):
                chat_photo_path = await client.download_media(
                    photo.big_file_id,
                    file_name=os.path.join(CACHE_DIR, f"gc_{chat.id}.jpg"),
                )
        except Exception:
            chat_photo_path = None

        out = os.path.join(CACHE_DIR, f"{kind}_{chat.id}_{user.id}_{int(time.time())}.png")
        return await asyncio.to_thread(
            _render_sync,
            out,
            kind,
            name,
            username,
            (getattr(chat, "title", None) or "Group")[:34],
            member_no,
            avatar_path,
            chat_photo_path,
        )
    except Exception:
        return None


def cleanup(path: "str | None") -> None:
    try:
        if path and os.path.exists(path) and CACHE_DIR in path:
            os.remove(path)
    except Exception:
        pass
