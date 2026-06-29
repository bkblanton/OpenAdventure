"""Sheet tools end-to-end through the registry + session roster."""

from openadventure.engine.tools import build_registry
from openadventure.engine.tools.registry import ToolContext
from tests.conftest import collect
from tests.test_agent_loop import text_turn


def make_ctx(workspace, campaign) -> ToolContext:
    import random

    meta = campaign.load_meta()
    return ToolContext(
        workspace=workspace,
        campaign=campaign,
        meta=meta,
        log=campaign.open_log(),
        rng=random.Random(7),
    )


def test_create_get_update_lifecycle(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)

    created = registry.dispatch(
        ctx,
        "create_sheet",
        {
            "kind": "pc",
            "name": "Kasimir Ironfoot",
            "fields": {"class": "Fighter", "level": 1},
            "resources": {"hp": {"current": 12, "max": 12}},
        },
    )
    assert created.ok
    assert "kasimir-ironfoot" in created.content

    got = registry.dispatch(ctx, "get_sheet", {"id": "kasimir-ironfoot"})
    assert got.ok and '"Fighter"' in got.content

    updated = registry.dispatch(
        ctx,
        "update_sheet",
        {
            "id": "kasimir-ironfoot",
            "ops": [
                {"op": "set", "path": "fields.level", "value": 2},
                {"op": "append", "path": "fields.inventory", "value": "healing potion"},
            ],
        },
    )
    assert updated.ok

    damaged = registry.dispatch(
        ctx, "modify_resource", {"sheet_id": "kasimir-ironfoot", "resource": "hp", "delta": -7}
    )
    assert damaged.ok and "5/12" in damaged.content

    conditioned = registry.dispatch(
        ctx, "set_conditions", {"sheet_id": "kasimir-ironfoot", "add": ["prone"]}
    )
    assert conditioned.ok and "prone" in conditioned.content

    listing = registry.dispatch(ctx, "list_sheets", {})
    assert "kasimir-ironfoot" in listing.content

    # state_change entries hit the log
    types = [e.type for e in ctx.log.read_all()]
    assert types.count("state_change") == 4


def test_modify_inventory_tracks_items_and_surfaces_in_roster(workspace, campaign, make_session):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(
        ctx,
        "create_sheet",
        {"kind": "pc", "name": "Booker", "resources": {"hp": {"current": 12, "max": 12}}},
    )

    gained = registry.dispatch(
        ctx,
        "modify_inventory",
        {"sheet_id": "booker", "add": ["Crimson cult vestments", "sealed vial of ritual blood"]},
    )
    assert gained.ok and "gained" in gained.summary

    # change an item's state in place: replace swaps the text, keeps the slot
    swapped = registry.dispatch(
        ctx,
        "modify_inventory",
        {
            "sheet_id": "booker",
            "replace": [{"old": "sealed vial of ritual blood", "new": "empty ritual vial"}],
        },
    )
    assert swapped.ok and "replaced" in swapped.summary

    from openadventure.store.sheetstore import SheetStore

    booker = SheetStore(campaign).load("booker")
    assert booker.items == ["Crimson cult vestments", "empty ritual vial"]

    # tracked items ride in the party roster every turn
    session = make_session(script=[])
    roster = session.party_roster()
    assert "Crimson cult vestments" in roster
    assert "empty ritual vial" in roster

    # both calls logged a state_change
    assert [e.type for e in ctx.log.read_all()].count("state_change") == 3  # create + 2 inventory


def test_create_folds_nested_inventory_into_items(workspace, campaign, make_session):
    # A CoC-style sheet nests starting gear under fields.gearAndPossessions and keeps a
    # separate weapons stat block. create_sheet should fold the carried gear into the
    # canonical items list (so the roster shows it) while leaving weapons intact.
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(
        ctx,
        "create_sheet",
        {
            "kind": "pc",
            "name": "Booker",
            "fields": {
                "occupation": "Private Detective",
                "gearAndPossessions": ["Notebook & pen", ".38 Revolver", "Flashlight"],
                "weapons": [{"name": ".38 Revolver", "damage": "1D10"}],
            },
            "resources": {"hp": {"current": 12, "max": 12}},
        },
    )

    from openadventure.store.sheetstore import SheetStore

    booker = SheetStore(campaign).load("booker")
    assert booker.items == ["Notebook & pen", ".38 Revolver", "Flashlight"]  # folded in
    assert booker.fields["gearAndPossessions"] == []  # emptied: items is now the source
    assert booker.fields["weapons"] == [{"name": ".38 Revolver", "damage": "1D10"}]  # untouched

    session = make_session(script=[])
    roster = session.party_roster()
    assert "Flashlight" in roster and ".38 Revolver" in roster


