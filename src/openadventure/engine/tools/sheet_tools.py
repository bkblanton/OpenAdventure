"""Sheet tools: the AI's only way to create and mutate character state."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from openadventure.engine.events import StateChanged
from openadventure.engine.tools.registry import Tool, ToolContext, ToolOutcome
from openadventure.engine.tools.search_render import render_hits
from openadventure.mechanics import sheets as sheets_mod
from openadventure.mechanics.sheets import Resource, Sheet, SheetError, SheetOp
from openadventure.store.sheetstore import SheetStore

# Field names that hold carried inventory. A derived template nests starting gear
# under a system-specific key (CoC's gear_possessions, D&D's
# equipment.weapons_and_gear), which the engine's inventory model and the party
# roster never see; _reconcile_inventory folds those into Sheet.items at creation.
_INVENTORY_KEY = re.compile(r"gear|possession|inventory|equipment|belonging|loadout|item", re.I)
_INVENTORY_SPLIT = re.compile(r"[;,\n]+")


def _split_inventory_text(text: str) -> list[str]:
    """Split a free-text inventory field ('a, b; c') into individual item labels. A
    derived template may declare carried gear as a text field rather than a list
    (CoC's gear_possessions is type 'text'), so without this its contents never reach
    Sheet.items. Strips list bullets and a trailing period; a single undelimited entry
    stays whole. Comma-splitting is a heuristic: prose with internal commas can split
    oddly, which is why a structured list field is preferred when a template has one."""
    items: list[str] = []
    for part in _INVENTORY_SPLIT.split(text):
        cleaned = part.strip().lstrip("-*•").strip().rstrip(".").strip()
        if cleaned:
            items.append(cleaned)
    return items


def _store(ctx: ToolContext) -> SheetStore:
    return SheetStore(ctx.campaign)


def _item_label(entry: Any) -> str | None:
    """One inventory line from a list entry: a bare string, or a dict's name/label."""
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict):
        for key in ("name", "item", "label", "description"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _reconcile_inventory(fields: dict[str, Any]) -> list[str]:
    """Move carried items out of template-nested inventory fields into the engine's
    canonical ``Sheet.items`` list (what the party roster shows and modify_inventory
    mutates). Without this, a freshly created non-D&D character carries gear the GM
    never sees, because create_sheet can only place it inside ``fields``.

    Recurses through ``fields``; for any key whose name looks like inventory, collects
    its contents (a list of strings/dicts, or a delimited free-text string) and empties
    it, so ``items`` is the single source of truth. The ``backstory`` subtree is left
    alone: a field like ``treasured_possessions`` is sentimental prose, not carried
    gear, and folding it would both mangle the text and clear the backstory. Combat-stat
    blocks like ``weapons`` (whose key does not match) are left intact. ``fields`` is
    mutated in place. Runs at the create_sheet chokepoint, so it covers every sheet
    regardless of how its template was derived (subcommand, on-the-fly, or none)."""
    collected: list[str] = []

    def walk(node: Any, *, in_backstory: bool) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                under = in_backstory or "backstory" in key.casefold()
                if not under and _INVENTORY_KEY.search(key) and isinstance(value, list):
                    collected.extend(label for entry in value if (label := _item_label(entry)))
                    value.clear()
                elif not under and _INVENTORY_KEY.search(key) and isinstance(value, str):
                    collected.extend(_split_inventory_text(value))
                    node[key] = ""
                else:
                    walk(value, in_backstory=under)
        elif isinstance(node, list):
            for entry in node:
                walk(entry, in_backstory=in_backstory)

    walk(fields, in_backstory=False)
    seen: set[str] = set()
    deduped: list[str] = []
    for item in collected:
        if item.casefold() not in seen:
            seen.add(item.casefold())
            deduped.append(item)
    return deduped


def _log_change(ctx: ToolContext, sheet: Sheet, summary: str) -> None:
    ctx.log.append("state_change", {"kind": "sheet", "ref": sheet.id, "summary": summary})


def _sheet_brief(sheet: Sheet) -> str:
    scalars = ", ".join(f"{k} {v}" for k, v in sheet.scalar_fields())
    detail = f" ({scalars})" if scalars else ""
    resources = ", ".join(f"{k} {v.current}/{v.max}" for k, v in sheet.resources.items())
    conditions = f" [{', '.join(sheet.conditions)}]" if sheet.conditions else ""
    items = f" items: {', '.join(sheet.items)}" if sheet.items else ""
    return (
        f"{sheet.id} ({sheet.kind}, {sheet.status}): "
        f"{sheet.name}{detail}, {resources}{conditions}{items}"
    )


# --- create_sheet ----------------------------------------------------------


class CreateSheetArgs(BaseModel):
    kind: Literal["pc", "npc", "monster"] = Field(description="pc = player character")
    name: str
    fields: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arbitrary structured data: class, species, level, abilities, skills, "
            "inventory, traits… Follow the campaign's character template for PCs."
        ),
    )
    resources: dict[str, dict[str, int]] = Field(
        default_factory=dict,
        description=(
            'Numeric pools, e.g. {"hp": {"current": 11, "max": 11}, '
            '"spell_slots_1": {"current": 2, "max": 2}}'
        ),
    )
    template: str | None = Field(
        default=None,
        description=(
            "Template name used, if any. Leave null for PCs: when the campaign has a "
            "character template, PC sheets are stamped with it automatically. Set this "
            "only to override with a different template."
        ),
    )


