"""JioSaavn + Piped last-resort fallback for Melody_music (403/bot-check fix)."""
import logging
import aiohttp
import asyncio
from difflib import SequenceMatcher

LOGGER = logging.getLogger(__name__)
_SAV = "https://saavn.dev/api"
_PIPED = ["https://pipedapi.kavin.rocks", "https://pipedapi.adminforge.de",
          "https://pipedapi.ducks.party", "https://api.piped.private.coffee"]


async def _jiosaavn(query, expected_title=None):
    """JioSaavn exact audio URL — title verified (55% threshold)."""
    if not query:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{_SAV}/search/songs",
                             params={"query": query, "limit": 5},
                             timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status != 200:
                    return None
                d = await r.json()
            results = (d.get("data") or {}).get("results") or []
            if not results:
                return None
            best, best_score = None, 0
            for item in results:
                t = (item.get("name") or "").lower()
                if expected_title:
                    score = SequenceMatcher(None, expected_title.lower(), t).ratio()
                    if score > best_score:
                        best_score, best = score, item
                else:
                    best = item
                    break
            if expected_title and best_score < 0.55:
                LOGGER.warning(f"JioSaavn skip ({best_score:.2f}): {best.get('name') if best else '?'}")
                return None
            if not best:
                return None
            url = (best.get("downloadUrl") or [])
            for u in url:
                if u.get("quality") in ("320kbps", "160kbps", "96kbps"):
                    link = u.get("link") or u.get("url")
                    if link:
                        LOGGER.info(f"✅ JioSaavn: {best.get('name')[:50]}")
                        return link
    except Exception as e:
        LOGGER.debug(f"JioSaavn error: {e}")
    return None


async def _piped(video_id):
    async def one(inst):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{inst}/streams/{video_id}",
                                 timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status != 200:
                        return None
                    d = await r.json()
                    for f in d.get("audioStreams", []):
                        u = f.get("url")
                        if u and u.startswith("http"):
                            return u
        except Exception:
            return None
        return None
    results = await asyncio.gather(*[one(i) for i in _PIPED], return_exceptions=True)
    for r in results:
        if isinstance(r, str) and r.startswith("http"):
            LOGGER.info(f"✅ Piped: {video_id}")
            return r
    return None


async def try_alt_sources(video_id, query=None, expected_title=None):
    """Try JioSaavn + Piped in parallel — return first working URL or None."""
    tasks = [
        asyncio.create_task(_jiosaavn(query or expected_title, expected_title)),
        asyncio.create_task(_piped(video_id)),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=10)
        while done or pending:
            for t in list(done):
                try:
                    r = t.result()
                    if r:
                        for p in pending:
                            p.cancel()
                        return r
                except Exception:
                    pass
                done.discard(t)
            if not pending:
                break
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED, timeout=3)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return None
