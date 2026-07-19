"""Workspace-scoped library jobs for the localhost browser interface."""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from openadventure.config import AppConfig, resolve_api_key, set_utility_model
from openadventure.engine.session import resolve_utility_settings
from openadventure.ingest import embeddings, pipeline, template_gen
from openadventure.providers.base import ModelRegistry
from openadventure.providers.factory import build_provider
from openadventure.store.workspace import Workspace, slugify

if TYPE_CHECKING:
    from openadventure.web.sessions import SessionManager

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
SUPPORTED_SUFFIXES = {".pdf", ".md", ".markdown", ".txt"}
MAX_RETAINED_JOBS = 64
_ROUND_RE = re.compile(r"^Round\s+(\d+)/(\d+):\s*(.*)$", re.IGNORECASE)


class LibraryJobError(ValueError):
    """A safe user-facing library job validation error."""


class LibraryJobConflict(LibraryJobError):
    """The requested book already exists or has an active job."""


@dataclass
class LibraryJob:
    id: str
    kind: Literal["ingest", "template"]
    book_slug: str
    label: str
    status: JobStatus = "queued"
    phase: str = "Queued"
    message: str = "Waiting to start"
    completed: int = 0
    total: int = 0
    round: int | None = None
    max_rounds: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    cancellable: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    started_at: float = field(default_factory=monotonic, repr=False)
    events: list[dict[str, Any]] = field(default_factory=list)
    event_seq: int = field(default=0, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "book_slug": self.book_slug,
            "label": self.label,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "completed": self.completed,
            "total": self.total,
            "round": self.round,
            "max_rounds": self.max_rounds,
            "result": self.result,
            "error": self.error,
            "cancellable": self.cancellable,
            "created_at": self.created_at,
            "elapsed_seconds": round(max(0.0, monotonic() - self.started_at), 1),
            "events": list(self.events),
        }


