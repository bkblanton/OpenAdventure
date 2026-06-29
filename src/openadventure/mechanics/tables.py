"""Random tables and a yes/no oracle. Pure and system-agnostic: a table is a
weighted (or dice-mapped) list of result strings, rolled with a supplied RNG so
results are seedable like dice.

Two table shapes, unified behind ``roll_on_table``:
- weighted: each entry has a ``weight`` (default 1); the pick is a weighted draw,
  equivalent to rolling a dN where N is the total weight, uniform when every
  weight is 1.
- dice-mapped: the table sets ``dice`` (e.g. "2d6", "1d100") and each entry a
  numeric [lo, hi] range; roll the dice and map the total to its range. This
  faithfully reproduces a published table's own (often non-uniform) odds."""

from __future__ import annotations

import random
from typing import Literal

from pydantic import BaseModel, Field

from openadventure.mechanics import dice


class TableError(ValueError):
    """Raised for malformed tables or rolls."""


class TableEntry(BaseModel):
    text: str  # the result
    weight: int = Field(default=1, ge=1)  # weighted mode: relative frequency
    lo: int | None = None  # dice-mapped mode: inclusive low end of the range
    hi: int | None = None  # dice-mapped mode: inclusive high end


class Table(BaseModel):
    name: str
    entries: list[TableEntry] = Field(min_length=1)
    dice: str | None = None  # e.g. "2d6"; with entry lo/hi, roll the dice and map

    def is_dice_mapped(self) -> bool:
        return bool(self.dice) and any(e.lo is not None and e.hi is not None for e in self.entries)


class TableRoll(BaseModel):
    table: str  # the table's name
    text: str  # the selected entry
    detail: str  # how it was rolled, e.g. "2d6 [3, 4] = 7" or "d8 -> 5"
    roll: int  # the numeric roll that selected the entry


def roll_on_table(table: Table, rng: random.Random) -> TableRoll:
    if not table.entries:
        raise TableError(f"table {table.name!r} has no entries")
    if table.is_dice_mapped():
        return _roll_dice_mapped(table, rng)
    return _roll_weighted(table, rng)


def _roll_dice_mapped(table: Table, rng: random.Random) -> TableRoll:
    assert table.dice is not None
    try:
        outcome = dice.roll(table.dice, rng)
    except dice.DiceError as exc:
        raise TableError(f"bad table dice {table.dice!r}: {exc}") from exc
    total = outcome.total
    ranged = [e for e in table.entries if e.lo is not None and e.hi is not None]
    for entry in ranged:
        if entry.lo <= total <= entry.hi:  # type: ignore[operator]
            return TableRoll(table=table.name, text=entry.text, detail=outcome.detail(), roll=total)
    # the dice landed outside every listed range, so clamp to the nearest entry
    nearest = min(ranged, key=lambda e: min(abs(total - e.lo), abs(total - e.hi)))  # type: ignore[arg-type]
    return TableRoll(
        table=table.name, text=nearest.text, detail=f"{outcome.detail()} (clamped)", roll=total
    )


def _roll_weighted(table: Table, rng: random.Random) -> TableRoll:
    total_weight = sum(e.weight for e in table.entries)
    pick = rng.randint(1, total_weight)
    cumulative = 0
    for entry in table.entries:
        cumulative += entry.weight
        if pick <= cumulative:
            return TableRoll(
                table=table.name, text=entry.text, detail=f"d{total_weight} -> {pick}", roll=pick
            )
    last = table.entries[-1]  # unreachable, but keeps the return total
    return TableRoll(
        table=table.name, text=last.text, detail=f"d{total_weight} -> {pick}", roll=pick
    )


# --- oracle -----------------------------------------------------------------

OracleOdds = Literal["certain", "likely", "even", "unlikely", "impossible"]

# percent chance the answer is "yes" for each odds label
_YES_THRESHOLD: dict[str, int] = {
    "certain": 95,
    "likely": 75,
    "even": 50,
    "unlikely": 25,
    "impossible": 5,
}


class OracleResult(BaseModel):
    question: str
    odds: str
    answer: Literal["yes", "no"]
    twist: str | None = None  # "and" (emphatic) or "but" (a complication)
    roll: int


def consult_oracle(question: str, odds: OracleOdds, rng: random.Random) -> OracleResult:
    """A Mythic-style yes/no draw: roll d100 under the odds' yes-threshold. A near-
    extreme roll adds an emphatic "and"; a doubles roll adds a complicating "but"."""
    threshold = _YES_THRESHOLD.get(odds, 50)
    roll = rng.randint(1, 100)
    answer: Literal["yes", "no"] = "yes" if roll <= threshold else "no"
    twist: str | None = None
    if roll <= 5 or roll >= 96:
        twist = "and"
    elif roll % 11 == 0:  # 11, 22, … 99
        twist = "but"
    return OracleResult(question=question, odds=odds, answer=answer, twist=twist, roll=roll)
