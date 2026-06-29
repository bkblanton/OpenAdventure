"""Dice parser/roller unit tests."""

import random

import pytest

from openadventure.mechanics.dice import DiceError, evaluate_check, roll

SEED = 1234


def rng() -> random.Random:
    return random.Random(SEED)


def test_constant_only():
    assert roll("5", rng()).total == 5


def test_single_die_in_range():
    for seed in range(200):
        total = roll("d20", random.Random(seed)).total
        assert 1 <= total <= 20


def test_simple_sum():
    r = random.Random(SEED)
    expected = sum(r.randint(1, 6) for _ in range(6))
    assert roll("6d6", rng()).total == expected


def test_modifier_addition_and_subtraction():
    r = random.Random(SEED)
    base = r.randint(1, 20)
    assert roll("d20+5", rng()).total == base + 5
    assert roll("d20-3", rng()).total == base - 3


def test_multiple_dice_terms():
    r = random.Random(SEED)
    expected = r.randint(1, 20) + sum(r.randint(1, 4) for _ in range(2)) + 1
    assert roll("d20+2d4+1", rng()).total == expected


def test_percentile():
    for seed in range(100):
        total = roll("d%", random.Random(seed)).total
        assert 1 <= total <= 100


def test_advantage_keeps_highest():
    r = random.Random(SEED)
    a, b = r.randint(1, 20), r.randint(1, 20)
    assert roll("2d20kh1", rng()).total == max(a, b)
    assert roll("2d20kl1", rng()).total == min(a, b)


def test_keep_default_count_is_one():
    assert roll("2d20kh", rng()).total == roll("2d20kh1", rng()).total


def test_stat_roll_keeps_top_three():
    r = random.Random(SEED)
    values = [r.randint(1, 6) for _ in range(4)]
    assert roll("4d6kh3", rng()).total == sum(sorted(values)[1:])


def test_drop_low_equals_keep_high():
    assert roll("4d6dl1", rng()).total == roll("4d6kh3", rng()).total


def test_drop_high():
    r = random.Random(SEED)
    values = [r.randint(1, 6) for _ in range(3)]
    assert roll("3d6dh1", rng()).total == sum(sorted(values)[:-1])


def test_reroll_threshold():
    # With reroll, no die's *initial kept* value can come from <= threshold
    # unless the reroll itself landed there again.
    out = roll("10d6r2", rng())
    for die in out.terms[0].dice:
        if die.rerolled_from is not None:
            assert die.rerolled_from <= 2


def test_reroll_changes_total_reproducibly():
    # Same seed: rerolled expression consumes more RNG draws when low values hit.
    plain = roll("10d6", rng()).total
    rerolled = roll("10d6r2", rng()).total
    assert rerolled >= plain  # rerolling 1s/2s can only help or tie for this seed


def test_exploding_dice():
    out = roll("10d2!", rng())
    dice = out.terms[0].dice
    assert any(d.exploded_values for d in dice)
    for d in dice:
        # every exploded chain entry except possibly the last must be max
        if d.exploded_values:
            assert d.value == 2
            assert all(v == 2 for v in d.exploded_values[:-1])
    assert out.total == sum(d.total for d in dice)


def test_seeded_reproducibility():
    assert roll("8d10kh5+2d6-1", rng()).total == roll("8d10kh5+2d6-1", rng()).total


def test_detail_marks_dropped_dice():
    out = roll("4d6kh3", rng())
    detail = out.detail()
    assert "(" in detail and detail.endswith(f"= {out.total}")
    assert "4d6kh3" in detail


def test_whitespace_and_case():
    assert roll(" D20 + 5 ", rng()).total == roll("d20+5", rng()).total


def test_leading_sign():
    r = random.Random(SEED)
    base = r.randint(1, 6)
    assert roll("-d6+10", rng()).total == 10 - base


def test_multiply_constant():
    assert roll("2*3", rng()).total == 6


def test_multiply_dice_by_constant():
    r = random.Random(SEED)
    expected = sum(r.randint(1, 6) for _ in range(3)) * 5
    assert roll("3d6*5", rng()).total == expected


def test_unicode_times_sign():
    assert roll("3d6×5", rng()).total == roll("3d6*5", rng()).total


def test_parenthesised_sum_times_constant():
    # The Call of Cthulhu characteristic formula.
    r = random.Random(SEED)
    expected = (sum(r.randint(1, 6) for _ in range(2)) + 6) * 5
    assert roll("(2d6+6)*5", rng()).total == expected


def test_multiplication_binds_tighter_than_addition():
    # 2d6+6*5 is 2d6 + 30, NOT (2d6+6)*5.
    r = random.Random(SEED)
    expected = sum(r.randint(1, 6) for _ in range(2)) + 6 * 5
    assert roll("2d6+6*5", rng()).total == expected


def test_nested_parentheses():
    assert roll("((d6))", rng()).total == roll("d6", rng()).total


def test_multiply_preserves_dice_groups():
    # terms stays populated so callers can still inspect the raw dice.
    out = roll("(2d6+6)*5", rng())
    assert out.terms[0].dice  # the 2d6 group
    assert out.terms[0].notation == "2d6"


def test_multiply_detail_shows_times_sign():
    out = roll("(2d6+6)*5", rng())
    detail = out.detail()
    assert "×" in detail
    assert detail.startswith("(2d6 [")
    assert detail.endswith(f"= {out.total}")


