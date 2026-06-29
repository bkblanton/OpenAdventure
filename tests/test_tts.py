"""Text-to-speech narration mode."""

import asyncio
import contextlib
import os

from rich.console import Console

from openadventure.cli.firstrun import ensure_elevenlabs_api_key
from openadventure.cli.render import EventRenderer
from openadventure.cli.repl import Repl
from openadventure.engine.events import ToolFinished, ToolStarted
from openadventure.engine.session import GameSession
from openadventure.engine.tools import build_registry
from openadventure.engine.tools.narration_tools import NARRATION_TOOLS
from openadventure.media.factory import load_backends
from openadventure.media.narration import (
    NarrationAgent,
    SoundEffectCue,
    VoiceAssignment,
    VoiceCast,
    VoiceCue,
    load_voice_cast,
    save_voice_cast,
)
from openadventure.media.sound_effects import ElevenLabsSoundEffects
from openadventure.media.tts import (
    DEFAULT_ELEVENLABS_VOICE_ID,
    ElevenLabsTTS,
    VoiceRecord,
    extract_voice_id,
    speech_text,
)
from openadventure.providers.base import PTextDelta, PToolUse, PToolUseStart, PTurnDone, Usage
from openadventure.providers.fake import FakeProvider
from tests.conftest import FakeMediaHost, collect
from tests.test_agent_loop import text_turn


class FakeSFX:
    """Generation-only sound-effects backend (returns a sentinel path)."""

    ready = True
    configuration_hint = ""

    def __init__(self):
        self.calls: list[dict] = []

    async def generate(self, description: str, **kwargs) -> str:
        self.calls.append({"description": description, **kwargs})
        return f"sfx::{description}"


class FakeTTS:
    """A generation-only TTS backend: records what it synthesized and returns a
    sentinel path. Playback (and stopping it) is the host's job now."""

    ready = True
    configuration_hint = ""

    def __init__(self):
        self.spoken: list[str] = []
        self.calls: list[dict] = []

    async def synthesize(self, text: str, *, voice_id: str | None = None) -> str:
        self.spoken.append(text)
        self.calls.append({"text": text, "voice_id": voice_id})
        return f"clip::{text}"


class FakeVoiceDirectoryTTS(FakeTTS):
    voice_id = "default-voice"

    def __init__(self):
        super().__init__()
        self.searches: list[dict] = []
        self.added: list[dict] = []

    async def search_voice_directory(self, **kwargs):
        self.searches.append(kwargs)
        return [
            VoiceRecord(
                voice_id="shared-voice-1",
                name="Old Noble",
                source="shared",
                accent="american",
                public_owner_id="owner-1",
                description="A controlled old aristocratic voice.",
            )
        ]

    async def add_shared_voice(self, public_owner_id: str, voice_id: str, *, new_name: str) -> str:
        self.added.append(
            {"public_owner_id": public_owner_id, "voice_id": voice_id, "new_name": new_name}
        )
        return "local-voice-1"


