"""Ambience: image/sound-effect tools, background tasks, narration-synced cues."""

import asyncio
from pathlib import Path

from openadventure.engine.events import ImageGenerated
from openadventure.engine.tools import build_registry
from openadventure.engine.tools.ambience_tools import make_ambience_tools
from openadventure.engine.tools.registry import ToolRegistry
from openadventure.media.narration import NarrationAgent
from openadventure.media.tasks import BackgroundTasks
from openadventure.providers.base import PToolUse, PTurnDone, Usage
from tests.conftest import collect
from tests.test_agent_loop import text_turn
from tests.test_sheet_tools import make_ctx


class FakeImageBackend:
    ready = True

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.calls = []

    async def generate(self, subject: str, description: str, *, reference_images=None) -> Path:
        await asyncio.sleep(self.delay)
        self.calls.append(
            {"subject": subject, "description": description, "reference_images": reference_images}
        )
        return Path(f"images/{subject}.png")


class FakeSoundEffectsBackend:
    ready = True

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.calls = []

    async def generate(
        self,
        description: str,
        *,
        duration_seconds: float | None = None,
        prompt_influence: float | None = None,
        loop: bool = False,
    ) -> Path:
        await asyncio.sleep(self.delay)
        self.calls.append(
            {
                "description": description,
                "duration_seconds": duration_seconds,
                "prompt_influence": prompt_influence,
                "loop": loop,
            }
        )
        return Path("sfx/door.mp3")


async def test_sound_effect_toggle_controls_agent_tool_visibility(make_session):
    session = make_session(script=[text_turn("Quiet start."), text_turn("Thunder rolls.")])
    assert not session.meta.sound_effects_enabled
    assert "play_sound_effect" not in session.tools
    assert "play_dialogue" not in session.tools
    assert "Sound effects: disabled" in session.build_system()[0].text

    await collect(session.handle_input("begin quietly"))
    first_tool_names = {tool.name for tool in session.provider.calls[0].tools}
    assert "play_sound_effect" not in first_tool_names
    assert "play_dialogue" not in first_tool_names

    session.set_tts_enabled(True)
    session.reload_tools()
    # Narration is automatic and exposes no model-controlled character voice tools.
    assert "play_dialogue" not in session.tools
    assert "stage_dialogue" not in session.tools
    assert "Narration audio: enabled" in session.build_system()[0].text

    session.set_sound_effects_enabled(True)
    assert session.campaign.load_meta().sound_effects_enabled
    assert "play_sound_effect" in session.tools
    assert "stage_sound_effect" in session.tools
    assert "Sound effects: enabled" in session.build_system()[0].text

    await collect(session.handle_input("make it dramatic"))
    second_tool_names = {tool.name for tool in session.provider.calls[1].tools}
    assert "play_sound_effect" in second_tool_names
    assert "stage_sound_effect" in second_tool_names
    assert "play_dialogue" not in second_tool_names
    assert "stage_dialogue" not in second_tool_names

    session.set_sound_effects_enabled(False)
    assert not session.campaign.load_meta().sound_effects_enabled
    assert "play_sound_effect" not in session.tools
    assert "stage_sound_effect" not in session.tools


async def test_background_task_does_not_block(workspace, campaign):
    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    tools = make_ambience_tools(FakeImageBackend(), None)
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)

    outcome = registry.dispatch(
        ctx, "generate_image", {"subject": "the innkeeper", "description": "stout, kind eyes"}
    )
    assert outcome.ok
    assert "background" in outcome.content
    assert outcome.events[0].type == "background_task_started"
    assert ctx.background.pending == 1  # returned before the render finished

    await ctx.background.wait_all()
    events = ctx.background.drain()
    types = [e.type for e in events]
    assert "image_generated" in types
    assert "background_task_finished" in types
    image = next(e for e in events if isinstance(e, ImageGenerated))
    assert image.caption == "the innkeeper"


async def test_sound_effect_tool_does_not_block(workspace, campaign):
    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    sfx = FakeSoundEffectsBackend()
    tools = make_ambience_tools(None, None, sfx)
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)

    outcome = registry.dispatch(
        ctx,
        "play_sound_effect",
        {
            "description": "a heavy crypt door grinding open",
            "duration_seconds": 1.5,
            "prompt_influence": 0.7,
        },
    )

    assert outcome.ok
    assert "background" in outcome.content
    assert outcome.events[0].type == "background_task_started"
    assert ctx.background.pending == 1

    await ctx.background.wait_all()
    events = ctx.background.drain()
    assert any(e.type == "background_task_finished" for e in events)
    assert sfx.calls == [
        {
            "description": "a heavy crypt door grinding open",
            "duration_seconds": 1.5,
            "prompt_influence": 0.7,
            "loop": False,
        }
    ]
    media = next(e for e in ctx.log.read_all() if e.type == "media")
    assert media.data["kind"] == "sound_effect"
    assert Path(media.data["path"]) == Path("sfx/door.mp3")


async def test_sound_effect_tool_stages_cue_when_tts_is_enabled(workspace, campaign):
    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    ctx.meta.tts_enabled = True
    sfx = FakeSoundEffectsBackend()
    ctx.narration = NarrationAgent(campaign, ctx.log, ctx.background, tts=None, sound_effects=sfx)
    tools = make_ambience_tools(None, None, sfx)
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)

    outcome = registry.dispatch(
        ctx,
        "stage_sound_effect",
        {
            "description": "a coffin lid cracking open",
            "duration_seconds": 2.0,
            "after_text": "The coffin lid cracks open.",
        },
    )

    assert outcome.ok
    assert "final visible narration" in outcome.content
    assert outcome.events == []
    assert ctx.background.pending == 0
    assert sfx.calls == []
    assert len(ctx.sound_effect_cues) == 1
    cue = ctx.sound_effect_cues[0]
    assert cue.description == "a coffin lid cracking open"
    assert cue.after_text == "The coffin lid cracks open."


