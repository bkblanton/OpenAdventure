"""Local ASGI application for the OpenAdventure browser interface."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import webbrowser
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from openadventure.character_import import (
    IMPORT_MAX_BYTES,
    IMPORT_SUFFIXES,
    prepare_character_import,
)
from openadventure.config import AppConfig, load_config, save_local_api_key
from openadventure.engine.events import EngineEvent, RollResult
from openadventure.engine.session import resolve_settings
from openadventure.mechanics.dice import DiceError
from openadventure.store.workspace import (
    BookTypeMismatch,
    Campaign,
    ModuleRef,
    Workspace,
    ensure_book_type,
    slugify,
    titleize,
)
from openadventure.web.library import (
    SUPPORTED_SUFFIXES,
    LibraryJobConflict,
    LibraryJobError,
    LibraryJobManager,
)
from openadventure.web.sessions import SessionHandle, SessionManager, WebMediaHost
from openadventure.web.views import (
    bootstrap_payload,
    campaign_payload,
    public_history,
    sanitize_event,
    state_snapshot,
    usage_payload,
)

STATIC_DIR = Path(__file__).with_name("static")
LOCAL_HOST = "127.0.0.1"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav", ".webm"}


class LocalStaticFiles(StaticFiles):
    """Serve browser assets without cache reuse during local development.

    The web UI ships as separately requested HTML, CSS, and JavaScript files.
    A normal page reload must not combine newly merged markup with stale cached
    scripts or styles, which can leave controls inert or show an obsolete layout.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


CREDENTIAL_SERVICES = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
}
logger = logging.getLogger(__name__)


