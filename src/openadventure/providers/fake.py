"""Scripted provider for engine tests: no network, fully deterministic."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from openadventure.providers.base import (
    GenerationSettings,
    Message,
    ProviderEvent,
    SystemBlock,
    ToolDef,
)


@dataclass
class CapturedCall:
    system: list[SystemBlock]
    messages: list[Message]
    tools: list[ToolDef]
    settings: GenerationSettings


@dataclass
class FakeProvider:
    """Yields pre-scripted event lists, one list per stream_turn call."""

    script: list[list[ProviderEvent]]
    calls: list[CapturedCall] = field(default_factory=list)
    error: Exception | None = None  # raised on the next call when set

    async def stream_turn(
        self,
        *,
        system: list[SystemBlock],
        messages: list[Message],
        tools: list[ToolDef],
        settings: GenerationSettings,
    ) -> AsyncIterator[ProviderEvent]:
        self.calls.append(
            CapturedCall(
                system=list(system), messages=list(messages), tools=list(tools), settings=settings
            )
        )
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        if not self.script:
            raise AssertionError("FakeProvider script exhausted")
        for event in self.script.pop(0):
            yield event


def fake_kwargs() -> dict[str, Any]:
    """Convenience: minimal kwargs for direct stream_turn calls in tests."""
    return {
        "system": [SystemBlock(text="test system")],
        "messages": [Message(role="user", content=[])],
        "tools": [],
        "settings": GenerationSettings(),
    }
