"""Local ASGI application for the OpenAdventure browser interface."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import webbrowser
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from openadventure.config import AppConfig, load_config
from openadventure.engine.events import EngineEvent, RollResult
from openadventure.engine.session import resolve_settings
from openadventure.mechanics.dice import DiceError
from openadventure.store.workspace import BookTypeMismatch, Campaign, Workspace, slugify
from openadventure.web.sessions import SessionHandle, SessionManager
from openadventure.web.views import (
    bootstrap_payload,
    campaign_payload,
    public_history,
    sanitize_event,
    state_snapshot,
)

STATIC_DIR = Path(__file__).with_name("static")
LOCAL_HOST = "127.0.0.1"
logger = logging.getLogger(__name__)


class LocalMutationGuard:
    """Reject browser-driven cross-site writes to the unauthenticated local API."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            content_type = headers.get(b"content-type", b"").decode("latin-1")
            if content_type.partition(";")[0].strip().lower() != "application/json":
                response = JSONResponse(
                    {"error": "Mutating requests require Content-Type: application/json."},
                    status_code=415,
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


async def _session_handle(request: Request) -> SessionHandle | JSONResponse:
    try:
        return await _manager(request).get(request.path_params["slug"])
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)


def _media_url(campaign: Campaign, slug: str, raw_path: str) -> str | None:
    """Turn a generated-image path into a URL without exposing its disk path."""
    try:
        target = Path(raw_path).resolve(strict=True)
        relative = target.relative_to(campaign.images_dir.resolve())
    except FileNotFoundError, OSError, ValueError:
        return None
    encoded_slug = quote(slug, safe="")
    encoded_path = quote(relative.as_posix(), safe="/")
    return f"/api/campaigns/{encoded_slug}/media/images/{encoded_path}"


def _event_payload(event: EngineEvent, handle: SessionHandle) -> dict[str, Any] | None:
    session = handle.session
    return sanitize_event(
        event,
        mode=session.meta.mode,
        image_url=lambda path: _media_url(session.campaign, session.meta.slug, path),
    )


def _ndjson(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


async def homepage(request: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html")


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def bootstrap(request: Request) -> Response:
    return JSONResponse(bootstrap_payload(_config(request), _workspace(request)))


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
    return JSONResponse(campaign_payload(handle.session), status_code=201)


async def get_campaign(request: Request) -> Response:
    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    return JSONResponse(campaign_payload(handle.session))


async def get_state(request: Request) -> Response:
    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    return JSONResponse({"state": state_snapshot(handle.session)})


async def poll_events(request: Request) -> Response:
    handle = await _session_handle(request)
    if isinstance(handle, JSONResponse):
        return handle
    payloads = [
        payload
        for event in handle.session.background.drain()
        if (payload := _event_payload(event, handle)) is not None
    ]
    response: dict[str, Any] = {"events": payloads}
    if payloads:
        response["state"] = state_snapshot(handle.session)
    return JSONResponse(response)


async def _stream_events(
    handle: SessionHandle,
    source: Callable[[], AsyncIterator[EngineEvent]],
    *,
    before: Callable[[], dict[str, Any] | None] | None = None,
) -> StreamingResponse | JSONResponse:
    if handle.lock.locked():
        return _json_error("This campaign already has a turn in progress.", 409)
    await handle.lock.acquire()

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

    def source() -> AsyncIterator[EngineEvent]:
        return handle.session.handle_input(
            text.strip(),
            steer=kind == "steer",
            ephemeral=kind == "aside" or (kind == "steer" and quiet),
            read_only=kind == "aside",
        )

    return await _stream_events(handle, source)


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

        return await _stream_events(handle, retry_source, before=before_retry)

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
    allowed = {"model", "effort", "thinking", "verbosity", "context_budget"}
    unknown = set(body) - allowed - {"mode"}
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

            proposed = dict(handle.session.meta.settings)
            proposed.update({key: body[key] for key in allowed if key in body})
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
            handle.session.campaign.save_meta(saved_meta)
            handle.session.meta.mode = mode
            handle.session.meta.settings = proposed
            handle.session.settings = settings
            handle.session.provider = provider
        except (TypeError, ValueError) as exc:
            return _json_error(str(exc))
    return JSONResponse(campaign_payload(handle.session))


async def campaign_media(request: Request) -> Response:
    slug = request.path_params["slug"]
    if slugify(slug) != slug:
        return _json_error("Media file not found.", 404)
    try:
        campaign = _workspace(request).campaign(slug)
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)
    kind = request.path_params["kind"]
    roots = {"images": campaign.images_dir, "music": campaign.music_dir}
    root = roots.get(kind)
    if root is None:
        return _json_error("Unknown media type.", 404)
    try:
        target = (root / request.path_params["media_path"]).resolve(strict=True)
        target.relative_to(root.resolve())
    except FileNotFoundError, OSError, ValueError:
        return _json_error("Media file not found.", 404)
    if not target.is_file():
        return _json_error("Media file not found.", 404)
    return FileResponse(target)


def create_app(config: AppConfig | None = None) -> Starlette:
    config = config or load_config()
    workspace = Workspace(config.workspace_dir)
    workspace.ensure()
    sessions = SessionManager(config, workspace)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        await sessions.close_all()

    routes = [
        Route("/", homepage),
        Route("/api/health", health),
        Route("/api/bootstrap", bootstrap),
        Route("/api/campaigns", create_campaign, methods=["POST"]),
        Route("/api/campaigns/{slug:str}", get_campaign),
        Route("/api/campaigns/{slug:str}/state", get_state),
        Route("/api/campaigns/{slug:str}/events", poll_events),
        Route("/api/campaigns/{slug:str}/turn", run_turn, methods=["POST"]),
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
            "/api/campaigns/{slug:str}/media/{kind:str}/{media_path:path}",
            campaign_media,
        ),
        Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
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
