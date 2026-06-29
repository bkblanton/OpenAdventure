"""Scene state + GM notes tools through the registry."""

from openadventure.engine.tools import build_registry
from openadventure.engine.tools.scene_tools import render_scene
from openadventure.store import snapshots
from tests.test_sheet_tools import make_ctx

_SCENE_FIXTURE = {
    "location": "Barovia village",
    "time": "dusk",
    "description": "Mist clings to the empty street.",
    "obvious_exits": ["front doors", "street"],
    "unresolved_options": ["the old well"],
    "flags": {"fog": True},
    # GM-only working state: surfaced only with full=True (assistant mode)
    "module_path": "death-house/1-entrance.md",
    "extra_paths": ["death-house/2-hall.md"],
    "prep_notes": "The trap resets each round.",
    "npcs_present": ["ireena"],
}


def test_render_scene_is_player_facing_by_default():
    rendered = render_scene(_SCENE_FIXTURE)
    assert "Barovia village (dusk)" in rendered
    assert "Mist clings to the empty street." in rendered
    assert "Exits: front doors, street" in rendered
    assert "Nearby: the old well" in rendered
    assert "fog: True" in rendered
    for hidden in ("death-house", "prep_notes", "resets", "ireena", "module_path"):
        assert hidden not in rendered


def test_render_scene_full_includes_gm_state():
    rendered = render_scene(_SCENE_FIXTURE, full=True)
    # still shows everything the player-facing view does...
    assert "Barovia village (dusk)" in rendered
    assert "Exits: front doors, street" in rendered
    # ...plus the GM-only working state
    assert "NPCs present: ireena" in rendered
    assert "Module path: death-house/1-entrance.md" in rendered
    assert "Extra paths: death-house/2-hall.md" in rendered
    assert "Prep notes: The trap resets each round." in rendered


def test_render_scene_empty_when_only_gm_state():
    assert render_scene({"module_path": "x.md", "prep_notes": "secret"}) == ""


def test_scene_and_notes_tools(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)

    scene = registry.dispatch(
        ctx,
        "update_scene",
        {
            "location": "Barovia village",
            "time": "dusk",
            "module_path": "death-house/1-entrance.md",
            "obvious_exits": ["front doors", "street"],
            "unresolved_options": ["front doors"],
            "flags": {"fog": True},
        },
    )
    assert scene.ok
    snapshot = snapshots.load_json(campaign.scene_path)
    assert snapshot["location"] == "Barovia village"
    assert snapshot["module_path"] == "death-house/1-entrance.md"
    assert snapshot["obvious_exits"] == ["front doors", "street"]
    assert snapshot["unresolved_options"] == ["front doors"]
    assert snapshot["flags"]["fog"] is True

    # merge, not replace
    registry.dispatch(ctx, "update_scene", {"time": "midnight"})
    snapshot = snapshots.load_json(campaign.scene_path)
    assert snapshot["location"] == "Barovia village"
    assert snapshot["time"] == "midnight"
    assert snapshot["obvious_exits"] == ["front doors", "street"]

    # scene-specific navigation should not leak into a new location
    registry.dispatch(
        ctx,
        "update_scene",
        {"location": "Upper Hall", "module_path": "death-house/6-upper-hall.md"},
    )
    snapshot = snapshots.load_json(campaign.scene_path)
    assert snapshot["location"] == "Upper Hall"
    assert snapshot["module_path"] == "death-house/6-upper-hall.md"
    assert "obvious_exits" not in snapshot
    assert "unresolved_options" not in snapshot

    secret = registry.dispatch(
        ctx,
        "note_canon",
        {"category": "world", "text": "The children are illusions", "visibility": "hidden"},
    )
    assert secret.private is True  # GM-only: not shown at the table
    registry.dispatch(
        ctx, "note_canon", {"category": "threads", "text": "Find the monster in the house"}
    )

    found = registry.dispatch(ctx, "search_canon", {"query": "illusions"})
    assert "The children are illusions" in found.content
    threads = registry.dispatch(ctx, "search_canon", {"query": "monster"})
    assert "monster in the house" in threads.content
    nothing = registry.dispatch(ctx, "search_canon", {"query": "zzz"})
    assert "No matching canon" in nothing.content

    # the durable fact also lands in canon.json, visible to the GM's context
    from openadventure.store import canon

    by_text = {e.text: e for e in canon.load(campaign).entries}
    assert by_text["The children are illusions"].visibility == "hidden"
    assert by_text["Find the monster in the house"].category == "threads"


