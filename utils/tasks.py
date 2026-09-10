"""Safe fire-and-forget task helper.

BUG FIX (whole-codebase): 79 call sites did a bare

    asyncio.create_task(coro())

as a statement. asyncio only keeps a *weak* reference to a running task, so
the event loop is free to garbage-collect any of those tasks mid-flight —
which shows up in production as background work (auto-delete, VC cleanup,
mass actions, autoplay warm-ups) that randomly never finishes and never logs
anything. On top of that, an exception inside such a task was only reported
as a noisy "Task exception was never retrieved" at GC time.

``spawn()`` fixes both: it keeps a strong reference until the task completes
and it logs any exception with the task's name.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Optional, Set

LOGGER = logging.getLogger(__name__)

# Strong references to in-flight background tasks (the whole point of this
# module) — discarded again in the done-callback so this never leaks.
_BACKGROUND_TASKS: Set[asyncio.Task] = set()


def _on_done(task: asyncio.Task) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        LOGGER.exception(
            "background task %s failed: %s", task.get_name(), exc, exc_info=exc
        )


def spawn(
    coro: Coroutine[Any, Any, Any], *, name: Optional[str] = None
) -> asyncio.Task:
    """Start ``coro`` in the background, keeping it alive and logging errors."""
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_done)
    return task


def pending_tasks() -> int:
    """Number of background tasks currently in flight (used by diagnostics)."""
    return len(_BACKGROUND_TASKS)


async def cancel_all(timeout: float = 5.0) -> None:
    """Cancel every in-flight background task — used on shutdown."""
    tasks = list(_BACKGROUND_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        try:
            await asyncio.wait(tasks, timeout=timeout)
        except Exception:  # pragma: no cover - shutdown best effort
            pass
