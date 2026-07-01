"""Provider-agnostic types: messages, tools, settings, and streaming events.

This is the seam that keeps models swappable. An adapter (Anthropic now,
OpenAI/LiteLLM later) maps these generic types to its provider's wire format
and silently drops knobs the model doesn't support.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from enum import StrEnum
from importlib import resources
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

# --- generation settings & model registry --------------------------------


class Effort(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    max = "max"


class Verbosity(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


# Per-turn generation knobs, set individually (no bundled "quality" presets):
# model picks the backend, effort/thinking trade latency for depth, and
# context_budget caps the assembled prompt. The default model is Claude Sonnet 5
# (the overall default); effort/thinking stay low/off so the real-time table
# stays snappy, and any field is overridable per campaign (/model, /effort,
# /thinking, /verbosity, /context).
class GenerationSettings(BaseModel):
    model: str = "claude-sonnet-5"
    # Output cap for a turn. With adaptive thinking the reasoning tokens and the
    # visible response share this budget, so it must be generous enough that a
    # high-effort, high-verbosity turn (e.g. rolling up a full party) can finish
    # thinking AND still narrate — otherwise the turn hits max_tokens mid-thought
    # and returns no text. 32k leaves ample room under every model's ceiling.
    max_tokens: int = 32_000
    effort: Effort = Effort.low
    verbosity: Verbosity = Verbosity.medium
    thinking: bool = False
    context_budget: int = 100_000  # target assembled-prompt tokens

    def merged(self, overrides: dict[str, Any]) -> GenerationSettings:
        known = {k: v for k, v in overrides.items() if k in type(self).model_fields}
        return type(self).model_validate({**self.model_dump(mode="json"), **known})


# One shared bundle for work that runs AWAY from the real-time table: one-time
# character-template derivation, and the background "chronicler" that maintains
# campaign canon. Both are off the hot path, so they can afford deeper reasoning
# without the latency mattering. Run the same Claude Sonnet 5 as the overall
# default, but turn thinking on at high effort. Staying on the Anthropic backend
# (the in-game default) means one ANTHROPIC_API_KEY covers the table and these
# jobs, with no separate key. Generous output room. Override per workspace via
# config.toml [high_effort] (any GenerationSettings field; pin a gemini-* model
# to run these on Gemini instead).
HIGH_EFFORT_SETTINGS = GenerationSettings(
    model="claude-sonnet-5",
    max_tokens=32_000,
    effort=Effort.high,
    thinking=True,
)


class ModelInfo(BaseModel):
    id: str
    display_name: str
    context_window: int
    max_output: int
    input_per_mtok: float  # USD per 1M input tokens
    output_per_mtok: float
    supports_effort: bool = True
    supports_thinking: bool = True
    # Gemini exposes reasoning depth as one discrete dial (thinkingLevel); these
    # are the values this model accepts, ordered shallow->deep. The Gemini adapter
    # folds the (thinking, effort) knobs onto this scale and clamps to it, e.g.
    # "minimal" is Gemini's "no thinking" floor but 3.x Pro rejects it. Unused by
    # Anthropic models, which control depth via adaptive thinking + the effort param.
    thinking_levels: list[str] = Field(default_factory=lambda: ["low", "high"])
    # The backend that serves this model. It is the model, not a separate
    # setting, that selects the provider, so picking a model picks its backend.
    provider: str = "anthropic"
    # A superseded model kept only for backward compatibility. Still fully usable
    # when pinned (config or /model) — the registry resolves it like any other —
    # but hidden from the model lists the wizards and /model print, so it isn't
    # offered to new picks. Deprecate an old model instead of deleting it so
    # campaigns already on it keep working.
    deprecated: bool = False


def infer_provider(model_id: str) -> str:
    """Best-effort backend for a model id not in the registry, so a brand-new
    model works without a code change. Keyed off the vendor's id convention."""
    return "gemini" if model_id.lower().startswith("gemini") else "anthropic"


class ModelRegistry(BaseModel):
    models: list[ModelInfo]

    def get(self, model_id: str) -> ModelInfo:
        for m in self.models:
            if m.id == model_id:
                return m
        # Unknown id: assume a capable default so new models work without a code change.
        return ModelInfo(
            id=model_id,
            display_name=model_id,
            context_window=200_000,
            max_output=8192,
            input_per_mtok=0.0,
            output_per_mtok=0.0,
            provider=infer_provider(model_id),
        )

    @property
    def visible(self) -> list[ModelInfo]:
        """Models to offer in pick lists: everything except deprecated ones.
        Deprecated models stay resolvable via ``get`` but aren't advertised."""
        return [m for m in self.models if not m.deprecated]

    def provider_for(self, model_id: str) -> str:
        """The backend that serves ``model_id``: the seam's single source of
        truth for model→provider, used to build and switch providers."""
        return self.get(model_id).provider

    @classmethod
    def load_default(cls) -> ModelRegistry:
        text = (resources.files("openadventure.data") / "models.json").read_text(encoding="utf-8")
        return cls.model_validate(json.loads(text))


# --- messages -------------------------------------------------------------


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ThinkingBlock(BaseModel):
    # Extended-thinking output. The signature must be round-tripped unmodified:
    # when thinking is on and the prior assistant turn used tools, the API
    # requires its thinking block(s) back (verified by signature) on the next turn.
    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str = ""


class RedactedThinkingBlock(BaseModel):
    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


ContentBlock = TextBlock | ThinkingBlock | RedactedThinkingBlock | ToolUseBlock | ToolResultBlock


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: list[ContentBlock]


class SystemBlock(BaseModel):
    text: str
    cache: bool = False  # ask the adapter to place a cache breakpoint here


class ToolDef(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


# --- streaming events out of a provider -----------------------------------


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


class PTextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class PThinkingDelta(BaseModel):
    # A streaming chunk of reasoning, surfaced live for progress display only
    # (e.g. a spinner that fills the long gap before tool calls arrive). The
    # whole block is re-emitted as PThinking at turn end with its signature for
    # convo reconstruction; this carries no signature and must not be persisted
    # or round-tripped. Consumers that don't show progress can ignore it.
    type: Literal["thinking_delta"] = "thinking_delta"
    thinking: str


class PThinking(BaseModel):
    # Emitted once per completed thinking block so a multi-round tool loop can
    # rebuild the assistant turn with its thinking intact (see ThinkingBlock).
    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str = ""


class PRedactedThinking(BaseModel):
    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str


class PToolUseStart(BaseModel):
    type: Literal["tool_use_start"] = "tool_use_start"
    id: str
    name: str


class PToolUse(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]


class PTurnDone(BaseModel):
    type: Literal["turn_done"] = "turn_done"
    stop_reason: StopReason
    usage: Usage = Field(default_factory=Usage)


ProviderEvent = (
    PTextDelta
    | PThinkingDelta
    | PThinking
    | PRedactedThinking
    | PToolUseStart
    | PToolUse
    | PTurnDone
)


class ProviderError(RuntimeError):
    """A provider/API failure the engine should surface, not crash on."""

    def __init__(self, message: str, *, recoverable: bool = True, suggest_model: bool = False):
        super().__init__(message)
        self.recoverable = recoverable
        # True when switching models (``/model``) is a plausible fix, e.g. the
        # current model is rate-limited, overloaded, or misconfigured.
        self.suggest_model = suggest_model


class Provider(Protocol):
    def stream_turn(
        self,
        *,
        system: list[SystemBlock],
        messages: list[Message],
        tools: list[ToolDef],
        settings: GenerationSettings,
    ) -> AsyncIterator[ProviderEvent]: ...
