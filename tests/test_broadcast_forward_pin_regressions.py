from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "melody/plugins/admin/broadcast.py").read_text(encoding="utf-8")


def test_broadcast_uses_true_forward_not_copy():
    assert "client.forward_messages" in SOURCE
    assert "message.reply_to_message.copy" not in SOURCE
    assert "from_chat_id=source.chat.id" in SOURCE
    assert "message_ids=source.id" in SOURCE


def test_broadcast_checks_pin_privilege_and_pins_forwarded_message():
    assert "can_pin_messages" in SOURCE
    assert "client.pin_chat_message" in SOURCE
    assert "sent.id" in SOURCE
    assert "pinned_count" in SOURCE


def test_pin_failure_does_not_mark_forward_as_failed():
    helper = SOURCE.split("async def _forward_one", 1)[1]
    assert "return True, False" in helper
    assert "return False, False" in helper


def test_flood_wait_is_retried_without_duplicate_forward():
    assert "except FloodWait as exc:" in SOURCE
    assert "await asyncio.sleep(max(1, int(exc.value)))" in SOURCE
    assert "except FloodWait:" in SOURCE