class LocalMutationGuard:
    """Reject browser-driven cross-site writes to the unauthenticated local API."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            content_type = headers.get(b"content-type", b"").decode("latin-1")
            path = scope.get("path", "")
            is_upload = scope["method"] == "POST" and (
                path == "/api/library/ingest"
                or (path.startswith("/api/campaigns/") and path.endswith("/import"))
            )
            expected_type = "application/octet-stream" if is_upload else "application/json"
            if content_type.partition(";")[0].strip().lower() != expected_type:
                response = JSONResponse(
                    {"error": f"This request requires Content-Type: {expected_type}."},
                    status_code=415,
                )
                await response(scope, receive, send)
                return

            if is_upload and b"x-openadventure-filename" not in headers:
                response = JSONResponse(
                    {"error": "Uploads require the OpenAdventure filename header."},
                    status_code=403,
                )
                await response(scope, receive, send)
                return

            fetch_site = headers.get(b"sec-fetch-site", b"").decode("latin-1").lower()
            if fetch_site == "cross-site":
                response = JSONResponse({"error": "Cross-site requests are not allowed."}, 403)
                await response(scope, receive, send)
                return

            origin = headers.get(b"origin", b"").decode("latin-1")
            if origin:
                parsed = urlsplit(origin)
                host = headers.get(b"host", b"").decode("latin-1")
                if parsed.scheme != "http" or parsed.netloc.casefold() != host.casefold():
                    response = JSONResponse(
                        {"error": "Cross-origin requests are not allowed."}, 403
                    )
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)


def _json_error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _request_json(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Request body must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object.")
    return value


def _manager(request: Request) -> SessionManager:
    return request.app.state.sessions


def _workspace(request: Request) -> Workspace:
    return request.app.state.workspace


def _config(request: Request) -> AppConfig:
    return request.app.state.config


def _library_jobs(request: Request) -> LibraryJobManager:
    return request.app.state.library_jobs


async def _session_handle(request: Request) -> SessionHandle | JSONResponse:
    try:
        return await _manager(request).get(request.path_params["slug"])
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)


def _media_url(campaign: Campaign, slug: str, kind: str, raw_path: str) -> str | None:
    """Turn a campaign media path into a URL without exposing its disk path."""
    roots = {"images": campaign.images_dir, "music": campaign.music_dir}
    root = roots.get(kind)
    if root is None:
        return None
    try:
        target = Path(raw_path).resolve(strict=True)
        relative = target.relative_to(root.resolve())
    except FileNotFoundError, OSError, ValueError:
        return None
    encoded_slug = quote(slug, safe="")
    encoded_path = quote(relative.as_posix(), safe="/")
    return f"/api/campaigns/{encoded_slug}/media/{kind}/{encoded_path}"


def _event_payload(event: EngineEvent, handle: SessionHandle) -> dict[str, Any] | None:
    session = handle.session
    if isinstance(session.media_host, WebMediaHost) and event.type in (
        "music_started",
        "music_stopped",
    ):
        # The host carries the validated playable URL. The engine event contains
        # only the generation prompt, so sending both would duplicate the cue.
        return None
    return sanitize_event(
        event,
        mode=session.meta.mode,
        image_url=lambda path: _media_url(session.campaign, session.meta.slug, "images", path),
    )


def _web_media_payloads(handle: SessionHandle) -> list[dict[str, Any]]:
    host = handle.session.media_host
    if not isinstance(host, WebMediaHost):
        return []
    payloads = host.drain_events()
    if handle.session.meta.mode == "gm":
        for payload in payloads:
            if payload.get("type") == "music_started":
                payload["mood"] = ""
    return payloads


def _restored_web_media(handle: SessionHandle) -> dict[str, Any]:
    """Describe durable visual media and the active track for a fresh browser.

    Live host events are intentionally transient, but generated images and music
    are recorded under the campaign. Rebuild their safe browser-facing form from
    that durable state whenever a campaign payload is requested.
    """

    session = handle.session
    restored_images: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for entry in session.log.read_all():
        if entry.type != "media" or entry.data.get("kind") != "image":
            continue
        raw_path = entry.data.get("path")
        if not isinstance(raw_path, str) or raw_path in seen_paths:
            continue
        url = _media_url(session.campaign, session.meta.slug, "images", raw_path)
        if url is None:
            continue
        seen_paths.add(raw_path)
        caption = entry.data.get("caption") or entry.data.get("subject") or "Generated image"
        restored_images.append({"path": url, "caption": str(caption)})

    current_music: dict[str, Any] | None = None
    host = session.media_host
    if isinstance(host, WebMediaHost):
        current_music = host.now_playing()
        if current_music is not None and session.meta.mode == "gm":
            # Prompts may include GM-only context. The player still receives the
            # playable track, but not the internal selection rationale.
            current_music["mood"] = ""
    return {"restored_images": restored_images[-24:], "now_playing": current_music}


def _campaign_payload(handle: SessionHandle) -> dict[str, Any]:
    """Return a campaign payload enriched with browser-restorable media."""

    payload = campaign_payload(handle.session)
    media = payload.get("media")
    if isinstance(media, dict):
        media.update(_restored_web_media(handle))
    return payload


def _ndjson(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


async def homepage(request: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html")


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def bootstrap(request: Request) -> Response:
    return JSONResponse(bootstrap_payload(_config(request), _workspace(request)))


def _parse_pages(value: str | None) -> tuple[int, int] | None:
    if value is None or not value.strip():
        return None
    start_text, separator, end_text = value.strip().partition("-")
    try:
        start = int(start_text)
        end = int(end_text) if separator else start
    except ValueError as exc:
        raise ValueError("Page range must look like 18-32.") from exc
    if start < 1 or end < start:
        raise ValueError("Page range must start at 1 or later and end after it starts.")
    return start, end


async def library_overview(request: Request) -> Response:
    bootstrap_data = bootstrap_payload(_config(request), _workspace(request))
    return JSONResponse(
        {
            "books": bootstrap_data["books"],
            "models": bootstrap_data["models"],
            "utility_model": bootstrap_data["utility_model"],
            "jobs": _library_jobs(request).snapshots(),
        }
    )


async def start_ingest(request: Request) -> Response:
    temporary: Path | None = None
    handed_off = False
    try:
        query = request.query_params
        book_type = query.get("book_type", "")
        name = query.get("name", "")
        if len(name) > 120:
            raise LibraryJobError("Library names must be 120 characters or fewer.")
        encoded_filename = request.headers.get("x-openadventure-filename") or query.get(
            "filename", ""
        )
        filename = unquote(encoded_filename)
        if not filename or "\x00" in filename:
            raise LibraryJobError("Choose a document to upload.")
        filename = Path(filename).name
        suffix = Path(filename).suffix.casefold()
        if suffix not in SUPPORTED_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
            raise LibraryJobError(f"Choose a supported document: {supported}.")
        pages = _parse_pages(query.get("pages"))

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise LibraryJobError("Upload size could not be read.") from exc
            if declared_size > MAX_UPLOAD_BYTES:
                raise LibraryJobError("Documents must be 512 MB or smaller.")

        descriptor, raw_path = tempfile.mkstemp(prefix="openadventure-upload-", suffix=suffix)
        temporary = Path(raw_path)
        size = 0
        with os.fdopen(descriptor, "wb") as destination:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise LibraryJobError("Documents must be 512 MB or smaller.")
                destination.write(chunk)
        if size == 0:
            raise LibraryJobError("The uploaded document is empty.")

        job = await _library_jobs(request).start_ingest(
            temporary,
            original_name=filename,
            name=name,
            book_type=book_type,
            pages=pages,
        )
        handed_off = True
        return JSONResponse(job.snapshot(), status_code=202)
    except LibraryJobConflict as exc:
        return _json_error(str(exc), 409)
    except (LibraryJobError, ValueError) as exc:
        return _json_error(str(exc))
    finally:
        if temporary is not None and not handed_off:
            temporary.unlink(missing_ok=True)


async def get_library_job(request: Request) -> Response:
    try:
        job = _library_jobs(request).get(request.path_params["job_id"])
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)
    return JSONResponse(job.snapshot())


async def cancel_library_job(request: Request) -> Response:
    try:
        cancelled = await _library_jobs(request).cancel(request.path_params["job_id"])
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)
    return JSONResponse({"cancelled": cancelled})


async def start_template(request: Request) -> Response:
    try:
        body = await _request_json(request)
        model = body.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise LibraryJobError("Model must be non-empty text.")
        overwrite = body.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise LibraryJobError("overwrite must be true or false.")
        job = await _library_jobs(request).start_template(
            request.path_params["book_slug"],
            model=model.strip() if isinstance(model, str) else None,
            overwrite=overwrite,
        )
        return JSONResponse(job.snapshot(), status_code=202)
    except LibraryJobConflict as exc:
        return _json_error(str(exc), 409)
    except LibraryJobError as exc:
        return _json_error(str(exc))


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of names.")
    return [item.strip() for item in value if item.strip()]


async def create_campaign(request: Request) -> Response:
    try:
        body = await _request_json(request)
        name = body.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Campaign name is required.")
        if len(name.strip()) > 120:
            raise ValueError("Campaign name must be 120 characters or fewer.")
        mode = body.get("mode", "gm")
        if mode not in ("gm", "assistant"):
            raise ValueError("Mode must be 'gm' or 'assistant'.")
        premise = body.get("premise")
        if premise is not None and not isinstance(premise, str):
            raise ValueError("Premise must be text.")
        campaign = _workspace(request).create_campaign(
            name.strip(),
            mode=mode,
            sources=_string_list(body.get("sources"), "sources"),
            modules=_string_list(body.get("modules"), "modules"),
            premise=premise.strip() if premise and premise.strip() else None,
        )
        handle = await _manager(request).get(campaign.load_meta().slug)
    except ValueError as exc:
        return _json_error(str(exc))
    except FileExistsError as exc:
        return _json_error(str(exc), 409)
    except BookTypeMismatch as exc:
        return _json_error(str(exc))
    return JSONResponse(_campaign_payload(handle), status_code=201)


async def get_campaign(request: Request) -> Response:
    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    return JSONResponse(_campaign_payload(handle))


async def get_state(request: Request) -> Response:
    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    return JSONResponse({"state": state_snapshot(handle.session)})


async def get_usage(request: Request) -> Response:
    """Return the campaign's current usage and estimated-cost report."""

    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    return JSONResponse({"usage": usage_payload(handle.session)})


