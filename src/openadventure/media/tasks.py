"""Background-task runner: ambience work (image renders, music cues) runs
without blocking play. The tool handler returns immediately; progress and
results arrive as EngineEvents, mid-turn if a turn is active, or drained by
the frontend between turns."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Coroutine
from typing import Any

from openadventure.engine.events import (
    BackgroundTaskFinished,
    BackgroundTaskStarted,
    EngineEvent,
)


class BackgroundTasks:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[EngineEvent] = asyncio.Queue()
        self._tasks: dict[str, asyncio.Task] = {}
        self._task_kinds: dict[str, str] = {}
        self._pending_coros: dict[str, Coroutine] = {}
        self._counter = itertools.count(1)

    def spawn(
        self,
        kind: str,
        label: str,
        coro: Coroutine[Any, Any, list[EngineEvent]],
    ) -> BackgroundTaskStarted:
        """Start background work. `coro` returns the result events to emit
        (e.g. [ImageGenerated(...)]) when it completes."""
        task_id = f"{kind}-{next(self._counter)}"
        started = BackgroundTaskStarted(task_id=task_id, kind=kind, label=label)

        async def runner() -> None:
            self._pending_coros.pop(task_id, None)
            try:
                results = await coro
                for event in results:
                    self._queue.put_nowait(event)
                self._queue.put_nowait(
                    BackgroundTaskFinished(task_id=task_id, ok=True, message=f"{label}: done")
                )
            except asyncio.CancelledError:
                self._queue.put_nowait(
                    BackgroundTaskFinished(task_id=task_id, ok=False, message=f"{label}: cancelled")
                )
                raise
            except Exception as exc:
                self._queue.put_nowait(
                    BackgroundTaskFinished(task_id=task_id, ok=False, message=f"{label}: {exc}")
                )
            finally:
                self._tasks.pop(task_id, None)
                self._task_kinds.pop(task_id, None)

        self._pending_coros[task_id] = coro
        self._tasks[task_id] = asyncio.ensure_future(runner())
        self._task_kinds[task_id] = kind
        return started

    def cancel_kind(self, kind: str) -> int:
        """Cancel all pending background tasks of one kind."""
        cancelled = 0
        for task_id, task in list(self._tasks.items()):
            if self._task_kinds.get(task_id) != kind:
                continue
            if not task.done():
                task.cancel()
                cancelled += 1
                # if the runner never got to start, close its work coroutine
                pending = self._pending_coros.pop(task_id, None)
                if pending is not None:
                    pending.close()
        return cancelled

    def drain(self) -> list[EngineEvent]:
        """All events that arrived since the last drain (non-blocking)."""
        events: list[EngineEvent] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return events

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def wait_all(self) -> None:
        """Test/shutdown helper: wait for in-flight tasks to settle."""
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