def test_create_folds_deeply_nested_equipment(workspace, campaign):
    # D&D nests gear under fields.equipment.weapons_and_gear alongside non-inventory
    # data (coins). Only the gear list should move into items; coins stay put.
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(
        ctx,
        "create_sheet",
        {
            "kind": "pc",
            "name": "Kasimir",
            "fields": {
                "class": "Fighter",
                "equipment": {
                    "weapons_and_gear": ["Longsword", "Shield", "Backpack"],
                    "coins": {"gp": 15},
                },
            },
            "resources": {"hp": {"current": 12, "max": 12}},
        },
    )

    from openadventure.store.sheetstore import SheetStore

    kasimir = SheetStore(campaign).load("kasimir")
    assert kasimir.items == ["Longsword", "Shield", "Backpack"]
    assert kasimir.fields["equipment"]["weapons_and_gear"] == []
    assert kasimir.fields["equipment"]["coins"] == {"gp": 15}  # non-inventory data preserved


def test_create_folds_text_inventory_and_spares_backstory(workspace, campaign, make_session):
    # CoC's derived template declares gear_possessions as a *text* field, so the GM
    # fills it with a comma-separated string. create_sheet should split it into items
    # (the list-only fold used to skip it, leaving the gear invisible), while leaving
    # the weapons stat block and the backstory's treasured_possessions prose untouched.
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(
        ctx,
        "create_sheet",
        {
            "kind": "pc",
            "name": "Malone",
            "fields": {
                "occupation": "Private Detective",
                "gear_possessions": "Flashlight, notebook, .38 revolver, lockpick set.",
                "weapons": [{"name": ".38 Revolver", "damage": "1D10"}],
                "backstory": {"treasured_possessions": "A locket, a photograph of his sister."},
            },
            "resources": {"hp": {"current": 11, "max": 11}},
        },
    )

    from openadventure.store.sheetstore import SheetStore

    malone = SheetStore(campaign).load("malone")
    assert malone.items == ["Flashlight", "notebook", ".38 revolver", "lockpick set"]
    assert malone.fields["gear_possessions"] == ""  # emptied: items is now the source
    assert malone.fields["weapons"] == [{"name": ".38 Revolver", "damage": "1D10"}]  # untouched
    # backstory prose is left intact, not split into items or cleared
    assert malone.fields["backstory"]["treasured_possessions"] == (
        "A locket, a photograph of his sister."
    )

    session = make_session(script=[])
    roster = session.party_roster()
    assert "Flashlight" in roster and ".38 revolver" in roster


def test_modify_inventory_leaves_duplicate_field_entry_alone(workspace, campaign):
    # An item can also be recorded as a structured entry elsewhere on the sheet (here a
    # weapon stat block, but any system may carry such a duplicate). modify_inventory is
    # scoped to the items list and never cascades into other fields, so the structured
    # entry is left untouched. (Which items a character has is carried by the fiction -
    # narration and canon - not by mirroring two sheet structures.) Pin that contract.
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(
        ctx,
        "create_sheet",
        {
            "kind": "pc",
            "name": "Booker",
            "fields": {
                "gearAndPossessions": [".38 Revolver"],
                "weapons": [{"name": ".38 Revolver", "damage": "1D10"}],
            },
            "resources": {"hp": {"current": 12, "max": 12}},
        },
    )

    from openadventure.store.sheetstore import SheetStore

    assert SheetStore(campaign).load("booker").items == [".38 Revolver"]  # folded in at creation

    registry.dispatch(ctx, "modify_inventory", {"sheet_id": "booker", "remove": [".38 Revolver"]})
    booker = SheetStore(campaign).load("booker")
    assert booker.items == []  # removed from the inventory list
    assert booker.fields["weapons"] == [
        {"name": ".38 Revolver", "damage": "1D10"}
    ]  # left for the GM


