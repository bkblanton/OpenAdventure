"""Context budgeting: token estimation and log-tail -> message rendering.

Token counts are estimated as chars//4, plenty accurate for budget decisions.

History rendering keeps the assistant *text* channel clean and represents the
GM's actions as its own typed tool calls:

* The ASSISTANT *text* role carries only genuine GM narration, never tool/roll/
  bracket syntax. This is load-bearing: when past mechanical activity was rendered
  as bracket *text* (``[search_campaign: results found]``, ``[rolled 1d100 →
  44]``) in the assistant channel, a long campaign accumulated enough of that
  shape that the model started *writing the brackets as prose* instead of making
  real tool calls. Typed ``tool_use``/``tool_result`` blocks are not narration
  prose, so they carry tool activity without that risk.

* Every tool call the GM makes renders as a real ``tool_use``/``tool_result``
  block, so the transcript shows the GM *acting* through its tools rather than the
  world changing, the dice rolling, or the music playing on its own. The
  read-only retrievals and lookups (``REPLAY_TOOLS``: search/read campaign and
  rules, get_sheet, search_canon) carry their stored result, keeping the module
  text and sheet/canon detail the always-fresh blocks drop; every other call
  (mutations, roll_dice, oracle, the media tools, ...) carries its short
  ``result_summary`` (e.g. ``"1d100 -> 88 (failure)"``, ``"Booker hp: 9/12"``).
  Showing the *call* is what stops the model learning that state, rolls, or media
  happen without it calling a tool, the mirror of the old brackets-as-prose bug.

* The one ambient event with no tool call is a *player's* own roll (``/roll`` or
  a physical die at the table), which stays a user-side note, like player input.
  Failed and content-less calls don't render.

The log keeps both representations, so each consumer reads what suits it: the
table agent (``render_history``) gets chats plus the GM's tool calls; the
chronicler (``compaction.render_span``) gets chats plus the compact roll/state
notes it summarizes into canon. ``render_history`` never emits those notes, and
the chronicler never reads the tool calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from openadventure.providers.base import (
    GenerationSettings,
    Message,
    ModelInfo,
    TextBlock,
    ToolDef,
    ToolResultBlock,
    ToolUseBlock,
)
from openadventure.store.eventlog import LogEntry

# fixed reserve fractions of the effective budget
OUTPUT_RESERVE_FRACTION = 0.15

# Corpus-retrieval reads: their result is static ingested book text, so it is
# both safe to replay and exactly re-derivable. The migration backfills these.
CORPUS_REPLAY_TOOLS = frozenset({"search_campaign", "read_campaign", "search_rules", "read_rules"})
# Read-only lookups whose result carries detail the always-fresh context blocks
# drop: the full sheet behind the at-a-glance roster, or a resolved/hidden canon
# entry the open-canon list omits. Replayed from live play, but NOT migrated:
# re-running get_sheet/search_canon now would stamp current state onto an old turn.
LOOKUP_REPLAY_TOOLS = frozenset({"get_sheet", "search_sheets", "list_sheets", "search_canon"})
# Read-only tools whose full result body is stored at log time and replayed (see
# the module docstring). Every other tool call also renders as a block, but from
# its short result_summary (always logged), so it needs nothing stored. This set
# governs only what gets a stored body, not what renders.
REPLAY_TOOLS = CORPUS_REPLAY_TOOLS | LOOKUP_REPLAY_TOOLS

# Per-entry cap on the tool-result content persisted for replay. Smaller than
# what the live turn saw (the search tools render the top hits in full): the
# replayed copy only has to keep the GM oriented, and it persists across turns,
# so it is deliberately lean. The GM re-searches/reads when it needs the rest.
REPLAY_CONTENT_CHARS = 2_000

# Nothing estimates the size of the assembled prompt. The log tail is always sized
# from measurement via ContextBudget.tail_for: every caller (the live turn, the
# compaction trigger, the cost preview) measures the real non-tail input (system
# prompt + context block + tool schemas) through GameSession.non_tail_tokens and
# subtracts it from the budget. So there are no per-block reservations to drift out
# of sync with the prompts.

# prepped-location text cap, and the one prompt-size constant. Functional, not an
# estimate: build_messages passes this to location_prep to bound how much canonical
# text it reads from the campaign library and inlines, before the context block is
# assembled. The real cost is then captured by the measured context length.
PREP_BUDGET = 3_500

# typical output tokens, for the per-prompt cost estimate. Thinking turns write
# more: reasoning, narration, and tool args.
TYPICAL_OUTPUT_THINKING = 4_000
TYPICAL_OUTPUT_PLAIN = 1_200

# compaction keeps the verbatim tail near 0.8 * tail budget, so a steady-state
# turn in a long campaign sends about this many input tokens
COMPACTION_TRIGGER_FRACTION = 0.8

# floor on the log tail so history never vanishes entirely, even when the rest of
# the prompt is unusually large. total already excludes the output reserve.
MIN_TAIL = 2_000


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def tool_schema_tokens(tools: list[ToolDef]) -> int:
    """Estimated tokens for the tool schemas sent alongside the prompt. The provider
    serializes each ToolDef (name + description + input_schema) to JSON and the model
    counts it as input, so the wire cost is close to est_tokens of that JSON. A
    fully-loaded campaign's toolset runs several thousand tokens, enough that the live
    tail must account for it."""
    if not tools:
        return 0
    return est_tokens(json.dumps([t.model_dump() for t in tools]))


@dataclass
class ContextBudget:
    total: int  # effective assembled-prompt budget (after the output reserve)
    prep: int  # share reserved for the prepped-location text (sized before the block is built)

    @classmethod
    def from_settings(cls, settings: GenerationSettings, model: ModelInfo) -> ContextBudget:
        total = min(settings.context_budget, model.context_window)
        total = int(total * (1 - OUTPUT_RESERVE_FRACTION))
        return cls(total=total, prep=PREP_BUDGET)

    def tail_for(self, non_tail_tokens: int) -> int:
        """Tail room given the measured token size of everything else in the assembled
        prompt: system prompt + context block + tool schemas. The log tail is always
        sized from this measurement, never an estimate, so a large roster, canon, prep
        block, or toolset shrinks the tail instead of pushing the prompt past ``total``.
        Floored at ``MIN_TAIL``; ``total`` already excludes the output reserve."""
        return max(MIN_TAIL, self.total - non_tail_tokens)


def estimate_prompt_cost(
    settings: GenerationSettings, model: ModelInfo, non_tail_tokens: int
) -> float:
    """Rough USD cost of one turn in a long, compacted campaign: the steady state the
    player actually pays. ``non_tail_tokens`` is the measured size of the prompt minus
    the log tail (system + context block + tool schemas), from
    GameSession.non_tail_tokens; at steady state the tail fills to roughly the
    compaction trigger fraction of the room left after it. Priced at the full input
    rate for every token: prefix caching (system prompt + head + history) makes the
    real per-prompt cost lower between compactions, so this is a conservative ceiling,
    not a floor."""
    budget = ContextBudget.from_settings(settings, model)
    peak_input = (
        int(COMPACTION_TRIGGER_FRACTION * budget.tail_for(non_tail_tokens)) + non_tail_tokens
    )
    out = TYPICAL_OUTPUT_THINKING if settings.thinking else TYPICAL_OUTPUT_PLAIN
    return (peak_input * model.input_per_mtok + out * model.output_per_mtok) / 1_000_000


def _cap_args(value: object, limit: int = 300) -> object:
    """Recursively clip long string values in a tool call's arguments so a verbose
    mutation (a long update_scene description, a full create_sheet) doesn't bloat
    the replayed tool_use block. Structure is preserved; only long strings clip."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {k: _cap_args(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap_args(v, limit) for v in value]
    return value


