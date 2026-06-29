"""Music feature: ElevenLabs backend, looping player, tools, session wiring."""

import asyncio
import json
from pathlib import Path

from openadventure.engine.events import MusicStarted, MusicStopped
from openadventure.engine.tools import build_registry
from openadventure.engine.tools.ambience_tools import make_ambience_tools
from openadventure.engine.tools.registry import ToolRegistry
from openadventure.mechanics.encounter import Combatant, Encounter
from openadventure.media.music import (
    ElevenLabsMusic,
    LoopingAudioPlayer,
    MusicTrack,
    persist_track,
)
from openadventure.media.tasks import BackgroundTasks
from openadventure.providers.base import PToolUse, PTurnDone, Usage
from openadventure.store import snapshots
from tests.conftest import FakeMediaHost, collect
from tests.test_agent_loop import text_turn
from tests.test_sheet_tools import make_ctx


class FakeMusicBackend:
    """Generation-only music backend: composes a track. Looping/volume/status
    are the host's job now (see FakeMediaHost)."""

    ready = True
    configuration_hint = ""

    def __init__(self, delay: float = 0.01):
        self.delay = delay
        self.calls = []

    async def generate(self, prompt, *, length_seconds=None, allow_vocals=False):
        await asyncio.sleep(self.delay)
        self.calls.append(
            {"prompt": prompt, "length_seconds": length_seconds, "allow_vocals": allow_vocals}
        )
        return MusicTrack(
            prompt=prompt, path=Path("music/track.mp3"), length_seconds=length_seconds or 120
        )


# --- tools -------------------------------------------------------------------


def _registry(music) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in make_ambience_tools(None, music):
        registry.register(tool)
    return registry


def _enable_fake_auto_music(session, music: FakeMusicBackend | None = None) -> FakeMusicBackend:
    music = music or FakeMusicBackend()
    session.meta.music_enabled = True
    session.meta.settings["music_auto"] = True
    session.music = music
    session.tools = build_registry(
        session.workspace,
        session.campaign,
        session.meta,
        media_backends=(None, music, None, None),
    )
    return music


def _text_blocks_from_second_call(session) -> list[str]:
    return [
        block.text
        for block in session.provider.calls[1].messages[-1].content
        if block.type == "text"
    ]


async def test_play_music_runs_in_background(workspace, campaign):
    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    music = FakeMusicBackend()
    registry = _registry(music)

    outcome = registry.dispatch(
        ctx, "play_music", {"prompt": "tense dungeon ambience", "length_seconds": 60}
    )
    assert outcome.ok
    assert "background" in outcome.content
    assert outcome.events[0].type == "background_task_started"
    assert ctx.background.pending == 1

    await ctx.background.wait_all()
    events = ctx.background.drain()
    started = next(e for e in events if isinstance(e, MusicStarted))
    assert started.track == "tense dungeon ambience"
    assert music.calls == [
        {"prompt": "tense dungeon ambience", "length_seconds": 60, "allow_vocals": False}
    ]
    media_entries = [e for e in ctx.log.read_all() if e.type == "media"]
    assert media_entries and media_entries[-1].data["kind"] == "music"


async def test_stop_music_cancels_pending_generation(workspace, campaign):
    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    ctx.media_host = FakeMediaHost()
    music = FakeMusicBackend(delay=5)
    registry = _registry(music)

    registry.dispatch(ctx, "play_music", {"prompt": "slow doom drums"})
    outcome = registry.dispatch(ctx, "stop_music", {})
    assert outcome.ok

    await ctx.background.wait_all()
    events = ctx.background.drain()
    assert any(isinstance(e, MusicStopped) for e in events)
    assert ctx.media_host.music_stopped == 1
    assert music.calls == []  # generation was cancelled before completing


async def test_new_play_supersedes_pending_generation(workspace, campaign):
    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    music = FakeMusicBackend(delay=0.05)
    registry = _registry(music)

    registry.dispatch(ctx, "play_music", {"prompt": "first track"})
    registry.dispatch(ctx, "play_music", {"prompt": "second track"})
    await ctx.background.wait_all()
    assert [c["prompt"] for c in music.calls] == ["second track"]


def test_play_music_reports_missing_key(workspace, campaign):
    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    music = FakeMusicBackend()
    music.ready = False
    music.configuration_hint = "Set ELEVENLABS_API_KEY."
    registry = _registry(music)

    outcome = registry.dispatch(ctx, "play_music", {"prompt": "anything"})
    assert not outcome.ok
    assert "ELEVENLABS_API_KEY" in outcome.content
    assert ctx.background.pending == 0


# --- registry gating + session wiring ---------------------------------------


