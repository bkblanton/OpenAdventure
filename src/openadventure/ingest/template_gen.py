"""Derive a character-sheet template from an ingested source.

A one-shot agent reads the source's character-creation material (via the same
search/read tools the GM uses) and emits a template JSON: the system-agnostic
path for running non-5e games."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from openadventure.engine.tools.registry import Tool, ToolOutcome, ToolRegistry
from openadventure.engine.tools.rules_tools import make_rules_tools
from openadventure.providers.base import (
    GenerationSettings,
    Message,
    Provider,
    ProviderError,
    RedactedThinkingBlock,
    SystemBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openadventure.store import snapshots

MAX_ROUNDS = 16

TEMPLATE_SYSTEM = """\
You are deriving a character-sheet template for a tabletop RPG harness from an
ingested rulebook. Research the rules with search_rules, read_rules, and
outline_rules. outline_rules lists the book's sections in reading order (its
table of contents), so you can locate the right chapters even without the exact
keyword, and narrow to one area with 'under'; read_rules opens a section and ends
with pointers to the previous/next section, so you can read a chapter straight
through. Find: the character creation chapter, core attributes, derived
statistics, resources (hit points, spell slots, sanity, stress, whatever this
system tracks), the creation procedure, and how characters advance after creation
(experience, levels, milestones, whatever this system uses to grow a character).

Then call save_template exactly once with:
- fields: list of {path, type, description, example} where path starts with
  "fields." (nested allowed, e.g. fields.abilities.str). Cover everything a
  filled sheet needs.
- resources: list of {name, description} for numeric pools with current/max.
- creation_guide: a numbered markdown checklist a game master can follow to
  create a starting character with a player, including which dice to roll. This
  is the priority; get it right first.
- advancement_guide: a SHORT numbered markdown checklist for the general
  procedure to advance a character one step (gain a level, spend experience,
  hit a milestone) and to build one above starting power. Describe the
  repeatable steps and which dice to roll, and point to where the per-option
  specifics live (e.g. "look up this level's class features in the rules") for
  the GM to search at the table. Do NOT enumerate every class/level/option;
  that is searched live during play. If this system has no advancement, pass an
  empty string.

Ground every entry in what you actually read. Do not assume D&D conventions
unless this source uses them."""


class SaveTemplateArgs(BaseModel):
    fields: list[dict[str, Any]] = Field(description="Sheet field specs")
    resources: list[dict[str, Any]] = Field(description="Resource pool specs")
    creation_guide: str = Field(description="Markdown creation checklist")
    advancement_guide: str = Field(
        default="", description="Markdown advancement/leveling checklist ('' if none)"
    )


def _condense(text: str, limit: int = 140) -> str:
    """Flatten whitespace and truncate so a thinking block fits one log line."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _live_sentence(text: str, limit: int = 140) -> str:
    """The sentence currently being written, shown from its start so the line
    reads forward as new sentences stream in (rather than scrolling mid-word as a
    raw tail would). A too-long sentence is truncated at the end, keeping its
    opening."""
    flat = " ".join(text.split())
    parts = [p for p in _SENTENCE_SPLIT.split(flat) if p.strip()]
    sentence = parts[-1] if parts else flat
    return sentence if len(sentence) <= limit else sentence[:limit].rstrip() + "…"


def _format_tool_call(name: str, args: dict[str, Any]) -> str:
    """A short tag for one tool call, terse enough that several can sit together
    on a single progress line (a round fires them in parallel)."""
    if name == "search_rules":
        return f"search {args.get('query', '')!r}"
    if name == "read_rules":
        # Just the leaf filename; the full section path is too long to stack.
        return f"read {args.get('section_path', '').rsplit('/', 1)[-1]}"
    if name == "outline_rules":
        under = args.get("under") or ""
        return f"outline {under!r}" if under else "outline"
    if name == "save_template":
        return "writing the template"
    return name


