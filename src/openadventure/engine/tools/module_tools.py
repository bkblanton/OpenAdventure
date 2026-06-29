"""Advancing through a campaign's arc of adventure modules.

The party, their sheets, the rolling story summary, and notes are all
campaign-wide and carry across modules untouched; only the keyed adventure
content (locations, NPCs, read-aloud text) is per-module. ``complete_module``
just moves the "now playing" pointer and records the handoff."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from openadventure.engine.events import ModuleTransition
from openadventure.engine.tools.registry import Tool, ToolContext, ToolOutcome


class CompleteModuleArgs(BaseModel):
    next_module: str | None = Field(
        default=None,
        description=(
            "Slug of the module to start next. Omit to advance to the next unfinished module "
            "in arc order."
        ),
    )
    handoff_note: str | None = Field(
        default=None,
        description=(
            "Short summary of how this module concluded and what carries into the next, "
            "recorded as a durable quest note so the through-line survives compaction."
        ),
    )


def _complete_module(ctx: ToolContext, args: CompleteModuleArgs) -> ToolOutcome:
    meta = ctx.meta
    campaign = ctx.campaign
    current = campaign.active_module(meta)
    if current is None:
        return ToolOutcome(
            content="No module is currently in play, so there is nothing to complete.",
            summary="no active module",
            ok=False,
        )

    if args.next_module is not None:
        nxt = next((m for m in meta.modules if m.slug == args.next_module), None)
        if nxt is None:
            known = ", ".join(m.slug for m in meta.modules) or "(none)"
            return ToolOutcome(
                content=f"No module named {args.next_module!r}. Known modules: {known}.",
                summary="unknown module",
                ok=False,
            )
    else:
        nxt = next(
            (m for m in meta.modules if m.status != "completed" and m.slug != current.slug),
            None,
        )

    current.status = "completed"
    if nxt is not None:
        nxt.status = "active"
        meta.active_module = nxt.slug
    else:
        meta.active_module = None
    campaign.save_meta(meta)

    if args.handoff_note and args.handoff_note.strip():
        note_path = campaign.notes_dir / "quest.jsonl"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        text = f"[module handoff: {current.title}] {args.handoff_note.strip()}"
        entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "text": text}
        with open(note_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    ctx.log.append(
        "module_transition",
        {
            "completed": current.slug,
            "active": meta.active_module,
            "handoff_note": (args.handoff_note or "").strip() or None,
        },
    )

    if nxt is not None:
        summary = f"module: {current.slug} done → {nxt.slug}"
        content = (
            f"Module '{current.title}' marked complete. Now playing '{nxt.title}'. "
            "search_campaign/read_campaign now resolve against it; narrate the transition "
            "and call update_scene for the party's new location."
        )
    else:
        summary = f"module: {current.slug} done (arc complete)"
        content = (
            f"Module '{current.title}' marked complete. No further modules remain; the "
            "campaign arc is finished."
        )
    return ToolOutcome(
        content=content,
        events=[
            ModuleTransition(
                completed=current.slug,
                completed_title=current.title,
                active=(nxt.slug if nxt is not None else None),
                active_title=(nxt.title if nxt is not None else None),
            )
        ],
        summary=summary,
    )


COMPLETE_MODULE = Tool(
    name="complete_module",
    description=(
        "Mark the adventure module currently in play as complete and advance the campaign "
        "to the next module in the arc. Call this when the party has resolved this module's "
        "climax and is moving on to the next stage of the overarching story. The party, "
        "their sheets, notes, and the story so far all carry forward automatically."
    ),
    args_model=CompleteModuleArgs,
    handler=_complete_module,
)
