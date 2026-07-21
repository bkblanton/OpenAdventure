import asyncio
import contextlib
from io import StringIO

import pytest
from rich.console import Console

from openadventure.cli.main import _run_repl
from openadventure.cli.repl import Repl


async def _hang():
    await asyncio.sleep(3600)


def test_repl_prompt_text_is_plain(make_session):
    session = make_session(script=[])
    repl = Repl(Console(file=StringIO()), session)

    assert repl._prompt_text() == "> "

    session.set_override("verbosity", "high")

    assert repl._prompt_text() == "> "


async def test_repl_does_not_send_right_prompt(make_session, monkeypatch):
    calls = []

    @contextlib.contextmanager
    def fake_patch_stdout(*, raw=False):
        yield

    class FakePrompt:
        async def prompt_async(self, prompt_text, *, rprompt=None):
            calls.append((prompt_text, rprompt))
            return "look around"

    monkeypatch.setattr("openadventure.cli.repl.patch_stdout", fake_patch_stdout)
    session = make_session(script=[])
    repl = Repl(Console(file=StringIO()), session)

    assert await repl._read_line(FakePrompt()) == "look around"
    assert calls == [("> ", None)]


async def test_interrupt_cancels_an_active_turn_and_hushes_audio(make_session, monkeypatch):
    session = make_session(script=[])
    hushed = []
    monkeypatch.setattr(session, "interrupt_narration", lambda: hushed.append(True) or 0)
    repl = Repl(Console(file=StringIO()), session)

    turn = asyncio.ensure_future(_hang())
    await asyncio.sleep(0)  # let the turn start
    repl._current_turn = turn

    repl.interrupt()  # Ctrl+C while the AI is thinking/narrating

    assert hushed == [True]
    with contextlib.suppress(asyncio.CancelledError):
        await turn
    assert turn.cancelled()


async def test_interrupt_at_prompt_stops_narration(make_session, monkeypatch):
    session = make_session(script=[])
    hushed = []
    monkeypatch.setattr(session, "interrupt_narration", lambda: hushed.append(True) or 2)
    repl = Repl(Console(file=StringIO()), session)
    repl._current_turn = None  # nothing thinking; a recap may be narrating

    repl.interrupt()

    assert hushed == [True]


async def test_handle_player_input_reports_a_cancelled_turn(make_session, monkeypatch):
    session = make_session(script=[])
    monkeypatch.setattr(session, "handle_input", lambda *a, **k: None)
    monkeypatch.setattr(session, "interrupt_narration", lambda: 0)
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    async def hang_render(_gen):
        await asyncio.sleep(3600)

    monkeypatch.setattr(repl.renderer, "render_turn", hang_render)

    player = asyncio.ensure_future(repl.handle_player_input("attack the armor"))
    for _ in range(100):
        await asyncio.sleep(0)
        if repl._current_turn is not None:
            break

    repl.interrupt()  # Ctrl+C mid-turn

    await asyncio.wait_for(player, timeout=1)  # returns cleanly, no exception escapes
    assert "turn cancelled" in out.getvalue()
    assert repl._current_turn is None


def test_run_repl_routes_ctrl_c_to_interrupt_without_crashing(make_session, monkeypatch):
    # A KeyboardInterrupt out of run_until_complete must be caught, routed to
    # repl.interrupt(), and never escape _run_repl.
    session = make_session(script=[])
    repl = Repl(Console(file=StringIO()), session)
    interrupts = []
    monkeypatch.setattr(repl, "interrupt", lambda: interrupts.append(True))

    async def fake_run():
        raise KeyboardInterrupt

    monkeypatch.setattr(repl, "run", fake_run)

    _run_repl(repl)  # must return, not propagate

    assert interrupts == [True]


