"""Dice expression parser and roller.

Grammar:
    expr     := ["+"|"-"] mul (("+"|"-") mul)*
    mul      := atom (("*"|"×") atom)*
    atom     := "(" expr ")" | dice | INT
    dice     := [INT] "d" (INT | "%") modifier*
    modifier := ("kh"|"kl"|"dh"|"dl") [INT] | "r" INT | "!"

``*`` binds tighter than ``+``/``-`` (standard precedence), so use parentheses
when you mean to multiply a sum, e.g. ``(2d6+6)*5`` (a Call of Cthulhu
characteristic), not ``2d6+6*5`` which is ``2d6+30``.

Examples: ``6d6``, ``d20+5``, ``2d20kh1`` (advantage), ``2d20kl1``
(disadvantage), ``4d6kh3`` (stat roll), ``2d6r2`` (reroll 1s and 2s once),
``d%`` (percentile), ``3d6!`` (exploding), ``3d6*5`` and ``(2d6+6)*5``
(CoC characteristics).

Pure logic: the caller supplies a ``random.Random`` so rolls are seedable
and reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

MAX_DICE = 1000
MAX_SIDES = 100_000
MAX_EXPRESSION_LENGTH = 500
MAX_EXPLOSIONS_PER_DIE = 100


class DiceError(ValueError):
    """Raised for malformed or out-of-range dice expressions."""


@dataclass(frozen=True)
class DieRoll:
    """A single die's final state within a term."""

    sides: int
    value: int
    kept: bool = True
    rerolled_from: int | None = None
    exploded_values: tuple[int, ...] = ()

    @property
    def total(self) -> int:
        return self.value + sum(self.exploded_values)


@dataclass(frozen=True)
class Term:
    """One additive term of an expression: a dice group or a constant."""

    notation: str
    sign: int  # +1 or -1
    dice: tuple[DieRoll, ...] = ()
    constant: int | None = None

    @property
    def subtotal(self) -> int:
        if self.constant is not None:
            return self.sign * self.constant
        return self.sign * sum(d.total for d in self.dice if d.kept)

    def detail(self) -> str:
        """Human-readable detail, e.g. ``4d6kh3 [6, 4, 4, (2)]``."""
        if self.constant is not None:
            return str(self.constant)
        parts = []
        for d in self.dice:
            s = str(d.value)
            if d.exploded_values:
                s = "+".join([s, *map(str, d.exploded_values)]) + "!"
            if d.rerolled_from is not None:
                s = f"{d.rerolled_from}→{s}"
            if not d.kept:
                s = f"({s})"
            parts.append(s)
        return f"{self.notation} [{', '.join(parts)}]"


@dataclass(frozen=True)
class RollOutcome:
    """Result of evaluating a full dice expression."""

    expression: str
    total: int
    terms: tuple[Term, ...] = field(default_factory=tuple)
    root: _Node | None = None  # the evaluated AST; drives detail()

    def detail(self) -> str:
        """e.g. ``4d6kh3 [6, 4, 4, (2)] + 2 = 16`` (dropped dice in parens),
        or ``(2d6 [5, 4] + 6) × 5 = 75``."""
        body = self.root.detail() if self.root is not None else ""
        return f"{body} = {self.total}".lstrip()

    @property
    def max_rolls(self) -> int:
        """How many *kept* dice landed on their highest face: a natural 20 on a
        d20, a 100 on d%, every 6 in a d6 pool. Game-neutral and modifier-blind
        (it reads the dice, not the total), so it's the right basis for a
        critical; the meaning of a max roll is the game system's to decide."""
        return sum(1 for term in self.terms for d in term.dice if d.kept and d.value == d.sides)

    @property
    def min_rolls(self) -> int:
        """How many *kept* dice landed on 1: a natural 1, every 1 in a pool."""
        return sum(1 for term in self.terms for d in term.dice if d.kept and d.value == 1)


@dataclass
class _Parser:
    text: str
    pos: int = 0

    def error(self, message: str) -> DiceError:
        return DiceError(f"{message} (at position {self.pos} in {self.text!r})")

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def take(self) -> str:
        ch = self.peek()
        self.pos += 1
        return ch

    def skip_ws(self) -> None:
        while self.peek() == " ":
            self.pos += 1

    def match(self, literal: str) -> bool:
        if self.text.startswith(literal, self.pos):
            self.pos += len(literal)
            return True
        return False

    def integer(self) -> int | None:
        start = self.pos
        while self.peek().isdigit():
            self.pos += 1
        if self.pos == start:
            return None
        return int(self.text[start : self.pos])


@dataclass(frozen=True)
class _DiceSpec:
    count: int
    sides: int
    keep_high: int | None = None
    keep_low: int | None = None
    drop_high: int | None = None
    drop_low: int | None = None
    reroll_at_most: int | None = None
    exploding: bool = False
    notation: str = ""


