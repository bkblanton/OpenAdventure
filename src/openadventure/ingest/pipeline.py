"""Ingestion orchestration: source file -> sections/*.md + index.sqlite + manifest."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from openadventure.ingest import embeddings, indexer, xref
from openadventure.ingest.extractors import select_extractor
from openadventure.ingest.progress import PHASE_INDEX, ProgressFn, report
from openadventure.ingest.sections import Section, assign_paths
from openadventure.store import snapshots

if TYPE_CHECKING:
    from openadventure.ingest.embeddings import EmbeddingBackend

INGESTER_VERSION = 1

# A stored section row, the unit every derived index is built from.
Row = tuple[str, str, str, str, str]  # (title, breadcrumb, body, path, kind)


def _frontmatter(section: Section) -> str:
    lines = [
        "---",
        f"title: {section.title!r}",
        f"breadcrumb: {section.breadcrumb!r}",
        f"kind: {section.kind}",
        f"order: {section.order}",
    ]
    if section.start_page:
        lines.append(f"pages: {section.start_page}-{section.end_page or section.start_page}")
    lines.append("---")
    return "\n".join(lines)


_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def parse_section_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter fields, body) from a stored section file. Values are
    unquoted; unknown keys pass through, so new fields need no parser change."""
    fields: dict[str, str] = {}
    match = _FM_RE.match(text)
    body = text
    if match:
        body = text[match.end() :]
        for line in match.group(1).splitlines():
            key, sep, value = line.strip().partition(":")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
                value = value[1:-1]
            fields[key] = value
    return fields, body.strip()


def parse_section_file(text: str) -> tuple[str, str, str]:
    """Return (title, breadcrumb, body) from a stored section file."""
    fields, body = parse_section_frontmatter(text)
    return fields.get("title", ""), fields.get("breadcrumb", ""), body


def section_rows(dest: Path) -> list[Row]:
    """Read the stored markdown back into index rows, in the source's reading order
    (the ``order`` frontmatter ordinal written at ingest); files without it fall
    back to filename order, after the ordered ones. Rows are inserted in this order,
    so the FTS rowid keeps recovering reading order across a reindex. The source of
    truth for every rebuild, so hand-edits to the .md files survive."""
    sections_dir = dest / "sections"
    entries: list[tuple[tuple[int, str], Row]] = []
    for path in sections_dir.rglob("*.md"):
        fields, body = parse_section_frontmatter(path.read_text(encoding="utf-8"))
        rel = path.relative_to(sections_dir).as_posix()
        try:
            order = int(fields.get("order", ""))
        except ValueError:
            order = 1_000_000  # no ordinal (pre-``order`` ingest): sort last, by filename
        entries.append(
            (
                (order, rel),
                (
                    fields.get("title") or path.stem,
                    fields.get("breadcrumb") or path.stem,
                    body,
                    rel,
                    fields.get("kind") or "section",
                ),
            )
        )
    entries.sort(key=lambda entry: entry[0])
    return [row for _, row in entries]


def build_indexes(
    dest: Path,
    rows: list[Row],
    *,
    embed_backend: EmbeddingBackend | None = None,
    progress: ProgressFn | None = None,
) -> None:
    """Build every derived artifact for a corpus from its section rows: the FTS5
    keyword index, the cross-reference graph, and (when an embedding backend is
    given) the window-vector index. The single path shared by ``ingest`` and
    ``reindex`` so the three can never drift."""
    report(progress, PHASE_INDEX, 0, 1)
    indexer.create_index(
        dest / indexer.INDEX_NAME, [(t, b, body, path) for t, b, body, path, _ in rows]
    )
    xref.build(dest / xref.XREF_NAME, rows)
    report(progress, PHASE_INDEX, 1, 1)
    embed_db = dest / embeddings.EMBEDDINGS_NAME
    if embed_backend is not None:
        embeddings.build_embeddings(embed_db, rows, embed_backend, progress=progress)
    elif embed_db.exists():
        # No backend to rebuild with, but a prior build left a vector index whose
        # windows reference the rows we just replaced. The FTS and xref indexes are
        # always rebuilt above, so leaving this one behind would be the lone way a
        # rebuilt corpus still points at stale (even removed) sections; drop it.
        embed_db.unlink()