def test_run_repl_resumes_the_suspended_session_after_ctrl_c(make_session, monkeypatch):
    # The reported bug: a Ctrl+C arriving while the REPL is *suspended* (the
    # common case, landing in the event loop, not inside the coroutine) unwound
    # out of run_until_complete and killed the campaign. _run_repl must resume the
    # same task, which is only suspended, not dead.
    import _thread
    import threading
    import time

    session = make_session(script=[])
    repl = Repl(Console(file=StringIO()), session)

    state = {"interrupts": 0, "completed": False}

    def fake_interrupt():
        state["interrupts"] += 1
        repl.running = False  # let the resumed loop notice it should stop

    monkeypatch.setattr(repl, "interrupt", fake_interrupt)

    async def fake_run():
        repl.running = True
        while repl.running:  # a Ctrl+C lands here, mid-await; task stays suspended
            await asyncio.sleep(0.02)
        state["completed"] = True

    monkeypatch.setattr(repl, "run", fake_run)

    def fire_ctrl_c():
        for _ in range(100):
            time.sleep(0.05)
            if state["interrupts"] == 0:
                _thread.interrupt_main()  # KeyboardInterrupt in the main thread
            else:
                return

    threading.Thread(target=fire_ctrl_c, daemon=True).start()
    _run_repl(repl)  # must return without raising

    assert state["interrupts"] >= 1
    assert state["completed"]  # the session resumed and finished cleanly


def test_run_repl_reprompts_after_ctrl_c_at_prompt(make_session, monkeypatch):
    # Full-stack: a Ctrl+C surfacing from the prompt read (as prompt_toolkit
    # delivers it) re-raises out of run_until_complete via Task.__step. Going
    # through _run_repl, it must stop narration and re-prompt, not exit.
    @contextlib.contextmanager
    def fake_patch_stdout(*, raw=False):
        yield

    monkeypatch.setattr("openadventure.cli.repl.patch_stdout", fake_patch_stdout)
    session = make_session(script=[])
    monkeypatch.setattr(session, "interrupt_narration", lambda: 1)  # narration was playing
    reads = {"n": 0}

    class FakePromptSession:
        def __init__(self, *args, **kwargs):
            pass

        async def prompt_async(self, prompt_text, *, rprompt=None):
            reads["n"] += 1
            if reads["n"] == 1:
                raise KeyboardInterrupt  # Ctrl+C at the prompt
            return "/quit"

    monkeypatch.setattr("openadventure.cli.repl.PromptSession", FakePromptSession)
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    _run_repl(repl)  # the real entry point; must not crash

    assert reads["n"] == 2  # re-prompted instead of exiting
    assert "narration stopped" in out.getvalue()


def test_sending_a_message_stops_prior_narration(make_session, monkeypatch):
    # A plain chat message should cut off any narration still playing from the
    # previous turn before the new turn begins.
    @contextlib.contextmanager
    def fake_patch_stdout(*, raw=False):
        yield

    monkeypatch.setattr("openadventure.cli.repl.patch_stdout", fake_patch_stdout)
    session = make_session(script=[])
    reads = {"n": 0}
    # Record which read each interrupt fired on, so the message-triggered one is
    # distinguishable from the unavoidable interrupt at session close.
    hushed_at = []
    monkeypatch.setattr(session, "interrupt_narration", lambda: hushed_at.append(reads["n"]) or 1)

    class FakePromptSession:
        def __init__(self, *args, **kwargs):
            pass

        async def prompt_async(self, prompt_text, *, rprompt=None):
            reads["n"] += 1
            if reads["n"] == 1:
                return "look around"  # a plain message, narration still playing
            return "/quit"

    monkeypatch.setattr("openadventure.cli.repl.PromptSession", FakePromptSession)
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    hushed_before_turn = []

    async def noop_turn(text, **k):
        hushed_before_turn.append(list(hushed_at))
        return None

    monkeypatch.setattr(repl, "handle_player_input", noop_turn)

    _run_repl(repl)

    # Narration was interrupted on the first read, before the turn ran.
    assert 1 in hushed_at
    assert hushed_before_turn == [[1]]


def test_slash_command_does_not_stop_narration_implicitly(make_session, monkeypatch):
    # Slash commands aren't chat messages: dispatching one must not interrupt
    # narration on its own (only commands that opt in, like /clear, do).
    @contextlib.contextmanager
    def fake_patch_stdout(*, raw=False):
        yield

    monkeypatch.setattr("openadventure.cli.repl.patch_stdout", fake_patch_stdout)
    session = make_session(script=[])
    reads = {"n": 0}
    hushed_at = []
    monkeypatch.setattr(session, "interrupt_narration", lambda: hushed_at.append(reads["n"]) or 0)

    class FakePromptSession:
        def __init__(self, *args, **kwargs):
            pass

        async def prompt_async(self, prompt_text, *, rprompt=None):
            reads["n"] += 1
            if reads["n"] == 1:
                return "/roll 1d20"  # a slash command, not a chat message
            return "/quit"

    monkeypatch.setattr("openadventure.cli.repl.PromptSession", FakePromptSession)
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    _run_repl(repl)

    # /roll didn't interrupt on its read (1); the only interrupt is at close (2).
    assert 1 not in hushed_at