def _parse_dice(p: _Parser, count: int | None) -> _DiceSpec:
    start = p.pos - (len(str(count)) if count is not None else 0) - 1  # include digits + 'd'
    n = count if count is not None else 1
    if n < 1 or n > MAX_DICE:
        raise p.error(f"dice count must be 1-{MAX_DICE}")
    if p.match("%"):
        sides = 100
    else:
        sides = p.integer()
        if sides is None:
            raise p.error("expected die size after 'd'")
    if sides < 2 or sides > MAX_SIDES:
        raise p.error(f"die size must be 2-{MAX_SIDES}")

    keep_high = keep_low = drop_high = drop_low = reroll = None
    exploding = False
    while True:
        if p.match("kh"):
            keep_high = p.integer() or 1
        elif p.match("kl"):
            keep_low = p.integer() or 1
        elif p.match("dh"):
            drop_high = p.integer() or 1
        elif p.match("dl"):
            drop_low = p.integer() or 1
        elif p.match("r"):
            reroll = p.integer()
            if reroll is None:
                raise p.error("expected threshold after 'r' (e.g. 2d6r2)")
            if reroll >= sides:
                raise p.error(f"reroll threshold r{reroll} must be below die size d{sides}")
        elif p.match("!"):
            exploding = True
        else:
            break

    selectors = [x for x in (keep_high, keep_low, drop_high, drop_low) if x is not None]
    if len(selectors) > 1:
        raise p.error("at most one of kh/kl/dh/dl per dice group")
    if selectors and selectors[0] > n:
        raise p.error(f"cannot keep/drop {selectors[0]} of {n} dice")

    return _DiceSpec(
        count=n,
        sides=sides,
        keep_high=keep_high,
        keep_low=keep_low,
        drop_high=drop_high,
        drop_low=drop_low,
        reroll_at_most=reroll,
        exploding=exploding,
        notation=p.text[max(start, 0) : p.pos],
    )


def _roll_spec(spec: _DiceSpec, rng: random.Random) -> tuple[DieRoll, ...]:
    dice: list[DieRoll] = []
    for _ in range(spec.count):
        value = rng.randint(1, spec.sides)
        rerolled_from = None
        if spec.reroll_at_most is not None and value <= spec.reroll_at_most:
            rerolled_from = value
            value = rng.randint(1, spec.sides)
        exploded: list[int] = []
        if spec.exploding:
            last = value
            while last == spec.sides and len(exploded) < MAX_EXPLOSIONS_PER_DIE:
                last = rng.randint(1, spec.sides)
                exploded.append(last)
        dice.append(
            DieRoll(
                sides=spec.sides,
                value=value,
                rerolled_from=rerolled_from,
                exploded_values=tuple(exploded),
            )
        )

    keep = [True] * len(dice)
    order = sorted(range(len(dice)), key=lambda i: dice[i].total)
    if spec.keep_high is not None:
        for i in order[: len(dice) - spec.keep_high]:
            keep[i] = False
    elif spec.keep_low is not None:
        for i in order[spec.keep_low :]:
            keep[i] = False
    elif spec.drop_low is not None:
        for i in order[: spec.drop_low]:
            keep[i] = False
    elif spec.drop_high is not None:
        for i in order[len(dice) - spec.drop_high :]:
            keep[i] = False

    return tuple(
        DieRoll(
            sides=d.sides,
            value=d.value,
            kept=k,
            rerolled_from=d.rerolled_from,
            exploded_values=d.exploded_values,
        )
        for d, k in zip(dice, keep, strict=True)
    )


# --- Expression AST ----------------------------------------------------------
#
# Parsing builds a small tree of these nodes; the dice are rolled as each leaf
# is parsed (left to right), so the RNG draw order matches the written order.
# Each node knows its ``value`` (integer total), its ``detail`` (human-readable
# breakdown, no trailing ``= total``), and the dice ``groups`` it contains (so
# ``RollOutcome.terms`` stays populated for callers that inspect raw dice).


@dataclass(frozen=True)
class _Leaf:
    """A single dice group or a bare constant, wrapping one ``Term``."""

    term: Term

    def value(self) -> int:
        return self.term.subtotal  # built with sign +1, so non-negative

    def detail(self) -> str:
        return self.term.detail()

    def groups(self) -> list[Term]:
        return [self.term]


@dataclass(frozen=True)
class _Group:
    """A parenthesised sub-expression."""

    inner: _Node

    def value(self) -> int:
        return self.inner.value()

    def detail(self) -> str:
        return f"({self.inner.detail()})"

    def groups(self) -> list[Term]:
        return self.inner.groups()


@dataclass(frozen=True)
class _Mul:
    """A product of two or more factors (``a * b * …``)."""

    factors: tuple[_Node, ...]

    def value(self) -> int:
        product = 1
        for factor in self.factors:
            product *= factor.value()
        return product

    def detail(self) -> str:
        return " × ".join(f.detail() for f in self.factors)

    def groups(self) -> list[Term]:
        return [g for f in self.factors for g in f.groups()]


