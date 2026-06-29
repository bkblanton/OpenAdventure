"""Agentic search over ingested sources (and, with a different root, campaign docs)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from openadventure.engine.tools.registry import Tool, ToolContext, ToolOutcome
from openadventure.engine.tools.search_render import (
    INLINE_HITS,
    render_hits,
    truncate_with_pointer,
)
from openadventure.ingest import embeddings, indexer, pipeline, xref

if TYPE_CHECKING:
    from openadventure.ingest.embeddings import EmbeddingBackend

READ_CHUNK_CHARS = 6000
INLINE_REF_CHARS = 1200  # per auto-pulled stat block / spell
MAX_INLINE_REFS = 2
INLINE_BODY_CHARS = 1800  # cap on each inlined hit's body before pointing at read_*


class SearchArgs(BaseModel):
    query: str = Field(description="Search terms, e.g. 'opportunity attack' or 'goblin stat block'")
    k: int = Field(default=5, ge=1, le=20, description="Number of results")


class CampaignSearchArgs(SearchArgs):
    scope: Literal["active", "all"] = Field(
        default="active",
        description=(
            "'active' (default) searches only the module currently in play; 'all' searches "
            "every ingested module; use only for deliberate callbacks or foreshadowing."
        ),
    )


class ReadArgs(BaseModel):
    section_path: str = Field(
        description="The exact section path returned by the search tool, including .md"
    )
    offset: int = Field(default=0, ge=0, description="Character offset for long sections")


OUTLINE_LIMIT = 80  # sections per outline page
OUTLINE_MAX = 400


class OutlineArgs(BaseModel):
    under: str = Field(
        default="",
        description=(
            "Restrict to sections whose breadcrumb contains this text, case-insensitive "
            "(a chapter or location name, e.g. 'Combat' or 'LOCATION 8'); '' lists everything."
        ),
    )
    start: int = Field(
        default=0, ge=0, description="Skip this many sections, to page a long outline"
    )
    limit: int = Field(
        default=OUTLINE_LIMIT, ge=1, le=OUTLINE_MAX, description="Max sections to list"
    )


class CampaignOutlineArgs(OutlineArgs):
    scope: Literal["active", "all"] = Field(
        default="active",
        description="'active' (default) outlines only the module in play; 'all' every module.",
    )


def _path_variants(section_path: str) -> list[str]:
    normalized = section_path.strip().replace("\\", "/")
    variants: list[str] = []

    def add(path: str) -> None:
        if path and path not in variants:
            variants.append(path)

    add(normalized)
    if normalized and not normalized.endswith(".md"):
        add(f"{normalized}.md")
    return variants


def _matching_leaf_paths(sections_root: Path, section_path: str) -> list[Path]:
    leaf = Path(section_path.replace("\\", "/")).name
    if not leaf or leaf in {".", ".."}:
        return []

    leaf_names = [leaf]
    if not leaf.endswith(".md"):
        leaf_names.insert(0, f"{leaf}.md")

    matches: dict[str, Path] = {}
    for leaf_name in leaf_names:
        for path in sections_root.rglob(leaf_name):
            if path.is_file():
                matches[str(path.resolve())] = path.resolve()
    return sorted(matches.values(), key=lambda path: path.as_posix())


def _resolve_section_path(sections_root: Path, section_path: str) -> tuple[Path | None, str]:
    normalized = section_path.strip().replace("\\", "/")
    direct_candidates = _path_variants(normalized)
    if not direct_candidates:
        return None, "empty"

    invalid = False
    for candidate in direct_candidates:
        target = (sections_root / candidate).resolve()
        if not target.is_relative_to(sections_root):
            invalid = True
            continue
        if target.is_file():
            return target, "found"

    if invalid:
        return None, "invalid"

    matches = [
        path
        for path in _matching_leaf_paths(sections_root, normalized)
        if path.is_relative_to(sections_root)
    ]
    if len(matches) == 1:
        return matches[0], "found"
    if len(matches) > 1:
        return None, "ambiguous"
    return None, "missing"


def _section_body(sections_root: Path, section_path: str) -> str | None:
    """Full body text of a section by its search-result path, or None if it
    can't be resolved, in which case search falls back to the FTS snippet."""
    target, status = _resolve_section_path(sections_root, section_path)
    if status != "found" or target is None:
        return None
    _, _, body = pipeline.parse_section_file(target.read_text(encoding="utf-8"))
    return body