def _system_template_name(ctx: ToolContext) -> str | None:
    """Name of the campaign's derived character template, if one exists.

    Used to stamp PC sheets with their provenance: the GM follows this template
    when building a sheet (it rides in the system prompt) but routinely leaves
    the optional ``template`` arg null, so we record it here instead of trusting
    the model to.
    """
    from openadventure.engine.prompts import load_character_template

    template = load_character_template(ctx.meta, ctx.workspace)
    return template.get("name") if template else None


def _create_sheet(ctx: ToolContext, args: CreateSheetArgs) -> ToolOutcome:
    store = _store(ctx)
    template = args.template
    if template is None and args.kind == "pc":
        template = _system_template_name(ctx)
    items = _reconcile_inventory(args.fields)
    sheet = Sheet(
        id=store.unique_id(args.name, args.kind),
        kind=args.kind,
        name=args.name,
        template=template,
        fields=args.fields,
        resources={k: Resource.model_validate(v) for k, v in args.resources.items()},
        items=items,
    )
    sheet.meta.created_at = sheet.meta.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    store.save(sheet)
    if sheet.kind == "pc":
        store.save_original(sheet)  # pristine copy for restart-with-originals
    summary = f"created {sheet.kind} sheet {sheet.id} ({sheet.name})"
    _log_change(ctx, sheet, summary)
    return ToolOutcome(
        content=f"Created sheet {sheet.id}:\n{sheet.model_dump_json(indent=2)}",
        events=[StateChanged(kind="sheet", ref=sheet.id, summary=summary)],
        summary=summary,
    )


# --- get / list --------------------------------------------------------------


class GetSheetArgs(BaseModel):
    id: str


def _get_sheet(ctx: ToolContext, args: GetSheetArgs) -> ToolOutcome:
    sheet = _store(ctx).load(args.id)
    if sheet is None:
        return ToolOutcome(content=f"Error: no sheet {args.id!r}", summary="not found", ok=False)
    return ToolOutcome(content=sheet.model_dump_json(indent=2), summary=f"read {args.id}")


class ListSheetsArgs(BaseModel):
    kind: Literal["pc", "npc", "monster"] | None = Field(
        default=None, description="Filter by kind; omit for all"
    )


