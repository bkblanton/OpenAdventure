"""OpenAI adapter: request shaping, reasoning mapping, Responses SSE event mapping."""

import json

from openadventure.config import AppConfig
from openadventure.engine.session import resolve_settings
from openadventure.providers.base import (
    Effort,
    GenerationSettings,
    Message,
    ModelRegistry,
    SystemBlock,
    TextBlock,
    ThinkingBlock,
    ToolDef,
    ToolResultBlock,
    ToolUseBlock,
    Verbosity,
)
from openadventure.providers.factory import build_provider
from openadventure.providers.openai_provider import (
    OpenAIProvider,
    _reasoning_effort,
    _reasoning_item,
    _reasoning_signature,
)


def _provider() -> OpenAIProvider:
    return OpenAIProvider("test-key", ModelRegistry.load_default())


# --- model -> backend mapping ---------------------------------------------
def test_factory_builds_openai_backend():
    provider = build_provider("openai", "k", ModelRegistry.load_default())
    assert isinstance(provider, OpenAIProvider)


def test_registry_maps_gpt_model_to_backend():
    registry = ModelRegistry.load_default()
    assert registry.provider_for("gpt-5.6-sol") == "openai"
    assert registry.provider_for("gpt-5.6-terra") == "openai"
    assert registry.provider_for("gpt-5.6-luna") == "openai"
    # unknown gpt ids are inferred from the vendor's id convention
    assert registry.provider_for("gpt-6-nova") == "openai"
    assert registry.provider_for("o3-pro") == "openai"


def test_gpt_model_selects_backend_via_settings():
    registry = ModelRegistry.load_default()
    picked = resolve_settings({}, AppConfig(workspace_dir=".", model="gpt-5.6-terra"), registry)
    assert picked.model == "gpt-5.6-terra"
    assert registry.provider_for(picked.model) == "openai"


# --- reasoning effort mapping ---------------------------------------------
def test_reasoning_effort_folds_thinking_and_effort():
    # thinking off -> the snappy "minimal" floor, regardless of effort
    assert _reasoning_effort(GenerationSettings(thinking=False, effort=Effort.high)) == "minimal"
    # thinking on -> effort sets depth, max clamps to high (OpenAI has no "max")
    assert _reasoning_effort(GenerationSettings(thinking=True, effort=Effort.low)) == "low"
    assert _reasoning_effort(GenerationSettings(thinking=True, effort=Effort.medium)) == "medium"
    assert _reasoning_effort(GenerationSettings(thinking=True, effort=Effort.high)) == "high"
    assert _reasoning_effort(GenerationSettings(thinking=True, effort=Effort.max)) == "high"


# --- request shaping ------------------------------------------------------
def test_request_sets_reasoning_verbosity_and_instructions():
    body = _provider()._request_body(
        system=[SystemBlock(text="be a GM")],
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        settings=GenerationSettings(model="gpt-5.6-sol", thinking=False, verbosity=Verbosity.high),
    )
    assert body["instructions"] == "be a GM"
    assert body["reasoning"] == {"effort": "minimal", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["text"] == {"verbosity": "high"}
    assert body["store"] is False
    assert body["stream"] is True
    assert body["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


def test_max_tokens_capped_to_model_output():
    body = _provider()._request_body(
        system=[],
        messages=[],
        tools=[],
        settings=GenerationSettings(model="gpt-5.6-sol", max_tokens=10_000_000),
    )
    assert body["max_output_tokens"] == 128000


def test_tools_become_function_tools_with_schema_passthrough():
    schema = {
        "type": "object",
        "title": "RollArgs",
        "properties": {"expr": {"type": "string"}},
        "required": ["expr"],
    }
    tool = ToolDef(name="roll", description="roll dice", input_schema=schema)
    body = _provider()._request_body(
        system=[], messages=[], tools=[tool], settings=GenerationSettings(model="gpt-5.6-sol")
    )
    [decl] = body["tools"]
    assert decl["type"] == "function"
    assert decl["name"] == "roll"
    assert decl["strict"] is False
    # Responses accepts standard JSON Schema, so the pydantic schema passes through.
    assert decl["parameters"] == schema


# --- message round-tripping -----------------------------------------------
def test_tool_use_and_result_become_function_items():
    items = _provider()._convert_messages(
        [
            Message(
                role="assistant",
                content=[ToolUseBlock(id="call-1", name="search_rules", input={"query": "hp"})],
            ),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="call-1", content="HP rules…")],
            ),
        ]
    )
    call, output = items
    assert call == {
        "type": "function_call",
        "call_id": "call-1",
        "name": "search_rules",
        "arguments": json.dumps({"query": "hp"}),
    }
    assert output == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "HP rules…",
    }


