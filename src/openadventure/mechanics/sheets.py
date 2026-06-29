"""System-agnostic character/NPC/monster sheets.

The engine owns structure (paths resolve, resources clamp); the *meaning* of
fields belongs to the AI and the system source's template. Nothing 5e-specific here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Kind = Literal["pc", "npc", "monster"]
Status = Literal["active", "dead", "retired"]


class SheetError(ValueError):
    """Raised for invalid sheet mutations."""


class Resource(BaseModel):
    current: int
    max: int
    min: int = 0


class SheetMeta(BaseModel):
    created_at: str = ""
    updated_at: str = ""
    rev: int = 0


class Sheet(BaseModel):
    id: str
    kind: Kind = "pc"
    status: Status = "active"
    name: str
    template: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Resource] = Field(default_factory=dict)
    conditions: list[str] = Field(default_factory=list)
    items: list[str] = Field(default_factory=list)
    # An NPC traveling with the party (companion, hireling, escorted captive). When
    # set, the sheet's brief rides in context every turn regardless of scene, so a
    # follower never drops out of view when the party moves. Ignored for PCs (they
    # are already in the party roster) and monsters.
    companion: bool = False
    meta: SheetMeta = Field(default_factory=SheetMeta)

    def touch(self) -> None:
        self.meta.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.meta.rev += 1

    def scalar_fields(self) -> list[tuple[str, Any]]:
        """Top-level ``fields`` entries that are simple scalars (str/int/float/bool),
        for a compact, system-agnostic at-a-glance line in the roster and sheet
        briefs. Nested structures (skills, characteristics, backstory, weapons) are
        deliberately omitted; fetch the full sheet for those. ``player`` is ordered
        first when present so ownership is always visible; ``name`` is dropped as it
        duplicates the sheet name. Nothing here is system-specific, so it adapts to
        any template (CoC occupation/age, D&D class/species/level, etc.)."""
        scalars = [
            (key, value)
            for key, value in self.fields.items()
            if key != "name" and isinstance(value, (str, int, float))
        ]
        scalars.sort(key=lambda kv: kv[0] != "player")  # stable: player first, rest in order
        return scalars


class SheetOp(BaseModel):
    op: Literal["set", "delete", "append"]
    path: str  # dotted, e.g. "fields.abilities.str" or "status" or "name"
    value: Any = None


_MUTABLE_ROOTS = ("fields", "name", "status", "template", "conditions", "items", "companion")


def _walk(container: Any, keys: list[str], *, create: bool) -> tuple[Any, str]:
    """Walk to the parent of the final key, optionally creating dicts."""
    for i, key in enumerate(keys[:-1]):
        if isinstance(container, list):
            raise SheetError(f"cannot traverse into a list at {'.'.join(keys[: i + 1])!r}")
        if not isinstance(container, dict):
            raise SheetError(f"path segment {'.'.join(keys[: i + 1])!r} is not an object")
        if key not in container:
            if not create:
                raise SheetError(f"no such path segment {'.'.join(keys[: i + 1])!r}")
            container[key] = {}
        container = container[key]
    return container, keys[-1]


def apply_ops(sheet: Sheet, ops: list[SheetOp]) -> tuple[Sheet, list[str]]:
    """Apply ops to a copy of the sheet; returns (new_sheet, change descriptions)."""
    data = sheet.model_dump(mode="json")
    changes: list[str] = []
    for op in ops:
        keys = [k for k in op.path.split(".") if k]
        if not keys or keys[0] not in _MUTABLE_ROOTS:
            raise SheetError(
                f"path must start with one of {', '.join(_MUTABLE_ROOTS)} (got {op.path!r})"
            )
        match op.op:
            case "set":
                parent, last = _walk(data, keys, create=True)
                parent[last] = op.value
                changes.append(f"{op.path} = {op.value!r}")
            case "delete":
                parent, last = _walk(data, keys, create=False)
                if isinstance(parent, dict):
                    if last not in parent:
                        raise SheetError(f"no such path {op.path!r}")
                    del parent[last]
                elif isinstance(parent, list):
                    raise SheetError("delete from a list is not supported; set the list instead")
                changes.append(f"deleted {op.path}")
            case "append":
                parent, last = _walk(data, keys, create=True)
                target = parent.get(last) if isinstance(parent, dict) else None
                if target is None:
                    target = []
                    parent[last] = target
                if not isinstance(target, list):
                    raise SheetError(f"{op.path!r} is not a list")
                target.append(op.value)
                changes.append(f"appended {op.value!r} to {op.path}")
    new_sheet = Sheet.model_validate(data)
    new_sheet.touch()
    return new_sheet, changes


def modify_resource(
    sheet: Sheet,
    name: str,
    *,
    delta: int | None = None,
    set_current: int | None = None,
    set_max: int | None = None,
) -> tuple[Sheet, str]:
    """Adjust a resource pool with clamping. Returns (new_sheet, description)."""
    new_sheet = sheet.model_copy(deep=True)
    resource = new_sheet.resources.get(name)
    if resource is None:
        if set_max is None:
            raise SheetError(
                f"no resource {name!r} on {sheet.name} "
                f"(has: {', '.join(sheet.resources) or 'none'})"
            )
        resource = Resource(current=set_max, max=set_max)
        new_sheet.resources[name] = resource
    if set_max is not None:
        resource.max = set_max
    if set_current is not None:
        resource.current = set_current
    if delta is not None:
        resource.current += delta
    resource.current = max(resource.min, min(resource.current, resource.max))
    new_sheet.touch()
    description = f"{sheet.name} {name}: {resource.current}/{resource.max}"
    return new_sheet, description


def set_conditions(
    sheet: Sheet, *, add: list[str] | None = None, remove: list[str] | None = None
) -> tuple[Sheet, str]:
    new_sheet = sheet.model_copy(deep=True)
    for condition in add or []:
        normalized = condition.strip().lower()
        if normalized and normalized not in new_sheet.conditions:
            new_sheet.conditions.append(normalized)
    for condition in remove or []:
        normalized = condition.strip().lower()
        if normalized in new_sheet.conditions:
            new_sheet.conditions.remove(normalized)
    new_sheet.touch()
    label = ", ".join(new_sheet.conditions) or "none"
    return new_sheet, f"{sheet.name} conditions: {label}"


def modify_items(
    sheet: Sheet,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    replace: list[tuple[str, str]] | None = None,
) -> tuple[Sheet, str]:
    """Add/remove/replace items on a sheet's tracked inventory. Unlike conditions,
    item text keeps its casing (it's descriptive, e.g. 'Crimson cult vestments');
    adds dedupe and removes match case-insensitively so the GM needn't echo casing
    exactly. ``replace`` swaps an item's text in place (old matched
    case-insensitively), keeping its position, for state changes like a lantern
    being lit or worn gear being stowed; if the old text isn't present the new text
    is simply added. The description reports the delta, not the whole inventory."""
    new_sheet = sheet.model_copy(deep=True)
    added: list[str] = []
    removed: list[str] = []
    replaced: list[tuple[str, str]] = []
    for old, new in replace or []:
        new_clean = new.strip()
        if not new_clean:
            continue
        target = old.strip().casefold()
        idx = next(
            (i for i, existing in enumerate(new_sheet.items) if existing.casefold() == target),
            None,
        )
        # the replacement text already living elsewhere would dupe; drop the stale slot instead
        dupe = any(
            i != idx and existing.casefold() == new_clean.casefold()
            for i, existing in enumerate(new_sheet.items)
        )
        if idx is None:
            if not dupe:
                new_sheet.items.append(new_clean)
                added.append(new_clean)
        elif dupe:
            removed.append(new_sheet.items.pop(idx))
        else:
            replaced.append((new_sheet.items[idx], new_clean))
            new_sheet.items[idx] = new_clean
    for item in add or []:
        cleaned = item.strip()
        if cleaned and not any(
            cleaned.casefold() == existing.casefold() for existing in new_sheet.items
        ):
            new_sheet.items.append(cleaned)
            added.append(cleaned)
    for item in remove or []:
        target = item.strip().casefold()
        match = next(
            (existing for existing in new_sheet.items if existing.casefold() == target), None
        )
        if match is not None:
            new_sheet.items.remove(match)
            removed.append(match)
    new_sheet.touch()
    parts = []
    if replaced:
        parts.append("replaced " + ", ".join(f"{old} with {new}" for old, new in replaced))
    if added:
        parts.append("gained " + ", ".join(added))
    if removed:
        parts.append("lost " + ", ".join(removed))
    change = "; ".join(parts) if parts else "no inventory change"
    return new_sheet, f"{sheet.name}: {change}"