def _list_sheets(ctx: ToolContext, args: ListSheetsArgs) -> ToolOutcome:
    entries = _store(ctx).list(kind=args.kind)
    if not entries:
        return ToolOutcome(content="No sheets yet.", summary="0 sheets")
    return ToolOutcome(
        content="\n".join(_sheet_brief(s) for s in entries),
        summary=f"{len(entries)} sheet{'s' if len(entries) != 1 else ''}",
    )


# --- search_sheets -----------------------------------------------------------


class SearchSheetsArgs(BaseModel):
    query: str = Field(
        description="Name or descriptor to recall a character by, e.g. 'Dooley' or 'cigar vendor'"
    )
    kind: Literal["pc", "npc", "monster"] | None = Field(
        default=None, description="Filter by kind; omit to search all sheets"
    )
    k: int = Field(default=5, ge=1, le=20, description="Number of results")


def _sheet_search_text(sheet: Sheet) -> str:
    """Lowercased searchable blob: name plus every field value and carried item."""
    parts = [sheet.name, json.dumps(sheet.fields, ensure_ascii=False), " ".join(sheet.items)]
    return " ".join(parts).casefold()


def _score_sheet(sheet: Sheet, tokens: list[str]) -> int:
    """A query term in the name counts more than one only in the fields/items."""
    name = sheet.name.casefold()
    blob = _sheet_search_text(sheet)
    return sum(3 if t in name else 1 if t in blob else 0 for t in tokens)


def _search_sheets(ctx: ToolContext, args: SearchSheetsArgs) -> ToolOutcome:
    tokens = [t for t in args.query.casefold().split() if t]
    if not tokens:
        return ToolOutcome(content="Error: empty query.", summary="empty", ok=False)
    scored = [
        (score, sheet)
        for sheet in _store(ctx).list(kind=args.kind)
        if (score := _score_sheet(sheet, tokens)) > 0
    ]
    if not scored:
        return ToolOutcome(content=f"No characters matched {args.query!r}.", summary="0 results")
    scored.sort(key=lambda pair: (-pair[0], pair[1].name.casefold()))
    top = [sheet for _score, sheet in scored[: args.k]]

    def _full(sheet: Sheet) -> str:
        # Top matches return the full sheet so the GM can act without a get_sheet.
        return f"{sheet.id} ({sheet.kind}, {sheet.status}): {sheet.name}\n" + sheet.model_dump_json(
            indent=2
        )

    return ToolOutcome(
        content=render_hits(top, full=_full, brief=_sheet_brief),
        summary=f"{len(top)} result{'s' if len(top) != 1 else ''}",
    )


# --- update_sheet -------------------------------------------------------------


class UpdateSheetArgs(BaseModel):
    id: str
    ops: list[SheetOp] = Field(
        description=(
            "Mutations, each {op: set|delete|append, path, value}. Paths are dotted and "
            "start with fields/name/status/template/conditions, e.g. "
            "{'op':'set','path':'fields.level','value':2} or "
            "{'op':'append','path':'fields.inventory','value':'rope (50 ft)'} or "
            "{'op':'set','path':'status','value':'dead'}"
        )
    )


def _update_sheet(ctx: ToolContext, args: UpdateSheetArgs) -> ToolOutcome:
    store = _store(ctx)
    sheet = store.load(args.id)
    if sheet is None:
        return ToolOutcome(content=f"Error: no sheet {args.id!r}", summary="not found", ok=False)
    try:
        new_sheet, changes = sheets_mod.apply_ops(sheet, args.ops)
    except SheetError as exc:
        return ToolOutcome(content=f"Error: {exc}", summary="bad op", ok=False)
    store.save(new_sheet)
    summary = f"{new_sheet.name}: " + "; ".join(changes[:3]) + ("…" if len(changes) > 3 else "")
    _log_change(ctx, new_sheet, summary)
    return ToolOutcome(
        content="Applied:\n" + "\n".join(changes),
        events=[StateChanged(kind="sheet", ref=new_sheet.id, summary=summary)],
        summary=summary,
    )


# --- modify_resource ------------------------------------------------------------