def _search(
    root: Path,
    args: SearchArgs,
    backend: EmbeddingBackend | None = None,
    *,
    path_prefix: str = "",
    read_tool: str = "read_rules",
    inline: int = INLINE_HITS,
) -> ToolOutcome:
    db = root / indexer.INDEX_NAME
    if not db.is_file():
        return ToolOutcome(content="Error: no search index found.", summary="no index", ok=False)
    hits = embeddings.hybrid_search(root, args.query, args.k, backend)
    if not hits:
        return ToolOutcome(content=f"No sections matched {args.query!r}.", summary="0 results")
    sections_root = (root / "sections").resolve()
    xref_db = root / xref.XREF_NAME

    def _refs(hit) -> str:
        # names only here (prefix-safe across modules); read_* pulls full text
        refs = xref.references_for(xref_db, hit.path, limit=6)
        return "\n  ↳ references: " + ", ".join(r.name for r in refs) if refs else ""

    def _brief(hit) -> str:
        return f"{path_prefix}{hit.path} — {hit.breadcrumb}\n  …{hit.snippet}…" + _refs(hit)

    def _full(hit) -> str:
        # Top hits come back with their full text so the GM can act without a second
        # read_* round-trip; fall back to the snippet if the body can't be resolved.
        display_path = f"{path_prefix}{hit.path}"
        body = _section_body(sections_root, hit.path)
        if body is None:
            return _brief(hit)
        text = truncate_with_pointer(
            body, INLINE_BODY_CHARS, f"{read_tool} '{display_path}' for the rest"
        )
        return f"{display_path} — {hit.breadcrumb}\n{text}" + _refs(hit)

    return ToolOutcome(
        content=render_hits(hits, full=_full, brief=_brief, inline=inline),
        summary=f"{len(hits)} result{'s' if len(hits) != 1 else ''}",
    )


def _reference_block(
    root: Path, sections_root: Path, src_path: str, *, inline: bool, prefix: str
) -> str:
    """Cross-references for a section: a list of referenced entries, plus the
    full text of the top monster/spell pulled inline so the GM needn't search
    again. ``prefix`` namespaces displayed paths (e.g. '<module>/' for modules)."""
    refs = xref.references_for(root / xref.XREF_NAME, src_path)
    if not refs:
        return ""
    lines = ["", "Referenced entries:"]
    for ref in refs:
        lines.append(f"  - {ref.name} [{ref.kind}] → {prefix}{ref.path}")
    out = "\n".join(lines)
    if not inline:
        return out
    pulled: list[str] = []
    for ref in refs:
        if ref.kind not in ("monster", "spell") or len(pulled) >= MAX_INLINE_REFS:
            continue
        target, status = _resolve_section_path(sections_root, ref.path)
        if status != "found" or target is None:
            continue
        _, _, body = pipeline.parse_section_file(target.read_text(encoding="utf-8"))
        snippet = body[:INLINE_REF_CHARS]
        if len(body) > INLINE_REF_CHARS:
            snippet += " […]"
        pulled.append(f"\n\n--- {ref.name} ({prefix}{ref.path}) ---\n{snippet}")
    return out + "".join(pulled)


def _adjacent_block(root: Path, matched_path: str, *, prefix: str, read_tool: str) -> str:
    """Pointers to the sections immediately before and after this one in the source's
    reading order, so the GM can step through a book sequentially even when the target
    isn't in the context outline: a module section past the listed limit, or anything
    in the rules sources, which aren't outlined in context at all. Each pointer is a
    ready-to-use ``read_*`` path with its breadcrumb. Empty at a book's first/last
    section, or if the index can't be read."""
    order = indexer.sections_in_reading_order(root / indexer.INDEX_NAME)
    if not order:
        return ""
    paths = [path for path, _ in order]
    try:
        i = paths.index(matched_path)
    except ValueError:
        return ""
    lines = []
    if i + 1 < len(order):
        path, breadcrumb = order[i + 1]
        lines.append(f"  next → {prefix}{path} — {breadcrumb}")
    if i > 0:
        path, breadcrumb = order[i - 1]
        lines.append(f"  previous → {prefix}{path} — {breadcrumb}")
    if not lines:
        return ""
    return f"\n\nAdjacent sections in reading order ({read_tool} to open):\n" + "\n".join(lines)


