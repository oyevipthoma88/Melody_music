"""
🪄 Query auto-correct for /play.

WHY (reported): "users ko song play krte tym bohot si spelling mistake hoti
hai to result invalid aata hai" — e.g. `aankho me tera hi cherra Satyajit
jain`. YouTube's own search is typo tolerant, but a `ytsearch1:` lookup for a
badly misspelt + noisy query can still come back empty, and then every source
"exhausts" and the user just sees an error.

The jugad, in order:
  1. clean the query (drop filler words like "song / gaana / play / video /
     lyrics / mp3 / full", strip emoji + punctuation, collapse "sooong" →
     "soong", squeeze spaces),
  2. ask YouTube's own autocomplete for the corrected spelling — this is the
     exact same suggestion engine the YouTube app uses, so "cherra" comes back
     as "chehra" — no dictionary or extra dependency required,
  3. retry with progressively looser variants (fewer words, + "song").

Everything is best-effort and never raises: a failed correction simply
returns the original query.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from difflib import SequenceMatcher

from melody.logging import LOGGER

# YouTube's own autocomplete. The `clients6` host is the one the YouTube app
# uses and is the only one that actually returns spelling corrections
# ("cherra" -> "chehra"); it answers with JSONP, hence the unwrap below.
_SUGGEST_URLS = (
    "https://suggestqueries-clients6.youtube.com/complete/search"
    "?client=youtube&ds=yt&gs_ri=youtube&q=",
    "https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q=",
)

_JSONP_RE = re.compile(r"^[^(]*\((.*)\)\s*;?\s*$", re.DOTALL)


# Words users add that carry no signal for the search itself.
_FILLER = {
    "song", "songs", "gaana", "gana", "gane", "play", "bajao", "baja", "sunao",
    "suna", "video", "vid", "audio", "mp3", "mp4", "full", "hd", "4k", "official",
    "lyrics", "lyrical", "please", "plz", "pls", "bhai", "yaar", "melody",
    "download", "track", "music", "musics", "ka", "ki", "wala", "wali",
}

_PUNCT_RE = re.compile(r"[^\w\s\u0900-\u097F&'-]+", re.UNICODE)
_REPEAT_RE = re.compile(r"(.)\1{2,}", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def is_url(text: str) -> bool:
    return bool(re.match(r"https?://", (text or "").strip(), re.I))


def clean_query(query: str) -> str:
    """Normalise a noisy user query. Never empties it out completely."""
    text = _PUNCT_RE.sub(" ", query or "")
    text = _REPEAT_RE.sub(r"\1\1", text)
    words = [w for w in _SPACE_RE.split(text.strip()) if w]
    kept = [w for w in words if w.lower() not in _FILLER]
    out = " ".join(kept or words).strip()
    return out or (query or "").strip()


def _suggest_sync(query: str) -> list[str]:
    """YouTube autocomplete — returns spelling-corrected completions."""
    if not query:
        return []
    for base in _SUGGEST_URLS:
        try:
            url = base + urllib.parse.quote(query)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                raw = resp.read().decode("utf-8", "replace").strip()
            match = _JSONP_RE.match(raw)
            if match:
                raw = match.group(1)
            data = json.loads(raw)
            items = data[1] if isinstance(data, list) and len(data) > 1 else []
            out = []
            for item in items:
                text = item[0] if isinstance(item, list) and item else item
                if isinstance(text, str) and text.strip():
                    out.append(text.strip())
            if out:
                return out[:5]
        except Exception as exc:  # network hiccup / rate limit — non-fatal
            LOGGER.debug("query autocomplete failed for %r: %s", query[:40], exc)
    return []


async def suggest(query: str) -> list[str]:
    import asyncio

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _suggest_sync, query), timeout=5.0
        )
    except Exception:
        return []


async def query_variants(query: str, limit: int = 4) -> list[str]:
    """Ordered, de-duplicated retry queries for a failed search."""
    original = (query or "").strip()
    if not original or is_url(original):
        return []

    variants: list[str] = []

    def add(candidate: str) -> None:
        candidate = (candidate or "").strip()
        if not candidate:
            return
        if candidate.lower() == original.lower():
            return
        if any(candidate.lower() == v.lower() for v in variants):
            return
        variants.append(candidate)

    cleaned = clean_query(original)
    add(cleaned)

    base = cleaned or original
    words = base.split()

    # Autocomplete on the full query first; a long, noisy query often has no
    # completion at all, so also ask for progressively shorter prefixes —
    # that is where the actual spelling correction ("cherra" -> "chehra")
    # comes back from YouTube.
    probes = [base]
    for size in (5, 4, 3):
        if len(words) > size:
            probes.append(" ".join(words[:size]))

    for probe in probes:
        for suggestion in await suggest(probe):
            # Keep only real corrections, not "<same query> + more words".
            if suggestion.lower().startswith(probe.lower()) and probe == base:
                continue
            add(suggestion)
            break
        if len(variants) >= limit:
            break

    # Progressive shortening: the first 3-4 words usually carry the title.
    if len(words) > 4:
        add(" ".join(words[:4]))
    if len(words) > 2:
        add(" ".join(words[:2]) + " song")

    return variants[:limit]

# ─────────────────────────────────────────────────────────────────────────────
#  Lightweight fuzzy matching (stdlib only — difflib.SequenceMatcher)
# ─────────────────────────────────────────────────────────────────────────────

def similarity(a: str, b: str) -> float:
    """0..1 similarity ratio between two strings (case-insensitive)."""
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def best_match(query: str, candidates: list[dict], key: str = "title") -> "dict | None":
    """Pick the candidate whose `key` field best fuzzy-matches `query`."""
    if not candidates:
        return None
    return max(candidates, key=lambda c: similarity(query, str(c.get(key, ""))))


async def find_suggestions(query: str, limit: int = 5) -> list[dict]:
    """Best-effort "did you mean" results for a query that resolved to
    nothing: cleaned query + a relaxed YouTube search, fuzzy-ranked against
    what the user actually typed.

    Used by /play (and reusable anywhere else) to offer tappable
    alternatives instead of a bare error when nothing plays.
    """
    from melody.core.ytdl import search_youtube

    original = (query or "").strip()
    if not original or is_url(original):
        return []

    cleaned = clean_query(original)
    queries = list(dict.fromkeys([cleaned, original]))

    candidates: list[dict] = []
    seen: set = set()
    for q in queries:
        try:
            results = await search_youtube(q, limit=limit)
        except Exception:
            results = []
        for r in results:
            vid = r.get("id")
            if vid and vid not in seen:
                seen.add(vid)
                candidates.append(r)
        if len(candidates) >= limit:
            break

    candidates.sort(key=lambda r: similarity(original, r.get("title", "")), reverse=True)
    return candidates[:limit]
