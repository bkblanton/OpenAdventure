"""Sheet persistence: one JSON file per sheet, atomic writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openadventure.mechanics.sheets import Sheet
from openadventure.store import snapshots
from openadventure.store.workspace import slugify

if TYPE_CHECKING:
    from pathlib import Path

    from openadventure.store.workspace import Campaign


class SheetStore:
    def __init__(self, campaign: Campaign):
        self.campaign = campaign

    def _dir_for(self, kind: str) -> Path:
        return self.campaign.characters_dir if kind == "pc" else self.campaign.npcs_dir

    def _path(self, sheet_id: str) -> Path | None:
        for directory in (self.campaign.characters_dir, self.campaign.npcs_dir):
            candidate = directory / f"{sheet_id}.json"
            if candidate.is_file():
                return candidate
        return None

    def unique_id(self, name: str, kind: str) -> str:
        base = slugify(name)
        sheet_id = base
        n = 2
        while self._path(sheet_id) is not None:
            sheet_id = f"{base}-{n}"
            n += 1
        return sheet_id

    def save(self, sheet: Sheet) -> None:
        directory = self._dir_for(sheet.kind)
        directory.mkdir(parents=True, exist_ok=True)
        snapshots.save_json(directory / f"{sheet.id}.json", sheet)

    def load(self, sheet_id: str) -> Sheet | None:
        path = self._path(sheet_id)
        if path is None:
            return None
        return Sheet.model_validate(snapshots.load_json(path))

    def save_original(self, sheet: Sheet) -> None:
        """Keep a pristine as-created copy (used by campaign restart)."""
        path = self.campaign.originals_dir / f"{sheet.id}.json"
        snapshots.save_json(path, sheet)

    def load_original(self, sheet_id: str) -> Sheet | None:
        data = snapshots.load_json(self.campaign.originals_dir / f"{sheet_id}.json")
        if data is None:
            return None
        return Sheet.model_validate(data)

    def list(self, kind: str | None = None) -> list[Sheet]:
        sheets: list[Sheet] = []
        for directory in (self.campaign.characters_dir, self.campaign.npcs_dir):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                sheet = Sheet.model_validate(snapshots.load_json(path))
                if kind is None or sheet.kind == kind:
                    sheets.append(sheet)
        return sheets

    def party(self) -> list[Sheet]:
        return [s for s in self.list(kind="pc") if s.status == "active"]

    def companions(self) -> list[Sheet]:
        """Active NPCs marked as traveling with the party. Their briefs ride in
        context every turn regardless of scene, so a follower never drops when the
        party moves."""
        return [s for s in self.list(kind="npc") if s.companion and s.status == "active"]
