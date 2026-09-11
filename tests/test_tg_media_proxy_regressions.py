import asyncio


def test_concurrent_chunk_waiters_receive_shared_bytes(monkeypatch):
    from utils import tg_media_proxy as proxy

    async def run():
        calls = 0

        async def fake_read_chunk_inner(_entry, _index):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return b"chunk-data"

        monkeypatch.setattr(proxy, "_read_chunk_inner", fake_read_chunk_inner)
        proxy_entry = type("Entry", (), {"cache": {}, "chunk_futures": {}})()
        result = await asyncio.gather(
            proxy._read_chunk(proxy_entry, 7),
            proxy._read_chunk(proxy_entry, 7),
        )
        assert result == [b"chunk-data", b"chunk-data"]
        assert calls == 1
        assert proxy_entry.chunk_futures == {}

    asyncio.run(run())


def test_proxy_cache_has_global_budget_for_many_large_movies(monkeypatch):
    from utils import tg_media_proxy as proxy

    old_entries = proxy._entries
    old_bytes = proxy._cache_bytes
    try:
        proxy._entries = {}
        proxy._cache_bytes = 0
        monkeypatch.setattr(proxy, "_CACHE_MAX_BYTES", 2 * proxy._CHUNK_BYTES)
        monkeypatch.setattr(proxy, "_CACHE_CHUNKS", 64)
        entries = [
            proxy._MediaEntry(None, None, 3 * 1024**3, "video/mp4", str(i), 0.0)
            for i in range(4)
        ]
        for i, entry in enumerate(entries):
            proxy._entries[str(i)] = entry
            proxy._cache_put(entry, i, b"x" * proxy._CHUNK_BYTES)
        assert proxy._cache_bytes <= 2 * proxy._CHUNK_BYTES
    finally:
        proxy._entries = old_entries
        proxy._cache_bytes = old_bytes
