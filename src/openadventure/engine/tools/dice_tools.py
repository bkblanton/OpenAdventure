"""The dice tool: same engine as /roll, exposed to the AI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from openadventure.engine.events import RollResult
from openadventure.engine.tools.registry import Tool, ToolContext, ToolOutcome
from openadventure.mechanics import dice


class RollDiceArgs(BaseModel):
    expression: str = Field(
        description=(
            "Complete dice expression with all known modifiers already included, "
            "e.g. '1d20+5', '4d6kh3', '2d20kh1'. Supports '*' and parentheses; '*' "
            "binds tighter than +/-, so parenthesise sums you mean to multiply, "
            "e.g. '(2d6+6)*5' or '3d6*5' for Call of Cthulhu characteristics."
        )
    )
    reason: str | None = Field(
        default=None,
        description=(
            "What the roll is for, shown to the player verbatim on the dice line, e.g. "
            "'Kasimir attack vs goblin'. Name only the actor and the skill/action; never "
            "what success would reveal. For a check to notice, realize, or sense something "
            "hidden, the thing being checked for is itself a secret: write 'Kragthor, "
            "Intelligence check', not '… to realize the skull pattern', or a failed roll "
            "spoils the secret it was meant to guard. (Use private: true if even an open "
            "roll would tip the player off.)"
        ),
    )
    private: bool = Field(
        default=False,
        description="GM-only roll: the player sees that a roll happened but not the result",
    )
    # --- optional check: let the engine decide success instead of you eyeballing it ---
    target: int | None = Field(
        default=None,
        description=(
            "Resolve this as a check against this number, and the engine decides the "
            "outcome. The skill value for a roll-under system (CoC: the skill, e.g. 65), "
            "or the DC for a roll-over system. Omit for a bare roll (damage, initiative)."
        ),
    )
    success_when: Literal["<=", ">="] | None = Field(
        default=None,
        description=(
            "Required with target. '<=' for roll-under (succeed at or below target, e.g. "
            "Call of Cthulhu); '>=' for roll-over (succeed at or above, e.g. d20 vs a DC). "
            "Both are inclusive."
        ),
    )
    tiers: dict[str, int] | None = Field(
        default=None,
        description=(
            "Optional named degrees of success beyond a plain pass, each a boundary "
            "compared the same way as target; the most extreme one reached wins. CoC "
            'example: {"hard": 32, "extreme": 13} for a skill of 65.'
        ),
    )


def _roll_dice(ctx: ToolContext, args: RollDiceArgs) -> ToolOutcome:
    try:
        outcome = dice.roll(args.expression, ctx.rng)
    except dice.DiceError as exc:
        return ToolOutcome(content=f"Error: {exc}", summary="bad expression", ok=False)
    detail = outcome.detail()

    wants_check = any(v is not None for v in (args.target, args.success_when, args.tiers))
    check = None
    if wants_check:
        if args.target is None or args.success_when is None:
            return ToolOutcome(
                content=(
                    "Error: to resolve a check, pass both target and success_when "
                    "('<=' for roll-under, '>=' for roll-over)."
                ),
                summary="incomplete check",
                ok=False,
            )
        check = dice.evaluate_check(
            outcome.total,
            target=args.target,
            success_when=args.success_when,
            tiers=args.tiers,
        )

    max_rolls, min_rolls = outcome.max_rolls, outcome.min_rolls
    log_data = {
        "expression": outcome.expression,
        "total": outcome.total,
        "detail": detail,
        "reason": args.reason,
        "private": args.private,
        "max_rolls": max_rolls,
        "min_rolls": min_rolls,
        "by": "gm",
    }
    if check is not None:
        log_data["outcome"] = check.label
        log_data["target"] = check.target
        log_data["success_when"] = check.comparator
    ctx.log.append("roll", log_data)

    content = detail
    summary = f"{outcome.expression} -> {outcome.total}"
    if check is not None:
        content = f"{detail} → {check.label} (target {check.comparator} {check.target})"
        summary += f" ({check.label})"
    if max_rolls or min_rolls:
        content += f"  (max_rolls={max_rolls}, min_rolls={min_rolls})"

    event = RollResult(
        expression=outcome.expression,
        total=outcome.total,
        detail=detail,
        reason=args.reason,
        private=args.private,
        outcome=check.label if check is not None else None,
        max_rolls=max_rolls,
        min_rolls=min_rolls,
    )
    if args.private and ctx.meta.mode == "gm":
        event = RollResult(
            expression=outcome.expression,
            total=0,
            detail="",
            reason=None,
            private=True,
        )
    return ToolOutcome(
        content=content,
        events=[event],
        summary=summary,
        private=args.private,
        public_summary="secret roll",
        public_args_summary="private=true",
    )


ROLL_DICE = Tool(
    name="roll_dice",
    description=(
        "Roll dice using a dice expression. Supports NdS, +/- modifiers, * with "
        "parentheses (e.g. (2d6+6)*5 for Call of Cthulhu characteristics), kh/kl/dh/dl "
        "(keep/drop, e.g. 2d20kh1 for advantage, 4d6kh3 for stats), rN (reroll results "
        "of N or lower once), ! (exploding), and d% (percentile). Before calling it, check "
        "sheets, active conditions, resources, proficiency, advantage/disadvantage, "
        "situational bonuses or penalties, and relevant rules; include every known modifier "
        "in the expression instead of rolling a bare die. Always use this tool "
        "for any randomness; never invent dice results. For a skill check or save, also "
        "pass target + success_when (and optional tiers) so the engine decides success "
        "instead of you judging it; omit them for a bare roll like damage. Every result "
        "reports max_rolls/min_rolls (dice on their highest face / on 1) for crits and fumbles."
    ),
    args_model=RollDiceArgs,
    handler=_roll_dice,
)
