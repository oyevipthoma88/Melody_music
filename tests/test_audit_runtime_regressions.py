"""Execute actual bot functions with network-free dependencies, not source assertions."""
import ast
import asyncio
import heapq
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text())
    selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names]
    assert len(selected) == len(names)
    exec(compile(ast.Module(body=selected, type_ignores=[]), path, 'exec'), namespace)
    return namespace


def run(coro):
    return asyncio.run(coro)


def test_reactions_are_default_off_and_explicitly_opt_in(monkeypatch):
    from utils import cmd_lock
    calls = []
    async def react(emoji):
        calls.append(emoji)
    async def scenario():
        for value in (None, '', 'false', '0', 'off'):
            if value is None:
                monkeypatch.delenv('COMMAND_REACTIONS', raising=False)
            else:
                monkeypatch.setenv('COMMAND_REACTIONS', value)
            cmd_lock.instant_ack(SimpleNamespace(react=react))
            await asyncio.sleep(0)
            assert calls == []
        monkeypatch.setenv('COMMAND_REACTIONS', 'true')
        cmd_lock.instant_ack(SimpleNamespace(react=react))
        await asyncio.sleep(0)
        assert calls == ['👍']
    run(scenario())


def test_catch_all_does_not_remove_actual_command_ownership(monkeypatch):
    from utils import cmd_lock
    monkeypatch.setattr(cmd_lock, '_inflight', {})
    monkeypatch.setattr(cmd_lock, '_owners', {})
    monkeypatch.setattr(cmd_lock, '_recent', {})
    monkeypatch.setattr(cmd_lock, '_seen_messages', {})
    def msg(id, user):
        return SimpleNamespace(text='/skip', chat=SimpleNamespace(id=1), from_user=SimpleNamespace(id=user), id=id)
    async def scenario():
        one, two = msg(11, 2), msg(12, 3)
        ok, key = await cmd_lock.acquire(one, handler='listener')
        assert ok
        cmd_lock.release(key, one)
        ok, key = await cmd_lock.acquire(one, handler='skip')
        assert ok and key
        ok2, _ = await cmd_lock.acquire(two, handler='skip')
        assert not ok2
        cmd_lock.release(key, one)
    run(scenario())


def test_expired_owner_cannot_release_new_lock(monkeypatch):
    from utils import cmd_lock
    monkeypatch.setattr(cmd_lock, '_inflight', {})
    monkeypatch.setattr(cmd_lock, '_owners', {})
    monkeypatch.setattr(cmd_lock, '_recent', {})
    monkeypatch.setattr(cmd_lock, '_seen_messages', {})
    clock = [0.0]
    monkeypatch.setattr(cmd_lock.time, 'monotonic', lambda: clock[0])
    def msg(id):
        return SimpleNamespace(text='/play', chat=SimpleNamespace(id=1), from_user=SimpleNamespace(id=id), id=id)
    async def scenario():
        _, old = await cmd_lock.acquire(msg(1))
        clock[0] = cmd_lock.MAX_HOLD + 1
        _, new = await cmd_lock.acquire(msg(2))
        cmd_lock.release(old)
        allowed, _ = await cmd_lock.acquire(msg(3))
        assert not allowed
        cmd_lock.release(new)
    run(scenario())


def settings_namespace():
    class Collection:
        async def find_one(self, query):
            return None
        async def update_one(self, *args, **kwargs):
            raise RuntimeError('writes are blocked: over your space quota')
    ns = dict(time=time, _settings_cache={}, _SETTINGS_TTL=45, _MISSING_SETTING=object(), settings_col=Collection(), _STORAGE_WRITES_DISABLED=False, _is_storage_quota_error=lambda exc: True)
    def disable(exc):
        ns['_STORAGE_WRITES_DISABLED'] = True
    ns['_disable_storage_writes'] = disable
    return functions('utils/database.py', {'get_setting', 'set_setting'}, ns)


def test_missing_setting_uses_each_callers_default():
    ns = settings_namespace()
    async def scenario():
        assert await ns['get_setting'](1, 'enabled', False) is False
        assert await ns['get_setting'](1, 'enabled', True) is True
        assert await ns['get_setting'](1, 'enabled', None) is None
    run(scenario())


def test_quota_failure_never_reports_setting_saved():
    ns = settings_namespace()
    async def scenario():
        for _ in range(2):
            try:
                await ns['set_setting'](1, 'enabled', True)
            except RuntimeError as exc:
                assert 'not saved' in str(exc)
            else:
                raise AssertionError('false success after quota failure')
        assert ns['_settings_cache'] == {}
    run(scenario())


def playback_namespace():
    clock = SimpleNamespace(value=100.0)
    track = SimpleNamespace(video_id='song', duration=1000)
    async def nothing(*args, **kwargs):
        return None
    class Call:
        def __init__(self):
            self.plays = []
        pause = staticmethod(nothing)
        resume = staticmethod(nothing)
        async def play(self, chat, stream):
            self.plays.append(stream)
    ns = dict(asyncio=asyncio, time=SimpleNamespace(time=lambda: clock.value), _pytgcalls=Call(), _active={1: True}, _paused_at={}, _play_start_time={1: 100}, _seek_offset={}, _speed={}, _silence_playing={}, _is_video={}, _stream_generation={1: 1}, _CONTROL_RPC_TIMEOUT=2, _LOCAL_PLAY_TIMEOUT=2, LOGGER=logging.getLogger('test'), send_error_log=nothing, get_current=lambda chat: track, _is_leaving=lambda chat: False, _forget_call_state=lambda chat: ns['_active'].pop(chat, None), _is_not_in_call=lambda exc: False, get_speed=lambda chat: ns['_speed'].get(chat, 1), get_queue=lambda chat: [], get_loop=lambda chat: 'none', get_volume=lambda chat: 100, _local_media_stream=lambda *args: 'local', _is_probe_error=lambda exc: False)
    lock = asyncio.Lock()
    ns['_get_stream_commit_lock'] = lambda chat: lock
    ns['_build_direct_stream'] = nothing
    functions('melody/core/call.py', {'pause_stream', 'resume_stream', 'get_playback_position', 'seek_stream', 'set_playback_speed'}, ns)
    return ns, clock