def test_music_tools_gated_by_meta_flag(workspace, campaign):
    meta = campaign.load_meta()
    backends = (None, FakeMusicBackend(), None, None)
    registry = build_registry(workspace, campaign, meta, media_backends=backends)
    assert "play_music" not in registry

    meta.music_enabled = True
    registry = build_registry(workspace, campaign, meta, media_backends=backends)
    assert "play_music" in registry
    assert "stop_music" in registry


async def test_music_toggle_controls_tools_and_prompt(make_session):
    session = make_session(script=[text_turn("Quiet."), text_turn("Drums!")])
    assert not session.meta.music_enabled
    assert "play_music" not in session.tools
    assert "Background music: disabled" in session.build_system()[0].text

    await collect(session.handle_input("begin"))
    first_tools = {tool.name for tool in session.provider.calls[0].tools}
    assert "play_music" not in first_tools

    session.set_music_enabled(True)
    assert session.campaign.load_meta().music_enabled
    assert "play_music" in session.tools
    assert "Background music: enabled (auto)" in session.build_system()[0].text

    session.set_music_auto(False)
    assert "Background music: enabled (manual)" in session.build_system()[0].text
    session.set_music_auto(True)

    await collect(session.handle_input("more"))
    second_tools = {tool.name for tool in session.provider.calls[1].tools}
    assert "play_music" in second_tools

    session.set_music_enabled(False)
    assert "play_music" not in session.tools


async def test_auto_music_check_after_scene_change(make_session):
    session = make_session(
        script=[
            [
                PToolUse(
                    id="scene1",
                    name="update_scene",
                    input={"location": "Blue Water Inn", "description": "warm tavern bustle"},
                ),
                PTurnDone(stop_reason="tool_use", usage=Usage()),
            ],
            text_turn("The inn's lamplight spills over crowded tables."),
        ]
    )
    _enable_fake_auto_music(session)

    await collect(session.handle_input("we head into the tavern"))

    hints = _text_blocks_from_second_call(session)
    assert any("auto music check: the scene changed" in text for text in hints)
    assert any("Current background music: no music playing" in text for text in hints)
    assert any("call play_music now" in text for text in hints)


async def test_auto_music_check_skipped_in_manual_mode(make_session):
    session = make_session(
        script=[
            [
                PToolUse(
                    id="scene1",
                    name="update_scene",
                    input={"location": "Blue Water Inn", "description": "warm tavern bustle"},
                ),
                PTurnDone(stop_reason="tool_use", usage=Usage()),
            ],
            text_turn("The inn is warm and busy."),
        ]
    )
    _enable_fake_auto_music(session)
    session.meta.settings["music_auto"] = False

    await collect(session.handle_input("we head into the tavern"))

    hints = _text_blocks_from_second_call(session)
    assert all("auto music check" not in text for text in hints)


async def test_auto_music_check_after_combat_ends(make_session, campaign):
    snapshots.save_json(
        campaign.encounter_path,
        Encounter(
            name="Wolf attack",
            combatants=[Combatant(tag="Dire Wolf", side="foe", initiative=12)],
        ),
    )
    session = make_session(
        script=[
            [
                PToolUse(id="enc1", name="update_encounter", input={"end": True}),
                PTurnDone(stop_reason="tool_use", usage=Usage()),
            ],
            text_turn("The last wolf flees into the pines."),
        ],
        media_host=FakeMediaHost(),
    )
    _enable_fake_auto_music(session)
    session.media_host.play_music(
        Path("battle.mp3"), prompt="fast battle drums and low brass", length_seconds=90
    )

    await collect(session.handle_input("the fight is over"))

    hints = _text_blocks_from_second_call(session)
    assert any("auto music check: combat ended" in text for text in hints)
    assert any("fast battle drums and low brass" in text for text in hints)


def test_assistant_mode_music_prompt(make_session):
    session = make_session(script=[])
    session.set_mode("assistant")
    session.set_music_enabled(True)
    text = session.build_system()[0].text
    assert "when the GM asks" in text


def test_session_context_block_shows_now_playing(make_session):
    session = make_session(script=[text_turn("ok")], media_host=FakeMediaHost())
    session.set_music_enabled(True)
    session.media_host.play_music(Path("x.mp3"), prompt="gentle forest theme", length_seconds=90)
    messages, _ = session.build_messages()
    context = "\n".join(b.text for m in messages for b in m.content if b.type == "text")
    assert "## Background music" in context
    assert "gentle forest theme" in context


