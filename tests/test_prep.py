"""Scene prep: the active keyed location's canonical text rides in context."""

from openadventure.engine.prep import location_prep
from openadventure.engine.prompts import build_context_block
from openadventure.ingest import pipeline
from openadventure.store import snapshots
from tests.conftest import collect
from tests.test_agent_loop import text_turn

# An adventure module (rooms/NPCs/plot) plus a stat block its room references,
# so prep can show both the keyed text and the inlined creature.
MODULE_MD = """\
# Death House

## Rose and Thorn

Read-aloud: Two children stand in the street. "There's a monster in our house!"

The children are illusions. A ghoul lurks in the cellar below.

## Ghoul

Medium undead, chaotic evil.
Armor Class 12
Hit Points 22 (5d8)
Actions. Claws. Melee Weapon Attack.

## Secret Stairs

A hidden staircase in the attic leads down to the dungeon level.
"""


def _ingest_module(workspace, tmp_path):
    source = tmp_path / "death-house.md"
    source.write_text(MODULE_MD, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir("death-house"))


def test_location_prep_returns_canonical_text(workspace, tmp_path):
    _ingest_module(workspace, tmp_path)
    prep = location_prep(workspace, "death-house/rose-and-thorn.md")
    assert prep is not None
    assert "There's a monster in our house" in prep  # the keyed room text
    assert "death-house/rose-and-thorn.md" in prep  # path header for grounding


def test_location_prep_inlines_referenced_statblock(workspace, tmp_path):
    _ingest_module(workspace, tmp_path)
    prep = location_prep(workspace, "death-house/rose-and-thorn.md")
    # the room mentions a ghoul; its stat block is pulled in
    assert "Armor Class 12" in prep
    assert "Hit Points 22" in prep


def test_location_prep_none_when_unset_or_unresolvable(workspace, tmp_path):
    _ingest_module(workspace, tmp_path)
    assert location_prep(workspace, None) is None
    assert location_prep(workspace, "") is None
    assert location_prep(workspace, "death-house/no-such-room.md") is None
    assert location_prep(workspace, "rose-and-thorn.md") is None  # needs <module>/ prefix


def test_location_prep_stitches_extra_paths(workspace, tmp_path):
    _ingest_module(workspace, tmp_path)
    # a location described across two sections: the keyed room plus its secret stairs
    prep = location_prep(
        workspace,
        "death-house/rose-and-thorn.md",
        ["death-house/secret-stairs.md"],
    )
    assert prep is not None
    assert "There's a monster in our house" in prep  # primary section
    assert "hidden staircase in the attic" in prep  # stitched-in extra section
    assert "death-house/secret-stairs.md" in prep  # path header for the extra section


def test_location_prep_dedupes_and_skips_unresolvable_extras(workspace, tmp_path):
    _ingest_module(workspace, tmp_path)
    # a repeated path and an unresolvable one don't duplicate or break the prep
    prep = location_prep(
        workspace,
        "death-house/rose-and-thorn.md",
        ["death-house/rose-and-thorn.md", "death-house/no-such-room.md"],
    )
    assert prep is not None
    assert prep.count("There's a monster in our house") == 1


BIG_MODULE_MD = """\
# Big Dungeon

## Room One

Body one with phrase BODYONE.

## Room Two

Body two with phrase BODYTWO.

## Room Three

Body three with phrase BODYTHREE.

## Room Four

Body four with phrase BODYFOUR.

## Room Five

Body five with phrase BODYFIVE.
"""


def test_location_prep_tiers_full_bodies_then_pointers(workspace, tmp_path):
    source = tmp_path / "big.md"
    source.write_text(BIG_MODULE_MD, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir("big"))
    prep = location_prep(
        workspace,
        "big/room-one.md",
        ["big/room-two.md", "big/room-three.md", "big/room-four.md", "big/room-five.md"],
    )
    assert prep is not None
    # the first PREP_FULL_SECTIONS (3) are inlined in full: module_path + two extras
    assert "BODYONE" in prep and "BODYTWO" in prep and "BODYTHREE" in prep
    # the rest become read_campaign pointers: path listed, body NOT inlined
    assert "Related sections" in prep
    assert "room-four.md" in prep and "room-five.md" in prep
    assert "BODYFOUR" not in prep and "BODYFIVE" not in prep


