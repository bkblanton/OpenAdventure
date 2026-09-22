"""Model migrations preserve saved selections, opaque reasoning, and billing."""

import json
from datetime import date

import anthropic
import httpx
import pytest

from openadventure.engine.session import estimate_cost
from openadventure.providers.anthropic_provider import AnthropicProvider, _request_kwargs
from openadventure.providers.base import (
    Effort,
    GenerationSettings,
    Message,
    ModelRegistry,
    PTextDelta,
    PThinking,
    PToolUse,
    PTurnDone,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from openadventure.providers.fake import FakeProvider
from openadventure.providers.gemini_provider import GeminiProvider
from openadventure.providers.openai_provider import OpenAIProvider, _usage
from tests.conftest import collect


def test_visible_models_and_saved_replacements(make_session):
    registry = ModelRegistry.load_default()
    assert {model.id for model in registry.visible} == {
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "gemini-3.8-flash",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
    }
    session = make_session(script=[])
    for model_id in (
        "claude-fable-5",
        "claude-opus-4-8",
        "gemini-3.6-flash",
        "claude-opus-5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ):
        assert registry.get(model_id).deprecated
        session.set_override("model", model_id)
        assert session.settings.model == model_id
        assert session.campaign.load_meta().settings["model"] == model_id
        assert session.models.get(session.settings.model).id == model_id


def test_gemini_announced_price_change():
    model = next(m for m in ModelRegistry.load_default().models if m.id == "gemini-3.8-flash")
    before = model.priced(date(2026, 12, 31))
    after = model.priced(date(2027, 1, 1))
    assert (before.input_per_mtok, before.output_per_mtok) == (0.75, 3.75)
    assert (after.input_per_mtok, after.output_per_mtok) == (1.5, 7.5)
    assert before.input_per_mtok * before.cache_read_multiplier == pytest.approx(0.075)
    assert after.input_per_mtok * after.cache_read_multiplier == pytest.approx(0.15)


def test_fable_cache_reads_and_astra_long_context_pricing():
    registry = ModelRegistry.load_default()
    assert (
        estimate_cost(Usage(cache_read_input_tokens=1_000_000), registry.get("claude-fable-5-1"))
        == 0.25
    )
    astra = registry.get("gpt-6-astra")
    assert estimate_cost(Usage(input_tokens=272_000, output_tokens=1_000), astra) == pytest.approx(
        2.77
    )
    # All prompt tokens, including cache reads and writes, determine the tier.
    assert estimate_cost(
        Usage(input_tokens=1, cache_read_input_tokens=272_000, output_tokens=1_000), astra
    ) == pytest.approx(0.61902)


@pytest.mark.parametrize(
    "model_id,input_rate,output_rate,read_rate,write_rate",
    [
        ("claude-opus-5-5", 4, 20, 0.2, 5),
        ("gpt-6-sol", 2, 10, 0.2, 2.5),
        ("gpt-6-luna", 0.1, 0.5, 0.01, 0.125),
    ],
)
def test_new_model_standard_pricing(model_id, input_rate, output_rate, read_rate, write_rate):
    model = ModelRegistry.load_default().get(model_id)
    # Separate usage buckets also ensure reasoning is not billed twice.
    usage = Usage(
        input_tokens=1_000,
        output_tokens=1_000,
        thinking_tokens=800,
        cache_read_input_tokens=1_000,
        cache_creation_input_tokens=1_000,
    )
    assert estimate_cost(usage, model) == pytest.approx(
        (input_rate + output_rate + read_rate + write_rate) / 1_000
    )
    assert model.input_per_mtok * model.cache_read_multiplier == pytest.approx(read_rate)
    assert model.input_per_mtok * model.cache_write_multiplier == pytest.approx(write_rate)


@pytest.mark.parametrize("model_id,scale", [("gpt-6-sol", 1), ("gpt-6-luna", 0.05)])
def test_new_openai_long_context_pricing(model_id, scale):
    model = ModelRegistry.load_default().get(model_id)
    assert estimate_cost(Usage(input_tokens=272_000, output_tokens=1_000), model) == pytest.approx(
        0.554 * scale
    )
    # Cached tokens push the whole request across the boundary, including cache writes.
    usage = Usage(
        input_tokens=1,
        cache_read_input_tokens=271_000,
        cache_creation_input_tokens=1_000,
        output_tokens=1_000,
    )
    assert estimate_cost(usage, model) == pytest.approx(0.128404 * scale)


@pytest.mark.parametrize("model_id", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"])
@pytest.mark.parametrize(
    "thinking,effort,expected",
    [
        (False, Effort.max, "low"),
        (True, Effort.low, "low"),
        (True, Effort.high, "high"),
        (True, Effort.max, "max"),
    ],
)
def test_gpt6_request_uses_supported_reasoning(model_id, thinking, effort, expected):
    if not thinking and model_id != "gpt-6-astra":
        expected = "none"
    body = OpenAIProvider("test")._request_body(
        system=[],
        messages=[],
        tools=[],
        settings=GenerationSettings(
            model=model_id, thinking=thinking, effort=effort, max_tokens=200_000
        ),
    )
    assert body["reasoning"]["effort"] == expected
    assert body["max_output_tokens"] == 128_000
    assert body["include"] == ["reasoning.encrypted_content"]
    assert not {"temperature", "top_p", "top_logprobs"} & body.keys()


def test_openai_cache_writes_are_not_double_counted():
    usage = _usage(
        {
            "input_tokens": 1_000,
            "input_tokens_details": {"cached_tokens": 400, "cache_write_tokens": 500},
            "output_tokens": 50,
            "output_tokens_details": {"reasoning_tokens": 40},
        }
    )
    assert usage == Usage(
        input_tokens=100,
        cache_read_input_tokens=400,
        cache_creation_input_tokens=500,
        output_tokens=50,
        thinking_tokens=40,
    )
    assert estimate_cost(usage, ModelRegistry.load_default().get("gpt-6-astra")) == pytest.approx(
        0.01015
    )


@pytest.mark.parametrize(
    "model,thinking,effort,kind,effective",
    [
        ("claude-fable-5-1", False, Effort.max, "adaptive", "low"),
        ("claude-fable-5-1", True, Effort.max, "adaptive", "max"),
        ("claude-opus-5-5", False, Effort.max, "adaptive", "low"),
        ("claude-opus-5-5", True, Effort.max, "adaptive", "max"),
        ("claude-opus-5", False, Effort.max, "disabled", "high"),
        ("claude-opus-5", False, Effort.low, "disabled", "low"),
        ("claude-opus-5", True, Effort.max, "adaptive", "max"),
    ],
)
def test_claude_thinking_and_effort_constraints(model, thinking, effort, kind, effective):
    body = _request_kwargs(
        system=[],
        messages=[],
        tools=[],
        registry=ModelRegistry.load_default(),
        settings=GenerationSettings(model=model, thinking=thinking, effort=effort),
    )
    assert body["thinking"] == {"type": kind}
    assert body["extra_body"]["output_config"]["effort"] == effective


@pytest.mark.parametrize("model_id", ["claude-fable-5-1", "claude-opus-5-5"])
async def test_anthropic_sdk_preserves_empty_signed_blocks_in_order(model_id):
    # Exercise the real SDK's SSE parser, including empty thinking deltas.
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model_id,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        }
    ]
    for index, block in enumerate(
        [
            {"type": "thinking", "thinking": "", "signature": ""},
            {"type": "tool_use", "id": "call1", "name": "roll_dice", "input": {}},
            {"type": "thinking", "thinking": "", "signature": ""},
            {"type": "tool_use", "id": "call2", "name": "roll_dice", "input": {}},
        ]
    ):
        events.append({"type": "content_block_start", "index": index, "content_block": block})
        if block["type"] == "thinking":
            events.extend(
                [
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "thinking_delta", "thinking": ""},
                    },
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "signature_delta", "signature": f"opaque-{index}"},
                    },
                ]
            )
        else:
            events.append(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": '{"expression":"1d20"}'},
                }
            )
        events.append({"type": "content_block_stop", "index": index})
    events.extend(
        [
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 80},
            },
            {"type": "message_stop"},
        ]
    )
    payload = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AnthropicProvider("test")
        provider.client = anthropic.AsyncAnthropic(api_key="test", http_client=client)
        settings = GenerationSettings(model=model_id)
        result = [
            event
            async for event in provider.stream_turn(
                system=[],
                messages=[Message(role="user", content=[TextBlock(text="roll")])],
                tools=[],
                settings=settings,
            )
        ]
        done = result[-1]
        assert done.usage.output_tokens == 80
        assert done.usage.thinking_tokens == 0  # unavailable, never inferred from ciphertext
        assert not any(event.type == "thinking_delta" for event in result)
        assert [block.type for block in done.message.content] == [
            "thinking",
            "tool_use",
            "thinking",
            "tool_use",
        ]
        assert done.message.content[0].signature == "opaque-0"
        assert done.message.content[2].signature == "opaque-2"
        replay = _request_kwargs(
            system=[],
            messages=[done.message],
            tools=[],
            settings=settings,
            registry=provider.registry,
        )["messages"][0]["content"]
        assert [block["type"] for block in replay] == [
            "thinking",
            "tool_use",
            "thinking",
            "tool_use",
        ]
        assert replay[0] == {"type": "thinking", "thinking": "", "signature": "opaque-0"}
        assert requests[0]["model"] == model_id


