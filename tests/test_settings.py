"""GenerationSettings: defaults, overrides, persistence, context budget."""

import pytest

from openadventure.engine.context import ContextBudget
from openadventure.providers.base import (
    HIGH_EFFORT_SETTINGS,
    Effort,
    GenerationSettings,
    ModelRegistry,
    Verbosity,
)


def test_registry_loads_and_has_models():
    registry = ModelRegistry.load_default()
    sonnet = registry.get("claude-sonnet-5")
    assert sonnet.context_window == 1_000_000
    assert sonnet.supports_effort
    fable = registry.get("claude-fable-5")
    assert fable.supports_thinking
    assert fable.output_per_mtok == 50.0
    assert "claude-haiku-4-5" in {m.id for m in registry.models}
    flash = registry.get("gemini-3.6-flash")
    assert flash.context_window == 1_048_576
    assert flash.max_output == 65_536
    assert (flash.input_per_mtok, flash.output_per_mtok) == (1.5, 7.5)


def test_deprecated_models_resolve_but_are_hidden_from_lists():
    registry = ModelRegistry.load_default()
    # Superseded models remain resolvable when pinned for campaign compatibility.
    old = registry.get("claude-sonnet-4-6")
    old_flash = registry.get("gemini-3.5-flash")
    old_pro = registry.get("gemini-3.1-pro-preview")
    assert old.deprecated is old_flash.deprecated is old_pro.deprecated is True
    assert registry.provider_for("claude-sonnet-4-6") == "anthropic"
    assert registry.provider_for("gemini-3.5-flash") == "gemini"
    assert registry.provider_for("gemini-3.1-pro-preview") == "gemini"
    # Deprecated models are kept out of both frontends' shared visible list.
    visible_ids = {m.id for m in registry.visible}
    assert "claude-sonnet-4-6" not in visible_ids
    assert "gemini-3.5-flash" not in visible_ids
    assert "gemini-3.1-pro-preview" not in visible_ids
    assert "claude-sonnet-5" in visible_ids
    assert "gemini-3.6-flash" in visible_ids


def test_registry_unknown_model_gets_safe_defaults():
    info = ModelRegistry.load_default().get("gpt-99-turbo")
    assert info.id == "gpt-99-turbo"
    assert info.context_window == 200_000


def test_default_settings_are_accuracy_first():
    # The default table: GPT-5.6 Luna, high effort, thinking on (no presets).
    s = GenerationSettings()
    assert s.model == "gpt-5.6-luna"
    assert s.effort == Effort.high
    assert s.thinking is True
    assert s.verbosity == Verbosity.medium


def test_high_effort_settings_are_accuracy_first_and_separate_from_the_table():
    # Off-hot-path work (template derivation, the canon chronicler) is not
    # real-time, so unlike the default table it turns thinking ON at high effort,
    # even though it runs the same GPT-5.6 Luna the table does. It stays on the
    # OpenAI backend (the in-game default) so one OpenAI key serves the
    # table and these jobs.
    assert HIGH_EFFORT_SETTINGS.thinking is True
    assert HIGH_EFFORT_SETTINGS.model == "gpt-5.6-luna"
    assert ModelRegistry.load_default().provider_for(HIGH_EFFORT_SETTINGS.model) == "openai"
    assert HIGH_EFFORT_SETTINGS.effort == Effort.high
    # generous output room for a full template plus its thinking
    assert HIGH_EFFORT_SETTINGS.max_tokens >= 16_000


def test_resolve_utility_settings_uses_config_override():
    from openadventure.config import AppConfig
    from openadventure.engine.session import resolve_utility_settings

    config = AppConfig(
        workspace_dir="/tmp/ws", utility={"model": "claude-sonnet-4-6", "effort": "medium"}
    )
    settings = resolve_utility_settings(config)
    assert settings.model == "claude-sonnet-4-6"
    assert settings.effort == Effort.medium
    assert settings.thinking is True  # untouched fields keep the default


def test_resolve_utility_settings_ignores_campaign_model():
    from openadventure.config import AppConfig
    from openadventure.engine.session import resolve_utility_settings

    # The campaign's default model must not bleed into off-hot-path work.
    config = AppConfig(workspace_dir="/tmp/ws", model="claude-fable-5")
    settings = resolve_utility_settings(config)
    assert settings == HIGH_EFFORT_SETTINGS