def test_session_volume_persists_and_applies(make_session):
    session = make_session(script=[], media_host=FakeMediaHost())
    volume = session.set_music_volume(0.3)
    assert volume == 0.3
    assert session.campaign.load_meta().settings["music_volume"] == 0.3
    session._apply_music_volume()
    assert session.media_host.music_volume() == 0.3


def test_session_close_stops_music(make_session):
    session = make_session(script=[], media_host=FakeMediaHost())
    session.close()
    assert session.media_host.music_stopped == 1


def test_persist_track_copies_into_music_dir_with_readable_name(tmp_path):
    src = tmp_path / "cache" / "deadbeef.mp3"
    src.parent.mkdir()
    src.write_bytes(b"audio-bytes")
    music_dir = tmp_path / "campaign" / "music"

    dest = persist_track(music_dir, src, "eerie crypt ambience, dripping water")

    assert dest.parent == music_dir
    assert dest.read_bytes() == b"audio-bytes"
    # Readable, prompt-derived name (slug + content digest), suffix preserved.
    assert dest.suffix == ".mp3"
    assert dest.name.startswith("eerie-crypt-ambience-dripping-water-")


def test_persist_track_caps_long_prompt_slug(tmp_path):
    src = tmp_path / "track.mp3"
    src.write_bytes(b"x")
    prompt = "haunted house interior ambience, 1920s, very quiet sustained low drone, creaks"

    dest = persist_track(tmp_path / "music", src, prompt)

    stem_before_digest = dest.stem.rsplit("-", 1)[0]
    assert len(stem_before_digest) <= 60


def test_persist_track_is_idempotent(tmp_path):
    src = tmp_path / "track.mp3"
    src.write_bytes(b"same")
    music_dir = tmp_path / "music"

    first = persist_track(music_dir, src, "warm tavern tune")
    second = persist_track(music_dir, src, "warm tavern tune")

    assert first == second
    assert list(music_dir.glob("*.mp3")) == [first]


def test_persist_track_passes_through_a_missing_source(tmp_path):
    # The test fakes (and any backend returning a placeholder) point at no real
    # file; persist_track returns the source unchanged rather than copying.
    missing = tmp_path / "nope.mp3"
    assert persist_track(tmp_path / "music", missing, "anything") == missing
    assert not (tmp_path / "music").exists()


def _logged_track(session, tmp_path, prompt: str, *, name: str = "track.mp3") -> Path:
    """Write a real (empty) audio file and log a music event pointing at it, the
    way a finished generation would. Resume replays this file straight from disk."""
    path = tmp_path / name
    path.write_bytes(b"\x00")
    session.log.append(
        "media",
        {"kind": "music", "prompt": prompt, "path": str(path), "length_seconds": 90},
    )
    return path


async def test_resume_music_replays_last_track_from_disk(make_session, tmp_path):
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    music = _enable_fake_auto_music(session)
    path = _logged_track(session, tmp_path, "warm tavern folk tune")

    resumed = session.resume_music()

    # Replayed from disk through the host: no background task, no API call.
    assert resumed == "warm tavern folk tune"
    assert host.music[-1] == (str(path), "warm tavern folk tune")
    assert music.calls == []
    assert session.background.drain() == []


def test_resume_music_replays_the_most_recent_track(make_session, tmp_path):
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    _enable_fake_auto_music(session)
    _logged_track(session, tmp_path, "warm tavern folk tune", name="a.mp3")
    latest = _logged_track(session, tmp_path, "tense dungeon drums", name="b.mp3")

    session.resume_music()

    assert host.music[-1] == (str(latest), "tense dungeon drums")


def test_resume_music_skips_after_stop(make_session, tmp_path):
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    _enable_fake_auto_music(session)
    _logged_track(session, tmp_path, "warm tavern folk tune")
    session.log.append("media", {"kind": "music", "action": "stop"})

    assert session.resume_music() is None
    assert host.music == []


def test_resume_music_skips_when_file_missing(make_session, tmp_path):
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    _enable_fake_auto_music(session)
    path = _logged_track(session, tmp_path, "warm tavern folk tune")
    path.unlink()  # the cached render was cleaned up since it last played

    assert session.resume_music() is None
    assert host.music == []


def test_stop_music_marks_a_stop_that_blocks_resume(make_session, tmp_path):
    # The player stops the music, then quits and comes back: it must stay off,
    # not resume the track they silenced. /music stop logs a stop marker; close()
    # (a plain exit) does not, so the marker is the last music event on return.
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    _enable_fake_auto_music(session)
    _logged_track(session, tmp_path, "warm tavern folk tune")

    session.stop_music()  # the /music stop path
    session.close()  # quit

    assert session.last_music_track() is None
    assert session.resume_music() is None