def test_pause_freezes_position_and_resume_preserves_speed():
    async def scenario():
        ns, clock = playback_namespace()
        ns['_speed'][1] = 1.5
        clock.value = 110
        assert await ns['pause_stream'](1)
        clock.value = 160
        assert ns['get_playback_position'](1) == 15
        assert await ns['resume_stream'](1)
        clock.value = 170
        assert ns['get_playback_position'](1) == 30
    run(scenario())


def test_pending_seek_is_fenced_after_stop_or_new_track(monkeypatch):
    import sys
    async def scenario(replace):
        ns, _ = playback_namespace()
        entered, ready = asyncio.Event(), asyncio.Event()
        async def build(*args, **kwargs):
            entered.set()
            await ready.wait()
            return 'stale'
        ns['_build_direct_stream'] = build
        monkeypatch.setitem(sys.modules, 'melody.core.ytdl', SimpleNamespace(cached_file_path=lambda *a, **k: None, download_audio=build))
        task = asyncio.create_task(ns['seek_stream'](1, 25))
        await entered.wait()
        ns['_stream_generation'][1] = 2
        if replace:
            ns['get_current'] = lambda chat: SimpleNamespace(video_id='new')
        else:
            ns['_active'].clear()
        ready.set()
        try:
            await task
        except RuntimeError as exc:
            assert 'changed' in str(exc)
        else:
            raise AssertionError('stale seek committed')
        assert ns['_pytgcalls'].plays == []
        assert ns['_seek_offset'] == {}
    run(scenario(False))
    run(scenario(True))


def test_failed_speed_change_rolls_back_rate(monkeypatch):
    import sys
    async def scenario():
        ns, clock = playback_namespace()
        async def build(*args, **kwargs):
            if kwargs.get('speed') == 2:
                raise ValueError('resolver failed')
            return 'direct'
        ns['_build_direct_stream'] = build
        monkeypatch.setitem(sys.modules, 'melody.core.ytdl', SimpleNamespace(cached_file_path=lambda *a, **k: None, download_audio=build))
        clock.value = 110
        assert not await ns['set_playback_speed'](1, 2)
        assert ns['get_speed'](1) == 1
        assert ns['get_playback_position'](1) == 10
        assert ns['_pytgcalls'].plays == []
    run(scenario())


def download_namespace():
    ns = dict(asyncio=asyncio, threading=threading, heapq=heapq, _download_cancel_events={}, _cache_tag=lambda audio: 'audio', LOGGER=logging.getLogger('test'))
    return functions('melody/core/ytdl.py', {'cancel_download', 'cancel_lower_priority_downloads', '_PriorityDownloadGate'}, ns)


def test_stopping_one_shared_download_owner_preserves_other():
    ns = download_namespace()
    event = threading.Event()
    ns['_download_cancel_events']['song:audio'] = (event, {1, 2}, 0)
    assert not ns['cancel_download']('song', owner=3)
    assert not ns['cancel_download']('song', owner=1)
    assert not event.is_set()
    assert ns['cancel_download']('song', owner=2)
    assert event.is_set()


def test_promoted_shared_download_is_not_preempted():
    ns = download_namespace()
    event = threading.Event()
    ns['_download_cancel_events']['song:audio'] = (event, {1, 2}, 0)
    assert ns['cancel_lower_priority_downloads'](0) == 0
    assert not event.is_set()


def test_waiting_download_can_be_promoted_and_cancelled_slot_is_returned():
    async def scenario():
        ns = download_namespace()
        gate = ns['_PriorityDownloadGate'](1)
        await gate.acquire()
        low = asyncio.create_task(gate.acquire(50, key='shared'))
        other = asyncio.create_task(gate.acquire(10, key='other'))
        await asyncio.sleep(0)
        gate.promote('shared', 0)
        await gate.release()
        # Cancellation after grant must not leak the only slot.
        low.cancel()
        await asyncio.gather(low, return_exceptions=True)
        await asyncio.wait_for(other, 1)
        await gate.release()
        assert gate.running == 0
        assert gate.waiters == []
    run(scenario())


def test_cancel_before_grant_cannot_reclaim_another_waiters_slot():
    async def scenario():
        ns = download_namespace()
        gate = ns['_PriorityDownloadGate'](1)
        await gate.acquire()
        cancelled = asyncio.create_task(gate.acquire(0))
        survivor = asyncio.create_task(gate.acquire(10))
        await asyncio.sleep(0)
        cancelled.cancel()
        await gate.release()
        await asyncio.gather(cancelled, return_exceptions=True)
        await survivor
        assert gate.running == 1
        extra = asyncio.create_task(gate.acquire(0))
        await asyncio.sleep(0)
        assert not extra.done()
        await gate.release()
        await extra
        await gate.release()
        assert gate.running == 0
    run(scenario())
