"""Image feature: Gemini backend, generate/show/find tools, session + render wiring."""

import asyncio
import base64
import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from openadventure.cli.render import EventRenderer
from openadventure.engine.events import ImageGenerated, ShowImage
from openadventure.engine.tools import build_registry
from openadventure.engine.tools.ambience_tools import make_ambience_tools
from openadventure.engine.tools.registry import ToolRegistry
from openadventure.media.image import GeminiImageBackend
from openadventure.media.tasks import BackgroundTasks
from tests.conftest import collect  # noqa: F401  (kept parallel with the audio suites)
from tests.test_agent_loop import text_turn
from tests.test_sheet_tools import make_ctx


class FakeImageBackend:
    ready = True
    configuration_hint = ""

    def __init__(self, delay: float = 0.0, out: Path | None = None):
        self.delay = delay
        self.out = out
        self.calls: list[dict] = []

    async def generate(self, subject, description, *, reference_images=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append(
            {
                "subject": subject,
                "description": description,
                "reference_images": list(reference_images or []),
            }
        )
        return self.out or Path(f"images/{subject}.png")


def _img_registry(images) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in make_ambience_tools(images, None):
        registry.register(tool)
    return registry


# --- generate_image tool -----------------------------------------------------


async def test_generate_image_persists_into_campaign(workspace, campaign, tmp_path):
    src = tmp_path / "render.png"
    src.write_bytes(b"\x89PNG-fake")
    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    registry = _img_registry(FakeImageBackend(out=src))

    outcome = registry.dispatch(
        ctx,
        "generate_image",
        {"subject": "Marta the innkeeper", "description": "stout, kind eyes, warm lamplight"},
    )
    assert outcome.ok
    assert "background" in outcome.content
    assert outcome.events[0].type == "background_task_started"

    await ctx.background.wait_all()
    events = ctx.background.drain()
    image = next(e for e in events if isinstance(e, ImageGenerated))
    assert image.caption == "Marta the innkeeper"

    saved = Path(image.path)
    assert saved.parent == campaign.images_dir  # copied into the campaign, not left in temp
    assert saved.read_bytes() == b"\x89PNG-fake"

    media = next(e for e in ctx.log.read_all() if e.type == "media")
    assert media.data["kind"] == "image"
    assert media.data["subject"] == "Marta the innkeeper"
    assert media.data["prompt"].startswith("stout")


async def test_generate_image_resolves_campaign_relative_reference(workspace, campaign, tmp_path):
    campaign.images_dir.mkdir(parents=True, exist_ok=True)
    ref = campaign.images_dir / "marta-abc.png"
    ref.write_bytes(b"ref-bytes")
    src = tmp_path / "out.png"
    src.write_bytes(b"new-bytes")
    fake = FakeImageBackend(out=src)
    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    registry = _img_registry(fake)

    outcome = registry.dispatch(
        ctx,
        "generate_image",
        {
            "subject": "Marta again",
            "description": "the same face, now smiling",
            "reference_images": ["marta-abc.png"],
        },
    )
    assert outcome.ok
    await ctx.background.wait_all()
    assert fake.calls[0]["reference_images"] == [ref]


async def test_generate_image_notes_missing_reference(workspace, campaign):
    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    registry = _img_registry(FakeImageBackend())

    outcome = registry.dispatch(
        ctx,
        "generate_image",
        {"subject": "x", "description": "y", "reference_images": ["nope.png"]},
    )
    assert outcome.ok  # still renders, just without the missing reference
    assert "couldn't find reference" in outcome.content
    await ctx.background.wait_all()


def test_generate_image_reports_missing_key(workspace, campaign):
    class MissingKey:
        ready = False
        configuration_hint = "Set GOOGLE_API_KEY."

        async def generate(self, *a, **k):
            raise AssertionError("should not be called")

    ctx = make_ctx(workspace, campaign)
    ctx.background = BackgroundTasks()
    registry = _img_registry(MissingKey())

    outcome = registry.dispatch(ctx, "generate_image", {"subject": "x", "description": "y"})
    assert not outcome.ok
    assert "GOOGLE_API_KEY" in outcome.content
    assert ctx.background.pending == 0


# --- show_image / find_images ------------------------------------------------


def _log_image(ctx, path: Path, caption: str) -> None:
    ctx.log.append(
        "media", {"kind": "image", "path": str(path), "caption": caption, "subject": caption}
    )


def test_show_image_finds_by_caption(workspace, campaign):
    campaign.images_dir.mkdir(parents=True, exist_ok=True)
    img = campaign.images_dir / "marta-1.png"
    img.write_bytes(b"x")
    ctx = make_ctx(workspace, campaign)
    _log_image(ctx, img, "Marta the innkeeper")
    registry = _img_registry(FakeImageBackend())

    outcome = registry.dispatch(ctx, "show_image", {"image": "marta"})
    assert outcome.ok
    shown = outcome.events[0]
    assert isinstance(shown, ShowImage)
    assert Path(shown.path) == img
    assert shown.caption == "Marta the innkeeper"


def test_show_image_missing_is_error(workspace, campaign):
    ctx = make_ctx(workspace, campaign)
    registry = _img_registry(FakeImageBackend())
    outcome = registry.dispatch(ctx, "show_image", {"image": "a dragon nobody drew"})
    assert not outcome.ok
    assert outcome.events == []


def test_find_images_lists_newest_first_and_filters(workspace, campaign):
    campaign.images_dir.mkdir(parents=True, exist_ok=True)
    bridge = campaign.images_dir / "bridge.png"
    bridge.write_bytes(b"a")
    vane = campaign.images_dir / "vane.png"
    vane.write_bytes(b"b")
    ctx = make_ctx(workspace, campaign)
    _log_image(ctx, bridge, "the old bridge")
    _log_image(ctx, vane, "Captain Vane")
    registry = _img_registry(FakeImageBackend())

    everything = registry.dispatch(ctx, "find_images", {})
    assert "Captain Vane" in everything.content
    # newest (Vane) listed before older (bridge)
    assert everything.content.index("Captain Vane") < everything.content.index("the old bridge")

    filtered = registry.dispatch(ctx, "find_images", {"query": "vane"})
    assert "Captain Vane" in filtered.content
    assert "the old bridge" not in filtered.content

    empty = registry.dispatch(ctx, "find_images", {"query": "dragon"})
    assert "No generated images match" in empty.content


# --- registry gating + session wiring ----------------------------------------


def test_image_tools_gated_by_meta_flag(workspace, campaign):
    meta = campaign.load_meta()
    backends = (FakeImageBackend(), None, None, None)
    registry = build_registry(workspace, campaign, meta, media_backends=backends)
    assert "generate_image" not in registry

    meta.images_enabled = True
    registry = build_registry(workspace, campaign, meta, media_backends=backends)
    assert "generate_image" in registry
    assert "show_image" in registry
    assert "find_images" in registry


async def test_image_toggle_controls_tools_and_prompt(make_session):
    session = make_session(script=[text_turn("dark"), text_turn("bright")])
    assert not session.meta.images_enabled
    assert "generate_image" not in session.tools
    assert "Images: disabled" in session.build_system()[0].text

    session.set_images_enabled(True)
    assert session.campaign.load_meta().images_enabled
    assert "generate_image" in session.tools
    assert "show_image" in session.tools
    assert "find_images" in session.tools
    assert "Images: enabled (auto)" in session.build_system()[0].text

    session.set_images_auto(False)
    assert "Images: enabled (manual)" in session.build_system()[0].text
    session.set_images_auto(True)

    session.set_images_enabled(False)
    assert "generate_image" not in session.tools


def test_assistant_mode_images_prompt(make_session):
    session = make_session(script=[])
    session.set_mode("assistant")
    session.set_images_enabled(True)
    text = session.build_system()[0].text
    assert "generate_image and show_image when the GM asks" in text


# --- Gemini backend ----------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _image_response(image: bytes, mime: str = "image/png") -> bytes:
    return json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Here it is."},
                            {
                                "inline_data": {
                                    "mime_type": mime,
                                    "data": base64.b64encode(image).decode("ascii"),
                                }
                            },
                        ]
                    }
                }
            ]
        }
    ).encode("utf-8")


