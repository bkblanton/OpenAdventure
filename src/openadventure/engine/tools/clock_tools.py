"""Clock tools: countdowns for off-screen threats and time pressure.

Clocks make the living world deterministic and always-in-view. A named track
fills as a danger advances and fires its trigger when full; the board is
rendered into the campaign context every turn, so the GM never loses track of a
ticking threat the way free narration would. Hidden clocks (``visible=False``)
advance behind the screen; in GM mode their movement is kept off the player's
transcript, like a secret note."""

from __future__ import annotations

from pydantic import BaseModel, Field

from openadventure.engine.events import StateChanged
from openadventure.engine.tools.registry import Tool, ToolContext, ToolOutcome
from openadventure.mechanics.clocks import Clock, ClockBoard, ClockError, unique_id
from openadventure.store import snapshots
from openadventure.store.workspace import slugify


def load_clocks(ctx: ToolContext) -> ClockBoard:
    data = snapshots.load_json(ctx.campaign.clocks_path)
    if data is None:
        return ClockBoard()
    return ClockBoard.model_validate(data)


def _bar(clock: Clock) -> str:
    filled = max(0, min(clock.filled, clock.size))
    return "●" * filled + "○" * (clock.size - filled)


def render_clock(clock: Clock) -> str:
    tag = ": FULL, its trigger fires" if clock.status == "filled" else ""
    hidden = "" if clock.visible else " (hidden, never reveal to the player)"
    trigger = f" → {clock.trigger}" if clock.trigger else ""
    return (
        f"- {clock.id}: {clock.name} [{_bar(clock)}] {clock.filled}/{clock.size}"
        f"{tag}{hidden}{trigger}"
    )


def render_clocks(board: ClockBoard) -> str:
    """The live board for the campaign context (GM-facing: shows hidden clocks)."""
    live = board.live()
    if not live:
        return ""
    return "\n".join(render_clock(c) for c in live)


def _save(ctx: ToolContext, board: ClockBoard, clock: Clock, summary: str) -> ToolOutcome:
    snapshots.save_json(ctx.campaign.clocks_path, board)
    ctx.log.append("state_change", {"kind": "clocks", "ref": clock.id, "summary": summary})
    # A hidden clock in GM mode must not surface its movement to the player.
    private = not clock.visible and ctx.meta.mode == "gm"
    public_summary = "a hidden clock advanced" if private else summary
    return ToolOutcome(
        content=render_clock(clock),
        events=[StateChanged(kind="clocks", ref=clock.id, summary=public_summary, private=private)],
        summary=summary,
        private=private,
        public_summary=public_summary,
    )


# --- create_clock -----------------------------------------------------------


class CreateClockArgs(BaseModel):
    name: str = Field(description="What the clock measures, e.g. 'The cult completes the ritual'")
    size: int = Field(
        ge=1, le=12, description="Total segments: 4 (soon), 6 (a while), or 8 (distant)"
    )
    trigger: str | None = Field(
        default=None, description="What happens in the world the moment it fills"
    )
    visible: bool = Field(
        default=True,
        description="True if the party can perceive this pressure; False for a hidden GM clock",
    )
    filled: int = Field(default=0, ge=0, description="Segments already shaded at creation")
    id: str | None = Field(
        default=None,
        description="Short handle to advance it later; defaults to a slug of the name",
    )


def _create_clock(ctx: ToolContext, args: CreateClockArgs) -> ToolOutcome:
    board = load_clocks(ctx)
    base = slugify(args.id) if args.id else "-".join(slugify(args.name).split("-")[:3])
    clock = Clock(
        id=unique_id(base or "clock", board),
        name=args.name,
        size=args.size,
        filled=args.filled,
        trigger=args.trigger,
        visible=args.visible,
    ).reconciled()
    board.clocks.append(clock)
    summary = f"clock started: {clock.name} ({clock.filled}/{clock.size})"
    return _save(ctx, board, clock, summary)


# --- advance_clock ----------------------------------------------------------


class AdvanceClockArgs(BaseModel):
    id: str = Field(description="The clock's id")
    delta: int = Field(default=1, description="Segments to fill; negative eases the threat back")
    reason: str | None = Field(default=None, description="Why it moved (for the log)")
    cancel: bool = Field(
        default=False, description="Remove the clock entirely (threat averted or resolved)"
    )


def _advance_clock(ctx: ToolContext, args: AdvanceClockArgs) -> ToolOutcome:
    board = load_clocks(ctx)
    try:
        clock = board.find(args.id)
    except ClockError as exc:
        return ToolOutcome(content=f"Error: {exc}", summary="no such clock", ok=False)
    if args.cancel:
        clock.status = "cancelled"
        summary = f"clock cancelled: {clock.name}"
        return _save(ctx, board, clock, summary)
    was_filled = clock.status == "filled"
    clock.filled += args.delta
    reconciled = clock.reconciled()
    clock.filled, clock.status = reconciled.filled, reconciled.status
    summary = f"clock {clock.name}: {clock.filled}/{clock.size}"
    if args.reason:
        summary += f" ({args.reason})"
    if clock.status == "filled" and not was_filled:
        summary += ": FULL"
    return _save(ctx, board, clock, summary)


# --- list_clocks ------------------------------------------------------------


class ListClocksArgs(BaseModel):
    pass


def _list_clocks(ctx: ToolContext, args: ListClocksArgs) -> ToolOutcome:
    board = load_clocks(ctx)
    rendered = render_clocks(board)
    if not rendered:
        return ToolOutcome(content="No active clocks.", summary="0 clocks")
    return ToolOutcome(content=rendered, summary=f"{len(board.live())} clock(s)")


CLOCK_TOOLS = [
    Tool(
        name="create_clock",
        description=(
            "Start a progress clock: a named countdown for an off-screen threat, a looming "
            "deadline, or a faction's plan (e.g. 'The cult completes the ritual', size 6). "
            "Make it visible when the party can sense the pressure; hidden for a secret "
            "threat. Advance it as time passes, the party dawdles, or a roll fails."
        ),
        args_model=CreateClockArgs,
        handler=_create_clock,
    ),
    Tool(
        name="advance_clock",
        description=(
            "Move a clock forward (or back with a negative delta), or cancel it when the "
            "threat is averted or resolved. When a clock fills, narrate its trigger firing "
            "in the world. Clocks persist and show in the campaign context every turn."
        ),
        args_model=AdvanceClockArgs,
        handler=_advance_clock,
    ),
    Tool(
        name="list_clocks",
        description="List the active progress clocks and how full each one is.",
        args_model=ListClocksArgs,
        handler=_list_clocks,
        read_only=True,
    ),
]
