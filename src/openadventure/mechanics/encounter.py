"""Encounter/initiative tracking. Pure, system-agnostic: initiative is just a
sortable number, so any system's turn order works."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EncounterError(ValueError):
    """Raised for invalid encounter operations."""


class Combatant(BaseModel):
    tag: str  # unique display name within the encounter, e.g. "Goblin 2"
    sheet_id: str | None = None
    side: str = "foe"  # "party" | "ally" | "foe" | free-form
    initiative: float = 0
    active: bool = True  # False = defeated/fled; stays for the record


class Encounter(BaseModel):
    name: str
    status: Literal["active", "ended"] = "active"
    round: int = 1
    turn_index: int = 0
    combatants: list[Combatant] = Field(default_factory=list)

    def find(self, tag: str) -> Combatant:
        for combatant in self.combatants:
            if combatant.tag.casefold() == tag.casefold():
                return combatant
        raise EncounterError(
            f"no combatant {tag!r} (have: {', '.join(c.tag for c in self.combatants)})"
        )

    def current(self) -> Combatant | None:
        if not self.combatants or self.status != "active":
            return None
        return self.combatants[self.turn_index % len(self.combatants)]


def sort_initiative(encounter: Encounter) -> Encounter:
    """Descending initiative; ties keep insertion order. Resets the turn pointer
    to the first active combatant."""
    enc = encounter.model_copy(deep=True)
    enc.combatants.sort(key=lambda c: -c.initiative)
    enc.turn_index = 0
    skipped = 0
    while enc.combatants and not enc.combatants[enc.turn_index].active:
        enc.turn_index += 1
        skipped += 1
        if skipped >= len(enc.combatants):
            break
    return enc


def next_turn(encounter: Encounter) -> tuple[Encounter, Combatant | None]:
    """Advance to the next active combatant, wrapping into a new round."""
    enc = encounter.model_copy(deep=True)
    if not enc.combatants or all(not c.active for c in enc.combatants):
        return enc, None
    n = len(enc.combatants)
    index = enc.turn_index
    for _ in range(n):
        index += 1
        if index >= n:
            index = 0
            enc.round += 1
        if enc.combatants[index].active:
            enc.turn_index = index
            return enc, enc.combatants[index]
    return enc, None
