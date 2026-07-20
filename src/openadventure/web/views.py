"""Public JSON projections for the browser frontend.

Campaign files and raw engine events contain GM-only material.  The browser is
not a trusted redaction boundary because its network inspector can see payloads
that CSS merely hides.  Every response therefore goes through the projections
in this module before it crosses the API.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openadventure.character_import import IMPORT_PREFIX
from openadventure.config import AppConfig
from openadventure.engine.events import EngineEvent
from openadventure.engine.kickoff import CAMPAIGN_KICKOFF_PREFIX
from openadventure.engine.session import (
    GameSession,
    empty_cost_breakdown,
    normalize_usage_data,
    resolve_settings,
    resolve_utility_settings,
)
from openadventure.mechanics.clocks import ClockBoard
from openadventure.mechanics.encounter import Encounter
from openadventure.mechanics.sheets import Sheet
from openadventure.providers.base import ModelRegistry, Usage
from openadventure.store import snapshots
from openadventure.store.eventlog import EventLog, LogEntry
from openadventure.store.sheetstore import SheetStore
from openadventure.store.workspace import Campaign, CampaignMeta, Mode, Workspace

type CampaignSource = GameSession | Campaign
ImageUrl = Callable[[str], str | None]

_PUBLIC_SCENE_KEYS = (
    "location",
    "description",
    "time",
    "obvious_exits",
    "unresolved_options",
    "flags",
)
_BOOK_MANIFEST_KEYS = (
    "source",
    "title",
    "type",
    "section_count",
    "ingested_at",
    "pages",
    "warning",
    "image_only_pages",
)


def bootstrap_payload(config: AppConfig, workspace: Workspace | None = None) -> dict[str, Any]:
    """Return JSON-ready campaign and ingested-book metadata for the home screen."""

    workspace = workspace or Workspace(config.workspace_dir)
    return {
        "campaigns": [
            {
                **campaign_metadata(meta),
                "has_prior_play": any(
                    entry.type in ("user_message", "gm_message")
                    for entry in workspace.campaign(meta.slug).open_log().read_all()
                ),
            }
            for meta in workspace.list_campaigns()
        ],
        "books": [book_metadata(workspace, slug) for slug in workspace.list_books()],
        "models": model_catalog(),
        "utility_model": resolve_utility_settings(config).model,
    }


def book_metadata(workspace: Workspace, slug: str) -> dict[str, Any]:
    """Return the non-sensitive portion of one ingested book's manifest."""

    manifest = snapshots.load_json(workspace.book_dir(slug) / "manifest.json") or {}
    payload = {"slug": slug}
    payload.update({key: manifest[key] for key in _BOOK_MANIFEST_KEYS if key in manifest})
    payload.setdefault("type", workspace.book_type(slug))
    template = snapshots.load_json(workspace.book_dir(slug) / "templates" / "character.json")
    payload["template"] = {
        "ready": template is not None,
        "fields": len(template.get("fields", [])) if isinstance(template, dict) else 0,
        "resources": len(template.get("resources", [])) if isinstance(template, dict) else 0,
    }
    return payload


def model_catalog() -> list[dict[str, Any]]:
    """Models that the web settings and template pickers may offer."""

    return [
        {
            "id": model.id,
            "display_name": model.display_name,
            "provider": model.provider,
            "context_window": model.context_window,
            "max_output": model.max_output,
            "supports_effort": model.supports_effort,
            "supports_thinking": model.supports_thinking,
        }
        for model in ModelRegistry.load_default().visible
    ]


def campaign_metadata(meta: CampaignMeta) -> dict[str, Any]:
    """Stable campaign metadata used by both list and detail responses."""

    return meta.model_dump(mode="json")


def campaign_payload(
    source: CampaignSource,
    config: AppConfig | None = None,
) -> dict[str, Any]:
    """Return a complete initial payload for a campaign page.

    A cached ``GameSession`` gives the most accurate effective settings and
    connection status.  A plain ``Campaign`` is also accepted for read-only
    routes, but then ``config`` is required to resolve settings.
    """

    campaign, session = _campaign_and_session(source)
    meta = session.meta if session is not None else campaign.load_meta()
    if session is not None:
        settings = session.settings.model_dump(mode="json")
        provider = {
            "name": session.provider_name(),
            "model": session.settings.model,
            "connected": session.provider is not None,
        }
    else:
        if config is None:
            raise TypeError("config is required when campaign_payload receives a Campaign")
        models = ModelRegistry.load_default()
        resolved = resolve_settings(meta.settings, config, models)
        settings = resolved.model_dump(mode="json")
        provider = {
            "name": models.provider_for(resolved.model),
            "model": resolved.model,
            "connected": False,
        }
    usage = usage_payload(source)
    return {
        "campaign": campaign_metadata(meta),
        "settings": settings,
        "provider": provider,
        "media": media_payload(session) if session is not None else None,
        "history": public_history(source, mode=meta.mode),
        # Usage is a first-class browser payload rather than an accidental
        # sidebar detail. Keep it in the state snapshot too so streamed state
        # updates refresh the display after every completed operation.
        "usage": usage,
        "state": state_snapshot(source, mode=meta.mode, usage=usage),
    }


