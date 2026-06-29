"""Encounter tools: start fights, track initiative and turns."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from openadventure.engine.events import StateChanged
from openadventure.engine.tools.registry import Tool, ToolContext, ToolOutcome
from openadventure.mechanics.encounter import (
    Combatant,
    Encounter,
    EncounterError,
    next_turn,
    sort_initiative,
)
from openadventure.mechanics.sheets import Resource, Sheet
from openadventure.store import snapshots
from openadventure.store.sheetstore import SheetStore


def load_encounter(ctx: ToolContext) -> Encounter | None:
    data = snapshots.load_json(ctx.campaign.encounter_path)
    if data is None:
        return None
    return Encounter.model_validate(data)


def save_encounter(ctx: ToolContext, encounter: Encounter, summary: str) -> ToolOutcome:
    snapshots.save_json(ctx.campaign.encounter_path, encounter)
    ctx.log.append("state_change", {"kind": "encounter", "ref": encounter.name, "summary": summary})
    return ToolOutcome(
        content=render_encounter(ctx, encounter),
        events=[StateChanged(kind="encounter", ref=encounter.name, summary=summary)],
        summary=summary,
    )


def render_encounter(ctx: ToolContext, encounter: Encounter) -> str:
    if encounter.status == "ended":
        return f"Encounter '{encounter.name}' has ended."
    store = SheetStore(ctx.campaign)
    current = encounter.current()
    lines = [f"Encounter: {encounter.name}, round {encounter.round}"]
    for combatant in encounter.combatants:
        marker = "→" if current is not None and combatant.tag == current.tag else " "
        state = ""
        if combatant.sheet_id:
            sheet = store.load(combatant.sheet_id)
            if sheet is not None:
                hp = sheet.resources.get("hp")
                if hp is not None:
                    state = f" hp {hp.current}/{hp.max}"
                if sheet.conditions:
                    state += f" [{', '.join(sheet.conditions)}]"
        down = "" if combatant.active else " (down)"
        lines.append(
            f"{marker} {combatant.initiative:>4.0f}  {combatant.tag} ({combatant.side})"
            f"{state}{down}"
        )
    return "\n".join(lines)


# --- start_encounter ---------------------------------------------------------


class SpawnSpec(BaseModel):
    name: str = Field(description="Creature name, e.g. 'Goblin Warrior'")
    fields: dict[str, Any] = Field(
        default_factory=dict, description="Stat block data: ac, speed, attacks, traits…"
    )
    resources: dict[str, dict[str, int]] = Field(
        default_factory=dict, description='Must include hp, e.g. {"hp": {"current": 7, "max": 7}}'
    )


class CombatantSpec(BaseModel):
    sheet_id: str | None = Field(default=None, description="Existing sheet id (PCs and known NPCs)")
    spawn: SpawnSpec | None = Field(
        default=None,
        description="Create a fresh monster sheet from a stat block (instead of sheet_id)",
    )
    tag: str | None = Field(
        default=None, description="Display name in the tracker; defaults to the sheet name"
    )
    side: str = Field(
        description="Required: 'party' (a PC), 'ally' (a friendly NPC, companion, or summon), "
        "or 'foe' (an enemy). Has no default, so an ally is never silently treated as a foe."
    )
    initiative: float = Field(
        default=0,
        description="Initiative roll result (set later via update_encounter if not yet rolled)",
    )


class StartEncounterArgs(BaseModel):
    name: str = Field(description="Encounter name, e.g. 'Goblin ambush'")
    combatants: list[CombatantSpec]


def _start_encounter(ctx: ToolContext, args: StartEncounterArgs) -> ToolOutcome:
    existing = load_encounter(ctx)
    if existing is not None and existing.status == "active":
        return ToolOutcome(
            content=f"Error: encounter '{existing.name}' is already active. End it first.",
            summary="encounter active",
            ok=False,
        )
    store = SheetStore(ctx.campaign)
    combatants: list[Combatant] = []
    tags: set[str] = set()
    for spec in args.combatants:
        if (spec.sheet_id is None) == (spec.spawn is None):
            return ToolOutcome(
                content="Error: each combatant needs exactly one of sheet_id or spawn.",
                summary="bad combatant",
                ok=False,
            )
        if spec.spawn is not None:
            sheet = Sheet(
                id=store.unique_id(spec.spawn.name, "monster"),
                kind="monster",
                name=spec.spawn.name,
                fields=spec.spawn.fields,
                resources={k: Resource.model_validate(v) for k, v in spec.spawn.resources.items()},
            )
            if "hp" not in sheet.resources:
                return ToolOutcome(
                    content=f"Error: spawn {spec.spawn.name!r} needs an hp resource.",
                    summary="no hp",
                    ok=False,
                )
            store.save(sheet)
            sheet_id = sheet.id
            default_tag = sheet.name
        else:
            sheet = store.load(spec.sheet_id)
            if sheet is None:
                return ToolOutcome(
                    content=f"Error: no sheet {spec.sheet_id!r}", summary="not found", ok=False
                )
            sheet_id = sheet.id
            default_tag = sheet.name
        tag = spec.tag or default_tag
        base, n = tag, 2
        while tag.casefold() in tags:
            tag = f"{base} {n}"
            n += 1
        tags.add(tag.casefold())
        combatants.append(
            Combatant(tag=tag, sheet_id=sheet_id, side=spec.side, initiative=spec.initiative)
        )

    encounter = sort_initiative(Encounter(name=args.name, combatants=combatants))
    return save_encounter(ctx, encounter, f"encounter started: {args.name}")


# --- update_encounter -----------------------------------------------------------


class InitiativeSet(BaseModel):
    tag: str
    value: float


class UpdateEncounterArgs(BaseModel):
    set_initiative: list[InitiativeSet] | None = Field(
        default=None, description="Set initiative values (re-sorts the order)"
    )
    add: list[CombatantSpec] | None = Field(default=None, description="Reinforcements")
    defeat: list[str] | None = Field(
        default=None, description="Tags of combatants now defeated/fled (kept, marked down)"
    )
    next_turn: bool = Field(default=False, description="Advance to the next active combatant")
    end: bool = Field(default=False, description="End the encounter")


def _update_encounter(ctx: ToolContext, args: UpdateEncounterArgs) -> ToolOutcome:
    encounter = load_encounter(ctx)
    if encounter is None or encounter.status != "active":
        return ToolOutcome(
            content="Error: no active encounter. Use start_encounter first.",
            summary="no encounter",
            ok=False,
        )
    store = SheetStore(ctx.campaign)
    summaries: list[str] = []
    try:
        if args.set_initiative:
            for item in args.set_initiative:
                encounter.find(item.tag).initiative = item.value
            encounter = sort_initiative(encounter)
            summaries.append("initiative set")
        if args.add:
            tags = {c.tag.casefold() for c in encounter.combatants}
            for spec in args.add:
                if (spec.sheet_id is None) == (spec.spawn is None):
                    raise EncounterError("each combatant needs exactly one of sheet_id or spawn")
                if spec.spawn is not None:
                    sheet = Sheet(
                        id=store.unique_id(spec.spawn.name, "monster"),
                        kind="monster",
                        name=spec.spawn.name,
                        fields=spec.spawn.fields,
                        resources={
                            k: Resource.model_validate(v) for k, v in spec.spawn.resources.items()
                        },
                    )
                    store.save(sheet)
                else:
                    sheet = store.load(spec.sheet_id)
                    if sheet is None:
                        raise EncounterError(f"no sheet {spec.sheet_id!r}")
                tag = spec.tag or sheet.name
                base, n = tag, 2
                while tag.casefold() in tags:
                    tag = f"{base} {n}"
                    n += 1
                tags.add(tag.casefold())
                encounter.combatants.append(
                    Combatant(
                        tag=tag, sheet_id=sheet.id, side=spec.side, initiative=spec.initiative
                    )
                )
            summaries.append(f"{len(args.add)} combatant(s) joined")
        if args.defeat:
            for tag in args.defeat:
                encounter.find(tag).active = False
            summaries.append(f"down: {', '.join(args.defeat)}")
        if args.next_turn:
            encounter, current = next_turn(encounter)
            if current is not None:
                summaries.append(f"round {encounter.round}, {current.tag}'s turn")
        if args.end:
            encounter.status = "ended"
            summaries.append(f"encounter ended: {encounter.name}")
    except EncounterError as exc:
        return ToolOutcome(content=f"Error: {exc}", summary="bad update", ok=False)

    if not summaries:
        return ToolOutcome(content="Error: nothing to do.", summary="no-op", ok=False)
    return save_encounter(ctx, encounter, "; ".join(summaries))


ENCOUNTER_TOOLS = [
    Tool(
        name="start_encounter",
        description=(
            "Begin combat: list every combatant, including the party's own side. Add existing "
            "PCs, allied NPCs, and companions by sheet_id; spawn monsters via spawn with stat "
            "block data from the rules. Set each combatant's side ('party', 'ally', or 'foe') -- "
            "it is required, so an ally is never mistaken for an enemy. Roll initiative with "
            "roll_dice, then set it here or via update_encounter. Only one encounter can be active."
        ),
        args_model=StartEncounterArgs,
        handler=_start_encounter,
    ),
    Tool(
        name="update_encounter",
        description=(
            "Manage the active encounter: set initiative, add reinforcements, mark "
            "combatants defeated, advance the turn, or end the fight. Damage/healing "
            "goes through modify_resource on the combatant's sheet."
        ),
        args_model=UpdateEncounterArgs,
        handler=_update_encounter,
    ),
]
