"""
🖼️ Thumbnail generator — compact "mini player" now playing card

DESIGN (v3 — circular thumbnail + side info, Modi-Meloni theme):
  Redesigned away from the old full-screen/full-bleed cover-art card. The
  card is now a short, wide "mini player" strip:
    • A CIRCULAR crop of the song's YouTube thumbnail sits on the left,
      framed with a saffron→gold ring (Modi-Meloni palette).
    • Title / channel / a small "views"-style meta line sit beside it.
    • A saffron→white→green progress bar runs under the info.
    • A row of playback-control glyphs (⏮ ⏯ ⏭ + shuffle/repeat) is drawn
      under the bar, purely as a visual echo of Telegram's own inline
      keyboard buttons that ship alongside the card.
  The requester's own avatar (small) + the bot badge sit in the corner
  instead of dominating the frame like the old design did.
  No `has_spoiler` blur is used any more — the point of the circular
  thumbnail is to be seen immediately, like a real music-player widget.
"""
import io
import os
import time
import aiohttp
from PIL import Image, ImageDraw, ImageFont
from utils.client_cache import get_me_cached

ASSETS = os.path.join(os.path.dirname(__file__), "..", "assets")
FONTS = os.path.join(ASSETS, "fonts")

# ── Modi-Meloni palette (kept in sync with melody/config.py Config.COLORS) ──
SAFFRON = "#FF6600"
GOLD = "#FFD700"
GREEN = "#009246"
WHITE = "#FFFFFF"
DARK = "#1A0500"
CHAKRA_BLUE = "#000080"

# Bot's own DP rarely changes during a run — fetch it once and reuse.
_bot_dp_cache: dict = {"path": None, "tried": False}


# BUG FIX: assets/fonts previously shipped empty (only a .gitkeep) — every
# _get_font() call silently fell back to PIL's tiny built-in bitmap font,
# so the whole "side info" text block (title/channel/meta/time) rendered
# almost unreadably small no matter how the layout code sized it. The real
# Poppins .ttf files are now committed to assets/fonts; this also keeps a
# couple of common system-font fallbacks so the card still looks like an
# actual card (not a wireframe) even if a font file ever goes missing again.
_SYSTEM_FONT_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/nix/store/*/share/fonts/**/DejaVuSans-Bold.ttf",
]


def _get_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = os.path.join(FONTS, name)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        pass
    for fallback in _SYSTEM_FONT_FALLBACKS:
        try:
            if "*" in fallback:
                import glob
                matches = glob.glob(fallback, recursive=True)
                if not matches:
                    continue
                fallback = matches[0]
            return ImageFont.truetype(fallback, size)
        except Exception:
            continue
    return ImageFont.load_default()