def media_payload(session: GameSession) -> dict[str, Any]:
    """Browser-safe capability, readiness, and per-campaign media settings."""

    capabilities = asdict(session.media_capabilities)

    def backend_status(backend: Any, fallback: str) -> dict[str, Any]:
        ready = backend is not None and bool(getattr(backend, "ready", True))
        hint = ""
        if backend is None:
            hint = f"No {fallback} backend configured."
        elif not ready:
            hint = str(getattr(backend, "configuration_hint", ""))
        return {"ready": ready, "hint": hint}

    return {
        "capabilities": capabilities,
        "backends": {
            "narration": backend_status(session.tts, "narration"),
            "sound_effects": backend_status(session.sound_effects, "sound effects"),
            "music": backend_status(session.music, "music"),
            "images": backend_status(session.images, "image"),
        },
        "enabled": {
            "narration": session.meta.tts_enabled,
            "sound_effects": session.meta.sound_effects_enabled,
            "music": session.meta.music_enabled,
            "images": session.meta.images_enabled,
        },
        "music_volume": session.media_host.music_volume(),
        "music_status": session.music_status_line(),
    }


def public_history(
    source: CampaignSource,
    *,
    mode: Mode | None = None,
) -> list[dict[str, Any]]:
    """Project the append-only log into the table-visible conversation.

    Tool calls and state-change rows are intentionally absent.  Their persisted
    summaries can contain module text and hidden clock details even when the live
    event was marked private.  Private rolls retain only the fact that a secret
    roll occurred.
    """

    campaign, session = _campaign_and_session(source)
    current_mode = mode or (session.meta.mode if session is not None else campaign.load_meta().mode)
    log = session.log if session is not None else EventLog(campaign.log_path)
    history: list[dict[str, Any]] = []
    for entry in log.read_all():
        projected = _history_entry(entry, current_mode)
        if projected is not None:
            history.append(projected)
    return history


