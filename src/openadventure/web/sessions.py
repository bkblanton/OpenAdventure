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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openadventure.config import AppConfig, resolve_api_key
from openadventure.engine.session import GameSession
from openadventure.media.host import MediaCapabilities, MediaHost, NullMediaHost
from openadventure.providers.factory import build_provider
from openadventure.store.workspace import Campaign, Workspace, slugify

MediaHostFactory = Callable[[], MediaHost]


class WebMediaHost(NullMediaHost):
    """A browser-presented media surface with no server-side playback.

    Images are delivered by ``ImageGenerated``/``ShowImage`` events and handouts
    are a browser concern.  Speech, effects, and looping music stay disabled so
    the engine neither spends money generating inaudible audio nor tries to use
    speakers on the server.
    """

    def __init__(self) -> None:
        super().__init__(MediaCapabilities(images=True, handouts=True))


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
        self._media_host_factory = media_host_factory or WebMediaHost
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
            media_host=self._media_host_factory(),
            docs=self._docs,
        )
        provider_name = session.provider_name()
        api_key = resolve_api_key(self.config, provider_name)
        if api_key:
            session.provider = build_provider(provider_name, api_key, session.models)
        return session