def _library_slugs(value: Any, field: str) -> list[str]:
    names = _string_list(value, field)
    result: list[str] = []
    for name in names:
        slug = slugify(name)
        if slug not in result:
            result.append(slug)
    return result


async def update_campaign_library(request: Request) -> Response:
    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    try:
        body = await _request_json(request)
        allowed = {"sources", "system_source", "modules", "active_module"}
        unknown = set(body) - allowed
        if unknown:
            raise ValueError(f"Unknown library setting(s): {', '.join(sorted(unknown))}.")
        sources = _library_slugs(body.get("sources", handle.session.meta.sources), "sources")
        modules = _library_slugs(
            body.get("modules", [module.slug for module in handle.session.meta.modules]),
            "modules",
        )
        available = set(_workspace(request).list_books())
        missing = [slug for slug in [*sources, *modules] if slug not in available]
        if missing:
            raise ValueError(f"Ingest these books first: {', '.join(missing)}.")
        for source in sources:
            ensure_book_type(_workspace(request), source, "source")
        for module in modules:
            ensure_book_type(_workspace(request), module, "module")

        raw_system = body.get("system_source", handle.session.meta.system_source)
        system_source = slugify(raw_system) if isinstance(raw_system, str) and raw_system else None
        if system_source is not None and system_source not in sources:
            raise ValueError("The system source must be one of the attached rule sources.")
        raw_active = body.get("active_module", handle.session.meta.active_module)
        active_module = slugify(raw_active) if isinstance(raw_active, str) and raw_active else None
        if active_module is not None and active_module not in modules:
            raise ValueError("The active module must be one of the attached modules.")
    except (BookTypeMismatch, ValueError) as exc:
        return _json_error(str(exc))

    if handle.lock.locked():
        return _json_error("The campaign library cannot change during a turn.", 409)
    async with handle.lock:
        existing = {module.slug: module for module in handle.session.meta.modules}
        module_refs: list[ModuleRef] = []
        for index, slug in enumerate(modules):
            reference = existing.get(slug)
            if reference is None:
                reference = ModuleRef(slug=slug, title=titleize(slug))
            else:
                reference = reference.model_copy(deep=True)
            reference.order = index
            module_refs.append(reference)
        selected_active = active_module or (modules[0] if modules else None)
        for reference in module_refs:
            if reference.slug == selected_active and reference.status == "pending":
                reference.status = "active"

        saved_meta = handle.session.meta.model_copy(deep=True)
        saved_meta.sources = sources
        saved_meta.system_source = system_source or (sources[0] if sources else None)
        saved_meta.modules = module_refs
        saved_meta.active_module = selected_active
        handle.session.campaign.save_meta(saved_meta)
        handle.session.meta.sources = saved_meta.sources
        handle.session.meta.system_source = saved_meta.system_source
        handle.session.meta.modules = saved_meta.modules
        handle.session.meta.active_module = saved_meta.active_module
        handle.session.reload_tools()
    return JSONResponse(_campaign_payload(handle))


