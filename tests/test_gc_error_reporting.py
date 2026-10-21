"""Exercise the real error reporter without Telegram credentials."""
import ast
import asyncio
import logging
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def reporter(monkeypatch):
    calls = []
    async def send_message(*args, **kwargs):
        calls.append((args, kwargs))
    bot_module = ModuleType('melody')
    bot_module.bot = SimpleNamespace(send_message=send_message)
    monkeypatch.setitem(sys.modules, 'melody', bot_module)
    source = Path(__file__).resolve().parents[1] / 'melody/logging.py'
    tree = ast.parse(source.read_text())
    names = {'send_error_log', 'is_expected_chat_problem'}
    selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    import html
    import traceback
    namespace = dict(re=re, LOGGER=logging.getLogger('test'), html=html, traceback=traceback,
                     redact_sensitive_text=str, fancy=lambda s: s, _utc_now=lambda: 'now',
                     Config=SimpleNamespace(LOG_GROUP_ID=-100123, OWNER_ID=456),
                     enums=SimpleNamespace(ParseMode=SimpleNamespace(HTML='html')),
                     LinkPreviewOptions=lambda **kw: kw)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['send_error_log'], calls


def test_muted_bot_sends_no_log_and_no_owner_tag(monkeypatch):
    send, calls = reporter(monkeypatch)
    error = type('ChatWriteForbidden', (Exception,), {})('Telegram refused')
    asyncio.run(send('play failed', error, context={'chat_id': -1001}))
    assert calls == []


def test_user_side_permissions_send_no_log_and_no_owner_tag(monkeypatch):
    send, calls = reporter(monkeypatch)
    for name in ('ChatAdminRequired', 'UserBannedInChannel', 'ChannelPrivate',
                 'UserIsBlocked', 'InviteRequestSent', 'NoActiveGroupCall',
                 'GroupCallFull', 'ParticipantJoinMissing', 'QueryIdInvalid',
                 'ChatSendPhotosForbidden', 'ChatSendMediaForbidden'):
        error = type(name, (Exception,), {})('Telegram refused')
        asyncio.run(send('command failed', error))
        assert calls == [], name


def test_wrapped_permission_error_is_not_reported(monkeypatch):
    send, calls = reporter(monkeypatch)
    wrapped = RuntimeError('voice join failed')
    wrapped.__cause__ = Exception('Telegram [403 CHAT_ADMIN_REQUIRED]')
    asyncio.run(send('play failed', wrapped))
    assert calls == []


def test_text_only_telegram_error_is_not_reported(monkeypatch):
    send, calls = reporter(monkeypatch)
    asyncio.run(send('Telegram: CHAT_WRITE_FORBIDDEN'))
    assert calls == []


def test_internal_errors_still_report_and_tag_owner(monkeypatch):
    send, calls = reporter(monkeypatch)
    for error in (PermissionError('disk permission denied'), RuntimeError('API 403 forbidden'),
                  TypeError('mute_stream invalid argument')):
        asyncio.run(send('mute_stream failed', error))
    assert len(calls) == 3
    assert all(args[0] == -100123 and 'tg://user?id=456' in args[1] for args, _ in calls)
