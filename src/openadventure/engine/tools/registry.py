"""Tool registry: pydantic-validated args, dispatch, JSON schema generation."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ValidationError

from openadventure.engine.events import EngineEvent
from openadventure.providers.base import ToolDef, Usage

if TYPE_CHECKING:
    from openadventure.media.host import MediaHost
    from openadventure.media.narration import NarrationAgent
    from openadventure.media.tasks import BackgroundTasks
    from openadventure.store.eventlog import EventLog
    from openadventure.store.workspace import Campaign, CampaignMeta, Workspace


@dataclass
class ToolContext:
    """Everything a tool handler may need. Handlers do their own domain
    logging (roll/state_change entries); the agent logs generic tool_call."""

    workspace: Workspace
    campaign: Campaign
    meta: CampaignMeta
    log: EventLog
    rng: random.Random
    background: BackgroundTasks | None = None
    narration: NarrationAgent | None = None
    # The frontend's media presenter (ambience tools generate, then present
    # through this). None on off-table dispatchers (template derivation); media
    # tools guard for it.
    media_host: MediaHost | None = None
    narration_cues: int = 0
    voice_cues: list[Any] = field(default_factory=list)
    sound_effect_cues: list[Any] = field(default_factory=list)
    # Session-owned accounting hook. Media work is deliberately asynchronous,
    # so it must record successful generation from its background task rather
    # than when the model merely requests a tool call.
    usage_recorder: Callable[[Usage, str, str, str | None], None] | None = None
    # Read-only turn (a /btw aside): only read-only tools may run; dispatch
    # refuses anything that would mutate state. Independent of whether the turn
    # is logged: an off-the-record /sudo directive is unlogged but DOES mutate.
    read_only: bool = False

    def record_media_usage(
        self,
        usage: Usage,
        *,
        kind: str,
        backend_name: str,
        model_id: str | None,
    ) -> None:
        if self.usage_recorder is not None:
            self.usage_recorder(usage, kind, backend_name, model_id)


@dataclass
class ToolOutcome:
    content: str  # fed back to the model as the tool result
    events: list[EngineEvent] = field(default_factory=list)  # extra frontend events
    summary: str = ""  # short human-readable result line (defaults to content)
    ok: bool = True
    private: bool = False
    public_summary: str = ""
    public_args_summary: str = ""

    @property
    def result_summary(self) -> str:
        return self.summary or (
            self.content if len(self.content) <= 80 else self.content[:77] + "…"
        )

    @property
    def public_result_summary(self) -> str:
        return self.public_summary or ("private" if self.private else self.result_summary)


Handler = Callable[[ToolContext, Any], ToolOutcome]


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Handler
    # Read-only and thread-safe: may be dispatched off the event loop and run
    # concurrently with its siblings. Leave False for anything that touches the
    # seeded RNG, spawns background tasks, or mutates campaign state.
    parallel_safe: bool = False
    # Pure retrieval: no state mutation, no RNG, no log writes, no media. Only
    # these run in an off-the-record /btw aside (see ToolContext.ephemeral). Set
    # it at the tool's definition site so there's no separate allowlist to keep
    # in sync.
    read_only: bool = False

    def tooldef(self) -> ToolDef:
        return ToolDef(
            name=self.name,
            description=self.description,
            input_schema=self.args_model.model_json_schema(),
        )


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name!r}")
        self._tools[tool.name] = tool

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def defs(self) -> list[ToolDef]:
        return [t.tooldef() for t in self._tools.values()]

    def read_only_defs(self) -> list[ToolDef]:
        """The pure-retrieval tools, the only ones offered in a /btw aside."""
        return [t.tooldef() for t in self._tools.values() if t.read_only]

    def dispatch(self, ctx: ToolContext, name: str, raw_input: dict[str, Any]) -> ToolOutcome:
        """Execute a tool. Errors come back as failed outcomes, never exceptions;
        the model sees the message and can recover."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolOutcome(
                content=f"Error: unknown tool {name!r}", summary="unknown tool", ok=False
            )
        if getattr(ctx, "read_only", False) and not tool.read_only:
            # Belt-and-suspenders: a read-only turn (a /btw aside) is offered only
            # read-only defs, but never let a mutating call through even if one is
            # somehow requested. (getattr tolerates the stub/None ctx used by
            # off-table dispatchers like template derivation, which never set it.)
            return ToolOutcome(
                content=(
                    f"Error: {name} changes state and can't run in a read-only aside; "
                    "only lookups are available here."
                ),
                summary="blocked: read-only aside",
                ok=False,
            )
        try:
            args = tool.args_model.model_validate(raw_input)
        except ValidationError as exc:
            return ToolOutcome(
                content=f"Error: invalid arguments for {name}: {exc}",
                summary="invalid args",
                ok=False,
            )
        try:
            return tool.handler(ctx, args)
        except Exception as exc:  # tool bugs must not crash the turn
            return ToolOutcome(
                content=f"Error: {name} failed: {exc}", summary=f"failed: {exc}", ok=False
            )

    async def dispatch_batch(
        self, ctx: ToolContext, calls: list[tuple[str, dict[str, Any]]]
    ) -> list[ToolOutcome]:
        """Dispatch one round's tool calls, returning outcomes in call order.

        Tools marked ``parallel_safe`` (the read-only retrieval tools, whose
        cost is an embedding pass plus an index scan and file IO) are offloaded
        to worker threads with ``asyncio.to_thread`` and run concurrently, so a
        slow lookup neither freezes the event loop (background renders, the UI)
        nor waits behind its siblings. Everything else runs inline on the loop
        thread in call order, which preserves the seeded-RNG sequence and keeps
        ``background.spawn`` on the loop it requires. Inline tools finish before
        any threaded one starts, so a state mutation never races a reader."""
        outcomes: list[ToolOutcome | None] = [None] * len(calls)
        deferred: list[int] = []
        for i, (name, raw_input) in enumerate(calls):
            tool = self._tools.get(name)
            if tool is not None and tool.parallel_safe:
                deferred.append(i)
            else:
                outcomes[i] = self.dispatch(ctx, name, raw_input)
        if deferred:
            results = await asyncio.gather(
                *(asyncio.to_thread(self.dispatch, ctx, calls[i][0], calls[i][1]) for i in deferred)
            )
            for i, outcome in zip(deferred, results, strict=True):
                outcomes[i] = outcome
        return cast("list[ToolOutcome]", outcomes)


def summarize_args(raw_input: dict[str, Any], limit: int = 70) -> str:
    parts = []
    for key, value in raw_input.items():
        text = str(value)
        if len(text) > 30:
            text = text[:27] + "…"
        parts.append(f"{key}={text!r}" if isinstance(value, str) else f"{key}={text}")
    joined = ", ".join(parts)
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"