async def test_gemini_native_replay_preserves_signature_only_parts_and_call_ids(monkeypatch):
    parts = [
        {"thoughtSignature": "opaque-first"},
        {"text": "Checking the rules.", "thoughtSignature": "opaque-text"},
        {
            "functionCall": {"id": "native-42", "name": "search_rules", "args": {"query": "hp"}},
            "thoughtSignature": "opaque-call",
        },
        {"thought": True, "text": "", "thoughtSignature": "opaque-last"},
    ]
    monkeypatch.setattr(
        "openadventure.providers.gemini_provider._stream_chunks",
        lambda *_args: iter(
            [
                {
                    "candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
                    "usageMetadata": {
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 5,
                        "thoughtsTokenCount": 30,
                    },
                }
            ]
        ),
    )
    provider = GeminiProvider("test")
    settings = GenerationSettings(model="gemini-3.8-flash", thinking=False)
    events = [
        event
        async for event in provider.stream_turn(system=[], messages=[], tools=[], settings=settings)
    ]
    done = events[-1]
    call = next(event for event in events if event.type == "tool_use")
    body = provider._request_body(
        system=[],
        messages=[
            done.message,
            Message(role="user", content=[ToolResultBlock(tool_use_id=call.id, content="rules")]),
        ],
        tools=[],
        settings=settings,
    )
    assert body["contents"][0]["parts"] == parts
    assert body["contents"][1]["parts"][0]["functionResponse"]["id"] == "native-42"
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert done.usage.output_tokens == 35
    assert done.usage.thinking_tokens == 30


