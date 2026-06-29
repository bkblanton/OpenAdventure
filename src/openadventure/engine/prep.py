"""Scene prep: keep the current keyed location's canonical text in context.

A human GM reads the room before the party walks in. This gives the AI GM the
same head start: whenever the scene's ``module_path`` points at a keyed module
location, that location's canonical text, plus any monster/spell stat blocks it
cross-references, rides in the campaign context every turn. So the GM narrates
from the book instead of reactively burning tool rounds on search_campaign /
read_campaign mid-turn, and the canonical text stays in front of it even after
compaction trims the transcript.

Module data is messy, though: a single location is often described across
several sections, and one keyed path can't capture all of it. So prep stitches
together ``module_path`` plus the scene's ``extra_paths``: additional sections
the GM has decided belong to this location. (Anything the stitching still can't
reach, such as a mangled table or a stat block whose cross-reference didn't
resolve, is the GM's own ``prep_notes`` to record; see scene_tools.)

The GM may list many ``extra_paths``, more than fit in the budget if every body
were inlined. So prep is tiered: ``module_path`` and the first few extras get
their full body (``PREP_FULL_SECTIONS``); the rest are listed as one-line
``read_campaign`` pointers (path + breadcrumb) so the GM knows they're relevant
and can pull any on demand without paying for all of them every turn.

It is computed fresh at context-assembly time (a few local file reads + small
SQLite lookups, sub-millisecond), so there is nothing to cache or invalidate:
the prep always matches whatever paths the scene currently holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openadventure.ingest import pipeline, xref

PREP_BODY_CHARS = 3500  # cap each location body surfaced in context
PREP_REF_CHARS = 900  # cap each inlined stat block / spell
PREP_MAX_REFS = 2  # total inlined stat blocks across all stitched sections
PREP_FULL_SECTIONS = 3  # ceiling on sections inlined in full (module_path + closest extras)
PREP_MAX_POINTERS = 24  # further related sections listed as read_campaign pointers
# Fallback char budget for the full-body tier when no caller-supplied budget is given
# (direct/test use). In a live turn the budget comes from ContextBudget.prep.
PREP_DEFAULT_CHARS = PREP_FULL_SECTIONS * PREP_BODY_CHARS

if TYPE_CHECKING:
    from openadventure.store.workspace import Workspace


def _section_prep(
    workspace: Workspace, path: str, *, inlined: set[str], refs_remaining: int
) -> tuple[str | None, int]:
    """Render one section's prepped text (body + any inlinable stat blocks not
    already pulled). Returns (text or None if unresolvable, refs_remaining).
    ``inlined`` tracks ref paths already pulled so the same stat block isn't
    inlined twice when several sections reference it."""
    module, _, rest = path.partition("/")
    if not rest:
        return None, refs_remaining
    sections_root = (workspace.book_dir(module) / "sections").resolve()
    if not sections_root.is_dir():
        return None, refs_remaining

    from openadventure.engine.tools.rules_tools import _resolve_section_path

    target, status = _resolve_section_path(sections_root, rest)
    if status != "found" or target is None:
        return None, refs_remaining
    _, _, body = pipeline.parse_section_file(target.read_text(encoding="utf-8"))
    matched = target.relative_to(sections_root).as_posix()

    text = body[:PREP_BODY_CHARS]
    if len(body) > PREP_BODY_CHARS:
        text += f"\n[…truncated; read_campaign '{module}/{matched}' for the rest]"
    parts = [f"{module}/{matched}:\n{text}"]

    xref_db = workspace.book_dir(module) / xref.XREF_NAME
    for ref in xref.references_for(xref_db, matched):
        if ref.kind not in ("monster", "spell") or refs_remaining <= 0:
            continue
        ref_key = f"{module}/{ref.path}"
        if ref_key in inlined:
            continue
        ref_target, ref_status = _resolve_section_path(sections_root, ref.path)
        if ref_status != "found" or ref_target is None:
            continue
        _, _, ref_body = pipeline.parse_section_file(ref_target.read_text(encoding="utf-8"))
        snippet = ref_body[:PREP_REF_CHARS] + (" […]" if len(ref_body) > PREP_REF_CHARS else "")
        parts.append(f"\n--- {ref.name} ({module}/{ref.path}) ---\n{snippet}")
        inlined.add(ref_key)
        refs_remaining -= 1

    return "\n".join(parts), refs_remaining


def _section_pointer(workspace: Workspace, path: str) -> str | None:
    """One-line 'path: breadcrumb' for a related section, or None if it can't
    resolve. Cheap (a local read for the breadcrumb), no body or stat blocks."""
    module, _, rest = path.partition("/")
    if not rest:
        return None
    sections_root = (workspace.book_dir(module) / "sections").resolve()
    if not sections_root.is_dir():
        return None

    from openadventure.engine.tools.rules_tools import _resolve_section_path

    target, status = _resolve_section_path(sections_root, rest)
    if status != "found" or target is None:
        return None
    title, breadcrumb, _ = pipeline.parse_section_file(target.read_text(encoding="utf-8"))
    matched = target.relative_to(sections_root).as_posix()
    label = breadcrumb or title or matched
    return f"- {module}/{matched}: {label}"


def location_prep(
    workspace: Workspace,
    module_path: str | None,
    extra_paths: list[str] | None = None,
    *,
    char_budget: int = PREP_DEFAULT_CHARS,
) -> str | None:
    """Canonical text for the scene's current keyed location, with referenced
    stat blocks inlined. None when no path resolves to ingested text (homebrew
    scene, unset/unknown paths, or a module that isn't ingested).

    ``module_path`` plus any ``extra_paths`` are each '<module>/<section>.md' in
    an ingested book. Sections are inlined in full (module_path first) until
    either ``PREP_FULL_SECTIONS`` or ``char_budget`` is reached, whichever binds
    first; the primary section is always kept even if it alone exceeds the budget.
    Everything past that point is listed as ``read_campaign`` pointers rather than
    inlined, so the GM can list many extras without blowing the budget. Paths that
    don't resolve are skipped silently (that's what ``prep_notes`` is for), but if
    some resolve, those are returned."""
    paths: list[str] = []
    for path in [module_path, *(extra_paths or [])]:
        if path and path not in paths:
            paths.append(path)
    if not paths:
        return None

    sections: list[str] = []
    inlined: set[str] = set()
    refs_remaining = PREP_MAX_REFS
    used = 0
    full_limit = min(PREP_FULL_SECTIONS, len(paths))
    cutoff = full_limit  # index where the pointer tier begins
    for i in range(full_limit):
        rendered, refs_remaining = _section_prep(
            workspace, paths[i], inlined=inlined, refs_remaining=refs_remaining
        )
        if rendered is None:
            continue
        # always keep the first resolved section; demote the rest once the budget is hit
        if sections and used + len(rendered) > char_budget:
            cutoff = i
            break
        sections.append(rendered)
        used += len(rendered)

    pointer_paths = paths[cutoff:]
    considered = pointer_paths[:PREP_MAX_POINTERS]
    dropped = len(pointer_paths) - len(considered)
    pointers = [line for path in considered if (line := _section_pointer(workspace, path))]

    if not sections and not pointers:
        return None
    if pointers:
        block = ["Related sections (read_campaign to pull full text):", *pointers]
        if dropped:
            block.append(f"…and {dropped} more.")
        sections.append("\n".join(block))
    return "\n\n".join(sections)
