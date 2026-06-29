"""One-off campaign-log migrations.

``backfill_tool_content`` re-derives the result content for the replay-eligible
corpus-retrieval tool calls (search_campaign / read_campaign / search_rules /
read_rules) on existing logs, so they can be replayed as structured tool_result
blocks (see ``engine/context.py``). Logs written before replay shipped only
stored a one-line ``result_summary``, not the body the model saw. This backfills
``content`` by re-running each retrieval against the campaign's ingested store:
exact for the ``read_*`` calls (path-addressed, stable) and best-effort for the
``search_*`` calls (the index may have changed since). It is idempotent, an entry
that already has ``content`` is left untouched, so the renderer needs no
graceful-degrade branch for pre-migration logs.
"""

from __future__ import annotations

import json
import os
import random
from typing import TYPE_CHECKING

from openadventure.engine.context import CORPUS_REPLAY_TOOLS, REPLAY_CONTENT_CHARS

if TYPE_CHECKING:
    from pathlib import Path

    from openadventure.store.eventlog import LogEntry
    from openadventure.store.workspace import Campaign, Workspace


def _cap(content: str) -> str:
    """Apply the same per-entry cap the live logging path uses."""
    if len(content) > REPLAY_CONTENT_CHARS:
        return content[:REPLAY_CONTENT_CHARS] + "\n[…truncated; search/read again for the rest]"
    return content


def _rewrite(path: Path, entries: list[LogEntry]) -> None:
    """Atomically rewrite the log file from ``entries`` (durable temp + replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def backfill_tool_content(workspace: Workspace, campaign: Campaign) -> int:
    """Backfill ``content`` on replay-eligible tool_call entries that lack it, by
    re-running the retrieval against the ingested store. Rewrites the log in place
    and returns the number of entries updated (0 leaves the file untouched).

    Best-effort: a call whose section no longer resolves (renamed/removed) is left
    content-less, which the renderer simply does not replay."""
    from openadventure.engine.tools import build_registry
    from openadventure.engine.tools.registry import ToolContext

    log = campaign.open_log()
    entries = log.read_all()
    meta = campaign.load_meta()
    registry = build_registry(workspace, campaign, meta)
    ctx = ToolContext(
        workspace=workspace, campaign=campaign, meta=meta, log=log, rng=random.Random(0)
    )

    updated = 0
    for entry in entries:
        data = entry.data
        if entry.type != "tool_call":
            continue
        name = data.get("name")
        # Only the corpus reads are re-derivable: re-running a lookup (get_sheet,
        # search_canon) now would stamp current state onto a historical turn, so
        # those get content only from live play going forward, never backfilled.
        if name not in CORPUS_REPLAY_TOOLS or not data.get("ok", True) or data.get("content"):
            continue
        if name not in registry:
            continue  # the source/module is no longer attached; nothing to re-derive
        outcome = registry.dispatch(ctx, name, data.get("args") or {})
        if outcome.ok and outcome.content:
            data["content"] = _cap(outcome.content)
            updated += 1

    if updated:
        _rewrite(log.path, entries)
        log.refresh()
    return updated