def test_gemini_resumed_history_has_matching_call_ids():
    body = GeminiProvider("test")._request_body(
        system=[],
        messages=[
            Message(
                role="assistant",
                content=[ToolUseBlock(id="history-1", name="search_rules", input={})],
            ),
            Message(
                role="user", content=[ToolResultBlock(tool_use_id="history-1", content="rules")]
            ),
        ],
        tools=[],
        settings=GenerationSettings(model="gemini-3.8-flash"),
    )
    assert body["contents"][0]["parts"][0]["functionCall"]["id"] == "history-1"
    assert body["contents"][1]["parts"][0]["functionResponse"]["id"] == "history-1"


async def test_engine_preserves_provider_message_and_hides_it_from_player(make_session):
    message = Message(
        role="assistant",
        content=[
            ThinkingBlock(thinking="", signature="opaque-one"),
            ToolUseBlock(id="call1", name="roll_dice", input={"expression": "1d20"}),
            ThinkingBlock(thinking="", signature="opaque-two"),
            TextBlock(text="Private pre-tool chatter."),
        ],
    )
    session = make_session(
        script=[
            [
                PThinking(thinking="", signature="opaque-one"),
                PToolUse(id="call1", name="roll_dice", input={"expression": "1d20"}),
                PThinking(thinking="", signature="opaque-two"),
                PTextDelta(text="Private pre-tool chatter."),
                PTurnDone(
                    stop_reason="tool_use",
                    usage=Usage(input_tokens=200_000, output_tokens=100),
                    message=message,
                ),
            ],
            [
                PTextDelta(text="The door opens."),
                PTurnDone(
                    stop_reason="end_turn", usage=Usage(input_tokens=200_000, output_tokens=100)
                ),
            ],
        ]
    )
    session.set_override("model", "gpt-6-astra")
    events = await collect(session.handle_input("open the door"))
    assert session.provider.calls[1].messages[-2] == message
    assert (
        "".join(event.text for event in events if event.type == "assistant_text_delta")
        == "The door opens."
    )
    assert "opaque" not in "".join(event.model_dump_json() for event in events)
    assert "Private pre-tool" not in str(session.log.read_all())
    # Two short-context calls must not be priced as one 400K long-context call.
    assert session.usage_report()["cost_usd"] == pytest.approx(4.01)