async def test_restart_interrupts_narration_and_music(make_session, monkeypatch):
    import types

    session = make_session(script=[])
    calls = []
    monkeypatch.setattr(session, "interrupt_narration", lambda: calls.append("narration") or 1)
    monkeypatch.setattr(session, "stop_music", lambda **k: calls.append("music"))
    report = types.SimpleNamespace(
        missing_originals=[], rerolled=["kasimir"], pcs=[], archive_dir="archive/old"
    )
    monkeypatch.setattr("openadventure.engine.timeline.restart_campaign", lambda *a, **k: report)
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    await repl._cmd_restart("reroll confirm")

    assert "narration" in calls and "music" in calls
    assert "Campaign restarted" in out.getvalue()


async def test_restart_without_confirm_leaves_audio_playing(make_session, monkeypatch):
    session = make_session(script=[])
    calls = []
    monkeypatch.setattr(session, "interrupt_narration", lambda: calls.append("narration") or 1)
    monkeypatch.setattr(session, "stop_music", lambda **k: calls.append("music"))
    repl = Repl(Console(file=StringIO(), force_terminal=False, color_system=None), session)

    await repl._cmd_restart("reroll")  # asks for confirmation, doesn't restart yet

    assert calls == []  # nothing touched until the player confirms


async def test_clear_wipes_screen_and_stops_narration(make_session, monkeypatch):
    session = make_session(script=[])
    hushed = []
    monkeypatch.setattr(session, "interrupt_narration", lambda: hushed.append(True) or 1)
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)
    cleared = []
    monkeypatch.setattr(repl.console, "clear", lambda *a, **k: cleared.append(True))

    await repl._cmd_clear("")

    assert hushed == [True]  # narration was interrupted
    assert cleared == [True]  # the screen was cleared
    assert "narration stopped" in out.getvalue()


async def test_clear_is_silent_when_nothing_is_narrating(make_session, monkeypatch):
    session = make_session(script=[])
    monkeypatch.setattr(session, "interrupt_narration", lambda: 0)
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)
    monkeypatch.setattr(repl.console, "clear", lambda *a, **k: None)

    await repl._cmd_clear("")

    assert "narration stopped" not in out.getvalue()


def test_clear_alias_is_registered(make_session):
    session = make_session(script=[])
    repl = Repl(Console(file=StringIO()), session)

    assert repl.commands["/cls"] is repl.commands["/clear"]


def test_voices_is_an_alias_for_voice(make_session):
    session = make_session(script=[])
    repl = Repl(Console(file=StringIO()), session)

    assert repl.commands["/voices"] is repl.commands["/voice"]


async def test_import_reads_file_and_drives_a_turn(make_session, monkeypatch, tmp_path):
    session = make_session(script=[])
    repl = Repl(Console(file=StringIO(), force_terminal=False, color_system=None), session)
    captured = []

    async def fake_turn(text, **k):
        captured.append(text)

    monkeypatch.setattr(repl, "handle_player_input", fake_turn)

    sheet = tmp_path / "thorin.md"
    sheet.write_text("# Thorin\nDwarf Fighter, level 3\nHP 28", encoding="utf-8")

    await repl._cmd_import(str(sheet))

    assert len(captured) == 1
    instruction = captured[0]
    assert "create_sheet" in instruction
    assert "Dwarf Fighter, level 3" in instruction  # the file content is embedded
    assert "thorin.md" in instruction


async def test_import_reads_json_and_drives_a_turn(make_session, monkeypatch, tmp_path):
    session = make_session(script=[])
    repl = Repl(Console(file=StringIO(), force_terminal=False, color_system=None), session)
    captured = []

    async def fake_turn(text, **k):
        captured.append(text)

    monkeypatch.setattr(repl, "handle_player_input", fake_turn)

    sheet = tmp_path / "thorin.json"
    sheet.write_text('{"name":"Thorin","class":"Fighter","level":3,"hp":28}', encoding="utf-8")

    await repl._cmd_import(str(sheet))

    assert len(captured) == 1
    instruction = captured[0]
    assert "create_sheet" in instruction
    assert "thorin.json" in instruction
    # The JSON is pretty-printed before being embedded.
    assert '"name": "Thorin"' in instruction


