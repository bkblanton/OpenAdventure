"""Random-table and oracle tools.

roll_table rolls an inline one-off table (weighted or dice-mapped); oracle is a
yes/no draw with odds. Both are pure mechanics (no game-system assumptions) and
support private rolls behind the GM screen."""

from __future__ import annotations

from pydantic import BaseModel, Field

from openadventure.engine.tools.registry import Tool, ToolContext, ToolOutcome
from openadventure.mechanics.tables import (
    OracleOdds,
    Table,
    TableEntry,
    TableError,
    consult_oracle,
    roll_on_table,
)


class InlineEntry(BaseModel):
    text: str = Field(description="The result text for this row")
    weight: int = Field(default=1, ge=1, description="Relative frequency (weighted tables)")
    lo: int | None = Field(default=None, description="Inclusive low end (dice-mapped tables)")
    hi: int | None = Field(default=None, description="Inclusive high end (dice-mapped tables)")


def _to_entries(entries: list[InlineEntry]) -> list[TableEntry]:
    return [TableEntry(text=e.text, weight=e.weight, lo=e.lo, hi=e.hi) for e in entries]


# --- roll_table -------------------------------------------------------------


class RollTableArgs(BaseModel):
    entries: list[InlineEntry] = Field(min_length=1, description="The table's rows")
    dice: str | None = Field(
        default=None, description="For a dice-mapped table, e.g. '2d6' with entry lo/hi"
    )
    name: str | None = Field(default=None, description="Label for the table")
    count: int = Field(default=1, ge=1, le=20, description="Roll this many times")
    reason: str | None = Field(default=None, description="What the roll is for")
    private: bool = Field(
        default=False, description="GM-only: the player sees a roll happened but not the result"
    )


def _roll_table(ctx: ToolContext, args: RollTableArgs) -> ToolOutcome:
    try:
        table = Table(
            name=args.name or "inline table", dice=args.dice, entries=_to_entries(args.entries)
        )
    except ValueError as exc:
        return ToolOutcome(content=f"Error: {exc}", summary="bad table", ok=False)

    try:
        rolls = [roll_on_table(table, ctx.rng) for _ in range(args.count)]
    except TableError as exc:
        return ToolOutcome(content=f"Error: {exc}", summary="bad roll", ok=False)

    content = f"{table.name}\n" + "\n".join(f"{r.detail}: {r.text}" for r in rolls)
    summary = f"{table.name}: " + "; ".join(r.text[:40] for r in rolls)
    ctx.log.append(
        "table_roll",
        {
            "table": table.name,
            "results": [r.text for r in rolls],
            "reason": args.reason,
            "private": args.private,
        },
    )
    if args.private and ctx.meta.mode == "gm":
        return ToolOutcome(
            content=content,
            summary=summary,
            private=True,
            public_summary="secret table roll",
            public_args_summary="private=true",
        )
    return ToolOutcome(content=content, summary=summary)


# --- oracle -----------------------------------------------------------------


class OracleArgs(BaseModel):
    question: str = Field(description="The yes/no question you're putting to the dice")
    odds: OracleOdds = Field(
        default="even",
        description="How likely 'yes' is: certain / likely / even / unlikely / impossible",
    )
    private: bool = Field(default=False, description="GM-only consult")


def _oracle(ctx: ToolContext, args: OracleArgs) -> ToolOutcome:
    result = consult_oracle(args.question, args.odds, ctx.rng)
    twist = f", {result.twist}" if result.twist else ""
    content = f"{result.answer}{twist} (rolled {result.roll} vs {args.odds})"
    summary = f"oracle [{args.odds}]: {result.answer}{twist}"
    ctx.log.append(
        "oracle",
        {
            "question": args.question,
            "odds": args.odds,
            "answer": result.answer,
            "twist": result.twist,
            "private": args.private,
        },
    )
    if args.private and ctx.meta.mode == "gm":
        return ToolOutcome(
            content=content,
            summary=summary,
            private=True,
            public_summary="secret oracle",
            public_args_summary="private=true",
        )
    return ToolOutcome(content=content, summary=summary)


TABLE_TOOLS = [
    Tool(
        name="roll_table",
        description=(
            "Roll on a random table instead of deciding an outcome yourself: wandering "
            "encounters, loot, rumors, complications, weather. Pass an inline `entries` list. "
            "Weighted tables use per-entry `weight`; to reproduce a published table faithfully, "
            "set `dice` (e.g. '2d6') and give each entry an [lo, hi] range. Use `private` for "
            "results the player shouldn't see."
        ),
        args_model=RollTableArgs,
        handler=_roll_table,
    ),
    Tool(
        name="oracle",
        description=(
            "Put an open yes/no question about the world to the dice rather than deciding it: "
            "set the odds of 'yes' (certain/likely/even/unlikely/impossible). A strong roll may "
            "add 'and' (emphatic) or 'but' (a complication). Use for improvised facts the module "
            "and your notes don't settle. Use `private` for a hidden consult."
        ),
        args_model=OracleArgs,
        handler=_oracle,
    ),
]
