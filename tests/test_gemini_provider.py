"""Gemini adapter: request shaping, schema translation, SSE event mapping."""

import pytest

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
)
from openadventure.providers.factory import build_provider
from openadventure.providers.gemini_provider import GeminiProvider, _tool_parameters


def _provider() -> GeminiProvider:
    return GeminiProvider("test-key", ModelRegistry.load_default())


# --- model -> backend mapping ---------------------------------------------
def test_factory_builds_gemini_backend():
    provider = build_provider("gemini", "k", ModelRegistry.load_default())
    assert isinstance(provider, GeminiProvider)


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError):
        build_provider("ollama", "k", ModelRegistry.load_default())


def test_registry_maps_model_to_backend():
    registry = ModelRegistry.load_default()
    assert registry.provider_for("gemini-3.5-flash") == "gemini"
    assert registry.provider_for("claude-opus-4-8") == "anthropic"
    # unknown ids are inferred from the vendor's id convention
    assert registry.provider_for("gemini-9-ultra") == "gemini"
    assert registry.provider_for("claude-future") == "anthropic"


def test_model_selects_backend_via_settings():
    registry = ModelRegistry.load_default()
    # default (no model set) -> overall default is Claude Sonnet 5 -> anthropic backend
    default = resolve_settings({}, AppConfig(workspace_dir="."), registry)
    assert default.model == "claude-sonnet-5"
    assert registry.provider_for(default.model) == "anthropic"
    # config model picks the model, which picks the backend
    claude = resolve_settings({}, AppConfig(workspace_dir=".", model="claude-opus-4-8"), registry)
    assert claude.model == "claude-opus-4-8"
    assert registry.provider_for(claude.model) == "anthropic"
    # a per-campaign override wins over config
    over = resolve_settings(
        {"model": "claude-opus-4-8"},
        AppConfig(workspace_dir=".", model="gemini-3.5-flash"),
        registry,
    )
    assert over.model == "claude-opus-4-8"


def test_utility_model_picks_its_own_backend():
    from openadventure.engine.session import resolve_utility_settings

    registry = ModelRegistry.load_default()
    # default utility model is Claude Sonnet 5 -> anthropic, independent of the campaign model
    default = resolve_utility_settings(AppConfig(workspace_dir=".", model="gemini-3.5-flash"))
    assert default.model == "claude-sonnet-5"
    assert registry.provider_for(default.model) == "anthropic"
    # pin a gemini utility model -> runs on the gemini backend
    pinned = resolve_utility_settings(
        AppConfig(workspace_dir=".", utility={"model": "gemini-3.5-flash"})
    )
    assert registry.provider_for(pinned.model) == "gemini"


