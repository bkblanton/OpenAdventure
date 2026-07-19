"""Usage estimates, persistence, and browser API contracts."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from openadventure.engine.context import est_tokens
from openadventure.engine.tools.ambience_tools import make_ambience_tools
from openadventure.engine.tools.registry import ToolRegistry
from openadventure.media.music import MusicTrack
from openadventure.media.narration import NarrationAgent
from openadventure.media.tasks import BackgroundTasks
from openadventure.media.tts import speech_text
from openadventure.providers.base import PTextDelta, PThinking, PTurnDone, Usage
from openadventure.store import snapshots
from openadventure.web.app import create_app
from tests.conftest import collect
from tests.test_sheet_tools import make_ctx


class _ImageBackend:
    ready = True
    model_id = "test-image"

    async def generate(self, subject: str, description: str, *, reference_images=None) -> Path:
        return Path("image.png")


class _MusicBackend:
    ready = True
    model_id = "test-music"

    async def generate(self, prompt: str, *, length_seconds=None, allow_vocals=False) -> MusicTrack:
        return MusicTrack(
            prompt=prompt,
            path=Path("music.mp3"),
            length_seconds=float(length_seconds or 120),
        )


class _SoundEffectsBackend:
    ready = True
    model_id = "test-sfx"

    async def generate(
        self,
        description: str,
        *,
        duration_seconds=None,
        prompt_influence=None,
        loop=False,
    ) -> Path:
        return Path("sound.mp3")


class _TTSBackend:
    ready = True
    model_id = "test-tts"
    voice_id = "test-voice"

    async def synthesize(self, text: str, *, voice_id: str | None = None) -> str:
        return "spoken.mp3"


def test_usage_add_includes_thinking_and_media_dimensions():
    first = Usage(
        input_tokens=100,
        output_tokens=80,
        thinking_tokens=30,
        image_count=1,
        tts_characters=240,
        sound_effect_seconds=1.5,
        music_seconds=45,
    )
    second = Usage(
        input_tokens=25,
        output_tokens=20,
        thinking_tokens=5,
        image_count=2,
        tts_characters=60,
        sound_effect_seconds=2.25,
        music_seconds=15,
    )

    total = first.add(second)

    assert total.input_tokens == 125
    assert total.output_tokens == 100
    assert total.thinking_tokens == 35
    assert total.image_count == 3
    assert total.tts_characters == 300
    assert total.sound_effect_seconds == 3.75
    assert total.music_seconds == 60


async def test_turn_estimates_thinking_when_provider_omits_breakdown(make_session):
    thought = "First I map the room, then I decide which clue matters."
    session = make_session(
        script=[
            [
                PThinking(thinking=thought, signature="thought-1"),
                PTextDelta(text="You find a hidden latch."),
                PTurnDone(stop_reason="end_turn", usage=Usage(input_tokens=20, output_tokens=30)),
            ]
        ]
    )

    events = await collect(session.handle_input("I examine the room."))
    completed = events[-1]

    assert completed.usage.output_tokens == 30
    assert completed.usage.thinking_tokens == est_tokens(thought)
    assert session.usage_report()["totals"]["thinking_tokens"] == est_tokens(thought)


async def test_media_tools_record_only_completed_estimated_usage(workspace, campaign):
    records: list[tuple[Usage, str, str, str | None]] = []
    context = make_ctx(workspace, campaign)
    context.background = BackgroundTasks()
    registry = ToolRegistry()
    for tool in make_ambience_tools(
        _ImageBackend(),
        _MusicBackend(),
        _SoundEffectsBackend(),
        usage_recorder=lambda usage, kind, backend, model: records.append(
            (usage, kind, backend, model)
        ),
    ):
        registry.register(tool)

    registry.dispatch(context, "generate_image", {"subject": "the tower", "description": "rain"})
    registry.dispatch(context, "play_music", {"prompt": "quiet strings", "length_seconds": 12})
    registry.dispatch(
        context,
        "play_sound_effect",
        {"description": "thunder", "duration_seconds": 1.5},
    )

    # The API work is backgrounded. Enqueueing a request must not itself count
    # as generation or billable usage.
    assert records == []
    await context.background.wait_all()

    by_kind = {kind: (usage, backend, model) for usage, kind, backend, model in records}
    assert by_kind["image"] == (Usage(image_count=1), "_ImageBackend", "test-image")
    assert by_kind["music"] == (Usage(music_seconds=12), "_MusicBackend", "test-music")
    assert by_kind["sound_effect"] == (
        Usage(sound_effect_seconds=1.5),
        "_SoundEffectsBackend",
        "test-sfx",
    )


async def test_narration_records_cleaned_tts_character_estimate(workspace, campaign):
    records: list[tuple[Usage, str, str, str | None]] = []
    text = "**The door** opens."
    narration = NarrationAgent(
        campaign,
        campaign.open_log(),
        BackgroundTasks(),
        tts=_TTSBackend(),
        usage_recorder=lambda usage, kind, backend, model: records.append(
            (usage, kind, backend, model)
        ),
    )

    await narration.play_line(text)

    assert records == [
        (
            Usage(tts_characters=len(speech_text(text))),
            "tts",
            "_TTSBackend",
            "test-tts",
        )
    ]


async def test_direct_start_music_records_estimated_usage(make_session):
    session = make_session(script=[])
    session.music = _MusicBackend()

    started = session.start_music("quiet strings")

    assert started is not None
    await session.background.wait_all()
    assert session.usage_report()["totals"]["music_seconds"] == 120


def test_usage_report_migrates_legacy_persisted_usage(make_session, campaign):
    snapshots.save_json(
        campaign.usage_path,
        {
            "totals": {
                "input_tokens": 12,
                "output_tokens": 8,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 4,
            },
            "cost_usd": 0.012,
            "by_model": {
                "legacy-model": {
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 4,
                    "cost_usd": 0.012,
                }
            },
        },
    )
    report = make_session(script=[]).usage_report()

    assert (
        report["totals"]
        == Usage(
            input_tokens=12,
            output_tokens=8,
            cache_creation_input_tokens=2,
            cache_read_input_tokens=4,
        ).model_dump()
    )
    assert report["by_model"]["legacy-model"] == {
        **Usage(
            input_tokens=12,
            output_tokens=8,
            cache_creation_input_tokens=2,
            cache_read_input_tokens=4,
        ).model_dump(),
        "cost_usd": 0.012,
    }
    assert report["cost_breakdown"] == {
        "text": 0.012,
        "images": 0.0,
        "tts": 0.0,
        "sound_effects": 0.0,
        "music": 0.0,
        "total": 0.012,
    }
    assert report["estimated"]["thinking_tokens"] == 0
    assert report["estimated_cost_usd"] == 0.012


def test_usage_report_combines_thinking_media_and_cost_components(make_session):
    session = make_session(script=[])
    session.accrue_usage(Usage(input_tokens=1_000, output_tokens=100, thinking_tokens=80))
    session.accrue_media_usage(
        Usage(image_count=2), "image", "GeminiImageBackend", "gemini-3.1-flash-image"
    )
    session.accrue_media_usage(
        Usage(tts_characters=100), "tts", "ElevenLabsTTS", "eleven_flash_v2_5"
    )
    session.accrue_media_usage(
        Usage(sound_effect_seconds=3),
        "sound_effect",
        "ElevenLabsSoundEffects",
        "eleven_text_to_sound_v2",
    )
    session.accrue_media_usage(Usage(music_seconds=4), "music", "ElevenLabsMusic", "music_v1")

    report = session.usage_report()

    assert (
        report["totals"]
        == Usage(
            input_tokens=1_000,
            output_tokens=100,
            thinking_tokens=80,
            image_count=2,
            tts_characters=100,
            sound_effect_seconds=3,
            music_seconds=4,
        ).model_dump()
    )
    # GPT-5.6 Luna bills 1M input tokens at $1 and output at $6. Thinking is
    # already included in those 100 output tokens, so text cost is $0.0016,
    # not a second reasoning charge.
    assert report["cost_breakdown"] == pytest.approx(
        {
            "text": 0.0016,
            "images": 0.134,
            "tts": 0.0075,
            "sound_effects": 0.009999,
            "music": 0.04,
            "total": 0.193099,
        }
    )
    assert report["cost_usd"] == pytest.approx(0.193099)
    assert report["estimated_cost_usd"] == pytest.approx(report["cost_usd"])
    assert report["estimated"]["thinking_tokens"] == 80
    assert report["estimated"]["music_seconds"] == 4


async def test_usage_endpoint_matches_campaign_and_state_payload(config):
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            campaign = app.state.workspace.create_campaign("Usage API")
            handle = await app.state.sessions.get(campaign.load_meta().slug)
            handle.session.accrue_usage(Usage(input_tokens=17, output_tokens=5, thinking_tokens=3))
            handle.session.accrue_media_usage(
                Usage(image_count=1), "image", "GeminiImageBackend", "gemini-3.1-flash-image"
            )
            expected = handle.session.usage_report()

            payload = (await client.get("/api/campaigns/usage-api")).json()
            endpoint = await client.get("/api/campaigns/usage-api/usage")
            homepage = await client.get("/")
    finally:
        await app.state.library_jobs.close()
        await app.state.sessions.close_all()

    assert payload["usage"] == expected
    assert payload["state"]["usage"] == expected
    assert endpoint.status_code == 200
    assert endpoint.json() == {"usage": expected}
    assert 'id="usage-tab"' in homepage.text
    assert 'data-inspector-tab="usage"' in homepage.text