class ModifyResourceArgs(BaseModel):
    sheet_id: str
    resource: str = Field(description="Resource name, e.g. 'hp' or 'spell_slots_1'")
    delta: int | None = Field(
        default=None, description="Change by this amount (negative = damage/spend)"
    )
    set_current: int | None = None
    set_max: int | None = None


def _modify_resource(ctx: ToolContext, args: ModifyResourceArgs) -> ToolOutcome:
    store = _store(ctx)
    sheet = store.load(args.sheet_id)
    if sheet is None:
        return ToolOutcome(
            content=f"Error: no sheet {args.sheet_id!r}", summary="not found", ok=False
        )
    if args.delta is None and args.set_current is None and args.set_max is None:
        return ToolOutcome(
            content="Error: provide delta, set_current, or set_max", summary="no-op", ok=False
        )
    try:
        new_sheet, description = sheets_mod.modify_resource(
            sheet,
            args.resource,
            delta=args.delta,
            set_current=args.set_current,
            set_max=args.set_max,
        )
    except SheetError as exc:
        return ToolOutcome(content=f"Error: {exc}", summary="bad resource", ok=False)
    store.save(new_sheet)
    _log_change(ctx, new_sheet, description)
    return ToolOutcome(
        content=description,
        events=[StateChanged(kind="sheet", ref=new_sheet.id, summary=description)],
        summary=description,
    )


# --- set_conditions ---------------------------------------------------------------


class SetConditionsArgs(BaseModel):
    sheet_id: str
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


def _set_conditions(ctx: ToolContext, args: SetConditionsArgs) -> ToolOutcome:
    store = _store(ctx)
    sheet = store.load(args.sheet_id)
    if sheet is None:
        return ToolOutcome(
            content=f"Error: no sheet {args.sheet_id!r}", summary="not found", ok=False
        )
    new_sheet, description = sheets_mod.set_conditions(sheet, add=args.add, remove=args.remove)
    store.save(new_sheet)
    _log_change(ctx, new_sheet, description)
    return ToolOutcome(
        content=description,
        events=[StateChanged(kind="sheet", ref=new_sheet.id, summary=description)],
        summary=description,
    )


# --- modify_inventory -----------------------------------------------------------


class ItemReplacement(BaseModel):
    old: str = Field(description="Existing item text to match, case-insensitively")
    new: str = Field(description="Text that replaces it in place")


class ModifyInventoryArgs(BaseModel):
    sheet_id: str
    add: list[str] = Field(
        default_factory=list,
        description="Items to add, e.g. ['brass key', 'crimson cult vestments (worn)']",
    )
    remove: list[str] = Field(
        default_factory=list,
        description="Items to remove (consumed, dropped, destroyed); matched case-insensitively",
    )
    replace: list[ItemReplacement] = Field(
        default_factory=list,
        description=(
            "Swap an item's text in place for a state change, keeping its slot, e.g. "
            "{'old': 'crimson cult vestments (worn)', 'new': 'crimson cult vestments (folded, "
            "carried)'} or {'old': 'unlit lantern', 'new': 'lit lantern'}. Prefer this over "
            "a remove+add pair whenever the same item just changed state."
        ),
    )


def _modify_inventory(ctx: ToolContext, args: ModifyInventoryArgs) -> ToolOutcome:
    store = _store(ctx)
    sheet = store.load(args.sheet_id)
    if sheet is None:
        return ToolOutcome(
            content=f"Error: no sheet {args.sheet_id!r}", summary="not found", ok=False
        )
    if not args.add and not args.remove and not args.replace:
        return ToolOutcome(
            content="Error: provide add, remove, and/or replace", summary="no-op", ok=False
        )
    new_sheet, description = sheets_mod.modify_items(
        sheet,
        add=args.add,
        remove=args.remove,
        replace=[(r.old, r.new) for r in args.replace],
    )
    store.save(new_sheet)
    _log_change(ctx, new_sheet, description)
    inventory = ", ".join(new_sheet.items) or "(empty)"
    return ToolOutcome(
        content=f"{description}\nInventory now: {inventory}",
        events=[StateChanged(kind="sheet", ref=new_sheet.id, summary=description)],
        summary=description,
    )


