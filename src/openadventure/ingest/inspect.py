"""Ingestion diagnostics for the ``openadventure inspect`` command.

Three plain-text views onto how a document extracts, for debugging PDFs whose
layout the heading/column heuristics don't yet handle:

* :func:`summary`: pages, fonts, detected headings, and the section tree with
  word counts (the first thing to look at: is the document split sensibly?).
* :func:`bodies`: every section's full text (did the reading order come out
  in order? did furniture/stat blocks leak in?).
* :func:`page`: one page's raw line geometry, font, and weight, plus the
  detected column split (why did a heading/column get mis-classified?).
* :func:`tables`: table detection: find_tables fragments (and where they jam),
  how they merge, borderless detections, and the final rendered markdown (did a
  table come out whole, or fragment / drop rows / mangle?).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from statistics import median

from openadventure.ingest.extract import (
    Line,
    _column_geometry,
    assess_quality,
    extract_pdf,
    raw_page_lines,
    table_diagnostics,
)
from openadventure.ingest.sections import (
    _is_heading,
    _is_location_header,
    _level_map,
    _merge_wrapped_headings,
    sections_from_pdf,
)

HUGE_WORDS = 3000  # a section this big usually means a missed heading
TINY_WORDS = 20


def summary(path: Path) -> str:
    content = extract_pdf(path)
    out: list[str] = [f"{path.name}", f"  pages={content.page_count} body_size={content.body_size}"]

    warning = assess_quality(content)
    if warning:
        out.append(f"\n  ** QUALITY WARNING: {warning}")

    out.append(f"  embedded TOC entries: {len(content.toc)}")
    for level, title, page in content.toc[:10]:
        out.append(f"    toc L{level} p{page}: {title!r}")

    sizes: Counter[float] = Counter()
    for line in content.lines:
        if not line.is_table:
            sizes[line.size] += len(line.text)
    out.append("\nfont-size distribution (size: total chars):")
    for size, chars in sorted(sizes.items(), reverse=True):
        out.append(f"  {size:5.1f}: {chars}")

    lines = _merge_wrapped_headings(content.lines, content.body_size)
    headings = [ln for ln in lines if _is_heading(ln, content.body_size)]
    levels = _level_map([ln.size for ln in headings])
    out.append(f"\ndetected headings: {len(headings)}")
    for ln in headings:
        loc = " +loc" if _is_location_header(ln) else ""
        out.append(
            f"  L{levels.get(ln.size, '?')} sz{ln.size} bold={ln.bold} p{ln.page}{loc}: {ln.text!r}"
        )

    sections = sections_from_pdf(content)
    counts = [s.word_count() for s in sections]
    out.append(f"\nsections produced: {len(sections)}")
    if counts:
        huge = sum(1 for w in counts if w > HUGE_WORDS)
        tiny = sum(1 for w in counts if w < TINY_WORDS)
        out.append(
            f"  word counts: min={min(counts)} max={max(counts)} median={int(median(counts))}"
        )
        out.append(f"  warnings: {huge} huge (>{HUGE_WORDS}w), {tiny} tiny (<{TINY_WORDS}w)")
    for s in sections:
        flag = "  <-- HUGE" if s.word_count() > HUGE_WORDS else ""
        out.append(
            f"  [{s.word_count():5d}w] L{s.level} p{s.start_page}-{s.end_page}: {s.breadcrumb}{flag}"
        )
    return "\n".join(out)


def bodies(path: Path) -> str:
    out: list[str] = []
    for s in sections_from_pdf(extract_pdf(path)):
        out.append("#" * 70)
        out.append(
            f"TITLE: {s.title!r}  L{s.level}  p{s.start_page}-{s.end_page}  ({s.word_count()}w)"
        )
        out.append(f"CRUMB: {s.breadcrumb}")
        out.append("-" * 70)
        out.append(s.body)
        out.append("")
    return "\n".join(out)


def page(path: Path, page_number: int) -> str:
    raw, width = raw_page_lines(path, page_number)
    geom = _column_geometry(
        [
            Line(text=r.text, size=r.size, bold=r.bold, page=r.page, y=r.y, x0=r.x0, x1=r.x1)
            for r in raw
        ],
        width,
    )
    if geom is None:
        geom_desc = "single column"
    else:
        splits, margins = geom
        geom_desc = (
            f"{len(margins)} columns; margins={[round(m) for m in margins]} "
            f"splits={[round(s) for s in splits]}"
        )
    out = [
        f"{path.name} - page {page_number}",
        f"  width={width:.0f}  midpoint={width / 2:.0f}",
        f"  column geometry: {geom_desc}",
        "",
    ]
    for r in sorted(raw, key=lambda r: (r.y, r.x0)):
        out.append(
            f"  y={r.y:6.1f} x0={r.x0:6.1f} x1={r.x1:6.1f} sz={r.size:4.1f} "
            f"{'B' if r.bold else ' '} {r.font[:22]:22} {r.text!r}"
        )
    return "\n".join(out)


def tables(path: Path, page_number: int | None = None) -> str:
    """Table diagnostics. With ``page_number``, a deep dive on that page:
    find_tables fragments (with jam counts), how they cluster for merging, any
    borderless detections, and the final rendered markdown. Without it, a
    document-wide census of pages that carry tables and any flags worth a look."""
    if page_number is not None:
        d = table_diagnostics(path, page_number)[0]
        out = [
            f"{path.name} - page {d.page} tables",
            f"  has_table_grid: {d.has_grid}",
            f"  find_tables fragments: {len(d.fragments)}",
        ]
        for rows, cols, bbox, first, jammed in d.fragments:
            flag = f"  JAMMED({jammed})" if jammed else ""
            out.append(f"    [{rows}x{cols}] bbox={bbox} first={first!r}{flag}")
        out.append(f"  merge clusters (fragments each): {d.clusters or '-'}")
        out.append(f"  borderless detections: {len(d.borderless)}")
        for rows, cols, bbox in d.borderless:
            out.append(f"    [{rows}x{cols}] bbox={bbox}")
        out.append(f"\n  emitted tables: {len(d.emitted)}")
        for i, md in enumerate(d.emitted, 1):
            out.append(f"  --- table {i} ---")
            out.append(md)
        return "\n".join(out)

    out = [f"{path.name} - table census (use --page N for detail)"]
    seen = False
    for d in table_diagnostics(path, with_emitted=False):
        if not (d.fragments or d.borderless):
            continue
        seen = True
        flags = []
        jam = sum(j for *_, j in d.fragments)
        if jam:
            flags.append(f"{jam} jammed")
        if any(s > 1 for s in d.clusters):
            flags.append(f"merge={[s for s in d.clusters if s > 1]}")
        if d.borderless:
            flags.append(f"{len(d.borderless)} borderless")
        note = f"  ({', '.join(flags)})" if flags else ""
        out.append(f"  p{d.page}: {len(d.fragments)} grid fragment(s), clusters={d.clusters}{note}")
    if not seen:
        out.append("  (no tables detected)")
    return "\n".join(out)