def test_npcs_present_clears_on_move(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)

    registry.dispatch(ctx, "update_scene", {"location": "Tavern", "npcs_present": ["barkeep"]})
    assert snapshots.load_json(campaign.scene_path)["npcs_present"] == ["barkeep"]

    # a stale cast shouldn't follow the party to the next location
    registry.dispatch(ctx, "update_scene", {"location": "Street"})
    assert "npcs_present" not in snapshots.load_json(campaign.scene_path)


def test_prep_notes_and_extra_paths_clear_on_move(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)

    registry.dispatch(
        ctx,
        "update_scene",
        {
            "location": "Crypt",
            "module_path": "mod/crypt.md",
            "extra_paths": ["mod/crypt-annex.md"],
            "prep_notes": "Reconstructed trap DC 15 Dex save.",
        },
    )
    snapshot = snapshots.load_json(campaign.scene_path)
    assert snapshot["extra_paths"] == ["mod/crypt-annex.md"]
    assert snapshot["prep_notes"] == "Reconstructed trap DC 15 Dex save."

    # scene-local working notes shouldn't follow the party to the next location
    registry.dispatch(ctx, "update_scene", {"location": "Stairs", "module_path": "mod/stairs.md"})
    snapshot = snapshots.load_json(campaign.scene_path)
    assert "extra_paths" not in snapshot
    assert "prep_notes" not in snapshot

    # but they persist across a same-location update (no move)
    registry.dispatch(ctx, "update_scene", {"prep_notes": "Trap now disarmed."})
    registry.dispatch(ctx, "update_scene", {"time": "midnight"})
    snapshot = snapshots.load_json(campaign.scene_path)
    assert snapshot["prep_notes"] == "Trap now disarmed."


def _make_dooley(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(
        ctx,
        "create_sheet",
        {"kind": "npc", "name": "Mr Dooley", "fields": {"attitude": "friendly"}},
    )
    return registry, ctx


def test_unstaged_scene_npcs_scans_last_narration(make_session, workspace, campaign):
    # The scene is frozen at a location whose text never mentions Dooley, but the
    # GM narrated him back on stage. The stale-scene scan alone would miss it; the
    # narration scan catches it the next turn.
    registry, ctx = _make_dooley(workspace, campaign)
    registry.dispatch(ctx, "update_scene", {"location": "Corbitt house upper landing"})
    ctx.log.append("gm_message", {"text": "Dooley sets up his stand and greets you warmly."})

    session = make_session()
    recall = session.unstaged_scene_npcs(snapshots.load_json(campaign.scene_path))
    assert recall is not None and "mr-dooley" in recall


def test_unstaged_scene_npcs_skips_when_already_staged(make_session, workspace, campaign):
    registry, ctx = _make_dooley(workspace, campaign)
    registry.dispatch(
        ctx, "update_scene", {"location": "French Hill street", "npcs_present": ["mr-dooley"]}
    )
    ctx.log.append("gm_message", {"text": "Dooley greets you warmly."})

    session = make_session()
    assert session.unstaged_scene_npcs(snapshots.load_json(campaign.scene_path)) is None


def test_npcs_referenced_unstaged_matches_query_and_id(make_session, workspace, campaign):
    registry, ctx = _make_dooley(workspace, campaign)
    session = make_session()

    assert [s.id for s in session.npcs_referenced_unstaged("dooley vendor", [])] == ["mr-dooley"]
    assert [s.id for s in session.npcs_referenced_unstaged("", ["mr-dooley"])] == ["mr-dooley"]

    # once staged, the same lookup no longer flags them
    registry.dispatch(ctx, "update_scene", {"location": "street", "npcs_present": ["mr-dooley"]})
    assert session.npcs_referenced_unstaged("dooley", ["mr-dooley"]) == []
