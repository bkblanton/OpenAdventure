"""A tiny, UI-agnostic progress channel for the ingest pipeline.

The heavy loops (PDF page extraction, window embedding) report their advance
through a ``ProgressFn`` callback so a large-PDF ingest can drive a progress bar
without the ingest code depending on rich or knowing whether a terminal is
attached. The CLI/REPL supply an adapter; tests and library callers pass
nothing and the reporting is a no-op."""

from __future__ import annotations

import contextlib
from collections.abc import Callable

# (phase, completed, total): ``completed`` of ``total`` units of the named phase
# are done. Called repeatedly for one phase as it advances; a new phase name
# starts a new bar. ``total`` stays constant across a phase's calls.
ProgressFn = Callable[[str, int, int], None]

PHASE_EXTRACT = "Extracting pages"
PHASE_INDEX = "Building search index"
PHASE_EMBED = "Embedding windows"


def report(progress: ProgressFn | None, phase: str, completed: int, total: int) -> None:
    """Forward a progress tick, swallowing any callback error so a UI glitch can
    never break an ingest. A no-op when ``progress`` is None."""
    if progress is None:
        return
    with contextlib.suppress(Exception):
        progress(phase, completed, total)
