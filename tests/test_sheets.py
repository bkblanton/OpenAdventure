"""Sheet mechanics: ops, clamping, conditions, status lifecycle."""

import pytest

from openadventure.mechanics.sheets import (
    Resource,
    Sheet,
    SheetError,
    SheetOp,
    apply_ops,
    modify_items,
    modify_resource,
    set_conditions,
)


def make_sheet(**kwargs) -> Sheet:
    defaults = dict(
        id="kasimir",
        kind="pc",
        name="Kasimir Ironfoot",
        fields={"class": "Fighter", "level": 1, "abilities": {"str": 16}, "inventory": ["axe"]},
        resources={"hp": Resource(current=12, max=12)},
    )
    defaults.update(kwargs)
    return Sheet(**defaults)


def test_set_nested_field():
    sheet, changes = apply_ops(
        make_sheet(), [SheetOp(op="set", path="fields.abilities.dex", value=14)]
    )
    assert sheet.fields["abilities"]["dex"] == 14
    assert sheet.fields["abilities"]["str"] == 16
    assert sheet.meta.rev == 1
    assert changes


def test_append_and_create_list():
    sheet, _ = apply_ops(
        make_sheet(),
        [
            SheetOp(op="append", path="fields.inventory", value="rope"),
            SheetOp(op="append", path="fields.scars", value="left cheek"),
        ],
    )
    assert sheet.fields["inventory"] == ["axe", "rope"]
    assert sheet.fields["scars"] == ["left cheek"]


def test_delete_field_and_missing_path_errors():
    sheet, _ = apply_ops(make_sheet(), [SheetOp(op="delete", path="fields.class")])
    assert "class" not in sheet.fields
    with pytest.raises(SheetError):
        apply_ops(make_sheet(), [SheetOp(op="delete", path="fields.nonexistent")])


def test_status_lifecycle():
    sheet, _ = apply_ops(make_sheet(), [SheetOp(op="set", path="status", value="dead")])
    assert sheet.status == "dead"
    with pytest.raises(ValueError):  # pydantic rejects junk statuses
        apply_ops(make_sheet(), [SheetOp(op="set", path="status", value="zombie")])


def test_protected_roots():
    with pytest.raises(SheetError):
        apply_ops(make_sheet(), [SheetOp(op="set", path="id", value="hacked")])
    with pytest.raises(SheetError):
        apply_ops(make_sheet(), [SheetOp(op="set", path="resources.hp.current", value=999)])


def test_resource_clamping():
    sheet, desc = modify_resource(make_sheet(), "hp", delta=-5)
    assert sheet.resources["hp"].current == 7
    assert "7/12" in desc
    sheet, _ = modify_resource(sheet, "hp", delta=-100)
    assert sheet.resources["hp"].current == 0  # clamped at min
    sheet, _ = modify_resource(sheet, "hp", delta=+100)
    assert sheet.resources["hp"].current == 12  # clamped at max


def test_resource_set_and_create():
    sheet, _ = modify_resource(make_sheet(), "hp", set_max=20, set_current=15)
    assert (sheet.resources["hp"].current, sheet.resources["hp"].max) == (15, 20)
    sheet, _ = modify_resource(make_sheet(), "rage", set_max=3)
    assert sheet.resources["rage"].current == 3
    with pytest.raises(SheetError):
        modify_resource(make_sheet(), "mana", delta=-1)


def test_conditions_dedupe_and_remove():
    sheet, _ = set_conditions(make_sheet(), add=["Prone", "prone", "poisoned"])
    assert sheet.conditions == ["prone", "poisoned"]
    sheet, desc = set_conditions(sheet, remove=["PRONE"])
    assert sheet.conditions == ["poisoned"]
    assert "poisoned" in desc


def test_modify_items_keeps_casing_dedupes_and_removes_case_insensitively():
    sheet, desc = modify_items(
        make_sheet(), add=["Crimson cult vestments", "crimson cult vestments", "Brass key"]
    )
    # casing is preserved (items are descriptive), and the duplicate is dropped
    assert sheet.items == ["Crimson cult vestments", "Brass key"]
    assert desc == "Kasimir Ironfoot: gained Crimson cult vestments, Brass key"

    sheet, desc = modify_items(sheet, remove=["BRASS KEY"], add=["lit lantern"])
    assert sheet.items == ["Crimson cult vestments", "lit lantern"]
    assert "gained lit lantern" in desc and "lost Brass key" in desc


def test_modify_items_noop_description():
    _, desc = modify_items(make_sheet(), remove=["nonexistent"])
    assert desc == "Kasimir Ironfoot: no inventory change"


def test_modify_items_replace_swaps_in_place_keeping_position():
    sheet, _ = modify_items(make_sheet(), add=["Brass key", "vestments (worn)", "lit lantern"])
    sheet, desc = modify_items(sheet, replace=[("VESTMENTS (WORN)", "vestments (folded, carried)")])
    # the item keeps its slot rather than moving to the end
    assert sheet.items == ["Brass key", "vestments (folded, carried)", "lit lantern"]
    assert desc == "Kasimir Ironfoot: replaced vestments (worn) with vestments (folded, carried)"


def test_modify_items_replace_missing_old_adds_new():
    sheet, desc = modify_items(make_sheet(), replace=[("ghost item", "brass key")])
    assert sheet.items == ["brass key"]
    assert "gained brass key" in desc


def test_modify_items_replace_into_existing_text_drops_stale_slot():
    sheet, _ = modify_items(make_sheet(), add=["torch", "lit torch"])
    # replacing 'torch' with the already-present 'lit torch' must not duplicate it
    sheet, desc = modify_items(sheet, replace=[("torch", "lit torch")])
    assert sheet.items == ["lit torch"]
    assert "lost torch" in desc


def test_original_sheet_untouched():
    original = make_sheet()
    apply_ops(original, [SheetOp(op="set", path="fields.level", value=5)])
    modify_resource(original, "hp", delta=-5)
    assert original.fields["level"] == 1
    assert original.resources["hp"].current == 12
