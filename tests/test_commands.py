"""The UI-agnostic command layer: parsing + session mutation + semantic results,
exercised without any frontend."""

from openadventure.engine import commands
from openadventure.engine.commands import Severity


def _only(result):
    assert len(result.messages) == 1, result.messages
    return result.messages[0]


# --- generation settings ----------------------------------------------------


def test_effort_show_and_set(make_session):
    session = make_session(script=[])
    shown = _only(commands.cmd_effort(session, ""))
    assert shown.severity is Severity.info and "Current effort" in shown.text

    done = _only(commands.cmd_effort(session, "high"))
    assert done.severity is Severity.success and "effort=high" in done.text
    assert session.settings.effort == "high"


def test_effort_rejects_bad_value_with_usage(make_session):
    session = make_session(script=[])
    msg = _only(commands.cmd_effort(session, "turbo"))
    assert msg.severity is Severity.error and "/effort low|medium|high|max" in msg.text


def test_thinking_toggle_and_hint(make_session):
    session = make_session(script=[])
    # Thinking is on by default; an unrecognized arg returns the state hint.
    assert "Thinking is on" in _only(commands.cmd_thinking(session, "maybe")).text
    commands.cmd_thinking(session, "off")
    assert session.settings.thinking is False
    commands.cmd_thinking(session, "on")
    assert session.settings.thinking is True


def test_context_parses_human_sizes(make_session):
    session = make_session(script=[])
    commands.cmd_context(session, "200k")
    assert session.settings.context_budget == 200_000
    bad = _only(commands.cmd_context(session, "lots"))
    assert bad.severity is Severity.error and "Couldn't parse" in bad.text


def test_model_show_returns_payload_not_messages(make_session):
    session = make_session(script=[])
    result = commands.cmd_model(session, "")
    assert result.messages == []
    assert isinstance(result.data, commands.ModelList)
    assert result.data.current == session.settings.model
    ids = {model.id for model in result.data.models}
    assert "gemini-3.6-flash" in ids
    assert "gemini-3.5-flash" not in ids
    assert "gemini-3.1-pro-preview" not in ids


def test_model_set_flags_backend_switch(make_session):
    session = make_session(script=[])  # default anthropic backend, provider connected
    result = commands.cmd_model(session, "gemini-3.6-flash")
    assert isinstance(result.data, commands.ModelChanged)
    assert result.data.backend == "gemini"
    assert result.data.switched is True and result.data.needs_provider is True
    assert session.settings.model == "gemini-3.6-flash"


def test_model_set_same_backend_connected_needs_no_provider(make_session):
    session = make_session(script=[])  # connected on the OpenAI default
    result = commands.cmd_model(session, "gpt-5.6-sol")
    assert result.data.switched is False and result.data.needs_provider is False


# --- campaign knobs ---------------------------------------------------------


def test_premise_set_show_clear(make_session):
    session = make_session(script=[])  # fixture seeds "a one-room dungeon"
    assert "a one-room dungeon" in _only(commands.cmd_premise(session, "")).text

    saved = _only(commands.cmd_premise(session, "a drowned elven city"))
    assert saved.severity is Severity.success
    assert session.campaign.load_meta().premise == "a drowned elven city"

    cleared = _only(commands.cmd_premise(session, "clear"))
    assert cleared.severity is Severity.warning
    assert session.meta.premise is None


def test_mode_set_and_invalid(make_session):
    session = make_session(script=[])
    assert "Current mode" in _only(commands.cmd_mode(session, "bogus")).text
    assert _only(commands.cmd_mode(session, "assistant")).severity is Severity.success
    assert session.meta.mode == "assistant"
    commands.cmd_mode(session, "dm")  # legacy alias -> gm
    assert session.meta.mode == "gm"


# --- timeline ---------------------------------------------------------------


def test_undo_with_nothing_to_undo_errors(make_session):
    session = make_session(script=[])
    msg = _only(commands.cmd_undo(session, ""))
    assert msg.severity is Severity.error and "no turns" in msg.text


def test_restart_requires_confirm(make_session):
    session = make_session(script=[])
    usage = _only(commands.cmd_restart(session, ""))
    assert "/restart original" in usage.text
    needs_confirm = _only(commands.cmd_restart(session, "reroll"))
    assert "/restart reroll confirm" in needs_confirm.text
    done = _only(commands.cmd_restart(session, "original confirm"))
    assert done.severity is Severity.success and "Campaign restarted" in done.text


# --- sources ----------------------------------------------------------------


def _ingest_source(session, name: str):
    d = session.workspace.book_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text("{}", encoding="utf-8")
    return d


def test_sources_show_returns_view(make_session):
    session = make_session(script=[])
    _ingest_source(session, "dnd5e")
    session.add_source("dnd5e")
    result = commands.cmd_sources(session, "")
    assert isinstance(result.data, commands.SourcesView)
    assert result.data.attached == ["dnd5e"] and result.data.available == ["dnd5e"]


def test_sources_set_list_and_template_note(make_session):
    session = make_session(script=[])
    _ingest_source(session, "dnd5e")
    result = commands.cmd_sources(session, "dnd5e")
    assert session.meta.sources == ["dnd5e"]
    texts = " ".join(m.text for m in result.messages)
    assert "Sources set to dnd5e" in texts
    assert "openadventure template dnd5e" in texts  # missing-template hint rides along


def test_sources_prefix_match_and_unknown(make_session):
    session = make_session(script=[])
    _ingest_source(session, "call-of-cthulhu")
    commands.cmd_sources(session, "call")  # prefix match
    assert session.meta.sources == ["call-of-cthulhu"]

    rejected = _only(commands.cmd_sources(session, "pathfinder"))
    assert rejected.severity is Severity.error and "No source matches" in rejected.text


def test_sources_clear(make_session):
    session = make_session(script=[])
    _ingest_source(session, "dnd5e")
    session.add_source("dnd5e")
    msg = _only(commands.cmd_sources(session, "none"))
    assert msg.severity is Severity.warning and session.meta.sources == []


# --- modules ----------------------------------------------------------------


def test_modules_empty_then_add_then_view(make_session):
    session = make_session(script=[])
    assert "No modules attached" in _only(commands.cmd_modules(session, "")).text

    _ingest_source(session, "death-house")
    added = _only(commands.cmd_modules(session, "add death-house"))
    assert added.severity is Severity.success and "Attached module death-house" in added.text

    view = commands.cmd_modules(session, "").data
    assert isinstance(view, commands.ModulesView)
    assert [m.slug for m in view.modules] == ["death-house"]
    assert view.modules[0].status == "active"  # first attached module starts NOW PLAYING


def test_modules_arc_and_unknown_verb(make_session):
    session = make_session(script=[])
    assert _only(commands.cmd_modules(session, "arc a war brews")).severity is Severity.success
    assert session.meta.arc == "a war brews"
    assert "Usage:" in _only(commands.cmd_modules(session, "frobnicate")).text


# --- registry ---------------------------------------------------------------


def test_run_dispatches_known_and_skips_others(make_session):
    session = make_session(script=[])
    assert commands.run(session, "premise", "") is not None
    assert commands.run(session, "/effort", "") is not None  # leading slash tolerated
    assert commands.run(session, "tts", "") is None  # media is frontend-owned
    assert commands.run(session, "nonexistent", "") is None
