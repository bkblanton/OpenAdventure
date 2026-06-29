"""Clock tools through the registry, plus their reach into session context."""

from openadventure.engine.tools import build_registry
from openadventure.store import checkpoints, snapshots
from tests.test_sheet_tools import make_ctx


def test_create_advance_fill_cancel(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)

    created = registry.dispatch(
        ctx,
        "create_clock",
        {"name": "The cult completes the ritual", "size": 4, "trigger": "the gate opens"},
    )
    assert created.ok
    board = snapshots.load_json(campaign.clocks_path)
    cid = board["clocks"][0]["id"]
    assert cid == "the-cult-completes"  # slug of the first three words

    advanced = registry.dispatch(
        ctx, "advance_clock", {"id": cid, "delta": 2, "reason": "they dawdled"}
    )
    assert "2/4" in advanced.content

    full = registry.dispatch(ctx, "advance_clock", {"id": cid, "delta": 5})
    assert "4/4" in full.content
    assert "FULL" in full.summary
    assert snapshots.load_json(campaign.clocks_path)["clocks"][0]["status"] == "filled"

    registry.dispatch(ctx, "advance_clock", {"id": cid, "cancel": True})
    listed = registry.dispatch(ctx, "list_clocks", {})
    assert "No active clocks" in listed.content


def test_advance_unknown_clock_errors(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    out = registry.dispatch(ctx, "advance_clock", {"id": "ghost", "delta": 1})
    assert not out.ok
    assert "no clock" in out.content


def test_hidden_clock_movement_is_private_in_dm_mode(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)  # campaign defaults to gm mode
    registry.dispatch(
        ctx,
        "create_clock",
        {"name": "Spy reports to the duke", "size": 6, "visible": False, "id": "spy"},
    )
    out = registry.dispatch(ctx, "advance_clock", {"id": "spy", "delta": 1})
    assert out.private
    assert out.public_result_summary == "a hidden clock advanced"


def test_clocks_survive_checkpoint_restore(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(ctx, "create_clock", {"name": "Flood", "size": 4, "id": "flood"})
    checkpoints.save(campaign, 1)
    registry.dispatch(ctx, "advance_clock", {"id": "flood", "delta": 3})
    assert snapshots.load_json(campaign.clocks_path)["clocks"][0]["filled"] == 3

    checkpoints.restore(campaign, 1)
    assert snapshots.load_json(campaign.clocks_path)["clocks"][0]["filled"] == 0


def test_clock_and_on_stage_npc_reach_context(make_session, workspace, campaign):
    session = make_session()
    session.tools.dispatch(
        session.tool_ctx, "create_clock", {"name": "Flood rises", "size": 6, "id": "flood"}
    )
    session.tools.dispatch(session.tool_ctx, "advance_clock", {"id": "flood", "delta": 3})
    session.tools.dispatch(
        session.tool_ctx,
        "create_sheet",
        {
            "kind": "npc",
            "name": "Ireena",
            "fields": {
                "goal": "escape Barovia",
                "attitude": "wary",
                "secret": "Strahd believes she is his lost love",
            },
            "resources": {"hp": {"current": 9, "max": 9}},
        },
    )
    session.tools.dispatch(
        session.tool_ctx, "update_scene", {"location": "Village square", "npcs_present": ["ireena"]}
    )

    messages, _ = session.build_messages()
    context = "\n".join(b.text for m in messages for b in m.content if b.type == "text")

    assert "Flood rises" in context
    assert "3/6" in context
    assert "Ireena" in context
    assert "escape Barovia" in context
    assert "Strahd believes she is his lost love" in context  # secret is GM-facing context
    assert "npcs_present:" not in context  # the bare id list isn't dumped as a scene line