# --- request shaping ------------------------------------------------------
def test_request_uses_minimal_thinking_when_thinking_off():
    # Flash supports "minimal" (Gemini's "no thinking" floor), so thinking off
    # maps there rather than to "low".
    body = _provider()._request_body(
        system=[SystemBlock(text="be a GM")],
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        settings=GenerationSettings(model="gemini-3.5-flash", thinking=False),
    )
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert body["systemInstruction"] == {"parts": [{"text": "be a GM"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def _level(model: str, *, thinking: bool, effort: Effort = Effort.low) -> str:
    body = _provider()._request_body(
        system=[],
        messages=[],
        tools=[],
        settings=GenerationSettings(model=model, thinking=thinking, effort=effort),
    )
    return body["generationConfig"]["thinkingConfig"]["thinkingLevel"]


def test_thinking_on_folds_effort_into_the_level():
    # With thinking on, effort drives the depth (and always clears the off-state
    # floor): low/medium -> "medium", high/max -> "high".
    assert _level("gemini-3.5-flash", thinking=True, effort=Effort.low) == "medium"
    assert _level("gemini-3.5-flash", thinking=True, effort=Effort.medium) == "medium"
    assert _level("gemini-3.5-flash", thinking=True, effort=Effort.high) == "high"
    assert _level("gemini-3.5-flash", thinking=True, effort=Effort.max) == "high"


def test_thinking_level_clamps_to_what_the_model_supports():
    # 3.x Pro rejects "minimal", so thinking off falls back to its lowest level.
    assert _level("gemini-3.1-pro-preview", thinking=False) == "low"
    assert _level("gemini-3.5-flash", thinking=False) == "minimal"
    # deep levels are unaffected by the clamp.
    assert _level("gemini-3.1-pro-preview", thinking=True, effort=Effort.high) == "high"
    # an unknown gemini id uses the safe ["low", "high"] default -> off=low.
    assert _level("gemini-9-ultra", thinking=False) == "low"
    assert _level("gemini-9-ultra", thinking=True, effort=Effort.high) == "high"


def test_max_tokens_capped_to_model_output():
    body = _provider()._request_body(
        system=[],
        messages=[],
        tools=[],
        settings=GenerationSettings(model="gemini-3.5-flash", max_tokens=10_000_000),
    )
    assert body["generationConfig"]["maxOutputTokens"] == 65536


def test_tools_become_function_declarations():
    tool = ToolDef(
        name="roll",
        description="roll dice",
        input_schema={
            "type": "object",
            "title": "RollArgs",
            "properties": {"expr": {"type": "string", "title": "Expr"}},
            "required": ["expr"],
            "additionalProperties": False,
        },
    )
    body = _provider()._request_body(
        system=[], messages=[], tools=[tool], settings=GenerationSettings(model="gemini-3.5-flash")
    )
    [decl] = body["tools"][0]["functionDeclarations"]
    assert decl["name"] == "roll"
    params = decl["parameters"]
    assert "title" not in params
    assert "additionalProperties" not in params
    assert "title" not in params["properties"]["expr"]


# --- schema translation ---------------------------------------------------
def test_schema_inlines_refs_and_normalises():
    schema = {
        "type": "object",
        "$defs": {
            "Op": {"type": "object", "properties": {"k": {"const": "set"}}},
        },
        "properties": {
            "op": {"$ref": "#/$defs/Op"},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }
    params = _tool_parameters(schema)
    assert "$defs" not in params
    assert params["properties"]["op"]["properties"]["k"] == {"enum": ["set"], "type": "string"}
    note = params["properties"]["note"]
    assert note["type"] == "string"
    assert note["nullable"] is True


def test_real_tool_schemas_have_no_unsupported_keywords(workspace, campaign):
    import json

    from openadventure.engine.tools import build_registry

    def walk(node, seen):
        if isinstance(node, dict):
            seen.update(node)
            for v in node.values():
                walk(v, seen)
        elif isinstance(node, list):
            for v in node:
                walk(v, seen)

    registry = build_registry(workspace, campaign, campaign.load_meta())
    for tdef in registry.defs():
        params = _tool_parameters(tdef.input_schema)
        seen: set = set()
        walk(params, seen)
        forbidden = seen & {"$ref", "$defs", "const", "additionalProperties", "title"}
        assert not forbidden, f"{tdef.name} leaked {forbidden}"
        # no bare null types survive (Optionals become nullable)
        assert '"type": "null"' not in json.dumps(params)


# --- message round-tripping -----------------------------------------------
def test_tool_use_and_result_round_trip_with_signature():
    provider = _provider()
    # seed the caches the way a streamed function call would
    provider._tool_names["gem-1"] = "search_rules"
    provider._tool_signatures["gem-1"] = "sig-xyz"

    contents = provider._convert_messages(
        [
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(thinking="consider", signature="t-sig"),
                    ToolUseBlock(id="gem-1", name="search_rules", input={"query": "hp"}),
                ],
            ),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="gem-1", content="HP rules…")],
            ),
        ]
    )
    model_turn = contents[0]
    assert model_turn["role"] == "model"
    thought, call = model_turn["parts"]
    assert thought == {"text": "consider", "thought": True, "thoughtSignature": "t-sig"}
    assert call["functionCall"] == {"name": "search_rules", "args": {"query": "hp"}}
    assert call["thoughtSignature"] == "sig-xyz"

    response_turn = contents[1]
    assert response_turn["role"] == "user"
    assert response_turn["parts"][0]["functionResponse"] == {
        "name": "search_rules",
        "response": {"result": "HP rules…"},
    }


def test_replayed_tool_pair_names_response_without_seeding():
    """A tool round replayed from history (synthetic id, never streamed by this
    provider, no cached name/signature) still names its functionResponse correctly:
    the name is learned from the tool_use block in the same request."""
    provider = _provider()  # caches empty, as on a fresh process resuming a campaign
    contents = provider._convert_messages(
        [
            Message(
                role="assistant",
                content=[ToolUseBlock(id="call-42", name="search_campaign", input={"query": "x"})],
            ),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="call-42", content="ROOM 1…")],
            ),
        ]
    )
    call = contents[0]["parts"][0]
    assert call["functionCall"]["name"] == "search_campaign"
    assert "thoughtSignature" not in call  # no signature replayed for past turns
    response = contents[1]["parts"][0]["functionResponse"]
    assert response == {"name": "search_campaign", "response": {"result": "ROOM 1…"}}


