"""GM tool registry and tool implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from openadventure.engine.tools.registry import ToolRegistry
from openadventure.providers.base import Usage

if TYPE_CHECKING:
    from openadventure.ingest.embeddings import EmbeddingBackend
    from openadventure.media.host import MediaCapabilities
    from openadventure.store.workspace import Campaign, CampaignMeta, Workspace


def build_registry(
    workspace: Workspace,
    campaign: Campaign,
    meta: CampaignMeta,
    media_backends: tuple | None = None,
    embed_backend: EmbeddingBackend | None = None,
    capabilities: MediaCapabilities | None = None,
    docs: dict[str, str] | None = None,
    usage_recorder: Callable[[Usage, str, str, str | None], None] | None = None,
) -> ToolRegistry:
    """Assemble the toolset for a campaign. Tools register conditionally:
    rules tools need an ingested source, campaign tools need module docs,
    ambience tools need a configured media backend whose surface the frontend
    can present. ``capabilities`` (None means all surfaces, the back-compat
    default) gates the media tools: a frontend that can't play audio never sees
    the speech/music/sfx tools, so nothing is generated for them. ``embed_backend``
    enables hybrid (semantic + keyword) search; None means FTS5-only. ``docs`` is
    the frontend's assembled self-knowledge (README + its --help and slash
    commands) for read_docs; None falls back to the README alone."""
    from openadventure.engine.self_knowledge import build_docs, make_read_docs_tool
    from openadventure.engine.tools.ambience_tools import make_ambience_tools
    from openadventure.engine.tools.clock_tools import CLOCK_TOOLS
    from openadventure.engine.tools.dice_tools import ROLL_DICE
    from openadventure.engine.tools.encounter_tools import ENCOUNTER_TOOLS
    from openadventure.engine.tools.narration_tools import (
        CAST_LOOKUP,
        PLAY_DIALOGUE,
        STAGE_DIALOGUE,
    )
    from openadventure.engine.tools.rules_tools import make_campaign_tools, make_rules_tools
    from openadventure.engine.tools.scene_tools import SCENE_TOOLS
    from openadventure.engine.tools.sheet_tools import SHEET_TOOLS
    from openadventure.engine.tools.table_tools import TABLE_TOOLS
    from openadventure.ingest import pipeline
    from openadventure.media.host import MediaCapabilities

    registry = ToolRegistry()
    registry.register(ROLL_DICE)
    # Self-knowledge: how OpenAdventure itself works, fetched only when a player
    # asks out of character. Always available (README at minimum).
    registry.register(make_read_docs_tool(docs if docs is not None else build_docs()))
    for tool in SHEET_TOOLS:
        registry.register(tool)
    for tool in ENCOUNTER_TOOLS:
        registry.register(tool)
    for tool in SCENE_TOOLS:
        registry.register(tool)
    for tool in CLOCK_TOOLS:
        registry.register(tool)
    for tool in TABLE_TOOLS:
        registry.register(tool)

    caps = capabilities or MediaCapabilities.all()
    images, music, tts, sound_effects = media_backends or (None, None, None, None)
    # A media tool needs three things: the campaign toggle on, a backend
    # configured, and a frontend that can actually present that surface (caps).
    # play_* tools fire immediately and are available in every mode; stage_*
    # tools queue cues synced to the final visible narration, which only exists
    # in GM mode.
    if meta.tts_enabled and tts is not None and caps.speech:
        registry.register(PLAY_DIALOGUE)
        registry.register(CAST_LOOKUP)
        if meta.mode == "gm":
            registry.register(STAGE_DIALOGUE)
    if not (meta.images_enabled and caps.images):
        images = None
    if not (meta.sound_effects_enabled and caps.sound_effects):
        sound_effects = None
    if not (meta.music_enabled and caps.music):
        music = None
    for tool in make_ambience_tools(images, music, sound_effects, usage_recorder=usage_recorder):
        if tool.name == "stage_sound_effect" and meta.mode != "gm":
            continue
        registry.register(tool)

    source_dirs = [workspace.book_dir(s) for s in meta.sources]
    if any(pipeline.is_ingested(d) for d in source_dirs):
        for tool in make_rules_tools(source_dirs, embed_backend):
            registry.register(tool)

    module_dirs = [workspace.book_dir(m.slug) for m in meta.modules]
    if any(pipeline.is_ingested(d) for d in module_dirs):
        for tool in make_campaign_tools(module_dirs, embed_backend):
            registry.register(tool)
        # advancing the arc only makes sense once there's more than one module
        if len(meta.modules) > 1:
            from openadventure.engine.tools.module_tools import COMPLETE_MODULE

            registry.register(COMPLETE_MODULE)

    return registry
