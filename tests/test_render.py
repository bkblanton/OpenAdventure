"""Terminal rendering of engine events."""

from io import StringIO

from rich.console import Console

from openadventure.cli.render import EventRenderer
from openadventure.engine.events import RollResult


def _render(*events) -> str:
    out = StringIO()
    renderer = EventRenderer(Console(file=out, force_terminal=False, color_system=None, width=200))
    renderer.render_events(list(events))
    return out.getvalue()


def test_roll_result_shows_engine_verdict_and_natural_extremes():
    text = _render(
        RollResult(
            expression="1d100",
            total=23,
            detail="1d100 [23] = 23",
            reason="Booker Library Use",
            outcome="hard success",
        ),
        RollResult(
            expression="1d100",
            total=100,
            detail="1d100 [100] = 100",
            reason="Pattis INT",
            outcome="failure",
            max_rolls=1,
        ),
    )
    # the engine-decided verdict rides on the dice line, next to the reason
    assert "→ hard success" in text
    assert "Booker Library Use" in text
    assert "→ failure" in text
    # natural max/min dice are surfaced (here a 100 on d%)
    assert "max ×1" in text


def test_bare_roll_has_no_verdict_or_extremes():
    # a roll with no check (damage, initiative) shows just the dice, no arrow
    text = _render(RollResult(expression="1d20+5", total=18, detail="1d20+5 = 18"))
    assert "1d20+5 = 18" in text
    assert "→" not in text
    assert "max ×" not in text


def test_secret_roll_hides_everything():
    # a private GM roll never leaks its result or verdict to the player
    text = _render(RollResult(expression="1d100", total=0, detail="", private=True, outcome=None))
    assert "secret roll" in text