async def test_gemini_request_and_cache(tmp_path, monkeypatch):
    requests = []
    image_bytes = b"\x89PNG-image-bytes"

    def fake_urlopen(request, timeout=None):
        requests.append(request)
        return _FakeResponse(_image_response(image_bytes))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    backend = GeminiImageBackend(api_key="k", cache_dir=tmp_path)

    path = await backend.generate("the inn", "cozy tavern, warm light")

    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-3.1-flash-image:generateContent"
    )
    assert request.get_header("X-goog-api-key") == "k"
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["contents"][0]["parts"][0]["text"] == "the inn. cozy tavern, warm light"
    assert payload["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
    assert path.read_bytes() == image_bytes
    assert path.suffix == ".png"

    # same prompt is served from cache, no new request
    await backend.generate("the inn", "cozy tavern, warm light")
    assert len(requests) == 1
    # a different prompt generates again
    await backend.generate("the inn", "now ablaze")
    assert len(requests) == 2


async def test_gemini_includes_reference_image(tmp_path, monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(_image_response(b"out"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ref = tmp_path / "ref.jpg"
    ref.write_bytes(b"jpeg-bytes")
    backend = GeminiImageBackend(api_key="k", cache_dir=tmp_path)

    await backend.generate("hero", "the same hero, in armor", reference_images=[ref])

    parts = captured["payload"]["contents"][0]["parts"]
    assert parts[0]["text"] == "hero. the same hero, in armor"
    inline = parts[1]["inline_data"]
    assert inline["mime_type"] == "image/jpeg"
    assert base64.b64decode(inline["data"]) == b"jpeg-bytes"


async def test_gemini_aspect_ratio_sent_when_set(tmp_path, monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(_image_response(b"out"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    backend = GeminiImageBackend(api_key="k", cache_dir=tmp_path, aspect_ratio="16:9")
    await backend.generate("vista", "wide mountain valley")
    assert captured["payload"]["generationConfig"]["imageConfig"]["aspectRatio"] == "16:9"


async def test_gemini_requires_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    backend = GeminiImageBackend(api_key=None, cache_dir=tmp_path)
    assert not backend.ready
    assert "GOOGLE_API_KEY" in backend.configuration_hint
    with pytest.raises(RuntimeError):
        await backend.generate("x", "y")


async def test_gemini_raises_when_no_image_returned(tmp_path, monkeypatch):
    def fake_urlopen(request, timeout=None):
        body = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "I can't draw that."}]}}]}
        ).encode("utf-8")
        return _FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    backend = GeminiImageBackend(api_key="k", cache_dir=tmp_path)
    with pytest.raises(RuntimeError) as exc:
        await backend.generate("x", "y")
    assert "can't draw" in str(exc.value)