async def test_import_rejects_invalid_json(make_session, monkeypatch, tmp_path):
    out = StringIO()
    session = make_session(script=[])
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)
    called = []
    monkeypatch.setattr(repl, "handle_player_input", lambda text, **k: called.append(text))

    sheet = tmp_path / "broken.json"
    sheet.write_text('{"name": "Thorin", ', encoding="utf-8")

    await repl._cmd_import(str(sheet))

    assert "isn't valid JSON" in out.getvalue()
    assert called == []  # no turn attempted for malformed JSON


async def test_ingest_requires_a_type_flag(make_session, tmp_path):
    out = StringIO()
    session = make_session(script=[])
    repl = Repl(Console(file=out, force_terminal=False, color_system=None, width=200), session)

    book = tmp_path / "book.md"
    book.write_text("# Rules\n\n## Combat\n\nRoll a d20.\n", encoding="utf-8")

    await repl._cmd_ingest(book.as_posix())  # no --source / --module

    text = out.getvalue()
    assert "--source" in text and "--module" in text
    # nothing was ingested or attached
    assert session.workspace.list_books() == []
    assert session.meta.sources == [] and session.meta.modules == []


async def test_ingest_rejects_both_type_flags(make_session, tmp_path):
    out = StringIO()
    session = make_session(script=[])
    repl = Repl(Console(file=out, force_terminal=False, color_system=None, width=200), session)

    book = tmp_path / "book.md"
    book.write_text("# Rules\n\n## Combat\n\nRoll a d20.\n", encoding="utf-8")

    await repl._cmd_ingest(f"{book.as_posix()} --source --module")

    assert "not both" in out.getvalue()
    assert session.workspace.list_books() == []


async def test_ingest_as_module_records_type_and_attaches(make_session, tmp_path):
    out = StringIO()
    session = make_session(script=[])
    repl = Repl(Console(file=out, force_terminal=False, color_system=None, width=200), session)

    book = tmp_path / "death-house.md"
    book.write_text("# Death House\n\n## Entrance\n\nA grim manor looms.\n", encoding="utf-8")

    await repl._cmd_ingest(f"{book.as_posix()} --module")

    assert session.workspace.book_type("death-house") == "module"
    assert [m.slug for m in session.meta.modules] == ["death-house"]
    # and it can't then be attached as a rules source
    from openadventure.store.workspace import BookTypeMismatch

    with pytest.raises(BookTypeMismatch):
        session.add_source("death-house")


async def test_import_rejects_unsupported_file_type(make_session, tmp_path):
    out = StringIO()
    session = make_session(script=[])
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    pdf = tmp_path / "hero.pdf"
    pdf.write_text("not really a pdf", encoding="utf-8")

    await repl._cmd_import(str(pdf))

    assert "Unsupported file type" in out.getvalue()


async def test_import_reports_a_missing_file(make_session, tmp_path):
    out = StringIO()
    session = make_session(script=[])
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    await repl._cmd_import(str(tmp_path / "nope.md"))

    assert "No such file" in out.getvalue()


async def test_import_needs_a_provider(make_session, monkeypatch, tmp_path):
    out = StringIO()
    session = make_session(provider=None)
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)
    called = []
    monkeypatch.setattr(repl, "handle_player_input", lambda text, **k: called.append(text))

    sheet = tmp_path / "hero.txt"
    sheet.write_text("Gandalf, Wizard", encoding="utf-8")

    await repl._cmd_import(str(sheet))

    assert "needs an AI provider" in out.getvalue()
    assert called == []  # no turn attempted without a provider


async def test_import_shows_usage_without_args(make_session):
    out = StringIO()
    session = make_session(script=[])
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    await repl._cmd_import("")

    assert "Usage: /import" in out.getvalue()


async def test_verbosity_without_args_shows_current_value(make_session):
    output = StringIO()
    session = make_session(script=[])
    repl = Repl(Console(file=output, force_terminal=False, color_system=None), session)

    await repl._cmd_verbosity("")

    text = output.getvalue()
    assert "Current verbosity: medium" in text
    assert "/verbosity low|medium|high" in text


async def test_voice_sets_and_clears_narrator_voice(make_session):
    output = StringIO()
    session = make_session(script=[])
    repl = Repl(Console(file=output, force_terminal=False, color_system=None), session)

    # accepts the voice-library URL the ElevenLabs UI copies
    await repl._cmd_voice("https://elevenlabs.io/app/voice-library?voiceId=6FiCmD8eY5VyjOdG5Zjk")

    assert session.narrator_voice_id() == "6FiCmD8eY5VyjOdG5Zjk"
    assert session.tts.voice_id == "6FiCmD8eY5VyjOdG5Zjk"
    assert "6FiCmD8eY5VyjOdG5Zjk" in output.getvalue()

    await repl._cmd_voice("default")

    assert session.narrator_voice_id() is None