async def _fetch_image_from_url(url: str) -> "Image.Image | None":
    """Download an image from a remote URL (e.g. YouTube thumbnail)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        pass
    return None


def _load_image_any(source: str) -> "Image.Image | None":
    """Open an image from a local file path (used for downloaded DPs)."""
    try:
        if source and os.path.exists(source):
            return Image.open(source).convert("RGBA")
    except Exception:
        pass
    return None


async def fetch_dp(client, user_id: int) -> "str | None":
    """
    Download a user's (or the bot's) current profile photo to a local file
    and return the path, or None if they have no photo / it fails.
    Pyrogram doesn't expose a direct CDN URL for profile photos — they must
    be downloaded through the client.

    BUG FIX: Pyrogram has no `get_profile_photos` method (that name doesn't
    exist on Client at all — every call here raised AttributeError, silently
    swallowed by the except below, so requester/bot avatars on the thumbnail
    ALWAYS fell back to the plain initials badge). The real method is
    `get_chat_photos()`, and it returns an async generator, not a list —
    it must be iterated with `async for`, not indexed.
    """
    try:
        photo = None
        async for p in client.get_chat_photos(user_id, limit=1):
            photo = p
            break
        if not photo:
            return None
        dest = f"/tmp/melody_dp_{user_id}_{int(time.time() * 1000)}.jpg"
        path = await client.download_media(photo.file_id, file_name=dest)
        return path
    except Exception:
        return None


async def get_bot_dp(client) -> "str | None":
    """Cached bot profile-photo path — fetched once per process lifetime."""
    if _bot_dp_cache["tried"]:
        return _bot_dp_cache["path"]
    _bot_dp_cache["tried"] = True
    try:
        me = await get_me_cached(client)
        path = await fetch_dp(client, me.id)
        _bot_dp_cache["path"] = path
        return path
    except Exception:
        return None


# Bot's own username/display-name rarely changes during a run — cache it
# once and reuse it for the "bot branding" button appended to every play
# card, instead of hitting get_me() on every /play.
_bot_identity_cache: dict = {"username": None, "name": None, "tried": False}


async def get_bot_identity(client) -> tuple[str | None, str]:
    """Returns (username_without_at, display_name) for the bot account."""
    if _bot_identity_cache["tried"]:
        return _bot_identity_cache["username"], _bot_identity_cache["name"]
    _bot_identity_cache["tried"] = True
    try:
        me = await get_me_cached(client)
        _bot_identity_cache["username"] = me.username
        _bot_identity_cache["name"] = me.first_name or "Melody"
    except Exception:
        _bot_identity_cache["name"] = "Melody"
    return _bot_identity_cache["username"], _bot_identity_cache["name"]


def _circle_crop(img: "Image.Image", size: int, ring_color=None, ring_width: int = 0) -> "Image.Image":
    """Crop image to a circle, optionally with a colored ring border."""
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    output.paste(img, (0, 0), mask)

    if ring_color and ring_width > 0:
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(output, (0, 0), output)
        ring_draw = ImageDraw.Draw(canvas)
        ring_draw.ellipse(
            (ring_width // 2, ring_width // 2, size - ring_width // 2, size - ring_width // 2),
            outline=ring_color, width=ring_width,
        )
        return canvas
    return output


def _dual_ring_crop(img: "Image.Image", size: int) -> "Image.Image":
    """Circular crop with a two-tone saffron→green ring (Modi-Meloni), used
    for the main mini-player thumbnail avatar."""
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    output.paste(img, (0, 0), mask)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(output, (0, 0), output)
    ring_width = max(4, size // 26)
    ring_draw = ImageDraw.Draw(canvas)
    box = (ring_width // 2, ring_width // 2, size - ring_width // 2, size - ring_width // 2)
    # Top-left half saffron, bottom-right half green — a subtle nod to the
    # tricolour without covering the artwork itself.
    ring_draw.arc(box, start=225, end=45, fill=SAFFRON, width=ring_width)
    ring_draw.arc(box, start=45, end=225, fill=GREEN, width=ring_width)
    return canvas


def _placeholder_avatar(size: int) -> "Image.Image":
    """Fallback circular art if the YouTube thumbnail can't be fetched."""
    img = Image.new("RGBA", (size, size), DARK)
    draw = ImageDraw.Draw(img)
    for y in range(size):
        ratio = y / size
        r = int(int(SAFFRON[1:3], 16) * (1 - ratio) + int(GREEN[1:3], 16) * ratio)
        g = int(int(SAFFRON[3:5], 16) * (1 - ratio) + int(GREEN[3:5], 16) * ratio)
        b = int(int(SAFFRON[5:7], 16) * (1 - ratio) + int(GREEN[5:7], 16) * ratio)
        draw.line([(0, y), (size, y)], fill=(r, g, b, 255))
    # BUG FIX: was drawing the "♪" text glyph, which Poppins has no design
    # for (and Heroku has no emoji/symbol font fallback) — it rendered as an
    # empty tofu box, which is exactly the broken placeholder users saw
    # whenever a YouTube thumbnail failed to fetch. Draw a real music-note
    # shape with primitives instead so it always renders.
    note_w = size * 0.34
    stem_x = size * 0.5 + note_w * 0.32
    stem_top = size * 0.28
    stem_bottom = size * 0.68
    draw.line([(stem_x, stem_top), (stem_x, stem_bottom)], fill=WHITE, width=max(3, int(size * 0.035)))
    draw.polygon(
        [(stem_x, stem_top), (stem_x + note_w * 0.3, stem_top + note_w * 0.12),
         (stem_x + note_w * 0.3, stem_top + note_w * 0.32), (stem_x, stem_top + note_w * 0.22)],
        fill=WHITE,
    )
    head_r = size * 0.11
    draw.ellipse(
        [(stem_x - head_r * 2, stem_bottom - head_r), (stem_x, stem_bottom + head_r)],
        fill=WHITE,
    )
    return img


