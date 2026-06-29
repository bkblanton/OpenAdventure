"""Cross-reference graph: entity registry, edge extraction, read-time expansion."""

from openadventure.engine.tools import build_registry
from openadventure.ingest import pipeline, xref
from tests.test_sheet_tools import make_ctx

# A source where an encounter section names a monster defined elsewhere. The
# stat block markers let classify_kind tag the Goblin section as a monster even
# though markdown carries no font geometry.
BESTIARY_MD = """\
# Bestiary

## Goblin

Small humanoid (goblinoid), neutral evil.
Armor Class 15 (leather armor, shield)
Hit Points 7 (2d6)
Actions. Scimitar. Melee Weapon Attack.

## Fire Elemental

Large elemental, neutral.
Armor Class 13
Hit Points 102 (12d10 + 36)

# Encounters

## Cragmaw Ambush

Three goblins leap from the bushes. If pressed, they summon a fire elemental.

## Quiet Glade

A peaceful clearing. Nothing stirs here.
"""


def _rows():
    return [
        (
            "Goblin",
            "Bestiary > Goblin",
            "Armor Class 15\nHit Points 7",
            "bestiary/goblin.md",
            "monster",
        ),
        ("Owlbear", "Bestiary > Owlbear", "no stats here", "bestiary/owlbear.md", "section"),
        (
            "Magic Missile",
            "Spells > Magic Missile",
            "Casting Time: 1 action\nRange: 120 feet\nComponents: V, S\nDuration: Instant",
            "spells/magic-missile.md",
            "spell",
        ),
        (
            "Ambush",
            "Encounters > Ambush",
            "Two goblins attack, then cast magic missile. The owlbear watches.",
            "encounters/ambush.md",
            "section",
        ),
    ]


def test_classify_kind_from_body():
    assert xref.classify_kind("Goblin", "Armor Class 15\nHit Points 7 (2d6)") == "monster"
    # abbreviated stat-block form (5.2 SRD): line-anchored AC/HP + number
    assert xref.classify_kind("Goblin Warrior", "Goblinoid\nAC 15\nHP 11 (2d6+4)") == "monster"
    # OCR-to-markdown stat block: list-marker + bold markup around the labels
    assert (
        xref.classify_kind(
            "Eelfolk Hunter", "- **Armor Class** 13 (hide armor)\n- **Hit Points** 37 (7d8 + 6)"
        )
        == "monster"
    )
    assert (
        xref.classify_kind(
            "Fireball", "Casting Time: 1 action\nComponents: V, S\nRange: 150 ft\nDuration: Instant"
        )
        == "spell"
    )
    assert xref.classify_kind("Foreword", "Welcome to the game, traveler.") is None
    # prose mentioning AC/HP mid-sentence must NOT look like a stat block
    assert xref.classify_kind("Combat", "Your AC is 10 and you regain HP on a rest.") is None


def test_classify_kind_dnd4e_power():
    # 4e power: usage tier + 4e action line + a stat-block label. Shares no
    # vocabulary with 5e spells, so it needs its own profile.
    power = (
        "Shape Magic\nArchmage Utility 26\nDaily\nStandard Action Personal\n"
        "Effect: You regain one arcane power you have already used."
    )
    assert xref.classify_kind("Archmage Power", power) == "spell"
    attack_power = (
        "Magic Missile\nWizard Attack 1\nAt-Will * Arcane, Force, Implement\n"
        "Standard Action Ranged 20\nTarget: One creature\nHit: 2d4 + Int force damage."
    )
    assert xref.classify_kind("Level 1 At-Will Spells", attack_power) == "spell"
    # prose that names a power in passing lacks the action + label cluster
    assert xref.classify_kind("Powers", "Daily powers are your most potent options.") is None


def test_classify_kind_gurps_and_coc():
    # GURPS Magic spell: cluster of GURPS labels
    gurps_spell = (
        "Regular; Resisted by Will\nLets the caster move objects.\n"
        "Duration: 1 minute.\nCost: 2.\nTime to cast: 1 second.\nPrerequisite: Apportation."
    )
    assert xref.classify_kind("Apportation", gurps_spell) == "spell"
    # Call of Cthulhu creature stat block
    coc_creature = (
        "STR 65 CON 80 SIZ 90\nHit Points: 17\nMagic Points: 13\n"
        "Damage Bonus: +1D6\nBuild: 2\nSanity Loss: 1D6 to see the beast."
    )
    assert xref.classify_kind("Hunting Horror", coc_creature) == "monster"
    # Call of Cthulhu spell
    coc_spell = "Cost: 8 magic points\nCasting time: 5 rounds\nSummons a servitor."
    assert xref.classify_kind("Contact Deity", coc_spell) == "spell"
    # a GURPS skills chapter that mentions spell costs in passing isn't a spell
    assert xref.classify_kind("Skills", "Most skills have a default. Duration varies.") is None


def test_entity_title_filter_drops_headers_and_page_artifacts():
    rows = [
        # real GURPS-style spell -> kept
        (
            "Lightning",
            "x",
            "Duration: instant.\nCost: 2.\nTime to cast: 1 second.\nPrerequisite: y",
            "magic/lightning.md",
            "section",
        ),
        # ALL-CAPS chapter header that classifies as spell -> dropped
        (
            "MAGIC",
            "x",
            "Duration: see below.\nCost: varies.\nPrerequisite: none.\nTime to cast: special",
            "magic/magic.md",
            "section",
        ),
        # page-number-tagged header -> dropped
        (
            "190 SKILLS",
            "x",
            "Duration: 1 day.\nCost: 1.\nPrerequisite: a.\nResisted by HT",
            "skills.md",
            "section",
        ),
    ]
    names = {e.name for e in xref.build_entities(rows)}
    assert names == {"lightning"}