def _read(
    root: Path, args: ReadArgs, *, ref_prefix: str = "", read_tool: str = "read_rules"
) -> ToolOutcome:
    sections_root = (root / "sections").resolve()
    target, status = _resolve_section_path(sections_root, args.section_path)
    if status == "invalid":
        return ToolOutcome(content="Error: invalid section path.", summary="bad path", ok=False)
    if status == "ambiguous":
        lookup_path = args.section_path.strip().replace("\\", "/")
        leaf = Path(lookup_path).name
        matches = [
            path.relative_to(sections_root).as_posix()
            for path in _matching_leaf_paths(sections_root, lookup_path)
            if path.is_relative_to(sections_root)
        ]
        choices = ", ".join(matches[:5])
        more = f", plus {len(matches) - 5} more" if len(matches) > 5 else ""
        return ToolOutcome(
            content=(
                f"Error: section path {leaf!r} is ambiguous: {choices}{more}. "
                "Use the full path returned by the search tool."
            ),
            summary="ambiguous",
            ok=False,
        )
    if target is None:
        return ToolOutcome(
            content=f"Error: no section at {args.section_path!r}. Use the search tool to find paths.",
            summary="not found",
            ok=False,
        )
    _, _, body = pipeline.parse_section_file(target.read_text(encoding="utf-8"))
    chunk = body[args.offset : args.offset + READ_CHUNK_CHARS]
    remaining = len(body) - (args.offset + len(chunk))
    if remaining > 0:
        chunk += (
            f"\n\n[…{remaining} more characters: call again with offset="
            f"{args.offset + READ_CHUNK_CHARS}]"
        )
    matched_path = target.relative_to(sections_root).as_posix()
    # only expand cross-refs on the first chunk so pagination doesn't repeat them
    chunk += _reference_block(
        root, sections_root, matched_path, inline=(args.offset == 0), prefix=ref_prefix
    )
    # reading-order navigation, once the section is fully read (last/only chunk), so the
    # GM can walk to the next section without a search even past the context outline
    if remaining <= 0:
        chunk += _adjacent_block(root, matched_path, prefix=ref_prefix, read_tool=read_tool)
    return ToolOutcome(content=chunk, summary=f"read {matched_path}")


def _outline(dirs: list[Path], args: OutlineArgs, *, read_tool: str, label: str) -> ToolOutcome:
    """A book's table of contents: every section as a ``read_*`` path with its
    breadcrumb, in reading order, windowed by ``start``/``limit`` and optionally
    filtered to a sub-tree by ``under``. Spans ``dirs`` in order (each section path
    namespaced by its book slug), so the GM can see structure that search wouldn't
    surface and that the context outline omits past its limit or doesn't carry at all
    (the rules). Bodies come from read_*; this stays a lightweight map."""
    rows: list[tuple[str, str]] = []
    for d in dirs:
        for path, breadcrumb in indexer.sections_in_reading_order(d / indexer.INDEX_NAME):
            if args.under and args.under.casefold() not in breadcrumb.casefold():
                continue
            rows.append((f"{d.name}/{path}", breadcrumb))
    if not rows:
        hint = f" matching {args.under!r}" if args.under else ""
        return ToolOutcome(content=f"No {label} sections{hint}.", summary="0 sections")
    window = rows[args.start : args.start + args.limit]
    if not window:
        return ToolOutcome(
            content=f"start={args.start} is past the {len(rows)} matching sections.",
            summary="0 sections",
        )
    lines = [f"- {path} — {breadcrumb}" for path, breadcrumb in window]
    shown_to = args.start + len(window)
    header = f"{len(rows)} {label} section{'s' if len(rows) != 1 else ''} in reading order"
    if args.under:
        header += f" under {args.under!r}"
    header += f" ({read_tool} a path to open it)."
    if args.start or shown_to < len(rows):
        header += f" Showing {args.start + 1}-{shown_to}."
    footer = ""
    if shown_to < len(rows):
        footer = f"\n…{len(rows) - shown_to} more; call again with start={shown_to}."
    return ToolOutcome(
        content=header + "\n" + "\n".join(lines) + footer,
        summary=f"{len(window)} of {len(rows)} sections",
    )


