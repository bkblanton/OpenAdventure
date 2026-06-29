"""Pure progress-clock model: clamping, status, lookup, id assignment."""

import pytest

from openadventure.mechanics.clocks import Clock, ClockBoard, ClockError, unique_id


def test_reconciled_clamps_and_derives_status():
    full = Clock(id="ritual", name="The ritual", size=4, filled=6).reconciled()
    assert full.filled == 4
    assert full.status == "filled"

    under = Clock(id="x", name="x", size=4, filled=-3).reconciled()
    assert under.filled == 0
    assert under.status == "active"


def test_cancelled_stays_cancelled_even_when_full():
    clock = Clock(id="x", name="x", size=4, filled=4, status="cancelled").reconciled()
    assert clock.status == "cancelled"


def test_find_is_case_insensitive_and_reports_missing():
    board = ClockBoard(clocks=[Clock(id="ritual", name="r", size=4)])
    assert board.find("RITUAL").id == "ritual"
    with pytest.raises(ClockError):
        board.find("nope")


def test_live_excludes_cancelled():
    board = ClockBoard(
        clocks=[
            Clock(id="a", name="a", size=4),
            Clock(id="b", name="b", size=4, status="cancelled"),
        ]
    )
    assert [c.id for c in board.live()] == ["a"]


def test_unique_id_suffixes_on_collision():
    board = ClockBoard(clocks=[Clock(id="ritual", name="r", size=4)])
    assert unique_id("ritual", board) == "ritual-2"
    assert unique_id("flood", board) == "flood"
