"""Progress clocks: countdowns for off-screen threats, time pressure, and
faction plans. Pure and system-agnostic: a clock is just ``filled``/``size``
segments and a trigger that fires when it fills.

Modeled on the "fronts"/"clock" device from Apocalypse World and Blades in the
Dark: a named track that advances as danger builds and resolves when full. The
board is rendered into the campaign context every turn so a ticking threat
never falls out of the GM's view the way free narration would."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ClockStatus = Literal["active", "filled", "cancelled"]


class ClockError(ValueError):
    """Raised for invalid clock operations."""


class Clock(BaseModel):
    id: str  # short stable handle used to advance it, e.g. "ritual"
    name: str  # what it measures, e.g. "The cult completes the ritual"
    size: int = Field(ge=1)  # total segments (commonly 4 = soon, 6 = a while, 8 = distant)
    filled: int = 0  # segments filled so far; kept within [0, size]
    trigger: str | None = None  # what happens in the world when it fills
    visible: bool = True  # True = the party can perceive this pressure
    status: ClockStatus = "active"

    def reconciled(self) -> Clock:
        """Copy with ``filled`` clamped and ``status`` derived from it.

        A cancelled clock stays cancelled; otherwise it is ``filled`` once every
        segment is shaded and ``active`` until then."""
        clock = self.model_copy(deep=True)
        clock.filled = max(0, min(clock.filled, clock.size))
        if clock.status != "cancelled":
            clock.status = "filled" if clock.filled >= clock.size else "active"
        return clock


class ClockBoard(BaseModel):
    clocks: list[Clock] = Field(default_factory=list)

    def find(self, clock_id: str) -> Clock:
        for clock in self.clocks:
            if clock.id.casefold() == clock_id.casefold():
                return clock
        have = ", ".join(c.id for c in self.live()) or "none"
        raise ClockError(f"no clock {clock_id!r} (have: {have})")

    def live(self) -> list[Clock]:
        """Clocks still in play (everything but cancelled ones)."""
        return [c for c in self.clocks if c.status != "cancelled"]

    def has(self, clock_id: str) -> bool:
        return any(c.id.casefold() == clock_id.casefold() for c in self.clocks)


def unique_id(base: str, board: ClockBoard) -> str:
    """A clock id not already taken on the board (numeric suffix on collision)."""
    candidate, n = base or "clock", 2
    while board.has(candidate):
        candidate = f"{base}-{n}"
        n += 1
    return candidate
