"""Quotly-style chat screenshot renderer (Pillow only, no external API).

render_quote(items) -> bytes (512px WebP, transparent) ready to send as a
sticker. Each item is a dict:
    name, user_id, avatar (bytes|None), text, media (bytes|None),
    reply_name, reply_text, show_avatar (bool)
Fonts: Poppins (bundled) for Latin, then system Noto fonts for Hindi /
other scripts, Noto Color Emoji for emoji. Missing fonts degrade quietly.
"""
from __future__ import annotations

import io
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSETS = os.path.join(os.path.dirname(_HERE), "assets", "fonts")

_FONT_DIRS = [
    _ASSETS,
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype",
    "/usr/share/fonts",
]
_REGULAR = [
    "Poppins-Regular.ttf", "NotoSans-Regular.ttf", "NotoSansDevanagari-Regular.ttf",
    "NotoSansBengali-Regular.ttf", "NotoSansArabic-Regular.ttf",
    "NotoSansSymbols-Regular.ttf", "NotoSansSymbols2-Regular.ttf",
    "NotoSansMath-Regular.ttf", "DejaVuSans.ttf",
]
_BOLD = [
    "Poppins-Bold.ttf", "NotoSans-Bold.ttf", "NotoSansDevanagari-Bold.ttf",
    "NotoSansDevanagari-Regular.ttf", "NotoSansSymbols-Regular.ttf",
    "NotoSansSymbols2-Regular.ttf", "NotoSansMath-Regular.ttf", "DejaVuSans-Bold.ttf",
]
_EMOJI = ["NotoColorEmoji.ttf"]

# Telegram's name-colour palette (by user id % 7).
NAME_COLORS = [
    (252, 92, 81), (250, 121, 13), (137, 95, 210), (15, 177, 63),
    (14, 177, 181), (62, 160, 233), (232, 92, 156),
]
BUBBLE = (30, 33, 41, 255)
TEXT = (238, 238, 240, 255)
SUB = (170, 174, 186, 255)

SCALE = 2           # draw at 2x, downscale at the end for crisp text
FS = 22 * SCALE     # message font size
NS = 20 * SCALE     # name font size
RS = 18 * SCALE     # reply font size
AV = 56 * SCALE     # avatar size
PAD = 14 * SCALE
GAP = 8 * SCALE
MAX_W = 380 * SCALE


def _find(name: str) -> str | None:
    for d in _FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
        if os.path.isdir(d):
            for root, _dirs, files in os.walk(d):
                if name in files:
                    return os.path.join(root, name)
    return None


@lru_cache(maxsize=64)
def _fonts(bold: bool, size: int) -> tuple:
    out = []
    for n in (_BOLD if bold else _REGULAR):
        p = _find(n)
        if p:
            try:
                out.append(ImageFont.truetype(p, size))
            except Exception:  # noqa: BLE001
                pass
    if not out:
        out.append(ImageFont.load_default(size=size))
    return tuple(out)


@lru_cache(maxsize=1)
def _emoji_font():
    for n in _EMOJI:
        p = _find(n)
        if p:
            try:
                return ImageFont.truetype(p, 109)  # CBDT only renders at 109
            except Exception:  # noqa: BLE001
                pass
    return None


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return (
        0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x2300 <= o <= 0x23FF
        or 0x2B00 <= o <= 0x2BFF or o in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x3030)
    )


try:
    from PIL import features as _features
    _HAS_RAQM = bool(_features.check("raqm"))
except Exception:  # noqa: BLE001
    _HAS_RAQM = False

_ZW = {0x200D, 0xFE0F, 0xFE0E, 0x200B, 0x200C, 0x20E3}


@lru_cache(maxsize=4096)
def _font_index(ch: str, bold: bool, size: int) -> int:
    fonts = _fonts(bold, size)
    for i, f in enumerate(fonts):
        try:
            if f.getmask(ch).getbbox() is not None or ch.isspace():
                # Reject the .notdef box: compare to a surely-missing glyph.
                if ch.isspace() or f.getmask(ch).tobytes() != f.getmask("\U0010FFFD").tobytes():
                    return i
        except Exception:  # noqa: BLE001
            continue
    return 0