def test_tool_error_result_uses_error_field():
    provider = _provider()
    provider._tool_names["gem-2"] = "update_scene"
    [turn] = provider._convert_messages(
        [
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="gem-2", content="boom", is_error=True)],
            )
        ]
    )
    assert turn["parts"][0]["functionResponse"]["response"] == {"error": "boom"}


# --- streaming ------------------------------------------------------------
async def _drain(provider, chunks):
    """Run stream_turn against a stubbed SSE source."""
    import openadventure.providers.gemini_provider as gp

    def fake_stream(url, body, key):
        yield from chunks

    original = gp._stream_chunks
    gp._stream_chunks = fake_stream
    try:
        events = []
        async for event in provider.stream_turn(
            system=[],
            messages=[Message(role="user", content=[TextBlock(text="go")])],
            tools=[],
            settings=GenerationSettings(model="gemini-3.5-flash"),
        ):
            events.append(event)
        return events
    finally:
        gp._stream_chunks = original


async def test_stream_emits_text_then_turn_done():
    chunks = [
        {"candidates": [{"content": {"parts": [{"text": "You enter "}]}}]},
        {
            "candidates": [{"content": {"parts": [{"text": "a cave."}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
        },
    ]
    events = await _drain(_provider(), chunks)
    text = "".join(e.text for e in events if e.type == "text_delta")
    assert text == "You enter a cave."
    done = events[-1]
    assert done.type == "turn_done"
    assert done.stop_reason == "end_turn"
    assert done.usage.input_tokens == 100
    assert done.usage.output_tokens == 20


async def test_stream_maps_function_call_to_tool_use():
    chunks = [
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {"name": "roll_dice", "args": {"expr": "1d20"}},
                                "thoughtSignature": "sig-1",
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 5},
        }
    ]
    provider = _provider()
    events = await _drain(provider, chunks)
    starts = [e for e in events if e.type == "tool_use_start"]
    uses = [e for e in events if e.type == "tool_use"]
    assert starts[0].name == "roll_dice"
    assert uses[0].input == {"expr": "1d20"}
    # a function-calling turn reports tool_use regardless of Gemini's STOP
    assert events[-1].stop_reason == "tool_use"
    # the signature is cached for the next round under the synthetic id
    assert provider._tool_signatures[uses[0].id] == "sig-1"


# --- connect_provider / live model->backend switch ------------------------
def test_connect_provider_follows_the_model(make_session, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    session = make_session(script=[])

    session.set_override("model", "claude-opus-4-8")
    assert session.connect_provider() is True
    assert session.provider_name() == "anthropic"

    session.set_override("model", "gemini-3.5-flash")
    assert session.connect_provider() is True
    assert session.provider_name() == "gemini"
    assert isinstance(session.provider, GeminiProvider)


def test_connect_provider_without_key_disconnects(make_session, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    session = make_session(script=[])
    session.set_override("model", "gemini-3.5-flash")
    assert session.connect_provider() is False
    assert session.provider is None


async def test_cmd_model_switches_backend(make_session, monkeypatch):
    # The default model is Claude (anthropic); switching to a Gemini model flips
    # the backend.
    from io import StringIO

    from rich.console import Console

    from openadventure.cli.repl import Repl
    from openadventure.providers.gemini_provider import GeminiProvider

    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    session = make_session(script=[])
    assert session.provider_name() == "anthropic"  # the new default
    out = StringIO()
    repl = Repl(Console(file=out, width=200), session)

    await repl._cmd_model("gemini-3.5-flash")
    assert session.settings.model == "gemini-3.5-flash"
    assert isinstance(session.provider, GeminiProvider)
    assert "Backend switched to gemini" in out.getvalue()


async def test_cmd_model_switch_without_key_warns(make_session, monkeypatch):
    from io import StringIO

    from rich.console import Console

    from openadventure.cli.repl import Repl

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    session = make_session(script=[])
    out = StringIO()
    repl = Repl(Console(file=out, width=200), session)

    await repl._cmd_model("gemini-3.5-flash")  # gemini, but no key set
    assert session.provider is None
    assert "no API key" in out.getvalue()


async def test_stream_collects_thinking_into_one_block():
    chunks = [
        {"candidates": [{"content": {"parts": [{"text": "hmm ", "thought": True}]}}]},
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "ok", "thought": True, "thoughtSignature": "ts"},
                            {"text": "Done."},
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        },
    ]
    events = await _drain(_provider(), chunks)
    thinking = [e for e in events if e.type == "thinking"]
    assert len(thinking) == 1
    assert thinking[0].thinking == "hmm ok"
    assert thinking[0].signature == "ts"