def source_digest(source: Path) -> str:
    """SHA-256 of a source's bytes, for the manifest's change-detection. A file
    is hashed directly; a directory (a future folder source, e.g. an Obsidian
    vault) is hashed over every contained file in sorted order, so the digest is
    stable and changes if anything in the tree does."""
    if source.is_dir():
        digest = hashlib.sha256()
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            digest.update(path.relative_to(source).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
        return digest.hexdigest()
    return hashlib.sha256(source.read_bytes()).hexdigest()


def extract_sections(
    source: Path,
    *,
    pages: tuple[int, int] | None = None,
    progress: ProgressFn | None = None,
) -> list[Section]:
    """Sections for ``source``, via the extractor that handles its type. Thin
    wrapper over the registry for callers that want only the sections."""
    return select_extractor(source).extract(source, pages=pages, progress=progress).sections


def ingest(
    source: Path,
    dest: Path,
    *,
    pages: tuple[int, int] | None = None,
    book_type: str | None = None,
    embed_backend: EmbeddingBackend | None = None,
    progress: ProgressFn | None = None,
) -> dict:
    """Ingest a document into `dest/` (sections/, index.sqlite, manifest.json).
    Returns the manifest; a poor-quality PDF adds a ``warning`` key.

    ``book_type`` records what the book is, ``source`` (rules/reference) or
    ``module`` (adventure), in the manifest, so a campaign can only attach it
    in the matching bucket. Omitting it leaves the book untyped (attachable as
    either), which is how books ingested before types existed behave.

    ``pages`` restricts a PDF to a 1-based inclusive page range, so a book that
    combines rules and an adventure can be ingested as two pieces (the rules as
    a source, the adventure as a campaign module) from the one file."""
    if not source.exists():
        raise FileNotFoundError(source)
    result = select_extractor(source).extract(source, pages=pages, progress=progress)
    sections = result.sections
    if not sections:
        raise ValueError(f"no sections could be extracted from {source.name}")
    assign_paths(sections)
    # Extractors that classify by structure (PDF font geometry) tag
    # monster/spell/location themselves; markdown and txt carry none, so
    # classify those from the body before the kind is stored.
    for section in sections:
        if section.kind == "section":
            inferred = xref.classify_kind(section.title, section.body)
            if inferred:
                section.kind = inferred

    sections_dir = dest / "sections"
    if sections_dir.exists():
        import shutil

        shutil.rmtree(sections_dir)
    for order, section in enumerate(sections):
        section.order = order  # persist reading order so a reindex can recover it
        path = sections_dir / section.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_frontmatter(section) + "\n\n" + section.body + "\n", encoding="utf-8")

    rows: list[Row] = [(s.title, s.breadcrumb, s.body, s.path, s.kind) for s in sections]
    build_indexes(dest, rows, embed_backend=embed_backend, progress=progress)

    manifest = {
        "source": source.name,
        "sha256": source_digest(source),
        "ingested_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "section_count": len(sections),
        "ingester_version": INGESTER_VERSION,
    }
    if book_type:
        manifest["type"] = book_type
    if pages:
        manifest["pages"] = f"{pages[0]}-{pages[1]}"
    if result.warning:
        manifest["warning"] = result.warning
    if result.image_only_pages:
        manifest["image_only_pages"] = result.image_only_pages
    manifest.update(result.manifest)
    snapshots.save_json(dest / "manifest.json", manifest)
    return manifest


def image_only_pages_note(manifest: dict) -> str | None:
    """A one-line heads-up about pages that ingested with no text (scanned sheets,
    handouts, maps), or None when there were none. Shared by the CLI and REPL."""
    pages = manifest.get("image_only_pages")
    if not pages:
        return None
    shown = ", ".join(str(p) for p in pages[:12])
    more = f" (+{len(pages) - 12} more)" if len(pages) > 12 else ""
    return (
        f"{len(pages)} page(s) had no extractable text, likely scanned character "
        f"sheets, handouts, or maps: {shown}{more}. That content was not ingested "
        "(view one with: openadventure inspect <file> --page N)."
    )


def index_report(dest: Path) -> dict:
    """Health snapshot of a corpus's derived indexes for reindex output:
    counts plus a dangling-cross-ref check (a proxy for parse damage)."""
    import sqlite3

    sections_dir = dest / "sections"
    report: dict = {
        "sections": sum(1 for _ in sections_dir.rglob("*.md")) if sections_dir.is_dir() else 0,
        "entities": 0,
        "edges": 0,
        "dangling": 0,
        "windows": 0,
        "embed_model": None,
    }

    xdb = dest / xref.XREF_NAME
    if xdb.is_file():
        con = sqlite3.connect(xdb)
        try:
            report["entities"] = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            report["edges"] = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            dsts = [r[0] for r in con.execute("SELECT DISTINCT dst_path FROM edges")]
        except sqlite3.OperationalError:
            dsts = []
        finally:
            con.close()
        report["dangling"] = sum(1 for d in dsts if not (sections_dir / d).is_file())

    edb = dest / embeddings.EMBEDDINGS_NAME
    if edb.is_file():
        identity = embeddings.stored_identity(edb)
        report["embed_model"] = identity[0] if identity else None
        con = sqlite3.connect(edb)
        try:
            report["windows"] = con.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        finally:
            con.close()
    return report


def reindex(
    dest: Path,
    *,
    embed_backend: EmbeddingBackend | None = None,
    progress: ProgressFn | None = None,
) -> int:
    """Rebuild every derived index from the markdown files (hand-edits survive):
    FTS5 keyword index, the cross-reference graph, and (when an embedding
    backend is given) the window-vector index."""
    rows = section_rows(dest)
    build_indexes(dest, rows, embed_backend=embed_backend, progress=progress)
    return len(rows)


def is_ingested(dest: Path) -> bool:
    return (dest / "manifest.json").is_file() and (dest / indexer.INDEX_NAME).is_file()
