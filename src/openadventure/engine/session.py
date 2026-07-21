"""GameSession: owns a campaign's state, provider, tools, and settings.

`handle_input()` is the entire play surface; frontends consume the
EngineEvent stream and never touch internals.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Literal

from openadventure.config import AppConfig
from openadventure.engine import agent
from openadventure.engine.context import (
    ContextBudget,
    est_tokens,
    render_history,
    tool_schema_tokens,
)
from openadventure.engine.events import BackgroundTaskStarted, EngineError, EngineEvent
from openadventure.engine.prompts import build_context_foot, build_context_head, build_system
from openadventure.engine.tools import build_registry
from openadventure.engine.tools.registry import ToolContext, ToolRegistry
from openadventure.mechanics import dice
from openadventure.providers.base import (
    HIGH_EFFORT_SETTINGS,
    Effort,
    GenerationSettings,
    Message,
    ModelInfo,
    ModelRegistry,
    Provider,
    SystemBlock,
    TextBlock,
    Usage,
    Verbosity,
)
from openadventure.store import canon, snapshots
from openadventure.store.workspace import Campaign, Workspace

if TYPE_CHECKING:
    from openadventure.media.host import MediaHost

SETTING_KEYS = {
    "model",
    "max_tokens",
    "effort",
    "verbosity",
    "thinking",
    "context_budget",
}
MAX_MODULE_SECTIONS_IN_CONTEXT = 80
_NATURAL_PART_RE = re.compile(r"(\d+)")


def _natural_string_key(text: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _NATURAL_PART_RE.split(text)
        if part
    )


def _natural_path_key(path: PurePath) -> tuple[tuple[tuple[int, int | str], ...], ...]:
    return tuple(_natural_string_key(part) for part in path.as_posix().split("/"))


def resolve_settings(
    overrides: dict[str, Any], config: AppConfig, models: ModelRegistry
) -> GenerationSettings:
    """Generation settings = type defaults, then config.model, then per-campaign
    overrides (meta.settings). Unknown keys in overrides are ignored, so a stale
    "quality" left by an older campaign is harmless."""
    base = GenerationSettings()
    if config.model and "model" not in overrides:
        base = base.merged({"model": config.model})
    return base.merged(overrides)


def resolve_utility_settings(config: AppConfig) -> GenerationSettings:
    """Settings for *out-of-game* jobs (the CLI ``openadventure
    template``/``ingest`` paths), where no campaign is loaded so there is no table
    model to borrow. Character-template derivation is the first such job; more
    accuracy-first, off-the-real-time-path work may join it later.

    Favors accuracy (HIGH_EFFORT_SETTINGS: GPT-5.6 Terra, thinking on at high
    effort), overridable per workspace via config.toml [utility]; this is the
    default the out-of-game wizard offers. The model picks its own backend (e.g.
    a gemini-* model runs on Gemini).

    In-game, off-table work uses the campaign's table model instead, via
    ``GameSession.high_effort_settings`` (the table model run at high effort).
    """
    return HIGH_EFFORT_SETTINGS.merged(config.utility)


def estimate_cost(usage: Usage, model: ModelInfo) -> float:
    """Rough USD cost for ``usage`` at ``model``'s per-MTok rates.

    Cache reads bill at a tenth of the input rate and cache writes at 1.25x, the
    Anthropic pricing convention; Gemini and OpenAI report no cache-write tokens
    (their prefix caches are implicit), so that term is zero for them. Output
    already includes reasoning/thinking tokens across all three backends. A model
    with unknown pricing (rates 0.0) yields 0.0."""
    return (
        usage.input_tokens * model.input_per_mtok
        + usage.cache_creation_input_tokens * model.input_per_mtok * 1.25
        + usage.cache_read_input_tokens * model.input_per_mtok * 0.10
        + usage.output_tokens * model.output_per_mtok
    ) / 1_000_000


# These are deliberately small, explicit estimates for the built-in media
# backends, not a claim that an account's invoice will match exactly. Media APIs
# use different billing units (credits, image resolution, generated seconds),
# and custom backends are unpriced until they can expose a reliable rate.
_MEDIA_USD_PER_UNIT = {
    ("image", "GeminiImageBackend", "gemini-3.1-flash-image"): 0.067,
    ("tts", "ElevenLabsTTS", "eleven_flash_v2_5"): 0.000075,
    ("sound_effect", "ElevenLabsSoundEffects", "eleven_text_to_sound_v2"): 0.003333,
    ("music", "ElevenLabsMusic", "music_v1"): 0.010,
}
_COST_COMPONENTS = ("text", "images", "tts", "sound_effects", "music")
_MEDIA_COMPONENTS = {
    "image": "images",
    "tts": "tts",
    "sound_effect": "sound_effects",
    "music": "music",
}


def empty_cost_breakdown() -> dict[str, float]:
    """A JSON-friendly component breakdown, including its derived total."""

    return {**dict.fromkeys(_COST_COMPONENTS, 0.0), "total": 0.0}


def normalized_cost_breakdown(value: object, *, legacy_total: float = 0.0) -> dict[str, float]:
    """Read a current or pre-media ``usage.json`` cost breakdown safely."""

    raw = value if isinstance(value, dict) else {}
    breakdown = {key: float(raw.get(key, 0.0) or 0.0) for key in _COST_COMPONENTS}
    if not raw and legacy_total:
        # Earlier usage files only recorded one aggregate model-token cost.
        breakdown["text"] = float(legacy_total)
    breakdown["total"] = round(sum(breakdown.values()), 6)
    return breakdown


def estimate_media_cost(
    usage: Usage,
    *,
    kind: str,
    backend_name: str,
    model_id: str | None,
) -> float:
    """Estimate one completed media operation's USD cost.

    Only the bundled backend/model combinations receive a rate. A custom or
    unrecognized backend still contributes usage counters but intentionally adds
    zero dollars rather than presenting an invented charge as a real estimate.
    """

    rate = _MEDIA_USD_PER_UNIT.get((kind, backend_name, model_id or ""), 0.0)
    match kind:
        case "image":
            units = usage.image_count
        case "tts":
            units = usage.tts_characters
        case "sound_effect":
            units = usage.sound_effect_seconds
        case "music":
            units = usage.music_seconds
        case _:
            units = 0
    return float(units) * rate


def empty_usage_data() -> dict[str, Any]:
    """The persisted campaign usage schema, including all optional estimates."""

    return {
        "totals": Usage().model_dump(),
        "cost_usd": 0.0,
        "by_model": {},
        "cost_breakdown": empty_cost_breakdown(),
    }


def normalize_usage_data(raw: object) -> dict[str, Any]:
    """Upgrade a current or legacy ``usage.json`` payload without writing it.

    This is shared by live sessions and read-only browser projections, so an
    older campaign looks complete even before its next generation call writes a
    migrated snapshot to disk.
    """

    data = dict(raw) if isinstance(raw, dict) else empty_usage_data()
    totals_raw = data.get("totals")
    data["totals"] = Usage.model_validate(
        totals_raw if isinstance(totals_raw, dict) else {}
    ).model_dump()
    data["cost_usd"] = float(data.get("cost_usd", 0.0) or 0.0)

    by_model_raw = data.get("by_model")
    by_model: dict[str, dict[str, Any]] = {}
    if isinstance(by_model_raw, dict):
        for model_id, row in by_model_raw.items():
            if not isinstance(row, dict):
                continue
            normalized = Usage.model_validate(row).model_dump()
            normalized["cost_usd"] = float(row.get("cost_usd", 0.0) or 0.0)
            by_model[str(model_id)] = normalized
    data["by_model"] = by_model
    data["cost_breakdown"] = normalized_cost_breakdown(
        data.get("cost_breakdown"), legacy_total=data["cost_usd"]
    )
    # The component total is canonical after migration. On an older file it
    # equals its legacy aggregate because normalized_cost_breakdown maps that
    # aggregate onto text.
    data["cost_usd"] = data["cost_breakdown"]["total"]
    return data


@dataclass
class RetryPlan:
    """The result of preparing a ``/retry``: the last user message to replay and
    whether its turn was actually undone first (``False`` when no checkpoint
    survived, so the caller replays without undoing the prior effects)."""

    text: str
    undone: bool


class GameSession:
    def __init__(
        self,
        config: AppConfig,
        workspace: Workspace,
        campaign: Campaign,
        provider: Provider | None,
        *,
        models: ModelRegistry | None = None,
        registry: ToolRegistry | None = None,
        session_seed: int | None = None,
        media_host: MediaHost | None = None,
        docs: dict[str, str] | None = None,
    ):
        from openadventure.media.factory import load_backends
        from openadventure.media.host import MediaCapabilities, NullMediaHost
        from openadventure.media.narration import NarrationAgent
        from openadventure.media.tasks import BackgroundTasks

        # The frontend presents media; the engine generates it. With no host
        # (headless/tests) presentation is a no-op but every surface is assumed
        # available, preserving the tools-registered, local-console behavior.
        self.media_host = media_host or NullMediaHost(MediaCapabilities.all())
        self.media_capabilities = self.media_host.capabilities
        self.config = config
        self.workspace = workspace
        self.campaign = campaign
        self.meta = campaign.load_meta()
        if campaign.sync_modules(self.meta, set(workspace.list_books())):
            campaign.save_meta(self.meta)
        self.log = campaign.open_log()
        prior_entries = self.log.read_all()
        self.has_prior_play = any(
            entry.type in ("user_message", "gm_message") for entry in prior_entries
        )
        self.provider = provider
        self.models = models or ModelRegistry.load_default()
        self.settings = resolve_settings(self.meta.settings, config, self.models)
        self.background = BackgroundTasks()
        self.session_usage = Usage()
        self.session_cost_usd = 0.0
        self.session_cost_breakdown = empty_cost_breakdown()
        self._compacting = False  # single-flight guard for the canon chronicler
        media_backends = load_backends(config.media)
        self.images = media_backends[0]
        self.music = media_backends[1]
        self.tts = media_backends[2]
        self.sound_effects = media_backends[3]
        self._apply_music_volume()
        self._apply_narrator_voice()
        self.narration = NarrationAgent(
            campaign,
            self.log,
            self.background,
            self.tts,
            self.sound_effects,
            host=self.media_host,
            usage_recorder=self.accrue_media_usage,
        )
        # one embedding backend per session (model load is expensive); None when
        # disabled or the optional dep is absent -> hybrid search degrades to FTS5
        from openadventure.ingest import embeddings

        self.embed_backend = embeddings.load_backend(config.embeddings)
        # Frontend-supplied self-knowledge (README + its --help and slash commands)
        # for read_docs; None falls back to the README alone.
        self.docs = docs
        self.tools = registry or build_registry(
            workspace,
            campaign,
            self.meta,
            media_backends=media_backends,
            embed_backend=self.embed_backend,
            capabilities=self.media_capabilities,
            docs=self.docs,
            usage_recorder=self.accrue_media_usage,
        )

        seed = session_seed if session_seed is not None else random.SystemRandom().randrange(2**32)
        self.rng = random.Random(seed)
        self.tool_ctx = ToolContext(
            workspace=workspace,
            campaign=campaign,
            meta=self.meta,
            log=self.log,
            rng=self.rng,
            background=self.background,
            narration=self.narration,
            media_host=self.media_host,
            usage_recorder=self.accrue_media_usage,
        )
        self.log.append("session_start", {"session_seed": seed})

    # --- play surface ---------------------------------------------------
    async def handle_input(
        self,
        text: str,
        *,
        debug: bool = False,
        steer: bool = False,
        ephemeral: bool = False,
        read_only: bool = False,
    ) -> AsyncIterator[EngineEvent]:
        """Run one turn over ``text``.

        ``ephemeral`` (both ``/btw`` and ``/sudo --quiet``) runs the turn without
        touching the campaign log or checkpoints: the conversation leaves no trace
        in the story. ``read_only`` (a ``/btw`` aside) additionally restricts the
        turn to read-only tools, so it can answer from lookups without changing
        state. A quiet ``/sudo`` directive is ephemeral but not read-only, so it
        still mutates the world.
        """
        if self.provider is None:
            yield EngineError(
                message=(
                    "No AI provider configured: set ANTHROPIC_API_KEY (env or .env) "
                    "and restart. Slash commands still work."
                ),
                recoverable=False,
            )
            return
        from openadventure.store import checkpoints

        self._sync_narration_backends()
        self.tool_ctx.sound_effect_cues.clear()
        if not ephemeral:
            checkpoints.save(self.campaign, self.log.last_seq)
            checkpoints.prune(self.campaign)
        try:
            async for event in agent.run_turn(
                self, text, debug=debug, steer=steer, ephemeral=ephemeral, read_only=read_only
            ):
                yield event
        except asyncio.CancelledError:
            if not ephemeral:
                self.log.append("turn_aborted")
            raise
        if ephemeral:
            return
        # Automatic compaction runs in the BACKGROUND so the player never waits
        # for the periodic stall. The ~20% tail headroom (compaction triggers at
        # 80%) is the room to keep playing while it runs; its result drains
        # between turns. Manual /compact stays foreground (see compact_now).
        started = self._spawn_compaction()
        if started is not None:
            yield started

    def _spawn_compaction(self) -> BackgroundTaskStarted | None:
        """Kick the canon chronicler onto the background runner. Single-flight:
        skips if a pass is already in flight or there is nothing to compact yet.
        Returns the started event to surface, or None."""
        from openadventure.engine import compaction

        if self._compacting or self.provider is None:
            return None
        if not compaction.should_compact(self):
            return None
        self._compacting = True

        async def work() -> list[EngineEvent]:
            try:
                # Drop compaction_started and compaction_progress: this result
                # drains after the pass is already done, so a "compacting…" line
                # or a live-spinner heartbeat would render stale (and the progress
                # ticks only drive the foreground /compact spinner anyway).
                skip = ("compaction_started", "compaction_progress")
                return [
                    event
                    async for event in compaction.run_compaction(self)
                    if event.type not in skip
                ]
            finally:
                self._compacting = False

        return self.background.spawn("compaction", "Compacting the story", work())

    async def compact_now(self) -> AsyncIterator[EngineEvent]:
        """Manual /compact: foreground, with progress, because the user asked and
        is waiting. If a background pass is already underway, join it rather than
        launch a duplicate (single-flight)."""
        from openadventure.engine import compaction

        if self._compacting:
            await self.background.wait_all()
            return
        self._compacting = True
        try:
            async for event in compaction.run_compaction(self, force=True):
                yield event
        finally:
            self._compacting = False

    # --- timeline (undo / retry / restart) --------------------------------
    def undo(self, n: int = 1):
        """Take back the last ``n`` AI turns: state and conversation both.
        Returns an ``UndoReport``; raises ``TimelineError`` when nothing can be
        undone. Frontend-agnostic: the CLI, and later any other frontend, share
        this instead of re-wiring the campaign/log plumbing themselves."""
        from openadventure.engine.timeline import undo_turns

        # A background chronicler pass may be summarizing a span this undo rewinds;
        # cancel it. Atomic writes + idempotent ops make a late write harmless,
        # and the summary is restored from the checkpoint regardless.
        self.background.cancel_kind("compaction")
        self._compacting = False
        return undo_turns(self.campaign, self.log, n)

    def prepare_retry(self) -> RetryPlan | None:
        """Undo the last turn and report the user message to replay, or ``None``
        when there's nothing to retry. The undo is best-effort: a turn with no
        surviving checkpoint still returns its text (``undone=False``) so the
        caller can replay it without undoing the prior effects."""
        from openadventure.engine.timeline import TimelineError, undo_turns

        last = next((e for e in reversed(self.log.read_all()) if e.type == "user_message"), None)
        if last is None:
            return None
        text = last.data.get("text", "")
        try:
            undo_turns(self.campaign, self.log, 1)
        except TimelineError:
            return RetryPlan(text=text, undone=False)
        return RetryPlan(text=text, undone=True)

    def restart(self, characters: Literal["original", "reroll"] = "original"):
        """Start the campaign over: stop live audio, archive the story, and
        reset or clear the party. Returns a ``RestartReport``. Owns the full
        reset (narration, music, log refresh, session marker) so every frontend
        gets a clean restart from one call."""
        from openadventure.engine.timeline import restart_campaign

        # Starting over should leave nothing from the old story playing. The log
        # is refreshed just below, so don't bother marking the stop.
        self.interrupt_narration()
        self.stop_music(mark_stopped=False)
        report = restart_campaign(self.campaign, characters=characters)
        self.log.refresh()
        self.log.append("session_start", {"restarted": True})
        return report

    def roll_local(self, expression: str) -> dice.RollOutcome:
        """Local /roll: same RNG and log as AI rolls."""
        outcome = dice.roll(expression, self.rng)
        self.log.append(
            "roll",
            {
                "expression": outcome.expression,
                "total": outcome.total,
                "detail": outcome.detail(),
                "by": "player",
            },
        )
        return outcome

    def set_mode(self, mode: str) -> None:
        if mode not in ("gm", "assistant"):
            raise ValueError("mode must be 'gm' or 'assistant'")
        self.meta.mode = mode  # type: ignore[assignment]
        self.campaign.save_meta(self.meta)

    def set_premise(self, text: str | None) -> str | None:
        """Set or clear the campaign premise: the seed idea the GM builds on.

        Returns the saved premise, or None when cleared. Applies from the next
        turn (the premise rides in the per-turn context block)."""
        value = text.strip() if isinstance(text, str) and text.strip() else None
        self.meta.premise = value
        self.campaign.save_meta(self.meta)
        return value

    def add_source(self, name: str) -> str | None:
        """Attach an ingested source (rulebook, monster manual, setting guide…).

        Appends the slug to ``sources`` if not already present, makes it the
        ``system_source`` when none is set, then reloads the toolset so
        search_rules/read_rules pick it up right away. Returns the slug, or None
        for empty input. Raises :class:`BookTypeMismatch` if the book was
        ingested as an adventure module."""
        from openadventure.store.workspace import ensure_book_type, slugify

        if not isinstance(name, str) or not name.strip():
            return None
        slug = slugify(name)
        ensure_book_type(self.workspace, slug, "source")
        if slug not in self.meta.sources:
            self.meta.sources.append(slug)
        if self.meta.system_source is None:
            self.meta.system_source = slug
        self.campaign.save_meta(self.meta)
        self.reload_tools()
        return slug

    def remove_source(self, name: str) -> bool:
        """Detach a source. If it was the system source, the next remaining source
        (if any) becomes the system source. Returns True if it was attached."""
        from openadventure.store.workspace import slugify

        slug = slugify(name) if isinstance(name, str) and name.strip() else ""
        if slug not in self.meta.sources:
            return False
        self.meta.sources.remove(slug)
        if self.meta.system_source == slug:
            self.meta.system_source = self.meta.sources[0] if self.meta.sources else None
        self.campaign.save_meta(self.meta)
        self.reload_tools()
        return True

    def set_system_source(self, name: str | None) -> str | None:
        """Designate which attached source defines the rules system and character
        template (attaching it first if needed), or clear it with None. Returns the
        slug, or None when cleared. Raises :class:`BookTypeMismatch` if the book
        was ingested as an adventure module."""
        from openadventure.store.workspace import ensure_book_type, slugify

        value = slugify(name) if isinstance(name, str) and name.strip() else None
        if value is not None:
            ensure_book_type(self.workspace, value, "source")
        if value is not None and value not in self.meta.sources:
            self.meta.sources.append(value)
        self.meta.system_source = value
        self.campaign.save_meta(self.meta)
        self.reload_tools()
        return value

    def set_sources(self, names: list[str], system: str | None = None) -> list[str]:
        """Replace the attached sources wholesale (used by the setup wizard). The
        system source defaults to the first entry. Returns the stored slugs.
        Raises :class:`BookTypeMismatch` if any book was ingested as a module."""
        from openadventure.store.workspace import ensure_book_type, slugify

        slugs: list[str] = []
        for name in names:
            slug = slugify(name)
            if slug and slug not in slugs:
                slugs.append(slug)
        for slug in slugs:
            ensure_book_type(self.workspace, slug, "source")
        sys_src = slugify(system) if system else (slugs[0] if slugs else None)
        self.meta.sources = slugs
        self.meta.system_source = sys_src if sys_src in slugs else (slugs[0] if slugs else None)
        self.campaign.save_meta(self.meta)
        self.reload_tools()
        return slugs

    def clear_sources(self) -> None:
        """Detach every source and clear the system source."""
        self.meta.sources = []
        self.meta.system_source = None
        self.campaign.save_meta(self.meta)
        self.reload_tools()

    # --- modules ----------------------------------------------------------
    def add_module(self, name: str) -> str | None:
        """Attach an ingested book as an adventure module for this campaign.

        Appends a ModuleRef (in arc order) if not already attached, activates it
        when nothing is in play, and reloads the toolset so search_campaign picks
        it up. Returns the slug, or None for empty input. Raises
        :class:`BookTypeMismatch` if the book was ingested as a rules source."""
        from openadventure.store.workspace import ModuleRef, ensure_book_type, slugify, titleize

        if not isinstance(name, str) or not name.strip():
            return None
        slug = slugify(name)
        ensure_book_type(self.workspace, slug, "module")
        if slug not in {m.slug for m in self.meta.modules}:
            order = max((m.order for m in self.meta.modules), default=-1) + 1
            self.meta.modules.append(ModuleRef(slug=slug, title=titleize(slug), order=order))
        if self.meta.active_module is None:
            self.meta.active_module = slug
            for module in self.meta.modules:
                if module.slug == slug and module.status == "pending":
                    module.status = "active"
        self.campaign.save_meta(self.meta)
        self.reload_tools()
        return slug

    def remove_module(self, name: str) -> bool:
        """Detach a module. If it was active, the next unfinished module (if any)
        becomes active. Returns True if it was attached."""
        from openadventure.store.workspace import slugify

        slug = slugify(name) if isinstance(name, str) and name.strip() else ""
        if slug not in {m.slug for m in self.meta.modules}:
            return False
        self.meta.modules = [m for m in self.meta.modules if m.slug != slug]
        for index, module in enumerate(self.meta.modules):
            module.order = index
        if self.meta.active_module == slug:
            nxt = next((m for m in self.meta.modules if m.status != "completed"), None)
            nxt = nxt or (self.meta.modules[0] if self.meta.modules else None)
            self.meta.active_module = nxt.slug if nxt else None
            if nxt is not None and nxt.status == "pending":
                nxt.status = "active"
        self.campaign.save_meta(self.meta)
        self.reload_tools()
        return True

    def set_modules(self, names: list[str], active: str | None = None) -> list[str]:
        """Replace the attached modules wholesale (used by the setup wizard),
        preserving arc state for modules that stay. The active module defaults to
        the first entry. Returns the stored slugs in order. Raises
        :class:`BookTypeMismatch` if any book was ingested as a rules source."""
        from openadventure.store.workspace import ModuleRef, ensure_book_type, slugify, titleize

        slugs: list[str] = []
        for name in names:
            slug = slugify(name)
            if slug and slug not in slugs:
                slugs.append(slug)
        for slug in slugs:
            ensure_book_type(self.workspace, slug, "module")
        existing = {m.slug: m for m in self.meta.modules}
        modules: list[ModuleRef] = []
        for index, slug in enumerate(slugs):
            ref = existing.get(slug) or ModuleRef(slug=slug, title=titleize(slug))
            ref.order = index
            modules.append(ref)
        self.meta.modules = modules
        act = slugify(active) if active else (slugs[0] if slugs else None)
        self.meta.active_module = act if act in slugs else (slugs[0] if slugs else None)
        if self.meta.active_module is not None:
            for module in self.meta.modules:
                if module.slug == self.meta.active_module and module.status == "pending":
                    module.status = "active"
        self.campaign.save_meta(self.meta)
        self.reload_tools()
        return slugs

    def set_active_module(self, slug: str) -> bool:
        """Make an attached module the active one (NOW PLAYING) and reload the
        toolset. Returns False when no attached module has that slug."""
        target = next((m for m in self.meta.modules if m.slug == slug), None)
        if target is None:
            return False
        self.meta.active_module = target.slug
        target.status = "active"
        self.campaign.save_meta(self.meta)
        self.reload_tools()
        return True

    def set_arc(self, text: str | None) -> str | None:
        """Set or clear the campaign arc blurb. Returns the stored value."""
        self.meta.arc = text or None
        self.campaign.save_meta(self.meta)
        return self.meta.arc

    def provider_name(self) -> str:
        """The backend the current model runs on: the model selects it."""
        return self.models.provider_for(self.settings.model)

    def connect_provider(self) -> bool:
        """(Re)build ``provider`` for the current model's backend, resolving its
        API key. Returns True if connected, False if no key is configured (in
        which case ``provider`` is set to None and play falls back to dice-only).

        Called after the model changes (``/model``) so switching to a model on a
        different backend transparently switches the backend too."""
        from openadventure.config import resolve_api_key
        from openadventure.providers.factory import build_provider

        name = self.provider_name()
        api_key = resolve_api_key(self.config, name)
        if not api_key:
            self.provider = None
            return False
        self.provider = build_provider(name, api_key, self.models)
        return True

    def attach_provider(self, api_key: str) -> None:
        """Attach a provider for the current model's backend from an explicit key
        (e.g. one just entered interactively), bypassing env/config resolution."""
        from openadventure.providers.factory import build_provider

        self.provider = build_provider(self.provider_name(), api_key, self.models)

    def high_effort_settings(self) -> GenerationSettings:
        """The high-effort profile for in-game off-table work: character-template
        derivation and the background canon chronicler. Both run on the campaign's
        table model but with deeper reasoning (thinking on at high effort), since
        they're off the real-time path and latency doesn't matter.

        This deliberately reuses the table model rather than the workspace
        ``[utility]`` config. That config now only defaults out-of-game jobs,
        where there is no campaign model to borrow."""
        return HIGH_EFFORT_SETTINGS.merged({"model": self.settings.model})

    def provider_for_settings(self, settings: GenerationSettings) -> Provider | None:
        """A provider for off-hot-path work (the canon chronicler) that may run on
        a different model and backend than the table. Reuses the live chat
        provider when the backend matches; otherwise builds one for that backend
        from its API key, or returns None if no key is configured.

        A provider is backend-level (the model is chosen per call via ``settings``),
        so reusing ``self.provider`` whenever the backend matches is correct."""
        from openadventure.config import resolve_api_key
        from openadventure.providers.factory import build_provider

        name = self.models.provider_for(settings.model)
        if self.provider is not None and name == self.provider_name():
            return self.provider
        api_key = resolve_api_key(self.config, name)
        if not api_key:
            return None
        return build_provider(name, api_key, self.models)

    def set_tts_enabled(self, enabled: bool) -> None:
        self.meta.tts_enabled = enabled
        self.campaign.save_meta(self.meta)

    def set_sound_effects_enabled(self, enabled: bool) -> None:
        self.meta.sound_effects_enabled = enabled
        self.campaign.save_meta(self.meta)
        self.reload_tools()

    def set_music_enabled(self, enabled: bool) -> None:
        self.meta.music_enabled = enabled
        self.campaign.save_meta(self.meta)
        if not enabled:
            self.stop_music()
        self.reload_tools()

    def set_images_enabled(self, enabled: bool) -> None:
        self.meta.images_enabled = enabled
        self.campaign.save_meta(self.meta)
        self.reload_tools()

    def set_music_volume(self, value: float) -> float:
        volume = self.media_host.set_music_volume(float(value))
        self.meta.settings["music_volume"] = volume
        self.campaign.save_meta(self.meta)
        return volume

    def stop_music(self, *, mark_stopped: bool = True) -> None:
        """Stop playback (via the host) and cancel any in-flight generation.

        ``mark_stopped`` records a stop marker in the log, so a later resume
        knows music was deliberately off when the table last left it and does
        not bring back a track the player silenced. It defaults to True (the
        intentional stops: ``/music stop`` and ``/music off``); session teardown
        and restart pass False, since exiting the app is not "turn the music
        off" and must not block the next resume."""
        if hasattr(self.background, "cancel_kind"):
            self.background.cancel_kind("music")
        self.media_host.stop_music()
        if mark_stopped:
            self.log.append("media", {"kind": "music", "action": "stop"})

    def music_status_line(self) -> str | None:
        return self.media_host.music_status_line()

    def _apply_music_volume(self) -> None:
        volume = self.meta.settings.get("music_volume")
        if volume is not None:
            self.media_host.set_music_volume(float(volume))

    def last_music_track(self) -> tuple[Path, str, float | None] | None:
        """The most recently played track still in effect, as
        ``(path, prompt, length_seconds)``, or None if music was last stopped,
        never played, or its rendered file is no longer on disk. Resume replays
        this file straight from disk, so it never re-hits the generative API."""
        for entry in reversed(self.log.read_all()):
            if entry.type != "media" or entry.data.get("kind") != "music":
                continue
            data = entry.data
            if data.get("action") == "stop":
                return None
            prompt = data.get("prompt")
            path = data.get("path")
            if not isinstance(prompt, str) or not prompt.strip() or not path:
                return None
            track_path = Path(path)
            if not track_path.is_file():
                return None
            length = data.get("length_seconds")
            return track_path, prompt.strip(), (float(length) if length is not None else None)
        return None

    def start_music(self, prompt: str, *, by: str = "player"):
        """Generate ``prompt`` and start looping it through the host, in the
        background. Returns the spawned BackgroundTaskStarted, or None when no
        music backend is configured. Shared by /music play and resume."""
        backend = self.music
        if backend is None:
            return None

        from openadventure.engine.events import MusicStarted
        from openadventure.media.music import persist_track

        async def work():
            track = await backend.generate(prompt)
            length_seconds = getattr(track, "length_seconds", None)
            if length_seconds is None:
                length_seconds = getattr(backend, "default_length_seconds", 0.0)
            if length_seconds:
                self.accrue_media_usage(
                    Usage(music_seconds=float(length_seconds)),
                    "music",
                    type(backend).__name__,
                    getattr(backend, "model_id", None),
                )
            # Copy out of the shared temp cache into the campaign under a readable
            # name, so the track persists with the campaign and resume can replay it.
            path = await asyncio.to_thread(
                persist_track, self.campaign.music_dir, track.path, prompt
            )
            self.media_host.play_music(path, prompt=prompt, length_seconds=length_seconds)
            self.log.append(
                "media",
                {
                    "kind": "music",
                    "prompt": prompt,
                    "path": str(path),
                    "length_seconds": length_seconds,
                    "by": by,
                },
            )
            return [MusicStarted(track=prompt)]

        self.background.cancel_kind("music")
        return self.background.spawn("music", f"Composing music: {prompt[:60]}…", work())

    def replay_music(self) -> str | None:
        """Replay the last track from its file on disk through the host, with no
        generative API call. Returns the track's prompt on success, or None when
        the host can't play music or there is nothing on disk to resume."""
        if not self.media_host.capabilities.music:
            return None
        track = self.last_music_track()
        if track is None:
            return None
        path, prompt, length = track
        self.media_host.play_music(path, prompt=prompt, length_seconds=length)
        return prompt

    def resume_music(self) -> str | None:
        """When resuming a GM campaign with music on, replay the track that was
        last playing from disk so the scene isn't silent until the next music cue.
        Replays the rendered file rather than regenerating it, so resuming is free
        and instant. Returns the resumed track's prompt, or None when nothing was
        replayed."""
        if self.meta.mode != "gm" or not self.meta.music_enabled or "play_music" not in self.tools:
            return None
        return self.replay_music()

    def narrator_voice_id(self) -> str | None:
        """The per-campaign narrator voice override, or None to use the default."""
        from openadventure.media.narration import NARRATOR_VOICE_SETTING

        value = self.meta.settings.get(NARRATOR_VOICE_SETTING)
        return value.strip() if isinstance(value, str) and value.strip() else None

    def set_narrator_voice_id(self, voice_id: str | None) -> str | None:
        """Pin the ElevenLabs voice the GM narrator speaks with for this campaign.

        Pass None (or blank) to clear the override and fall back to the config
        default. Applies to the live backend immediately; returns the saved id
        or None when cleared."""
        from openadventure.media.narration import NARRATOR_VOICE_SETTING

        if voice_id is None or not voice_id.strip():
            self.meta.settings.pop(NARRATOR_VOICE_SETTING, None)
            result: str | None = None
        else:
            result = voice_id.strip()
            self.meta.settings[NARRATOR_VOICE_SETTING] = result
        self.campaign.save_meta(self.meta)
        self._apply_narrator_voice()
        return result

    def _apply_narrator_voice(self) -> None:
        """Push the per-campaign narrator voice override (or, when cleared, the
        config default) onto the live ElevenLabs TTS backend, which is where
        narration reads the narrator's ``voice_id`` from."""
        backend = self.tts
        if backend is None or not hasattr(backend, "voice_id"):
            return
        override = self.narrator_voice_id()
        if override:
            backend.voice_id = override
        elif backend.__class__.__name__ == "ElevenLabsTTS":
            from openadventure.media.tts import DEFAULT_ELEVENLABS_VOICE_ID

            backend.voice_id = self.config.media.get(
                "elevenlabs_voice_id", DEFAULT_ELEVENLABS_VOICE_ID
            )

    def custom_instructions(self) -> str | None:
        value = self.meta.settings.get("custom_instructions")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def set_custom_instructions(self, text: str | None) -> str | None:
        """Store free-form GM style/personality instructions for this campaign.

        Returns the saved value, or None when cleared. Applies from the next
        turn (the system prompt is rebuilt per turn)."""
        if text is None or not text.strip():
            self.meta.settings.pop("custom_instructions", None)
            self.campaign.save_meta(self.meta)
            return None
        value = text.strip()
        self.meta.settings["custom_instructions"] = value
        self.campaign.save_meta(self.meta)
        return value

    def queue_narration(
        self,
        text: str,
        *,
        sound_effect_cues: list | None = None,
    ):
        """Narrate visible GM output in the background when GM-mode TTS is on."""
        if (
            self.meta.mode != "gm"
            or not self.meta.tts_enabled
            or self.tts is None
            or not text.strip()
        ):
            return None
        self._sync_narration_backends()
        return self.narration.queue_turn(text, sound_effects=sound_effect_cues)

    def replay_narration(self):
        """Re-narrate the most recent turn from cached audio, making no new API
        calls. Stops any narration still playing first. Returns the spawned task,
        or None when narration is off or nothing has been narrated yet."""
        if self.meta.mode != "gm" or not self.meta.tts_enabled or self.tts is None:
            return None
        self._sync_narration_backends()
        return self.narration.queue_replay(interrupt=True)

    def interrupt_narration(self) -> int:
        """Stop current narration audio and cancel queued narration tasks."""
        self._sync_narration_backends()
        return self.narration.interrupt()

    def reload_tools(self) -> None:
        """Re-evaluate conditional tools (after an in-session ingest)."""
        from openadventure.media.factory import load_backends

        # drop module refs whose source has gone and keep the active one valid
        if self.campaign.sync_modules(self.meta, set(self.workspace.list_books())):
            self.campaign.save_meta(self.meta)

        # Regenerated backends are safe to swap wholesale: the music loop lives in
        # the host, not the backend, so reloading never orphans a playing track.
        media_backends = load_backends(self.config.media)
        self.images = media_backends[0]
        self.music = media_backends[1]
        self.tts = media_backends[2]
        self.sound_effects = media_backends[3]
        self._apply_music_volume()
        self._apply_narrator_voice()
        self._sync_narration_backends()
        self.tools = build_registry(
            self.workspace,
            self.campaign,
            self.meta,
            media_backends=media_backends,
            embed_backend=self.embed_backend,
            capabilities=self.media_capabilities,
            docs=self.docs,
            usage_recorder=self.accrue_media_usage,
        )
        self.tool_ctx.narration = self.narration

    def _sync_narration_backends(self) -> None:
        self.narration.tts = self.tts
        self.narration.sound_effects = self.sound_effects

    def close(self) -> None:
        self.interrupt_narration()
        self.stop_music(mark_stopped=False)  # exiting isn't "stop the music"; keep resume working
        self.background.cancel_kind("compaction")  # don't leave a chronicler pass dangling
        self._compacting = False
        self.log.append("session_end")

    def first_gm_message_if_only_turn(self) -> str | None:
        """The GM's opening message verbatim, when it is the only turn played
        (e.g. the campaign kickoff). With a single GM message there is nothing to
        summarize, so the recap just replays it instead of paying for an AI pass.
        Returns None once play has gone further."""
        gm_messages = [e for e in self.log.read_all() if e.type == "gm_message"]
        if len(gm_messages) != 1:
            return None
        return gm_messages[0].data.get("text") or None

    async def recap(self) -> str | None:
        """AI "Previously, on…" recap of recent play; narrated when TTS is on.

        Returns None when no provider is configured or nothing has happened yet.
        """
        from openadventure.engine.recap import generate_recap

        text = await generate_recap(self)
        if text:
            self.queue_narration(text)
        return text

    # --- prompt assembly --------------------------------------------------
    def has_character_template(self) -> bool:
        """Whether the system source has a derived character-sheet template.

        Templates are optional: when one exists (generated out of band by
        ``openadventure template``) the GM follows it for ``create_sheet``; when
        absent the GM improvises character creation from the rules. Nothing is
        derived automatically; the CLI is the only thing that writes one."""
        from openadventure.engine.prompts import load_character_template

        return load_character_template(self.meta, self.workspace) is not None

    def build_system(self) -> list[SystemBlock]:
        return build_system(self.meta, self.workspace)

    def settings_summary(self) -> str:
        """The live generation settings, for the GM to answer out-of-character
        questions about its model and dials. Rides in the per-turn context (not
        the cached system prompt) so a mid-session change is reflected at once.

        Lists values only, no commands: how to change a setting is
        frontend-specific (slash command, menu, button) and lives in read_docs,
        whose command help the frontend supplies."""
        s = self.settings
        return (
            f"Model: {s.model}. "
            f"Effort: {s.effort.value}. "
            f"Thinking: {'on' if s.thinking else 'off'}. "
            f"Narration verbosity: {s.verbosity.value}. "
            f"Context budget: {s.context_budget:,} tokens."
        )

    def build_messages(
        self, *, system_tokens: int | None = None, tool_tokens: int | None = None
    ) -> tuple[list[Message], int]:
        """Context head + rendered log tail + context foot. Returns (messages, est).

        The context splits by time: the stable head (premise, arc, canon, summary)
        leads, the replayed history follows, and the point-in-time foot (scene, prep,
        roster, encounter, clocks, NPCs, music) sits last so it reads as "now",
        immediately before the player's live message (which ``run_turn`` appends after
        this). The live player message, if already logged, is held back from the tail
        so it lands after the foot rather than inside the history.

        The log tail is sized from measurement, not an estimate: the measured head +
        foot length plus the system prompt and tool-schema sizes is subtracted from the
        total budget. The live turn passes ``system_tokens`` and ``tool_tokens``; other
        callers leave them None and they are measured here from the full toolset. So a
        large roster, canon, prep block, or toolset shrinks the tail to fit rather than
        pushing the prompt past budget.
        """
        budget = ContextBudget.from_settings(self.settings, self.models.get(self.settings.model))
        head_text, foot_text, summary = self._assemble_context(budget)
        after_seq = int(summary.get("through_seq", 0))
        if system_tokens is None:
            system_tokens = est_tokens("\n".join(block.text for block in self.build_system()))
        if tool_tokens is None:
            tool_tokens = tool_schema_tokens(self.tools.defs())
        context_tokens = est_tokens(head_text) + est_tokens(foot_text)
        tail_budget = budget.tail_for(system_tokens + tool_tokens + context_tokens)
        # Hold the just-logged live player message back from the history; run_turn
        # appends it after the foot so current-state sits between history and message.
        entries = self.log.read_all()
        if entries and entries[-1].type == "user_message":
            entries = entries[:-1]
        history, tail_tokens = render_history(entries, tail_budget=tail_budget, after_seq=after_seq)
        # Cache breakpoints on the two byte-stable boundaries: the end of the head, and
        # the end of the replayed history (before the volatile foot). Between compactions
        # the head+history span is an exact prefix of the next turn, so a provider with
        # explicit prefix caching reads it back instead of re-processing the transcript.
        # The head breakpoint still holds when the oldest history is later evicted.
        head_msg = Message(role="user", content=[TextBlock(text=head_text)], cache=True)
        if history:
            history[-1].cache = True
        messages = [head_msg, *history]
        if foot_text:
            messages.append(Message(role="user", content=[TextBlock(text=foot_text)]))
        return messages, context_tokens + tail_tokens

    def _assemble_context(self, budget: ContextBudget) -> tuple[str, str, dict]:
        """The per-turn context, split into the stable head and the point-in-time
        foot (everything in the prompt except the system prompt, tool schemas, and the
        log tail), plus the loaded summary snapshot the tail needs for its after_seq
        cutoff. Shared by build_messages and non_tail_tokens so both measure the
        identical blocks."""
        summary = snapshots.load_json(self.campaign.summary_path) or {}
        scene = snapshots.load_json(self.campaign.scene_path)
        # The GM agent is always behind the screen, so it sees hidden (GM-only)
        # canon; the visibility flag gates only player-facing output (the recap).
        canon_open, _dropped = canon.render_open(canon.load(self.campaign), include_hidden=True)
        head_text = build_context_head(
            self.meta,
            canon_open=canon_open or None,
            summary_md=summary.get("summary_md"),
            modules=self.campaign_arc_overview(),
        )
        foot_text = build_context_foot(
            self.meta,
            scene=scene,
            location_prep=self._location_prep(scene, budget.prep),
            roster=self.party_roster(),
            encounter=self.encounter_summary(),
            clocks=self.clocks_summary(),
            npcs=self.staged_npcs(scene),
            npc_recall=self.unstaged_scene_npcs(scene),
            music=self.music_status_line() if self.meta.music_enabled else None,
            settings=self.settings_summary(),
        )
        return head_text, foot_text, summary

    def non_tail_tokens(self) -> int:
        """Measured token size of everything in the assembled prompt except the log
        tail: system prompt + tool schemas + context head + foot. Lets the compaction
        trigger and the cost preview size the tail from the real prompt with no
        estimate."""
        budget = ContextBudget.from_settings(self.settings, self.models.get(self.settings.model))
        head_text, foot_text, _ = self._assemble_context(budget)
        system_tokens = est_tokens("\n".join(block.text for block in self.build_system()))
        tool_tokens = tool_schema_tokens(self.tools.defs())
        return system_tokens + tool_tokens + est_tokens(head_text) + est_tokens(foot_text)

    def _location_prep(self, scene: dict | None, prep_tokens: int) -> str | None:
        """Canonical text for the scene's keyed module location, pre-loaded into
        context so the GM narrates from the book without a mid-turn search. The
        full-body tier is bounded by ``prep_tokens`` (the context budget's prep
        slice, in tokens); the rest of the scene's paths become read_campaign
        pointers."""
        from openadventure.engine.prep import location_prep

        if not scene:
            return None
        return location_prep(
            self.workspace,
            scene.get("module_path"),
            scene.get("extra_paths"),
            char_budget=prep_tokens * 4,  # est_tokens is chars // 4
        )

    @staticmethod
    def _roster_line(sheet: Any) -> str:
        """One at-a-glance roster line: name, scalar fields, resources, conditions,
        carried items. Shared by the party roster and the companion roster."""
        scalars = ", ".join(f"{key} {value}" for key, value in sheet.scalar_fields())
        resources = ", ".join(f"{name} {r.current}/{r.max}" for name, r in sheet.resources.items())
        conditions = f" [{', '.join(sheet.conditions)}]" if sheet.conditions else ""
        items = f"; items: {', '.join(sheet.items)}" if sheet.items else ""
        descriptor = f" ({scalars})" if scalars else ""
        return f"- {sheet.name}{descriptor}, id {sheet.id}; {resources}{conditions}{items}"

    def party_roster(self) -> str | None:
        from openadventure.store.sheetstore import SheetStore

        party = SheetStore(self.campaign).party()
        if not party:
            return None
        return "\n".join(self._roster_line(sheet) for sheet in party)

    def companion_roster(self) -> str | None:
        """Roster lines for the NPCs traveling with the party (companions, mounts,
        animal companions), in the same format as the party roster. None if none.
        The GM already sees these every turn via the staged-NPC briefs; this is for
        player-facing display (e.g. the /party command)."""
        from openadventure.store.sheetstore import SheetStore

        companions = SheetStore(self.campaign).companions()
        if not companions:
            return None
        return "\n".join(self._roster_line(sheet) for sheet in companions)

    def _module_section_paths(self, slug: str) -> str:
        """The module's sections as an outline the GM reads against: one
        '- <slug>/<section>: <breadcrumb>' line per section, in the source's own
        reading order so each handout or read-aloud box sits right beneath the
        section it belongs to. (The old alphabetical file listing scattered children
        away from their parents and mislabeled itself "in order", so a handout could
        land far from the location whose text it completes.) The breadcrumb, minus
        the redundant module-title root, carries the hierarchy. Falls back to a bare
        filename listing only if the index can't be read."""
        from openadventure.ingest import indexer, pipeline

        module_dir = self.workspace.book_dir(slug)
        if not pipeline.is_ingested(module_dir):
            return ""
        rows = indexer.sections_in_reading_order(module_dir / indexer.INDEX_NAME)
        if not rows:
            sections_dir = module_dir / "sections"
            files = sorted(
                sections_dir.rglob("*.md"),
                key=lambda path: _natural_path_key(path.relative_to(sections_dir)),
            )
            rows = [(path.relative_to(sections_dir).as_posix(), "") for path in files]
        shown = rows[:MAX_MODULE_SECTIONS_IN_CONTEXT]
        lines = []
        for path, breadcrumb in shown:
            crumb = breadcrumb.split(" > ", 1)[1] if " > " in breadcrumb else ""
            lines.append(f"- {slug}/{path}: {crumb}" if crumb else f"- {slug}/{path}")
        if len(rows) > len(shown):
            lines.append(f"- …plus {len(rows) - len(shown)} more")
        return "\n".join(lines)

    def campaign_arc_overview(self, *, include_section_paths: bool = True) -> str | None:
        """The campaign arc + every module's status, but section paths for the
        ACTIVE module only; that keeps unreached modules from leaking into the
        narration and keeps the context block small as modules accrue.

        Set ``include_section_paths`` to False for player-facing display (e.g. the
        resume recap), where the section filenames are noise and can spoil what's
        coming."""
        from openadventure.ingest import pipeline

        ingested = {
            m.slug
            for m in self.meta.modules
            if pipeline.is_ingested(self.workspace.book_dir(m.slug))
        }
        modules = [m for m in self.meta.modules if m.slug in ingested]
        if not modules:
            return None
        markers = {"completed": "done", "active": "NOW PLAYING", "pending": "upcoming"}
        lines = []
        if self.meta.arc:
            lines.append(f"Arc: {self.meta.arc}")
        lines.append("Modules in order:")
        for module in modules:
            marker = markers.get(module.status, module.status)
            role = f": {module.role}" if module.role else ""
            lines.append(f"- {module.title} ({module.slug}) [{marker}]{role}")
        active = self.campaign.active_module(self.meta)
        if include_section_paths and active is not None and active.slug in ingested:
            section_paths = self._module_section_paths(active.slug)
            lines.append("")
            if section_paths:
                lines.append(
                    f"Now running '{active.slug}': your canonical source this module. "
                    "Its sections in reading order, as exact read_campaign paths each with "
                    "its breadcrumb; a deeper breadcrumb (a handout or read-aloud box) belongs "
                    "with the section it sits beneath, so read those together:"
                )
                lines.append(section_paths)
            else:
                lines.append(f"Now running '{active.slug}'.")
        return "\n".join(lines)

    def scene_summary(self) -> str | None:
        """Rendering of the current scene for the /scene command. Player-facing in
        GM mode; in assistant mode (no GM screen to keep) it shows the full
        snapshot, GM-only working state included. None when no scene has been set
        yet, or it holds nothing to show."""
        from openadventure.engine.tools.scene_tools import render_scene

        scene = snapshots.load_json(self.campaign.scene_path)
        if not scene:
            return None
        return render_scene(scene, full=self.meta.mode == "assistant") or None

    def encounter_summary(self) -> str | None:
        from openadventure.engine.tools.encounter_tools import load_encounter, render_encounter

        encounter = load_encounter(self.tool_ctx)
        if encounter is None or encounter.status != "active":
            return None
        return render_encounter(self.tool_ctx, encounter)

    def clocks_summary(self) -> str | None:
        from openadventure.engine.tools.clock_tools import load_clocks, render_clocks

        return render_clocks(load_clocks(self.tool_ctx)) or None

    @staticmethod
    def _npc_brief(sheet: Any) -> str:
        """One on-stage NPC line: name + the sheet's scalar fields, with any
        ``secret`` pulled onto its own indented line so it reads as GM-only.

        The inline fields come from ``scalar_fields()`` rather than a fixed list,
        so the brief adapts to whatever the template or GM conventions name them
        (the GM is steered toward role/attitude/goal/bond, but nothing here
        depends on those exact keys). ``secret`` is the one recognized key,
        because it needs hidden, GM-only rendering, not just a label."""
        bits = [f"{key}: {value}" for key, value in sheet.scalar_fields() if key != "secret"]
        if sheet.conditions:
            bits.append(f"[{', '.join(sheet.conditions)}]")
        tag = ", with the party" if getattr(sheet, "companion", False) else ""
        head = f"- {sheet.name} (id {sheet.id}{tag})"
        if bits:
            head += ": " + "; ".join(bits)
        fields = sheet.fields if isinstance(sheet.fields, dict) else {}
        secret = fields.get("secret")
        if secret:
            head += f"\n    secret (GM-only, reveal through play): {secret}"
        return head

    def staged_npcs(self, scene: dict | None) -> str | None:
        """Briefs for the NPCs in front of the GM this turn, pulled from each
        sheet's structured fields so recurring NPCs keep a consistent personality and
        motive. Companions (NPCs traveling with the party) come first and are
        always present regardless of scene; the scene's ``npcs_present`` adds the
        NPCs on stage at this location."""
        from openadventure.store.sheetstore import SheetStore

        store = SheetStore(self.campaign)
        sheets = list(store.companions())
        seen = {sheet.id for sheet in sheets}
        scene_ids = scene.get("npcs_present") if scene else None
        if isinstance(scene_ids, list):
            for sheet_id in scene_ids:
                if isinstance(sheet_id, str) and sheet_id not in seen:
                    sheet = store.load(sheet_id)
                    if sheet is not None:
                        sheets.append(sheet)
                        seen.add(sheet_id)
        if not sheets:
            return None
        lines = [self._npc_brief(sheet) for sheet in sheets]
        return "\n".join(lines) if lines else None

    # Honorifics and bare articles dropped before matching a sheet name against scene
    # text, so "Mr. Dooley" matches on "Dooley" and "The Watcher" on "Watcher".
    _NAME_NOISE = frozenset(
        {
            "mr",
            "mrs",
            "ms",
            "dr",
            "miss",
            "sir",
            "lord",
            "lady",
            "captain",
            "father",
            "brother",
            "sister",
            "madam",
            "madame",
            "master",
            "the",
            "a",
            "an",
            "of",
        }
    )

    @classmethod
    def _name_in_text(cls, name: str, text_cf: str) -> bool:
        """Whether a meaningful token of ``name`` appears as a whole word in the
        already-casefolded ``text_cf``. Honorifics, articles, and tokens shorter
        than three letters are ignored so a match needs a real name word."""
        tokens = [
            t
            for t in re.split(r"[^\w]+", name.casefold())
            if len(t) >= 3 and t not in cls._NAME_NOISE
        ]
        return any(re.search(rf"\b{re.escape(token)}\b", text_cf) for token in tokens)

    def _last_narration(self) -> str:
        """The most recent GM narration text in the log, or '' if none yet. The
        scene-drift backstop scans it because the GM may narrate a move or a
        returning NPC in prose and leave the scene snapshot stale."""
        for entry in reversed(self.log.tail(20)):
            if entry.type == "gm_message":
                return entry.data.get("text", "") or ""
        return ""

    def unstaged_scene_npcs(self, scene: dict | None) -> str | None:
        """Hint listing active NPC sheets whose name appears in the current scene's
        location/description OR in the GM's last narration, but who are not staged
        in ``npcs_present`` (companions, which ride in context anyway, are skipped).

        After compaction an NPC's sheet id is usually not in context, so the GM
        tends to narrate a returning NPC by name without restaging them, dropping
        their brief and secret. The scene text alone misses this when the GM let
        the scene go stale (the name is in the narration, not the snapshot), so we
        scan the last narration too: that catches the drift on the turn after it
        happens and surfaces the id for the next ``update_scene`` without a search."""
        sources: list[str] = []
        if scene:
            sources.append(scene.get("location") or "")
            sources.append(scene.get("description") or "")
        sources.append(self._last_narration())
        text_cf = " ".join(sources).casefold()
        if not text_cf.strip():
            return None
        from openadventure.store.sheetstore import SheetStore

        staged = set(scene.get("npcs_present") or []) if scene else set()
        hits = [
            sheet
            for sheet in SheetStore(self.campaign).list(kind="npc")
            if sheet.status == "active"
            and not sheet.companion
            and sheet.id not in staged
            and self._name_in_text(sheet.name, text_cf)
        ]
        if not hits:
            return None
        return "\n".join(f"- {sheet.name} (id {sheet.id})" for sheet in hits)

    def npcs_referenced_unstaged(self, lookup_text: str, get_sheet_ids: list[str]) -> list[Any]:
        """Active, non-companion NPC sheets the GM engaged with this round (fetched
        by id via get_sheet, or named in a search_sheets query) that are NOT staged
        in the current scene. Drives the mid-round scene-drift nudge: when the GM
        recalls a character but neither stages them nor moves the scene, surface
        them so a present NPC is not left out of context. The signal is the GM's own
        tool calls, not text heuristics; a pure lore recall (the NPC is not actually
        here) is handled by phrasing the nudge conditionally at the call site."""
        from openadventure.store.sheetstore import SheetStore

        scene = snapshots.load_json(self.campaign.scene_path) or {}
        staged = set(scene.get("npcs_present") or [])
        text_cf = lookup_text.casefold()
        ids = {i for i in get_sheet_ids if i}
        return [
            sheet
            for sheet in SheetStore(self.campaign).list(kind="npc")
            if sheet.status == "active"
            and not sheet.companion
            and sheet.id not in staged
            and (sheet.id in ids or self._name_in_text(sheet.name, text_cf))
        ]

    # --- settings ---------------------------------------------------------
    def set_override(self, key: str, value: Any) -> GenerationSettings:
        """Persist one settings override on the campaign and apply it now."""
        if key not in SETTING_KEYS:
            raise ValueError(f"unknown setting {key!r}")
        if key == "effort":
            value = Effort(value).value
        elif key == "verbosity":
            value = Verbosity(value).value
        elif key in ("max_tokens", "context_budget"):
            value = int(value)
        elif key == "thinking":
            value = bool(value)
        self.meta.settings[key] = value
        self.campaign.save_meta(self.meta)
        self.settings = resolve_settings(self.meta.settings, self.config, self.models)
        return self.settings

    # --- usage / cost -------------------------------------------------------
    @staticmethod
    def _empty_usage_data() -> dict[str, Any]:
        return empty_usage_data()

    def _usage_data(self) -> dict[str, Any]:
        """Load usage.json and upgrade its old, token-only shape in memory."""

        return normalize_usage_data(snapshots.load_json(self.campaign.usage_path))

    @staticmethod
    def _add_cost_breakdown(current: dict[str, float], delta: dict[str, float]) -> dict[str, float]:
        updated = normalized_cost_breakdown(current)
        for key in _COST_COMPONENTS:
            updated[key] = round(updated[key] + float(delta.get(key, 0.0) or 0.0), 6)
        updated["total"] = round(sum(updated[key] for key in _COST_COMPONENTS), 6)
        return updated

    def _accrue(
        self,
        usage: Usage,
        *,
        cost_delta: dict[str, float],
        model_id: str | None = None,
    ) -> None:
        """Persist one completed unit of usage and update this process's view."""

        self.session_usage = self.session_usage.add(usage)
        self.session_cost_breakdown = self._add_cost_breakdown(
            self.session_cost_breakdown, cost_delta
        )
        self.session_cost_usd = self.session_cost_breakdown["total"]

        data = self._usage_data()
        data["totals"] = Usage.model_validate(data["totals"]).add(usage).model_dump()
        data["cost_breakdown"] = self._add_cost_breakdown(data["cost_breakdown"], cost_delta)
        data["cost_usd"] = data["cost_breakdown"]["total"]

        if model_id is not None:
            per = data["by_model"].setdefault(model_id, {**Usage().model_dump(), "cost_usd": 0.0})
            merged = Usage.model_validate(per).add(usage)
            per.update(merged.model_dump())
            per["cost_usd"] = round(per.get("cost_usd", 0.0) + cost_delta["text"], 6)
        snapshots.save_json(self.campaign.usage_path, data)

    def accrue_usage(self, usage: Usage) -> None:
        """Accrue one completed model response, with thinking already included."""

        model = self.models.get(self.settings.model)
        self._accrue(
            usage,
            cost_delta={"text": estimate_cost(usage, model)},
            model_id=self.settings.model,
        )

    def accrue_media_usage(
        self,
        usage: Usage,
        kind: str,
        backend_name: str,
        model_id: str | None,
    ) -> None:
        """Accrue a successful media operation from a background task callback."""

        component = _MEDIA_COMPONENTS.get(kind)
        cost = estimate_media_cost(
            usage,
            kind=kind,
            backend_name=backend_name,
            model_id=model_id,
        )
        self._accrue(usage, cost_delta={component: cost} if component else {})

    def usage_report(self) -> dict[str, Any]:
        """Campaign and process usage, including estimated media and cost detail."""

        data = self._usage_data()
        data["session"] = self.session_usage.model_dump()
        data["session_cost_usd"] = self.session_cost_usd
        data["session_cost_breakdown"] = self.session_cost_breakdown
        # Additive aliases for consumers that want an explicit reminder that
        # media rates and provider thinking splits are estimates. The legacy
        # totals/cost_usd/session fields remain the canonical stable surface.
        data["estimated"] = {
            **data["totals"],
            "cost_usd": data["cost_usd"],
            "cost_breakdown": data["cost_breakdown"],
        }
        data["estimated_cost_usd"] = data["cost_usd"]
        return data
