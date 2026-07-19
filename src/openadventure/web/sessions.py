"""Long-lived game sessions owned by the web server.

The storage layer is deliberately single-writer.  ``EventLog`` keeps its next
sequence number in memory and snapshot files use a fixed temporary filename, so
two ``GameSession`` instances must never drive the same campaign concurrently.
This module gives the web frontend one cached session and one turn lock per
campaign.  HTTP handlers may serve any number of readers, but must hold the
handle's ``lock`` while a command or turn mutates the campaign.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from openadventure.config import AppConfig, resolve_api_key
from openadventure.engine.session import GameSession
from openadventure.media.host import MediaCapabilities, MediaHost
from openadventure.providers.factory import build_provider
from openadventure.store.workspace import Campaign, Workspace, slugify

MediaHostFactory = Callable[[Campaign], MediaHost]


class WebMediaHost:
    """Present generated media through browser-consumable events and URLs.

    Generation remains in the engine's configured media backends. This host only
    records presentation instructions. The existing web event poll drains them,
    while the controlled media route serves the referenced local files.
    """

    def __init__(self, campaign: Campaign, *, music_volume: float = 0.2) -> None:
        self.campaign = campaign
        self._capabilities = MediaCapabilities.all()
        self._events: deque[dict[str, Any]] = deque()
        self._audio: OrderedDict[str, Path] = OrderedDict()
        self._lock = threading.Lock()
        self._music_prompt: str | None = None
        self._music_length: float | None = None
        self._music_volume = max(0.0, min(1.0, float(music_volume)))
        self._audio_active = False

    @property
    def capabilities(self) -> MediaCapabilities:
        return self._capabilities

    def _media_url(self, kind: str, path: Path) -> str | None:
        roots = {"images": self.campaign.images_dir, "music": self.campaign.music_dir}
        root = roots.get(kind)
        if root is None:
            return None
        try:
            relative = Path(path).resolve(strict=True).relative_to(root.resolve())
        except FileNotFoundError, OSError, ValueError:
            return None
        slug = quote(self.campaign.load_meta().slug, safe="")
        encoded = quote(relative.as_posix(), safe="/")
        return f"/api/campaigns/{slug}/media/{kind}/{encoded}"

    def _audio_url(self, path: Path) -> str | None:
        try:
            target = Path(path).resolve(strict=True)
        except FileNotFoundError, OSError:
            return None
        token = uuid4().hex
        with self._lock:
            self._audio[token] = target
            while len(self._audio) > 256:
                self._audio.popitem(last=False)
        slug = quote(self.campaign.load_meta().slug, safe="")
        return f"/api/campaigns/{slug}/media/audio/{token}"

    def _emit(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._events.append(payload)

    async def play_speech(self, path: Path) -> None:
        url = self._audio_url(path)
        if url:
            with self._lock:
                self._audio_active = True
                self._events.append({"type": "audio_ready", "kind": "speech", "path": url})

    async def play_sound_effect(self, path: Path) -> None:
        url = self._audio_url(path)
        if url:
            with self._lock:
                self._audio_active = True
                self._events.append({"type": "audio_ready", "kind": "sound_effect", "path": url})

    def play_music(
        self, path: Path, *, prompt: str = "", length_seconds: float | None = None
    ) -> None:
        url = self._media_url("music", path)
        if url is None:
            return
        with self._lock:
            self._music_prompt = prompt
            self._music_length = length_seconds
            self._events.append(
                {
                    "type": "music_started",
                    "track": url,
                    "mood": prompt,
                    "length_seconds": length_seconds,
                }
            )

    def stop_music(self) -> None:
        with self._lock:
            self._music_prompt = None
            self._music_length = None
            self._events.append({"type": "music_stopped"})

    def set_music_volume(self, value: float) -> float:
        volume = max(0.0, min(1.0, float(value)))
        with self._lock:
            self._music_volume = volume
            self._events.append({"type": "media_volume", "kind": "music", "value": volume})
        return volume

    def music_volume(self) -> float:
        with self._lock:
            return self._music_volume

    def music_status_line(self) -> str | None:
        with self._lock:
            if self._music_prompt is None:
                return None
            length = f" ({int(self._music_length)}s track)" if self._music_length else ""
            return (
                f"looping {self._music_prompt!r}{length} "
                f"at volume {int(round(self._music_volume * 100))}%"
            )

    def stop_audio(self) -> None:
        with self._lock:
            if not self._audio_active:
                return
            self._audio_active = False
            self._events = deque(event for event in self._events if event["type"] != "audio_ready")
            self._events.append({"type": "audio_stopped"})

    def present_image(self, path: Path, *, caption: str = "") -> None:
        url = self._media_url("images", path)
        if url:
            self._emit({"type": "show_image", "path": url, "caption": caption})

    def present_handout(self, title: str, body: str, *, path: Path | None = None) -> None:
        self._emit({"type": "handout", "title": title, "body": body})

    def drain_events(self) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def resolve_audio(self, token: str) -> Path | None:
        if len(token) != 32 or not token.isalnum():
            return None
        with self._lock:
            path = self._audio.get(token)
        return path if path is not None and path.is_file() else None


@dataclass
class SessionHandle:
    """One campaign's cached engine plus its single-writer coordination state."""

    session: GameSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_task: asyncio.Task[Any] | None = None

    @property
    def busy(self) -> bool:
        """Whether a turn task is currently running for this campaign."""

        return self.current_task is not None and not self.current_task.done()