async def test_voice_without_args_shows_status(make_session):
    output = StringIO()
    session = make_session(script=[])
    repl = Repl(Console(file=output, force_terminal=False, color_system=None), session)

    await repl._cmd_voice("")

    assert "Narrator voice" in output.getvalue()


async def test_narration_does_not_manage_voice_subcommands(make_session):
    output = StringIO()
    session = make_session(script=[])
    repl = Repl(Console(file=output, force_terminal=False, color_system=None), session)

    await repl._cmd_narration("accent british")

    # /narration only controls playback; narrator selection lives under /voice.
    assert "Usage: /narration" in output.getvalue()


async def test_model_without_args_shows_current_model(make_session):
    output = StringIO()
    session = make_session(script=[])
    session.set_override("model", "claude-opus-4-8")
    repl = Repl(Console(file=output, force_terminal=False, color_system=None), session)

    await repl._cmd_model("")

    text = output.getvalue()
    assert "Current model: claude-opus-4-8" in text
    # the active model is marked in the list, others are not
    selected_line = next(line for line in text.splitlines() if "claude-opus-4-8 ←" in line)
    assert selected_line
    assert "claude-sonnet-4-6 ←" not in text


async def test_model_without_args_marks_custom_model(make_session):
    output = StringIO()
    session = make_session(script=[])
    session.set_override("model", "some-custom-model")
    repl = Repl(Console(file=output, force_terminal=False, color_system=None), session)

    await repl._cmd_model("")

    text = output.getvalue()
    assert "Current model: some-custom-model" in text
    assert "some-custom-model ← (current)" in text


async def test_sudo_quiet_flag_runs_off_the_record(make_session, monkeypatch):
    # /sudo -q steers out of character (steer=True) AND off the record
    # (ephemeral=True); plain /sudo steers but stays on the record.
    session = make_session(script=[])
    monkeypatch.setattr(session, "interrupt_narration", lambda: 0)
    repl = Repl(Console(file=StringIO(), force_terminal=False, color_system=None), session)

    calls = []

    async def fake_handle(text, *, steer=False, ephemeral=False):
        calls.append((text, steer, ephemeral))

    monkeypatch.setattr(repl, "handle_player_input", fake_handle)

    await repl._cmd_sudo("-q the vault is already open")
    await repl._cmd_sudo("the bandit defects")

    assert calls == [
        ("the vault is already open", True, True),
        ("the bandit defects", True, False),
    ]


async def test_premise_sets_shows_and_clears(make_session):
    out = StringIO()
    session = make_session(script=[])  # campaign fixture starts with a premise
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    await repl._cmd_premise("")  # no args -> show the current premise
    assert "a one-room dungeon" in out.getvalue()

    await repl._cmd_premise("a heist in a drowned elven city")
    assert session.meta.premise == "a heist in a drowned elven city"
    # persisted to the campaign, not just the in-memory meta
    assert session.campaign.load_meta().premise == "a heist in a drowned elven city"

    await repl._cmd_premise("clear")
    assert session.meta.premise is None
    assert session.campaign.load_meta().premise is None


async def test_premise_show_when_unset_explains_how_to_set(make_session):
    out = StringIO()
    session = make_session(script=[])
    session.set_premise(None)
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    await repl._cmd_premise("show")

    text = out.getvalue()
    assert "No premise set" in text
    assert "/premise" in text


async def test_open_campaign_drives_a_kickoff_turn(make_session, monkeypatch):
    # The GM-opens-the-table turn: shares the premise and lays out the ways to
    # bring in characters (roll, pre-made, or /import), without starting play.
    session = make_session(script=[])
    repl = Repl(Console(file=StringIO(), force_terminal=False, color_system=None), session)
    captured = []

    async def fake_turn(text, **k):
        captured.append((text, k))

    monkeypatch.setattr(repl, "handle_player_input", fake_turn)

    await repl._open_campaign()

    assert len(captured) == 1
    instruction, kwargs = captured[0]
    assert "START OF CAMPAIGN" in instruction
    assert "premise" in instruction.lower()
    assert "/import" in instruction
    assert "pre-generated" in instruction
    assert "Do NOT create any sheets" in instruction
    # the opening is a normal, logged turn (not ephemeral) so it persists
    assert kwargs.get("ephemeral", False) is False


