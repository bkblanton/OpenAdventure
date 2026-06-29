"""Encounter tools + a scripted FakeProvider combat round."""

from openadventure.engine.tools import build_registry
from openadventure.providers.base import PToolUse, PTurnDone, Usage
from openadventure.store import snapshots
from openadventure.store.sheetstore import SheetStore
from tests.conftest import collect
from tests.test_agent_loop import text_turn
from tests.test_sheet_tools import make_ctx

GOBLIN_SPAWN = {
    "spawn": {
        "name": "Goblin Warrior",
        "fields": {"ac": 15, "speed": "30 ft."},
        "resources": {"hp": {"current": 10, "max": 10}},
    },
    "side": "foe",
    "initiative": 12,
}


def setup_fight(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(
        ctx,
        "create_sheet",
        {
            "kind": "pc",
            "name": "Kasimir",
            "fields": {"class": "Fighter"},
            "resources": {"hp": {"current": 12, "max": 12}},
        },
    )
    return registry, ctx


def test_start_spawn_damage_defeat_end(workspace, campaign):
    registry, ctx = setup_fight(workspace, campaign)

    started = registry.dispatch(
        ctx,
        "start_encounter",
        {
            "name": "Goblin ambush",
            "combatants": [
                {"sheet_id": "kasimir", "side": "party", "initiative": 17},
                GOBLIN_SPAWN,
                GOBLIN_SPAWN,
            ],
        },
    )
    assert started.ok
    assert "round 1" in started.content
    assert "Goblin Warrior 2" in started.content  # tag dedupe

    # spawned monsters got sheets
    store = SheetStore(campaign)
    assert store.load("goblin-warrior") is not None
    assert store.load("goblin-warrior-2") is not None

    # damage one goblin to 0 and defeat it
    damaged = registry.dispatch(
        ctx, "modify_resource", {"sheet_id": "goblin-warrior", "resource": "hp", "delta": -10}
    )
    assert "0/10" in damaged.content
    updated = registry.dispatch(
        ctx, "update_encounter", {"defeat": ["Goblin Warrior"], "next_turn": True}
    )
    assert updated.ok
    assert "(down)" in updated.content

    # turn order skips the downed goblin: Kasimir (17) -> Goblin Warrior 2 (12)
    snapshot = snapshots.load_json(campaign.encounter_path)
    tags_active = [c["tag"] for c in snapshot["combatants"] if c["active"]]
    assert tags_active == ["Kasimir", "Goblin Warrior 2"]

    ended = registry.dispatch(ctx, "update_encounter", {"end": True})
    assert ended.ok
    assert snapshots.load_json(campaign.encounter_path)["status"] == "ended"


def test_combatant_side_is_required(workspace, campaign):
    # side has no default, so an ally is never silently filed as a foe: omitting it
    # is a validation error the agent must fix, not a quiet miscategorization.
    registry, ctx = setup_fight(workspace, campaign)
    result = registry.dispatch(
        ctx,
        "start_encounter",
        {"name": "Ambush", "combatants": [{"sheet_id": "kasimir", "initiative": 17}]},
    )
    assert not result.ok
    assert "invalid args" in result.summary


def test_second_encounter_requires_first_ended(workspace, campaign):
    registry, ctx = setup_fight(workspace, campaign)
    registry.dispatch(
        ctx,
        "start_encounter",
        {"name": "Fight 1", "combatants": [{"sheet_id": "kasimir", "side": "party"}]},
    )
    again = registry.dispatch(
        ctx,
        "start_encounter",
        {"name": "Fight 2", "combatants": [{"sheet_id": "kasimir", "side": "party"}]},
    )
    assert not again.ok
    registry.dispatch(ctx, "update_encounter", {"end": True})
    now_ok = registry.dispatch(
        ctx,
        "start_encounter",
        {"name": "Fight 2", "combatants": [{"sheet_id": "kasimir", "side": "party"}]},
    )
    assert now_ok.ok


def test_set_initiative_resorts(workspace, campaign):
    registry, ctx = setup_fight(workspace, campaign)
    registry.dispatch(
        ctx,
        "start_encounter",
        {
            "name": "Skirmish",
            "combatants": [{"sheet_id": "kasimir", "side": "party"}, GOBLIN_SPAWN],
        },
    )
    updated = registry.dispatch(
        ctx,
        "update_encounter",
        {
            "set_initiative": [
                {"tag": "Kasimir", "value": 20},
                {"tag": "Goblin Warrior", "value": 5},
            ]
        },
    )
    snapshot = snapshots.load_json(campaign.encounter_path)
    assert [c["tag"] for c in snapshot["combatants"]] == ["Kasimir", "Goblin Warrior"]
    assert updated.ok


async def test_scripted_combat_turn(make_session, workspace, campaign):
    """A full AI combat turn: roll initiative, start encounter, deal damage."""
    registry, ctx = setup_fight(workspace, campaign)
    script = [
        [
            PToolUse(
                id="t1",
                name="roll_dice",
                input={"expression": "1d20+1", "reason": "Kasimir initiative"},
            ),
            PToolUse(
                id="t2",
                name="start_encounter",
                input={
                    "name": "Ambush",
                    "combatants": [
                        {"sheet_id": "kasimir", "side": "party", "initiative": 17},
                        GOBLIN_SPAWN,
                    ],
                },
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        [
            PToolUse(
                id="t3",
                name="modify_resource",
                input={"sheet_id": "goblin-warrior", "resource": "hp", "delta": -6},
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("Your axe bites deep, the goblin reels!"),
    ]
    session = make_session(script=script)
    events = await collect(session.handle_input("I charge the goblins!"))

    kinds = [e.type for e in events]
    assert kinds.count("state_changed") >= 2  # encounter started + hp change
    assert events[-1].type == "turn_completed"

    # encounter snapshot + sheet state both persisted
    snapshot = snapshots.load_json(campaign.encounter_path)
    assert snapshot["name"] == "Ambush"
    goblin = SheetStore(campaign).load("goblin-warrior")
    assert goblin.resources["hp"].current == 4

    # encounter table reaches the next prompt's context block
    assert "Ambush" in (session.encounter_summary() or "")