class LibraryJobManager:
    """Run and retain observable ingestion and template-generation jobs."""

    def __init__(
        self,
        config: AppConfig,
        workspace: Workspace,
        sessions: SessionManager,
    ) -> None:
        self.config = config
        self.workspace = workspace
        self.sessions = sessions
        self.models = ModelRegistry.load_default()
        self._jobs: dict[str, LibraryJob] = {}
        self._active_books: dict[str, str] = {}
        self._start_lock = asyncio.Lock()
        self._closed = False

    def get(self, job_id: str) -> LibraryJob:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise FileNotFoundError(f"no library job named {job_id!r}") from None

    def snapshots(self) -> list[dict[str, Any]]:
        jobs = sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)
        return [job.snapshot() for job in jobs]

    async def start_ingest(
        self,
        source_path: Path,
        *,
        original_name: str,
        name: str,
        book_type: str,
        pages: tuple[int, int] | None,
    ) -> LibraryJob:
        if self._closed:
            raise RuntimeError("the library job manager is closed")
        suffix = Path(original_name).suffix.casefold()
        if suffix not in SUPPORTED_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
            raise LibraryJobError(f"Choose a supported document: {supported}.")
        if pages is not None and suffix != ".pdf":
            raise LibraryJobError("Page ranges are only available for PDF documents.")
        if book_type not in ("source", "module"):
            raise LibraryJobError("Book type must be source or module.")
        clean_name = name.strip() or Path(original_name).stem
        slug = slugify(clean_name)
        destination = self.workspace.book_dir(slug)

        async with self._start_lock:
            self._prune()
            if slug in self._active_books:
                raise LibraryJobConflict(f"A library job for {slug!r} is already running.")
            if destination.exists():
                raise LibraryJobConflict(
                    f"A library book named {slug!r} already exists. Choose another name."
                )
            job = self._new_job("ingest", slug, f"Ingesting {clean_name}")
            self._active_books[slug] = job.id
            job.task = asyncio.create_task(
                self._run_ingest(
                    job,
                    source_path=source_path,
                    original_name=Path(original_name).name,
                    destination=destination,
                    book_type=book_type,
                    pages=pages,
                ),
                name=f"web-ingest-{slug}",
            )
        return job

    async def start_template(
        self,
        slug: str,
        *,
        model: str | None,
        overwrite: bool,
    ) -> LibraryJob:
        if self._closed:
            raise RuntimeError("the library job manager is closed")
        normalized = slugify(slug)
        if normalized != slug:
            raise LibraryJobError(f"No rules source named {slug!r}.")
        source_dir = self.workspace.book_dir(slug)
        if not pipeline.is_ingested(source_dir):
            raise LibraryJobError(f"No ingested rules source named {slug!r}.")
        if self.workspace.book_type(slug) == "module":
            raise LibraryJobError("Character templates can only be generated from rule sources.")
        template_path = source_dir / "templates" / "character.json"
        if template_path.is_file() and not overwrite:
            raise LibraryJobConflict(
                f"{slug!r} already has a character template. Confirm regeneration first."
            )

        visible_ids = {entry.id for entry in self.models.visible}
        chosen = model or resolve_utility_settings(self.config).model
        if model is not None and chosen not in visible_ids:
            raise LibraryJobError(f"Unknown model {chosen!r}.")
        settings = resolve_utility_settings(self.config).merged({"model": chosen})
        provider_name = self.models.provider_for(chosen)
        api_key = resolve_api_key(self.config, provider_name)
        if not api_key:
            raise LibraryJobError(
                f"Template generation needs the {provider_name} API key for {chosen}."
            )
        provider = build_provider(provider_name, api_key, self.models)

        async with self._start_lock:
            self._prune()
            if slug in self._active_books:
                raise LibraryJobConflict(f"A library job for {slug!r} is already running.")
            job = self._new_job("template", slug, f"Building {slug} character template")
            job.cancellable = True
            self._active_books[slug] = job.id
            if model is not None:
                set_utility_model(self.config, chosen)
            job.task = asyncio.create_task(
                self._run_template(job, provider, settings, source_dir),
                name=f"web-template-{slug}",
            )
        return job

    async def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job.cancellable or job.task is None or job.task.done():
            return False
        job.task.cancel()
        return True

    async def close(self) -> None:
        self._closed = True
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for job in self._jobs.values():
            if job.kind == "template" and job.task is not None and not job.task.done():
                job.task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _new_job(self, kind: Literal["ingest", "template"], slug: str, label: str) -> LibraryJob:
        job = LibraryJob(id=uuid4().hex, kind=kind, book_slug=slug, label=label)
        self._jobs[job.id] = job
        self._record(job, phase="Queued", message="Job accepted")
        return job

    def _record(
        self,
        job: LibraryJob,
        *,
        phase: str,
        message: str,
        completed: int | None = None,
        total: int | None = None,
        round_number: int | None = None,
        max_rounds: int | None = None,
    ) -> None:
        job.phase = phase
        job.message = message
        if completed is not None:
            job.completed = max(0, completed)
        if total is not None:
            job.total = max(0, total)
        job.round = round_number
        job.max_rounds = max_rounds
        job.event_seq += 1
        event = {
            "seq": job.event_seq,
            "phase": job.phase,
            "message": job.message,
            "completed": job.completed,
            "total": job.total,
            "round": job.round,
            "max_rounds": job.max_rounds,
            "elapsed_seconds": round(max(0.0, monotonic() - job.started_at), 1),
        }
        job.events.append(event)
        if len(job.events) > 160:
            job.events[:] = job.events[-160:]

    def _ingest_progress(self, job_id: str, phase: str, completed: int, total: int) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.status not in ("queued", "running"):
            return
        self._record(
            job,
            phase=phase,
            message=f"{phase}: {completed} of {total}" if total else phase,
            completed=completed,
            total=total,
        )

    async def _run_ingest(
        self,
        job: LibraryJob,
        *,
        source_path: Path,
        original_name: str,
        destination: Path,
        book_type: str,
        pages: tuple[int, int] | None,
    ) -> None:
        staging_root = self.config.workspace_dir / ".web-jobs" / job.id
        staging = staging_root / "book"
        staged_source = staging_root / "upload" / Path(original_name).name
        loop = asyncio.get_running_loop()
        job.status = "running"
        self._record(job, phase="Reading document", message=f"Opening {original_name}")

        def progress(phase: str, completed: int, total: int) -> None:
            loop.call_soon_threadsafe(self._ingest_progress, job.id, phase, completed, total)

        def work() -> dict[str, Any]:
            backend, embed_reason = embeddings.try_load_backend(self.config.embeddings)
            manifest = pipeline.ingest(
                staged_source,
                staging,
                pages=pages,
                book_type=book_type,
                embed_backend=backend,
                progress=progress,
            )
            return {
                "manifest": manifest,
                "index": pipeline.index_report(staging),
                "image_note": pipeline.image_only_pages_note(manifest),
                "embedding_note": embed_reason,
            }

        try:
            staged_source.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, source_path, staged_source)
            result = await asyncio.to_thread(work)
            await asyncio.sleep(0)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                staging.rename(destination)
            except FileExistsError:
                raise LibraryJobConflict(
                    f"A library book named {job.book_slug!r} was created while this job ran."
                ) from None
            job.status = "succeeded"
            job.result = result
            self._record(
                job, phase="Ready", message="Book is ready in the library", completed=1, total=1
            )
            self._refresh_sessions(job.book_slug)
        except Exception as exc:
            job.status = "failed"
            job.error = self._safe_error(exc, source_path, original_name)
            self._record(job, phase="Failed", message=job.error)
        finally:
            source_path.unlink(missing_ok=True)
            shutil.rmtree(staging_root, ignore_errors=True)
            self._active_books.pop(job.book_slug, None)

    async def _run_template(
        self,
        job: LibraryJob,
        provider: Any,
        settings: Any,
        source_dir: Path,
    ) -> None:
        job.status = "running"
        self._record(
            job,
            phase="Researching rules",
            message="The template agent is mapping character creation",
        )

        def on_progress(message: str) -> None:
            match = _ROUND_RE.match(message.strip())
            if match:
                round_number = int(match.group(1))
                max_rounds = int(match.group(2))
                detail = match.group(3) or "Reading the rules"
                self._record(
                    job,
                    phase="Researching rules",
                    message=detail,
                    completed=round_number,
                    total=max_rounds,
                    round_number=round_number,
                    max_rounds=max_rounds,
                )
            else:
                self._record(job, phase="Researching rules", message=message)

        try:
            template = await template_gen.derive_template(
                provider,
                settings,
                source_dir,
                job.book_slug,
                on_progress=on_progress,
            )
            if template is None:
                raise RuntimeError("The template agent finished without saving a template.")
            job.status = "succeeded"
            job.result = {
                "fields": len(template.get("fields", [])),
                "resources": len(template.get("resources", [])),
                "model": settings.model,
            }
            self._record(
                job, phase="Ready", message="Character template saved", completed=1, total=1
            )
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error = "Template generation was cancelled."
            self._record(job, phase="Cancelled", message=job.error)
        except Exception as exc:
            job.status = "failed"
            job.error = self._safe_error(exc, source_dir, job.book_slug)
            self._record(job, phase="Failed", message=job.error)
        finally:
            self._active_books.pop(job.book_slug, None)

    def _refresh_sessions(self, slug: str) -> None:
        for handle in self.sessions.sessions.values():
            meta = handle.session.meta
            if slug in meta.sources or slug in {module.slug for module in meta.modules}:
                handle.session.reload_tools()

    def _safe_error(self, exc: Exception, private_path: Path, public_name: str) -> str:
        message = str(exc).strip() or type(exc).__name__
        replacements = {
            str(private_path): public_name,
            str(self.config.workspace_dir): "workspace",
        }
        for private, public in replacements.items():
            message = message.replace(private, public)
        return message[:500]

    def _prune(self) -> None:
        if len(self._jobs) < MAX_RETAINED_JOBS:
            return
        finished = [
            job_id
            for job_id, job in self._jobs.items()
            if job.status in ("succeeded", "failed", "cancelled")
        ]
        for job_id in finished[: max(0, len(self._jobs) - MAX_RETAINED_JOBS + 1)]:
            self._jobs.pop(job_id, None)
