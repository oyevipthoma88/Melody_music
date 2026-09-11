import asyncio
from types import SimpleNamespace


def _update(user_id, message_id, text):
    return SimpleNamespace(
        id=message_id,
        text=text,
        caption=None,
        chat=SimpleNamespace(id=-100),
        from_user=SimpleNamespace(id=user_id),
        react=None,
    )


def test_play_commands_do_not_block_different_users_but_controls_do():
    from utils import cmd_lock

    async def run():
        cmd_lock._inflight.clear()
        cmd_lock._recent.clear()
        cmd_lock._seen_messages.clear()
        first = await cmd_lock.acquire(_update(1, 101, "/play song one"))
        second = await cmd_lock.acquire(_update(2, 102, "/play song two"))
        control_one = await cmd_lock.acquire(_update(1, 103, "/skip"))
        control_two = await cmd_lock.acquire(_update(2, 104, "/skip"))
        cmd_lock.release(first[1], _update(1, 101, "/play song one"))
        cmd_lock.release(second[1], _update(2, 102, "/play song two"))
        cmd_lock.release(control_one[1], _update(1, 103, "/skip"))
        return first, second, control_one, control_two

    first, second, control_one, control_two = asyncio.run(run())
    assert first[0] and second[0]
    assert control_one[0]
    assert control_two[0] is False


def test_same_user_play_double_tap_is_still_suppressed():
    from utils import cmd_lock

    async def run():
        cmd_lock._inflight.clear()
        cmd_lock._recent.clear()
        cmd_lock._seen_messages.clear()
        update_one = _update(7, 201, "/play song")
        update_two = _update(7, 202, "/play song")
        first = await cmd_lock.acquire(update_one)
        cmd_lock.release(first[1], update_one)
        second = await cmd_lock.acquire(update_two)
        return first, second

    first, second = asyncio.run(run())
    assert first[0]
    assert second[0] is False