def test_load_config_reads_legacy_utility_sections(tmp_path):
    # Existing workspaces keep working: [utility] is the current name, but the
    # loader falls back through the old [high_effort] and [template] names, most
    # specific first.
    from openadventure.config import load_config

    (tmp_path / "config.toml").write_text(
        '[template]\nmodel = "claude-opus-4-8"\n', encoding="utf-8"
    )
    assert load_config(tmp_path).utility == {"model": "claude-opus-4-8"}

    (tmp_path / "config.toml").write_text(
        '[template]\nmodel = "legacy"\n[high_effort]\nmodel = "mid"\n', encoding="utf-8"
    )
    assert load_config(tmp_path).utility == {"model": "mid"}

    (tmp_path / "config.toml").write_text(
        '[template]\nmodel = "legacy"\n[high_effort]\nmodel = "mid"\n[utility]\nmodel = "new"\n',
        encoding="utf-8",
    )
    assert load_config(tmp_path).utility == {"model": "new"}


def test_set_utility_model_creates_file_from_default_when_missing(tmp_path):
    # No config.toml yet: the writer seeds one and records the choice, so a fresh
    # workspace gets a reliable way to set the out-of-game utility model.
    from openadventure.config import load_config, set_utility_model

    config = load_config(tmp_path)
    assert config.utility == {}

    assert set_utility_model(config, "claude-opus-4-8") is True
    assert config.utility["model"] == "claude-opus-4-8"  # live config updated
    assert load_config(tmp_path).utility["model"] == "claude-opus-4-8"  # and persisted
    # idempotent: re-setting the same model is a no-op
    assert set_utility_model(config, "claude-opus-4-8") is False


