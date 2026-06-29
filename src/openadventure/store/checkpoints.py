"""Per-turn state checkpoints: the substrate for /undo.

A checkpoint is a copy of the campaign's mutable state taken just before an
AI turn, keyed by the log seq at that moment. The log itself and usage.json
are deliberately excluded (the log is handled by truncate_to; spent tokens
stay spent)."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from openadventure.store.workspace import Campaign

CHECKPOINT_DIRS = ("characters", "npcs", "notes")
CHECKPOINT_FILES = ("scene.json", "encounter.json", "clocks.json", "summary.json", "canon.json")
DEFAULT_KEEP = 30


def _checkpoint_path(campaign: Campaign, seq: int) -> Path:
    return campaign.checkpoints_dir / str(seq)


def save(campaign: Campaign, seq: int) -> Path:
    """Snapshot mutable state into checkpoints/<seq>/ (overwrites if present)."""
    dest = _checkpoint_path(campaign, seq)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in CHECKPOINT_DIRS:
        source = campaign.root / name
        if source.is_dir():
            shutil.copytree(source, dest / name)
    for name in CHECKPOINT_FILES:
        source = campaign.root / name
        if source.is_file():
            shutil.copy2(source, dest / name)
    return dest


def restore(campaign: Campaign, seq: int) -> None:
    """Restore state from checkpoints/<seq>/. Targets absent from the
    checkpoint are DELETED (e.g. NPCs or a summary created after it)."""
    source = _checkpoint_path(campaign, seq)
    if not source.is_dir():
        raise FileNotFoundError(f"no checkpoint for seq {seq}")
    for name in CHECKPOINT_DIRS:
        target = campaign.root / name
        if target.is_dir():
            shutil.rmtree(target)
        if (source / name).is_dir():
            shutil.copytree(source / name, target)
        else:
            target.mkdir(parents=True, exist_ok=True)
    for name in CHECKPOINT_FILES:
        target = campaign.root / name
        target.unlink(missing_ok=True)
        if (source / name).is_file():
            shutil.copy2(source / name, target)


def list_seqs(campaign: Campaign) -> list[int]:
    if not campaign.checkpoints_dir.is_dir():
        return []
    seqs = []
    for entry in campaign.checkpoints_dir.iterdir():
        if entry.is_dir() and entry.name.isdigit():
            seqs.append(int(entry.name))
    return sorted(seqs)


def has(campaign: Campaign, seq: int) -> bool:
    return _checkpoint_path(campaign, seq).is_dir()


def prune(campaign: Campaign, keep: int = DEFAULT_KEEP) -> None:
    for seq in list_seqs(campaign)[:-keep]:
        shutil.rmtree(_checkpoint_path(campaign, seq), ignore_errors=True)


def delete_after(campaign: Campaign, seq: int) -> None:
    """Drop checkpoints describing an erased future (key > seq)."""
    for existing in list_seqs(campaign):
        if existing > seq:
            shutil.rmtree(_checkpoint_path(campaign, existing), ignore_errors=True)