# --- renderer ---------------------------------------------------------------


def _render_to_text(events) -> str:
    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=False)
    EventRenderer(console).render_events(events)
    return buf.getvalue()


def test_image_generated_renders_caption_and_path():
    out = _render_to_text([ImageGenerated(path="x/marta.png", caption="Marta the innkeeper")])
    assert "Marta the innkeeper" in out
    assert "marta.png" in out


def test_show_image_renders_showing_line():
    out = _render_to_text([ShowImage(path="x/marta.png", caption="Marta")])
    assert "Showing" in out
    assert "marta.png" in out


def test_template_derivation_is_announced_unlike_other_tasks():
    from openadventure.engine.events import BackgroundTaskFinished, BackgroundTaskStarted

    # The template task blocks the turn and has no landing event, so it must be
    # surfaced: both its kickoff and its completion.
    out = _render_to_text(
        [
            BackgroundTaskStarted(
                task_id="template-coc7e",
                kind="template",
                label="Deriving character template for coc7e",
            ),
            BackgroundTaskFinished(task_id="template-coc7e", ok=True),
        ]
    )
    assert "Deriving character template for coc7e" in out
    assert "Character template ready" in out

    # A normal background task (e.g. music) stays quiet on start and on success.
    quiet = _render_to_text(
        [
            BackgroundTaskStarted(task_id="m1", kind="music", label="composing music"),
            BackgroundTaskFinished(task_id="m1", ok=True),
        ]
    )
    assert "composing music" not in quiet


def test_open_image_skipped_when_not_terminal(monkeypatch, tmp_path):
    import openadventure.cli.media_host as host_mod

    opened: list = []
    monkeypatch.setattr(host_mod.os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(host_mod.subprocess, "Popen", lambda *a, **k: opened.append(a) or None)
    img = tmp_path / "i.png"
    img.write_bytes(b"x")
    renderer = EventRenderer(Console(file=io.StringIO(), force_terminal=False))
    renderer._open_image(str(img))
    assert opened == []  # no screen to show it on


def test_open_image_invoked_on_terminal(monkeypatch, tmp_path):
    import openadventure.cli.media_host as host_mod

    opened: list = []
    monkeypatch.setattr(
        host_mod.os, "startfile", lambda p: opened.append(("startfile", p)), raising=False
    )
    monkeypatch.setattr(
        host_mod.subprocess, "Popen", lambda *a, **k: opened.append(("popen", a)) or None
    )
    img = tmp_path / "i.png"
    img.write_bytes(b"x")
    renderer = EventRenderer(Console(file=io.StringIO(), force_terminal=True))
    renderer._open_image(str(img))
    assert opened  # the platform's opener was invoked


def test_open_image_skipped_when_file_missing(monkeypatch, tmp_path):
    import openadventure.cli.media_host as host_mod

    opened: list = []
    monkeypatch.setattr(host_mod.os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(host_mod.subprocess, "Popen", lambda *a, **k: opened.append(a) or None)
    renderer = EventRenderer(Console(file=io.StringIO(), force_terminal=True))
    renderer._open_image(str(tmp_path / "does-not-exist.png"))
    assert opened == []
