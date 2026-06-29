"""Log compaction: the canon chronicler. When the verbatim log fills, the oldest
uncompacted span is read once by a careful "chronicler" pass that (1) patches the
structured canon with the durable facts in that span and (2) refreshes a short
prose summary, before the span scrolls out of verbatim memory.

The log itself is never modified. canon.json holds the structured facts and
summary.json holds the summary plus through_seq, which records how far the
chronicler has processed. through_seq is written LAST (after canon), so an
interrupted pass is reprocessed from the same point next time; because canon ops
are idempotent by id, that reprocessing converges instead of duplicating."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from openadventure.engine.context import ContextBudget, est_tokens, uncompacted_span_tokens
from openadventure.engine.events import (
    CompactionFinished,
    CompactionProgress,
    CompactionStarted,
    EngineError,
    EngineEvent,
)
from openadventure.engine.tools.registry import Tool, ToolOutcome, ToolRegistry
from openadventure.providers.base import (
    GenerationSettings,
    Message,
    Provider,
    ProviderError,
    SystemBlock,
    TextBlock,
    Usage,
)
from openadventure.store import canon, snapshots
from openadventure.store.eventlog import LogEntry

if TYPE_CHECKING:
    from openadventure.engine.session import GameSession

TRIGGER_FRACTION = 0.8  # compact when the uncompacted span exceeds this share of the tail budget
SPAN_FRACTION = 0.6  # how much of the uncompacted span one pass summarizes
SUMMARY_WORD_LIMIT = 1200  # the summary is now short: canon carries the facts

CANON_SYSTEM = """\
You are the canon chronicler for an ongoing tabletop RPG campaign. You maintain
the campaign's durable memory so the Game Master stays consistent over a long
game.

You are given three things: the current CANON (open entries, each tagged with an
[id]), the running SUMMARY (short prose), and a TRANSCRIPT of new play that is
about to scroll out of the GM's verbatim memory. Capture what matters from the
transcript before it is gone.

Call record_canon exactly once with two things:

1. ops: patches to the canon. Each op is one of:
   - add: a new entry. Give a short kebab-case id, a category, and a one-line
     text. Categories:
       threads  open questions, quests, mysteries, unresolved plot
       seeds    planted foreshadowing or setups not yet paid off
       promises debts, oaths, deals the party or an NPC owes
       rulings  a table ruling to apply consistently later
       world    a durable fact about the setting
   - update: change an existing entry by its [id] (a thread gains detail, a
     new fact via facts_add, a promise's terms change).
   - resolve: close an entry by its [id] when it is done, setting status to one
     of resolved, paid, lost, or dropped.

   Open threads, seeds, and promises are shown to the GM every turn, so keep the
   open set lean: resolve what the transcript settled, update an existing entry
   rather than add a near-duplicate, and do not open trivial items. Be concrete
   with names and numbers. Do not write a person's profile into canon: who a PC
   or NPC is, their attitude, goal, bond, or stats live on their character sheet,
   so never add a canon entry that just describes someone. A plot or world fact
   that happens to involve a person still belongs in canon under its own
   category: a debt they owe is a promise, a mystery about them is a thread, and
   a durable truth like their hidden allegiance is a world fact (keyed by name,
   and use facts_add to accumulate further facts on it).
   More broadly, do not restate anything the GM already sees in context every
   turn: the party roster (stats, conditions, carried items), the current scene
   (location, time, visible exits), the clocks (countdowns), or module text you
   can look up. Canon is for what those do not carry. Items a character carries
   live on their sheet, so there is no items category: record an item the party
   SEEKS or has LOST as a thread (the objective), and a notable item out in the
   world as a seed or world fact.

   Set visibility "hidden" for GM-only facts the players do not yet know. Set
   priority "major" only for the spine of the campaign, so it is never dropped.

2. summary: rewrite the running summary, folding in the new events, as tight
   markdown prose under {limit} words. Merge into ONE seamless summary; do not
   label what came from where. Do not restate facts you captured as canon ops
   (canon carries the facts; the summary carries tone and narrative flow). Drop
   blow-by-blow combat from concluded fights and small talk."""


class CanonOpArg(BaseModel):
    op: str = Field(description="add | update | resolve")
    id: str = Field(description="short kebab-case id; for update/resolve, an existing [id]")
    category: str | None = Field(
        default=None, description="for add: threads|seeds|promises|rulings|world"
    )
    text: str | None = Field(default=None, description="one-line entry text")
    facts_add: list[str] = Field(
        default_factory=list, description="discrete facts to append to an entry"
    )
    status: str | None = Field(default=None, description="for resolve: resolved|paid|lost|dropped")
    visibility: str | None = Field(default=None, description="'hidden' for GM-only facts")
    priority: str | None = Field(default=None, description="'major' for the campaign spine")


class RecordCanonArgs(BaseModel):
    ops: list[CanonOpArg] = Field(default_factory=list, description="canon patches to apply")
    summary: str = Field(description="the refreshed short summary, as markdown prose")


def active_encounter_start_seq(entries: list[LogEntry]) -> int | None:
    """Seq where the in-progress encounter began, or None if no fight is open.
    Only one encounter can be active at a time, so the most recent
    "encounter started" with no later "encounter ended" marks the live fight.
    Derived from the log so compaction protects exactly the entries it would
    otherwise fold away."""
    start: int | None = None
    for entry in entries:
        if entry.type != "state_change" or entry.data.get("kind") != "encounter":
            continue
        summary = entry.data.get("summary", "")
        if summary.startswith("encounter started:"):
            start = entry.seq
        elif "encounter ended:" in summary:
            start = None
    return start


def select_span(
    entries: list[LogEntry], through_seq: int, *, protect_after_seq: int | None = None
) -> int | None:
    """Choose the new through_seq: ~60% of the uncompacted narrative entries,
    extended to end on a gm_message (turn boundary). If protect_after_seq is set
    (an in-progress encounter's start), never advance into it; the whole live
    fight stays verbatim in the tail. None = nothing to compact."""
    narrative = [
        e
        for e in entries
        if e.seq > through_seq
        and e.type in ("user_message", "gm_message", "roll", "tool_call", "state_change")
    ]
    if len(narrative) < 4:
        return None
    cut = max(1, int(len(narrative) * SPAN_FRACTION)) - 1
    # extend forward to the next gm_message so we cut between turns
    while cut < len(narrative) and narrative[cut].type != "gm_message":
        cut += 1
    if cut >= len(narrative):
        cut = len(narrative) - 1
    new_through = narrative[cut].seq
    if protect_after_seq is not None and new_through >= protect_after_seq:
        # the cut lands inside an active encounter: pull it back to the last
        # turn boundary before the fight began, or skip compaction this turn.
        before = [e.seq for e in narrative if e.type == "gm_message" and e.seq < protect_after_seq]
        if not before:
            return None
        new_through = max(before)
    return new_through


def render_span(entries: list[LogEntry], through_seq: int, new_through: int) -> str:
    lines: list[str] = []
    for entry in entries:
        if not (through_seq < entry.seq <= new_through):
            continue
        data = entry.data
        match entry.type:
            case "user_message":
                lines.append(f"PLAYER: {data.get('text', '')}")
            case "gm_message":
                lines.append(f"GM: {data.get('text', '')}")
            case "roll":
                reason = f" ({data['reason']})" if data.get("reason") else ""
                lines.append(f"[roll {data.get('expression')} = {data.get('total')}{reason}]")
            case "state_change":
                if data.get("summary"):
                    lines.append(f"[{data['summary']}]")
    return "\n".join(lines)


def should_compact(session: GameSession) -> bool:
    budget = ContextBudget.from_settings(
        session.settings, session.models.get(session.settings.model)
    )
    # Tail room from the real non-tail prompt (system + context block + tools), not an
    # estimate: compact once the uncompacted span fills most of what's actually left.
    tail = budget.tail_for(session.non_tail_tokens())
    summary = snapshots.load_json(session.campaign.summary_path) or {}
    through_seq = int(summary.get("through_seq", 0))
    span = uncompacted_span_tokens(session.log.read_all(), through_seq)
    return span > TRIGGER_FRACTION * tail


async def run_compaction(
    session: GameSession, *, force: bool = False
) -> AsyncIterator[EngineEvent]:
    """Compact if needed (or forced). Yields engine events; quiet no-op otherwise."""
    if session.provider is None:
        if force:
            yield EngineError(message="Cannot compact without an AI provider.", recoverable=True)
        return
    if not force and not should_compact(session):
        return

    summary_data = snapshots.load_json(session.campaign.summary_path) or {}
    through_seq = int(summary_data.get("through_seq", 0))
    entries = session.log.read_all()
    protect = active_encounter_start_seq(entries)
    new_through = select_span(entries, through_seq, protect_after_seq=protect)
    if new_through is None:
        if force:
            msg = (
                "Combat is in progress; holding the fight verbatim until it ends."
                if protect is not None
                else "Nothing to compact yet."
            )
            yield EngineError(message=msg, recoverable=True)
        return

    yield CompactionStarted()

    old_summary = summary_data.get("summary_md", "")
    current = canon.load(session.campaign)
    transcript = render_span(entries, through_seq, new_through)

    he_settings = session.high_effort_settings()
    provider = session.provider_for_settings(he_settings)
    if provider is not None:
        settings = he_settings
    else:
        # No key for the high-effort backend: fall back to the chat provider/model
        # so compaction still runs, just at the table's quality.
        provider, settings = session.provider, session.settings

    user_text = _chronicler_user_message(current, old_summary, transcript)
    result: dict | None = None
    usage = Usage()
    try:
        async for item in _extract_canon(provider, settings, user_text):
            if isinstance(item, CompactionProgress):
                yield item
            else:
                result, usage = item
    except ProviderError as exc:
        yield EngineError(message=f"Compaction failed: {exc}", recoverable=True)
        return
    session.accrue_usage(usage)

    summary = (result.get("summary", "") if result else "").strip()
    if not summary:
        yield EngineError(
            message="Compaction produced no summary; keeping full log.", recoverable=True
        )
        return

    ops = result.get("ops", [])
    # Re-load: a note_canon call during the (awaited) chronicler call may have
    # written canon since `current` was read. Apply onto the latest so a
    # concurrent GM note is not clobbered.
    updated, warnings = canon.apply_ops(canon.load(session.campaign), ops, at_seq=new_through)
    updated.through_seq = new_through
    # Write canon FIRST; the through_seq commit marker (in summary.json) goes LAST,
    # so an interruption between the two reprocesses the span (idempotent ops).
    canon.save(session.campaign, updated)
    snapshots.save_json(
        session.campaign.summary_path,
        {
            "summary_md": summary,
            "through_seq": new_through,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    session.log.append(
        "compaction",
        {
            "through_seq": new_through,
            "summary_tokens": est_tokens(summary),
            "canon_ops": len(ops),
            "canon_dropped": len(warnings),
        },
    )
    yield CompactionFinished(summary_tokens_est=est_tokens(summary))


def _chronicler_user_message(current: canon.Canon, old_summary: str, transcript: str) -> str:
    open_md, overflow_md = canon.render_open_with_overflow(current, include_hidden=True)
    parts = [
        "## Current canon (open entries; update or resolve these by their [id])",
        open_md or "(canon is empty)",
    ]
    if overflow_md:
        parts += [
            "## Also open, but no longer shown to the GM",
            "The open set has outgrown what fits in the GM's context, so these "
            "entries scrolled out of its view. Patch them by their [id]: resolve "
            "any the story has since settled, and merge duplicates or near-"
            "duplicates into a single entry, so the working set stays lean and the "
            "threads that matter stay visible. Do not close a thread that is still "
            "genuinely open.\n" + overflow_md,
        ]
    parts += [
        "## Running summary (fold the new events into this)",
        old_summary or "(no summary yet)",
        "## New play to fold in (about to leave verbatim memory)",
        transcript,
    ]
    return "\n\n".join(parts)


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _sentence_count(text: str) -> int:
    """How many sentences the accumulated reasoning has reached. Used only to
    throttle the progress heartbeat to ~one tick per sentence; the text itself is
    never surfaced (it may reference GM-only canon)."""
    flat = " ".join(text.split())
    return sum(1 for part in _SENTENCE_SPLIT.split(flat) if part.strip())


async def _extract_canon(
    provider: Provider, settings: GenerationSettings, user_text: str
) -> AsyncIterator[CompactionProgress | tuple[dict | None, Usage]]:
    """Run the chronicler once, streaming its reasoning. Yields CompactionProgress
    snippets while it thinks (so a manual /compact shows live work, like template
    derivation), then yields the final ({ops, summary} or None, usage) tuple as its
    last item. Structured output via a one-shot record_canon tool call."""
    captured: dict = {}

    def _record(ctx, args: RecordCanonArgs) -> ToolOutcome:
        captured["ops"] = [op.model_dump(exclude_none=True) for op in args.ops]
        captured["summary"] = args.summary
        return ToolOutcome(content="Canon recorded.", summary="canon recorded")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="record_canon",
            description="Record the canon patches and the refreshed summary. Call exactly once.",
            args_model=RecordCanonArgs,
            handler=_record,
        )
    )
    system = [SystemBlock(text=CANON_SYSTEM.format(limit=SUMMARY_WORD_LIMIT))]
    convo = [Message(role="user", content=[TextBlock(text=user_text)])]
    usage = Usage()
    tool_uses = []
    think_acc = ""
    ticks = 0
    async for event in provider.stream_turn(
        system=system, messages=convo, tools=registry.defs(), settings=settings
    ):
        if event.type == "tool_use":
            tool_uses.append(event)
        elif event.type == "turn_done":
            usage = usage.add(event.usage)
        elif event.type == "thinking_delta":
            # The chronicler thinks before emitting its one tool call. Tick about
            # once per sentence to fill the wait on a manual /compact, without
            # surfacing the reasoning itself (the renderer shows a flavor phrase).
            think_acc += event.thinking
            reached = _sentence_count(think_acc)
            if reached > ticks:
                ticks = reached
                yield CompactionProgress()
    for tu in tool_uses:
        registry.dispatch(None, tu.name, tu.input)
    yield (captured or None), usage
