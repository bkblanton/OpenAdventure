"""Public JSON projections for the browser frontend.

Campaign files and raw engine events contain GM-only material.  The browser is
not a trusted redaction boundary because its network inspector can see payloads
that CSS merely hides.  Every response therefore goes through the projections
in this module before it crosses the API.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openadventure.config import AppConfig
from openadventure.engine.events import EngineEvent
from openadventure.engine.session import GameSession, resolve_settings
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
    }


def book_metadata(workspace: Workspace, slug: str) -> dict[str, Any]:
    """Return the non-sensitive portion of one ingested book's manifest."""

    manifest = snapshots.load_json(workspace.book_dir(slug) / "manifest.json") or {}
    payload = {"slug": slug}
    payload.update({key: manifest[key] for key in _BOOK_MANIFEST_KEYS if key in manifest})
    payload.setdefault("type", workspace.book_type(slug))
    return payload


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
            "connected": False,
        }
    return {
        "campaign": campaign_metadata(meta),
        "settings": settings,
        "provider": provider,
        "history": public_history(source, mode=meta.mode),
        "state": state_snapshot(source, mode=meta.mode),
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

    usage = session.usage_report() if session is not None else _stored_usage(campaign)
    return {
        "meta": campaign_metadata(meta),
        "party": party,
        "companions": companions,
        "scene": scene,
        "encounter": encounter,
        "clocks": [clock.model_dump(mode="json") for clock in clocks],
        "usage": usage,
    }


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
    return snapshots.load_json(campaign.usage_path) or {
        "totals": Usage().model_dump(mode="json"),
        "cost_usd": 0.0,
        "by_model": {},
    }


def _history_entry(entry: LogEntry, mode: Mode) -> dict[str, Any] | None:
    base = {"seq": entry.seq, "ts": entry.ts}
    if entry.type in ("user_message", "gm_message"):
        text = str(entry.data.get("text", "")).strip()
        if not text:
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
