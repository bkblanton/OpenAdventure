"""Pure table + oracle mechanics: weighted draw, dice-mapping, clamp, oracle odds."""

import random

import pytest

from openadventure.mechanics.tables import (
    Table,
    TableEntry,
    TableError,
    consult_oracle,
    roll_on_table,
)


def test_weighted_roll_is_deterministic_and_in_range():
    table = Table(
        name="Loot",
        entries=[TableEntry(text="gold"), TableEntry(text="gem"), TableEntry(text="junk")],
    )
    a = roll_on_table(table, random.Random(1))
    b = roll_on_table(table, random.Random(1))
    assert a.text == b.text  # same seed -> same draw
    assert a.text in {"gold", "gem", "junk"}
    assert 1 <= a.roll <= 3


def test_weight_skews_the_draw():
    table = Table(
        name="Weighted",
        entries=[TableEntry(text="common", weight=9), TableEntry(text="rare", weight=1)],
    )
    draws = [roll_on_table(table, random.Random(s)).text for s in range(200)]
    assert draws.count("common") > draws.count("rare")
    assert "rare" in draws  # the long tail still happens


def test_dice_mapped_respects_ranges():
    table = Table(
        name="2d6 reaction",
        dice="1d6",
        entries=[
            TableEntry(text="low", lo=1, hi=3),
            TableEntry(text="high", lo=4, hi=6),
        ],
    )
    assert table.is_dice_mapped()
    for seed in range(50):
        result = roll_on_table(table, random.Random(seed))
        expected = "low" if result.roll <= 3 else "high"
        assert result.text == expected


def test_dice_mapped_clamps_when_roll_falls_outside_ranges():
    # only 1 to 2 are covered; rolls of 3 to 6 must clamp to the nearest (and only) entry
    table = Table(name="Sparse", dice="1d6", entries=[TableEntry(text="only", lo=1, hi=2)])
    clamped = [roll_on_table(table, random.Random(s)) for s in range(30)]
    assert all(r.text == "only" for r in clamped)
    assert any("clamped" in r.detail for r in clamped)


def test_empty_table_rejected_by_model():
    with pytest.raises(ValueError):
        Table(name="empty", entries=[])


def test_bad_dice_expression_raises_table_error():
    table = Table(name="bad", dice="notdice", entries=[TableEntry(text="x", lo=1, hi=9)])
    with pytest.raises(TableError):
        roll_on_table(table, random.Random(0))


def test_oracle_odds_bias_the_answer():
    certain = [consult_oracle("?", "certain", random.Random(s)).answer for s in range(100)]
    impossible = [consult_oracle("?", "impossible", random.Random(s)).answer for s in range(100)]
    assert certain.count("yes") > impossible.count("yes")
    assert certain.count("yes") >= 90


def test_oracle_is_deterministic_and_bounded():
    a = consult_oracle("Is the door locked?", "even", random.Random(5))
    b = consult_oracle("Is the door locked?", "even", random.Random(5))
    assert (a.answer, a.twist, a.roll) == (b.answer, b.twist, b.roll)
    assert 1 <= a.roll <= 100
    assert a.answer in {"yes", "no"}
    assert a.twist in {None, "and", "but"}