def state_snapshot(
    source: CampaignSource,
    *,
    mode: Mode | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the browser sidebar's current, public campaign state."""

    campaign, session = _campaign_and_session(source)
    meta = session.meta if session is not None else campaign.load_meta()
    current_mode = mode or meta.mode
    store = SheetStore(campaign)
    party = [_sheet_payload(sheet, public_npc=False) for sheet in store.party()]
    companions = [
        _sheet_payload(sheet, public_npc=current_mode == "gm") for sheet in store.companions()
    ]

    scene = snapshots.load_json(campaign.scene_path)
    if scene is not None and current_mode == "gm":
        # A whitelist is safer than subtracting today's private fields because a
        # future GM-only scene key then defaults to staying server-side.
        scene = {key: scene[key] for key in _PUBLIC_SCENE_KEYS if key in scene}

    encounter = _encounter_payload(campaign)
    clocks_data = snapshots.load_json(campaign.clocks_path)
    board = ClockBoard.model_validate(clocks_data) if clocks_data is not None else ClockBoard()
    clocks = board.live()
    if current_mode == "gm":
        clocks = [clock for clock in clocks if clock.visible]

    usage = usage if usage is not None else usage_payload(source)
    return {
        "meta": campaign_metadata(meta),
        "campaign_kickoff_available": campaign_kickoff_available(source),
        "party": party,
        "companions": companions,
        "scene": scene,
        "encounter": encounter,
        "clocks": [clock.model_dump(mode="json") for clock in clocks],
        "usage": usage,
    }


def campaign_kickoff_available(source: CampaignSource) -> bool:
    """Whether the web table can still run its dedicated campaign opening.

    Character imports are preparation turns, so they do not consume the opening.
    Any ordinary player turn or an existing kickoff marker does.
    """

    campaign, session = _campaign_and_session(source)
    meta = session.meta if session is not None else campaign.load_meta()
    if meta.mode != "gm":
        return False
    log = session.log if session is not None else EventLog(campaign.log_path)
    for entry in log.read_all():
        if entry.type != "user_message":
            continue
        text = str(entry.data.get("text", "")).strip()
        if text.startswith(CAMPAIGN_KICKOFF_PREFIX):
            return False
        if not text.startswith(IMPORT_PREFIX):
            return False
    return True


def usage_payload(source: CampaignSource) -> dict[str, Any]:
    """Return the usage and rough-cost report safe to show in the browser."""

    campaign, session = _campaign_and_session(source)
    return session.usage_report() if session is not None else _stored_usage(campaign)


def sanitize_event(
    event: EngineEvent,
    *,
    mode: Mode,
    image_url: ImageUrl | None = None,
) -> dict[str, Any] | None:
    """Return a public event dictionary, or ``None`` for a hidden event.

    In GM mode no model chatter, tool payload, private roll result, or private
    state reference is sent to the client.  Image paths are converted by the
    caller's validated media-route callback; without one only the basename is
    retained, never an absolute filesystem path.
    """

    if mode == "gm" and event.type == "debug_chatter":
        return None
    payload = event.model_dump(mode="json")

    if event.type in ("image_generated", "show_image"):
        raw_path = str(payload.get("path", ""))
        payload["path"] = (
            image_url(raw_path) if image_url is not None else Path(raw_path.replace("\\", "/")).name
        )

    if mode == "gm" and event.type == "background_task_started":
        labels = {
            "image": "Generating an image",
            "music": "Composing music",
            "music-stop": "Stopping music",
            "sfx": "Preparing a sound effect",
            "tts": "Preparing narration",
            "compaction": "Updating the story summary",
        }
        payload["label"] = labels.get(str(payload.get("kind")), "Working in the background")
    elif mode == "gm" and event.type == "background_task_finished":
        payload["message"] = ""

    if mode != "gm":
        return payload

    if event.type == "tool_started":
        payload["args_summary"] = ""
    elif event.type == "tool_finished":
        # Even ostensibly public tools may name unreached module sections in
        # their arguments or result.  The browser gets the activity and status,
        # never the raw payload or summaries, matching the normal CLI view.
        payload["args"] = {}
        payload["result"] = ""
        payload["args_summary"] = ""
        payload["result_summary"] = ""
    elif event.type == "roll_result" and bool(payload.get("private")):
        return {"type": "roll_result", "private": True}
    elif event.type == "state_changed" and bool(payload.get("private")):
        return None
    return payload


def serialize_event(
    event: EngineEvent,
    *,
    mode: Mode,
    image_url: ImageUrl | None = None,
) -> str | None:
    """Encode one sanitized public event as compact JSON."""

    payload = sanitize_event(event, mode=mode, image_url=image_url)
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _campaign_and_session(source: CampaignSource) -> tuple[Campaign, GameSession | None]:
    if isinstance(source, GameSession):
        return source.campaign, source
    if isinstance(source, Campaign):
        return source, None
    raise TypeError(f"expected GameSession or Campaign, got {type(source).__name__}")


def _sheet_payload(sheet: Sheet, *, public_npc: bool) -> dict[str, Any]:
    payload = sheet.model_dump(mode="json")
    if public_npc:
        # Companion NPC working notes can include a secret or nested GM-only
        # structures.  Match the public roster surface: simple at-a-glance
        # fields, excluding the engine's recognized ``secret`` key.
        payload["fields"] = {
            key: value for key, value in sheet.scalar_fields() if key.casefold() != "secret"
        }
    return payload


def _encounter_payload(campaign: Campaign) -> dict[str, Any] | None:
    data = snapshots.load_json(campaign.encounter_path)
    if data is None:
        return None
    encounter = Encounter.model_validate(data)
    payload = encounter.model_dump(mode="json")
    store = SheetStore(campaign)
    for combatant, public in zip(encounter.combatants, payload["combatants"], strict=True):
        if not combatant.sheet_id:
            continue
        sheet = store.load(combatant.sheet_id)
        if sheet is None:
            continue
        public["sheet"] = {
            "id": sheet.id,
            "name": sheet.name,
            "kind": sheet.kind,
            "status": sheet.status,
            "resources": {
                name: resource.model_dump(mode="json") for name, resource in sheet.resources.items()
            },
            "conditions": list(sheet.conditions),
        }
    return payload


def _stored_usage(campaign: Campaign) -> dict[str, Any]:
    """Load and upgrade a usage report for a campaign without a live session."""

    data = normalize_usage_data(snapshots.load_json(campaign.usage_path))
    empty_session = Usage().model_dump(mode="json")
    empty_session_costs = empty_cost_breakdown()
    data["session"] = empty_session
    data["session_cost_usd"] = 0.0
    data["session_cost_breakdown"] = empty_session_costs
    data["estimated"] = {
        **data["totals"],
        "cost_usd": data["cost_usd"],
        "cost_breakdown": data["cost_breakdown"],
    }
    data["estimated_cost_usd"] = data["cost_usd"]
    return data


def _history_entry(entry: LogEntry, mode: Mode) -> dict[str, Any] | None:
    base = {"seq": entry.seq, "ts": entry.ts}
    if entry.type in ("user_message", "gm_message"):
        text = str(entry.data.get("text", "")).strip()
        if not text:
            return None
        # The one-time campaign opening instruction is a logged engine prompt,
        # not table dialogue. Preserve it for context and CLI recovery, but let
        # the browser transcript begin with the Game Master's welcome.
        if entry.type == "user_message" and text.startswith(
            (CAMPAIGN_KICKOFF_PREFIX, IMPORT_PREFIX)
        ):
            return None
        sudo = bool(entry.data.get("sudo"))
        if sudo and mode == "gm":
            text = "(out-of-character direction)"
        return {
            **base,
            "type": entry.type,
            "role": "user" if entry.type == "user_message" else "assistant",
            "text": text,
            **({"sudo": True} if sudo else {}),
        }
    if entry.type != "roll":
        return None
    if mode == "gm" and bool(entry.data.get("private")):
        return {**base, "type": "roll", "private": True}
    allowed = (
        "expression",
        "total",
        "detail",
        "reason",
        "private",
        "outcome",
        "max_rolls",
        "min_rolls",
        "by",
    )
    return {
        **base,
        "type": "roll",
        **{key: entry.data[key] for key in allowed if key in entry.data},
    }
