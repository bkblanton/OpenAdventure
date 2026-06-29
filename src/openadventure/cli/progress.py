"""Rich progress-bar adapter for the ingest pipeline's progress callback.

Bridges the UI-agnostic :data:`~openadventure.ingest.progress.ProgressFn` the
pipeline reports through onto a live rich progress display, one bar per phase
(page extraction, index build, window embedding). Off a terminal (piped output,
tests) the bars disable themselves and the callback is a quiet no-op."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from openadventure.ingest.progress import ProgressFn


@contextlib.contextmanager
def ingest_progress(console: Console) -> Iterator[ProgressFn]:
    """Yield a ``ProgressFn`` driving a live progress bar, one bar per phase,
    each appearing the first time its phase reports. Bars are transient (they
    vanish on exit, leaving the surrounding Done/summary lines clean). On a
    non-terminal console the display is disabled and the callback does nothing,
    so piped logs stay tidy. The callback may be invoked from a worker thread
    (ingest runs under ``asyncio.to_thread`` in the REPL); rich's updates are
    lock-guarded, so that is safe."""
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
        disable=not console.is_terminal,
    )
    tasks: dict[str, int] = {}

    def report(phase: str, completed: int, total: int) -> None:
        task_id = tasks.get(phase)
        if task_id is None:
            task_id = progress.add_task(phase, total=total or None)
            tasks[phase] = task_id
        progress.update(task_id, completed=completed, total=total or None)

    with progress:
        yield report