def _runs(text: str, bold: bool, size: int):
    """Split text into (kind, str, font) runs; kind is 'txt' or 'emoji'."""
    fonts = _fonts(bold, size)
    runs: list = []
    for ch in text:
        if ord(ch) in _ZW:
            if runs and runs[-1][0] == "emoji":
                runs[-1][1] += ch
            continue
        if ord(ch) < 32 and ch != "\n":
            continue
        if _is_emoji(ch) and _emoji_font() is not None:
            if runs and runs[-1][0] == "emoji" and runs[-1][1] and (
                ord(runs[-1][1][-1]) == 0x200D or 0x1F3FB <= ord(ch) <= 0x1F3FF
                or (0x1F1E6 <= ord(ch) <= 0x1F1FF and len(runs[-1][1]) == 1
                    and 0x1F1E6 <= ord(runs[-1][1]) <= 0x1F1FF)
            ):
                runs[-1][1] += ch
            else:
                runs.append(["emoji", ch, None])
            continue
        f = fonts[_font_index(ch, bold, size)]
        if runs and runs[-1][0] == "txt" and runs[-1][2] is f:
            runs[-1][1] += ch
        else:
            runs.append(["txt", ch, f])
    return runs


def _w(kind, s, font, size) -> int:
    if kind == "emoji":
        return int(size * 1.2)
    return int(font.getlength(s))


def _wrap(text: str, bold: bool, size: int, max_w: int, max_lines: int = 60):
    """Word-wrap into lines of runs."""
    lines: list = []
    for para in (text or "").split("\n"):
        tokens = []
        cur = ""
        for ch in para:
            cur += ch
            if ch == " ":
                tokens.append(cur)
                cur = ""
        if cur:
            tokens.append(cur)
        line: list = []
        lw = 0
        for tok in tokens or [""]:
            truns = _runs(tok, bold, size)
            tw = sum(_w(k, s, f, size) for k, s, f in truns)
            if lw + tw > max_w and line:
                lines.append((line, lw))
                line, lw = [], 0
            if tw > max_w:  # very long word: break by chars
                for k, s, f in truns:
                    for ch in s:
                        cw = _w(k, ch, f, size)
                        if lw + cw > max_w and line:
                            lines.append((line, lw))
                            line, lw = [], 0
                        line.append([k, ch, f])
                        lw += cw
                continue
            line.extend(truns)
            lw += tw
        lines.append((line, lw))
        if len(lines) >= max_lines:
            break
    return lines[:max_lines]


def _draw_line(img, draw, x, y, runs, size, color):
    ef = _emoji_font()
    for kind, s, f in runs:
        if kind == "emoji" and ef is not None:
            es = int(size * 1.15)
            try:
                if not _HAS_RAQM:
                    s = s[:1]  # no shaping engine: draw the base emoji only
                tile = Image.new("RGBA", (200, 160), (0, 0, 0, 0))
                try:
                    ImageDraw.Draw(tile).text((10, 10), s, font=ef, embedded_color=True)
                except Exception:  # noqa: BLE001
                    ImageDraw.Draw(tile).text((10, 10), s[:1], font=ef, embedded_color=True)
                bb = tile.getbbox()
                if bb:
                    tile = tile.crop(bb).resize((es, es), Image.LANCZOS)
                    img.alpha_composite(tile, (int(x), int(y + size * 0.05)))
            except Exception:  # noqa: BLE001
                pass
            x += _w(kind, s, f, size)
        else:
            draw.text((x, y), s, font=f, fill=color)
            x += f.getlength(s)


def _avatar(data: bytes | None, name: str, uid: int) -> Image.Image:
    size = AV
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    av = None
    if data:
        try:
            av = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:  # noqa: BLE001
            av = None
    if av is None:
        av = Image.new("RGBA", (size, size), NAME_COLORS[abs(uid) % 7] + (255,))
        ch = (name or "?").strip()[:1].upper() or "?"
        f = _fonts(True, int(size * 0.45))[0]
        d = ImageDraw.Draw(av)
        tw = f.getlength(ch)
        d.text(((size - tw) / 2, size * 0.2), ch, font=f, fill=(255, 255, 255, 255))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(av, (0, 0), mask)
    return out