def make_rules_tools(
    source_dirs: list[Path], embed_backend: EmbeddingBackend | None = None
) -> list[Tool]:
    """search_rules/read_rules over one or more ingested sources.

    Every section path is prefixed by its source slug ('dnd5e/monsters/goblin.md'),
    even with a single source, so the GM can always tell which book (and so which
    system) a rule came from, and read_rules can route back to the right book. This
    is the same namespacing campaign search uses for modules."""
    dirs = [d for d in source_dirs if pipeline.is_ingested(d)]
    by_name = {d.name: d for d in dirs}

    def _no_index() -> ToolOutcome:
        return ToolOutcome(content="Error: no search index found.", summary="no index", ok=False)

    def search_handler(ctx: ToolContext, args: SearchArgs) -> ToolOutcome:
        if not dirs:
            return _no_index()
        results = []
        for d in dirs:
            outcome = _search(
                d,
                args,
                embed_backend,
                path_prefix=f"{d.name}/",
                read_tool="read_rules",
            )
            if outcome.ok and "No sections matched" not in outcome.content:
                results.append(outcome.content)
        if not results:
            return ToolOutcome(content=f"No sections matched {args.query!r}.", summary="0 results")
        return ToolOutcome(content="\n\n".join(results), summary="results found")

    def read_handler(ctx: ToolContext, args: ReadArgs) -> ToolOutcome:
        if not dirs:
            return _no_index()
        source, _, rest = args.section_path.partition("/")
        match = by_name.get(source)
        if match is not None and rest:
            return _read(
                match,
                ReadArgs(section_path=rest, offset=args.offset),
                ref_prefix=f"{source}/",
                read_tool="read_rules",
            )
        if len(dirs) == 1:
            # tolerate a bare path (no source prefix) when there's only one source
            return _read(dirs[0], args, ref_prefix=f"{dirs[0].name}/", read_tool="read_rules")
        return ToolOutcome(
            content="Error: use '<source>/<section path>' as returned by search_rules.",
            summary="bad path",
            ok=False,
        )

    def outline_handler(ctx: ToolContext, args: OutlineArgs) -> ToolOutcome:
        if not dirs:
            return _no_index()
        return _outline(dirs, args, read_tool="read_rules", label="rules")

    return [
        Tool(
            name="search_rules",
            description=(
                "Full-text search over this campaign's ingested sources (rules, spells, "
                "monsters, items, lore). The top hits come back with their full section "
                "text inlined: act on those directly; lower-ranked hits show a path and "
                "snippet, and read_rules pulls any section (or more of a truncated one) "
                "in full."
            ),
            args_model=SearchArgs,
            handler=search_handler,
            parallel_safe=True,
            read_only=True,
        ),
        Tool(
            name="read_rules",
            description=(
                "Read the full text of a source section by path (from search_rules). The "
                "result ends with the previous/next section in reading order, so you can "
                "step through a book in sequence without another search."
            ),
            args_model=ReadArgs,
            handler=read_handler,
            parallel_safe=True,
            read_only=True,
        ),
        Tool(
            name="outline_rules",
            description=(
                "List a source's sections in reading order as read_rules paths with their "
                "breadcrumbs: the table of contents the search tool doesn't give you. Use it "
                "to see a book's structure or find a chapter (e.g. character creation, "
                "advancement) when you don't know the keyword. Filter to a sub-tree with "
                "'under' (a chapter/heading name), and page a long book with start/limit. "
                "Bodies come from read_rules."
            ),
            args_model=OutlineArgs,
            handler=outline_handler,
            parallel_safe=True,
            read_only=True,
        ),
    ]


