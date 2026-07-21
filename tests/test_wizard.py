"""Setup-wizard steps: sources, premise, and module selection.

The individual steps don't gate on a tty (only ``run_setup_wizard`` does), so
each is driven directly with a scripted ``_input``."""

from io import StringIO

import pytest
from rich.console import Console

from openadventure.cli import wizard


def _console() -> Console:
    # wide so Rich doesn't wrap mid-phrase and break substring assertions
    return Console(file=StringIO(), force_terminal=False, color_system=None, width=200)


def _script_input(monkeypatch, answers):
    """Feed ``_step_*`` prompts a queue of answers (empty string once drained)."""
    it = iter(answers)
    monkeypatch.setattr(wizard, "_input", lambda prompt_text: next(it, ""))


def _raise_eof(*_args, **_kwargs):
    raise EOFError  # what Ctrl+D delivers to input()/getpass()


# --- cancel vs skip vs complete -------------------------------------------
def test_input_cancels_on_ctrl_d(monkeypatch):
    monkeypatch.setattr("builtins.input", _raise_eof)
    with pytest.raises(wizard._CancelWizard):
        wizard._input("x: ")


def test_input_skips_on_typed_skip(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "skip")
    with pytest.raises(wizard._SkipWizard):
        wizard._input("x: ")


def test_ask_secret_cancels_on_ctrl_d(monkeypatch):
    monkeypatch.setattr("builtins.input", _raise_eof)
    with pytest.raises(wizard._CancelWizard):
        wizard._ask_secret("key: ")


def test_ctrl_d_cancels_setup_and_leaves_it_to_reappear(make_session, monkeypatch):
    session = make_session(script=[])  # provider set -> the API-key step self-skips
    assert session.meta.setup_done is False
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _raise_eof)  # Ctrl+D at the first prompt
    out = _console()

    wizard.run_setup_wizard(out, session, first_run=True)

    # not marked done -> first_run stays true, so the wizard re-offers next launch
    assert session.meta.setup_done is False
    assert session.campaign.load_meta().setup_done is False
    assert "cancelled" in out.file.getvalue().lower()
    assert "Setup complete" not in out.file.getvalue()


def test_typed_skip_marks_setup_done(make_session, monkeypatch):
    session = make_session(script=[])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "skip")
    out = _console()

    wizard.run_setup_wizard(out, session, first_run=True)

    assert session.meta.setup_done is True
    assert "skipped" in out.file.getvalue().lower()


def test_completed_setup_marks_done(make_session, monkeypatch):
    session = make_session(script=[])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")  # Enter through every step
    out = _console()

    wizard.run_setup_wizard(out, session, first_run=True)

    assert session.meta.setup_done is True
    assert "Setup complete" in out.file.getvalue()


def _capture_prompts(monkeypatch, answers):
    """Like ``_script_input`` but also records the prompt text each call saw, so a
    test can assert which questions the wizard actually asked."""
    it = iter(answers)
    prompts: list[str] = []

    def _input(prompt_text):
        prompts.append(prompt_text)
        return next(it, "")

    monkeypatch.setattr(wizard, "_input", _input)
    return prompts


def test_api_key_step_follows_the_selected_model_backend(make_session, monkeypatch):
    """Model is chosen first, so the API-key step asks for the key of that model's
    own backend; pick a Claude model and it asks for the Anthropic key."""
    session = make_session(provider=None)  # no provider yet -> the key step prompts
    for var in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    anthropic_model = next(m.id for m in session.models.models if m.provider == "anthropic")
    answers = iter([anthropic_model])  # choose it at the model step; Enter through the rest

    prompts: list[str] = []

    def fake_input(prompt):
        prompts.append(prompt)
        return next(answers, "")  # an empty key at the secret step skips it (side-effect free)

    monkeypatch.setattr("builtins.input", fake_input)

    wizard.run_setup_wizard(_console(), session, first_run=True)

    assert session.settings.model == anthropic_model  # model was set first
    assert any("Anthropic" in p for p in prompts)  # key asked for that backend


