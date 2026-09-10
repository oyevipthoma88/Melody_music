"""Regression guards for the VC notification / chat-log policy."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_stale_vc_chatlog_module_is_gone():
    assert not (ROOT / "melody/plugins/misc/vc_chatlog.py").exists()


def test_join_left_cards_are_separate_and_two_seconds():
    notify = _read("melody/core/vc_notify.py")
    assert "JOIN_LEFT_AUTO_DELETE = 2" in notify
    assert "def _join_card(" in notify and "def _left_card(" in notify
    assert "#JᴏɪɴᴇᴅVɪᴅᴇᴏCʜᴀᴛ" in notify and "#LᴇғᴛVɪᴅᴇᴏCʜᴀᴛ" in notify


def test_vc_invite_is_never_auto_deleted_and_tags_users():
    notify = _read("melody/core/vc_notify.py")
    events = _read("melody/plugins/misc/vc_events.py")
    assert "INVITE_AUTO_DELETE = 0" in notify
    assert "INVITE_AUTO_DELETE = 0" in events
    assert "tag_list(invited)" in events
    assert "tg://user?id=" in events


def test_every_vc_surface_uses_the_shared_theme():
    assert (ROOT / "melody/core/vc_theme.py").exists()
    for path in (
        "melody/core/vc_notify.py",
        "melody/core/vc_chat_log.py",
        "melody/plugins/misc/vc_events.py",
        "melody/plugins/misc/vc_chat_log.py",
    ):
        assert "vc_theme" in _read(path), path


def test_chat_log_defaults_and_routing():
    core = _read("melody/core/vc_chat_log.py")
    assert "DEFAULT_DELETE = 5" in core
    assert "OFF_DURATION = 30 * 60" in core
    assert "VC_CHAT_LOG_CHANNEL_ID" in core
    # The plain activity log group must never receive in-call chat text:
    # no code path may send to LOG_GROUP_ID (docstrings may mention it).
    code = "\n".join(
        line for line in core.splitlines() if "LOG_GROUP_ID" in line
    )
    assert "send_message" not in code


def test_chat_log_does_not_depend_on_music_playing():
    listener = _read("melody/core/vc_listener.py")
    plugin = _read("melody/plugins/misc/vc_chat_log.py")
    assert "async def note(" in listener
    assert "_wanted(" in listener
    assert "vc_listener.note(" in plugin


def test_group_chat_is_never_logged():
    """REQUESTED: "bs vc chat ka log dikhaya kr, gc chat log system hata de"."""
    core = _read("melody/core/vc_chat_log.py")
    plugin = _read("melody/plugins/misc/vc_chat_log.py")
    assert "feed_group_message" not in core
    assert "feed_group_message" not in plugin
    assert "mirror" not in core.lower().split('"""')[2].lower()


def test_vc_service_cards_are_deduped_once():
    events = _read("melody/plugins/misc/vc_events.py")
    notify = _read("melody/core/vc_notify.py")
    assert "def _fresh(" in events
    for kind in ('"started"', '"ended"', 'f"invited:'):
        assert kind in events
    # py-tgcalls must stay silent for events that already arrive as service
    # messages, otherwise every invite / end card is posted twice.
    tail = notify.split("async def notify_chat_update")[1]
    assert "INVITED_VOICE_CHAT:\n            return" in tail
    assert "CLOSED_VOICE_CHAT:\n            return" in tail


def test_chat_log_card_has_only_two_buttons():
    core = _read("melody/core/vc_chat_log.py")
    kb = core.split("def log_keyboard(")[1].split("# ─────────────────────────────── the log card")[0]
    # Sirf 2 buttons: ON/OFF + close. Koi timer / settings panel nahi.
    assert "vclog:toggle:" in kb
    assert "gc_close" in kb
    assert "Mᴏʀᴇ Sᴇᴛᴛɪɴɢs" not in kb
    assert "vclog:mode" not in core


def test_assistant_never_sits_in_a_voice_chat():
    """The assistant may only be in a VC while it is actually streaming."""
    listener = _read("melody/core/vc_listener.py")
    call = _read("melody/core/call.py")
    # No silent join anywhere in the watcher.
    assert "join_as_listener" not in listener
    # join_as_listener is a disabled no-op.
    body = call.split("async def join_as_listener")[1].split("async def leave_listener")[0]
    assert "return False" in body
    assert "_pre_join_locked" not in body


def test_join_flood_wait_is_handled():
    call = _read("melody/core/call.py")
    assert "def _flood_seconds(" in call
    assert "_FLOOD_RETRY_LIMIT" in call
    assert "FloodWait," in call


def test_chat_log_card_has_no_time_at_all():
    """REQUESTED: "vc chat card se all typ ka time hata de"."""
    core = _read("melody/core/vc_chat_log.py")
    flush = core.split("async def _flush(")[1].split("async def _pending_worker(")[0]
    assert "vclog:toggle:" in core
    assert "gc_close" in core
    assert "vclog:timer:" not in core
    assert "vclog:set:" not in core
    # no clock / countdown text on the card, but it DOES self-destruct in 5s
    assert "strftime" not in flush
    assert "auto_delete(sent, AUTO_DELETE)" in flush
    assert "AUTO_DELETE = 5" in core


def test_no_vc_chat_message_is_ever_dropped():
    """REQUESTED: "strictly all vc ki chat show"."""
    core = _read("melody/core/vc_chat_log.py")
    assert "_queue_pending(" in core
    assert "async def _pending_worker(" in core
    assert "async def _orphan_dump(" in core


def test_hidden_group_owner_is_never_revealed():
    view = _read("utils/owner_view.py")
    assert "def is_anonymous_member(" in view
    assert "HIDDEN" in view
    gc = _read("melody/plugins/admin/gcmanage.py")
    assert "owner_label(" in gc


# ─── log-noise / flood regressions (from the worker log dump) ────────────────

def test_leaving_a_dead_call_is_not_a_warning():
    call = _read("melody/core/call.py")
    assert "no active group call" in call
    assert "group call not found" in call


def test_auto_join_backs_off_after_permission_failure():
    call = _read("melody/core/call.py")
    assert "_join_blocked(chat_id)" in call
    assert "_block_join(chat_id)" in call
    # the repeated CHAT_ADMIN_REQUIRED lines must not stay at warning level
    assert "_join_log_level(chat_id)," in call
    assert 'LOGGER.warning("Auto-join: bot could not export invite link' not in call
    assert 'LOGGER.warning("Could not auto-unban/unmute assistant' not in call


def test_call_is_live_handles_flood_wait_without_blocking():
    notify = _read("melody/core/vc_notify.py")
    assert 'type(exc).__name__ == "FloodWait"' in notify
    assert "flood_for" in notify


def test_startup_scan_is_rate_limited_and_feature_gated():
    listener = _read("melody/core/vc_listener.py")
    assert "asyncio.Semaphore(2)" in listener
    assert "if not await _wanted(chat_id)" in listener


def test_chat_admin_required_fails_before_download_and_notifies_once():
    call = _read("melody/core/call.py")
    assert "VC_ADMIN_REQUIRED_MESSAGE" in call
    assert "_vc_admin_blocked(chat_id)" in call
    assert "_vc_admin_notice_needed(chat_id)" in call
    assert "return False" in call.split("if _vc_admin_blocked(chat_id)", 1)[1].split("try:", 1)[0]
    assert "playing_now = await force_play_stream" in _read("melody/plugins/music/play.py")
