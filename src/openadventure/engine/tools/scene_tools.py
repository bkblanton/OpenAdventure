"""Scene state + the GM's canon notebook."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from openadventure.engine.events import StateChanged
from openadventure.engine.tools.registry import Tool, ToolContext, ToolOutcome
from openadventure.store import canon, snapshots
from openadventure.store.workspace import slugify

CanonCategory = Literal["threads", "seeds", "promises", "rulings", "world"]
NAVIGATION_KEYS = ("module_path", "extra_paths", "obvious_exits", "unresolved_options")
# Scene-local keys that should not survive a move to a new location.
SCENE_RESET_KEYS = (*NAVIGATION_KEYS, "npcs_present", "prep_notes", "hidden_notes")


class UpdateSceneArgs(BaseModel):
    location: str | None = Field(default=None, description="Where the party is now")
    description: str | None = Field(default=None, description="One-line scene description")
    time: str | None = Field(default=None, description="In-world time, e.g. 'dusk, day 3'")
    module_path: str | None = Field(
        default=None,
        description=(
            "Exact read_campaign section path for the current keyed module location, when known"
        ),
    )
    extra_paths: list[str] | None = Field(
        default=None,
        description=(
            "Additional read_campaign section paths relevant to this location, in priority "
            "order, for a location the module spreads across several sections. The closest few "
            "are stitched into context in full alongside module_path; list as many more as are "
            "relevant. They appear as read_campaign pointers you can pull on demand. Clears "
            "when the party moves to a new location."
        ),
    )
    obvious_exits: list[str] | None = Field(
        default=None,
        description=(
            "Player-visible exits or route choices from the current location. "
            "Do not include hidden exits unless discovered."
        ),
    )
    unresolved_options: list[str] | None = Field(
        default=None,
        description=(
            "Nearby player-visible rooms, doors, objects, or route choices the party has "
            "not resolved yet. Do not include secrets."
        ),
    )
    npcs_present: list[str] | None = Field(
        default=None,
        description=(
            "Sheet ids of the NPCs on stage in this scene. Their goal, bond, attitude, and "
            "secret are surfaced in the campaign context so you voice them consistently. "
            "Clears automatically when the party moves to a new location."
        ),
    )
    flags: dict[str, Any] | None = Field(
        default=None, description="Free-form scene flags, e.g. {'fog': true} (merged in)"
    )
    prep_notes: str | None = Field(
        default=None,
        description=(
            "Your own working notes for THIS location, kept in context every turn and cleared "
            "when the party moves. Use it for messy module data the auto-prep can't capture: a "
            "table that parsed badly and you reconstructed, a stat block whose cross-reference "
            "didn't resolve, details scattered across sections. Replaces the previous notes, so "
            "include everything still relevant. For durable campaign facts use note_canon instead."
        ),
    )
    hidden_notes: str | None = Field(
        default=None,
        description=(
            "GM-only secrets for THIS location that the players do not know yet: a hidden door "
            "or trap, an ambush lying in wait, treasure stashed out of sight, an NPC's concealed "
            "agenda in this scene. Kept in your context every turn so you don't forget to play "
            "them, but never shown to the table (the /scene command hides them) and never stated "
            "outright: reveal them through play. Replaces the previous notes, so include "
            "everything still relevant. Cleared when the party moves on, so use note_canon with "
            "visibility='hidden' for a secret that outlives this location (a twist, a villain's "
            "plan); use this for the location-scoped ones."
        ),
    )


def render_scene(scene: dict, *, full: bool = False) -> str:
    """Summary of the current scene snapshot, for the /scene command.

    By default this is player-facing: it leaves out the GM-only working state
    (module/section paths, prep notes, and the staged-NPC ids) and only shows
    what the players can see (where they are, the time, the one-line
    description, visible exits and options, scene flags). Pass ``full=True`` to
    also include that GM-only working state, for assistant mode where there is no
    screen to keep between the GM and the player. ``hidden_notes`` (location
    secrets) are never rendered in either mode: this is a display command, so
    secrets stay out of it and ride only in the GM's own context. Returns ''
    when the scene holds nothing to show."""
    lines: list[str] = []
    location = scene.get("location")
    time = scene.get("time")
    if location and time:
        lines.append(f"{location} ({time})")
    elif location or time:
        lines.append(location or time)
    if scene.get("description"):
        lines.append(scene["description"])
    for key, label in (("obvious_exits", "Exits"), ("unresolved_options", "Nearby")):
        values = scene.get(key)
        if values:
            lines.append(f"{label}: {', '.join(values)}")
    flags = scene.get("flags")
    if flags:
        rendered = ", ".join(f"{name}: {value}" for name, value in flags.items())
        lines.append(f"Flags: {rendered}")
    if full:
        npcs = scene.get("npcs_present")
        if npcs:
            lines.append(f"NPCs present: {', '.join(npcs)}")
        if scene.get("module_path"):
            lines.append(f"Module path: {scene['module_path']}")
        extra = scene.get("extra_paths")
        if extra:
            lines.append(f"Extra paths: {', '.join(extra)}")
        if scene.get("prep_notes"):
            lines.append(f"Prep notes: {scene['prep_notes']}")
    return "\n".join(lines)


def _update_scene(ctx: ToolContext, args: UpdateSceneArgs) -> ToolOutcome:
    scene = snapshots.load_json(ctx.campaign.scene_path) or {}
    location_changed = args.location is not None and args.location != scene.get("location")
    module_changed = args.module_path is not None and args.module_path != scene.get("module_path")
    if location_changed or module_changed:
        for key in SCENE_RESET_KEYS:
            if getattr(args, key) is None:
                scene.pop(key, None)
    for key in (
        "location",
        "description",
        "time",
        "module_path",
        "extra_paths",
        "obvious_exits",
        "unresolved_options",
        "npcs_present",
        "prep_notes",
        "hidden_notes",
    ):
        value = getattr(args, key)
        if value is not None:
            scene[key] = value
    if args.flags:
        scene.setdefault("flags", {}).update(args.flags)
    snapshots.save_json(ctx.campaign.scene_path, scene)
    summary = f"scene: {scene.get('location', '?')}" + (
        f", {scene.get('time')}" if scene.get("time") else ""
    )
    ctx.log.append("state_change", {"kind": "scene", "ref": "scene", "summary": summary})
    return ToolOutcome(
        content=json.dumps(scene, ensure_ascii=False),
        events=[StateChanged(kind="scene", ref="scene", summary=summary)],
        summary=summary,
    )


class NoteCanonArgs(BaseModel):
    text: str = Field(description="the fact to record, in one concrete line")
    category: CanonCategory = Field(
        default="world",
        description="threads (open plot), seeds (foreshadowing), promises (debts/oaths), "
        "rulings, world (lore/facts)",
    )
    id: str | None = Field(
        default=None, description="an existing canon [id] to update; omit to create a new entry"
    )
    visibility: Literal["open", "hidden"] | None = Field(
        default=None, description="'hidden' for a GM-only secret the players do not know"
    )
    priority: Literal["normal", "major"] | None = Field(
        default=None, description="'major' to pin the campaign spine so it is never dropped"
    )
    status: str | None = Field(
        default=None, description="set 'resolved'/'paid'/'lost'/'dropped' to close an entry"
    )


def _unique_canon_id(current: canon.Canon, text: str, seq: int) -> str:
    base = slugify(text)[:40] or "note"
    return base if current.find(base) is None else f"{base}-{seq}"


def _note_canon(ctx: ToolContext, args: NoteCanonArgs) -> ToolOutcome:
    """Write or update one canon entry directly. For deliberate GM intent the
    chronicler cannot infer from the transcript: secrets, planted setups, rulings.
    The everyday 'remember what happened' case is handled automatically by the
    background chronicler, so reach for this only when you mean to."""
    current = canon.load(ctx.campaign)
    entry_id = args.id or _unique_canon_id(current, args.text, ctx.log.last_seq)
    closing = bool(args.status) and args.status in canon.CLOSED_STATUSES
    if closing:
        op: dict[str, Any] = {"op": "resolve", "id": entry_id, "status": args.status}
    else:
        op = {"op": "add", "id": entry_id, "category": args.category, "text": args.text}
        if args.status:
            op["status"] = args.status
    if args.visibility:
        op["visibility"] = args.visibility
    if args.priority:
        op["priority"] = args.priority

    updated, _warnings = canon.apply_ops(current, [op], at_seq=ctx.log.last_seq)
    canon.save(ctx.campaign, updated)

    hidden = args.visibility == "hidden"
    verb = "closed" if closing else "noted"
    summary = f"canon {verb} ({args.category}): {args.text[:60]}"
    public_summary = "canon (GM-only)" if hidden else summary
    event_summary = public_summary if hidden and ctx.meta.mode == "gm" else summary
    return ToolOutcome(
        content=f"Recorded to canon [{entry_id}].",
        events=[StateChanged(kind="canon", ref=entry_id, summary=event_summary, private=hidden)],
        summary=summary,
        private=hidden,
        public_summary=public_summary,
        public_args_summary="visibility='hidden'" if hidden else "",
    )


class SearchCanonArgs(BaseModel):
    query: str | None = Field(
        default=None, description="case-insensitive substring; omit to list everything"
    )


def _search_canon(ctx: ToolContext, args: SearchCanonArgs) -> ToolOutcome:
    """Search the full canon, including closed and archived entries, so a
    long-resolved thread, an old promise, or a planted seed can be recalled."""
    rendered = canon.render_full(canon.load(ctx.campaign), include_hidden=True, query=args.query)
    if not rendered:
        return ToolOutcome(content="No matching canon.", summary="0 canon entries")
    return ToolOutcome(content=rendered, summary="canon search")


SCENE_TOOLS = [
    Tool(
        name="update_scene",
        description=(
            "Update the current scene snapshot (location, time, module_path, extra_paths, "
            "visible exits, unresolved options, one-line description, flags, prep_notes). Keep "
            "it current; it anchors the campaign context every turn."
        ),
        args_model=UpdateSceneArgs,
        handler=_update_scene,
    ),
    Tool(
        name="note_canon",
        description=(
            "Record a durable fact to canon DELIBERATELY: a GM secret, a planted setup, a "
            "ruling, or a fact you decided that has not happened in the fiction yet. Everyday "
            "'what happened' is captured automatically, so use this for intent the game has "
            "not shown. Pass an existing [id] to update it, or status to close it."
        ),
        args_model=NoteCanonArgs,
        handler=_note_canon,
    ),
    Tool(
        name="search_canon",
        description=(
            "Search the campaign canon (open and closed entries) to recall a thread, "
            "promise, ruling, or world fact by keyword."
        ),
        args_model=SearchCanonArgs,
        handler=_search_canon,
        read_only=True,
    ),
]
