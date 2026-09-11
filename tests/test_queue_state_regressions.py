from melody.core import queue


def _track(video_id="id1"):
    return queue.Track(
        video_id=video_id,
        title="Track",
        duration=120,
        stream_url="",
        thumbnail="",
        uploader="Uploader",
        requester_id=1,
        requester_name="User",
        requested_in=-100,
    )


def test_clear_queue_removes_predownloaded_track(monkeypatch):
    chat_id = 990001
    monkeypatch.setattr(queue, "_queues", {chat_id: [_track("queued")]})
    monkeypatch.setattr(queue, "_current", {chat_id: _track("current")})
    monkeypatch.setattr(queue, "_predownloaded", {chat_id: _track("stale")})
    monkeypatch.setattr(queue, "_persist", lambda _chat_id: None)

    queue.clear_queue(chat_id)

    assert queue.get_queue(chat_id) == []
    assert queue.get_current(chat_id) is None
    assert queue.peek_predownloaded(chat_id) is None


def test_restore_snapshot_replaces_stale_state_and_sanitizes_values(monkeypatch):
    chat_id = 990002
    monkeypatch.setattr(queue, "_current", {chat_id: _track("old")})
    monkeypatch.setattr(queue, "_queues", {chat_id: [_track("old-queued")]})
    monkeypatch.setattr(queue, "_loop", {chat_id: "single"})
    monkeypatch.setattr(queue, "_volume", {chat_id: 200})

    queue.restore_snapshot({
        "chat_id": chat_id,
        "current": None,
        "queue": [],
        "loop": "corrupt",
        "volume": "not-a-number",
    })

    assert queue.get_current(chat_id) is None
    assert queue.get_queue(chat_id) == []
    assert queue.get_loop(chat_id) == "none"
    assert queue.get_volume(chat_id) == 100
