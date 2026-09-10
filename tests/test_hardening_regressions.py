from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_unmuteall_floodwait_path_imports_exception_type():
    mass = source("melody/plugins/admin/massactions.py")
    assert "from pyrogram.errors import FloodWait" in mass
    assert "except FloodWait as fw:" in mass


def test_single_join_request_commands_are_implemented_and_registered():
    handler = source("melody/plugins/admin/join_approve.py")
    registry = source("melody/__main__.py")
    assert '"approverequest"' in handler
    assert '"declinerequest"' in handler
    assert 'BotCommand("approverequest"' in registry
    assert 'BotCommand("declinerequest"' in registry


def test_clean_handler_has_no_unnecessary_get_me_rpc():
    clean = source("melody/plugins/admin/cleanall.py")
    block = clean[clean.index("async def clean_cmd"):]
    assert "me = await client.get_me()" not in block


def test_ytdl_resolver_has_no_unconditional_wrapper():
    ytdl = source("melody/core/ytdl.py")
    assert "        if True:\n            ydl_task" not in ytdl


def test_request_help_exposes_single_request_flow():
    help_source = source("melody/plugins/misc/help.py")
    assert "/approverequest" in help_source
    assert "/declinerequest" in help_source


def test_get_me_cache_deduplicates_calls_per_client():
    import asyncio
    from utils.client_cache import get_me_cached, invalidate_me_cache

    class FakeClient:
        def __init__(self, ident):
            self.ident = ident
            self.calls = 0

        async def get_me(self):
            self.calls += 1
            return type("Me", (), {"id": self.ident})()

    async def scenario():
        first = FakeClient(1)
        second = FakeClient(2)
        invalidate_me_cache()
        await get_me_cached(first)
        await get_me_cached(first)
        await get_me_cached(second)
        assert first.calls == 1
        assert second.calls == 1
        assert (await get_me_cached(first)).id == 1

    asyncio.run(scenario())


def test_queue_snapshot_validation_guards_state():
    queue_source = source("melody/core/queue.py")
    assert "if not isinstance(snapshot, dict):" in queue_source
    assert "except (KeyError, TypeError, ValueError):" in queue_source
    assert "_current.pop(chat_id, None)" in queue_source
    assert 'mode if mode in {"none", "single", "all"} else "none"' in queue_source
    assert "except (TypeError, ValueError):" in queue_source


def test_start_ui_exposes_music_and_vc_guides_with_valid_help_routes():
    start = source("melody/plugins/misc/start.py")
    help_source = source("melody/plugins/misc/help.py")
    assert 'callback_data="help_play"' in start
    assert 'callback_data="help_vc"' in start
    assert '"play": (' in help_source
    assert '"vc": (' in help_source


def test_start_group_uses_bounded_parallel_ban_checks_and_pic_lookup():
    start = source("melody/plugins/misc/start.py")
    group = start[start.index("async def start_group"):start.index("# ─── Bot added", start.index("async def start_group"))]
    assert "asyncio.gather(" in group
    assert "_start_db_call(is_gbanned" in group
    assert "_start_db_call(is_banned" in group
    assert "_start_db_call(get_pic(\"start\"), None)" in group


def test_archive_is_explicitly_opt_in_and_never_uses_foreign_default():
    archive = source("utils/telegram_archive.py")
    config = source("melody/config.py")
    assert "_DEFAULT_CHANNEL_ID" not in archive
    assert "if not raw:" in archive
    assert "return None" in archive
    assert 'MUSIC_ARCHIVE_CHANNEL_ID")' in config
    assert "-1004436084947" not in config


def test_admin_enum_and_authlist_gate_are_hardened():
    auth = source("melody/plugins/admin/auth.py")
    assert 'getattr(member.status, "value", member.status)' in auth
    assert "if not message.from_user:" in auth
    assert "if not await _is_admin(client, message):" in auth


def test_partial_early_cache_and_staging_cleanup_are_safe():
    ytdl = source("melody/core/ytdl.py")
    diskguard = source("utils/diskguard.py")
    assert 'or f.endswith(".early")' in ytdl
    assert 'os.path.basename(f).startswith("yt_")' in diskguard
    assert "shutil.rmtree(f, ignore_errors=True)" in diskguard


def test_vc_watchdog_is_started_and_startup_scan_is_bounded():
    main = source("melody/__main__.py")
    listener = source("melody/core/vc_listener.py")
    assert "vc_listener.start()" in main
    assert "get_chats_page(limit=400)" in listener


def test_natural_end_clears_current_track_state():
    call = source("melody/core/call.py")
    assert "from melody.core.queue import clear_queue" in call
    assert "clear_queue(chat_id)" in call


def test_playback_source_race_has_absolute_start_budget():
    call = source("melody/core/call.py")
    assert 'PLAY_START_BUDGET", "9.5"' in call
    assert "startup_deadline = time.monotonic() + _PLAY_START_BUDGET" in call
    assert "playback source startup budget exceeded" in call
    assert "fallback download exceeded startup budget" in call
    assert "startup_deadline - time.monotonic() > 2.0" in call


def test_startup_budget_does_not_cancel_started_fallback_download():
    call = source("melody/core/call.py")
    assert 'PLAY_FALLBACK_TIMEOUT", "30"' in call
    assert "async def _finish_started_fallback()" in call
    assert "asyncio.shield(download_task)" in call
    assert "fallback source exceeded grace window" in call