def test_wants_campaign_kickoff_true_for_fresh_campaign_with_no_party(make_session):
    session = make_session(script=[])  # provider set, nothing played, no PCs
    session.meta.setup_done = True
    repl = Repl(Console(file=StringIO()), session)

    assert repl._wants_campaign_kickoff() is True


def test_wants_campaign_kickoff_false_when_setup_not_done(make_session):
    # If setup was interrupted (Ctrl+D), setup_done stays False; the kickoff
    # must not fire so we don't drop the user into the game mid-onboarding.
    session = make_session(script=[])  # provider set, setup_done defaults to False
    repl = Repl(Console(file=StringIO()), session)

    assert repl._wants_campaign_kickoff() is False


def test_wants_campaign_kickoff_false_without_a_provider(make_session):
    session = make_session(provider=None)
    repl = Repl(Console(file=StringIO()), session)

    assert repl._wants_campaign_kickoff() is False


def test_wants_campaign_kickoff_false_once_a_party_exists(make_session):
    from openadventure.mechanics.sheets import Sheet
    from openadventure.store.sheetstore import SheetStore

    session = make_session(script=[])
    SheetStore(session.campaign).save(Sheet(id="thorin", name="Thorin", kind="pc"))
    repl = Repl(Console(file=StringIO()), session)

    assert repl._wants_campaign_kickoff() is False


def test_wants_campaign_kickoff_false_after_prior_play(make_session):
    session = make_session(script=[])
    session.has_prior_play = True  # the campaign has already been played
    repl = Repl(Console(file=StringIO()), session)

    assert repl._wants_campaign_kickoff() is False


def _ingest_source(session, name: str):
    """Minimal on-disk footprint so list_books() surfaces a book."""
    d = session.workspace.book_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text("{}", encoding="utf-8")
    return d


def _wide_repl(session):
    # width avoids Rich wrapping the template hint mid-command
    return Repl(
        Console(file=StringIO(), force_terminal=False, color_system=None, width=200), session
    )


async def test_sources_command_sets_from_ingested_list(make_session):
    session = make_session(script=[])
    _ingest_source(session, "dnd5e")
    repl = _wide_repl(session)

    await repl._cmd_sources("dnd5e")

    assert session.meta.sources == ["dnd5e"]
    assert session.meta.system_source == "dnd5e"
    assert session.campaign.load_meta().sources == ["dnd5e"]
    text = repl.console.file.getvalue()
    assert "Sources set to dnd5e" in text
    assert "openadventure template dnd5e" in text  # missing-template hint


async def test_sources_command_attaches_several(make_session):
    session = make_session(script=[])
    _ingest_source(session, "dnd5e")
    _ingest_source(session, "monster-manual")
    repl = _wide_repl(session)

    await repl._cmd_sources("dnd5e, monster-manual")

    assert session.meta.sources == ["dnd5e", "monster-manual"]
    assert session.meta.system_source == "dnd5e"


async def test_sources_command_add_and_remove(make_session):
    session = make_session(script=[])
    _ingest_source(session, "dnd5e")
    _ingest_source(session, "monster-manual")
    repl = _wide_repl(session)

    await repl._cmd_sources("dnd5e")
    await repl._cmd_sources("add monster-manual")
    assert session.meta.sources == ["dnd5e", "monster-manual"]

    await repl._cmd_sources("remove monster-manual")
    assert session.meta.sources == ["dnd5e"]


async def test_sources_command_system_designates_system_source(make_session):
    session = make_session(script=[])
    _ingest_source(session, "dnd5e")
    _ingest_source(session, "monster-manual")
    repl = _wide_repl(session)

    await repl._cmd_sources("dnd5e, monster-manual")
    await repl._cmd_sources("system monster-manual")

    assert session.meta.system_source == "monster-manual"


async def test_sources_command_prefix_match(make_session):
    session = make_session(script=[])
    _ingest_source(session, "call-of-cthulhu")
    repl = _wide_repl(session)

    await repl._cmd_sources("call")

    assert session.meta.sources == ["call-of-cthulhu"]


async def test_sources_command_clears(make_session):
    session = make_session(script=[])
    _ingest_source(session, "dnd5e")
    session.add_source("dnd5e")
    repl = _wide_repl(session)

    await repl._cmd_sources("none")

    assert session.meta.sources == []
    assert "cleared" in repl.console.file.getvalue().lower()