async def poll_events(request: Request) -> Response:
    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    payloads = [
        payload
        for event in handle.session.background.drain()
        if (payload := _event_payload(event, handle)) is not None
    ]
    payloads.extend(_web_media_payloads(handle))
    response: dict[str, Any] = {"events": payloads}
    if payloads:
        response["state"] = state_snapshot(handle.session)
    return JSONResponse(response)


async def _stream_events(
    handle: SessionHandle,
    source: Callable[[], AsyncIterator[EngineEvent]],
    *,
    before: Callable[[], dict[str, Any] | None] | None = None,
    on_start: Callable[[], Any] | None = None,
) -> StreamingResponse | JSONResponse:
    if handle.lock.locked():
        return _json_error("This campaign already has a turn in progress.", 409)
    await handle.lock.acquire()
    try:
        if on_start is not None:
            on_start()
    except Exception:
        handle.lock.release()
        raise

    async def generate() -> AsyncIterator[bytes]:
        queue: asyncio.Queue[EngineEvent | object] = asyncio.Queue()
        finished = object()
        result: dict[str, Any] = {}
        pump_task: asyncio.Task[None] | None = None

        async def pump() -> None:
            try:
                async for event in source():
                    queue.put_nowait(event)
            except asyncio.CancelledError:
                result["cancelled"] = True
            except Exception as exc:
                result["error"] = exc
            finally:
                queue.put_nowait(finished)

        try:
            initial_payloads: list[dict[str, Any]] = []
            for background_event in handle.session.background.drain():
                payload = _event_payload(background_event, handle)
                if payload is not None:
                    initial_payloads.append(payload)
            initial_payloads.extend(_web_media_payloads(handle))
            if before is not None:
                payload = before()
                if payload is not None:
                    initial_payloads.append(payload)

            # Only the isolated engine task is cancellable. The response task
            # remains alive to close its NDJSON body and release the campaign lock.
            pump_task = asyncio.create_task(pump(), name=f"web-turn-{handle.session.meta.slug}")
            handle.current_task = pump_task
            for payload in initial_payloads:
                yield _ndjson(payload)
            while True:
                event = await queue.get()
                if event is finished:
                    break
                payload = _event_payload(event, handle)
                if payload is not None:
                    yield _ndjson(payload)

            await pump_task
            error = result.get("error")
            if isinstance(error, Exception):
                raise error
            if result.get("cancelled"):
                yield _ndjson({"type": "action_message", "message": "Turn cancelled."})
            yield _ndjson({"type": "state_snapshot", "state": state_snapshot(handle.session)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Web turn failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            yield _ndjson(
                {
                    "type": "engine_error",
                    "message": "The turn could not be completed. Check the server log and retry.",
                    "recoverable": True,
                    "suggest_retry": True,
                }
            )
        finally:
            if pump_task is not None and not pump_task.done():
                pump_task.cancel()
                await pump_task
            if handle.current_task is pump_task:
                handle.current_task = None
            if handle.lock.locked():
                handle.lock.release()

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def run_turn(request: Request) -> Response:
    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    try:
        body = await _request_json(request)
        text = body.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Enter a message before sending.")
        if len(text) > 100_000:
            raise ValueError("Messages must be 100,000 characters or fewer.")
        kind = body.get("kind", "normal")
        if kind not in ("normal", "aside", "steer"):
            raise ValueError("Turn kind must be normal, aside, or steer.")
        quiet = body.get("quiet", False)
        if not isinstance(quiet, bool):
            raise ValueError("quiet must be true or false.")
    except ValueError as exc:
        return _json_error(str(exc))

    async def source() -> AsyncIterator[EngineEvent]:
        async for engine_event in handle.session.handle_input(
            text.strip(),
            steer=kind == "steer",
            ephemeral=kind == "aside" or (kind == "steer" and quiet),
            read_only=kind == "aside",
        ):
            yield engine_event

    return await _stream_events(handle, source, on_start=handle.session.interrupt_narration)


async def import_character(request: Request) -> Response:
    """Import a text-based character sheet through the campaign's GM agent."""

    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    if handle.session.provider is None:
        return _json_error("Connect an AI provider before importing a character sheet.", 409)

    encoded_filename = request.headers.get("x-openadventure-filename", "")
    filename = Path(unquote(encoded_filename)).name
    if not filename or "\x00" in filename:
        return _json_error("Choose a character sheet to import.")
    if Path(filename).suffix.casefold() not in IMPORT_SUFFIXES:
        return _json_error("Import a .md, .txt, or .json character sheet.", 415)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > IMPORT_MAX_BYTES:
                return _json_error("Character sheets must be 160 KB or smaller.", 413)
        except ValueError:
            return _json_error("Upload size could not be read.")

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > IMPORT_MAX_BYTES:
            return _json_error("Character sheets must be 160 KB or smaller.", 413)
        chunks.append(chunk)

    try:
        content = b"".join(chunks).decode("utf-8", errors="replace")
        instruction, _truncated = prepare_character_import(filename, content)
    except ValueError as exc:
        return _json_error(str(exc))

    async def source() -> AsyncIterator[EngineEvent]:
        async for engine_event in handle.session.handle_input(instruction):
            yield engine_event

    return await _stream_events(handle, source, on_start=handle.session.interrupt_narration)


async def _locked_json_action(
    handle: SessionHandle, action: Callable[[], dict[str, Any]]
) -> Response:
    if handle.lock.locked():
        return _json_error("This campaign already has a turn in progress.", 409)
    async with handle.lock:
        try:
            return JSONResponse(action())
        except (DiceError, ValueError) as exc:
            return _json_error(str(exc))


async def campaign_action(request: Request) -> Response:
    action = request.path_params["action"]
    slug = request.path_params["slug"]
    if action == "cancel":
        return JSONResponse({"cancelled": _manager(request).cancel(slug)})

    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle

    if action == "retry":
        plan: dict[str, Any] = {}

        def before_retry() -> dict[str, Any]:
            retry = handle.session.prepare_retry()
            plan["retry"] = retry
            if retry is None:
                return {"type": "action_message", "message": "Nothing to retry."}
            # Replace the visible transcript after the rewind so the discarded
            # response is not left beside the replayed turn in the browser.
            return {
                "type": "history_snapshot",
                "history": [
                    *public_history(handle.session),
                    {
                        "type": "user_message",
                        "role": "user",
                        "text": retry.text,
                        "retry": True,
                    },
                ],
            }

        async def retry_source() -> AsyncIterator[EngineEvent]:
            retry = plan.get("retry")
            if retry is None:
                return
            async for event in handle.session.handle_input(retry.text):
                yield event

        return await _stream_events(
            handle,
            retry_source,
            before=before_retry,
            on_start=handle.session.interrupt_narration,
        )

    if action == "compact":
        return await _stream_events(handle, handle.session.compact_now)

    if action == "recap":
        if handle.lock.locked():
            return _json_error("This campaign already has a turn in progress.", 409)
        async with handle.lock:
            recap_task = asyncio.create_task(
                handle.session.recap(), name=f"web-recap-{handle.session.meta.slug}"
            )
            handle.current_task = recap_task
            try:
                text = await recap_task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                return JSONResponse({"cancelled": True, "text": "Recap cancelled."})
            finally:
                if handle.current_task is recap_task:
                    handle.current_task = None
        return JSONResponse({"text": text or "There is not enough story to recap yet."})

    try:
        body = await _request_json(request)
    except ValueError as exc:
        return _json_error(str(exc))

    if action == "roll":
        expression = body.get("expression", "")
        if not isinstance(expression, str) or not expression.strip():
            return _json_error("A dice expression is required.")

        def roll() -> dict[str, Any]:
            outcome = handle.session.roll_local(expression.strip())
            event = RollResult(
                expression=outcome.expression,
                total=outcome.total,
                detail=outcome.detail(),
                max_rolls=outcome.max_rolls,
                min_rolls=outcome.min_rolls,
            )
            return {
                "event": sanitize_event(event, mode=handle.session.meta.mode),
                "state": state_snapshot(handle.session),
            }

        return await _locked_json_action(handle, roll)

    if action == "undo":
        count = body.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 30:
            return _json_error("Undo count must be between 1 and 30.")

        def undo() -> dict[str, Any]:
            from openadventure.engine.commands import run

            result = run(handle.session, "undo", str(count))
            messages = []
            if result is not None:
                messages = [
                    {"severity": str(message.severity), "text": message.text}
                    for message in result.messages
                ]
            return {
                "message": messages[-1]["text"] if messages else "Turn undone.",
                "messages": messages,
                "state": state_snapshot(handle.session),
                "history": public_history(handle.session),
            }

        return await _locked_json_action(handle, undo)

    return _json_error(f"Unknown action {action!r}.", 404)


async def update_settings(request: Request) -> Response:
    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    try:
        body = await _request_json(request)
    except ValueError as exc:
        return _json_error(str(exc))
    generation_keys = {"model", "effort", "thinking", "verbosity", "context_budget"}
    media_bool_keys = {
        "tts_enabled",
        "sound_effects_enabled",
        "music_enabled",
        "images_enabled",
        "music_auto",
        "images_auto",
    }
    allowed = generation_keys | media_bool_keys | {"mode", "music_volume"}
    unknown = set(body) - allowed
    if unknown:
        return _json_error(f"Unknown setting(s): {', '.join(sorted(unknown))}.")
    if handle.lock.locked():
        return _json_error("Settings cannot change during a turn.", 409)
    async with handle.lock:
        try:
            mode = body.get("mode", handle.session.meta.mode)
            if mode not in ("gm", "assistant"):
                raise ValueError("mode must be 'gm' or 'assistant'")
            if "thinking" in body and not isinstance(body["thinking"], bool):
                raise ValueError("thinking must be true or false")
            if "context_budget" in body and (
                not isinstance(body["context_budget"], int)
                or isinstance(body["context_budget"], bool)
                or body["context_budget"] <= 0
            ):
                raise ValueError("context_budget must be a positive integer")
            for key in ("model", "effort", "verbosity"):
                if key in body and (not isinstance(body[key], str) or not body[key].strip()):
                    raise ValueError(f"{key} must be non-empty text")
            for key in media_bool_keys:
                if key in body and not isinstance(body[key], bool):
                    raise ValueError(f"{key} must be true or false")
            if "music_volume" in body and (
                not isinstance(body["music_volume"], int | float)
                or isinstance(body["music_volume"], bool)
                or not 0 <= float(body["music_volume"]) <= 1
            ):
                raise ValueError("music_volume must be between 0 and 1")

            proposed = dict(handle.session.meta.settings)
            proposed.update({key: body[key] for key in generation_keys if key in body})
            proposed.update(
                {key: body[key] for key in ("music_auto", "images_auto") if key in body}
            )
            if "music_volume" in body:
                proposed["music_volume"] = float(body["music_volume"])
            settings = resolve_settings(proposed, handle.session.config, handle.session.models)
            provider_before = handle.session.provider_name()
            provider_after = handle.session.models.provider_for(settings.model)
            provider = handle.session.provider
            if "model" in body or provider_after != provider_before:
                # Build the replacement before committing metadata. Provider
                # construction can fail, and a failed request must not leave the
                # campaign configured for a model the live session cannot use.
                provider = handle.session.provider_for_settings(settings)

            # Persist one validated metadata snapshot, then update the shared
            # in-memory object that ToolContext already references.
            saved_meta = handle.session.meta.model_copy(deep=True)
            saved_meta.mode = mode
            saved_meta.settings = proposed
            for key in (
                "tts_enabled",
                "sound_effects_enabled",
                "music_enabled",
                "images_enabled",
            ):
                if key in body:
                    setattr(saved_meta, key, body[key])
            reload_tools = any(
                getattr(saved_meta, key) != getattr(handle.session.meta, key)
                for key in (
                    "mode",
                    "tts_enabled",
                    "sound_effects_enabled",
                    "music_enabled",
                    "images_enabled",
                )
            )
            stop_music = handle.session.meta.music_enabled and not saved_meta.music_enabled
            handle.session.campaign.save_meta(saved_meta)
            handle.session.meta.mode = mode
            handle.session.meta.settings = proposed
            handle.session.meta.tts_enabled = saved_meta.tts_enabled
            handle.session.meta.sound_effects_enabled = saved_meta.sound_effects_enabled
            handle.session.meta.music_enabled = saved_meta.music_enabled
            handle.session.meta.images_enabled = saved_meta.images_enabled
            handle.session.settings = settings
            handle.session.provider = provider
            if "music_volume" in body:
                handle.session.media_host.set_music_volume(float(body["music_volume"]))
            if stop_music:
                handle.session.stop_music()
            if reload_tools:
                handle.session.reload_tools()
        except (TypeError, ValueError) as exc:
            return _json_error(str(exc))
    return JSONResponse(_campaign_payload(handle))


async def save_credential(request: Request) -> Response:
    """Persist one local provider key without ever returning it to the browser."""

    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    try:
        body = await _request_json(request)
        if set(body) != {"service", "api_key"}:
            raise ValueError("Credentials require a service and API key.")
        service = body["service"]
        api_key = body["api_key"]
        if not isinstance(service, str) or service not in CREDENTIAL_SERVICES:
            raise ValueError("Choose a supported local service.")
        if not isinstance(api_key, str):
            raise ValueError("Enter an API key.")
    except ValueError as exc:
        return _json_error(str(exc))
    if handle.lock.locked():
        return _json_error("Credentials cannot change during a turn.", 409)
    async with handle.lock:
        try:
            save_local_api_key(CREDENTIAL_SERVICES[service], api_key)
            # A Google image key can also satisfy a Gemini game model, and a
            # just-entered provider key should take effect without a restart.
            handle.session.connect_provider()
            handle.session.reload_tools()
        except (OSError, ValueError) as exc:
            return _json_error(str(exc))
    return JSONResponse(_campaign_payload(handle))


async def campaign_media(request: Request) -> Response:
    slug = request.path_params["slug"]
    if slugify(slug) != slug:
        return _json_error("Media file not found.", 404)
    try:
        campaign = _workspace(request).campaign(slug)
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)
    kind = request.path_params["kind"]
    media_path = request.path_params["media_path"]
    target: Path | None = None
    allowed_suffixes: set[str]
    if kind == "audio":
        handle = await _session_handle(request)
        if isinstance(handle, JSONResponse):
            return handle
        host = handle.session.media_host
        if isinstance(host, WebMediaHost):
            target = host.resolve_audio(media_path)
        allowed_suffixes = AUDIO_SUFFIXES
    else:
        roots = {"images": campaign.images_dir, "music": campaign.music_dir}
        root = roots.get(kind)
        if root is None:
            return _json_error("Unknown media type.", 404)
        try:
            target = (root / media_path).resolve(strict=True)
            target.relative_to(root.resolve())
        except FileNotFoundError, OSError, ValueError:
            return _json_error("Media file not found.", 404)
        allowed_suffixes = IMAGE_SUFFIXES if kind == "images" else AUDIO_SUFFIXES
    if target is None or not target.is_file():
        return _json_error("Media file not found.", 404)
    if target.suffix.casefold() not in allowed_suffixes:
        return _json_error("Media file type is not allowed.", 415)
    return FileResponse(
        target,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Cross-Origin-Resource-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
        },
    )


