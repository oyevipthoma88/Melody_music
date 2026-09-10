"""Bounded localhost range proxy for large Telegram media.

PyTgCalls accepts HTTP media URLs, while Kurigram's ``stream_media`` yields
Telegram files in <=1 MiB chunks with an offset. This adapter bridges those two
APIs: FFmpeg can issue HEAD/Range requests and the proxy fetches only the
requested Telegram chunks. No full movie is kept in RAM or on disk.
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionError, ClientConnectionResetError

from melody.logging import LOGGER

_CHUNK_BYTES = 1024 * 1024
_MAX_PROXIES = 64
_PROXY_TTL = 6 * 3600.0
_PROXY_CONCURRENCY = 4


@dataclass
class _MediaEntry:
    client: object
    message: object
    size: int
    content_type: str
    name: str
    touched: float


_entries: dict[str, _MediaEntry] = {}
_runner: web.AppRunner | None = None
_port: int | None = None
_server_lock = asyncio.Lock()
_stream_slots = asyncio.Semaphore(_PROXY_CONCURRENCY)


def _prune() -> None:
    now = time.monotonic()
    for token, entry in list(_entries.items()):
        if now - entry.touched > _PROXY_TTL:
            _entries.pop(token, None)
    if len(_entries) > _MAX_PROXIES:
        victims = sorted(_entries, key=lambda token: _entries[token].touched)
        for token in victims[: len(_entries) - _MAX_PROXIES]:
            _entries.pop(token, None)


def _content_type(name: str, fallback: str | None = None) -> str:
    return fallback or mimetypes.guess_type(name or "")[0] or "application/octet-stream"


def _parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise ValueError
    raw = value[6:].strip()
    if "-" not in raw:
        raise ValueError
    left, right = raw.split("-", 1)
    try:
        if left:
            start = int(left)
            end = int(right) if right else size - 1
        else:
            length = int(right)
            if length <= 0:
                raise ValueError
            start = max(0, size - length)
            end = size - 1
    except ValueError:
        raise ValueError from None
    if start < 0 or start >= size or end < start:
        raise ValueError
    return start, min(end, size - 1)


async def _media_handler(request: web.Request) -> web.StreamResponse:
    _prune()
    token = request.match_info.get("token", "")
    entry = _entries.get(token)
    if entry is None or entry.size <= 0:
        raise web.HTTPNotFound()
    entry.touched = time.monotonic()
    try:
        byte_range = _parse_range(request.headers.get("Range"), entry.size)
    except ValueError:
        raise web.HTTPRequestRangeNotSatisfiable(
            headers={"Content-Range": f"bytes */{entry.size}"}
        )

    if byte_range is None:
        start, end = 0, entry.size - 1
        status = 200
    else:
        start, end = byte_range
        status = 206
    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": entry.content_type,
        "Cache-Control": "no-store",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{entry.size}"
    if request.method == "HEAD":
        return web.Response(status=status, headers=headers)

    # Limit active Telegram chunk streams across all chats. HEAD remains cheap,
    # while at most four large media bodies can consume Telegram/network work.
    async with _stream_slots:
        response = web.StreamResponse(status=status, headers=headers)
        try:
            await response.prepare(request)
        except (ClientConnectionResetError, ClientConnectionError, ConnectionResetError, BrokenPipeError):
            LOGGER.debug("telegram media proxy client disconnected before headers")
            return response
        first_chunk = start // _CHUNK_BYTES
        skip = start % _CHUNK_BYTES
        remaining = length
        try:
            limit = (end // _CHUNK_BYTES) - first_chunk + 1
            async for chunk in entry.client.stream_media(
                entry.message, limit=limit, offset=first_chunk
            ):
                if skip:
                    chunk = chunk[skip:]
                    skip = 0
                if not chunk:
                    continue
                piece = chunk[:remaining]
                await response.write(piece)
                remaining -= len(piece)
                if remaining <= 0:
                    break
            await response.write_eof()
        except asyncio.CancelledError:
            raise
        except (ClientConnectionResetError, ClientConnectionError, ConnectionResetError, BrokenPipeError):
            # FFmpeg may abandon a probe/range request as soon as it has enough
            # bytes. This is normal for a bounded proxy and must not produce a
            # traceback or take down the worker.
            LOGGER.debug("telegram media proxy client disconnected during stream")
            try:
                await response.write_eof()
            except Exception:
                pass
        except Exception as exc:  # Telegram disconnects must not crash the bot.
            LOGGER.debug("telegram media proxy request failed: %s", type(exc).__name__)
            try:
                await response.write_eof()
            except Exception:
                pass
        return response


async def _ensure_server() -> int:
    global _runner, _port
    if _runner is not None and _port is not None:
        return _port
    async with _server_lock:
        if _runner is not None and _port is not None:
            return _port
        app = web.Application(client_max_size=1)
        app.router.add_route("HEAD", "/tg/{token}/{name:.*}", _media_handler)
        app.router.add_route("GET", "/tg/{token}/{name:.*}", _media_handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        requested_port = int(os.getenv("TG_PROXY_PORT", "0") or 0)
        site = web.TCPSite(runner, "127.0.0.1", requested_port)
        await site.start()
        sockets = getattr(site, "_server", None).sockets if getattr(site, "_server", None) else None
        if not sockets:
            await runner.cleanup()
            raise RuntimeError("telegram media proxy failed to bind")
        _runner = runner
        _port = int(sockets[0].getsockname()[1])
        LOGGER.info("telegram media range proxy ready on localhost:%d", _port)
        return _port


async def create_media_proxy(client, message, *, size: int, filename: str = "media") -> str:
    """Register a Telegram media object and return a localhost HTTP URL."""
    size = int(size or 0)
    if size <= 0:
        raise ValueError("Telegram media size is unavailable")
    port = await _ensure_server()
    token = uuid.uuid4().hex
    name = os.path.basename(filename or "media")
    _entries[token] = _MediaEntry(
        client=client,
        message=message,
        size=size,
        content_type=_content_type(name, getattr(message, "mime_type", None)),
        name=name,
        touched=time.monotonic(),
    )
    _prune()
    return f"http://127.0.0.1:{port}/tg/{token}/{quote(name, safe='')}"


async def close_media_proxy_server() -> None:
    global _runner, _port
    async with _server_lock:
        if _runner is not None:
            await _runner.cleanup()
        _runner = None
        _port = None
        _entries.clear()
