"""Shared rendering for the search_* tools: top hits in full, the rest in brief.

search_rules / search_campaign (over file sections) and search_sheets (over
character sheets) all return their best matches with full detail inlined, so the
GM can act without a second fetch, and lower-ranked matches as a one-line brief.
The corpus-specific formatting lives in each tool's ``full``/``brief`` callables;
this module owns the shared contract: how many hits come back full, how a
truncated body points at the rest, and how blocks join.

search_canon is deliberately not built on this: its entries are already
one-liners, so it has no full-vs-brief split to share.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

INLINE_HITS = 2  # top matches returned in full; lower-ranked ones as a brief


def truncate_with_pointer(body: str, cap: int, more_hint: str) -> str:
    """First ``cap`` chars of ``body``, plus a '[…N more characters: <hint>]'
    pointer when truncated, so the reader knows how to pull the rest."""
    text = body[:cap]
    remaining = len(body) - len(text)
    if remaining > 0:
        text += f"\n  […{remaining} more characters: {more_hint}]"
    return text


def render_hits[T](
    hits: Sequence[T],
    *,
    full: Callable[[T], str],
    brief: Callable[[T], str],
    inline: int = INLINE_HITS,
) -> str:
    """Join ranked ``hits`` into blocks: the first ``inline`` rendered with
    ``full``, the rest with ``brief`` (each ``hit -> str``), a blank line between."""
    blocks = [full(hit) if rank < inline else brief(hit) for rank, hit in enumerate(hits)]
    return "\n\n".join(blocks)