@dataclass(frozen=True)
class _Add:
    """A signed sum of one or more parts (``+a - b + …``)."""

    parts: tuple[tuple[int, _Node], ...]  # (sign, node)

    def value(self) -> int:
        return sum(sign * node.value() for sign, node in self.parts)

    def detail(self) -> str:
        out: list[str] = []
        for i, (sign, node) in enumerate(self.parts):
            d = node.detail()
            if i == 0:
                out.append(d if sign > 0 else f"-{d}")
            else:
                out.append(f"{'+' if sign > 0 else '-'} {d}")
        return " ".join(out)

    def groups(self) -> list[Term]:
        return [g for _, node in self.parts for g in node.groups()]


_Node = _Leaf | _Group | _Mul | _Add


def _parse_expr(p: _Parser, rng: random.Random) -> _Node:
    p.skip_ws()
    if p.match("-"):
        sign = -1
    else:
        sign = 1
        p.match("+")
    parts: list[tuple[int, _Node]] = [(sign, _parse_mul(p, rng))]
    while True:
        p.skip_ws()
        if p.match("+"):
            parts.append((1, _parse_mul(p, rng)))
        elif p.match("-"):
            parts.append((-1, _parse_mul(p, rng)))
        else:
            break
    return _Add(tuple(parts))


def _parse_mul(p: _Parser, rng: random.Random) -> _Node:
    factors: list[_Node] = [_parse_atom(p, rng)]
    while True:
        p.skip_ws()
        if p.match("*") or p.match("×"):
            factors.append(_parse_atom(p, rng))
        else:
            break
    return factors[0] if len(factors) == 1 else _Mul(tuple(factors))


def _parse_atom(p: _Parser, rng: random.Random) -> _Node:
    p.skip_ws()
    if p.match("("):
        inner = _parse_expr(p, rng)
        p.skip_ws()
        if not p.match(")"):
            raise p.error("expected ')'")
        return _Group(inner)
    n = p.integer()
    if p.match("d"):
        spec = _parse_dice(p, n)
        return _Leaf(Term(notation=spec.notation, sign=1, dice=_roll_spec(spec, rng)))
    if n is not None:
        return _Leaf(Term(notation=str(n), sign=1, constant=n))
    raise p.error("expected a number, dice group, or '('")


Comparator = str  # "<=" (roll-under) or ">=" (roll-over)


@dataclass(frozen=True)
class CheckOutcome:
    """Engine-decided pass/fail (and degree) of comparing a roll to a target.

    The comparator carries both direction and inclusivity: ``<=`` is roll-under
    (succeed at or below the target, e.g. Call of Cthulhu skills), ``>=`` is
    roll-over (succeed at or above, e.g. a d20 vs a DC). Both boundaries are
    inclusive, the only convention real systems use.

    Criticals and fumbles are *not* decided here: they key off natural dice, not
    the modified total, so they're reported separately as ``RollOutcome``'s
    ``max_rolls`` / ``min_rolls`` counts and interpreted per game system."""

    success: bool
    label: str  # "success", "hard success", "failure", …
    target: int
    comparator: Comparator
    tier: str | None = None  # named degree reached, if any (e.g. "hard")


def _meets(value: int, boundary: int, comparator: Comparator) -> bool:
    return value <= boundary if comparator == "<=" else value >= boundary


def evaluate_check(
    total: int,
    *,
    target: int,
    success_when: Comparator,
    tiers: dict[str, int] | None = None,
) -> CheckOutcome:
    """Decide pass/fail (and degree) deterministically from a rolled ``total``.

    ``success_when`` is ``"<="`` (roll-under) or ``">="`` (roll-over). ``tiers``
    are named degrees beyond a plain success, each a boundary compared with the
    same comparator (e.g. CoC ``{"hard": 32, "extreme": 13}``); the most extreme
    one the roll reaches wins."""
    if success_when not in ("<=", ">="):
        raise DiceError(f"success_when must be '<=' or '>=' (got {success_when!r})")
    if not _meets(total, target, success_when):
        return CheckOutcome(False, "failure", target, success_when)

    best: str | None = None
    if tiers:
        met = {name: b for name, b in tiers.items() if _meets(total, b, success_when)}
        if met:
            # the most extreme tier is the hardest boundary to reach: the lowest
            # number for roll-under, the highest for roll-over.
            best = (
                min(met, key=met.__getitem__)
                if success_when == "<="
                else max(met, key=met.__getitem__)
            )
    label = f"{best} success" if best else "success"
    return CheckOutcome(True, label, target, success_when, tier=best)


def roll(expression: str, rng: random.Random | None = None) -> RollOutcome:
    """Parse and evaluate a dice expression."""
    if rng is None:
        rng = random.Random()
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise DiceError(f"expression longer than {MAX_EXPRESSION_LENGTH} characters")
    p = _Parser(expression.strip().lower())
    if not p.text:
        raise DiceError("empty dice expression")

    root = _parse_expr(p, rng)
    p.skip_ws()
    if p.pos != len(p.text):
        raise p.error(f"unexpected trailing input {p.text[p.pos :]!r}")

    return RollOutcome(
        expression=expression.strip(),
        total=root.value(),
        terms=tuple(root.groups()),
        root=root,
    )