async def test_sound_effect_tool_plays_immediately_in_assistant_mode_with_tts(workspace, campaign):
    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    ctx.meta.mode = "assistant"
    ctx.meta.tts_enabled = True
    sfx = FakeSoundEffectsBackend(delay=0.01)
    ctx.narration = NarrationAgent(campaign, ctx.log, ctx.background, tts=None, sound_effects=sfx)
    tools = make_ambience_tools(None, None, sfx)
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)

    outcome = registry.dispatch(
        ctx,
        "play_sound_effect",
        {
            "description": "a coffin lid cracking open",
            "duration_seconds": 2.0,
        },
    )

    assert outcome.ok
    assert "background" in outcome.content
    assert outcome.events[0].type == "background_task_started"
    assert ctx.sound_effect_cues == []
    await ctx.background.wait_all()
    assert sfx.calls == [
        {
            "description": "a coffin lid cracking open",
            "duration_seconds": 2.0,
            "prompt_influence": None,
            "loop": False,
        }
    ]


async def test_staged_sound_effect_plays_with_final_visible_narration(make_session):
    final_text = "Glass bursts across the floor. The guard shouts from the stair."
    session = make_session(
        script=[
            [
                PToolUse(
                    id="t1",
                    name="stage_sound_effect",
                    input={
                        "description": "a glass vial shattering on stone",
                        "after_text": "Glass bursts across the floor.",
                    },
                ),
                PTurnDone(stop_reason="tool_use", usage=Usage()),
            ],
            text_turn(final_text),
        ]
    )

    class FakeTTS:
        ready = True
        voice_id = "default-voice"

        def __init__(self):
            self.spoken = []

        async def synthesize(self, text: str, *, voice_id: str | None = None) -> str:
            self.spoken.append(text)
            return f"clip::{text}"

    tts = FakeTTS()
    sfx = FakeSoundEffectsBackend(delay=0.01)
    session.tts = tts
    session.sound_effects = sfx
    session.set_tts_enabled(True)
    session.tools = ToolRegistry()
    for tool in make_ambience_tools(None, None, sfx):
        session.tools.register(tool)

    events = await collect(session.handle_input("drop the vial"))
    await session.background.wait_all()

    assert any(e.type == "background_task_started" and e.kind == "tts" for e in events)
    assert "".join(tts.spoken) == final_text
    assert sfx.calls == [
        {
            "description": "a glass vial shattering on stone",
            "duration_seconds": None,
            "prompt_influence": None,
            "loop": False,
        }
    ]


def test_sound_effect_tool_reports_missing_key(workspace, campaign):
    class MissingKeySoundEffects:
        ready = False
        configuration_hint = "Set ELEVENLABS_API_KEY."

        async def generate(self, *args, **kwargs):
            raise AssertionError("should not be called")

    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    registry = ToolRegistry()
    for tool in make_ambience_tools(None, None, MissingKeySoundEffects()):
        registry.register(tool)

    outcome = registry.dispatch(ctx, "play_sound_effect", {"description": "a short thunderclap"})

    assert not outcome.ok
    assert "ELEVENLABS_API_KEY" in outcome.content
    assert ctx.background.pending == 0


async def test_background_failure_reports(workspace, campaign):
    class BrokenBackend:
        ready = True

        async def generate(self, subject, description, *, reference_images=None):
            raise RuntimeError("no GPU")

    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    registry = ToolRegistry()
    for tool in make_ambience_tools(BrokenBackend(), None):
        registry.register(tool)
    registry.dispatch(ctx, "generate_image", {"subject": "x", "description": "y"})
    await ctx.background.wait_all()
    events = ctx.background.drain()
    finished = next(e for e in events if e.type == "background_task_finished")
    assert not finished.ok
    assert "no GPU" in finished.message


def test_ambience_tools_absent_without_backends(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    assert "generate_image" not in registry
    assert "play_music" not in registry
    assert "play_sound_effect" not in registry


async def test_background_events_flow_through_turn(make_session, workspace, campaign):
    """An ambience tool fired mid-turn surfaces its started event in the stream."""
    session = make_session(
        script=[
            [
                PToolUse(
                    id="t1", name="generate_image", input={"subject": "inn", "description": "cozy"}
                ),
                PTurnDone(stop_reason="tool_use", usage=Usage()),
            ],
            text_turn("The inn glows warmly ahead."),
        ]
    )
    from openadventure.engine.tools.ambience_tools import make_ambience_tools as mat

    for tool in mat(FakeImageBackend(delay=0.01), None):
        session.tools.register(tool)
    events = await collect(session.handle_input("show me the inn"))
    types = [e.type for e in events]
    assert "background_task_started" in types
    await session.background.wait_all()
    later = session.background.drain()
    assert any(e.type == "image_generated" for e in later)


async def test_sound_effect_events_flow_through_turn(make_session):
    session = make_session(
        script=[
            [
                PToolUse(
                    id="t1",
                    name="play_sound_effect",
                    input={"description": "a glass vial shattering on stone"},
                ),
                PTurnDone(stop_reason="tool_use", usage=Usage()),
            ],
            text_turn("Glass bursts across the floor."),
        ]
    )

    session.tools = ToolRegistry()
    for tool in make_ambience_tools(None, None, FakeSoundEffectsBackend(delay=0.01)):
        session.tools.register(tool)
    events = await collect(session.handle_input("drop the vial"))
    types = [e.type for e in events]
    assert "background_task_started" in types
    await session.background.wait_all()
    later = session.background.drain()
    assert any(e.type == "background_task_finished" for e in later)
