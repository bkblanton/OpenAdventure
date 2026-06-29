"""Borderless-table reconstruction from positioned words (ingest.tables)."""

from openadventure.ingest.tables import (
    _looks_corrupted,
    find_borderless_tables,
    repair_merged_rows,
)


def _w(x: float, y: float, text: str):
    """A positioned word; width scales with text length so long prose tokens
    occupy real horizontal space (the detector reasons about x-extents)."""
    return (x, y, x + max(8.0, len(text) * 5.0), y + 8.0, text)


def test_detects_simple_numeric_table():
    data = [("1", "-5", "18"), ("2", "-4", "19"), ("3", "-3", "20"), ("4", "-2", "21")]
    words = []
    for i, (a, b, c) in enumerate(data):
        y = 10 + i * 12
        words += [_w(10, y, a), _w(60, y, b), _w(110, y, c)]
    tables = find_borderless_tables(words)
    assert len(tables) == 1
    assert tables[0].rows[0] == ["1", "-5", "18"]
    assert len(tables[0].rows) == 4


def test_prose_is_not_a_table():
    # one word per line: a single column, never enough to be a table
    words = [_w(10, 10 + i * 12, "paragraph") for i in range(6)]
    assert find_borderless_tables(words) == []


def test_adjacent_prose_column_is_stripped():
    # a long prose column on the left shares rows with a 3-column numeric table on
    # the right; the empty lane / prose-edge trim must drop it
    words = []
    nums = [("1", "5", "9"), ("2", "6", "8"), ("3", "7", "7"), ("4", "8", "6")]
    for i, (a, b, c) in enumerate(nums):
        y = 10 + i * 12
        words.append(_w(10, y, f"longprosetokenthatwraps{i:02d}"))
        words += [_w(200, y, a), _w(250, y, b), _w(300, y, c)]
    tables = find_borderless_tables(words)
    assert len(tables) == 1
    assert len(tables[0].rows[0]) == 3
    assert tables[0].rows[0] == ["1", "5", "9"]
    assert all("longprose" not in cell for row in tables[0].rows for cell in row)


def test_numeric_gate_rejects_text_only_alignment():
    # three aligned text columns with no numeric data (an index / glossary block)
    data = [("alpha", "bravo", "delta"), ("echo", "foxtrot", "golf"), ("hotel", "india", "juliet")]
    words = []
    for i, (a, b, c) in enumerate(data):
        y = 10 + i * 12
        words += [_w(10, y, a), _w(80, y, b), _w(150, y, c)]
    assert find_borderless_tables(words) == []


def test_corruption_gate_rejects_jammed_and_soup_tables():
    # two side-by-side tables merged: Lvl+Name jammed into one cell
    jammed = [
        ["18 Thundering +4", "85,000", "Any", "25 Flaming +5", "625,000", "Any"],
        ["19 Dragonslayer +4", "105,000", "Any", "25 Holy Avenger +5", "625,000", "Axe"],
        ["20 Berserker +4", "125,000", "Axe", "27 Resounding +6", "1,625,000", "Flail"],
    ]
    assert _looks_corrupted(jammed)
    # a stat block's mini-table fused with prose into long digit-dense soup
    soup = [
        ["Lvl 5", "+1", "1,000 gp Lvl 20 +4 125,000 gp Enhancement: AC"],
        ["Lvl 10", "+2", "5,000 gp Lvl 25 +5 625,000 gp Property: when bloodied"],
        ["Lvl 15", "+3", "25,000 gp Lvl 30 +6 3,125,000 gp to AC and saves"],
    ]
    assert _looks_corrupted(soup)


def test_corruption_gate_keeps_clean_tables():
    # a numeric table with a legit prose column (Character Advancement shape)
    clean = [
        ["1,000", "2nd", "—", "gain 1 utility power; gain 1 feat", "2", "2/1/1/1"],
        ["2,250", "3rd", "—", "gain 1 encounter attack power", "2", "2/2/1/1"],
        ["3,750", "4th", "—", "gain 1 feat", "3", "2/2/1/1"],
    ]
    assert not _looks_corrupted(clean)
    # value+unit cells are not jams: lowercase unit ("1 gp", "3 lb.") OR an
    # all-caps currency code ("4 CP", "50 GP") -- only a Capital+lowercase Name is
    weapons = [["Club", "+2", "1d6", "1 gp", "3 lb."], ["Mace", "+2", "1d8", "5 gp", "6 lb."]]
    assert not _looks_corrupted(weapons)
    prices = [["Ale (mug)", "4 CP"], ["Bread (loaf)", "2 CP"], ["Wealthy", "2 GP"]]
    assert not _looks_corrupted(prices)


def test_repair_resplits_alternating_merge():
    # a d100 + description table whose even rows jammed into the first cell
    rows = [
        ["01", "A mummified goblin hand"],
        ["02 A crystal that faintly glows in moonlight", ""],
        ["03", "A gold coin minted in an unknown land"],
        ["04 A diary written in a language you don't know", ""],
    ]
    out = repair_merged_rows(rows)
    assert out[1] == ["02", "A crystal that faintly glows in moonlight"]
    assert out[3] == ["04", "A diary written in a language you don't know"]
    # untouched rows are unchanged, and no token is lost
    assert out[0] == rows[0]
    for before, after in zip(rows, out, strict=True):
        assert " ".join(before).split() == " ".join(after).split()


def test_repair_is_idempotent():
    rows = [
        ["1", "Protection from Evil and Good"],
        ["5 Aid, Zone of Truth", ""],
        ["9", "Beacon of Hope, Dispel Magic"],
    ]
    once = repair_merged_rows(rows)
    assert repair_merged_rows(once) == once


def test_repair_leaves_variable_width_tables_untouched():
    # spell list: name (variable words) + school + concentration/material flag.
    # token allocation is ambiguous here, so the repair must not touch it.
    spells = [
        ["Detect Magic", "Divination", "C, R"],
        ["Chill Touch Necromancy —", "", ""],
        ["Fireball", "Evocation", "—"],
    ]
    assert repair_merged_rows(spells) == spells
    # a multi-word key ("Animal Handling") must not be split into two cells
    skills = [
        ["Athletics", "Strength", "Jump farther than normal"],
        ["Animal Handling Wisdom Calm or train an animal", "", ""],
        ["Stealth", "Dexterity", "Move unseen and unheard"],
    ]
    assert repair_merged_rows(skills) == skills


def test_repair_no_op_on_clean_table():
    clean = [
        ["1,000", "2nd", "gain 1 utility power", "2/1/1/1"],
        ["2,250", "3rd", "gain 1 encounter attack power", "2/2/1/1"],
        ["3,750", "4th", "gain 1 feat", "2/2/1/1"],
    ]
    assert repair_merged_rows(clean) == clean


def test_markdown_render_has_header_separator():
    words = []
    for i, (a, b, c) in enumerate([("1", "x", "y"), ("2", "p", "q"), ("3", "m", "n")]):
        y = 10 + i * 12
        words += [_w(10, y, a), _w(60, y, b), _w(110, y, c)]
    md = find_borderless_tables(words)[0].markdown()
    lines = md.splitlines()
    assert lines[0] == "| 1 | x | y |"
    assert lines[1] == "|---|---|---|"
