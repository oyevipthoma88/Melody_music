import asyncio
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_command_filter_refreshes_live_username(monkeypatch):
    from utils import command_patch
    from melody import config

    command_patch._cached_username = "oldbot"
    monkeypatch.setattr(config.Config, "BOT_USERNAME", "oldbot", raising=False)
    import melody
    monkeypatch.setattr(melody, "bot", SimpleNamespace(me=SimpleNamespace(username="newbot")))
    assert command_patch._get_bot_username_lower() == "newbot"


def test_assistant_identity_refreshes_username_without_rpc(monkeypatch):
    from utils import error_translate
    import melody

    error_translate._assistant_cache.clear()
    assistant = SimpleNamespace(me=SimpleNamespace(id=42, username="fresh_assistant", first_name="Fresh"))
    monkeypatch.setattr(melody, "assistant", assistant)
    result = asyncio.run(error_translate.assistant_identity())
    assert result == (42, "fresh_assistant", "@fresh_assistant")


def test_large_media_policy_accepts_three_gb_and_three_hour_files():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    proxy = (ROOT / "utils/tg_media_proxy.py").read_text(encoding="utf-8")
    assert "_TAGGED_MAX_DURATION = max(600, _env_int(\"TAGGED_MAX_DURATION_SECONDS\", 6 * 3600))" in source
    assert "size alone is no longer rejected" in source
    assert "No full movie is kept in RAM or on disk" in proxy
    assert "_CHUNK_BYTES = 1024 * 1024" in proxy


def test_assistant_rejoin_path_unbans_before_joining():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "await _unban_or_unmute_assistant(chat_id)" in source
    assert "await _auto_join_assistant(chat_id)" in source
    assert "await bot.unban_chat_member(chat_id, assistant_id)" in source
    assert "_block_join(chat_id)" in source