def test_build_entities_uses_kind_and_heuristic():
    entities = {e.name: e for e in xref.build_entities(_rows())}
    assert set(entities) == {"goblin", "magic missile"}  # owlbear has no stats -> not an entity
    assert entities["goblin"].kind == "monster"
    assert entities["magic missile"].kind == "spell"
    assert entities["magic missile"].path == "spells/magic-missile.md"


def test_build_entities_drops_ambiguous_names():
    rows = [
        ("Goblin", "A > Goblin", "Armor Class 1\nHit Points 1", "a/goblin.md", "monster"),
        ("Goblin", "B > Goblin", "Armor Class 2\nHit Points 2", "b/goblin.md", "monster"),
    ]
    assert xref.build_entities(rows) == []  # a name pointing two places can't resolve


def test_build_edges_links_plural_and_multiword_skips_self():
    rows = _rows()
    entities = xref.build_entities(rows)
    edges = xref.build_edges(rows, entities)
    by_src: dict[str, set[str]] = {}
    for src, dst, _name, _kind in edges:
        by_src.setdefault(src, set()).add(dst)
    # "goblins" (plural) and "magic missile" (multiword) both resolve from the encounter
    assert by_src["encounters/ambush.md"] == {"spells/magic-missile.md", "bestiary/goblin.md"}
    # a section never links to itself
    assert "bestiary/goblin.md" not in by_src.get("bestiary/goblin.md", set())


def test_write_and_query_references(tmp_path):
    rows = _rows()
    db = tmp_path / xref.XREF_NAME
    entity_count, edge_count = xref.build(db, rows)
    assert entity_count == 2 and edge_count >= 2
    refs = xref.references_for(db, "encounters/ambush.md")
    names = [r.name for r in refs]
    assert names == ["goblin", "magic missile"]  # monster before spell, then by name
    # graceful when there's no graph
    assert xref.references_for(tmp_path / "nope.sqlite", "x") == []


def test_read_rules_inlines_referenced_statblock(workspace, campaign, tmp_path):
    source = tmp_path / "rules.md"
    source.write_text(BESTIARY_MD, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir("dnd5e"))
    meta = campaign.load_meta()
    meta.sources = ["dnd5e"]
    meta.system_source = "dnd5e"
    campaign.save_meta(meta)

    registry = build_registry(workspace, campaign, meta)
    ctx = make_ctx(workspace, campaign)

    out = registry.dispatch(ctx, "read_rules", {"section_path": "encounters/cragmaw-ambush.md"})
    assert out.ok
    assert "Referenced entries:" in out.content
    # the goblin stat block is pulled in without a second search
    assert "Armor Class 15" in out.content
    assert "Hit Points 7" in out.content

    # search surfaces the reference names as a hint
    hits = registry.dispatch(ctx, "search_rules", {"query": "cragmaw ambush"})
    assert "references:" in hits.content and "goblin" in hits.content.lower()


def test_search_rules_inlines_top_hit_body(workspace, campaign, tmp_path):
    source = tmp_path / "rules.md"
    source.write_text(BESTIARY_MD, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir("dnd5e"))
    meta = campaign.load_meta()
    meta.sources = ["dnd5e"]
    meta.system_source = "dnd5e"
    campaign.save_meta(meta)

    registry = build_registry(workspace, campaign, meta)
    ctx = make_ctx(workspace, campaign)

    # the top hit's full section body is returned inline, so the GM can act on
    # the goblin stat block without a second read_rules round-trip
    hits = registry.dispatch(ctx, "search_rules", {"query": "goblin"})
    assert hits.ok
    assert "Armor Class 15" in hits.content
    # the body's last line, well beyond any 12-token FTS snippet, proves the
    # whole section is inlined, not just the matched excerpt
    assert "Melee Weapon Attack" in hits.content
    # the path is shown, namespaced by the source slug even though it's the only
    # source, so the GM always knows which book (and system) the rule came from
    assert "dnd5e/bestiary/goblin.md" in hits.content


def test_kind_roundtrips_and_reindex_rebuilds_xref(tmp_path):
    source = tmp_path / "rules.md"
    source.write_text(BESTIARY_MD, encoding="utf-8")
    dest = tmp_path / "source"
    pipeline.ingest(source, dest)

    # kind is persisted in frontmatter and read back
    goblin_file = next((dest / "sections").rglob("goblin.md"))
    fields, _ = pipeline.parse_section_frontmatter(goblin_file.read_text(encoding="utf-8"))
    assert fields["kind"] == "monster"

    # ingest built the graph; reindex rebuilds it from the markdown alone
    assert (dest / xref.XREF_NAME).is_file()
    refs = xref.references_for(dest / xref.XREF_NAME, "encounters/cragmaw-ambush.md")
    assert {r.name for r in refs} == {"goblin", "fire elemental"}

    (dest / xref.XREF_NAME).unlink()
    pipeline.reindex(dest)
    refs = xref.references_for(dest / xref.XREF_NAME, "encounters/cragmaw-ambush.md")
    assert {r.name for r in refs} == {"goblin", "fire elemental"}