@pytest.mark.parametrize("summary", [None, []])
@pytest.mark.parametrize("model_id", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"])
async def test_gpt6_encrypted_reasoning_without_summary_round_trips(monkeypatch, summary, model_id):
    reasoning = {"type": "reasoning", "id": "rs_opaque", "encrypted_content": "encrypted-only"}
    if summary is not None:
        reasoning["summary"] = summary
    output = [
        reasoning,
        {
            "type": "function_call",
            "call_id": "call_native",
            "name": "roll_dice",
            "arguments": '{"expression":"1d20"}',
        },
    ]
    monkeypatch.setattr(
        "openadventure.providers.openai_provider._stream_events",
        lambda *_args: iter(
            [
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": output,
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 100,
                            "output_tokens_details": {"reasoning_tokens": 90},
                        },
                    },
                }
            ]
        ),
    )
    provider = OpenAIProvider("test")
    events = [
        event
        async for event in provider.stream_turn(
            system=[], messages=[], tools=[], settings=GenerationSettings(model=model_id)
        )
    ]
    thinking = next(event for event in events if event.type == "thinking")
    call = next(event for event in events if event.type == "tool_use")
    assert thinking.thinking == ""
    assert not any(event.type == "thinking_delta" for event in events)
    replay = provider._convert_messages(
        [
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(thinking=thinking.thinking, signature=thinking.signature),
                    ToolUseBlock(id=call.id, name=call.name, input=call.input),
                ],
            )
        ]
    )
    assert replay[0] == {**reasoning, "summary": []}
    assert replay[1]["call_id"] == "call_native"
    assert events[-1].usage == Usage(input_tokens=10, output_tokens=100, thinking_tokens=90)


async def test_template_loop_preserves_native_response_without_thinking_text(tmp_path, monkeypatch):
    from openadventure.ingest.template_gen import derive_template

    monkeypatch.setattr("openadventure.ingest.template_gen.make_rules_tools", lambda _sources: [])
    native = Message(
        role="assistant",
        content=[
            ThinkingBlock(thinking="", signature="template-opaque"),
            ToolUseBlock(id="lookup", name="search_rules", input={"query": "creation"}),
            ThinkingBlock(thinking="", signature="template-progress"),
        ],
    )
    provider = FakeProvider(
        script=[
            [
                PThinking(thinking="", signature="template-opaque"),
                PToolUse(id="lookup", name="search_rules", input={"query": "creation"}),
                PThinking(thinking="", signature="template-progress"),
                PTurnDone(stop_reason="tool_use", message=native),
            ],
            [
                PToolUse(
                    id="save",
                    name="save_template",
                    input={"fields": [], "resources": [], "creation_guide": "Choose a name."},
                ),
                PTurnDone(stop_reason="tool_use"),
            ],
            [PTextDelta(text="Saved."), PTurnDone(stop_reason="end_turn")],
        ]
    )
    result = await derive_template(
        provider, GenerationSettings(model="claude-fable-5-1"), tmp_path, "rules"
    )
    assert result["creation_guide"] == "Choose a name."
    assert provider.calls[1].messages[-2] == native
    assert (tmp_path / "templates" / "character.json").is_file()
