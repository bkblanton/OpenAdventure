"""Browser API behavior, including the server-side privacy boundary."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from openadventure.cli.main import _cmd_web, build_parser
from openadventure.config import save_local_api_key
from openadventure.engine.events import (
    DebugChatter,
    EngineEvent,
    RollResult,
    StateChanged,
    ToolFinished,
    TurnCompleted,
    TurnStarted,
)
from openadventure.engine.session import resolve_utility_settings
from openadventure.providers.base import ModelRegistry, PTextDelta, PTurnDone, Usage
from openadventure.providers.fake import FakeProvider
from openadventure.store.eventlog import EventLog
from openadventure.web.app import create_app


@pytest.fixture
async def web_client(config):
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield app, client
    await app.state.library_jobs.close()
    await app.state.sessions.close_all()


def _ndjson(response: httpx.Response) -> list[dict]:
    assert response.headers["content-type"].startswith("application/x-ndjson")
    payloads = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert payloads
    assert all(isinstance(payload, dict) for payload in payloads)
    return payloads


def _scripted_events(
    events: list[EngineEvent],
) -> Callable[..., AsyncIterator[EngineEvent]]:
    async def handle_input(_text: str, **_kwargs: object) -> AsyncIterator[EngineEvent]:
        for event in events:
            yield event

    return handle_input


async def test_bootstrap_create_duplicate_and_unknown_campaign(web_client):
    app, client = web_client

    initial = await client.get("/api/bootstrap")
    assert initial.status_code == 200
    initial_payload = initial.json()
    assert initial_payload["campaigns"] == []
    assert initial_payload["books"] == []
    assert [model["id"] for model in initial_payload["models"]] == [
        model.id for model in ModelRegistry.load_default().visible
    ]
    assert initial_payload["utility_model"] == resolve_utility_settings(app.state.config).model

    created = await client.post(
        "/api/campaigns",
        json={
            "name": "Lantern Keep",
            "mode": "assistant",
            "premise": "A beacon has gone dark.",
        },
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["campaign"]["name"] == "Lantern Keep"
    assert payload["campaign"]["slug"] == "lantern-keep"
    assert payload["campaign"]["mode"] == "assistant"
    assert payload["campaign"]["premise"] == "A beacon has gone dark."
    assert payload["history"] == []
    assert app.state.workspace.campaign("lantern-keep").load_meta().name == "Lantern Keep"

    duplicate = await client.post("/api/campaigns", json={"name": "Lantern Keep"})
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["error"]

    missing = await client.get("/api/campaigns/not-a-campaign")
    assert missing.status_code == 404
    assert "no campaign named" in missing.json()["error"]

    refreshed = (await client.get("/api/bootstrap")).json()
    assert [campaign["slug"] for campaign in refreshed["campaigns"]] == ["lantern-keep"]


async def test_local_mutation_guard_rejects_untrusted_requests(web_client):
    app, client = web_client

    wrong_type = await client.post(
        "/api/campaigns",
        content='{"name":"Wrong Content Type"}',
        headers={"content-type": "text/plain"},
    )
    assert wrong_type.status_code == 415
    assert app.state.workspace.list_campaigns() == []

    hostile_origin = await client.post(
        "/api/campaigns",
        json={"name": "Hostile Origin"},
        headers={"origin": "http://evil.example"},
    )
    assert hostile_origin.status_code == 403
    assert app.state.workspace.list_campaigns() == []

    hostile_host = await client.get(
        "/api/health",
        headers={"host": "evil.example"},
    )
    assert hostile_host.status_code == 400


async def test_homepage_and_static_assets_are_served(web_client):
    _app, client = web_client

    homepage = await client.get("/")
    assert homepage.status_code == 200
    assert homepage.headers["content-type"].startswith("text/html")
    assert "OpenAdventure" in homepage.text
    assert 'id="app"' in homepage.text

    stylesheet = await client.get("/static/styles.css")
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert stylesheet.headers["cache-control"] == "no-store"
    assert stylesheet.text.strip()

    script = await client.get("/static/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert script.headers["cache-control"] == "no-store"
    assert script.text.strip()


async def test_campaign_kickoff_instruction_is_hidden_from_browser_history(web_client):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Opening Scene")
    log = EventLog(campaign.log_path)
    log.append("user_message", {"text": "[START OF CAMPAIGN. Internal GM setup note.]"})
    log.append("gm_message", {"text": "Welcome to the adventure."})

    response = await client.get("/api/campaigns/opening-scene")

    assert response.status_code == 200
    history = response.json()["history"]
    assert len(history) == 1
    assert history[0]["seq"] == 2
    assert history[0]["type"] == "gm_message"
    assert history[0]["role"] == "assistant"
    assert history[0]["text"] == "Welcome to the adventure."


async def test_turn_stream_is_valid_ndjson_from_fake_provider(web_client):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Stream Test")
    handle = await app.state.sessions.get(campaign.load_meta().slug)
    handle.session.provider = FakeProvider(
        script=[
            [
                PTextDelta(text="The iron door opens."),
                PTurnDone(
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=17, output_tokens=5),
                ),
            ]
        ]
    )

    response = await client.post(
        "/api/campaigns/stream-test/turn",
        json={"text": "I open the door."},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    events = _ndjson(response)
    assert [event["type"] for event in events] == [
        "turn_started",
        "assistant_text_delta",
        "turn_completed",
        "state_snapshot",
    ]
    assert events[1]["text"] == "The iron door opens."
    assert events[2]["usage"] == {
        "input_tokens": 17,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "thinking_tokens": 0,
        "image_count": 0,
        "tts_characters": 0,
        "sound_effect_seconds": 0.0,
        "music_seconds": 0.0,
    }
    assert events[-1]["state"]["meta"]["slug"] == "stream-test"


async def test_retry_stream_rewinds_history_before_replaying_turn(web_client):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Retry Test")
    handle = await app.state.sessions.get(campaign.load_meta().slug)
    handle.session.provider = FakeProvider(
        script=[
            [
                PTextDelta(text="This response will be discarded."),
                PTurnDone(stop_reason="end_turn", usage=Usage()),
            ]
        ]
    )
    seeded = await client.post(
        "/api/campaigns/retry-test/turn",
        json={"text": "I test the old approach."},
    )
    assert seeded.status_code == 200
    _ndjson(seeded)

    handle.session.provider = FakeProvider(
        script=[
            [
                PTextDelta(text="The replay takes a better path."),
                PTurnDone(stop_reason="end_turn", usage=Usage()),
            ]
        ]
    )
    retried = await client.post(
        "/api/campaigns/retry-test/actions/retry",
        json={},
    )

    assert retried.status_code == 200
    streamed = _ndjson(retried)
    assert streamed[0] == {
        "type": "history_snapshot",
        "history": [
            {
                "type": "user_message",
                "role": "user",
                "text": "I test the old approach.",
                "retry": True,
            }
        ],
    }
    assert [event["type"] for event in streamed[1:]] == [
        "turn_started",
        "assistant_text_delta",
        "turn_completed",
        "state_snapshot",
    ]
    assert streamed[2]["text"] == "The replay takes a better path."
    assert "This response will be discarded." not in retried.text

    history = (await client.get("/api/campaigns/retry-test")).json()["history"]
    assert [entry["text"] for entry in history] == [
        "I test the old approach.",
        "The replay takes a better path.",
    ]


async def test_gm_turn_redacts_private_and_tool_payloads_before_streaming(web_client, monkeypatch):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Hidden Test", mode="gm")
    handle = await app.state.sessions.get(campaign.load_meta().slug)
    secret = "THE-LICH-WAITS-BELOW"
    events: list[EngineEvent] = [
        TurnStarted(turn_id="turn-private"),
        DebugChatter(text=secret, reason="model planning"),
        ToolFinished(
            call_id="tool-private",
            name="search_campaign",
            args_summary=secret,
            result_summary=secret,
            args={"query": secret},
            result=secret,
        ),
        RollResult(
            expression="1d20",
            total=20,
            detail=f"1d20 = 20 ({secret})",
            reason=secret,
            private=True,
        ),
        StateChanged(kind="scene", ref=secret, summary=secret, private=True),
        TurnCompleted(turn_id="turn-private"),
    ]
    monkeypatch.setattr(handle.session, "handle_input", _scripted_events(events))

    response = await client.post(
        "/api/campaigns/hidden-test/turn",
        json={"text": "I search the crypt."},
    )

    assert response.status_code == 200
    streamed = _ndjson(response)
    assert secret not in response.text
    assert "debug_chatter" not in {event["type"] for event in streamed}
    tool = next(event for event in streamed if event["type"] == "tool_finished")
    assert tool["args"] == {}
    assert tool["result"] == ""
    assert tool["args_summary"] == ""
    assert tool["result_summary"] == ""
    private_roll = next(event for event in streamed if event["type"] == "roll_result")
    assert private_roll == {"type": "roll_result", "private": True}
    assert "state_changed" not in {event["type"] for event in streamed}


async def test_assistant_turn_keeps_debug_and_private_payloads_visible(web_client, monkeypatch):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Assistant Test", mode="assistant")
    handle = await app.state.sessions.get(campaign.load_meta().slug)
    secret = "VISIBLE-TO-THE-GM"
    events: list[EngineEvent] = [
        DebugChatter(text=secret, reason="assistant diagnostics"),
        ToolFinished(
            call_id="tool-visible",
            name="search_campaign",
            args_summary=secret,
            result_summary=secret,
            args={"query": secret},
            result=secret,
            private=True,
        ),
        RollResult(
            expression="1d20",
            total=20,
            detail="1d20 = 20",
            reason=secret,
            private=True,
        ),
    ]
    monkeypatch.setattr(handle.session, "handle_input", _scripted_events(events))

    response = await client.post(
        "/api/campaigns/assistant-test/turn",
        json={"text": "Show me the GM details."},
    )

    assert response.status_code == 200
    streamed = _ndjson(response)
    assert secret in response.text
    debug = next(event for event in streamed if event["type"] == "debug_chatter")
    assert debug["text"] == secret
    tool = next(event for event in streamed if event["type"] == "tool_finished")
    assert tool["args"] == {"query": secret}
    assert tool["result"] == secret
    private_roll = next(event for event in streamed if event["type"] == "roll_result")
    assert private_roll["total"] == 20
    assert private_roll["reason"] == secret


async def test_roll_and_undo_actions(web_client):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Action Test")
    handle = await app.state.sessions.get(campaign.load_meta().slug)

    rolled = await client.post(
        "/api/campaigns/action-test/actions/roll",
        json={"expression": "1d20"},
    )
    assert rolled.status_code == 200
    assert rolled.json()["event"]["type"] == "roll_result"
    roll_total = rolled.json()["event"]["total"]
    assert 1 <= roll_total <= 20

    handle.session.provider = FakeProvider(
        script=[
            [
                PTextDelta(text="A temporary story beat."),
                PTurnDone(stop_reason="end_turn", usage=Usage()),
            ]
        ]
    )
    turn = await client.post(
        "/api/campaigns/action-test/turn",
        json={"text": "Advance the story."},
    )
    assert turn.status_code == 200
    _ndjson(turn)

    undone = await client.post(
        "/api/campaigns/action-test/actions/undo",
        json={"count": 1},
    )
    assert undone.status_code == 200
    history = undone.json()["history"]
    assert [entry["type"] for entry in history] == ["roll"]
    assert history[0]["total"] == roll_total


async def test_settings_update_is_applied_and_persisted(web_client):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Settings Test")

    response = await client.patch(
        "/api/campaigns/settings-test/settings",
        json={
            "mode": "assistant",
            "effort": "low",
            "thinking": False,
            "verbosity": "high",
            "context_budget": 48_000,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["campaign"]["mode"] == "assistant"
    assert payload["settings"]["effort"] == "low"
    assert payload["settings"]["thinking"] is False
    assert payload["settings"]["verbosity"] == "high"
    assert payload["settings"]["context_budget"] == 48_000
    saved = campaign.load_meta()
    assert saved.mode == "assistant"
    assert saved.settings == {
        "effort": "low",
        "thinking": False,
        "verbosity": "high",
        "context_budget": 48_000,
    }

    invalid = await client.patch(
        "/api/campaigns/settings-test/settings",
        json={"mode": "gm", "effort": "not-an-effort"},
    )
    assert invalid.status_code == 400
    unchanged = campaign.load_meta()
    assert unchanged.mode == saved.mode
    assert unchanged.settings == saved.settings


async def test_provider_failure_does_not_commit_settings(web_client, monkeypatch):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Provider Failure")
    handle = await app.state.sessions.get(campaign.load_meta().slug)
    saved = campaign.load_meta()
    settings_before = handle.session.settings
    provider_before = handle.session.provider

    def fail_provider(_settings):
        raise ValueError("simulated provider construction failure")

    monkeypatch.setattr(handle.session, "provider_for_settings", fail_provider)
    response = await client.patch(
        "/api/campaigns/provider-failure/settings",
        json={"mode": "assistant", "model": "gemini-3.5-flash"},
    )

    assert response.status_code == 400
    assert "simulated provider construction failure" in response.json()["error"]
    assert campaign.load_meta() == saved
    assert handle.session.meta == saved
    assert handle.session.settings == settings_before
    assert handle.session.provider is provider_before


async def test_campaign_model_switch_updates_provider_and_persists(web_client, monkeypatch):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Model Switch")
    handle = await app.state.sessions.get(campaign.load_meta().slug)
    replacement_provider = object()
    proposed_models = []

    def provider_for_settings(settings):
        proposed_models.append(settings.model)
        return replacement_provider

    monkeypatch.setattr(handle.session, "provider_for_settings", provider_for_settings)
    response = await client.patch(
        "/api/campaigns/model-switch/settings",
        json={"model": "gemini-3.5-flash"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert proposed_models == ["gemini-3.5-flash"]
    assert payload["settings"]["model"] == "gemini-3.5-flash"
    assert payload["provider"] == {
        "name": "gemini",
        "model": "gemini-3.5-flash",
        "connected": True,
    }
    assert campaign.load_meta().settings["model"] == "gemini-3.5-flash"
    assert handle.session.settings.model == "gemini-3.5-flash"
    assert handle.session.provider is replacement_provider


async def test_media_settings_persist_and_reload_conditional_tools(web_client, monkeypatch):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Media Settings")
    handle = await app.state.sessions.get(campaign.load_meta().slug)
    reloads = 0
    original_reload = handle.session.reload_tools

    def reload_tools():
        nonlocal reloads
        reloads += 1
        original_reload()

    monkeypatch.setattr(handle.session, "reload_tools", reload_tools)
    response = await client.patch(
        "/api/campaigns/media-settings/settings",
        json={
            "tts_enabled": True,
            "sound_effects_enabled": True,
            "music_enabled": True,
            "images_enabled": True,
            "music_auto": False,
            "images_auto": False,
            "music_volume": 0.65,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert reloads == 1
    assert payload["media"]["enabled"] == {
        "narration": True,
        "sound_effects": True,
        "music": True,
        "images": True,
    }
    assert payload["media"]["automatic"] == {"music": False, "images": False}
    assert payload["media"]["music_volume"] == pytest.approx(0.65)
    saved = campaign.load_meta()
    assert saved.tts_enabled is True
    assert saved.sound_effects_enabled is True
    assert saved.music_enabled is True
    assert saved.images_enabled is True
    assert saved.settings["music_auto"] is False
    assert saved.settings["images_auto"] is False
    assert saved.settings["music_volume"] == pytest.approx(0.65)

    invalid = await client.patch(
        "/api/campaigns/media-settings/settings",
        json={"music_enabled": False, "music_volume": 1.5},
    )
    assert invalid.status_code == 400
    assert campaign.load_meta() == saved
    assert reloads == 1


async def test_campaign_payload_restores_persisted_images_and_music(web_client):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Restored Media")
    meta = campaign.load_meta()
    meta.mode = "assistant"
    meta.music_enabled = True
    campaign.save_meta(meta)

    campaign.images_dir.mkdir(parents=True)
    image = campaign.images_dir / "haunted-foyer.png"
    image.write_bytes(b"image")
    campaign.music_dir.mkdir(parents=True)
    track = campaign.music_dir / "dreadful-strings.mp3"
    track.write_bytes(b"music")
    log = EventLog(campaign.log_path)
    log.append(
        "media",
        {"kind": "image", "path": str(image), "caption": "The haunted foyer"},
    )
    log.append(
        "media",
        {
            "kind": "music",
            "path": str(track),
            "prompt": "dreadful strings",
            "length_seconds": 30,
        },
    )

    response = await client.get("/api/campaigns/restored-media")

    assert response.status_code == 200
    media = response.json()["media"]
    assert media["restored_images"] == [
        {
            "path": "/api/campaigns/restored-media/media/images/haunted-foyer.png",
            "caption": "The haunted foyer",
        }
    ]
    assert media["now_playing"] == {
        "track": "/api/campaigns/restored-media/media/music/dreadful-strings.mp3",
        "mood": "dreadful strings",
        "length_seconds": 30,
    }


def test_save_local_api_key_updates_only_the_local_env_file(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    save_local_api_key("ELEVENLABS_API_KEY", "test-audio-key", env_path=env_path)

    assert os.environ["ELEVENLABS_API_KEY"] == "test-audio-key"
    assert "ELEVENLABS_API_KEY" in env_path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="line breaks"):
        save_local_api_key("ELEVENLABS_API_KEY", "bad\nkey", env_path=env_path)
    with pytest.raises(ValueError, match="Unknown"):
        save_local_api_key("UNRELATED_SECRET", "not-allowed", env_path=env_path)


async def test_web_credential_setup_connects_local_services_without_echoing_keys(
    web_client, monkeypatch
):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Credential Setup")
    handle = await app.state.sessions.get(campaign.load_meta().slug)
    saved: list[tuple[str, str]] = []
    reloaded = 0

    def save_key(name: str, value: str) -> None:
        saved.append((name, value))
        monkeypatch.setenv(name, value)

    def connect_provider() -> bool:
        handle.session.provider = object()
        return True

    def reload_tools() -> None:
        nonlocal reloaded
        reloaded += 1

    monkeypatch.setattr("openadventure.web.app.save_local_api_key", save_key)
    monkeypatch.setattr(handle.session, "connect_provider", connect_provider)
    monkeypatch.setattr(handle.session, "reload_tools", reload_tools)

    secret = "test-main-model-key"
    response = await client.post(
        "/api/campaigns/credential-setup/credentials",
        json={"service": "anthropic", "api_key": secret},
    )

    assert response.status_code == 200
    assert saved == [("ANTHROPIC_API_KEY", secret)]
    assert reloaded == 1
    assert response.json()["provider"]["connected"] is True
    assert secret not in response.text
    assert "api_key" not in response.json()

    audio_secret = "test-elevenlabs-key"
    audio = await client.post(
        "/api/campaigns/credential-setup/credentials",
        json={"service": "elevenlabs", "api_key": audio_secret},
    )
    assert audio.status_code == 200
    assert saved[-1] == ("ELEVENLABS_API_KEY", audio_secret)
    assert audio_secret not in audio.text

    image_secret = "test-google-image-key"
    image = await client.post(
        "/api/campaigns/credential-setup/credentials",
        json={"service": "google", "api_key": image_secret},
    )
    assert image.status_code == 200
    assert saved[-1] == ("GOOGLE_API_KEY", image_secret)
    assert image_secret not in image.text

    invalid = await client.post(
        "/api/campaigns/credential-setup/credentials",
        json={"service": "unknown", "api_key": "not-saved"},
    )
    assert invalid.status_code == 400
    assert saved == [
        ("ANTHROPIC_API_KEY", secret),
        ("ELEVENLABS_API_KEY", audio_secret),
        ("GOOGLE_API_KEY", image_secret),
    ]


async def test_concurrent_turn_is_rejected_when_campaign_lock_is_held(web_client):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Busy Test")
    handle = await app.state.sessions.get(campaign.load_meta().slug)

    async with handle.lock:
        response = await client.post(
            "/api/campaigns/busy-test/turn",
            json={"text": "This must wait."},
        )

    assert response.status_code == 409
    assert "already has a turn in progress" in response.json()["error"]


async def test_cancelling_turn_completes_stream_and_clears_handle(web_client, monkeypatch):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Cancel Test")
    handle = await app.state.sessions.get(campaign.load_meta().slug)
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def hanging_input(_text: str, **_kwargs: object) -> AsyncIterator[EngineEvent]:
        entered.set()
        await blocker.wait()
        yield TurnStarted(turn_id="never-reached")

    monkeypatch.setattr(handle.session, "handle_input", hanging_input)
    turn_request = asyncio.create_task(
        client.post(
            "/api/campaigns/cancel-test/turn",
            json={"text": "Begin a long turn."},
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        cancelled = await client.post(
            "/api/campaigns/cancel-test/actions/cancel",
            json={},
        )
        assert cancelled.status_code == 200
        assert cancelled.json() == {"cancelled": True}
        response = await asyncio.wait_for(turn_request, timeout=5)
    finally:
        if not turn_request.done():
            turn_request.cancel()
            await asyncio.gather(turn_request, return_exceptions=True)

    assert response.status_code == 200
    streamed = _ndjson(response)
    assert [event["type"] for event in streamed] == ["action_message", "state_snapshot"]
    assert streamed[0]["message"] == "Turn cancelled."
    assert handle.current_task is None
    assert not handle.lock.locked()


async def test_media_route_serves_campaign_file_but_rejects_traversal(web_client):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Media Test")
    campaign.images_dir.mkdir(parents=True)
    image = campaign.images_dir / "map.png"
    image.write_bytes(b"not-a-real-png")
    outside = campaign.root / "secret.txt"
    outside.write_text("must remain private", encoding="utf-8")

    served = await client.get("/api/campaigns/media-test/media/images/map.png")
    assert served.status_code == 200
    assert served.content == b"not-a-real-png"

    escaped = await client.get("/api/campaigns/media-test/media/images/%2e%2e%2fsecret.txt")
    assert escaped.status_code == 404
    assert escaped.json() == {"error": "Media file not found."}
    assert "must remain private" not in escaped.text

    evil = app.state.workspace.create_campaign("evil")
    evil.images_dir.mkdir(parents=True)
    sibling_secret = evil.images_dir / "secret.txt"
    sibling_secret.write_bytes(b"sibling-campaign-secret")

    encoded_backslash = await client.get("/api/campaigns/..%5Cevil/media/images/secret.txt")
    assert encoded_backslash.status_code == 404
    assert encoded_backslash.content != b"sibling-campaign-secret"
    assert b"sibling-campaign-secret" not in encoded_backslash.content


def test_web_cli_parser_defaults_and_overrides():
    defaults = build_parser().parse_args(["web"])
    assert defaults.command == "web"
    assert defaults.workspace is None
    assert defaults.port == 8000
    assert defaults.no_open is False
    assert defaults.func is _cmd_web

    custom = build_parser().parse_args(
        ["--workspace", "other-workspace", "web", "--port", "9123", "--no-open"]
    )
    assert custom.workspace == "other-workspace"
    assert custom.port == 9123
    assert custom.no_open is True