def _tool_block(entry: LogEntry) -> dict | None:
    """The structured payload for a successful tool call, rendered as a
    ``tool_use``/``tool_result`` pair, or None. A read-only retrieval/lookup
    (``REPLAY_TOOLS``) carries its stored content; every other call carries its
    short ``result_summary``, so the transcript shows the GM acting through its
    tools (changing state, rolling, playing media) rather than those happening on
    their own. Failed calls, and (pre-migration) retrievals with nothing stored,
    do not render."""
    if entry.type != "tool_call":
        return None
    data = entry.data
    name = data.get("name")
    if not data.get("ok", True):
        return None
    if name in REPLAY_TOOLS:
        content = data.get("content")
        if not content:
            return None  # pre-migration corpus/lookup with no stored body
    else:
        content = data.get("result_summary") or "done"
    return {
        "id": f"call-{entry.seq}",
        "name": name,
        "input": _cap_args(data.get("args") or {}),
        "content": content,
    }


def _call_tokens(call: dict) -> int:
    """Token cost of one replayed call: its arguments plus its result content. The
    same figure is used by the tail budget and the compaction trigger so they agree
    on what the overlay costs."""
    return est_tokens(json.dumps(call["input"], ensure_ascii=False)) + est_tokens(call["content"])


def _entry_text(entry: LogEntry) -> tuple[str, str] | None:
    """Map a non-tool log entry to (role, text), or None if it shouldn't render.

    The assistant role is reserved for ``gm_message`` narration. Mechanical
    activity is reported as user-side engine notes (or dropped) so the assistant
    *text* transcript never contains roll/bracket syntax for the model to imitate;
    see the module docstring. Replay-eligible tool calls are handled separately
    (``_tool_block``); other ``tool_call`` entries render nothing here.
    """
    data = entry.data
    match entry.type:
        case "user_message":
            return ("user", data.get("text", ""))
        case "gm_message":
            return ("assistant", data.get("text", ""))
        case "roll":
            # A GM roll renders as its roll_dice/oracle tool_use block (carrying the
            # "1d100 -> 88 (failure)" summary), so only a player's own roll (/roll or
            # a physical die) is an ambient event with no tool call and stays a note.
            if data.get("by") != "player":
                return None
            reason = f" ({data['reason']})" if data.get("reason") else ""
            verdict = f" = {data['outcome']}" if data.get("outcome") else ""
            extremes = []
            if data.get("max_rolls"):
                extremes.append(f"max_rolls={data['max_rolls']}")
            if data.get("min_rolls"):
                extremes.append(f"min_rolls={data['min_rolls']}")
            extra = f" [{', '.join(extremes)}]" if extremes else ""
            return (
                "user",
                f"[engine note · player rolled {data.get('expression')} → "
                f"{data.get('total')}{verdict}{extra}{reason}]",
            )
        case "tool_call":
            # Rendered as a structured tool_use/tool_result block via _tool_block,
            # never as text here.
            return None
        case "media" | "state_change":
            # The GM's own tool call (play_music, generate_image, update_scene, ...)
            # renders as its tool_use/tool_result block, so these byproduct entries
            # are not also rendered to the table agent. They stay in the log for the
            # chronicler (compaction) and /log.
            return None
        case "note":
            return None  # legacy notes (now canon); recalled via search_canon, not replayed
        case _:
            return None