async def test_tts_speaks_visible_assistant_text_only(make_session):
    script = [
        [
            PTextDelta(text="Let me check something first. "),
            PToolUseStart(id="tc1", name="roll_dice"),
            PToolUse(id="tc1", name="roll_dice", input={"expression": "1d20+2"}),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("**You spot** a `tripwire`."),
    ]
    session = make_session(script=script)
    fake_tts = FakeTTS()
    session.tts = fake_tts
    session.set_tts_enabled(True)

    events = await collect(session.handle_input("I look around"))
    await session.background.wait_all()
    later = session.background.drain()

    assert fake_tts.spoken == ["**You spot** a `tripwire`."]
    assert all("Let me check" not in text for text in fake_tts.spoken)
    assert any(e.type == "background_task_started" and e.kind == "tts" for e in events)
    assert any(e.type == "background_task_finished" for e in later)


async def test_tts_disabled_by_default(make_session):
    session = make_session(script=[text_turn("Quiet words.")])
    fake_tts = FakeTTS()
    session.tts = fake_tts

    events = await collect(session.handle_input("hello"))
    await session.background.wait_all()

    assert fake_tts.spoken == []
    assert not any(e.type == "background_task_started" and e.kind == "tts" for e in events)


async def test_assistant_mode_tts_plays_voice_without_narrating_output(config, workspace):
    campaign = workspace.create_campaign("Assistant Audio", mode="assistant")
    script = [
        [
            PToolUse(
                id="n1",
                name="play_dialogue",
                input={"speaker": "Strahd", "text": "I am the ancient."},
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("The line is playing."),
    ]
    session = GameSession(
        config,
        workspace,
        campaign,
        FakeProvider(script=script),
        session_seed=42,
    )
    fake_tts = FakeTTS()
    session.tts = fake_tts
    session.set_tts_enabled(True)
    session.tools = build_registry(
        workspace, campaign, session.meta, media_backends=(None, None, fake_tts, None)
    )

    system = session.build_system()[0].text
    assert "Output narration: disabled in assistant mode" in system
    assert "Voice commands: enabled" in system
    assert "play_dialogue" in session.tools

    events = await collect(session.handle_input("play his line"))
    await session.background.wait_all()

    assert any(e.type == "background_task_started" and e.kind == "tts" for e in events)
    assert fake_tts.spoken == ["I am the ancient."]
    assert all("The line is playing" not in text for text in fake_tts.spoken)


async def test_stage_dialogue_marks_matching_visible_text_with_named_voice(make_session):
    script = [
        [
            PToolUse(
                id="n1",
                name="stage_dialogue",
                input={
                    "speaker": "Strahd",
                    "text": "Welcome to my house.",
                    "voice_hint": "ancient noble",
                },
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn('Strahd says, "Welcome to my house."'),
    ]
    session = make_session(script=script)
    fake_tts = FakeVoiceDirectoryTTS()
    session.tts = fake_tts
    session.set_tts_enabled(True)
    session.set_cast_accent("American")
    for tool in NARRATION_TOOLS:
        session.tools.register(tool)

    events = await collect(session.handle_input("I enter"))
    await session.background.wait_all()

    assert any(e.type == "background_task_started" and e.kind == "tts" for e in events)
    assert "".join(call["text"] for call in fake_tts.calls) == (
        'Strahd says, "Welcome to my house."'
    )
    assert fake_tts.calls == [
        {"text": 'Strahd says, "', "voice_id": "default-voice"},
        {"text": "Welcome to my house.", "voice_id": "local-voice-1"},
        {"text": '"', "voice_id": "default-voice"},
    ]
    assert fake_tts.searches[0]["search"] == "ancient noble"
    assert fake_tts.searches[0]["accent"] == "american"
    assert fake_tts.added[0]["new_name"] == "OpenAdventure Strahd"
    cast = load_voice_cast(session.campaign)
    assert cast.speakers["strahd"].voice_id == "local-voice-1"
    assert cast.speakers["strahd"].voice_name == "Old Noble"
    assert cast.speakers["strahd"].accent == "american"
    assert cast.speakers["strahd"].target_accent == "american"


class FakeGenderedDirectoryTTS(FakeTTS):
    voice_id = "default-voice"

    def __init__(self):
        super().__init__()
        self.searches: list[dict] = []

    async def search_voice_directory(self, **kwargs):
        self.searches.append(kwargs)
        # The directory does not strictly honor the gender filter: it returns a
        # mismatched actor first, so selection must post-filter by gender itself.
        return [
            VoiceRecord(
                voice_id="male-voice",
                name="Gravel Baron",
                source="owned",
                accent="american",
                gender="male",
            ),
            VoiceRecord(
                voice_id="female-voice",
                name="Steel Matron",
                source="owned",
                accent="american",
                gender="female",
            ),
        ]


async def test_stage_dialogue_casts_voice_of_requested_gender(make_session):
    script = [
        [
            PToolUse(
                id="n1",
                name="stage_dialogue",
                input={
                    "speaker": "Ireena",
                    "text": "You will not take me.",
                    "gender": "female",
                },
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("You will not take me."),
    ]
    session = make_session(script=script)
    fake_tts = FakeGenderedDirectoryTTS()
    session.tts = fake_tts
    session.set_tts_enabled(True)
    for tool in NARRATION_TOOLS:
        session.tools.register(tool)

    await collect(session.handle_input("she speaks"))
    await session.background.wait_all()

    assert fake_tts.searches[0]["gender"] == "female"
    assert fake_tts.calls == [{"text": "You will not take me.", "voice_id": "female-voice"}]
    cast = load_voice_cast(session.campaign)
    assert cast.speakers["ireena"].voice_id == "female-voice"
    assert cast.speakers["ireena"].gender == "female"
    assert cast.speakers["ireena"].target_gender == "female"


class FakeAccentedDirectoryTTS(FakeTTS):
    voice_id = "default-voice"

    def __init__(self):
        super().__init__()
        self.searches: list[dict] = []

    async def search_voice_directory(self, **kwargs):
        self.searches.append(kwargs)
        # The directory does not strictly honor the accent filter: it returns a
        # mismatched actor first, so selection must post-filter by accent itself.
        return [
            VoiceRecord(
                voice_id="southern-voice",
                name="Gentle Southern",
                source="owned",
                accent="southern us",
            ),
            VoiceRecord(
                voice_id="italian-voice",
                name="Vittorio",
                source="owned",
                accent="italian",
            ),
        ]


async def test_stage_dialogue_casts_voice_of_requested_accent(make_session):
    script = [
        [
            PToolUse(
                id="n1",
                name="stage_dialogue",
                input={
                    "speaker": "Vittorio Macario",
                    "text": "Buongiorno, traveler.",
                    "accent": "italian",
                },
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("Buongiorno, traveler."),
    ]
    session = make_session(script=script)
    fake_tts = FakeAccentedDirectoryTTS()
    session.tts = fake_tts
    session.set_tts_enabled(True)
    # A campaign-wide default that the per-speaker accent should override.
    session.set_cast_accent("southern us")
    for tool in NARRATION_TOOLS:
        session.tools.register(tool)

    await collect(session.handle_input("he greets us"))
    await session.background.wait_all()

    assert fake_tts.searches[0]["accent"] == "italian"
    assert fake_tts.calls == [{"text": "Buongiorno, traveler.", "voice_id": "italian-voice"}]
    cast = load_voice_cast(session.campaign)
    assert cast.speakers["vittorio-macario"].voice_id == "italian-voice"
    assert cast.speakers["vittorio-macario"].accent == "italian"
    assert cast.speakers["vittorio-macario"].target_accent == "italian"


async def test_stage_dialogue_does_not_speak_unmatched_hidden_text(make_session):
    script = [
        [
            PToolUse(
                id="n1",
                name="stage_dialogue",
                input={"speaker": "Strahd", "text": "This line is not visible."},
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("Only this line reaches the player."),
    ]
    session = make_session(script=script)
    fake_tts = FakeTTS()
    session.tts = fake_tts
    session.set_tts_enabled(True)
    for tool in NARRATION_TOOLS:
        session.tools.register(tool)

    await collect(session.handle_input("I listen"))
    await session.background.wait_all()

    assert fake_tts.spoken == ["Only this line reaches the player."]
    assert all("This line is not visible" not in text for text in fake_tts.spoken)


async def test_interrupt_narration_cancels_queue_and_stops_audio(make_session):
    # Playback lives in the host now: a slow play_speech stands in for audio that
    # is mid-flight, and interrupt cancels the queue + tells the host to stop.
    host = FakeMediaHost(speech_delay=60)
    session = make_session(script=[], media_host=host)
    session.tts = FakeTTS()
    session.set_tts_enabled(True)

    session.queue_narration("first")
    session.queue_narration("second")
    await host.started.wait()
    cancelled = session.interrupt_narration()
    await session.background.wait_all()
    later = session.background.drain()

    assert cancelled >= 1
    assert host.stopped_audio >= 1
    assert any(e.type == "background_task_finished" and not e.ok for e in later)


async def test_interrupt_narration_cancels_whole_turn_with_sound_effect(make_session):
    # The sound effect playback hangs; interrupt cancels the turn, so only the
    # first sentence was actually played before the stuck effect.
    host = FakeMediaHost(sfx_delay=60)
    session = make_session(script=[], media_host=host)
    session.tts = FakeTTS()
    session.sound_effects = FakeSFX()
    session.set_tts_enabled(True)

    session.queue_narration(
        "The door groans open. The hallway waits beyond it.",
        sound_effect_cues=[
            SoundEffectCue(
                description="a heavy door groaning open",
                after_text="The door groans open.",
            )
        ],
    )
    await host.started.wait()
    cancelled = session.interrupt_narration()
    await session.background.wait_all()
    later = session.background.drain()

    assert cancelled == 1
    assert host.stopped_audio >= 1
    assert host.speech == ["clip::The door groans open."]
    assert any(e.type == "background_task_finished" and not e.ok for e in later)


async def test_narration_replay_reuses_cached_audio(make_session):
    # A replay re-plays the last turn's clips through the host without asking the
    # TTS backend to synthesize anything again (no new API calls).
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    fake_tts = FakeTTS()
    session.tts = fake_tts
    session.set_tts_enabled(True)

    session.queue_narration("The torch gutters in the cold draft.")
    await session.background.wait_all()
    assert host.speech == ["clip::The torch gutters in the cold draft."]
    assert fake_tts.spoken == ["The torch gutters in the cold draft."]

    started = session.replay_narration()
    assert started is not None
    await session.background.wait_all()

    # The clip played a second time, but the backend was never called again.
    assert host.speech == ["clip::The torch gutters in the cold draft."] * 2
    assert fake_tts.spoken == ["The torch gutters in the cold draft."]


async def test_narration_replay_targets_the_latest_turn(make_session):
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    session.tts = FakeTTS()
    session.set_tts_enabled(True)

    session.queue_narration("First beat.")
    await session.background.wait_all()
    session.queue_narration("Second beat.")
    await session.background.wait_all()

    session.replay_narration()
    await session.background.wait_all()

    # Only the most recent turn is replayed, not the whole history.
    assert host.speech == ["clip::First beat.", "clip::Second beat.", "clip::Second beat."]


async def test_narration_replay_noop_when_nothing_played(make_session):
    session = make_session(script=[], media_host=FakeMediaHost())
    session.tts = FakeTTS()
    session.set_tts_enabled(True)

    # Nothing has been narrated yet, so there's nothing to replay.
    assert session.replay_narration() is None


async def test_narration_replay_stops_current_narration_first(make_session):
    # A replay interrupts whatever is still playing before it starts, like /stop.
    host = FakeMediaHost(speech_delay=60)
    session = make_session(script=[], media_host=host)
    session.tts = FakeTTS()
    session.set_tts_enabled(True)

    session.queue_narration("A long, slow line still playing.")
    await host.started.wait()
    stopped_before = host.stopped_audio
    # interrupt() runs synchronously inside replay_narration(), before the replay
    # task is spawned, so the stop is observable right away.
    started = session.replay_narration()
    assert started is not None
    assert host.stopped_audio > stopped_before

    # The replay clip also rides the slow host; cancel it so teardown is clean.
    session.interrupt_narration()
    await session.background.wait_all()


async def test_repl_renders_finished_narration_while_waiting_for_input(make_session):
    session = make_session(script=[])
    console = Console(record=True)
    repl = Repl(console, session)
    release_input = asyncio.Event()
    rendered_events = []

    async def fake_read(prompt):
        await release_input.wait()
        return "next move"

    class RecordingRenderer:
        def render_events(self, events):
            rendered_events.extend(events)

    repl._read_line = fake_read
    repl.renderer = RecordingRenderer()

    async def work():
        return []

    session.background.spawn("tts", "narrating turn: The room falls quiet.", work())
    read_task = asyncio.ensure_future(repl._read_line_with_background(None))
    for _ in range(10):
        await asyncio.sleep(0.05)
        if rendered_events:
            break

    assert any(
        event.type == "background_task_finished"
        and "narrating turn: The room falls quiet." in event.message
        for event in rendered_events
    )
    assert not read_task.done()

    release_input.set()
    assert await read_task == "next move"


async def test_repl_prompt_stdout_allows_rich_ansi(make_session, monkeypatch):
    raw_values = []

    @contextlib.contextmanager
    def fake_patch_stdout(*, raw=False):
        raw_values.append(raw)
        yield

    class FakePrompt:
        async def prompt_async(self, prompt_text, *, rprompt=None):
            return "next move"

    monkeypatch.setattr("openadventure.cli.repl.patch_stdout", fake_patch_stdout)
    repl = Repl(Console(record=True), make_session(script=[]))

    assert await repl._read_line(FakePrompt()) == "next move"
    assert raw_values == [True]


def test_renderer_hides_tool_details_outside_debug():
    console = Console(record=True)
    renderer = EventRenderer(console, debug=False)
    renderer.render_events(
        [
            ToolStarted(call_id="n1", name="play_dialogue"),
            ToolFinished(
                call_id="n1",
                name="play_dialogue",
                args_summary="speaker='Narrator'",
                result_summary="voice cue: Narrator",
            ),
        ]
    )

    # Debug off shows the tool name and number, but never the args or result;
    # those can carry spoilers.
    text = console.export_text()
    assert "play_dialogue" in text
    assert "speaker='Narrator'" not in text
    assert "voice cue" not in text


def test_renderer_shows_play_dialogue_tool_in_debug():
    console = Console(record=True)
    renderer = EventRenderer(console, debug=True)
    renderer.render_events(
        [
            ToolStarted(call_id="n1", name="play_dialogue"),
            ToolFinished(
                call_id="n1",
                name="play_dialogue",
                args_summary="speaker='Narrator'",
                result_summary="voice cue: Narrator",
            ),
        ]
    )

    assert "play_dialogue" in console.export_text()


def test_tts_enabled_persists(make_session):
    session = make_session(script=[])
    session.set_tts_enabled(True)

    assert session.campaign.load_meta().tts_enabled
    fresh = make_session(script=[])
    assert fresh.meta.tts_enabled


async def test_tts_command_toggles_saved_mode(make_session):
    session = make_session(script=[])
    repl = Repl(Console(record=True), session)

    await repl._cmd_tts("on")
    assert session.campaign.load_meta().tts_enabled

    await repl._cmd_tts("off")
    assert not session.campaign.load_meta().tts_enabled


async def test_voice_accent_command_sets_campaign_setting(make_session):
    session = make_session(script=[])
    repl = Repl(Console(record=True), session)

    await repl._cmd_voice("accent british")
    assert session.cast_accent() == "british"
    assert session.campaign.load_meta().settings["narration_accent"] == "british"

    await repl._cmd_voice("accent clear")
    assert session.cast_accent() is None
    assert "narration_accent" not in session.campaign.load_meta().settings


async def test_voice_clear_command_removes_one_speaker(make_session):
    session = make_session(script=[])
    save_voice_cast(
        session.campaign,
        VoiceCast(
            speakers={
                "strahd": VoiceAssignment(
                    speaker="Strahd",
                    voice_id="wrong-accent",
                    voice_name="Wrong Accent",
                    accent="british",
                ),
                "ireena": VoiceAssignment(
                    speaker="Ireena",
                    voice_id="keep-this",
                    voice_name="Right Accent",
                    accent="american",
                ),
            }
        ),
    )
    repl = Repl(Console(record=True), session)

    await repl._cmd_voice("clear Strahd")

    cast = load_voice_cast(session.campaign)
    assert "strahd" not in cast.speakers
    assert cast.speakers["ireena"].voice_id == "keep-this"


async def test_voice_clear_all_command_removes_cast(make_session):
    session = make_session(script=[])
    save_voice_cast(
        session.campaign,
        VoiceCast(
            speakers={
                "strahd": VoiceAssignment(speaker="Strahd", voice_id="one"),
                "ireena": VoiceAssignment(speaker="Ireena", voice_id="two"),
            }
        ),
    )
    repl = Repl(Console(record=True), session)

    await repl._cmd_voice("clear all")

    assert load_voice_cast(session.campaign).speakers == {}


async def test_tts_command_prompts_for_elevenlabs_key(make_session, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "el-test-key")
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    session = make_session(script=[])
    session.tts = ElevenLabsTTS()
    repl = Repl(Console(record=True), session)

    await repl._cmd_tts("on")

    assert session.tts.api_key == "el-test-key"
    assert session.tts.ready
    assert "ELEVENLABS_API_KEY=el-test-key" in (tmp_path / ".env").read_text(encoding="utf-8")


async def test_sfx_command_prompts_for_elevenlabs_key(make_session, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "el-sfx-key")
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    session = make_session(script=[])
    session.sound_effects = ElevenLabsSoundEffects()
    repl = Repl(Console(record=True), session)

    await repl._cmd_sfx("on")

    assert session.campaign.load_meta().sound_effects_enabled
    assert "play_sound_effect" in session.tools
    assert session.sound_effects.ready
    assert "ELEVENLABS_API_KEY=el-sfx-key" in (tmp_path / ".env").read_text(encoding="utf-8")


async def test_sfx_command_turns_tool_off(make_session):
    session = make_session(script=[])
    repl = Repl(Console(record=True), session)

    await repl._cmd_sfx("on")
    assert "play_sound_effect" in session.tools

    await repl._cmd_sfx("off")
    assert not session.campaign.load_meta().sound_effects_enabled
    assert "play_sound_effect" not in session.tools


def test_ensure_elevenlabs_api_key_sets_process_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "el-direct-key")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    key = ensure_elevenlabs_api_key(Console(record=True))

    assert key == "el-direct-key"
    assert os.environ["ELEVENLABS_API_KEY"] == "el-direct-key"
    assert not (tmp_path / ".env").exists()


def test_tts_factory_defaults_to_elevenlabs(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    _images, _music, tts, sfx = load_backends({})

    assert isinstance(tts, ElevenLabsTTS)
    assert tts.model_id == "eleven_flash_v2_5"
    assert not tts.ready
    assert "ELEVENLABS_API_KEY" in tts.configuration_hint
    assert isinstance(sfx, ElevenLabsSoundEffects)
    assert sfx.model_id == "eleven_text_to_sound_v2"
    assert not sfx.ready


def test_speech_text_strips_markdown_for_narration():
    spoken = speech_text("# Scene\n\n- **Listen:** [the door](https://example.com) says `clink`.")

    assert spoken == "Scene. Listen: the door says clink."


class _PrefetchTTS(FakeTTS):
    """Records synthesize ordering so we can prove pipelining (the backend only
    synthesizes now; the host plays)."""

    voice_id = "default-voice"

    def __init__(self):
        super().__init__()
        self.events: list[tuple[str, str]] = []

    async def synthesize(self, text: str, *, voice_id: str | None = None):
        self.events.append(("synth", text))
        return f"path::{text}"


class _RecordingHost(FakeMediaHost):
    """A host that records play ordering alongside the backend's synth ordering."""

    def __init__(self, events: list[tuple[str, str]]):
        super().__init__()
        self._events = events

    async def play_speech(self, path) -> None:
        await asyncio.sleep(0)  # yield so a primed prefetch can make progress
        self._events.append(("play", str(path)))


class _ListLog:
    def __init__(self):
        self.entries: list[tuple[str, dict]] = []

    def append(self, kind: str, payload: dict) -> None:
        self.entries.append((kind, payload))


async def test_play_turn_prefetches_next_voice_clip_while_current_plays(campaign):
    tts = _PrefetchTTS()
    log = _ListLog()
    host = _RecordingHost(tts.events)  # synth + play events interleave in one list
    agent = NarrationAgent(campaign, log, None, tts=tts, host=host)

    text = "The guard blocks the door. Halt, who goes there? The hall stays silent."
    await agent.play_turn(
        text,
        voice_cues=[VoiceCue(text="Halt, who goes there?", speaker="Guard", role="dialogue")],
    )

    synth_texts = [t for kind, t in tts.events if kind == "synth"]
    play_paths = [t for kind, t in tts.events if kind == "play"]
    # Three segments (narrator, the dialogue line, narrator again), each played in order.
    assert len(synth_texts) == 3
    assert play_paths == [f"path::{t}" for t in synth_texts]

    def first_index(kind: str, needle: str) -> int:
        return next(i for i, (k, t) in enumerate(tts.events) if k == kind and needle in t)

    # The dialogue clip is synthesized before the first narrator clip finishes playing;
    # that overlap is the gap the prefetch removes.
    assert first_index("synth", "Halt, who goes there?") < first_index(
        "play", "The guard blocks the door"
    )
    assert log.entries[-1][1]["prefetched"] is True


def test_speech_text_breaks_lists_into_sentences():
    markdown = (
        "Where to next?\n\n"
        "- 📰 Boston Globe — dig the clippings files\n"
        "- 🏛️ Hall of Records — deeds and death certificates\n"
        "- 🚪 Head to 428 Crane Street — you've dawdled long enough"
    )

    spoken = speech_text(markdown)

    # Each bullet is its own sentence (no emoji, no run-on between beats).
    assert spoken == (
        "Where to next? "
        "Boston Globe — dig the clippings files. "
        "Hall of Records — deeds and death certificates. "
        "Head to 428 Crane Street — you've dawdled long enough."
    )


def test_extract_voice_id_handles_urls_and_bare_ids():
    # The voice-library URL the ElevenLabs UI copies carries the id as a query param.
    assert (
        extract_voice_id("https://elevenlabs.io/app/voice-library?voiceId=6FiCmD8eY5VyjOdG5Zjk")
        == "6FiCmD8eY5VyjOdG5Zjk"
    )
    # A bare id (optionally quoted/padded) passes through untouched.
    assert extract_voice_id("  6FiCmD8eY5VyjOdG5Zjk  ") == "6FiCmD8eY5VyjOdG5Zjk"
    assert extract_voice_id('"abc123"') == "abc123"
    # A URL with the id as the final path segment falls back to that segment.
    assert extract_voice_id("https://elevenlabs.io/voices/xyz789") == "xyz789"


def test_set_narrator_voice_overrides_then_clears_to_default(make_session):
    session = make_session(script=[])
    assert session.narrator_voice_id() is None
    assert session.tts.voice_id == DEFAULT_ELEVENLABS_VOICE_ID

    session.set_narrator_voice_id("custom-narrator-voice")

    assert session.narrator_voice_id() == "custom-narrator-voice"
    assert session.tts.voice_id == "custom-narrator-voice"  # applied to the live backend
    # persisted to the campaign meta
    assert session.campaign.load_meta().settings["narrator_voice_id"] == "custom-narrator-voice"

    session.set_narrator_voice_id(None)

    assert session.narrator_voice_id() is None
    assert session.tts.voice_id == DEFAULT_ELEVENLABS_VOICE_ID  # reverts to the default
    assert "narrator_voice_id" not in session.campaign.load_meta().settings


def test_narrator_voice_survives_reload_tools(make_session):
    session = make_session(script=[])
    session.set_narrator_voice_id("pinned-voice")

    session.reload_tools()  # rebuilds backends from config

    assert session.tts.voice_id == "pinned-voice"


def test_set_narrator_voice_invalidates_cached_narrator(make_session):
    # A campaign that already narrated has a remembered "narrator" cast entry
    # pinned to the old voice; setting a new voice must drop it so the change
    # actually takes effect on the next line (instead of the cast shadowing it).
    session = make_session(script=[])
    cast = load_voice_cast(session.campaign)
    cast.speakers["narrator"] = VoiceAssignment(
        speaker="Narrator", voice_id="stale-old-voice", voice_name="Narrator"
    )
    save_voice_cast(session.campaign, cast)

    session.set_narrator_voice_id("fresh-voice")

    assert "narrator" not in load_voice_cast(session.campaign).speakers
    assert session.tts.voice_id == "fresh-voice"


async def test_honorific_variant_reuses_one_cast_entry(campaign):
    # "Mr. Dooley" on a later line must land on the entry the first "Dooley" line
    # created, not spawn a second casting of the same character.
    agent = NarrationAgent(campaign, _ListLog(), None, tts=FakeVoiceDirectoryTTS())
    first = await agent.assign_voice("Dooley", accent="boston", gender="male")
    second = await agent.assign_voice("Mr. Dooley")

    assert second.voice_id == first.voice_id
    cast = load_voice_cast(campaign)
    assert list(cast.speakers) == ["dooley"]
    assert cast.aliases == {"mr-dooley": "dooley"}


async def test_speaker_binds_to_matching_npc_sheet_id(campaign):
    # Two different surface names for one NPC collapse onto that NPC's stable
    # sheet id, so casting is tied to the character rather than the spelling.
    from openadventure.mechanics.sheets import Sheet
    from openadventure.store.sheetstore import SheetStore

    SheetStore(campaign).save(Sheet(id="dooley", kind="npc", name="Officer Dooley"))
    agent = NarrationAgent(campaign, _ListLog(), None, tts=FakeVoiceDirectoryTTS())
    first = await agent.assign_voice("Dooley", accent="boston", gender="male")
    second = await agent.assign_voice("Officer Dooley")

    assert second.voice_id == first.voice_id
    cast = load_voice_cast(campaign)
    assert list(cast.speakers) == ["dooley"]
    assert cast.aliases == {"officer-dooley": "dooley"}


async def test_staged_npcs_marks_already_cast_speakers(make_session):
    # On-stage NPCs already cast carry a note naming the exact speaker to reuse,
    # so the GM doesn't reintroduce them under a new name. Uncast NPCs stay quiet
    # to keep context lean.
    from openadventure.mechanics.sheets import Sheet
    from openadventure.store.sheetstore import SheetStore

    session = make_session(script=[text_turn("hi")])
    session.set_tts_enabled(True)
    store = SheetStore(session.campaign)
    store.save(Sheet(id="dooley", kind="npc", name="Dooley", fields={"role": "dockworker"}))
    store.save(Sheet(id="arty", kind="npc", name="Arty", fields={"role": "clerk"}))
    save_voice_cast(
        session.campaign,
        VoiceCast(
            speakers={
                "dooley": VoiceAssignment(speaker="Dooley", voice_id="v1", voice_name="Chris")
            }
        ),
    )

    brief = session.staged_npcs({"npcs_present": ["dooley", "arty"]})

    assert 'voice: already cast, reuse speaker "Dooley"' in brief
    arty_line = next(line for line in brief.splitlines() if line.startswith("- Arty"))
    assert "voice:" not in arty_line


def test_cast_lookup_reports_saved_voice(workspace, campaign):
    import random

    from openadventure.engine.tools.narration_tools import CastLookupArgs, cast_lookup
    from openadventure.engine.tools.registry import ToolContext

    save_voice_cast(
        campaign,
        VoiceCast(
            speakers={
                "dooley": VoiceAssignment(
                    speaker="Dooley",
                    voice_id="v1",
                    voice_name="Chris",
                    target_accent="boston",
                    target_gender="male",
                )
            }
        ),
    )
    meta = campaign.load_meta()
    meta.tts_enabled = True
    agent = NarrationAgent(campaign, _ListLog(), None, tts=FakeVoiceDirectoryTTS())
    ctx = ToolContext(
        workspace=workspace,
        campaign=campaign,
        meta=meta,
        log=campaign.open_log(),
        rng=random.Random(1),
        narration=agent,
    )

    # A honorific variant still resolves to the saved entry.
    found = cast_lookup(ctx, CastLookupArgs(speaker="Mr. Dooley"))
    assert "already cast" in found.content
    assert 'Reuse speaker "Dooley"' in found.content

    missing = cast_lookup(ctx, CastLookupArgs(speaker="Stranger"))
    assert "not cast yet" in missing.content

    listing = cast_lookup(ctx, CastLookupArgs())
    assert "Dooley" in listing.content