def test_steps_hint_their_in_play_command(make_session, monkeypatch):
    session = make_session(script=[])
    monkeypatch.setattr("builtins.input", lambda prompt: "")  # Enter through each step's prompts
    cases = [
        (wizard._step_model, "/model"),
        (wizard._step_mode, "/mode"),
        (wizard._step_sources, "/sources"),
        (wizard._step_premise, "/premise"),
        (wizard._step_modules, "/modules"),
        (wizard._step_special_instructions, "/instructions"),
        (wizard._step_verbosity, "/verbosity"),
        (wizard._step_media, "/tts"),
        (wizard._step_images, "/images"),
    ]
    for step, cmd in cases:
        out = _console()
        step(out, session)
        assert cmd in out.file.getvalue(), f"{step.__name__} should hint {cmd}"


def test_summary_hints_setup(make_session, monkeypatch):
    session = make_session(script=[])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")  # Enter through every step
    out = _console()

    wizard.run_setup_wizard(out, session, first_run=True)

    text = out.file.getvalue()
    assert "/setup" in text  # re-run setup


# --- media / narrator voice ------------------------------------------------
def test_step_media_offers_narrator_voice_when_narration_on(make_session, monkeypatch):
    session = make_session(script=[])
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")  # skip the key prompt
    # Set up audio? y; narration y; sfx n; music n; then the narrator voice URL.
    _script_input(
        monkeypatch,
        [
            "y",
            "y",
            "n",
            "n",
            "https://elevenlabs.io/app/voice-library?voiceId=6FiCmD8eY5VyjOdG5Zjk",
        ],
    )

    wizard._step_media(_console(), session)

    assert session.meta.tts_enabled is True
    assert session.narrator_voice_id() == "6FiCmD8eY5VyjOdG5Zjk"
    assert session.tts.voice_id == "6FiCmD8eY5VyjOdG5Zjk"


def test_step_narrator_voice_reset_to_default(make_session, monkeypatch):
    session = make_session(script=[])
    session.set_narrator_voice_id("old-voice")
    _script_input(monkeypatch, ["default"])

    wizard._step_narrator_voice(_console(), session)

    assert session.narrator_voice_id() is None


# --- mode ------------------------------------------------------------------
def test_step_mode_switches_to_assistant(make_session, monkeypatch):
    session = make_session(script=[])  # campaign fixture defaults to gm
    _script_input(monkeypatch, ["assistant"])

    wizard._step_mode(_console(), session)

    assert session.meta.mode == "assistant"
    assert session.campaign.load_meta().mode == "assistant"


def test_step_mode_prefix_match(make_session, monkeypatch):
    session = make_session(script=[])
    _script_input(monkeypatch, ["a"])  # prefix of "assistant"

    wizard._step_mode(_console(), session)

    assert session.meta.mode == "assistant"


def test_step_mode_enter_keeps_current(make_session, monkeypatch):
    session = make_session(script=[])
    _script_input(monkeypatch, [""])

    wizard._step_mode(_console(), session)

    assert session.meta.mode == "gm"


# --- premise --------------------------------------------------------------
def test_step_premise_sets_a_new_premise(make_session, monkeypatch):
    session = make_session(script=[])
    _script_input(monkeypatch, ["a frostbound pilgrimage"])

    wizard._step_premise(_console(), session)

    assert session.meta.premise == "a frostbound pilgrimage"


def test_step_premise_enter_keeps_the_current_one(make_session, monkeypatch):
    session = make_session(script=[])  # campaign fixture seeds "a one-room dungeon"
    _script_input(monkeypatch, [""])

    wizard._step_premise(_console(), session)

    assert session.meta.premise == "a one-room dungeon"


def test_step_premise_clear_removes_it(make_session, monkeypatch):
    session = make_session(script=[])
    _script_input(monkeypatch, ["clear"])

    wizard._step_premise(_console(), session)

    assert session.meta.premise is None


# --- sources --------------------------------------------------------------
def test_step_sources_with_none_ingested_offers_ingest(make_session, monkeypatch):
    session = make_session(script=[])
    out = _console()
    _script_input(monkeypatch, ["n"])  # decline the "Ingest a new source now?" offer

    wizard._step_sources(out, session)

    assert session.meta.sources == []
    assert "No sources ingested" in out.file.getvalue()


