"""Browser library jobs and controlled media delivery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from openadventure.store import snapshots
from openadventure.web import library as library_module
from openadventure.web.app import create_app
from openadventure.web.sessions import WebMediaHost


@pytest.fixture
async def web_client(config):
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield app, client
    await app.state.library_jobs.close()
    await app.state.sessions.close_all()


def _ingested_book(workspace, slug: str, *, kind: str = "source", template=None) -> Path:
    destination = workspace.book_dir(slug)
    sections = destination / "sections"
    sections.mkdir(parents=True)
    (sections / "introduction.md").write_text("# Introduction\n", encoding="utf-8")
    (destination / "index.sqlite").write_bytes(b"")
    snapshots.save_json(
        destination / "manifest.json",
        {
            "source": f"{slug}.md",
            "title": slug.replace("-", " ").title(),
            "type": kind,
            "section_count": 1,
            "ingested_at": "2026-07-19T00:00:00+00:00",
        },
    )
    if template is not None:
        snapshots.save_json(destination / "templates" / "character.json", template)
    return destination


async def _upload(
    client: httpx.AsyncClient,
    filename: str,
    content: bytes,
    *,
    name: str = "",
    book_type: str = "source",
    pages: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    params = {"name": name, "book_type": book_type}
    if pages is not None:
        params["pages"] = pages
    upload_headers = {
        "content-type": "application/octet-stream",
        "x-openadventure-filename": quote(filename, safe=""),
        **(headers or {}),
    }
    return await client.post(
        "/api/library/ingest",
        params=params,
        content=content,
        headers=upload_headers,
    )


async def _wait_for_job(client: httpx.AsyncClient, job_id: str) -> dict:
    for _ in range(200):
        response = await client.get(f"/api/library/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"library job {job_id} did not finish")


async def test_bootstrap_exposes_model_catalog_and_template_summary(web_client):
    app, client = web_client
    _ingested_book(
        app.state.workspace,
        "game-rules",
        template={
            "name": "game-rules/character",
            "version": 2,
            "fields": [{"name": "class"}, {"name": "ancestry"}],
            "resources": [{"name": "health"}],
        },
    )

    response = await client.get("/api/bootstrap")

    assert response.status_code == 200
    payload = response.json()
    assert payload["books"][0]["template"] == {
        "ready": True,
        "fields": 2,
        "resources": 1,
    }
    assert payload["models"]
    model_ids = {model["id"] for model in payload["models"]}
    assert "gemini-3.6-flash" in model_ids
    assert "gemini-3.5-flash" not in model_ids
    assert "gemini-3.1-pro-preview" not in model_ids
    assert all(
        {
            "id",
            "display_name",
            "provider",
            "context_window",
            "max_output",
            "supports_effort",
            "supports_thinking",
        }
        <= set(model)
        for model in payload["models"]
    )
    assert "claude-sonnet-4-6" not in {model["id"] for model in payload["models"]}
    assert payload["utility_model"] in {model["id"] for model in payload["models"]}


async def test_raw_upload_guard_and_validation_leave_no_jobs(web_client):
    _app, client = web_client

    wrong_type = await client.post(
        "/api/library/ingest",
        content=b"rules",
        headers={"content-type": "text/plain", "x-openadventure-filename": "rules.md"},
    )
    assert wrong_type.status_code == 415

    missing_header = await client.post(
        "/api/library/ingest",
        content=b"rules",
        headers={"content-type": "application/octet-stream"},
    )
    assert missing_header.status_code == 403

    cross_origin = await _upload(
        client,
        "rules.md",
        b"rules",
        headers={"origin": "http://evil.example"},
    )
    assert cross_origin.status_code == 403

    unsupported = await _upload(client, "rules.svg", b"<svg/>")
    assert unsupported.status_code == 400
    assert "supported document" in unsupported.json()["error"]

    empty = await _upload(client, "rules.md", b"")
    assert empty.status_code == 400
    assert "empty" in empty.json()["error"]

    non_pdf_pages = await _upload(client, "rules.md", b"rules", pages="2-4")
    assert non_pdf_pages.status_code == 400
    assert "only available for PDF" in non_pdf_pages.json()["error"]

    bad_type = await _upload(client, "rules.md", b"rules", book_type="other")
    assert bad_type.status_code == 400
    assert "source or module" in bad_type.json()["error"]

    overview = await client.get("/api/library")
    assert overview.status_code == 200
    assert overview.json()["jobs"] == []


async def test_ingest_job_retains_progress_and_original_filename(web_client, monkeypatch):
    app, client = web_client
    observed = {}

    monkeypatch.setattr(
        library_module.embeddings,
        "try_load_backend",
        lambda _config: (None, "Semantic search disabled for this test."),
    )

    def fake_ingest(source, destination, **kwargs):
        observed["source_name"] = source.name
        observed["source_body"] = source.read_bytes()
        observed["book_type"] = kwargs["book_type"]
        kwargs["progress"]("Extracting pages", 1, 2)
        kwargs["progress"]("Extracting pages", 2, 2)
        sections = destination / "sections"
        sections.mkdir(parents=True)
        (sections / "rules.md").write_text("# Rules\n", encoding="utf-8")
        (destination / "index.sqlite").write_bytes(b"")
        manifest = {
            "source": source.name,
            "type": kwargs["book_type"],
            "section_count": 1,
            "ingested_at": "2026-07-19T00:00:00+00:00",
        }
        snapshots.save_json(destination / "manifest.json", manifest)
        return manifest

    monkeypatch.setattr(library_module.pipeline, "ingest", fake_ingest)
    accepted = await _upload(
        client,
        "Player's Handbook.md",
        b"# Character rules",
        name="Web Rules",
    )

    assert accepted.status_code == 202
    job_id = accepted.json()["id"]
    completed = await _wait_for_job(client, job_id)
    assert completed["status"] == "succeeded"
    assert completed["book_slug"] == "web-rules"
    assert observed == {
        "source_name": "Player's Handbook.md",
        "source_body": b"# Character rules",
        "book_type": "source",
    }
    assert any(
        event["phase"] == "Extracting pages" and event["completed"] == 2 and event["total"] == 2
        for event in completed["events"]
    )
    assert [event["seq"] for event in completed["events"]] == sorted(
        event["seq"] for event in completed["events"]
    )

    retained = (await client.get(f"/api/library/jobs/{job_id}")).json()
    assert retained["events"] == completed["events"]
    overview = (await client.get("/api/library")).json()
    assert overview["jobs"][0]["id"] == job_id
    assert overview["books"][0]["source"] == "Player's Handbook.md"
    assert overview["books"][0]["template"] == {
        "ready": False,
        "fields": 0,
        "resources": 0,
    }
    assert not (app.state.config.workspace_dir / ".web-jobs" / job_id).exists()


async def test_ingest_duplicate_conflict_does_not_start_job(web_client):
    app, client = web_client
    _ingested_book(app.state.workspace, "existing-rules")

    response = await _upload(
        client,
        "different-file.md",
        b"# Replacement",
        name="Existing Rules",
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["error"]
    assert app.state.library_jobs.snapshots() == []
    assert (
        snapshots.load_json(app.state.workspace.book_dir("existing-rules") / "manifest.json")[
            "source"
        ]
        == "existing-rules.md"
    )


async def test_failed_ingest_cleans_staging_and_releases_book(web_client, monkeypatch):
    app, client = web_client
    monkeypatch.setattr(
        library_module.embeddings,
        "try_load_backend",
        lambda _config: (None, None),
    )

    def fail_ingest(source, _destination, **_kwargs):
        raise RuntimeError(f"parse exploded while reading {source}")

    monkeypatch.setattr(library_module.pipeline, "ingest", fail_ingest)
    accepted = await _upload(client, "Broken.md", b"not parseable", name="Broken Rules")
    assert accepted.status_code == 202
    failed = await _wait_for_job(client, accepted.json()["id"])

    assert failed["status"] == "failed"
    assert "parse exploded" in failed["error"]
    assert str(app.state.config.workspace_dir) not in failed["error"]
    assert not app.state.workspace.book_dir("broken-rules").exists()
    assert not (app.state.config.workspace_dir / ".web-jobs" / accepted.json()["id"]).exists()

    def recover_ingest(source, destination, **kwargs):
        (destination / "sections").mkdir(parents=True)
        (destination / "sections" / "rules.md").write_text("# Recovered\n", encoding="utf-8")
        (destination / "index.sqlite").write_bytes(b"")
        manifest = {
            "source": source.name,
            "type": kwargs["book_type"],
            "section_count": 1,
        }
        snapshots.save_json(destination / "manifest.json", manifest)
        return manifest

    monkeypatch.setattr(library_module.pipeline, "ingest", recover_ingest)
    retried = await _upload(client, "Broken.md", b"# Fixed", name="Broken Rules")
    assert retried.status_code == 202
    assert (await _wait_for_job(client, retried.json()["id"]))["status"] == "succeeded"


async def test_template_job_progress_success_and_overwrite(web_client, monkeypatch):
    app, client = web_client
    source_dir = _ingested_book(app.state.workspace, "template-rules")
    monkeypatch.setattr(library_module, "resolve_api_key", lambda _config, _provider: "test-key")
    monkeypatch.setattr(library_module, "build_provider", lambda *_args: object())

    async def derive_template(_provider, settings, directory, source_name, on_progress=None):
        assert directory == source_dir
        assert source_name == "template-rules"
        assert settings.model == "gpt-5.6-luna"
        on_progress("Round 3/16: Searching for character creation rules")
        on_progress("Round 3/16: It")
        on_progress("Round 3/16: It looks")
        on_progress("Round 3/16: It looks like the creation chapter is indexed")
        template = {
            "name": "template-rules/character",
            "version": 2,
            "fields": [{"name": "class"}, {"name": "ancestry"}],
            "resources": [{"name": "health"}],
        }
        snapshots.save_json(directory / "templates" / "character.json", template)
        return template

    monkeypatch.setattr(library_module.template_gen, "derive_template", derive_template)
    accepted = await client.post(
        "/api/library/books/template-rules/template",
        json={},
    )
    assert accepted.status_code == 202
    completed = await _wait_for_job(client, accepted.json()["id"])

    assert completed["status"] == "succeeded"
    assert completed["result"] == {
        "fields": 2,
        "resources": 1,
        "model": "gpt-5.6-luna",
    }
    round_events = [event for event in completed["events"] if event["round"] == 3]
    assert len(round_events) == 1
    assert round_events[0]["max_rounds"] == 16
    assert round_events[0]["message"] == "It looks like the creation chapter is indexed"
    summary = (await client.get("/api/bootstrap")).json()["books"][0]["template"]
    assert summary == {"ready": True, "fields": 2, "resources": 1}

    refused = await client.post(
        "/api/library/books/template-rules/template",
        json={},
    )
    assert refused.status_code == 409
    assert "Confirm regeneration" in refused.json()["error"]

    async def replace_template(_provider, _settings, directory, _source_name, on_progress=None):
        on_progress("Round 1/16: revising the sheet")
        template = {
            "name": "template-rules/character",
            "version": 2,
            "fields": [{"name": "calling"}],
            "resources": [{"name": "health"}, {"name": "luck"}],
        }
        snapshots.save_json(directory / "templates" / "character.json", template)
        return template

    monkeypatch.setattr(library_module.template_gen, "derive_template", replace_template)
    overwrite = await client.post(
        "/api/library/books/template-rules/template",
        json={"overwrite": True},
    )
    assert overwrite.status_code == 202
    overwritten = await _wait_for_job(client, overwrite.json()["id"])
    assert overwritten["status"] == "succeeded"
    assert overwritten["result"]["fields"] == 1
    assert overwritten["result"]["resources"] == 2


async def test_template_job_requires_provider_key(web_client, monkeypatch):
    app, client = web_client
    _ingested_book(app.state.workspace, "keyless-rules")
    monkeypatch.setattr(library_module, "resolve_api_key", lambda _config, _provider: None)

    response = await client.post(
        "/api/library/books/keyless-rules/template",
        json={},
    )

    assert response.status_code == 400
    assert "API key" in response.json()["error"]
    assert app.state.library_jobs.snapshots() == []


async def test_campaign_library_patch_is_atomic_and_reloads_tools(web_client, monkeypatch):
    app, client = web_client
    _ingested_book(app.state.workspace, "core-rules", kind="source")
    _ingested_book(app.state.workspace, "lost-mine", kind="module")
    campaign = app.state.workspace.create_campaign("Library Attach")
    handle = await app.state.sessions.get(campaign.load_meta().slug)
    reloads = 0
    original_reload = handle.session.reload_tools

    def reload_tools():
        nonlocal reloads
        reloads += 1
        original_reload()

    monkeypatch.setattr(handle.session, "reload_tools", reload_tools)
    attached = await client.patch(
        "/api/campaigns/library-attach/library",
        json={
            "sources": ["Core Rules"],
            "system_source": "core-rules",
            "modules": ["Lost Mine"],
            "active_module": "lost-mine",
        },
    )

    assert attached.status_code == 200
    assert reloads == 1
    payload = attached.json()["campaign"]
    assert payload["sources"] == ["core-rules"]
    assert payload["system_source"] == "core-rules"
    assert [module["slug"] for module in payload["modules"]] == ["lost-mine"]
    assert payload["active_module"] == "lost-mine"
    saved = campaign.load_meta()
    assert saved == handle.session.meta

    invalid = await client.patch(
        "/api/campaigns/library-attach/library",
        json={"sources": ["lost-mine"], "modules": ["core-rules"]},
    )
    assert invalid.status_code == 400
    assert campaign.load_meta() == saved
    assert handle.session.meta == saved
    assert reloads == 1


async def test_web_media_host_preserves_fifo_events(campaign):
    campaign.images_dir.mkdir(parents=True)
    campaign.music_dir.mkdir(parents=True)
    image = campaign.images_dir / "scene.png"
    music = campaign.music_dir / "ambience.mp3"
    speech = campaign.root / "speech.wav"
    effect = campaign.root / "effect.ogg"
    image.write_bytes(b"png")
    music.write_bytes(b"mp3")
    speech.write_bytes(b"wav")
    effect.write_bytes(b"ogg")
    host = WebMediaHost(campaign, music_volume=0.35)

    await host.play_speech(speech)
    await host.play_sound_effect(effect)
    host.play_music(music, prompt="quiet ruins", length_seconds=42)
    status = host.music_status_line()
    host.present_image(image, caption="The old gate")
    host.present_handout("A torn note", "Meet me at midnight.")
    host.set_music_volume(0.6)
    host.stop_music()
    events = host.drain_events()

    assert [event["type"] for event in events] == [
        "audio_ready",
        "audio_ready",
        "music_started",
        "show_image",
        "handout",
        "media_volume",
        "music_stopped",
    ]
    assert [events[0]["kind"], events[1]["kind"]] == ["speech", "sound_effect"]
    assert events[2]["track"].endswith("/media/music/ambience.mp3")
    assert events[3]["path"].endswith("/media/images/scene.png")
    assert status == "looping 'quiet ruins' (42s track) at volume 35%"
    assert host.music_status_line() is None
    assert host.drain_events() == []


async def test_controlled_media_routes_serve_safe_types_with_security_headers(web_client):
    app, client = web_client
    campaign = app.state.workspace.create_campaign("Browser Media")
    campaign.images_dir.mkdir(parents=True)
    campaign.music_dir.mkdir(parents=True)
    image = campaign.images_dir / "scene.png"
    music = campaign.music_dir / "ambience.mp3"
    speech = campaign.root / "speech.wav"
    image.write_bytes(b"image-bytes")
    music.write_bytes(b"music-bytes")
    speech.write_bytes(b"speech-bytes")
    handle = await app.state.sessions.get("browser-media")
    host = handle.session.media_host
    assert isinstance(host, WebMediaHost)

    host.present_image(image, caption="A moonlit bridge")
    host.play_music(music, prompt="night travel", length_seconds=30)
    await host.play_speech(speech)
    event_response = await client.get("/api/campaigns/browser-media/events")
    assert event_response.status_code == 200
    events = event_response.json()["events"]
    assert [event["type"] for event in events] == [
        "show_image",
        "music_started",
        "audio_ready",
    ]
    urls = [events[0]["path"], events[1]["track"], events[2]["path"]]

    for url, expected, media_prefix in zip(
        urls,
        (b"image-bytes", b"music-bytes", b"speech-bytes"),
        ("image/", "audio/", "audio/"),
        strict=True,
    ):
        served = await client.get(url)
        assert served.status_code == 200
        assert served.content == expected
        assert served.headers["content-type"].startswith(media_prefix)
        assert served.headers["cache-control"] == "private, max-age=3600"
        assert served.headers["cross-origin-resource-policy"] == "same-origin"
        assert served.headers["x-content-type-options"] == "nosniff"

    active_image = campaign.images_dir / "active.svg"
    active_music = campaign.music_dir / "active.html"
    active_audio = campaign.root / "active.html"
    active_image.write_text("<svg onload='alert(1)'/>", encoding="utf-8")
    active_music.write_text("<script>alert(1)</script>", encoding="utf-8")
    active_audio.write_text("<script>alert(1)</script>", encoding="utf-8")

    rejected_image = await client.get("/api/campaigns/browser-media/media/images/active.svg")
    rejected_music = await client.get("/api/campaigns/browser-media/media/music/active.html")
    assert rejected_image.status_code == 415
    assert rejected_music.status_code == 415

    await host.play_speech(active_audio)
    active_event = (await client.get("/api/campaigns/browser-media/events")).json()["events"][0]
    assert active_event["type"] == "audio_ready"
    rejected_audio = await client.get(active_event["path"])
    assert rejected_audio.status_code == 415