def test_search_sheets_recalls_by_name_and_descriptor(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(
        ctx,
        "create_sheet",
        {
            "kind": "npc",
            "name": "Mr. Dooley",
            "fields": {"occupation": "Cigar and newspaper vendor", "attitude": "Friendly"},
        },
    )
    registry.dispatch(
        ctx,
        "create_sheet",
        {"kind": "npc", "name": "Steven Knott", "fields": {"occupation": "Landlord"}},
    )

    # recall by name: the top hit comes back as the FULL sheet inline, so the GM can
    # act in one turn without a second get_sheet
    by_name = registry.dispatch(ctx, "search_sheets", {"query": "Dooley"})
    assert by_name.ok
    assert "mr-dooley" in by_name.content
    assert "Cigar and newspaper vendor" in by_name.content  # full sheet inlined
    assert "Steven Knott" not in by_name.content

    # recall by descriptor: a field match, not just the name
    by_desc = registry.dispatch(ctx, "search_sheets", {"query": "cigar vendor"})
    assert by_desc.ok and "Mr. Dooley" in by_desc.content

    # kind filter + a miss returns no results, not a false match
    miss = registry.dispatch(ctx, "search_sheets", {"query": "dragon", "kind": "npc"})
    assert "No characters matched" in miss.content


def test_dead_pc_and_replacement_roster(workspace, campaign, make_session):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)

    registry.dispatch(
        ctx,
        "create_sheet",
        {"kind": "pc", "name": "Old Hero", "resources": {"hp": {"current": 0, "max": 10}}},
    )
    registry.dispatch(
        ctx,
        "update_sheet",
        {"id": "old-hero", "ops": [{"op": "set", "path": "status", "value": "dead"}]},
    )
    registry.dispatch(
        ctx,
        "create_sheet",
        {
            "kind": "pc",
            "name": "Fresh Face",
            "fields": {"class": "Rogue", "level": 1},
            "resources": {"hp": {"current": 9, "max": 9}},
        },
    )
    registry.dispatch(
        ctx,
        "create_sheet",
        {"kind": "monster", "name": "Goblin", "resources": {"hp": {"current": 7, "max": 7}}},
    )

    session = make_session(script=[])
    roster = session.party_roster()
    assert "Fresh Face" in roster
    assert "Old Hero" not in roster  # dead PCs drop off the roster
    assert "Goblin" not in roster  # monsters aren't party

    # both sheets still exist on disk
    from openadventure.store.sheetstore import SheetStore

    store = SheetStore(campaign)
    assert store.load("old-hero").status == "dead"
    assert store.load("fresh-face").status == "active"


def test_companion_npc_rides_in_context_across_moves(workspace, campaign, make_session):
    from openadventure.store import snapshots

    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)

    registry.dispatch(
        ctx,
        "create_sheet",
        {"kind": "npc", "name": "Brother Kael", "fields": {"attitude": "loyal"}},
    )
    registry.dispatch(
        ctx,
        "create_sheet",
        {"kind": "npc", "name": "Random Guard", "fields": {"attitude": "bored"}},
    )
    # Mark Kael as traveling with the party; the guard stays a plain off-stage NPC.
    registry.dispatch(
        ctx,
        "update_sheet",
        {"id": "brother-kael", "ops": [{"op": "set", "path": "companion", "value": True}]},
    )
    # The party moves to a new location and the GM stages no one.
    registry.dispatch(ctx, "update_scene", {"location": "The high pass", "npcs_present": []})

    session = make_session(script=[])
    npcs = session.staged_npcs(snapshots.load_json(campaign.scene_path))
    assert npcs is not None
    assert "Brother Kael" in npcs and "with the party" in npcs

    # the context blocks only (head + foot), not the history: "Random Guard" appears
    # in a creation engine note in the tail, but must not be staged in the context.
    msgs = session.build_messages()[0]
    context = "\n".join(b.text for m in (msgs[0], msgs[-1]) for b in m.content if b.type == "text")
    assert "Brother Kael" in context  # companion persists without being staged
    assert "Random Guard" not in context  # a non-companion, unstaged NPC does not

    # /party surfaces companions in their own section; the plain NPC stays off it
    companions = session.companion_roster()
    assert companions is not None and "Brother Kael" in companions
    assert "Random Guard" not in companions
    assert session.party_roster() is None  # companions are not party (PCs only)


