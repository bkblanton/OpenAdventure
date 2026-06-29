"""read_docs self-knowledge tool: section assembly, dispatch, and registration."""

from openadventure.engine.self_knowledge import build_docs, make_read_docs_tool
from openadventure.engine.tools import build_registry
from tests.test_sheet_tools import make_ctx


def test_build_docs_includes_readme_by_default():
    docs = build_docs()
    # The README is sourced from package metadata (or the repo file); the product
    # name is a stable anchor in it.
    assert "about" in docs
    assert "OpenAdventure" in docs["about"]
    assert "cli" not in docs and "commands" not in docs


def test_build_docs_merges_frontend_help():
    docs = build_docs(
        cli_help="usage: openadventure ...", slash_help="Session\n  /help  show this help"
    )
    assert set(docs) == {"about", "cli", "commands"}
    assert docs["cli"] == "usage: openadventure ..."
    assert "/help" in docs["commands"]


def test_build_docs_ignores_blank_frontend_help():
    docs = build_docs(cli_help="   ", slash_help="")
    assert "cli" not in docs and "commands" not in docs


def test_read_docs_overview_leads_with_about_and_points_at_rest(workspace, campaign):
    docs = build_docs(cli_help="usage: openadventure", slash_help="/help  show this help")
    registry = build_registry(workspace, campaign, campaign.load_meta(), docs=docs)
    ctx = make_ctx(workspace, campaign)

    out = registry.dispatch(ctx, "read_docs", {})
    assert out.ok
    assert "OpenAdventure" in out.content
    # the menu points the model at the command-specific sections
    assert "section='cli'" in out.content and "section='commands'" in out.content


def test_read_docs_returns_requested_section(workspace, campaign):
    docs = build_docs(cli_help="usage: openadventure", slash_help="/help  show this help")
    registry = build_registry(workspace, campaign, campaign.load_meta(), docs=docs)
    ctx = make_ctx(workspace, campaign)

    out = registry.dispatch(ctx, "read_docs", {"section": "commands"})
    assert out.ok and "/help" in out.content
    assert out.summary == "docs: commands"


def test_read_docs_unknown_section_lists_available(workspace, campaign):
    docs = build_docs(cli_help="usage: openadventure")  # no slash_help -> no commands
    registry = build_registry(workspace, campaign, campaign.load_meta(), docs=docs)
    ctx = make_ctx(workspace, campaign)

    out = registry.dispatch(ctx, "read_docs", {"section": "commands"})
    assert not out.ok
    assert "about" in out.content and "cli" in out.content


def test_read_docs_reports_when_no_docs(workspace, campaign):
    # An explicitly empty docs map (no README, no frontend help) -> graceful error.
    tool = make_read_docs_tool({})
    ctx = make_ctx(workspace, campaign)
    out = tool.handler(ctx, tool.args_model())
    assert not out.ok and "isn't available" in out.content


def test_read_docs_registered_and_read_only(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    assert "read_docs" in registry
    # pure retrieval: must be offered in a /btw read-only aside
    assert any(d.name == "read_docs" for d in registry.read_only_defs())


def test_commands_help_text_covers_every_group():
    from openadventure.cli.repl import Repl, commands_help_text

    text = commands_help_text()
    for header, _ in Repl.HELP_GROUPS:
        assert header in text
    assert "/help" in text and "/undo" in text


def test_settings_summary_names_the_model_and_dials(make_session):
    session = make_session()
    summary = session.settings_summary()
    assert session.settings.model in summary
    # every per-campaign dial is named by value...
    for label in ("Model", "Effort", "Thinking", "verbosity", "Context budget"):
        assert label in summary
    # ...but no frontend-specific commands (slash commands are CLI-only)
    assert "/" not in summary


def test_live_settings_ride_in_the_turn_context(make_session):
    session = make_session()
    messages, _ = session.build_messages()
    context_text = "\n".join(b.text for m in messages for b in m.content if b.type == "text")
    assert "Session settings" in context_text
    # the actual model id is present, so "what model powers you?" is answerable
    assert session.settings.model in context_text