async def test_sources_command_rejects_unknown(make_session):
    session = make_session(script=[])
    _ingest_source(session, "dnd5e")
    repl = _wide_repl(session)

    await repl._cmd_sources("pathfinder")

    assert session.meta.sources == []  # unchanged
    text = repl.console.file.getvalue()
    assert "no source matches" in text.lower()
    assert "dnd5e" in text  # lists what's available


async def test_sources_command_shows_current_and_available(make_session):
    session = make_session(script=[])
    _ingest_source(session, "dnd5e")
    _ingest_source(session, "coc7e")
    session.add_source("dnd5e")
    repl = _wide_repl(session)

    await repl._cmd_sources("")

    text = repl.console.file.getvalue()
    assert "dnd5e" in text and "coc7e" in text


async def test_sources_command_no_template_note_when_present(make_session):
    session = make_session(script=[])
    d = _ingest_source(session, "dnd5e")
    (d / "templates").mkdir(parents=True, exist_ok=True)
    (d / "templates" / "character.json").write_text(
        '{"name": "dnd5e/character", "fields": [], "resources": []}', encoding="utf-8"
    )
    repl = _wide_repl(session)

    await repl._cmd_sources("dnd5e")

    assert session.meta.sources == ["dnd5e"]
    assert "openadventure template" not in repl.console.file.getvalue()


def test_model_switch_connects_silently_when_key_present(make_session, monkeypatch):
    session = make_session(script=[])
    monkeypatch.setattr(session, "connect_provider", lambda: True)  # key already available
    prompted = []
    monkeypatch.setattr(
        "openadventure.cli.firstrun.ensure_api_key",
        lambda console, config, provider: prompted.append(True),
    )
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    repl._ensure_model_provider("anthropic", switched=True)

    assert prompted == []  # didn't prompt; connected from the existing key
    assert "switched to anthropic" in out.getvalue().lower()


def test_model_switch_prompts_for_a_missing_key(make_session, monkeypatch):
    session = make_session(script=[])
    monkeypatch.setattr(session, "connect_provider", lambda: False)  # no key yet
    attached = []
    monkeypatch.setattr(session, "attach_provider", lambda key: attached.append(key))
    monkeypatch.setattr(
        "openadventure.cli.firstrun.ensure_api_key",
        lambda console, config, provider: "sk-new",  # the player enters one
    )
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    repl._ensure_model_provider("anthropic", switched=True)

    assert attached == ["sk-new"]  # the entered key was used to connect
    assert "connected on the anthropic backend" in out.getvalue().lower()


def test_model_switch_warns_when_key_prompt_skipped(make_session, monkeypatch):
    session = make_session(script=[])
    monkeypatch.setattr(session, "connect_provider", lambda: False)
    attached = []
    monkeypatch.setattr(session, "attach_provider", lambda key: attached.append(key))
    monkeypatch.setattr(
        "openadventure.cli.firstrun.ensure_api_key",
        lambda console, config, provider: None,  # skipped / non-interactive
    )
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    repl._ensure_model_provider("anthropic", switched=True)

    assert attached == []  # nothing connected
    assert "no api key is configured" in out.getvalue().lower()


async def test_model_same_backend_prompts_for_key_when_disconnected(make_session, monkeypatch):
    # /model to another model on the SAME backend, while disconnected, now also
    # prompts for that backend's key (not just on a backend switch).
    session = make_session(provider=None)  # disconnected, default gemini backend
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(session, "connect_provider", lambda: False)  # still no key
    attached = []
    monkeypatch.setattr(session, "attach_provider", lambda key: attached.append(key))
    prompted = []

    def fake_ensure(console, config, provider):
        prompted.append(provider)
        return "key-123"

    monkeypatch.setattr("openadventure.cli.firstrun.ensure_api_key", fake_ensure)
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    await repl._cmd_model("gemini-3.1-pro-preview")  # same (gemini) backend

    assert prompted == ["gemini"]  # prompted for the current backend's key
    assert attached == ["key-123"]
    assert "connected on the gemini backend" in out.getvalue().lower()