def test_unstaged_named_npc_is_surfaced_for_restaging(workspace, campaign, make_session):
    from openadventure.store import snapshots

    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(
        ctx,
        "create_sheet",
        {"kind": "npc", "name": "Mr. Dooley", "fields": {"occupation": "vendor"}},
    )
    registry.dispatch(
        ctx, "create_sheet", {"kind": "npc", "name": "Gabriela Macario", "fields": {}}
    )
    # Return to a scene that names Dooley in the description but stages no one.
    registry.dispatch(
        ctx,
        "update_scene",
        {"location": "French Hill", "description": "Dooley at his cart, hawking papers."},
    )

    session = make_session(script=[])
    scene = snapshots.load_json(campaign.scene_path)
    hint = session.unstaged_scene_npcs(scene)
    assert hint is not None
    assert "mr-dooley" in hint  # the id the GM needs to restage, surfaced
    assert "gabriela-macario" not in hint  # not named in the scene, so not surfaced

    context = "\n".join(
        b.text for m in session.build_messages()[0] for b in m.content if b.type == "text"
    )
    assert "Possible NPCs to stage" in context
    assert "Mr. Dooley (id mr-dooley)" in context

    # once staged, the hint stops (no nag)
    registry.dispatch(ctx, "update_scene", {"npcs_present": ["mr-dooley"]})
    assert session.unstaged_scene_npcs(snapshots.load_json(campaign.scene_path)) is None