async def derive_template(
    provider: Provider,
    settings: GenerationSettings,
    source_dir: Path,
    source_name: str,
    on_progress: Callable[[str], None] | None = None,
) -> dict | None:
    """Run the derivation agent; writes templates/character.json and returns it.

    ``on_progress``, if given, is called with one short line per piece of agent
    activity (a thinking snippet, a tool call, or a tool result). Each line is
    meant to replace the previous one (e.g. a live spinner's status) so a long
    run shows it is really working without scrolling its full reasoning."""
    report = on_progress or (lambda _message: None)
    saved: dict | None = None

    def save_template(ctx, args: SaveTemplateArgs) -> ToolOutcome:
        nonlocal saved
        saved = {
            "name": f"{source_name}/character",
            "version": 2,
            "fields": args.fields,
            "resources": args.resources,
            "creation_guide": args.creation_guide,
            "advancement_guide": args.advancement_guide,
        }
        return ToolOutcome(content="Template saved.", summary="template saved")

    registry = ToolRegistry()
    for tool in make_rules_tools([source_dir]):
        registry.register(tool)
    registry.register(
        Tool(
            name="save_template",
            description="Save the finished character template (call exactly once).",
            args_model=SaveTemplateArgs,
            handler=save_template,
        )
    )
    ctx = None  # rules tools and save_template ignore the context
    # The agent researches over rounds and the host stops it at MAX_ROUNDS; tell
    # it the budget so it converges and saves in time instead of researching
    # until it is cut off (which would discard the whole run).
    budget_note = (
        f"\n\nYou research over a series of rounds, up to {MAX_ROUNDS} total, and the "
        "budget is counted in rounds, not calls. Each round, issue ALL the searches and "
        "reads you can in parallel, then receive the results together; a single round "
        "that fans out many calls costs the same as one that makes a single call, so "
        "never walk the rules one call per round. Gather what you need in the first "
        "several rounds, then call save_template; you do not need to read everything. "
        "You must call save_template before the rounds run out. A saved, partial "
        "template is far better than none."
    )
    system = [SystemBlock(text=TEMPLATE_SYSTEM + budget_note)]
    convo: list[Message] = [
        Message(
            role="user",
            content=[
                TextBlock(
                    text=f"Derive the character template for the source {source_name!r}. "
                    "Start by searching for character creation."
                )
            ],
        )
    ]

    for round_index in range(MAX_ROUNDS):
        # The bulk of a round is the model thinking before it emits any tool
        # calls, so we stream that reasoning live to keep the line moving, then
        # switch to the round's calls once they arrive. A round fans out several
        # search/read calls at once, so the calls accumulate onto one line
        # ("Round 3/16: search 'x' · read y.md") instead of flashing one per call.
        round_label = f"Round {round_index + 1}/{MAX_ROUNDS}"
        report(f"{round_label}: Reading the rules…")
        text_acc = ""
        think_acc = ""
        thinking: list[ThinkingBlock | RedactedThinkingBlock] = []
        tool_uses = []
        calls: list[str] = []
        stop = None
        try:
            async for event in provider.stream_turn(
                system=system, messages=convo, tools=registry.defs(), settings=settings
            ):
                match event.type:
                    case "text_delta":
                        text_acc += event.text
                    case "thinking_delta":
                        # Fill the long pre-tool-call wait with live reasoning,
                        # rolling forward one sentence at a time.
                        if not calls:
                            think_acc += event.thinking
                            report(f"{round_label}: {_live_sentence(think_acc, 120)}")
                    case "thinking":
                        thinking.append(
                            ThinkingBlock(thinking=event.thinking, signature=event.signature)
                        )
                    case "redacted_thinking":
                        thinking.append(RedactedThinkingBlock(data=event.data))
                    case "tool_use":
                        tool_uses.append(event)
                        calls.append(_format_tool_call(event.name, event.input))
                        report(_condense(f"{round_label}: {' · '.join(calls)}"))
                    case "turn_done":
                        stop = event
        except ProviderError:
            raise
        if saved is not None:
            break
        if stop is None or stop.stop_reason != "tool_use" or not tool_uses:
            break
        # Thinking blocks come first so the API can verify reasoning continuity
        # when we send tool results back with thinking enabled.
        assistant_content: list[
            ThinkingBlock | RedactedThinkingBlock | TextBlock | ToolUseBlock
        ] = [*thinking]
        if text_acc:
            assistant_content.append(TextBlock(text=text_acc))
        assistant_content.extend(
            ToolUseBlock(id=tu.id, name=tu.name, input=tu.input) for tu in tool_uses
        )
        convo.append(Message(role="assistant", content=assistant_content))
        results: list[ToolResultBlock | TextBlock] = []
        for tu in tool_uses:
            outcome = registry.dispatch(ctx, tu.name, tu.input)
            results.append(
                ToolResultBlock(tool_use_id=tu.id, content=outcome.content, is_error=not outcome.ok)
            )
        # ``remaining`` counts the rounds still available, including the next one.
        # Nudge the agent to converge as the budget runs low, and demand a save
        # on the final round so a long run is never discarded unsaved.
        remaining = MAX_ROUNDS - (round_index + 1)
        if remaining == 1:
            results.append(
                TextBlock(
                    text="This is your final research round. Call save_template now with "
                    "whatever you have gathered; you will not get another round."
                )
            )
        elif remaining <= 3:
            results.append(
                TextBlock(
                    text=f"Only {remaining} research rounds remain. Start consolidating and "
                    "call save_template before they run out."
                )
            )
        convo.append(Message(role="user", content=results))

    if saved is not None:
        snapshots.save_json(source_dir / "templates" / "character.json", saved)
    return saved