def _initials_avatar(name: str, size: int, color: str) -> "Image.Image":
    img = Image.new("RGBA", (size, size), color)
    d = ImageDraw.Draw(img)
    letter = (name or "?").strip()[:1].upper() or "?"
    f = _get_font("Poppins-Bold.ttf", int(size * 0.45))
    bbox = d.textbbox((0, 0), letter, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), letter, font=f, fill=WHITE)
    return img


def _truncate_to_width(draw: "ImageDraw.ImageDraw", text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    ellipsis = "…"
    while text and draw.textlength(text + ellipsis, font=font) > max_w:
        text = text[:-1]
    return text + ellipsis


def _draw_control_icon(draw: "ImageDraw.ImageDraw", kind: str, cx: float, cy: float, size: int, color) -> None:
    """Draw a playback-control icon as real vector shapes.

    Replaces emoji glyphs (see BUG FIX note above the caller) so the row
    always renders identically regardless of what fonts happen to be
    installed on the host.
    """
    h = size / 2
    if kind == "playpause":
        draw.polygon([(cx - h * 0.6, cy - h), (cx - h * 0.6, cy + h), (cx + h * 0.9, cy)], fill=color)
    elif kind == "prev":
        draw.polygon([(cx + h * 0.2, cy - h), (cx + h * 0.2, cy + h), (cx - h * 0.9, cy)], fill=color)
        draw.rectangle([(cx - h * 1.1, cy - h), (cx - h * 0.85, cy + h)], fill=color)
    elif kind == "next":
        draw.polygon([(cx - h * 0.2, cy - h), (cx - h * 0.2, cy + h), (cx + h * 0.9, cy)], fill=color)
        draw.rectangle([(cx + h * 0.85, cy - h), (cx + h * 1.1, cy + h)], fill=color)
    elif kind == "shuffle":
        draw.line([(cx - h, cy - h * 0.5), (cx + h, cy + h * 0.5)], fill=color, width=2)
        draw.line([(cx - h, cy + h * 0.5), (cx + h, cy - h * 0.5)], fill=color, width=2)
        for sx, sy in ((cx + h, cy + h * 0.5), (cx + h, cy - h * 0.5)):
            draw.polygon([(sx, sy - 4), (sx, sy + 4), (sx + 6, sy)], fill=color)
    elif kind == "repeat":
        bbox = [cx - h, cy - h, cx + h, cy + h]
        draw.arc(bbox, start=20, end=340, fill=color, width=2)
        ax = cx + h * 0.6
        ay = cy - h * 0.75
        draw.polygon([(ax - 6, ay - 5), (ax - 6, ay + 5), (ax + 5, ay)], fill=color)


def _blurred_backdrop(cover: "Image.Image | None", w: int, h: int) -> "Image.Image":
    """Full-bleed, blurred + darkened backdrop from the YouTube cover art,
    stretched/cropped to (w, h) — the same "paused video player" look as a
    real YouTube thumbnail card (blurred banner behind the controls)."""
    from PIL import ImageFilter

    if cover is None:
        cover = _placeholder_avatar(max(w, h))

    src = cover.convert("RGB")
    src_ratio = src.width / src.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        new_h = h
        new_w = int(h * src_ratio)
    else:
        new_w = w
        new_h = int(w / src_ratio)
    src = src.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    src = src.crop((left, top, left + w, top + h))
    src = src.filter(ImageFilter.GaussianBlur(18))

    # Darken so white/gold foreground text stays legible over any artwork.
    dark = Image.new("RGB", (w, h), (0, 0, 0))
    backdrop = Image.blend(src, dark, 0.55)
    return backdrop.convert("RGBA")


async def make_thumbnail(
    song_title: str,
    artist: str,
    duration: str,
    requester_name: str,
    group_name: str,
    owner_name: str,
    yt_thumbnail_url: str,
    requester_dp_path: str = None,
    bot_dp_path: str = None,
) -> str:
    """
    Generate a 16:9 "paused video player" now-playing card — same aspect
    ratio/shape as a normal shared video thumbnail (1280×720):
      ┌──────────────────────────────────────────────┐
      │  blurred cover backdrop, darkened             │
      │      (●)   Title                              │
      │  thumb     Group · views-style meta           │
      │            ▬▬▬▬▬▭▭▭▭▭▭▭▭  00:00 / duration     │
      │              🔀  ⏮  ⏯  ⏭  🔁                  │
      └──────────────────────────────────────────────┘
    Styled in the Modi-Meloni theme (saffron / white / green / gold).

    BUG FIX ("mene ek pic share ki vese hi size ka thumbnail chaiye"): the
    card used to render as a thin 1280×420 (~3:1) horizontal strip, which
    doesn't match the size/shape of a normal shared photo or video
    thumbnail. It's now a standard 16:9 (1280×720) card — the same
    proportions as any regular picture/video preview Telegram shows.

    `requester_dp_path` / `bot_dp_path` are LOCAL file paths (already
    downloaded via fetch_dp()/get_bot_dp()) — not remote URLs.
    Returns the path to the saved PNG.
    """
    cover = await _fetch_image_from_url(yt_thumbnail_url)

    # PERF FIX: all of the PIL work below (resize / GaussianBlur / font draw)
    # is CPU-bound and used to run inline on the asyncio event loop, freezing
    # every other chat's /play for the whole render. Run it in a thread.
    import asyncio as _asyncio

    return await _asyncio.get_running_loop().run_in_executor(
        None,
        _render_thumbnail_sync_professional,
        song_title, artist, duration, requester_name, group_name,
        owner_name, cover, requester_dp_path, bot_dp_path,
    )


def _render_thumbnail_sync(
    song_title: str,
    artist: str,
    duration: str,
    requester_name: str,
    group_name: str,
    owner_name: str,
    cover,
    requester_dp_path: str = None,
    bot_dp_path: str = None,
) -> str:
    W, H = 1280, 720
    PAD = 56

    # ── Background — full-bleed blurred cover art + tricolour edge glow ──
    bg = _blurred_backdrop(cover, W, H)
    edge = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    edge_draw = ImageDraw.Draw(edge)
    edge_draw.rectangle([(0, 0), (W, 8)], fill=SAFFRON)
    edge_draw.rectangle([(0, H - 8), (W, H)], fill=GREEN)
    bg = Image.alpha_composite(bg, edge)
    draw = ImageDraw.Draw(bg)

    # ── Fonts ────────────────────────────────────────────────────────────
    font_title = _get_font("Poppins-Bold.ttf", 48)
    font_channel = _get_font("Poppins-Bold.ttf", 30)
    font_meta = _get_font("Poppins-Regular.ttf", 24)
    font_small = _get_font("Poppins-Regular.ttf", 20)

    # ── Circular thumbnail (left, vertically centered) ─────────────────
    thumb_size = int(H * 0.52)
    cover_circle = _dual_ring_crop(cover if cover else _placeholder_avatar(thumb_size), thumb_size)
    thumb_x = PAD
    thumb_y = (H - thumb_size) // 2
    bg.paste(cover_circle, (thumb_x, thumb_y), cover_circle)

    # ── Song info (right of thumbnail) ─────────────────────────────────
    info_x = thumb_x + thumb_size + 52
    info_w = W - info_x - PAD
    block_top = thumb_y + int(thumb_size * 0.06)

    title_y = block_top
    safe_title = _truncate_to_width(draw, song_title, font_title, info_w)
    draw.text((info_x, title_y), safe_title, font=font_title, fill=WHITE)

    channel_y = title_y + 66
    safe_channel = _truncate_to_width(draw, artist, font_channel, info_w)
    draw.text((info_x, channel_y), safe_channel, font=font_channel, fill=GOLD)

    meta_y = channel_y + 48
    # REQUEST: "Play card me group ka name show kr" — lead with the group
    # name (in gold, matching the channel line) instead of burying it after
    # a generic "Playing now" prefix in dim gray, so it's actually visible.
    meta_line = f"🏠 {group_name}"
    safe_meta = _truncate_to_width(draw, meta_line, font_meta, info_w)
    draw.text((info_x, meta_y), safe_meta, font=font_meta, fill=GOLD)

    # ── Progress bar (saffron → white → green, Modi-Meloni tricolour) ──
    bar_y = meta_y + 56
    bar_h = 12
    bar_w = info_w
    third = bar_w // 3
    draw.rounded_rectangle([(info_x, bar_y), (info_x + bar_w, bar_y + bar_h)], radius=bar_h // 2, fill="#3A3A3A")
    filled_w = int(bar_w * 0.28)  # static preview position for the "now playing" card
    for i in range(filled_w):
        if i < third:
            r, g, b = 255, int(153 + 100 * (i / third)), int(51 + 150 * (i / third))
        else:
            r = int(255 - 117 * ((i - third) / max(third, 1)))
            g = int(255 - 119 * ((i - third) / max(third, 1)))
            b = int(255 - 247 * ((i - third) / max(third, 1)))
        draw.line([(info_x + i, bar_y), (info_x + i, bar_y + bar_h)], fill=(max(r, 0), max(g, 0), max(b, 0)))
    knob_x = info_x + filled_w
    draw.ellipse([(knob_x - 10, bar_y - 5), (knob_x + 10, bar_y + bar_h + 5)], fill=GOLD)

    time_y = bar_y + bar_h + 14
    draw.text((info_x, time_y), "00:00", font=font_small, fill="#DDDDDD")
    dur_w = draw.textlength(duration, font=font_small)
    draw.text((info_x + bar_w - dur_w, time_y), duration, font=font_small, fill="#DDDDDD")

    # ── Playback control glyphs row ────────────────────────────────────
    # Drawn as real vector shapes (not emoji text) so they always render
    # correctly regardless of which fonts happen to be installed on host.
    controls_y = time_y + 40
    icon_size = 26
    kinds = ["shuffle", "prev", "playpause", "next", "repeat"]
    gap = info_w / (len(kinds) + 1)
    cy = controls_y + icon_size // 2
    for idx, kind in enumerate(kinds, 1):
        cx = info_x + gap * idx
        color = WHITE if kind in ("prev", "playpause", "next") else GOLD
        _draw_control_icon(draw, kind, cx, cy, icon_size, color)

    # ── Requester + bot badge, top-right corner ─────────────────────────
    badge_size = 92
    bot_img = _load_image_any(bot_dp_path) if bot_dp_path else None
    user_img = _load_image_any(requester_dp_path) if requester_dp_path else None

    user_circle = _circle_crop(
        user_img if user_img else _initials_avatar(requester_name, badge_size, "#8B0000"),
        badge_size, ring_color=GOLD, ring_width=4,
    )
    badge_x = W - PAD - badge_size
    badge_y = PAD // 2
    bg.paste(user_circle, (badge_x, badge_y), user_circle)

    bot_badge_size = 38
    bot_circle = _circle_crop(
        bot_img if bot_img else _initials_avatar("Melody", bot_badge_size, "#B8860B"),
        bot_badge_size, ring_color=WHITE, ring_width=2,
    )
    bg.paste(
        bot_circle,
        (badge_x + badge_size - bot_badge_size + 8, badge_y + badge_size - bot_badge_size + 8),
        bot_circle,
    )
    req_label = _truncate_to_width(draw, requester_name, font_small, 180)
    label_w = draw.textlength(req_label, font=font_small)
    draw.text((badge_x - label_w - 14, badge_y + badge_size // 2 - 10), req_label, font=font_small, fill="#EEEEEE")

    # ── Watermark ───────────────────────────────────────────────────────
    wm_draw = ImageDraw.Draw(bg)
    wm_draw.text((PAD, H - PAD // 2 - 10), f"Melody · {owner_name[:18]}", font=font_small, fill=(255, 255, 255, 170))

    # ── Save ──────────────────────────────────────────────────────────────
    out_path = f"/tmp/melody_thumb_{int(time.time() * 1000)}.png"
    bg.convert("RGB").save(out_path, "PNG")
    return out_path


def _render_thumbnail_sync_professional(
    song_title: str,
    artist: str,
    duration: str,
    requester_name: str,
    group_name: str,
    owner_name: str,
    cover,
    requester_dp_path: str = None,
    bot_dp_path: str = None,
) -> str:
    """Render a polished dark-glass music card with strong hierarchy.

    The old card placed too much information on a narrow strip. This version
    uses a standard 16:9 canvas, a large cover focus, a readable two-line title,
    and a compact metadata/progress/control panel that remains legible in
    Telegram previews.
    """
    from PIL import ImageFilter, ImageOps

    W, H = 1280, 720
    PAD = 48
    bg = _blurred_backdrop(cover, W, H)
    tint = Image.new("RGBA", (W, H), (7, 13, 27, 88))
    bg = Image.alpha_composite(bg, tint)
    draw = ImageDraw.Draw(bg)

    # Thin tricolour edge treatment, intentionally restrained.
    draw.rectangle((0, 0, W, 7), fill=SAFFRON)
    draw.rectangle((0, H - 7, W, H), fill=GREEN)

    # Main glass panel and soft border.
    panel = Image.new("RGBA", (W - PAD * 2, H - 130), (10, 18, 36, 224))
    panel = panel.filter(ImageFilter.GaussianBlur(0.2))
    bg.alpha_composite(panel, (PAD, 82))
    draw.rounded_rectangle((PAD, 82, W - PAD, H - 48), radius=34, outline=(255, 255, 255, 42), width=2)

    font_kicker = _get_font("Poppins-Bold.ttf", 20)
    font_title = _get_font("Poppins-Bold.ttf", 52)
    font_artist = _get_font("Poppins-Regular.ttf", 28)
    font_meta = _get_font("Poppins-Regular.ttf", 22)
    font_small = _get_font("Poppins-Regular.ttf", 18)
    font_control = _get_font("Poppins-Bold.ttf", 20)

    # Kicker pill.
    draw.rounded_rectangle((PAD + 38, 112, PAD + 220, 150), radius=19, fill=(255, 102, 0, 225))
    draw.text((PAD + 58, 120), "NOW PLAYING", font=font_kicker, fill=WHITE)

    # Cover art: rounded-square, not a small circle, so the source artwork is
    # recognizable in a Telegram preview.
    cover_size = 330
    cover_src = cover if cover else _placeholder_avatar(cover_size)
    cover_src = ImageOps.fit(cover_src.convert("RGB"), (cover_size, cover_size), method=Image.LANCZOS, centering=(0.5, 0.5))
    cover_src = cover_src.filter(ImageFilter.UnsharpMask(radius=1.2, percent=115, threshold=3))
    cover_rgba = cover_src.convert("RGBA")
    cover_mask = Image.new("L", (cover_size, cover_size), 0)
    ImageDraw.Draw(cover_mask).rounded_rectangle((0, 0, cover_size - 1, cover_size - 1), radius=28, fill=255)
    cover_rgba.putalpha(cover_mask)
    cover_x, cover_y = PAD + 42, 184
    # Gold shadow + saffron/green outline.
    draw.rounded_rectangle((cover_x - 7, cover_y - 7, cover_x + cover_size + 7, cover_y + cover_size + 7), radius=35, outline=GOLD, width=5)
    bg.alpha_composite(cover_rgba, (cover_x, cover_y))

    info_x = cover_x + cover_size + 54
    info_w = W - PAD - info_x - 42

    # Wrap title over at most two lines, preserving a clear focal hierarchy.
    title = " ".join((song_title or "Unknown track").split())
    title_lines: list[str] = []
    current = ""
    for word in title.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font_title) > info_w:
            title_lines.append(current)
            current = word
            if len(title_lines) == 2:
                break
        else:
            current = candidate
    if len(title_lines) < 2 and current:
        title_lines.append(current)
    if len(title_lines) > 2:
        title_lines = title_lines[:2]
    if len(title_lines) == 2 and title_lines[-1] != current and current:
        title_lines[-1] = _truncate_to_width(draw, title_lines[-1], font_title, info_w)
    if not title_lines:
        title_lines = ["Unknown track"]
    for idx, line in enumerate(title_lines[:2]):
        draw.text((info_x, 188 + idx * 62), _truncate_to_width(draw, line, font_title, info_w), font=font_title, fill=WHITE)

    artist_y = 188 + min(len(title_lines), 2) * 62 + 10
    draw.text((info_x, artist_y), _truncate_to_width(draw, artist or "Unknown artist", font_artist, info_w), font=font_artist, fill=GOLD)
    draw.text((info_x, artist_y + 48), _truncate_to_width(draw, group_name or "Music room", font_meta, info_w), font=font_meta, fill=(220, 226, 239, 235))
    draw.text((info_x, artist_y + 83), _truncate_to_width(draw, f"Requested by {requester_name or 'listener'}", font_small, info_w), font=font_small, fill=(165, 177, 198, 235))

    # Progress bar with a proper track and knob.
    bar_y = 470
    bar_h = 12
    draw.rounded_rectangle((info_x, bar_y, info_x + info_w, bar_y + bar_h), radius=bar_h // 2, fill=(89, 101, 124, 180))
    filled = max(36, int(info_w * 0.24))
    for x in range(filled):
        ratio = x / max(filled - 1, 1)
        if ratio < 0.5:
            c = (255, int(102 + ratio * 2 * 153), int(0 + ratio * 2 * 215), 255)
        else:
            c = (int(255 - (ratio - 0.5) * 2 * 255), 255, int(215 - (ratio - 0.5) * 2 * 215), 255)
        draw.line((info_x + x, bar_y, info_x + x, bar_y + bar_h), fill=c, width=bar_h)
    draw.ellipse((info_x + filled - 11, bar_y - 5, info_x + filled + 11, bar_y + bar_h + 5), fill=GOLD)
    draw.text((info_x, bar_y + 24), "00:00", font=font_small, fill=(190, 198, 213, 230))
    duration = duration or "--:--"
    dur_w = draw.textlength(duration, font=font_small)
    draw.text((info_x + info_w - dur_w, bar_y + 24), duration, font=font_small, fill=(190, 198, 213, 230))

    # Vector controls with labels, avoiding unreliable emoji glyphs.
    controls_y = 572
    control_gap = max(94, min(132, info_w // 6))
    control_kinds = [("shuffle", "SHUFFLE"), ("prev", "BACK"), ("playpause", "PLAY"), ("next", "NEXT"), ("repeat", "REPEAT")]
    start_x = info_x + (info_w - control_gap * (len(control_kinds) - 1)) // 2
    for idx, (kind, label) in enumerate(control_kinds):
        cx = start_x + idx * control_gap
        color = GOLD if kind in ("shuffle", "repeat") else WHITE
        _draw_control_icon(draw, kind, cx, controls_y, 28, color)
        label_w = draw.textlength(label, font=font_control)
        draw.text((cx - label_w / 2, controls_y + 24), label, font=font_control, fill=(160, 174, 196, 230))

    # Requester avatar and a small bot badge in the panel's upper-right corner.
    badge_size = 62
    user_img = _load_image_any(requester_dp_path) if requester_dp_path else None
    bot_img = _load_image_any(bot_dp_path) if bot_dp_path else None
    user_circle = _circle_crop(user_img if user_img else _initials_avatar(requester_name, badge_size, "#8B0000"), badge_size, ring_color=GOLD, ring_width=3)
    badge_x, badge_y = W - PAD - 42 - badge_size, 105
    bg.paste(user_circle, (badge_x, badge_y), user_circle)
    bot_circle = _circle_crop(bot_img if bot_img else _initials_avatar("M", 28, "#B8860B"), 28, ring_color=WHITE, ring_width=2)
    bg.paste(bot_circle, (badge_x + 42, badge_y + 42), bot_circle)

    watermark = f"Melody  ·  {owner_name or 'Music bot'}"
    draw.text((PAD + 42, H - 36), _truncate_to_width(draw, watermark, font_small, 280), font=font_small, fill=(180, 190, 208, 205))

    out_path = f"/tmp/melody_thumb_{int(time.time() * 1000)}.png"
    bg.convert("RGB").save(out_path, "PNG", optimize=True)
    return out_path