def test_distribution_sanity():
    totals = [roll("d6", random.Random(seed)).total for seed in range(6000)]
    for face in range(1, 7):
        count = totals.count(face)
        assert 800 < count < 1200, f"face {face} appeared {count} times"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "d",
        "20",  # valid constant, excluded below
        "d0",
        "d1",
        "0d6",
        "2000d6",
        "4d6kh5",
        "2d20kh1kl1",
        "d20+",
        "d20x",
        "2d6r",
        "2d6r6",
        "5 5",
        "(3d6",  # unbalanced open paren
        "3d6)",  # unbalanced close paren
        "()",  # empty parens
        "3d6*",  # trailing operator
        "*5",  # leading operator
    ],
)
def test_invalid_expressions(bad):
    if bad == "20":
        assert roll(bad, rng()).total == 20
        return
    with pytest.raises(DiceError):
        roll(bad, rng())


def test_roll_dice_tool_mentions_complete_modifier_math():
    from openadventure.engine.tools.dice_tools import ROLL_DICE

    tooldef = ROLL_DICE.tooldef()
    assert "include every known modifier" in tooldef.description
    expression_description = tooldef.input_schema["properties"]["expression"]["description"]
    assert "all known modifiers" in expression_description


# --- evaluate_check (deterministic success resolution) ----------------------

_COC = {"tiers": {"hard": 32, "extreme": 13}}  # a CoC skill of 65


def test_check_roll_under_degrees():
    assert evaluate_check(70, target=65, success_when="<=", **_COC).label == "failure"
    assert evaluate_check(65, target=65, success_when="<=", **_COC).label == "success"  # inclusive
    assert evaluate_check(32, target=65, success_when="<=", **_COC).label == "hard success"
    assert evaluate_check(13, target=65, success_when="<=", **_COC).label == "extreme success"
    # a plain pass between the regular and hard thresholds reaches no named tier
    plain = evaluate_check(40, target=65, success_when="<=", **_COC)
    assert plain.success and plain.tier is None


def test_check_roll_over_dc():
    assert evaluate_check(15, target=15, success_when=">=").label == "success"  # meets it beats it
    assert evaluate_check(14, target=15, success_when=">=").label == "failure"
    extreme = evaluate_check(30, target=15, success_when=">=", tiers={"crushing": 25})
    assert extreme.label == "crushing success"


def test_check_rejects_bad_comparator():
    with pytest.raises(DiceError):
        evaluate_check(10, target=20, success_when="<")


# --- natural max/min counts (crit/fumble basis, game-neutral) ---------------


def _outcome(*dice, expression="roll"):
    """A RollOutcome wrapping one term of explicit DieRolls, for testing counts."""
    from openadventure.mechanics.dice import RollOutcome, Term

    total = sum(d.total for d in dice if d.kept)
    return RollOutcome(expression=expression, total=total, terms=(Term(expression, 1, dice),))


def test_max_min_rolls_count_natural_faces():
    from openadventure.mechanics.dice import DieRoll

    # 3d6 showing 6, 1, 3 → one max, one min
    o = _outcome(DieRoll(6, 6), DieRoll(6, 1), DieRoll(6, 3))
    assert o.max_rolls == 1 and o.min_rolls == 1

    # a d% of 100 is a max; of 1 is a min (single die, total == die)
    assert _outcome(DieRoll(100, 100)).max_rolls == 1
    assert _outcome(DieRoll(100, 1)).min_rolls == 1

    # a dice pool: 8d6 → count of 6s and 1s
    pool = _outcome(*(DieRoll(6, v) for v in (6, 6, 1, 4, 6, 2, 1, 5)))
    assert pool.max_rolls == 3 and pool.min_rolls == 2


def test_dropped_dice_dont_count_toward_max_min():
    from openadventure.mechanics.dice import DieRoll

    # advantage 2d20kh1: a dropped natural 20 must not register as a crit
    o = _outcome(DieRoll(20, 20, kept=False), DieRoll(20, 5))
    assert o.max_rolls == 0 and o.min_rolls == 0


def test_real_roll_reports_counts():
    # exercise the actual roller: a 1d2 must report exactly one of max/min
    for seed in range(20):
        o = roll("1d2", random.Random(seed))
        assert o.max_rolls + o.min_rolls == 1  # a d2 is always either max(2) or min(1)


def test_roll_dice_tool_resolves_check_deterministically(workspace, campaign):
    import random

    from openadventure.engine.tools import build_registry
    from openadventure.engine.tools.registry import ToolContext

    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = ToolContext(
        workspace=workspace,
        campaign=campaign,
        meta=campaign.load_meta(),
        log=campaign.open_log(),
        rng=random.Random(7),
    )
    # a constant expression makes the total deterministic (5) regardless of RNG
    out = registry.dispatch(
        ctx,
        "roll_dice",
        {
            "expression": "5",
            "target": 65,
            "success_when": "<=",
            "tiers": {"hard": 32, "extreme": 13},
        },
    )
    assert out.ok
    assert "extreme success" in out.content  # 5 <= 13
    roll_entries = [e for e in ctx.log.read_all() if e.type == "roll"]
    assert roll_entries[-1].data["outcome"] == "extreme success"


def test_roll_dice_tool_check_needs_target_and_comparator(workspace, campaign):
    import random

    from openadventure.engine.tools import build_registry
    from openadventure.engine.tools.registry import ToolContext

    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = ToolContext(
        workspace=workspace,
        campaign=campaign,
        meta=campaign.load_meta(),
        log=campaign.open_log(),
        rng=random.Random(7),
    )
    out = registry.dispatch(ctx, "roll_dice", {"expression": "1d20", "success_when": "<="})
    assert not out.ok and "target" in out.content
    # the incomplete check logged no roll
    assert [e for e in ctx.log.read_all() if e.type == "roll"] == []