def test_duplicate_names_get_suffixes(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    for _ in range(3):
        registry.dispatch(
            ctx,
            "create_sheet",
            {"kind": "monster", "name": "Goblin", "resources": {"hp": {"current": 7, "max": 7}}},
        )
    listing = registry.dispatch(ctx, "list_sheets", {"kind": "monster"})
    assert "goblin" in listing.content
    assert "goblin-2" in listing.content
    assert "goblin-3" in listing.content


async def test_roster_reaches_the_provider(make_session, workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(
        ctx,
        "create_sheet",
        {
            "kind": "pc",
            "name": "Kasimir",
            "fields": {"class": "Fighter", "level": 1},
            "resources": {"hp": {"current": 12, "max": 12}},
        },
    )
    session = make_session(script=[text_turn("Onward!")])
    await collect(session.handle_input("hello"))
    context_text = "\n".join(
        b.text for m in session.provider.calls[0].messages for b in m.content if b.type == "text"
    )
    assert "Kasimir" in context_text
    assert "hp 12/12" in context_text


def _write_template(workspace, source: str) -> dict:
    from openadventure.store import snapshots

    template = {
        "name": f"{source}/character",
        "version": 1,
        "fields": [
            {"path": "fields.class", "type": "str", "description": "Class", "example": "Fighter"}
        ],
        "resources": [{"name": "hp", "description": "Hit points"}],
        "creation_guide": "1. Roll 4d6kh3 for each ability score.",
    }
    snapshots.save_json(workspace.book_dir(source) / "templates" / "character.json", template)
    return template


def test_template_lands_in_system_prompt(make_session, workspace, campaign):
    meta = campaign.load_meta()
    meta.sources = ["dnd5e"]
    meta.system_source = "dnd5e"
    campaign.save_meta(meta)
    _write_template(workspace, "dnd5e")

    session = make_session(script=[])
    system = session.build_system()[0].text
    assert "Character template" in system
    assert "creation_guide" in system
    assert "4d6kh3" in system
    # The guide-adherence reminder is generic, not bound to any one system's terms.
    assert "before calling create_sheet" in system
    assert "FINAL values" in system
    # A v1 template with no advancement guide draws no advancement prose.
    assert "advancement_guide" not in system


def test_advancement_guide_lands_in_system_prompt(make_session, workspace, campaign):
    from openadventure.store import snapshots

    meta = campaign.load_meta()
    meta.sources = ["dnd5e"]
    meta.system_source = "dnd5e"
    campaign.save_meta(meta)
    template = {
        "name": "dnd5e/character",
        "version": 2,
        "fields": [{"path": "fields.level", "type": "int", "description": "Level", "example": 1}],
        "resources": [{"name": "hp", "description": "Hit points"}],
        "creation_guide": "1. Roll 4d6kh3 for each ability score.",
        "advancement_guide": "1. On level up, roll your class hit die for new HP.",
    }
    snapshots.save_json(workspace.book_dir("dnd5e") / "templates" / "character.json", template)

    session = make_session(script=[])
    system = session.build_system()[0].text
    # Both the JSON key and the prose telling the GM how to use it appear.
    assert "advancement_guide" in system
    assert "levels up" in system
    assert "above 1st level" in system


def test_pc_sheet_stamped_with_system_template_name(workspace, campaign):
    """A PC created without an explicit template arg is stamped with the
    campaign's character-template name: the GM follows the template but routinely
    omits the optional arg, so the handler records provenance for it."""
    from openadventure.store.sheetstore import SheetStore

    meta = campaign.load_meta()
    meta.sources = ["dnd5e"]
    meta.system_source = "dnd5e"
    campaign.save_meta(meta)
    _write_template(workspace, "dnd5e")

    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    store = SheetStore(campaign)

    registry.dispatch(ctx, "create_sheet", {"kind": "pc", "name": "Aria"})
    assert store.load("aria").template == "dnd5e/character"

    # monsters/NPCs are never stamped with the PC template
    registry.dispatch(ctx, "create_sheet", {"kind": "monster", "name": "Goblin"})
    assert store.load("goblin").template is None

    # an explicit template arg is honored, never overwritten
    registry.dispatch(
        ctx, "create_sheet", {"kind": "pc", "name": "Imported", "template": "homebrew/v2"}
    )
    assert store.load("imported").template == "homebrew/v2"


def test_pc_sheet_template_null_without_a_system_template(workspace, campaign):
    """No derived template -> nothing to stamp, template stays null."""
    from openadventure.store.sheetstore import SheetStore

    meta = campaign.load_meta()
    meta.sources = ["coc7e"]
    meta.system_source = "coc7e"  # set, but never derived
    campaign.save_meta(meta)

    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    registry.dispatch(ctx, "create_sheet", {"kind": "pc", "name": "Nobody"})
    assert SheetStore(campaign).load("nobody").template is None


def test_no_template_section_when_underived(make_session, campaign):
    """With no shipped baseline, a source whose template isn't derived yet adds
    no character-template section to the system prompt."""
    meta = campaign.load_meta()
    meta.sources = ["coc7e"]  # set, but never derived
    meta.system_source = "coc7e"
    campaign.save_meta(meta)

    session = make_session(script=[])
    system = session.build_system()[0].text
    assert "Character template" not in system


def _ingest_marker(workspace, source: str) -> None:
    """Minimal on-disk footprint so pipeline.is_ingested() passes in a test."""
    source_dir = workspace.book_dir(source)
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (source_dir / "index.sqlite").write_text("", encoding="utf-8")


async def test_play_does_not_auto_derive_a_template(make_session, workspace, campaign):
    """Templates are optional: playing an ingested-but-underived source no longer
    auto-generates one (it's created out of band by `openadventure template`)."""
    meta = campaign.load_meta()
    meta.sources = ["coc7e"]
    meta.system_source = "coc7e"
    campaign.save_meta(meta)
    _ingest_marker(workspace, "coc7e")
    template_path = workspace.book_dir("coc7e") / "templates" / "character.json"
    assert not template_path.is_file()

    # A single scripted game turn. If play still derived, the FakeProvider would
    # need an extra script entry for the derivation turn (or write the file).
    session = make_session(script=[text_turn("You awaken in Arkham.")])
    await collect(session.handle_input("begin"))

    assert not template_path.is_file()  # nothing was derived
    assert "Character template" not in session.build_system()[0].text


def test_has_character_template_reflects_disk(make_session, workspace, campaign):
    meta = campaign.load_meta()
    meta.sources = ["coc7e"]
    meta.system_source = "coc7e"
    campaign.save_meta(meta)
    session = make_session(script=[])

    assert session.has_character_template() is False  # source set, no template

    _write_template(workspace, "coc7e")
    assert session.has_character_template() is True  # now present on disk


def test_has_character_template_false_without_a_source(make_session, campaign):
    meta = campaign.load_meta()
    meta.sources = []
    meta.system_source = None
    campaign.save_meta(meta)
    session = make_session(script=[])

    assert session.has_character_template() is False