def test_reasoning_block_round_trips_via_signature():
    signature = _reasoning_signature("rs_123", "ENC")
    items = _provider()._convert_messages(
        [
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(thinking="considering", signature=signature),
                    ToolUseBlock(id="call-9", name="get_sheet", input={"id": "x"}),
                ],
            )
        ]
    )
    reasoning, call = items
    assert reasoning == {
        "type": "reasoning",
        "id": "rs_123",
        "encrypted_content": "ENC",
        "summary": [{"type": "summary_text", "text": "considering"}],
    }
    assert call["type"] == "function_call"


def test_reasoning_without_encrypted_signature_is_dropped():
    # A thinking block with no replayable (encrypted) reasoning yields no input item.
    assert _reasoning_item("summary only", "") is None
    items = _provider()._convert_messages(
        [
            Message(
                role="assistant",
                content=[ThinkingBlock(thinking="summary only", signature="")],
            )
        ]
    )
    assert items == []


def test_assistant_text_uses_output_text_part():
    [item] = _provider()._convert_messages(
        [Message(role="assistant", content=[TextBlock(text="You win.")])]
    )
    assert item == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "You win."}],
    }


# --- streaming ------------------------------------------------------------
async def _drain(provider, events):
    """Run stream_turn against a stubbed Responses SSE source."""
    import openadventure.providers.openai_provider as op

    def fake_stream(url, body, key):
        yield from events

    original = op._stream_events
    op._stream_events = fake_stream
    try:
        out = []
        async for event in provider.stream_turn(
            system=[],
            messages=[Message(role="user", content=[TextBlock(text="go")])],
            tools=[],
            settings=GenerationSettings(model="gpt-5.6-sol"),
        ):
            out.append(event)
        return out
    finally:
        op._stream_events = original


async def test_stream_emits_text_then_turn_done():
    events = [
        {"type": "response.output_text.delta", "delta": "You enter "},
        {"type": "response.output_text.delta", "delta": "a cave."},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "You enter a cave."}],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 40},
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 8},
                },
            },
        },
    ]
    out = await _drain(_provider(), events)
    text = "".join(e.text for e in out if e.type == "text_delta")
    assert text == "You enter a cave."
    done = out[-1]
    assert done.type == "turn_done"
    assert done.stop_reason == "end_turn"
    # cached input tokens are split out so the cache read bills cheaply
    assert done.usage.input_tokens == 60
    assert done.usage.cache_read_input_tokens == 40
    assert done.usage.output_tokens == 20
    # Reasoning is already included in OpenAI's billable output total; the
    # breakdown must expose it without adding another output category.
    assert done.usage.thinking_tokens == 8


async def test_stream_maps_function_call_and_reasoning():
    events = [
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "call_id": "call-7", "name": "roll_dice"},
        },
        {"type": "response.reasoning_summary_text.delta", "delta": "rolling"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "encrypted_content": "ENC",
                        "summary": [{"type": "summary_text", "text": "rolling"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call-7",
                        "name": "roll_dice",
                        "arguments": json.dumps({"expr": "1d20"}),
                    },
                ],
                "usage": {"input_tokens": 50, "output_tokens": 5},
            },
        },
    ]
    out = await _drain(_provider(), events)
    starts = [e for e in out if e.type == "tool_use_start"]
    uses = [e for e in out if e.type == "tool_use"]
    thinking = [e for e in out if e.type == "thinking"]
    deltas = [e for e in out if e.type == "thinking_delta"]
    assert starts[0].name == "roll_dice"
    assert uses[0].id == "call-7"
    assert uses[0].input == {"expr": "1d20"}
    # a function-calling turn reports tool_use as the stop reason
    assert out[-1].stop_reason == "tool_use"
    # reasoning is surfaced live (delta) and re-emitted whole with its signature
    assert deltas[0].thinking == "rolling"
    assert thinking[0].thinking == "rolling"
    assert json.loads(thinking[0].signature) == {"id": "rs_1", "enc": "ENC"}


async def test_stream_reports_max_tokens_when_incomplete():
    events = [
        {
            "type": "response.completed",
            "response": {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
                "usage": {"input_tokens": 10, "output_tokens": 128000},
            },
        }
    ]
    out = await _drain(_provider(), events)
    assert out[-1].stop_reason == "max_tokens"


# --- connect_provider / live model->backend switch ------------------------
def test_connect_provider_follows_gpt_model(make_session, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setenv("OPENAI_API_KEY", "o-key")
    session = make_session(script=[])

    session.set_override("model", "gpt-5.6-sol")
    assert session.connect_provider() is True
    assert session.provider_name() == "openai"
    assert isinstance(session.provider, OpenAIProvider)


def test_connect_provider_without_openai_key_disconnects(make_session, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    session = make_session(script=[])
    session.set_override("model", "gpt-5.6-sol")
    assert session.connect_provider() is False
    assert session.provider is None