async def test_model_same_backend_stays_silent_when_connected(make_session, monkeypatch):
    # If already connected, switching models on the same backend touches nothing.
    session = make_session(script=[])  # provider set (connected), OpenAI default backend
    monkeypatch.setattr(session, "connect_provider", lambda: pytest.fail("should not reconnect"))
    prompted = []
    monkeypatch.setattr(
        "openadventure.cli.firstrun.ensure_api_key",
        lambda console, config, provider: prompted.append(provider),
    )
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    await repl._cmd_model("gpt-5.6-sol")

    assert prompted == []
    text = out.getvalue().lower()
    assert "connected on the" not in text and "backend switched" not in text


async def test_btw_runs_off_record_but_still_instructs_lookups(make_session, monkeypatch):
    # /btw is off the record (ephemeral=True) but its framing must tell the GM to
    # look things up with read-only tools rather than answer from memory.
    session = make_session(script=[])
    monkeypatch.setattr(session, "interrupt_narration", lambda: 0)
    repl = Repl(Console(file=StringIO(), force_terminal=False, color_system=None), session)

    calls = []

    async def fake_handle(text, *, steer=False, ephemeral=False, read_only=False):
        calls.append((text, steer, ephemeral, read_only))

    monkeypatch.setattr(repl, "handle_player_input", fake_handle)

    await repl._cmd_btw("how much XP is the party sitting on?")

    assert len(calls) == 1
    text, steer, ephemeral, read_only = calls[0]
    assert ephemeral is True and steer is False
    assert read_only is True  # an aside is read-only, not just off the record
    assert "how much XP is the party sitting on?" in text
    assert "OUT-OF-CHARACTER" in text
    # the bug fix: the aside must not push the GM to guess from memory
    assert "read-only tools" in text
    assert "rather than guessing" in text


def _seed_log(session):
    """A turn's worth of log entries: narrative beats interleaved with mechanics."""
    session.log.append("user_message", {"text": "I search the desk"})
    session.log.append("tool_call", {"name": "search_campaign", "args": {"q": "secret door"}})
    session.log.append("roll", {"expression": "1d20", "total": 17, "private": True})
    session.log.append("state_change", {"summary": "Found a hidden lever"})
    session.log.append("gm_message", {"text": "The desk slides aside, revealing a passage."})


async def test_cmd_log_shows_only_narrative_beats_by_default(make_session):
    session = make_session(script=[])
    _seed_log(session)
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    await repl._cmd_log("")

    text = out.getvalue()
    # narrative the table experienced
    assert "I search the desk" in text
    assert "The desk slides aside, revealing a passage." in text
    assert "Found a hidden lever" in text
    # mechanics and behind-the-screen events stay out of the default view
    assert "search_campaign" not in text
    assert "1d20" not in text
    assert "tool_call" not in text


async def test_cmd_log_raw_dumps_the_full_event_log(make_session):
    session = make_session(script=[])
    _seed_log(session)
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    await repl._cmd_log("--raw")

    text = out.getvalue()
    # --raw is the debug view: everything, including the hidden mechanics
    assert "tool_call" in text
    assert "search_campaign" in text
    assert "roll" in text


async def test_cmd_log_respects_a_count(make_session):
    session = make_session(script=[])
    for i in range(5):
        session.log.append("gm_message", {"text": f"beat {i}"})
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    await repl._cmd_log("2")

    text = out.getvalue()
    assert "beat 4" in text and "beat 3" in text
    assert "beat 2" not in text


async def test_show_recap_replays_first_message_verbatim(make_session):
    # one turn (the kickoff): replay the GM's opening verbatim, no AI recap call
    session = make_session(script=[])
    session.log.append("user_message", {"text": "(kickoff)"})
    session.log.append("gm_message", {"text": "The tavern door creaks open."})
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    await repl._show_recap()

    assert "The tavern door creaks open." in out.getvalue()
    assert session.provider.calls == []  # no model call


async def test_show_recap_is_cancellable(make_session, monkeypatch):
    # more than one turn -> the AI recap path, which Ctrl+C can skip
    session = make_session(script=[])
    session.log.append("gm_message", {"text": "one"})
    session.log.append("gm_message", {"text": "two"})
    out = StringIO()
    repl = Repl(Console(file=out, force_terminal=False, color_system=None), session)

    async def _hang():
        await asyncio.sleep(3600)

    monkeypatch.setattr(session, "recap", _hang)
    show = asyncio.ensure_future(repl._show_recap())
    await asyncio.sleep(0.05)  # let it reach the await
    repl.interrupt()  # the runner's Ctrl+C path cancels _current_turn
    await show

    assert "recap skipped" in out.getvalue()
