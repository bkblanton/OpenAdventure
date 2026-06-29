"""Table + oracle tools through the registry: inline rolls, private rolls,
and the yes/no oracle."""

from openadventure.engine.tools import build_registry
from tests.test_sheet_tools import make_ctx


def test_roll_inline_table(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    out = registry.dispatch(
        ctx,
        "roll_table",
        {
            "name": "Coin flip",
            "entries": [{"text": "heads"}, {"text": "tails"}],
            "count": 3,
        },
    )
    assert out.ok
    assert out.content.count("\n") == 3  # title line + three rolls
    assert all(word in {"heads", "tails"} for word in _result_words(out.content))


def test_roll_table_requires_entries(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    out = registry.dispatch(ctx, "roll_table", {})
    assert not out.ok


def test_private_table_roll_is_hidden_in_dm_mode(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)  # gm mode by default
    out = registry.dispatch(
        ctx,
        "roll_table",
        {"name": "Ambush?", "entries": [{"text": "ambush"}, {"text": "quiet"}], "private": True},
    )
    assert out.private
    assert out.public_result_summary == "secret table roll"


def test_oracle_returns_yes_no(workspace, campaign):
    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = make_ctx(workspace, campaign)
    out = registry.dispatch(ctx, "oracle", {"question": "Is the bridge guarded?", "odds": "likely"})
    assert out.ok
    assert out.content.split(maxsplit=1)[0].rstrip(",") in {"yes", "no"}


def _result_words(content: str) -> list[str]:
    # each roll line looks like "d2 -> 1: heads"; grab the text after the colon
    return [line.split(": ", 1)[1] for line in content.splitlines() if ": " in line]