class SessionManager:
    """Lazily create and retain exactly one ``GameSession`` per campaign slug."""

    def __init__(
        self,
        config: AppConfig,
        workspace: Workspace | None = None,
        *,
        media_host_factory: MediaHostFactory | None = None,
        docs: dict[str, str] | None = None,
    ) -> None:
        self.config = config
        self.workspace = workspace or Workspace(config.workspace_dir)
        self.workspace.ensure()
        self._media_host_factory = media_host_factory or (
            lambda campaign: WebMediaHost(
                campaign,
                music_volume=float(config.media.get("music_volume", 0.2)),
            )
        )
        self._docs = docs
        self._sessions: dict[str, SessionHandle] = {}
        self._creation_lock = asyncio.Lock()
        self._closed = False

    @property
    def sessions(self) -> dict[str, SessionHandle]:
        """A shallow copy of the active handles, keyed by campaign slug."""

        return dict(self._sessions)

    async def get(self, slug: str) -> SessionHandle:
        """Return the campaign's cached handle, creating it on first use.

        Provider setup is intentionally noninteractive.  The campaign's selected
        model determines the provider, and its key is resolved from normal config
        and environment sources.  With no key the session remains usable for
        local commands and emits the engine's normal missing-provider error for a
        play turn.
        """

        if self._closed:
            raise RuntimeError("the web session manager is closed")
        normalized = slugify(slug)
        if normalized != slug:
            # ``Workspace.campaign`` accepts a path-like string.  Rejecting rather
            # than silently normalizing protects this server boundary from path
            # traversal and makes route behavior predictable.
            raise FileNotFoundError(f"no campaign named {slug!r}")

        cached = self._sessions.get(slug)
        if cached is not None:
            return cached

        async with self._creation_lock:
            cached = self._sessions.get(slug)
            if cached is not None:
                return cached
            if self._closed:
                raise RuntimeError("the web session manager is closed")

            campaign = self.workspace.campaign(slug)
            session = self._build_session(campaign)
            handle = SessionHandle(session=session)
            self._sessions[slug] = handle
            return handle

    def cancel(self, slug: str) -> bool:
        """Request cancellation of the active turn for ``slug``.

        Returns ``True`` only when an unfinished task was found.  Cancellation is
        cooperative: the caller that owns the task must await it and clear
        ``current_task`` in a ``finally`` block.
        """

        handle = self._sessions.get(slug)
        if handle is None or not handle.busy:
            return False
        assert handle.current_task is not None
        handle.current_task.cancel()
        handle.session.interrupt_narration()
        return True

    async def close_all(self) -> None:
        """Cancel active work and close every cached engine exactly once."""

        async with self._creation_lock:
            if self._closed:
                return
            self._closed = True
            handles = list(self._sessions.values())
            self._sessions.clear()

        active = [handle.current_task for handle in handles if handle.busy]
        for task in active:
            if task is not None:
                task.cancel()
        if active:
            await asyncio.gather(
                *(task for task in active if task is not None), return_exceptions=True
            )

        # ``GameSession.close`` handles narration, music, and compaction.  Cancel
        # the remaining web-capable background kinds too so shutdown does not
        # leave image generation or a queued media stop task behind.
        for handle in handles:
            session = handle.session
            for kind in ("image", "music", "music-stop", "sfx", "tts", "compaction"):
                session.background.cancel_kind(kind)
            session.close()
        await asyncio.gather(
            *(handle.session.background.wait_all() for handle in handles),
            return_exceptions=True,
        )

    def _build_session(self, campaign: Campaign) -> GameSession:
        session = GameSession(
            self.config,
            self.workspace,
            campaign,
            provider=None,
            media_host=self._media_host_factory(campaign),
            docs=self._docs,
        )
        provider_name = session.provider_name()
        api_key = resolve_api_key(self.config, provider_name)
        if api_key:
            session.provider = build_provider(provider_name, api_key, session.models)
        session.resume_music()
        return session