SHEET_TOOLS = [
    Tool(
        name="create_sheet",
        description=(
            "Create a character sheet for a PC, NPC, or monster. Use the campaign's "
            "character template for PCs. For an NPC, make one the first time a named "
            "character could recur or carry a thread; it can be lightweight (a name and "
            "an 'attitude' field) and needs no template or stat block unless they fight. "
            "A creature allied with the party (an animal companion, familiar, or mount) is "
            "an NPC too, not a monster, even with a full stat block. For monsters (adversaries), "
            "transcribe the stat block you read from the rules into fields + resources (always "
            "include an 'hp' resource)."
        ),
        args_model=CreateSheetArgs,
        handler=_create_sheet,
    ),
    Tool(
        name="get_sheet",
        description=(
            "Read the COMPLETE sheet by id: every field the briefs omit "
            "(skills, characteristics, backstory, weapons, player/owner) plus all "
            "resources. Use this whenever you need a detail not shown in a summary, "
            "rather than assuming it is absent. The party roster, the staged-NPC "
            "briefs, and a search_sheets brief hit are all only summaries; this "
            "fetches the full sheet behind any of them."
        ),
        args_model=GetSheetArgs,
        handler=_get_sheet,
        read_only=True,
    ),
    Tool(
        name="list_sheets",
        description=(
            "Brief index of all sheets (id, kind, status, name, scalar fields, "
            "resources). Does NOT include full per-field data such as skills, "
            "backstory, or weapons; call get_sheet by id for that."
        ),
        args_model=ListSheetsArgs,
        handler=_list_sheets,
        read_only=True,
    ),
    Tool(
        name="search_sheets",
        description=(
            "Search character sheets (PCs, NPCs, monsters) by name or descriptor to "
            "recall someone met earlier who is not in your current context. The top "
            "matches come back as full sheets so you can act at once; lower-ranked "
            "matches show a one-line brief. Use this before telling a player you do not "
            "recognize someone they name; get_sheet fetches one by id, list_sheets lists all."
        ),
        args_model=SearchSheetsArgs,
        handler=_search_sheets,
        parallel_safe=True,
        read_only=True,
    ),
    Tool(
        name="update_sheet",
        description=(
            "Mutate a sheet with set/delete/append ops on dotted paths. Use for inventory, "
            "level-ups, status changes (e.g. set status to 'dead' or 'retired'), or marking an "
            "NPC who travels with the party (set companion true so their brief stays in context "
            "across moves). For numeric pools like HP use modify_resource instead."
        ),
        args_model=UpdateSheetArgs,
        handler=_update_sheet,
    ),
    Tool(
        name="modify_resource",
        description=(
            "The canonical damage/heal/spend operation: adjust a sheet's resource pool "
            "(hp, spell slots, ammo…). Values clamp to [min, max]."
        ),
        args_model=ModifyResourceArgs,
        handler=_modify_resource,
    ),
    Tool(
        name="set_conditions",
        description="Add/remove conditions on a sheet (free-form strings like 'prone', 'poisoned').",
        args_model=SetConditionsArgs,
        handler=_set_conditions,
    ),
    Tool(
        name="modify_inventory",
        description=(
            "Add, remove, or replace items on a character's tracked inventory. Use whenever "
            "a character gains, buys, picks up, equips, consumes, drops, or loses a "
            "significant item (a key, a recovered tome, a weapon, worn gear). When the same "
            "item just changes state (a lantern lit, a vial emptied, worn vestments stowed), "
            "use `replace` to swap its text in place rather than a remove+add pair. Tracked "
            "items stay in the party roster every turn and survive context compaction, so "
            "record anything that matters later."
        ),
        args_model=ModifyInventoryArgs,
        handler=_modify_inventory,
    ),
]