def test_close_alone_does_not_block_resume(make_session, tmp_path):
    # A clean exit while music was playing must NOT count as a stop; resuming
    # next session should bring the track back.
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    _enable_fake_auto_music(session)
    path = _logged_track(session, tmp_path, "warm tavern folk tune")

    session.close()

    assert session.resume_music() == "warm tavern folk tune"
    assert host.music[-1] == (str(path), "warm tavern folk tune")


def test_resume_music_skips_when_auto_off(make_session, tmp_path):
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    _enable_fake_auto_music(session)
    session.meta.settings["music_auto"] = False
    _logged_track(session, tmp_path, "warm tavern folk tune")

    assert session.resume_music() is None


def test_resume_music_skips_without_prior_music(make_session):
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    _enable_fake_auto_music(session)

    assert session.resume_music() is None


def test_replay_music_ignores_auto_and_mode_gates(make_session, tmp_path):
    # /music resume calls replay_music directly: a manual replay should work even
    # with auto music off, as long as the host can play and a track is on disk.
    host = FakeMediaHost()
    session = make_session(script=[], media_host=host)
    _enable_fake_auto_music(session)
    session.meta.settings["music_auto"] = False
    path = _logged_track(session, tmp_path, "warm tavern folk tune")

    assert session.replay_music() == "warm tavern folk tune"
    assert host.music[-1] == (str(path), "warm tavern folk tune")


def _render_to_text(events) -> str:
    import io

    from rich.console import Console

    from openadventure.cli.render import EventRenderer

    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=False)
    EventRenderer(console).render_events(events)
    return buf.getvalue()


def test_music_task_finished_line_suppressed():
    from openadventure.engine.events import BackgroundTaskFinished

    out = _render_to_text(
        [
            MusicStarted(track="warm tavern folk tune"),
            BackgroundTaskFinished(
                task_id="music-1", ok=True, message="composing music: warm tavern folk tune (done)"
            ),
        ]
    )
    assert "Now playing on loop" in out
    assert "done" not in out  # the redundant generic completion line is gone


def test_music_task_failure_line_still_shown():
    from openadventure.engine.events import BackgroundTaskFinished

    out = _render_to_text(
        [BackgroundTaskFinished(task_id="music-1", ok=False, message="composing music: x (boom)")]
    )
    assert "boom" in out


def test_successful_task_finished_line_suppressed():
    from openadventure.engine.events import BackgroundTaskFinished

    # Every task announces its result via a dedicated event (image_generated,
    # music_started, …), so the generic "(done)" completion line is just chatter.
    out = _render_to_text(
        [BackgroundTaskFinished(task_id="image-1", ok=True, message="generating image (done)")]
    )
    assert "done" not in out


# --- ElevenLabs backend -------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


async def test_elevenlabs_music_request_and_cache(tmp_path, monkeypatch):
    requests = []

    def fake_urlopen(request, timeout=None):
        requests.append(request)
        return _FakeResponse(b"audio-bytes")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    backend = ElevenLabsMusic(api_key="k", cache_dir=tmp_path)
    track = await backend.generate("brooding swamp ambience", length_seconds=45)

    assert len(requests) == 1
    request = requests[0]
    assert request.full_url.startswith("https://api.elevenlabs.io/v1/music?")
    assert request.get_header("Xi-api-key") == "k"
    payload = json.loads(request.data.decode("utf-8"))
    assert payload == {
        "prompt": "brooding swamp ambience",
        "model_id": "music_v1",
        "music_length_ms": 45000,
        "force_instrumental": True,
    }
    assert track.path.read_bytes() == b"audio-bytes"
    assert track.prompt == "brooding swamp ambience"

    # same prompt+length is served from cache without another request
    await backend.generate("brooding swamp ambience", length_seconds=45)
    assert len(requests) == 1
    # a different prompt generates again
    await backend.generate("triumphant fanfare", length_seconds=45)
    assert len(requests) == 2


async def test_elevenlabs_music_clamps_length(tmp_path, monkeypatch):
    payloads = []

    def fake_urlopen(request, timeout=None):
        payloads.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse(b"x")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    backend = ElevenLabsMusic(api_key="k", cache_dir=tmp_path)
    await backend.generate("a", length_seconds=1)
    await backend.generate("b", length_seconds=9999)
    assert payloads[0]["music_length_ms"] == 10000
    assert payloads[1]["music_length_ms"] == 300000


def test_volume_clamping():
    player = LoopingAudioPlayer(volume=2.0)
    assert player.volume == 1.0
    assert player.set_volume(-0.5) == 0.0
    assert player.set_volume(0.45) == 0.45
    assert not player.playing
