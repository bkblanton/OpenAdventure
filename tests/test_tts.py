"""Text-to-speech narration mode."""

import os

from rich.console import Console

from openadventure.cli.firstrun import ensure_elevenlabs_api_key
from openadventure.cli.repl import Repl
from openadventure.media.factory import load_backends
from openadventure.media.narration import NarrationAgent, SoundEffectCue
from openadventure.media.sound_effects import ElevenLabsSoundEffects
from openadventure.media.tts import (
    DEFAULT_ELEVENLABS_VOICE_ID,
    ElevenLabsTTS,
    extract_voice_id,
    speech_text,
)
from openadventure.providers.base import PTextDelta, PToolUse, PToolUseStart, PTurnDone, Usage
from tests.conftest import FakeMediaHost, collect
from tests.test_agent_loop import text_turn


class FakeSFX:
    ready = True
    configuration_hint = ""

    def __init__(self):
        self.calls: list[dict] = []

    async def generate(self, description: str, **kwargs) -> str:
        self.calls.append({"description": description, **kwargs})
        return f"sfx::{description}"


class FakeTTS:
    ready = True
    configuration_hint = ""
    voice_id = "narrator-voice"

    def __init__(self):
        self.spoken: list[str] = []
        self.calls: list[dict] = []

    async def synthesize(self, text: str, *, voice_id: str | None = None) -> str:
        self.spoken.append(text)
        self.calls.append({"text": text, "voice_id": voice_id})
        return f"clip::{text}"


class _ListLog:
    def __init__(self):
        self.entries: list[tuple[str, dict]] = []

    def append(self, kind: str, payload: dict) -> None:
        self.entries.append((kind, payload))


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

    assert fake_tts.spoken == ["**You spot** a `tripwire`."]
    assert all("Let me check" not in text for text in fake_tts.spoken)
    assert any(event.type == "background_task_started" and event.kind == "tts" for event in events)


async def test_tts_disabled_by_default(make_session):
    session = make_session(script=[text_turn("Quiet words.")])
    fake_tts = FakeTTS()
    session.tts = fake_tts

    events = await collect(session.handle_input("hello"))
    await session.background.wait_all()

    assert fake_tts.spoken == []
    assert not any(
        event.type == "background_task_started" and event.kind == "tts" for event in events
    )


def test_npc_voice_tools_are_not_registered(make_session):
    session = make_session(script=[])
    session.set_tts_enabled(True)
    session.reload_tools()

    assert "Narration audio: enabled" in session.build_system()[0].text
    assert "selected Narrator voice" in session.build_system()[0].text
    for name in ("play_dialogue", "stage_dialogue", "cast_lookup"):
        assert name not in session.tools


async def test_entire_turn_uses_narrator_voice_even_around_sound_effects(campaign):
    tts = FakeTTS()
    sfx = FakeSFX()
    host = FakeMediaHost()
    log = _ListLog()
    agent = NarrationAgent(campaign, log, None, tts=tts, sound_effects=sfx, host=host)
    text = 'The guard raises a hand. "Halt, who goes there?" The hall falls quiet.'

    await agent.play_turn(
        text,
        sound_effects=[SoundEffectCue(description="armor rustles", after_text="raises a hand.")],
    )

    assert "".join(tts.spoken) == text
    assert all(call["voice_id"] is None for call in tts.calls)
    assert log.entries[-1][1]["voice_id"] == "narrator-voice"
    assert "voice_cues" not in log.entries[-1][1]


async def test_interrupt_narration_cancels_queue_and_stops_audio(make_session):
    host = FakeMediaHost(speech_delay=60)
    session = make_session(script=[], media_host=host)
    session.tts = FakeTTS()
    session.set_tts_enabled(True)

    session.queue_narration("first")
    session.queue_narration("second")
    await host.started.wait()
    cancelled = session.interrupt_narration()
    await session.background.wait_all()

    assert cancelled >= 1
    assert host.stopped_audio >= 1


async def test_narration_replay_reuses_cached_audio(make_session):
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    fake_tts = FakeTTS()
    session.tts = fake_tts
    session.set_tts_enabled(True)

    session.queue_narration("The torch gutters in the cold draft.")
    await session.background.wait_all()
    started = session.replay_narration()
    assert started is not None
    await session.background.wait_all()

    assert host.speech == ["clip::The torch gutters in the cold draft."] * 2
    assert fake_tts.spoken == ["The torch gutters in the cold draft."]


def test_tts_enabled_persists(make_session):
    session = make_session(script=[])
    session.set_tts_enabled(True)

    assert session.campaign.load_meta().tts_enabled
    assert make_session(script=[]).meta.tts_enabled


async def test_tts_command_toggles_saved_mode(make_session):
    session = make_session(script=[])
    repl = Repl(Console(record=True), session)

    await repl._cmd_tts("on")
    assert session.campaign.load_meta().tts_enabled

    await repl._cmd_tts("off")
    assert not session.campaign.load_meta().tts_enabled


async def test_tts_command_prompts_for_elevenlabs_key(make_session, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda prompt: "el-test-key" if "API key" in prompt else ""
    )
    session = make_session(script=[])
    session.tts = ElevenLabsTTS()
    repl = Repl(Console(record=True), session)

    await repl._cmd_tts("on")

    assert session.tts.api_key == "el-test-key"
    assert "ELEVENLABS_API_KEY=el-test-key" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_ensure_elevenlabs_api_key_sets_process_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda prompt: "el-direct-key" if "API key" in prompt else "n"
    )

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
    assert isinstance(sfx, ElevenLabsSoundEffects)


def test_speech_text_strips_markdown_for_narration():
    spoken = speech_text("# Scene\n\n- **Listen:** [the door](https://example.com) says `clink`.")

    assert spoken == "Scene. Listen: the door says clink."


def test_extract_voice_id_handles_urls_and_bare_ids():
    assert (
        extract_voice_id("https://elevenlabs.io/app/voice-library?voiceId=6FiCmD8eY5VyjOdG5Zjk")
        == "6FiCmD8eY5VyjOdG5Zjk"
    )
    assert extract_voice_id("  6FiCmD8eY5VyjOdG5Zjk  ") == "6FiCmD8eY5VyjOdG5Zjk"
    assert extract_voice_id('"abc123"') == "abc123"
    assert extract_voice_id("https://elevenlabs.io/voices/xyz789") == "xyz789"


def test_set_narrator_voice_overrides_then_clears_to_default(make_session):
    session = make_session(script=[])
    assert session.narrator_voice_id() is None
    assert session.tts.voice_id == DEFAULT_ELEVENLABS_VOICE_ID

    session.set_narrator_voice_id("custom-narrator-voice")

    assert session.narrator_voice_id() == "custom-narrator-voice"
    assert session.tts.voice_id == "custom-narrator-voice"
    assert session.campaign.load_meta().settings["narrator_voice_id"] == ("custom-narrator-voice")

    session.set_narrator_voice_id(None)

    assert session.narrator_voice_id() is None
    assert session.tts.voice_id == DEFAULT_ELEVENLABS_VOICE_ID
    assert "narrator_voice_id" not in session.campaign.load_meta().settings


def test_narrator_voice_survives_reload_tools(make_session):
    session = make_session(script=[])
    session.set_narrator_voice_id("pinned-voice")

    session.reload_tools()

    assert session.tts.voice_id == "pinned-voice"
