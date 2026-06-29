"""Timeline operations: undo recent turns, restart a campaign.

Pure campaign+log orchestration with no GameSession or provider dependency, so
the CLI can call these without constructing a session."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from openadventure.store import checkpoints
from openadventure.store.eventlog import EventLog
from openadventure.store.sheetstore import SheetStore
from openadventure.store.workspace import Campaign


class TimelineError(RuntimeError):
    """Raised when an undo/restart cannot be performed."""


@dataclass
class UndoReport:
    restored_seq: int
    turns_undone: int
    undone_texts: list[str] = field(default_factory=list)
    archive: Path | None = None


def undo_turns(campaign: Campaign, log: EventLog, n: int = 1) -> UndoReport:
    """Revert the last `n` AI turns: state files from the checkpoint taken
    before the target turn, log truncated to just before it (removed lines
    archived). Clamps to the deepest turn whose checkpoint still exists."""
    if n < 1:
        raise TimelineError("nothing to undo (n must be at least 1)")

    user_messages = [e for e in log.read_all() if e.type == "user_message"]
    if not user_messages:
        raise TimelineError("no turns to undo yet")

    # the checkpoint for a turn is keyed at user_message.seq - 1 (taken
    # immediately before the user_message was appended)
    candidates = list(reversed(user_messages))[: max(n, 1)]
    reachable = [e for e in candidates if checkpoints.has(campaign, e.seq - 1)]
    if not reachable:
        raise TimelineError(
            "no checkpoint available for that turn (checkpoints are kept for the "
            f"last {checkpoints.DEFAULT_KEEP} turns, and older campaigns predate undo)"
        )
    target = reachable[-1]
    turns_undone = candidates.index(target) + 1
    restored_seq = target.seq - 1
    undone_texts = [e.data.get("text", "") for e in candidates[:turns_undone]]

    # order matters for crash-safety: state first, then log, then marker
    checkpoints.restore(campaign, restored_seq)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive = campaign.archive_dir / f"undone-{timestamp}.jsonl"
    log.truncate_to(restored_seq, archive=archive)
    log.append(
        "undo",
        {"restored_to_seq": restored_seq, "turns": turns_undone, "archive": archive.name},
    )
    checkpoints.delete_after(campaign, restored_seq)
    return UndoReport(
        restored_seq=restored_seq,
        turns_undone=turns_undone,
        undone_texts=undone_texts,
        archive=archive,
    )


@dataclass
class RestartReport:
    archive_dir: Path
    pcs: list[str] = field(default_factory=list)
    rerolled: list[str] = field(default_factory=list)
    missing_originals: list[str] = field(default_factory=list)


_RESTART_MOVES = (
    "log.jsonl",
    "summary.json",
    "scene.json",
    "encounter.json",
    "npcs",
    "notes",
    "checkpoints",
)


def restart_campaign(
    campaign: Campaign, *, characters: Literal["original", "reroll"] = "original"
) -> RestartReport:
    """Start the story over. The old story is archived (never deleted);
    campaign.json, usage.json and docs/ are untouched.

    characters="original": each PC is restored to its as-created sheet, undoing
    every level, wound, and bit of loot (missing originals, e.g. PCs from
    before restart support, are instead rested to full and reported).
    characters="reroll": every PC sheet (and its saved original) is moved into
    the archive, leaving an empty party so fresh characters can be rolled."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive_dir = campaign.archive_dir / timestamp
    archive_dir.mkdir(parents=True, exist_ok=True)
    for name in _RESTART_MOVES:
        source = campaign.root / name
        if source.exists():
            shutil.move(str(source), str(archive_dir / name))
    campaign.npcs_dir.mkdir(parents=True, exist_ok=True)
    campaign.notes_dir.mkdir(parents=True, exist_ok=True)

    store = SheetStore(campaign)
    report = RestartReport(archive_dir=archive_dir)
    for sheet in store.list(kind="pc"):
        if characters == "reroll":
            _archive_pc(campaign, archive_dir, sheet.id)
            report.rerolled.append(sheet.id)
            continue
        original = store.load_original(sheet.id)
        if original is not None:
            store.save(original)
            report.pcs.append(original.id)
            continue
        # no original on file: rest the current sheet to a clean start instead
        report.missing_originals.append(sheet.id)
        for resource in sheet.resources.values():
            resource.current = resource.max
        sheet.conditions = []
        sheet.touch()
        store.save(sheet)
        report.pcs.append(sheet.id)
    return report


def _archive_pc(campaign: Campaign, archive_dir: Path, sheet_id: str) -> None:
    """Move a PC's current sheet and its as-created original into the archive,
    so a reroll wipes the party from play without ever destroying the records."""
    moves = (
        (campaign.characters_dir / f"{sheet_id}.json", archive_dir / "characters"),
        (campaign.originals_dir / f"{sheet_id}.json", archive_dir / "originals"),
    )
    for source, dest_dir in moves:
        if source.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest_dir / source.name))