def test_set_utility_model_patches_existing_table_in_place(tmp_path):
    from openadventure.config import load_config, set_utility_model

    (tmp_path / "config.toml").write_text(
        '[provider]\nmodel = "claude-fable-5"\n\n[utility]\nmodel = "old-model"\neffort = "high"\n',
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert set_utility_model(config, "gemini-3.5-flash") is True

    reloaded = load_config(tmp_path)
    # only the utility model changed; its other keys and other tables survive
    assert reloaded.utility == {"model": "gemini-3.5-flash", "effort": "high"}
    assert reloaded.model == "claude-fable-5"


def test_set_utility_model_ignores_commented_default_example(tmp_path):
    # The shipped config.toml ships a *commented* [utility] example; setting the
    # model must add a real table, not edit the documentation.
    from openadventure.config import DEFAULT_CONFIG_TOML, load_config, set_utility_model

    (tmp_path / "config.toml").write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    config = load_config(tmp_path)
    assert config.utility == {}  # the commented example is not parsed

    assert set_utility_model(config, "claude-sonnet-4-6") is True
    assert load_config(tmp_path).utility["model"] == "claude-sonnet-4-6"
    # the documentation block is preserved
    assert "default model for out-of-game jobs" in (tmp_path / "config.toml").read_text(
        encoding="utf-8"
    )


def test_provider_for_settings_reuses_chat_provider_on_matching_backend(make_session):
    # The chronicler's settings run on the same OpenAI backend as the table
    # default, so it reuses the live provider rather than building a second one.
    session = make_session(script=[])
    assert session.provider_for_settings(HIGH_EFFORT_SETTINGS) is session.provider


def test_thinking_blocks_round_trip_through_anthropic_conversion():
    from openadventure.providers.anthropic_provider import _convert_messages
    from openadventure.providers.base import Message, ThinkingBlock, ToolUseBlock

    msg = Message(
        role="assistant",
        content=[
            ThinkingBlock(thinking="reason about the rules", signature="abc"),
            ToolUseBlock(id="t1", name="search_rules", input={"query": "hp"}),
        ],
    )
    [converted] = _convert_messages([msg])
    blocks = converted["content"]
    assert blocks[0] == {
        "type": "thinking",
        "thinking": "reason about the rules",
        "signature": "abc",
    }
    assert blocks[1]["type"] == "tool_use"


def test_merged_ignores_unknown_keys():
    s = GenerationSettings().merged(
        {"model": "x", "quality": "high", "verbosity": "low", "bogus": 1}
    )
    assert s.model == "x"
    assert s.verbosity == Verbosity.low


def test_context_budget_respects_model_window():
    registry = ModelRegistry.load_default()
    # an unknown model gets the 200k safe-default window, which caps the budget
    settings = GenerationSettings(context_budget=800_000, model="legacy-200k-model")
    budget = ContextBudget.from_settings(settings, registry.get("legacy-200k-model"))
    assert budget.total <= 200_000
    assert budget.tail_for(10_000) < budget.total

    big = GenerationSettings(context_budget=800_000, model="claude-sonnet-4-6")
    budget_big = ContextBudget.from_settings(big, registry.get("claude-sonnet-4-6"))
    assert budget_big.total == int(800_000 * 0.85)


def test_tail_for_shrinks_as_rendered_context_grows():
    from openadventure.engine.context import MIN_TAIL

    registry = ModelRegistry.load_default()
    settings = GenerationSettings(context_budget=200_000, model="claude-sonnet-4-6")
    budget = ContextBudget.from_settings(settings, registry.get("claude-sonnet-4-6"))

    # the tail is whatever's left after the measured non-tail input
    assert budget.tail_for(10_000) == budget.total - 10_000
    # a bigger context block leaves a smaller tail
    assert budget.tail_for(50_000) < budget.tail_for(10_000)
    # never collapses below the floor, even when the rest nearly fills the budget
    assert budget.tail_for(budget.total) == MIN_TAIL
    assert budget.tail_for(budget.total + 1_000_000) == MIN_TAIL


def test_tool_schema_tokens_counts_serialized_defs():
    from openadventure.engine.context import tool_schema_tokens
    from openadventure.providers.base import ToolDef

    assert tool_schema_tokens([]) == 0
    tools = [
        ToolDef(name="roll", description="Roll dice", input_schema={"type": "object"}),
        ToolDef(
            name="search",
            description="Search the campaign library",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        ),
    ]
    # non-empty toolset costs tokens, and a bigger toolset costs more
    assert tool_schema_tokens(tools) > 0
    assert tool_schema_tokens(tools) > tool_schema_tokens(tools[:1])


def test_session_set_override_persists(make_session):
    session = make_session(script=[])

    settings = session.set_override("model", "claude-fable-5")
    assert settings.model == "claude-fable-5"

    settings = session.set_override("effort", "high")
    assert settings.effort == Effort.high
    assert settings.model == "claude-fable-5"  # other overrides retained

    settings = session.set_override("context_budget", "200000")
    assert settings.context_budget == 200_000

    settings = session.set_override("thinking", "on")
    assert settings.thinking is True

    settings = session.set_override("verbosity", "low")
    assert settings.verbosity == Verbosity.low

    # persisted: a fresh meta load sees the overrides
    meta = session.campaign.load_meta()
    assert meta.settings["model"] == "claude-fable-5"
    assert meta.settings["effort"] == "high"
    assert meta.settings["verbosity"] == "low"

    with pytest.raises(ValueError):
        session.set_override("effort", "ludicrous")
    with pytest.raises(ValueError):
        session.set_override("nonsense", 1)
    # quality is no longer a setting
    with pytest.raises(ValueError):
        session.set_override("quality", "high")


def test_set_premise_trims_persists_and_clears(make_session):
    session = make_session(script=[])

    assert session.set_premise("  a daring vault heist  ") == "a daring vault heist"
    assert session.campaign.load_meta().premise == "a daring vault heist"

    assert session.set_premise("   ") is None  # whitespace clears
    assert session.campaign.load_meta().premise is None

    assert session.set_premise(None) is None


def test_add_source_slugifies_persists_and_reloads_tools(make_session):
    session = make_session(script=[])

    assert session.add_source("D&D 5e") == "d-d-5e"  # stored as a slug
    assert session.campaign.load_meta().sources == ["d-d-5e"]
    assert session.meta.sources == ["d-d-5e"]
    assert session.meta.system_source == "d-d-5e"  # first source becomes the system source

    session.clear_sources()
    assert session.campaign.load_meta().sources == []
    assert session.campaign.load_meta().system_source is None


def test_premise_rides_in_the_context_block(make_session):
    session = make_session(script=[])
    session.set_premise("the moon has cracked open")
    messages, _ = session.build_messages()
    context = messages[0].content[0].text
    assert "the moon has cracked open" in context


def test_verbosity_goes_into_campaign_prompt(make_session):
    session = make_session(script=[])
    session.set_override("verbosity", "low")
    system = session.build_system()[0].text
    assert "Response verbosity: low" in system
    assert "one or two sentences" in system


def test_anthropic_request_does_not_send_verbosity():
    from openadventure.providers.anthropic_provider import _request_kwargs

    kwargs = _request_kwargs(
        system=[],
        messages=[],
        tools=[],
        # an Anthropic model (supports effort) so output_config is populated
        settings=GenerationSettings(
            model="claude-opus-4-8", effort=Effort.low, verbosity=Verbosity.low
        ),
        registry=ModelRegistry.load_default(),
    )
    assert kwargs["extra_body"]["output_config"] == {"effort": "low"}


def test_new_campaign_parser_keeps_mode_and_sources():
    from openadventure.cli.main import build_parser

    args = build_parser().parse_args(
        ["new", "Stone Quest", "--mode", "assistant", "--source", "dnd5e", "--source", "mm"]
    )
    assert args.mode == "assistant"
    assert args.source == ["dnd5e", "mm"]
    # premise and verbosity were dropped from `new` (set in play instead)
    assert not hasattr(args, "premise")
    assert not hasattr(args, "verbosity")


def test_new_campaign_parser_rejects_dropped_flags():
    from openadventure.cli.main import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["new", "Stone Quest", "--premise", "x"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["new", "Stone Quest", "--verbosity", "low"])