def _bubble(item: dict) -> Image.Image:
    name = (item.get("name") or "Unknown")[:40]
    color = NAME_COLORS[abs(int(item.get("user_id") or 0)) % 7] + (255,)
    show_name = item.get("show_name", True)
    name_lines = _wrap(name, True, NS, MAX_W, 1) if show_name else []
    reply_lines = []
    reply_name_lines = []
    if item.get("reply_name") is not None:
        reply_name_lines = _wrap(item["reply_name"][:40], True, RS, MAX_W - 16 * SCALE, 1)
        reply_lines = _wrap((item.get("reply_text") or "").replace("\n", " ")[:120],
                            False, RS, MAX_W - 16 * SCALE, 1)
    text_lines = _wrap(item.get("text") or "", False, FS, MAX_W) if item.get("text") else []

    media = None
    if item.get("media"):
        try:
            media = Image.open(io.BytesIO(item["media"])).convert("RGBA")
            mw = MAX_W
            ratio = min(mw / media.width, (300 * SCALE) / media.height)
            media = media.resize((max(1, int(media.width * ratio)), max(1, int(media.height * ratio))), Image.LANCZOS)
        except Exception:  # noqa: BLE001
            media = None

    lh_n, lh_r, lh_t = int(NS * 1.35), int(RS * 1.35), int(FS * 1.4)
    content_w = max(
        [w for _l, w in name_lines] + [w + 16 * SCALE for _l, w in reply_name_lines + reply_lines]
        + [w for _l, w in text_lines] + [media.width if media else 0] + [60 * SCALE]
    )
    h = PAD
    if name_lines:
        h += lh_n
    if reply_name_lines:
        h += lh_r * 2 + GAP
    if media:
        h += media.height + GAP
    h += lh_t * len(text_lines)
    h += PAD - (GAP if media and not text_lines else 0)
    w = content_w + PAD * 2

    img = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=22 * SCALE, fill=BUBBLE)
    y = PAD
    if name_lines:
        _draw_line(img, d, PAD, y - 4 * SCALE, name_lines[0][0], NS, color)
        y += lh_n
    if reply_name_lines:
        rc = NAME_COLORS[abs(int(item.get("reply_user_id") or 0)) % 7] + (255,)
        d.rounded_rectangle((PAD, y, PAD + 4 * SCALE, y + lh_r * 2 - 4 * SCALE), radius=2 * SCALE, fill=rc)
        _draw_line(img, d, PAD + 14 * SCALE, y - 2 * SCALE, reply_name_lines[0][0], RS, rc)
        if reply_lines:
            _draw_line(img, d, PAD + 14 * SCALE, y + lh_r - 2 * SCALE, reply_lines[0][0], RS, SUB)
        y += lh_r * 2 + GAP
    if media:
        m = Image.new("L", media.size, 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, media.width - 1, media.height - 1), radius=14 * SCALE, fill=255)
        img.paste(media, (PAD, int(y)), m)
        y += media.height + GAP
    for line, _lw in text_lines:
        _draw_line(img, d, PAD, y, line, FS, TEXT)
        y += lh_t
    return img


def render_quote(items: list[dict]) -> bytes:
    blocks = []
    prev = None
    for it in items:
        same = prev is not None and prev == it.get("user_id")
        it = dict(it, show_name=not same)
        blocks.append((_bubble(it), it, same))
        prev = it.get("user_id")
    # Avatar only on the LAST bubble of each sender run (Telegram style).
    left = AV + GAP
    width = left + max(b.width for b, _i, _s in blocks)
    height = sum(b.height for b, _i, _s in blocks) + GAP * (len(blocks) - 1)
    canvas = Image.new("RGBA", (int(width), int(height)), (0, 0, 0, 0))
    y = 0
    for idx, (b, it, _same) in enumerate(blocks):
        canvas.alpha_composite(b, (left, y))
        nxt = blocks[idx + 1][1].get("user_id") if idx + 1 < len(blocks) else None
        if nxt != it.get("user_id"):
            av = _avatar(it.get("avatar"), it.get("name") or "", int(it.get("user_id") or 0))
            canvas.alpha_composite(av, (0, max(0, y + b.height - AV)))
        y += b.height + GAP
    # Fit into Telegram's 512px sticker box.
    ratio = 512 / max(canvas.width, canvas.height)
    canvas = canvas.resize((max(1, int(canvas.width * ratio)), max(1, int(canvas.height * ratio))), Image.LANCZOS)
    out = io.BytesIO()
    canvas.save(out, "WEBP", lossless=False, quality=92, method=4)
    return out.getvalue()


def to_sticker_webp(data: bytes) -> bytes:
    """Any image -> 512px (one side exactly 512) WebP for sticker packs."""
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    ratio = 512 / max(im.width, im.height)
    im = im.resize((max(1, int(im.width * ratio)), max(1, int(im.height * ratio))), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "WEBP", quality=92, method=4)
    return out.getvalue()
