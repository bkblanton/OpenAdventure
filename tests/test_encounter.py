"""Encounter mechanics: initiative ties, turn order, round wrap, skip-inactive."""

from openadventure.mechanics.encounter import Combatant, Encounter, next_turn, sort_initiative


def make_encounter() -> Encounter:
    return Encounter(
        name="Test fight",
        combatants=[
            Combatant(tag="Kasimir", side="party", initiative=17),
            Combatant(tag="Goblin 1", side="foe", initiative=12),
            Combatant(tag="Goblin 2", side="foe", initiative=20),
            Combatant(tag="Mirena", side="party", initiative=12),
        ],
    )


def test_sort_descending_with_stable_ties():
    enc = sort_initiative(make_encounter())
    assert [c.tag for c in enc.combatants] == ["Goblin 2", "Kasimir", "Goblin 1", "Mirena"]
    assert enc.current().tag == "Goblin 2"


def test_next_turn_and_round_wrap():
    enc = sort_initiative(make_encounter())
    order = []
    for _ in range(5):
        enc, current = next_turn(enc)
        order.append((current.tag, enc.round))
    assert order == [
        ("Kasimir", 1),
        ("Goblin 1", 1),
        ("Mirena", 1),
        ("Goblin 2", 2),  # wrapped into round 2
        ("Kasimir", 2),
    ]


def test_skip_inactive():
    enc = sort_initiative(make_encounter())
    enc.find("Kasimir").active = False
    enc.find("Goblin 1").active = False
    enc, current = next_turn(enc)
    assert current.tag == "Mirena"
    enc, current = next_turn(enc)
    assert current.tag == "Goblin 2"
    assert enc.round == 2


def test_all_inactive_is_safe():
    enc = sort_initiative(make_encounter())
    for combatant in enc.combatants:
        combatant.active = False
    enc, current = next_turn(enc)
    assert current is None
    assert enc.current() is None or not enc.current().active


def test_sort_skips_leading_inactive():
    enc = make_encounter()
    enc.find("Goblin 2").active = False
    enc = sort_initiative(enc)
    assert enc.current().tag == "Kasimir"


def test_find_is_case_insensitive():
    enc = make_encounter()
    assert enc.find("goblin 2").initiative == 20
