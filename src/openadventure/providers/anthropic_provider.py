"""Anthropic adapter: maps the generic provider seam onto the Anthropic SDK.

The only module in the codebase that imports `anthropic`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from openadventure.providers.base import (
    GenerationSettings,
    Message,
    ModelRegistry,
    PRedactedThinking,
    ProviderError,
    ProviderEvent,
    PTextDelta,
    PThinking,
    PThinkingDelta,
    PToolUse,
    PToolUseStart,
    PTurnDone,
    StopReason,
    SystemBlock,
    ToolDef,
    Usage,
)

_STOP_REASONS: dict[str | None, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def _convert_messages(messages: list[Message], *, cache_last: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        content: list[dict[str, Any]] = []
        cache_here = message.cache
        for block in message.content:
            match block.type:
                case "text":
                    content.append({"type": "text", "text": block.text})
                case "thinking":
                    # Sent back verbatim (incl. signature) so the API can verify
                    # reasoning continuity across tool rounds.
                    content.append(
                        {
                            "type": "thinking",
                            "thinking": block.thinking,
                            "signature": block.signature,
                        }
                    )
                case "redacted_thinking":
                    content.append({"type": "redacted_thinking", "data": block.data})
                case "tool_use":
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
                case "tool_result":
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        }
                    )
        out.append({"role": message.role, "content": content})
        # Byte-stable boundaries (context head, last history message) get a breakpoint
        # so the whole head+history prefix is read back cheaply on the next turn; the
        # volatile foot and live message that follow are re-sent either way.
        if cache_here and content:
            content[-1]["cache_control"] = {"type": "ephemeral"}
    if cache_last:
        # Also cache the very end of the conversation. Turn-to-turn this trails the
        # volatile foot so it isn't re-read, but within one turn's multi-round tool
        # loop the prefix only grows, so each round reads the previous round's cache.
        for message in reversed(out):
            if message["content"]:
                message["content"][-1]["cache_control"] = {"type": "ephemeral"}
                break
    return out


def _convert_system(system: list[SystemBlock]) -> list[dict[str, Any]]:
    out = []
    for block in system:
        item: dict[str, Any] = {"type": "text", "text": block.text}
        if block.cache:
            item["cache_control"] = {"type": "ephemeral"}
        out.append(item)
    return out


def _request_kwargs(
    *,
    system: list[SystemBlock],
    messages: list[Message],
    tools: list[ToolDef],
    settings: GenerationSettings,
    registry: ModelRegistry,
) -> dict[str, Any]:
    model = registry.get(settings.model)
    kwargs: dict[str, Any] = {
        "model": settings.model,
        "max_tokens": min(settings.max_tokens, model.max_output),
        "system": _convert_system(system),
        "messages": _convert_messages(messages, cache_last=True),
    }
    if tools:
        kwargs["tools"] = [t.model_dump() for t in tools]
    if settings.thinking and model.supports_thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    output_config: dict[str, Any] = {}
    if model.supports_effort:
        output_config["effort"] = settings.effort.value
    if output_config:
        # via extra_body for forward-compat with SDKs lacking the typed param
        kwargs["extra_body"] = {"output_config": output_config}
    return kwargs


class AnthropicProvider:
    def __init__(self, api_key: str, registry: ModelRegistry | None = None):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.registry = registry or ModelRegistry.load_default()

    async def stream_turn(
        self,
        *,
        system: list[SystemBlock],
        messages: list[Message],
        tools: list[ToolDef],
        settings: GenerationSettings,
    ) -> AsyncIterator[ProviderEvent]:
        kwargs = _request_kwargs(
            system=system,
            messages=messages,
            tools=tools,
            settings=settings,
            registry=self.registry,
        )

        try:
            async with self.client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if (
                        event.type == "content_block_delta"
                        and getattr(event.delta, "type", "") == "text_delta"
                    ):
                        yield PTextDelta(text=event.delta.text)
                    elif (
                        event.type == "content_block_delta"
                        and getattr(event.delta, "type", "") == "thinking_delta"
                    ):
                        # Live reasoning for progress display; the completed
                        # block is re-emitted below from get_final_message().
                        yield PThinkingDelta(thinking=event.delta.thinking)
                    elif (
                        event.type == "content_block_start"
                        and getattr(event.content_block, "type", "") == "tool_use"
                    ):
                        yield PToolUseStart(
                            id=event.content_block.id, name=event.content_block.name
                        )
                final = await stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise ProviderError(
                "Authentication failed. Check your ANTHROPIC_API_KEY.", recoverable=False
            ) from exc
        except anthropic.APIStatusError as exc:
            # 404 unknown model, 413 too large for this model, 429 rate limit,
            # 529 overloaded: a different model is a plausible fix.
            suggest_model = exc.status_code in (404, 413, 429, 529)
            raise ProviderError(
                f"API error {exc.status_code}: {exc.message}", suggest_model=suggest_model
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Could not reach the Anthropic API (network error).") from exc

        for block in final.content:
            if block.type == "thinking":
                yield PThinking(thinking=block.thinking, signature=block.signature or "")
            elif block.type == "redacted_thinking":
                yield PRedactedThinking(data=block.data)
            elif block.type == "tool_use":
                yield PToolUse(id=block.id, name=block.name, input=dict(block.input or {}))

        usage = Usage(
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens,
            cache_creation_input_tokens=final.usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=final.usage.cache_read_input_tokens or 0,
        )
        yield PTurnDone(stop_reason=_STOP_REASONS.get(final.stop_reason, "other"), usage=usage)