def test_location_prep_char_budget_demotes_to_pointers(workspace, tmp_path):
    source = tmp_path / "big.md"
    source.write_text(BIG_MODULE_MD, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir("big"))
    # a tiny budget fits only the primary section in full; the rest become pointers
    prep = location_prep(
        workspace,
        "big/room-one.md",
        ["big/room-two.md", "big/room-three.md"],
        char_budget=40,
    )
    assert prep is not None
    assert "BODYONE" in prep  # primary section always kept, even over budget
    assert "BODYTWO" not in prep and "BODYTHREE" not in prep  # demoted, not inlined
    assert "Related sections" in prep
    assert "room-two.md" in prep and "room-three.md" in prep


def test_location_prep_extras_only_when_module_path_missing(workspace, tmp_path):
    _ingest_module(workspace, tmp_path)
    # extra_paths still prep even if module_path itself is unset
    prep = location_prep(workspace, None, ["death-house/secret-stairs.md"])
    assert prep is not None
    assert "hidden staircase in the attic" in prep


def test_long_body_is_truncated_with_pointer(workspace, tmp_path):
    big = "# Big Module\n\n## Sprawl\n\n" + ("words " * 2000)
    source = tmp_path / "big.md"
    source.write_text(big, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir("big"))
    prep = location_prep(workspace, "big/sprawl.md")
    assert "truncated" in prep and "read_campaign" in prep


def test_build_context_block_renders_prepped_location():
    out = build_context_block(
        _meta(), location_prep="death-house/rose-and-thorn.md:\nTwo children stand..."
    )
    assert "## Prepped location" in out
    assert "Two children stand" in out
    # the read-aloud vs GM-only split is spelled out so secrets aren't leaked
    assert "read-aloud" in out.lower() and "GM-only" in out
    # absent when there's nothing to prep
    assert "## Prepped location" not in build_context_block(_meta())


def test_build_context_block_renders_scene_notes():
    out = build_context_block(
        _meta(),
        scene={
            "location": "Hall of Mirrors",
            "prep_notes": "Loot table (reconstructed): 1-3 gold ring, 4-6 silver dagger.",
            "extra_paths": ["mod/extra.md"],
        },
    )
    assert "## Scene notes" in out
    assert "Loot table (reconstructed)" in out
    # plumbing keys never echo inline in the scene block
    assert "extra_paths" not in out
    assert "prep_notes:" not in out
    # absent when there are no notes
    assert "## Scene notes" not in build_context_block(_meta(), scene={"location": "X"})


def test_build_context_block_renders_scene_secrets():
    out = build_context_block(
        _meta(),
        scene={
            "location": "Crypt",
            "hidden_notes": "A ghoul waits behind the sarcophagus to ambush.",
        },
    )
    assert "## Scene secrets" in out
    assert "ghoul waits behind the sarcophagus" in out
    # the GM-only, reveal-through-play framing rides with it
    assert "GM-only" in out and "reveal through play" in out.lower()
    # the plumbing key never echoes inline in the scene block
    assert "hidden_notes:" not in out
    # absent when there are no secrets
    assert "## Scene secrets" not in build_context_block(_meta(), scene={"location": "X"})


async def test_prep_rides_in_session_context(make_session, workspace, campaign, tmp_path):
    _ingest_module(workspace, tmp_path)
    # the party is standing in a keyed location
    snapshots.save_json(
        campaign.scene_path,
        {"location": "Death House porch", "module_path": "death-house/rose-and-thorn.md"},
    )
    session = make_session(script=[text_turn("You approach the house.")])
    session.reload_tools()
    await collect(session.handle_input("we go in"))
    context = "\n".join(
        b.text for m in session.provider.calls[0].messages for b in m.content if b.type == "text"
    )
    assert "## Prepped location" in context
    assert "There's a monster in our house" in context  # canonical room text pre-loaded


def _meta():
    from openadventure.store.workspace import CampaignMeta

    return CampaignMeta(name="T", slug="t")