def test_step_sources_selects_from_the_ingested_list(make_session, monkeypatch, workspace):
    # list_books() surfaces any book dir holding a manifest.json
    source = workspace.book_dir("dnd5e")
    source.mkdir(parents=True, exist_ok=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    session = make_session(script=[])
    _script_input(monkeypatch, ["dnd5e"])

    wizard._step_sources(_console(), session)

    assert session.meta.sources == ["dnd5e"]
    assert session.meta.system_source == "dnd5e"


def test_step_sources_accepts_a_comma_separated_list(make_session, monkeypatch, workspace):
    for name in ("dnd5e", "monster-manual"):
        d = workspace.book_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text("{}", encoding="utf-8")
    session = make_session(script=[])
    _script_input(monkeypatch, ["dnd5e, monster"])

    wizard._step_sources(_console(), session)

    assert session.meta.sources == ["dnd5e", "monster-manual"]
    assert session.meta.system_source == "dnd5e"  # first entry is the system source


def test_step_sources_matches_on_a_prefix(make_session, monkeypatch, workspace):
    source = workspace.book_dir("call-of-cthulhu")
    source.mkdir(parents=True, exist_ok=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    session = make_session(script=[])
    _script_input(monkeypatch, ["call"])

    wizard._step_sources(_console(), session)

    assert session.meta.sources == ["call-of-cthulhu"]


def test_step_sources_none_clears_set_sources(make_session, monkeypatch, workspace):
    source = workspace.book_dir("dnd5e")
    source.mkdir(parents=True, exist_ok=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    session = make_session(script=[])
    session.add_source("dnd5e")
    _script_input(monkeypatch, ["none"])

    wizard._step_sources(_console(), session)

    assert session.meta.sources == []
    assert session.meta.system_source is None


def test_maybe_offer_template_hints_cli_when_non_interactive(make_session, monkeypatch, workspace):
    source = workspace.book_dir("dnd5e")
    source.mkdir(parents=True, exist_ok=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    session = make_session(script=[])
    session.set_sources(["dnd5e"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)  # non-interactive -> hint only
    out = _console()

    wizard._maybe_offer_template(out, session)

    text = out.file.getvalue()
    assert "template" in text.lower()
    assert "openadventure template dnd5e" in text  # points at the CLI to generate it


def test_maybe_offer_template_no_note_when_one_exists(make_session, monkeypatch, workspace):
    source = workspace.book_dir("dnd5e")
    (source / "templates").mkdir(parents=True, exist_ok=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    (source / "templates" / "character.json").write_text(
        '{"name": "dnd5e/character", "fields": [], "resources": []}', encoding="utf-8"
    )
    session = make_session(script=[])
    session.set_sources(["dnd5e"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    out = _console()

    wizard._maybe_offer_template(out, session)

    assert "openadventure template" not in out.file.getvalue()


def test_maybe_offer_template_offers_and_derives_when_accepted(
    make_session, monkeypatch, workspace
):
    """Under a tty, the wizard actively offers to derive a missing template and,
    on yes, runs the derivation, matching the ingest pipelines."""
    source = workspace.book_dir("dnd5e")
    source.mkdir(parents=True, exist_ok=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    session = make_session(script=[])
    session.set_sources(["dnd5e"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")  # accept the offer
    monkeypatch.setattr(wizard, "run_template_wizard", lambda *a, **k: ("PROVIDER", "SETTINGS"))

    derived_with = {}

    async def fake_derive(provider, settings, dest, name, on_progress=None):
        derived_with.update(provider=provider, settings=settings, dest=dest, name=name)
        return {"fields": [1, 2], "resources": [3]}

    monkeypatch.setattr("openadventure.ingest.template_gen.derive_template", fake_derive)
    out = _console()

    wizard._maybe_offer_template(out, session)

    text = out.file.getvalue()
    assert "generate one now" in text  # actively offered, not a passive hint
    assert "Saved" in text  # derivation ran and reported
    assert derived_with["name"] == "dnd5e"
    assert derived_with["dest"] == workspace.book_dir("dnd5e")


def test_maybe_offer_template_declined_hints_cli(make_session, monkeypatch, workspace):
    source = workspace.book_dir("dnd5e")
    source.mkdir(parents=True, exist_ok=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    session = make_session(script=[])
    session.set_sources(["dnd5e"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")  # decline

    called = []
    monkeypatch.setattr(wizard, "run_template_wizard", lambda *a, **k: called.append(1))
    out = _console()

    wizard._maybe_offer_template(out, session)

    assert not called  # declined -> never built a provider
    assert "openadventure template dnd5e" in out.file.getvalue()


# --- modules ---------------------------------------------------------------
def _ingest_marker(workspace, name):
    """Minimal on-disk footprint so list_books() surfaces a book."""
    d = workspace.book_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text("{}", encoding="utf-8")


def test_step_modules_with_none_ingested_offers_ingest(make_session, monkeypatch):
    session = make_session(script=[])
    out = _console()
    _script_input(monkeypatch, ["n"])  # decline the "Ingest a new module now?" offer

    wizard._step_modules(out, session)

    assert session.meta.modules == []
    assert "No modules ingested" in out.file.getvalue()


def test_step_modules_picks_from_ingested(make_session, monkeypatch, workspace):
    _ingest_marker(workspace, "sunken-keep")
    session = make_session(script=[])
    _script_input(monkeypatch, ["sunken-keep"])

    wizard._step_modules(_console(), session)

    assert [m.slug for m in session.meta.modules] == ["sunken-keep"]
    assert session.meta.active_module == "sunken-keep"


def test_step_modules_accepts_a_comma_separated_list(make_session, monkeypatch, workspace):
    for name in ("sunken-keep", "barovia"):
        _ingest_marker(workspace, name)
    session = make_session(script=[])
    _script_input(monkeypatch, ["sunken-keep, barovia"])

    wizard._step_modules(_console(), session)

    assert [m.slug for m in session.meta.modules] == ["sunken-keep", "barovia"]
    assert session.meta.active_module == "sunken-keep"  # first = where play starts


def test_step_modules_none_clears(make_session, monkeypatch, workspace):
    _ingest_marker(workspace, "sunken-keep")
    session = make_session(script=[])
    session.add_module("sunken-keep")
    _script_input(monkeypatch, ["none"])

    wizard._step_modules(_console(), session)

    assert session.meta.modules == []
    assert session.meta.active_module is None


# --- ingest wizard --------------------------------------------------------
def _fake_ingest_pipeline(monkeypatch, *, book_type="source", result=None):
    """Patch the ingest pipeline so tests don't touch the filesystem."""
    from contextlib import contextmanager

    @contextmanager
    def fake_progress(console):
        yield None

    monkeypatch.setattr("openadventure.cli.progress.ingest_progress", fake_progress)
    monkeypatch.setattr(
        "openadventure.ingest.embeddings.try_load_backend", lambda cfg: (None, None)
    )
    monkeypatch.setattr(
        "openadventure.ingest.pipeline.ingest",
        lambda source, dest, **kw: result or {"section_count": 5},
    )


def test_ingest_wizard_skips_on_empty_path(make_session, monkeypatch):
    session = make_session(script=[])
    out = _console()
    _script_input(monkeypatch, [""])  # Enter at "File path:" → skip

    result = wizard._ingest_wizard(out, session, "source")

    assert result is None


def test_ingest_wizard_errors_on_missing_file(make_session, monkeypatch, tmp_path):
    session = make_session(script=[])
    out = _console()
    _script_input(monkeypatch, [str(tmp_path / "missing.pdf")])

    result = wizard._ingest_wizard(out, session, "source")

    assert result is None
    assert "No such file" in out.file.getvalue()


def test_ingest_wizard_returns_slug_after_successful_ingest(
    make_session, monkeypatch, tmp_path, workspace
):
    session = make_session(script=[])
    out = _console()
    source_file = tmp_path / "my-rulebook.pdf"
    source_file.write_bytes(b"fake")
    # path, name (Enter = use stem default), page range (Enter = none)
    _script_input(monkeypatch, [str(source_file), "", ""])
    _fake_ingest_pipeline(monkeypatch)
    # Make sure the workspace finds the slug after ingest (pipeline is mocked,
    # so manually lay down the manifest the workspace needs).
    dest = workspace.book_dir("my-rulebook")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text('{"type": "source"}', encoding="utf-8")

    result = wizard._ingest_wizard(out, session, "source")

    assert result == "my-rulebook"
    assert "Ingested" in out.file.getvalue()


def test_ingest_wizard_uses_custom_name(make_session, monkeypatch, tmp_path, workspace):
    session = make_session(script=[])
    out = _console()
    source_file = tmp_path / "huge-compendium.pdf"
    source_file.write_bytes(b"x")
    _script_input(monkeypatch, [str(source_file), "dnd5e", ""])
    _fake_ingest_pipeline(monkeypatch)
    dest = workspace.book_dir("dnd5e")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text('{"type": "source"}', encoding="utf-8")

    result = wizard._ingest_wizard(out, session, "source")

    assert result == "dnd5e"


def test_ingest_wizard_passes_page_range(make_session, monkeypatch, tmp_path, workspace):
    session = make_session(script=[])
    out = _console()
    source_file = tmp_path / "big-book.pdf"
    source_file.write_bytes(b"x")
    _script_input(monkeypatch, [str(source_file), "", "10-50"])

    captured = {}

    from contextlib import contextmanager

    @contextmanager
    def fake_progress(console):
        yield None

    monkeypatch.setattr("openadventure.cli.progress.ingest_progress", fake_progress)
    monkeypatch.setattr(
        "openadventure.ingest.embeddings.try_load_backend", lambda cfg: (None, None)
    )

    def capturing_ingest(source, dest, *, pages, **kw):
        captured["pages"] = pages
        return {"section_count": 3}

    monkeypatch.setattr("openadventure.ingest.pipeline.ingest", capturing_ingest)
    dest = workspace.book_dir("big-book")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text('{"type": "source"}', encoding="utf-8")

    wizard._ingest_wizard(out, session, "source")

    assert captured["pages"] == (10, 50)


def test_step_sources_ingest_when_none_available(make_session, monkeypatch, workspace):
    session = make_session(script=[])
    out = _console()

    def fake_ingest_wizard(console, sess, book_type):
        d = workspace.book_dir("new-source")
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text('{"type": "source"}', encoding="utf-8")
        return "new-source"

    monkeypatch.setattr(wizard, "_ingest_wizard", fake_ingest_wizard)
    # "y" to "Ingest a new source now?", then pick the freshly ingested one
    _script_input(monkeypatch, ["y", "new-source"])

    wizard._step_sources(out, session)

    assert session.meta.sources == ["new-source"]


def test_step_sources_ingest_keyword_in_selection_loop(make_session, monkeypatch, workspace):
    # Existing source already present
    source = workspace.book_dir("dnd5e")
    source.mkdir(parents=True, exist_ok=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    session = make_session(script=[])
    out = _console()

    def fake_ingest_wizard(console, sess, book_type):
        d = workspace.book_dir("pathfinder")
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text('{"type": "source"}', encoding="utf-8")
        return "pathfinder"

    monkeypatch.setattr(wizard, "_ingest_wizard", fake_ingest_wizard)
    # Type 'ingest' first, then select both
    _script_input(monkeypatch, ["ingest", "dnd5e, pathfinder"])

    wizard._step_sources(out, session)

    assert set(session.meta.sources) == {"dnd5e", "pathfinder"}


def test_step_modules_ingest_when_none_available(make_session, monkeypatch, workspace):
    session = make_session(script=[])
    out = _console()

    def fake_ingest_wizard(console, sess, book_type):
        d = workspace.book_dir("sunken-keep")
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text('{"type": "module"}', encoding="utf-8")
        return "sunken-keep"

    monkeypatch.setattr(wizard, "_ingest_wizard", fake_ingest_wizard)
    _script_input(monkeypatch, ["y", "sunken-keep"])

    wizard._step_modules(out, session)

    assert [m.slug for m in session.meta.modules] == ["sunken-keep"]


def test_step_modules_ingest_keyword_in_selection_loop(make_session, monkeypatch, workspace):
    _ingest_marker(workspace, "barovia")
    session = make_session(script=[])
    out = _console()

    def fake_ingest_wizard(console, sess, book_type):
        d = workspace.book_dir("sunken-keep")
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text('{"type": "module"}', encoding="utf-8")
        return "sunken-keep"

    monkeypatch.setattr(wizard, "_ingest_wizard", fake_ingest_wizard)
    _script_input(monkeypatch, ["ingest", "barovia, sunken-keep"])

    wizard._step_modules(out, session)

    assert [m.slug for m in session.meta.modules] == ["barovia", "sunken-keep"]


# --- skip-already-done ----------------------------------------------------
def test_wizard_skips_sources_attached_before_setup(make_session, monkeypatch, workspace):
    """Sources attached via CLI before setup are auto-confirmed; wizard never prompts."""
    source = workspace.book_dir("dnd5e")
    (source / "templates").mkdir(parents=True, exist_ok=True)  # template present -> no offer
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    (source / "templates" / "character.json").write_text(
        '{"name": "dnd5e/character", "fields": [], "resources": []}', encoding="utf-8"
    )
    session = make_session(script=[])
    session.add_source("dnd5e")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _no_sources_prompt(p):
        assert "Sources" not in p, f"should not prompt for sources, got: {p!r}"
        return ""

    monkeypatch.setattr(wizard, "_input", _no_sources_prompt)
    out = _console()

    wizard.run_setup_wizard(out, session, first_run=True)

    assert "✓ Sources" in out.file.getvalue()
    assert session.meta.sources == ["dnd5e"]  # unchanged


def test_wizard_skips_modules_attached_before_setup(make_session, monkeypatch, workspace):
    """Modules attached via CLI before setup are auto-confirmed; wizard never prompts."""
    _ingest_marker(workspace, "sunken-keep")
    session = make_session(script=[])
    session.add_module("sunken-keep")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _no_modules_prompt(p):
        assert "Modules" not in p, f"should not prompt for modules, got: {p!r}"
        return ""

    monkeypatch.setattr(wizard, "_input", _no_modules_prompt)
    out = _console()

    wizard.run_setup_wizard(out, session, first_run=True)

    assert "✓ Modules" in out.file.getvalue()
    assert [m.slug for m in session.meta.modules] == ["sunken-keep"]


def test_wizard_offers_template_for_source_attached_before_setup(
    make_session, monkeypatch, workspace
):
    """The README flow: a source attached before setup has its sources step
    skipped, but a missing template is still offered (the whole point of the
    wizard handling it)."""
    source = workspace.book_dir("dnd5e")
    source.mkdir(parents=True, exist_ok=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    session = make_session(script=[])
    session.add_source("dnd5e")  # attached before setup -> sources step is skipped

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")  # decline the template offer
    out = _console()

    wizard.run_setup_wizard(out, session, first_run=True)

    text = out.file.getvalue()
    assert "✓ Sources" in text  # the step itself was auto-confirmed
    assert "generate one now" in text  # but the missing template was still offered


def test_wizard_resumes_after_cancel_skipping_completed_steps(make_session, monkeypatch):
    """Cancel mid-wizard; on re-entry completed steps are auto-skipped."""
    session = make_session(script=[])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    # First run: cancel on the very first prompt (model step).
    monkeypatch.setattr("builtins.input", lambda p: (_ for _ in ()).throw(EOFError()))
    wizard.run_setup_wizard(_console(), session, first_run=True)
    assert session.meta.setup_done is False
    # Nothing completed yet; no _wizard_steps saved.
    assert "_wizard_steps" not in session.meta.settings

    # Second run: press Enter through model, then cancel at mode.
    call_count = [0]

    def _cancel_at_mode(p):
        call_count[0] += 1
        if call_count[0] == 1:
            return ""  # first prompt = model: accept default
        raise EOFError  # subsequent prompts = cancel

    monkeypatch.setattr("builtins.input", _cancel_at_mode)
    wizard.run_setup_wizard(_console(), session, first_run=True)
    assert session.meta.setup_done is False
    # model step was completed and saved.
    assert "model" in session.meta.settings.get("_wizard_steps", [])

    # Third run: model should be skipped (auto-confirmed); mode is asked.
    seen_prompts: list[str] = []

    def _record_and_accept(p):
        seen_prompts.append(p)
        return ""

    monkeypatch.setattr("builtins.input", _record_and_accept)
    out = _console()
    wizard.run_setup_wizard(out, session, first_run=True)

    text = out.file.getvalue()
    assert "✓ Model" in text  # auto-confirmed, not re-asked
    assert session.meta.setup_done is True
    # _wizard_steps cleared on completion.
    assert "_wizard_steps" not in session.meta.settings


def test_explicit_setup_reruns_from_scratch_after_completion(make_session, monkeypatch):
    """After a completed setup, /setup (first_run=False) re-asks all steps."""
    session = make_session(script=[])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda p: "")  # complete it once
    wizard.run_setup_wizard(_console(), session, first_run=True)
    assert session.meta.setup_done is True
    assert "_wizard_steps" not in session.meta.settings

    # Now run /setup explicitly; all steps should be asked again.
    model_prompts: list[str] = []

    def _record(p):
        model_prompts.append(p)
        return ""

    monkeypatch.setattr(wizard, "_input", _record)
    wizard.run_setup_wizard(_console(), session)

    assert any("Model" in p for p in model_prompts), "model step should re-run on /setup"


# --- template wizard -------------------------------------------------------
def test_template_wizard_uses_default_model_non_interactive(config, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)  # scripted: no model prompt
    asked = {}

    def fake_ensure(console, cfg, provider):
        asked["provider"] = provider
        return "key-1"

    monkeypatch.setattr("openadventure.cli.firstrun.ensure_api_key", fake_ensure)
    monkeypatch.setattr(
        "openadventure.providers.factory.build_provider",
        lambda name, key, registry: ("PROVIDER", name, key),
    )

    result = wizard.run_template_wizard(_console(), config, "dnd5e")

    assert result is not None
    provider, settings = result
    assert settings.model == "gpt-5.6-terra"  # the accuracy-first default
    assert settings.thinking is True
    assert settings.effort.value == "high"  # always runs at high effort
    assert asked["provider"] == "openai"  # key resolved for the model's backend
    assert provider == ("PROVIDER", "openai", "key-1")


def test_template_wizard_in_game_reuses_table_model_without_prompt(config, monkeypatch):
    """In-game (table_model given): no model prompt, the table model is used at
    high effort, and nothing is persisted to the workspace config."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _no_prompt(prompt):  # the in-game path must not ask for a model
        raise AssertionError(f"unexpected prompt: {prompt!r}")

    monkeypatch.setattr("builtins.input", _no_prompt)
    asked = {}

    def fake_ensure(console, cfg, provider):
        asked["provider"] = provider
        return "k"

    monkeypatch.setattr("openadventure.cli.firstrun.ensure_api_key", fake_ensure)
    monkeypatch.setattr(
        "openadventure.providers.factory.build_provider",
        lambda name, key, registry: ("P", name),
    )

    result = wizard.run_template_wizard(_console(), config, "dnd5e", table_model="claude-opus-4-8")

    assert result is not None
    _provider, settings = result
    assert settings.model == "claude-opus-4-8"  # the campaign's table model
    assert settings.thinking is True and settings.effort.value == "high"  # at high effort
    assert asked["provider"] == "anthropic"  # key for the table model's backend
    assert config.utility.get("model") is None  # workspace config left untouched


def test_template_wizard_lets_you_pick_a_model(config, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "claude-opus-4-8")
    asked = {}

    def fake_ensure(console, cfg, provider):
        asked["provider"] = provider
        return "k"

    monkeypatch.setattr("openadventure.cli.firstrun.ensure_api_key", fake_ensure)
    monkeypatch.setattr(
        "openadventure.providers.factory.build_provider",
        lambda name, key, registry: ("P", name),
    )

    result = wizard.run_template_wizard(_console(), config, "dnd5e")

    assert result is not None
    _provider, settings = result
    assert settings.model == "claude-opus-4-8"  # the picked model
    assert settings.thinking is True and settings.effort.value == "high"  # effort kept
    assert asked["provider"] == "anthropic"  # key for the chosen model's backend


def test_template_wizard_cancel_returns_none(config, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _raise_eof)  # Ctrl+D at the model prompt
    out = _console()

    result = wizard.run_template_wizard(out, config, "dnd5e")

    assert result is None
    assert "cancelled" in out.file.getvalue().lower()


def test_template_wizard_no_key_returns_none(config, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "openadventure.cli.firstrun.ensure_api_key", lambda console, cfg, provider: None
    )
    out = _console()

    result = wizard.run_template_wizard(out, config, "dnd5e")

    assert result is None
    assert "needs an api key" in out.file.getvalue().lower()