def _coalesce_text(blocks: list) -> list:
    """Merge consecutive TextBlocks in one message into a single block, leaving
    tool_use/tool_result blocks untouched. Tidier and cheaper than many tiny text
    blocks when engine notes and a player message land in the same user turn."""
    out: list = []
    for block in blocks:
        if isinstance(block, TextBlock) and out and isinstance(out[-1], TextBlock):
            out[-1] = TextBlock(text=out[-1].text + "\n\n" + block.text)
        else:
            out.append(block)
    return out


def render_history(
    entries: list[LogEntry], *, tail_budget: int, after_seq: int = 0
) -> tuple[list[Message], int]:
    """Render log entries (seq > after_seq) into messages, newest-first greedy
    within budget. Returns (messages, estimated_tokens).

    Narration and engine notes render as text messages; a run of consecutive
    replay-eligible tool calls renders as one ``tool_use`` assistant message paired
    with one ``tool_result`` user message. Each replayed call produces both its
    ``tool_use`` and its ``tool_result`` from the same log entry, so the pair is
    never orphaned, and a tool round is an atomic budget unit so the greedy cut
    cannot split a pair. Because narration always trails its turn's tool calls,
    newest-first keeps the narration whenever it keeps the round.
    """
    # 1. Build ordered units: ("msg", (role, text)) or ("tools", [call, ...]).
    units: list[tuple[str, object]] = []
    pending: list[dict] = []

    def flush() -> None:
        if pending:
            units.append(("tools", list(pending)))
            pending.clear()

    for entry in entries:
        if entry.seq <= after_seq:
            continue
        call = _tool_block(entry)
        if call is not None:
            pending.append(call)
            continue
        if entry.type == "tool_call":
            continue  # failed / content-less tool call: renders nothing
        pair = _entry_text(entry)
        if pair is None or not pair[1]:
            continue  # GM roll / state_change / media: render nothing, don't split the round
        flush()
        units.append(("msg", pair))
    flush()

    def unit_cost(unit: tuple[str, object]) -> int:
        kind, payload = unit
        if kind == "msg":
            return est_tokens(payload[1])  # type: ignore[index]
        return sum(_call_tokens(c) for c in payload)  # type: ignore[union-attr]

    # 2. Greedy newest-first, atomic units, keep at least the newest.
    kept: list[tuple[str, object]] = []
    used = 0
    for unit in reversed(units):
        cost = unit_cost(unit)
        if kept and used + cost > tail_budget:
            break
        kept.append(unit)
        used += cost
    kept.reverse()

    # 3. Units -> messages (a tool round becomes an assistant + user pair).
    raw: list[Message] = []
    for kind, payload in kept:
        if kind == "msg":
            role, text = payload  # type: ignore[misc]
            raw.append(Message(role=role, content=[TextBlock(text=text)]))  # type: ignore[arg-type]
        else:
            calls: list[dict] = payload  # type: ignore[assignment]
            raw.append(
                Message(
                    role="assistant",
                    content=[
                        ToolUseBlock(id=c["id"], name=c["name"], input=c["input"]) for c in calls
                    ],
                )
            )
            raw.append(
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(tool_use_id=c["id"], content=c["content"]) for c in calls
                    ],
                )
            )

    # 4. Merge consecutive same-role messages, coalescing adjacent text blocks.
    merged: list[Message] = []
    for msg in raw:
        if merged and merged[-1].role == msg.role:
            merged[-1].content.extend(msg.content)
        else:
            merged.append(Message(role=msg.role, content=list(msg.content)))
    messages = [Message(role=m.role, content=_coalesce_text(m.content)) for m in merged]
    return messages, used


def uncompacted_span_tokens(entries: list[LogEntry], after_seq: int) -> int:
    """Token size of everything after ``after_seq`` that the tail would render:
    narration, engine notes, AND the replayed tool overlay. Counting the overlay is
    what makes the compaction trigger fire on the full rendered tail (so a
    search-heavy stretch compacts sooner) rather than on narration alone."""
    total = 0
    for entry in entries:
        if entry.seq <= after_seq:
            continue
        call = _tool_block(entry)
        if call is not None:
            total += _call_tokens(call)
            continue
        if entry.type == "tool_call":
            continue
        pair = _entry_text(entry)
        if pair and pair[1]:
            total += est_tokens(pair[1])
    return total