def make_campaign_tools(
    module_dirs: list[Path], embed_backend: EmbeddingBackend | None = None
) -> list[Tool]:
    """search_campaign/read_campaign over the campaign's attached module books.

    Each path is the module's slug in the shared library store, so ``module_dirs``
    are ``workspace.book_dir(slug)`` for the campaign's attached modules. Search
    defaults to the active module (``ctx.meta.active_module``) and namespaces
    every path by slug, the same way multi-source rules search does."""
    dirs = [d for d in module_dirs if pipeline.is_ingested(d)]
    by_name = {d.name: d for d in dirs}

    def search_handler(ctx: ToolContext, args: CampaignSearchArgs) -> ToolOutcome:
        active = getattr(ctx.meta, "active_module", None)
        scoped = args.scope == "active" and active in by_name
        search_dirs = [by_name[active]] if scoped else dirs
        results = []
        for module_dir in search_dirs:
            outcome = _search(
                module_dir,
                args,
                embed_backend,
                path_prefix=f"{module_dir.name}/",
                read_tool="read_campaign",
                # One full hit, not two: the result now persists in the tail as a
                # replayed tool_result, and a follow-up read_campaign of a lower hit
                # is cheap and likewise persists. Keeps the overlay lean.
                inline=1,
            )
            if outcome.ok and "No sections matched" not in outcome.content:
                results.append(outcome.content)
        if not results:
            hint = (
                " Searched only the active module; retry with scope='all' to search every module."
                if scoped
                else ""
            )
            return ToolOutcome(
                content=f"No module content matched {args.query!r}.{hint}", summary="0 results"
            )
        return ToolOutcome(content="\n".join(results), summary="results found")

    def read_handler(ctx: ToolContext, args: ReadArgs) -> ToolOutcome:
        module, _, rest = args.section_path.partition("/")
        if not rest:
            if len(dirs) == 1:
                return _read(
                    dirs[0], args, ref_prefix=f"{dirs[0].name}/", read_tool="read_campaign"
                )
            return ToolOutcome(
                content="Error: use '<module>/<section path>' as returned by search_campaign.",
                summary="bad path",
                ok=False,
            )
        target_dir = by_name.get(module)
        if target_dir is None:
            return ToolOutcome(
                content=f"Error: no module {module!r} attached to this campaign.",
                summary="bad path",
                ok=False,
            )
        return _read(
            target_dir,
            ReadArgs(section_path=rest, offset=args.offset),
            ref_prefix=f"{module}/",
            read_tool="read_campaign",
        )

    def outline_handler(ctx: ToolContext, args: CampaignOutlineArgs) -> ToolOutcome:
        active = getattr(ctx.meta, "active_module", None)
        scoped = args.scope == "active" and active in by_name
        outline_dirs = [by_name[active]] if scoped else dirs
        return _outline(outline_dirs, args, read_tool="read_campaign", label="module")

    return [
        Tool(
            name="search_campaign",
            description=(
                "Full-text search over this campaign's adventure module documents "
                "(room descriptions, read-aloud text, NPCs, plot). The top hits come "
                "back with their full section text inlined; lower-ranked hits show a "
                "path and snippet. Searches the module currently in play by default; "
                "pass scope='all' to search every ingested module. Read more with "
                "read_campaign."
            ),
            args_model=CampaignSearchArgs,
            handler=search_handler,
            parallel_safe=True,
            read_only=True,
        ),
        Tool(
            name="read_campaign",
            description=(
                "Read a module section by exact path from search_campaign, including .md. "
                "Use the full first-line path, e.g. '<module>/<section>.md'. The result ends "
                "with the previous/next section in reading order, so you can step through the "
                "module in sequence (e.g. past the limit of the context outline) without "
                "another search."
            ),
            args_model=ReadArgs,
            handler=read_handler,
            parallel_safe=True,
            read_only=True,
        ),
        Tool(
            name="outline_campaign",
            description=(
                "List the active module's sections in reading order as read_campaign paths "
                "with their breadcrumbs: the keyed table of contents. Use it to see the "
                "module's structure or reach a section past the context outline's limit. "
                "Filter to one area with 'under' (a location/heading name), page with "
                "start/limit, and pass scope='all' to span every module. Bodies come from "
                "read_campaign; this is GM-only structure, never read it aloud."
            ),
            args_model=CampaignOutlineArgs,
            handler=outline_handler,
            parallel_safe=True,
            read_only=True,
        ),
    ]
