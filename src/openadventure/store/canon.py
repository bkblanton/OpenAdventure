"""Canon: the campaign's structured, drift-free memory of durable facts.

Unlike the prose "story so far" (which is re-summarized each compaction and so
slowly erodes), canon is a list of addressable entries that are *patched* one at
a time (add / update / resolve) and never re-summarized. Old entries pass
through untouched unless an op names them by id, which is the no-drift
guarantee.

This module is pure: it owns the data model, the op-application logic, and the
two render paths (the always-injected open subset, and the full set for search).
It does no AI work and does not touch the turn loop. See
docs/design/canon-memory.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from openadventure.store import snapshots

if TYPE_CHECKING:
    from openadventure.store.workspace import Campaign

# Display order for categories. `category` is a plain str (not a Literal) so an
# older build reading a canon written by a newer one degrades gracefully:
# unknown categories render last instead of failing to load.
KNOWN_CATEGORIES: tuple[str, ...] = (
    "threads",
    "seeds",
    "promises",
    "rulings",
    "world",
)

# A status in this set closes an entry: it leaves the open working set (no longer
# injected, no longer fed to the chronicler) and moves to the searchable archive.
CLOSED_STATUSES: frozenset[str] = frozenset(
    {"resolved", "paid", "lost", "dropped", "closed", "done"}
)

_VALID_OPS: frozenset[str] = frozenset({"add", "update", "resolve"})


# Token estimate, chars // 4. Mirrors engine.context.est_tokens; duplicated here
# (one line) to keep the store layer free of an engine import.
def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class CanonEntry(BaseModel):
    """One durable fact. `text` is the rendered content; `facts` is an optional
    accumulating bullet list (used mainly for NPCs). Lifecycle is tracked by
    `status` plus the seq stamps."""

    model_config = ConfigDict(extra="allow")  # preserve unknown future fields

    id: str
    category: str
    text: str = ""
    facts: list[str] = Field(default_factory=list)
    status: str = "open"
    visibility: str = "open"  # "open" (table-visible) or "hidden" (GM-only)
    priority: str = "normal"  # "normal" or "major" (survives ranking)
    created_seq: int = 0
    updated_seq: int = 0
    closed_seq: int | None = None

    @property
    def is_open(self) -> bool:
        return self.status not in CLOSED_STATUSES


class Canon(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int = 1
    through_seq: int = 0
    entries: list[CanonEntry] = Field(default_factory=list)

    def find(self, entry_id: str) -> CanonEntry | None:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    def open_entries(self) -> list[CanonEntry]:
        return [e for e in self.entries if e.is_open]

    def archived_entries(self) -> list[CanonEntry]:
        return [e for e in self.entries if not e.is_open]


def empty() -> Canon:
    """A fresh empty canon. (A function, not a shared constant, so callers can
    never accidentally mutate a global.)"""
    return Canon()


def load(campaign: Campaign) -> Canon:
    """Load canon.json, or an empty canon when the file is absent (an
    un-migrated campaign) or unreadable. Never raises on a missing/garbled file:
    canon is a derived snapshot and is safe to rebuild."""
    try:
        data = snapshots.load_json(campaign.canon_path)
        if not isinstance(data, dict):
            return empty()
        return Canon.model_validate(data)
    except ValueError, OSError:
        return empty()


def save(campaign: Campaign, canon: Canon) -> None:
    snapshots.save_json(campaign.canon_path, canon)


# --- op application ------------------------------------------------------


class CanonOp(BaseModel):
    """A single patch emitted by the chronicler. Validated before application;
    malformed ops are dropped rather than raising (see apply_ops)."""

    model_config = ConfigDict(extra="ignore")

    op: str
    id: str
    category: str | None = None
    text: str | None = None
    facts_add: list[str] = Field(default_factory=list)
    status: str | None = None
    visibility: str | None = None
    priority: str | None = None
    closed_seq: int | None = None
    seq: int | None = None  # seq of the event this op records; defaults to at_seq


def _coerce_op(raw: object) -> CanonOp | None:
    if isinstance(raw, CanonOp):
        return raw
    if not isinstance(raw, dict):
        return None
    try:
        return CanonOp.model_validate(raw)
    except ValueError:
        return None


def _merge_facts(existing: list[str], additions: list[str]) -> list[str]:
    merged = list(existing)
    for fact in additions:
        if fact and fact not in merged:
            merged.append(fact)
    return merged


def apply_ops(canon: Canon, ops: list[object], *, at_seq: int) -> tuple[Canon, list[str]]:
    """Apply patch ops, returning a NEW canon and a list of human-readable
    warnings for dropped/malformed ops (the caller logs them).

    `at_seq` is the seq through which this pass has processed; it stamps
    created/updated/closed seqs when an op does not carry its own `seq`.

    `add` is idempotent by id: re-adding an existing id routes to update, so
    reprocessing the same span (e.g. after an interrupted compaction) converges
    instead of duplicating entries.
    """
    result = canon.model_copy(deep=True)
    by_id = {e.id: e for e in result.entries}
    warnings: list[str] = []

    for raw in ops:
        op = _coerce_op(raw)
        if op is None:
            warnings.append(f"dropped unparseable op: {raw!r}")
            continue
        if op.op not in _VALID_OPS or not op.id:
            warnings.append(f"dropped op with bad kind/id: {op.model_dump()!r}")
            continue

        seq = op.seq if op.seq is not None else at_seq
        existing = by_id.get(op.id)

        if op.op == "add" and existing is None:
            category = op.category
            if category not in KNOWN_CATEGORIES:
                warnings.append(f"dropped add '{op.id}' with unknown category {category!r}")
                continue
            entry = CanonEntry(
                id=op.id,
                category=category,
                text=op.text or "",
                facts=_merge_facts([], op.facts_add),
                status=op.status or "open",
                visibility=op.visibility or "open",
                priority=op.priority or "normal",
                created_seq=seq,
                updated_seq=seq,
            )
            result.entries.append(entry)
            by_id[entry.id] = entry
            continue

        # update / resolve / idempotent re-add all patch an existing entry
        if existing is None:
            warnings.append(f"dropped {op.op} for unknown id '{op.id}'")
            continue

        # Stale-write guard: if the entry was last touched at a seq beyond this
        # op's processing point, it carries fresher knowledge than this op (e.g.
        # a GM note_canon landed during a long background compaction, which folds
        # the transcript against a snapshot taken before that edit). Skip the
        # destructive scalar overwrites so the older value cannot clobber the
        # newer one. facts_add is a union merge, never lossy, so it still applies.
        # `>` (not `>=`) keeps interrupted-pass reprocessing convergent: the same
        # span re-applied carries the same seq the entry already holds.
        stale = existing.updated_seq > seq
        wants_scalar = (
            op.text is not None
            or op.visibility is not None
            or op.priority is not None
            or op.status is not None
            or op.op == "resolve"
        )

        if op.facts_add:
            existing.facts = _merge_facts(existing.facts, op.facts_add)

        if stale:
            if wants_scalar:
                warnings.append(
                    f"skipped stale {op.op} for '{op.id}': entry edited at seq "
                    f"{existing.updated_seq}, after this pass's seq {seq}"
                )
            continue

        if op.text is not None:
            existing.text = op.text
        if op.visibility is not None:
            existing.visibility = op.visibility
        if op.priority is not None:
            existing.priority = op.priority

        if op.op == "resolve":
            existing.status = op.status or "resolved"
        elif op.status is not None:
            existing.status = op.status

        if not existing.is_open and existing.closed_seq is None:
            existing.closed_seq = op.closed_seq if op.closed_seq is not None else seq
        existing.updated_seq = seq

    return result, warnings


# --- rendering -----------------------------------------------------------

# Caps for the always-injected open subset. The hard guarantee is that the
# injected text never exceeds OPEN_BUDGET_TOKENS regardless of how many entries
# exist; dropped entries are logged and stay reachable via render_full / search.
OPEN_BUDGET_TOKENS = 3_500
OPEN_PER_CATEGORY_CAP = 12

# Cap for a single search/list result (render_full). The whole canon (open plus
# the ever-growing archive of closed entries) would otherwise dump unbounded
# into a turn's context when search_canon is called with no query ("list
# everything"). The cap keeps that result bounded no matter how long the campaign
# runs; truncated entries stay reachable by narrowing the query.
SEARCH_BUDGET_TOKENS = 6_000


def _priority_rank(entry: CanonEntry) -> int:
    return 0 if entry.priority == "major" else 1


def _category_order(category: str) -> int:
    try:
        return KNOWN_CATEGORIES.index(category)
    except ValueError:
        return len(KNOWN_CATEGORIES)  # unknown categories render last


def _rank_key(entry: CanonEntry) -> tuple[int, int]:
    """Major before normal; within that, most-recently-touched first. A stale
    (long-untouched) open entry sorts last and so is the first to be dropped
    when the cap bites."""
    return (_priority_rank(entry), -entry.updated_seq)


def _render_line(entry: CanonEntry, *, mark_hidden: bool) -> str:
    body = entry.text.strip()
    if entry.facts:
        joined = "; ".join(f.strip() for f in entry.facts if f.strip())
        body = f"{body}: {joined}" if body else joined
    tag = " (GM-only)" if mark_hidden and entry.visibility == "hidden" else ""
    return f"- {body}{tag} [{entry.id}]"


def select_open(
    canon: Canon,
    *,
    include_hidden: bool,
    budget_tokens: int = OPEN_BUDGET_TOKENS,
    per_category_cap: int = OPEN_PER_CATEGORY_CAP,
) -> tuple[list[CanonEntry], list[str]]:
    """Choose the open entries to inject, ranked and capped. Returns
    (kept, dropped_ids). Deterministic: the bound does not depend on the model.
    """
    candidates = [e for e in canon.open_entries() if include_hidden or e.visibility != "hidden"]
    candidates.sort(key=_rank_key)

    kept: list[CanonEntry] = []
    dropped: list[str] = []
    per_category: dict[str, int] = {}
    used = 0
    for entry in candidates:
        line_cost = _est_tokens(_render_line(entry, mark_hidden=include_hidden))
        count = per_category.get(entry.category, 0)
        if count >= per_category_cap or (kept and used + line_cost > budget_tokens):
            dropped.append(entry.id)
            continue
        kept.append(entry)
        per_category[entry.category] = count + 1
        used += line_cost
    return kept, dropped


def _render_grouped(entries: list[CanonEntry], *, mark_hidden: bool) -> str:
    if not entries:
        return ""
    ordered = sorted(entries, key=lambda e: (_category_order(e.category), e.created_seq))
    parts: list[str] = []
    current: str | None = None
    for entry in ordered:
        if entry.category != current:
            current = entry.category
            parts.append(f"### {current.capitalize()}")
        parts.append(_render_line(entry, mark_hidden=mark_hidden))
    return "\n".join(parts)


def render_open(
    canon: Canon,
    *,
    include_hidden: bool,
    budget_tokens: int = OPEN_BUDGET_TOKENS,
    per_category_cap: int = OPEN_PER_CATEGORY_CAP,
) -> tuple[str, list[str]]:
    """Markdown for the open subset that is injected every turn, hard-capped.
    Returns (markdown, dropped_ids); dropped ids are logged by the caller and
    remain reachable via render_full / search."""
    kept, dropped = select_open(
        canon,
        include_hidden=include_hidden,
        budget_tokens=budget_tokens,
        per_category_cap=per_category_cap,
    )
    return _render_grouped(kept, mark_hidden=include_hidden), dropped


# The chronicler is shown more open entries than the GM. The GM injection cap
# (OPEN_*) silently drops the stale overflow, but the chronicler patches entries
# by [id], so an [id] it never sees can never be resolved or merged, leaving
# unresolved canon to accumulate forever. Surfacing the overflow (still bounded,
# so the chronicler prompt stays finite) is what lets it prune.
CHRONICLER_OVERFLOW_BUDGET_TOKENS = 4_000


def render_open_with_overflow(canon: Canon, *, include_hidden: bool) -> tuple[str, str]:
    """For the chronicler: (injected, overflow). ``injected`` is exactly what the
    GM sees (the OPEN_* capped subset); ``overflow`` is the open entries that cap
    drops (bounded by CHRONICLER_OVERFLOW_BUDGET_TOKENS), so the chronicler can
    resolve the ones the story settled and merge duplicates instead of letting
    them pile up unreachable. ``overflow`` is "" when nothing was dropped."""
    kept, dropped_ids = select_open(canon, include_hidden=include_hidden)
    injected = _render_grouped(kept, mark_hidden=include_hidden)
    if not dropped_ids:
        return injected, ""

    # dropped_ids is already in rank order (select_open ranks then caps); keep the
    # highest-ranked overflow within the chronicler's own bound.
    overflow_entries: list[CanonEntry] = []
    used = 0
    for entry_id in dropped_ids:
        entry = canon.find(entry_id)
        if entry is None:
            continue
        cost = _est_tokens(_render_line(entry, mark_hidden=include_hidden))
        if overflow_entries and used + cost > CHRONICLER_OVERFLOW_BUDGET_TOKENS:
            break
        overflow_entries.append(entry)
        used += cost
    return injected, _render_grouped(overflow_entries, mark_hidden=include_hidden)


def render_full(
    canon: Canon,
    *,
    include_hidden: bool,
    query: str | None = None,
    budget_tokens: int = SEARCH_BUDGET_TOKENS,
) -> str:
    """Markdown for the whole canon (open + archived), for search and recap.
    Optional case-insensitive substring filter over text/facts/id.

    Hard-capped at ``budget_tokens``: when the matching set is larger, the
    highest-priority, most-recently-touched entries are kept (the same ranking as
    the injected subset) and a trailing notice reports how many were withheld, so
    a no-query "list everything" on a long campaign can never dump an unbounded
    archive into context. Narrowing the query surfaces the rest."""
    entries = [e for e in canon.entries if include_hidden or e.visibility != "hidden"]
    if query:
        needle = query.casefold()
        entries = [
            e
            for e in entries
            if needle in e.text.casefold()
            or needle in e.id.casefold()
            or any(needle in f.casefold() for f in e.facts)
        ]

    # Rank so the entries that survive truncation are the ones most worth keeping;
    # _render_grouped re-sorts the kept set back into category/created order.
    ranked = sorted(entries, key=_rank_key)
    kept: list[CanonEntry] = []
    used = 0
    for entry in ranked:
        cost = _est_tokens(_render_line(entry, mark_hidden=include_hidden))
        if kept and used + cost > budget_tokens:
            break
        kept.append(entry)
        used += cost

    body = _render_grouped(kept, mark_hidden=include_hidden)
    dropped = len(ranked) - len(kept)
    if dropped:
        notice = f"…and {dropped} more not shown. Narrow your search with a query."
        body = f"{body}\n\n{notice}" if body else notice
    return body