def create_app(config: AppConfig | None = None) -> Starlette:
    config = config or load_config()
    workspace = Workspace(config.workspace_dir)
    workspace.ensure()
    sessions = SessionManager(config, workspace)
    library_jobs = LibraryJobManager(config, workspace, sessions)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        await library_jobs.close()
        await sessions.close_all()

    routes = [
        Route("/", homepage),
        Route("/api/health", health),
        Route("/api/bootstrap", bootstrap),
        Route("/api/library", library_overview),
        Route("/api/library/ingest", start_ingest, methods=["POST"]),
        Route("/api/library/jobs/{job_id:str}", get_library_job),
        Route(
            "/api/library/jobs/{job_id:str}/cancel",
            cancel_library_job,
            methods=["POST"],
        ),
        Route(
            "/api/library/books/{book_slug:str}/template",
            start_template,
            methods=["POST"],
        ),
        Route("/api/campaigns", create_campaign, methods=["POST"]),
        Route("/api/campaigns/{slug:str}", get_campaign),
        Route("/api/campaigns/{slug:str}/state", get_state),
        Route("/api/campaigns/{slug:str}/usage", get_usage),
        Route("/api/campaigns/{slug:str}/events", poll_events),
        Route("/api/campaigns/{slug:str}/turn", run_turn, methods=["POST"]),
        Route("/api/campaigns/{slug:str}/import", import_character, methods=["POST"]),
        Route(
            "/api/campaigns/{slug:str}/actions/{action:str}",
            campaign_action,
            methods=["POST"],
        ),
        Route(
            "/api/campaigns/{slug:str}/settings",
            update_settings,
            methods=["PATCH"],
        ),
        Route(
            "/api/campaigns/{slug:str}/credentials",
            save_credential,
            methods=["POST"],
        ),
        Route(
            "/api/campaigns/{slug:str}/library",
            update_campaign_library,
            methods=["PATCH"],
        ),
        Route(
            "/api/campaigns/{slug:str}/media/{kind:str}/{media_path:path}",
            campaign_media,
        ),
        Mount("/static", app=LocalStaticFiles(directory=STATIC_DIR), name="static"),
    ]
    middleware = [
        Middleware(
            TrustedHostMiddleware,
            allowed_hosts=["127.0.0.1", "localhost", "testserver"],
            www_redirect=False,
        ),
        Middleware(LocalMutationGuard),
    ]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.config = config
    app.state.workspace = workspace
    app.state.sessions = sessions
    app.state.library_jobs = library_jobs
    return app


def run_server(
    *, workspace: str | Path | None = None, port: int = 8000, open_browser: bool = True
) -> None:
    """Start the localhost server used by ``openadventure web``."""
    if not 1 <= port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    import uvicorn

    url = f"http://{LOCAL_HOST}:{port}"
    print(f"OpenAdventure is ready at {url}")
    if open_browser:
        timer = threading.Timer(0.7, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    uvicorn.run(create_app(load_config(workspace)), host=LOCAL_HOST, port=port, workers=1)
