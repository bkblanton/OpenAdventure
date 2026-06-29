"""PDF text extraction. The ONLY module that imports pymupdf (fitz)."""

from __future__ import annotations

import bisect
import os
import re
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import pymupdf

from openadventure.ingest import tables as tables_mod
from openadventure.ingest.progress import PHASE_EXTRACT, ProgressFn, report

# PyMuPDF prints a one-time advert for its separate ``pymupdf_layout`` package
# the first time ``find_tables`` runs. We do our own layout analysis and never
# call into it, so the suggestion is noise. Silence it (set the var to "1" to
# bring it back). Read at call time, so setting it before extraction suffices.
os.environ.setdefault("PYMUPDF_SUGGEST_LAYOUT_ANALYZER", "0")

BOLD_FLAG = 1 << 4  # pymupdf span flag bit for bold

# Glyphs a folio is commonly framed with: rules, bullets, guillemets, and
# diamonds (e.g. "~ 7 ~", "« 1 »", "❖ 12 ❖"). Kept in one place so both
# page-number patterns recognise the same decoration.
_FOLIO_DECOR = r"\s~\-–—•·|=_'’«»‹›❖◆◇♦✦✧*°"
# A page number alone on its line, optionally wrapped in rule/decoration glyphs
# (e.g. "7", "~ 7 ~", "« 12 »"). Footer furniture, not content.
_PAGE_NUMBER = re.compile(rf"^[{_FOLIO_DECOR}]*\d{{1,4}}[{_FOLIO_DECOR}]*$")
# A line that is only digits/spaces/decoration (e.g. "1 0", "2 2") whose digits
# equal the page index. Some modules set the folio in a large display font with
# letter-spacing, which would otherwise read as a heading rather than furniture.
_DIGITS_AND_SPACES = re.compile(rf"^[{_FOLIO_DECOR}]*\d[\d{_FOLIO_DECOR}]*$")


def _is_page_number(line: Line) -> bool:
    if _PAGE_NUMBER.match(line.text):
        return True
    if _DIGITS_AND_SPACES.match(line.text):
        digits = "".join(ch for ch in line.text if ch.isdigit())
        return digits == str(line.page)
    return False


@dataclass
class Line:
    text: str
    size: float  # dominant font size
    bold: bool
    page: int  # 1-based
    y: float
    x0: float = 0.0
    x1: float = 0.0
    is_table: bool = False  # text is a pre-rendered markdown table


@dataclass
class PdfContent:
    toc: list[tuple[int, str, int]] = field(default_factory=list)  # (level, title, page)
    lines: list[Line] = field(default_factory=list)  # reading order, headers/footers dropped
    page_count: int = 0
    body_size: float = 10.0  # dominant body-text font size
    # 1-based page numbers that carry an image but almost no text: scanned
    # handouts, character sheets, maps. Their content is NOT in `lines`; surfaced
    # at ingest so the loss isn't silent.
    image_only_pages: list[int] = field(default_factory=list)


def _jammed_rows(rows: list[list[str]]) -> int:
    """Rows where the whole line landed in one cell and the rest are empty -- the
    signature of find_tables' alternating-row merge bug."""
    return sum(1 for r in rows if len(r) >= 2 and sum(1 for c in r if c) == 1)


def _token_bag(rows: list[list[str]]) -> Counter:
    return Counter(tok for row in rows for cell in row for tok in cell.split())


def _cell_reading_order(cell_words: list[tuple]) -> str:
    """Join a cell's words in reading order: top line to bottom, left to right
    within each line. A wrapped multi-line cell would otherwise interleave its
    lines if sorted by x alone."""
    lines: list[list[tuple]] = []
    for w in sorted(cell_words, key=lambda w: (w[1] + w[3]) / 2):
        center_y = (w[1] + w[3]) / 2
        if lines and center_y - (lines[-1][-1][1] + lines[-1][-1][3]) / 2 <= 4:
            lines[-1].append(w)
        else:
            lines.append([w])
    parts = []
    for line in lines:
        parts.extend(w[4] for w in sorted(line, key=lambda w: w[0]))
    return " ".join(parts)


def _column_edges(table) -> tuple[list[float], float, float] | None:
    """Column left-edges, table left, and table right, taken from the cells
    find_tables split correctly. Returns None if the grid geometry is unusable."""
    ncol = table.col_count
    if ncol < 2:
        return None
    edges: list[float] = []
    for ci in range(ncol):
        xs = [
            r.cells[ci][0]
            for r in table.rows
            if r.cells[ci] and r.cells[ci][2] - r.cells[ci][0] > 1
        ]
        if not xs:
            return None
        edges.append(median(xs))
    rights = [r.cells[ncol - 1][2] for r in table.rows if r.cells[ncol - 1]]
    if not rights:
        return None
    return edges, edges[0], max(rights)


def _bucket_by_column(line_words: list[tuple], edges: list[float]) -> list[str]:
    """Assign words to columns by x and read each cell in top-to-bottom order."""
    buckets: list[list[tuple]] = [[] for _ in edges]
    for w in line_words:
        center_x = (w[0] + w[2]) / 2
        ci = 0
        for k in range(len(edges)):
            if center_x >= edges[k] - 2:
                ci = k
        buckets[ci].append(w)
    return [_cell_reading_order(b) for b in buckets]


def _reextract_table(table, words: list[tuple]) -> list[list[str]] | None:
    """Re-derive a jammed table's cells from positioned words. find_tables detects
    the column grid correctly (the rows it splits right prove it) but botches the
    word-to-cell assignment on alternating rows; here we take the column x-edges
    from the cells it did split and re-bucket every row's words by x against them,
    clipped to the table's own column span so an adjacent prose column can't leak
    in. Each cell is read top-to-bottom so wrapped lines keep their order. Returns
    None when the grid geometry is unusable."""
    try:
        info = _column_edges(table)
        if info is None:
            return None
        edges, left, right = info
        grid: list[list[str]] = []
        for r in table.rows:
            boxes = [c for c in r.cells if c]
            if not boxes:
                return None
            y0, y1 = min(b[1] for b in boxes), max(b[3] for b in boxes)
            row_words = [
                w
                for w in words
                if y0 - 1 <= (w[1] + w[3]) / 2 <= y1 + 1
                and left - 2 <= (w[0] + w[2]) / 2 <= right + 2
            ]
            grid.append(_bucket_by_column(row_words, edges))
        return [r for r in grid if any(r)]
    except Exception:
        return None


def _recover_trailing_row(table, words: list[tuple]) -> list[list[str]]:
    """find_tables sometimes stops one row short of a zebra-striped table, leaving
    the last row just below the detected grid. Recover that single row (with its
    wrapped continuation lines) when it is body-font (not the next section
    heading), respects the column gaps rather than flowing across them like prose,
    and isn't a ``*`` footnote. Returns ``[]`` otherwise, so it can only ever add a
    row that clearly belongs to the table."""
    try:
        info = _column_edges(table)
        if info is None:
            return []
        edges, left, right = info
        bx0, by0, bx1, by1 = table.bbox
        heights = [r.cells[0][3] - r.cells[0][1] for r in table.rows if r.cells[0]]
        row_h = median(heights) if heights else 14.0
        inbox = [
            w[3] - w[1]
            for w in words
            if by0 <= (w[1] + w[3]) / 2 <= by1 and bx0 <= (w[0] + w[2]) / 2 <= bx1
        ]
        body_h = median(inbox) if inbox else 9.0
        below = sorted(
            (
                w
                for w in words
                if (w[1] + w[3]) / 2 > by1 + 1 and left - 2 <= (w[0] + w[2]) / 2 <= right + 2
            ),
            key=lambda w: (w[1] + w[3]) / 2,
        )
        if not below:
            return []
        # cluster the words below into lines
        lines: list[list] = []
        for w in below:
            cy = (w[1] + w[3]) / 2
            if lines and cy - lines[-1][0] <= 4:
                lines[-1][0] = cy
                lines[-1][1].append(w)
            else:
                lines.append([cy, [w]])

        interior = edges[1:]
        col1 = edges[1] if len(edges) > 1 else right

        def has_col0(lw):
            return any(edges[0] - 2 <= (w[0] + w[2]) / 2 < col1 - 2 for w in lw)

        def bad(lw):  # a heading (larger font) or prose (crosses a column gap)
            tall = median([w[3] - w[1] for w in lw]) > body_h * 1.25
            crosses = any(any(w[0] < e - 2 and w[2] > e + 2 for e in interior) for w in lw)
            return tall or crosses

        first_cy, first_line = lines[0]
        if (first_cy - by1) / row_h > 1.3 or bad(first_line) or not has_col0(first_line):
            return []
        row_words = list(first_line)
        for cy, lw in lines[1:]:  # gather wrapped continuation lines (no new key cell)
            if has_col0(lw) or bad(lw) or (cy - first_cy) / row_h > 3:
                break
            row_words += lw
        cells = _bucket_by_column(row_words, edges)
        if sum(1 for c in cells if c) < 2 or " ".join(cells).lstrip().startswith("*"):
            return []
        return [cells]
    except Exception:
        return []


def _render_table(rows: list[list[str]]) -> str:
    rows = [row for row in rows if any(row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    for row in rows[1:]:
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def _table_rows(table, words: list[tuple], *, recover_trailing: bool = True) -> list[list[str]]:
    """One find_tables table as cleaned cell rows: re-derive jammed grids from word
    geometry (kept only when it unjams and preserves every token), optionally
    recover a dropped trailing row, and re-split any alternating merge. The merge
    path passes ``recover_trailing=False`` because its gap recovery already fills
    the rows between fragments."""
    try:
        rows = table.extract()
    except Exception:
        return []
    rows = [
        [("" if cell is None else str(cell)).replace("\n", " ").strip() for cell in row]
        for row in rows
    ]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    if _jammed_rows(rows):
        regrid = _reextract_table(table, words)
        if (
            regrid is not None
            and _jammed_rows(regrid) < _jammed_rows(rows)
            and _token_bag(regrid) == _token_bag(rows)
        ):
            rows = regrid
    if recover_trailing:
        rows = rows + _recover_trailing_row(table, words)
    return tables_mod.repair_merged_rows(rows)


def _table_markdown(table, words: list[tuple]) -> str:
    return _render_table(_table_rows(table, words))


def _mergeable(top, bot, words: list[tuple], page_body_h: float) -> bool:
    """Whether two vertically-adjacent find_tables fragments are really one table
    that find_tables split (commonly when it detects only the alternating rows of
    a zebra table). Requires the same column count and aligned column edges, real
    horizontal overlap (not a side-by-side neighbour), a small vertical gap, and
    no section heading sitting in the gap."""
    if top.col_count != bot.col_count:
        return False
    # Only merge single-row fragments -- the signature of find_tables splitting a
    # zebra table into its alternating rows (CoC damage table, PF/3.5 ability
    # tables). Anything find_tables detects with two or more rows is a real table
    # (e.g. a per-level spell list) and must not fuse with its neighbour, whose
    # heading is often barely larger than the body and so unsafe to detect by font.
    if top.row_count > 1 or bot.row_count > 1:
        return False
    et, eb = _column_edges(top), _column_edges(bot)
    if et is None or eb is None:
        return False
    if any(abs(a - b) > 8 for a, b in zip(et[0], eb[0], strict=True)):
        return False
    # overlap horizontally: the same column block, not two columns side by side
    if not (top.bbox[0] < bot.bbox[2] - 5 and bot.bbox[0] < top.bbox[2] - 5):
        return False
    heights = [r.cells[0][3] - r.cells[0][1] for r in top.rows if r.cells[0]]
    row_h = median(heights) if heights else 14.0
    # The gap holds the row(s) find_tables skipped. One skipped row -- even a
    # wrapped multi-line one -- stays under ~3 row-heights; a larger gap means a
    # heading + column header sits between two separate tables (per-level spell
    # lists), so don't merge across it.
    gap = bot.bbox[1] - top.bbox[3]
    if gap < -2 or gap > row_h * 3.0:
        return False
    left, right = et[1], et[2]
    # body height from the fragment's own cell text (the page median is inflated by
    # headings/prose and would miss a heading only ~1.2x the body, as in spell lists)
    inbox = [
        w[3] - w[1]
        for w in words
        if top.bbox[1] <= (w[1] + w[3]) / 2 <= top.bbox[3]
        and left - 2 <= (w[0] + w[2]) / 2 <= right + 2
    ]
    body_h = median(inbox) if inbox else page_body_h
    # cluster the gap into lines; a heading-sized line means these are separate
    # tables (a per-level "Level N Spells" title between two spell lists), not one
    gap_words = sorted(
        (
            w
            for w in words
            if top.bbox[3] - 1 < (w[1] + w[3]) / 2 < bot.bbox[1] + 1
            and left - 2 <= (w[0] + w[2]) / 2 <= right + 2
        ),
        key=lambda w: (w[1] + w[3]) / 2,
    )
    gap_lines: list[list] = []
    for w in gap_words:
        cy = (w[1] + w[3]) / 2
        if gap_lines and cy - gap_lines[-1][0] <= 4:
            gap_lines[-1][0] = cy
            gap_lines[-1][1].append(w)
        else:
            gap_lines.append([cy, [w]])
    return not any(median([w[3] - w[1] for w in lw]) > body_h * 1.15 for _, lw in gap_lines)


def _cluster_tables(tables: list, words: list[tuple], body_h: float) -> list[list]:
    """Group find_tables tables top-to-bottom into vertical merge-clusters."""
    clusters: list[list] = []
    for t in sorted(tables, key=lambda t: (round(t.bbox[1]), t.bbox[0])):
        if clusters and _mergeable(clusters[-1][-1], t, words, body_h):
            clusters[-1].append(t)
        else:
            clusters.append([t])
    return clusters


def _gap_row(top_y: float, bot_y: float, edges: list[float], left: float, right: float, words):
    """Recover the row find_tables skipped between two fragments, reading its
    (possibly wrapped, multi-line) cells in order. Returns [] when the gap is
    empty."""
    if bot_y - top_y < 4:
        return []
    row_words = [
        w
        for w in words
        if top_y - 1 < (w[1] + w[3]) / 2 < bot_y + 1 and left - 2 <= (w[0] + w[2]) / 2 <= right + 2
    ]
    if not row_words:
        return []
    cells = _bucket_by_column(row_words, edges)
    return [cells] if sum(1 for c in cells if c) >= 2 else []


def _merged_cluster_rows(cluster: list, words: list[tuple]) -> list[list[str]] | None:
    """Re-extract a vertical cluster of fragments as one table: each fragment's own
    rows (trusting find_tables, which gets the rows it detects right) interleaved
    with the rows it skipped in the gaps between them. Returns None if unusable."""
    info = _column_edges(cluster[0])
    if info is None:
        return None
    edges, left, right = info
    rows: list[list[str]] = []
    for i, frag in enumerate(cluster):
        rows.extend(r for r in _table_rows(frag, words, recover_trailing=False) if any(r))
        if i + 1 < len(cluster):
            rows.extend(_gap_row(frag.bbox[3], cluster[i + 1].bbox[1], edges, left, right, words))
    rows = rows + _recover_trailing_row(cluster[-1], words)
    rows = tables_mod.repair_merged_rows(rows)
    # drop any adjacent identical rows (a fragment row and a gap row can coincide)
    deduped = [r for i, r in enumerate(rows) if i == 0 or r != rows[i - 1]]
    return deduped if len(deduped) >= 2 else None


def _point_in_rects(x: float, y: float, rects: list[tuple[float, float, float, float]]) -> bool:
    return any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in rects)


def _rects_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _has_table_grid(page: pymupdf.Page) -> bool:
    """Whether the page has enough ruled line-work to possibly hold a table.

    ``find_tables`` is the dominant cost of extraction, roughly 35x the text
    pass per page, yet most pages carry no table at all. Its default strategy
    detects only tables bounded by vector lines, so the smallest table it could
    keep needs a 2x2 grid of rules: three distinct horizontal edge positions and
    three vertical. Counting those from the cheap C-level drawing list (about
    2ms) lets prose and lightly ruled pages skip the expensive scan. The test
    only errs toward running ``find_tables``: it counts every axis-aligned
    segment and rectangle edge on the page, never fewer, so it cannot hide a
    grid that detection would have found."""
    ys: set[int] = set()
    xs: set[int] = set()
    for drawing in page.get_cdrawings():
        for item in drawing.get("items", ()):
            kind = item[0]
            if kind == "l":  # line: (x0, y0) -> (x1, y1)
                (x0, y0), (x1, y1) = item[1], item[2]
                if abs(y0 - y1) <= 1:
                    ys.add(round(y0))
                elif abs(x0 - x1) <= 1:
                    xs.add(round(x0))
            elif kind == "re":  # rectangle: every edge is a candidate rule
                x0, y0, x1, y1 = item[1]
                ys.add(round(y0))
                ys.add(round(y1))
                xs.add(round(x0))
                xs.add(round(x1))
            elif kind == "qu":  # quad: take its corner coordinates
                for point in item[1]:
                    xs.add(round(point[0]))
                    ys.add(round(point[1]))
        if len(ys) >= 3 and len(xs) >= 3:
            return True
    return False


_RULE_CHARS = set("-_–—=•·~")


def _is_decorative_rule(text: str) -> bool:
    """A line of only dash/underscore/rule glyphs, used as a divider (e.g. in
    stat-block grey boxes). Carries no content; drop it."""
    stripped = text.strip()
    return len(stripped) >= 3 and all(char in _RULE_CHARS for char in stripped)


# Quality gates for warning that a PDF won't ingest well.
_MIN_CHARS_PER_PAGE = 100  # below this the text layer is effectively absent
# A page with an image but fewer than this many extracted chars is image-only
# (a scanned sheet/handout/map); its text, if any, didn't survive extraction.
_IMAGE_PAGE_MAX_TEXT_CHARS = 50
_MIN_DOMINANT_SIZE = 0.5  # body font should carry at least half the text
_MIN_LETTER_RATIO = 0.7  # share of non-space chars that must be letters

# Per-page extraction is CPU-bound and embarrassingly parallel, so a long PDF is
# split across processes. Below this page count the process-spawn cost (each
# worker re-imports pymupdf) outweighs the win, so we stay in-process. Workers
# are capped at the core count and at one per ``_PAGES_PER_WORKER`` pages so a
# medium PDF doesn't spawn more processes than it can keep busy.
_PARALLEL_MIN_PAGES = 16
_MAX_WORKERS = 8
_PAGES_PER_WORKER = 8


def assess_quality(content: PdfContent) -> str | None:
    """Warn when a PDF's text layer is too poor to ingest well; return a
    one-line reason, or ``None`` when it looks healthy.

    Three failure modes show up in practice. A scanned image with no real text
    layer yields almost no characters. Low-quality OCR yields plenty of text
    but renders it at dozens of fractional sizes with no dominant body font, so
    heading and section detection then produce noise. Healthy digital PDFs put
    well over half their text at a single body size (measured: 81 to 98% across
    the sample set; a bad OCR scan sat at 20%). Sizes are bucketed to whole
    points before that share is measured: a clean PDF often renders its one body
    font at a spread of near-identical sizes (sub-point rendering jitter like
    8.4/8.5, plus stat-block text set a hair smaller than the prose at 8.0),
    which should count as one font, whereas a real OCR smear stays spread across
    many *whole-point* buckets and still falls short.

    The third mode passes both volume and dominance gates yet is still
    unusable: a subset Type0 font with ``Identity-H`` encoding whose ToUnicode
    (and ``cmap``) tables were stripped extracts as a per-font substitution
    cipher: plenty of characters at one dominant size, but each "word" is
    studded with digits and punctuation ("The players" → ":6)!13$4),#"). Real
    prose is overwhelmingly letters among its non-space characters (measured:
    89 to 96% across the sample set; the broken PDF sat at 46%), so a low letter
    share is the tell."""
    body = [line for line in content.lines if not line.is_table]
    total_chars = sum(len(line.text) for line in body)
    pages = max(1, content.page_count)
    if total_chars < _MIN_CHARS_PER_PAGE * pages:
        return (
            f"almost no extractable text ({total_chars // pages} chars/page) - this looks "
            "like a scanned image with no text layer; OCR it before ingesting"
        )
    sizes: Counter[int] = Counter()
    for line in body:
        sizes[round(line.size)] += len(line.text)
    coverage = sizes.most_common(1)[0][1] / total_chars
    if coverage < _MIN_DOMINANT_SIZE:
        n_sizes = sum(1 for chars in sizes.values() if chars >= 0.01 * total_chars)
        return (
            f"no dominant body font (text spread across {n_sizes} sizes, only "
            f"{coverage:.0%} at the most common) - this PDF looks like a scan/OCR and "
            "ingestion will likely be unreliable"
        )
    non_space = sum(1 for line in body for char in line.text if not char.isspace())
    letters = sum(1 for line in body for char in line.text if char.isalpha())
    if non_space and letters / non_space < _MIN_LETTER_RATIO:
        return (
            f"text is mostly non-letters ({letters / non_space:.0%} letters) - the PDF's "
            "fonts encode text with no recoverable Unicode mapping, so extraction yields "
            "gibberish; OCR it before ingesting"
        )
    return None


@dataclass
class RawLine:
    """One line of a page as pymupdf reports it, before merging/furniture
    removal. Used by the ``openadventure inspect`` diagnostics to see raw geometry,
    font, and weight: the signals heading/column detection keys on."""

    page: int  # 1-based
    y: float
    x0: float
    x1: float
    size: float
    bold: bool
    font: str
    text: str


def raw_page_lines(path: Path, page_number: int) -> tuple[list[RawLine], float]:
    """Raw per-line geometry for one page (1-based). Returns ``(lines, width)``.
    The only place outside :func:`extract_pdf` that reads a page's text dict."""
    doc = pymupdf.open(path)
    try:
        page = doc[page_number - 1]
        width = page.rect.width
        out: list[RawLine] = []
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
                text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                if not text:
                    continue
                x0, y0, x1, _ = line.get("bbox", (0, 0, 0, 0))
                sizes = [round(s.get("size", 0), 1) for s in spans]
                fonts = Counter(s.get("font", "") for s in spans)
                out.append(
                    RawLine(
                        page=page_number,
                        y=y0,
                        x0=x0,
                        x1=x1,
                        size=max(sizes) if sizes else 0.0,
                        bold=any(s.get("flags", 0) & BOLD_FLAG for s in spans),
                        font=fonts.most_common(1)[0][0] if fonts else "",
                        text=text,
                    )
                )
        return out, width
    finally:
        doc.close()


@dataclass
class TableDiag:
    """One page's table-detection diagnostics for ``openadventure inspect``: the
    raw find_tables fragments (with jam counts), how they cluster for merging, any
    borderless detections, and the final rendered markdown tables."""

    page: int  # 1-based
    has_grid: bool
    fragments: list[tuple[int, int, tuple, str, int]]  # rows, cols, bbox, first cell, jammed rows
    clusters: list[int]  # fragment count per merge cluster
    borderless: list[tuple[int, int, tuple]]  # rows, cols, bbox
    emitted: list[str]  # final rendered markdown tables


def table_diagnostics(
    path: Path, page_number: int | None = None, *, with_emitted: bool = True
) -> list[TableDiag]:
    """Table-detection diagnostics for one page (``page_number``) or the whole
    document. ``with_emitted=False`` skips rendering the final tables (one
    find_tables pass per page) for a cheap document-wide census."""
    doc = pymupdf.open(path)
    try:
        indices = [page_number - 1] if page_number else range(doc.page_count)
        out: list[TableDiag] = []
        for i in indices:
            page = doc[i]
            words = [tuple(w[:5]) for w in page.get_text("words")]
            has_grid = _has_table_grid(page)
            found = list(page.find_tables()) if has_grid else []
            body_h = median([w[3] - w[1] for w in words]) if words else 9.0
            fragments = []
            for t in found:
                rows = [
                    [("" if c is None else str(c)).replace("\n", " ").strip() for c in r]
                    for r in t.extract()
                ]
                rows = [r for r in rows if any(r)]
                first = rows[0][0][:28] if rows and rows[0] else ""
                bbox = tuple(round(v) for v in t.bbox)
                fragments.append((t.row_count, t.col_count, bbox, first, _jammed_rows(rows)))
            clusters = [len(c) for c in _cluster_tables(found, words, body_h)]
            borderless = [
                (len(b.rows), len(b.rows[0]), (round(b.x0), round(b.y0), round(b.x1), round(b.y1)))
                for b in tables_mod.find_borderless_tables(words)
            ]
            emitted = [ln.text for ln in _page_tables(page, i, words)[0]] if with_emitted else []
            out.append(TableDiag(i + 1, has_grid, fragments, clusters, borderless, emitted))
        return out
    finally:
        doc.close()


@dataclass
class _PageExtract:
    """One page's contribution, sized to ship cheaply between processes."""

    lines: list[Line]  # reading order, including any table markdown
    size_weight: Counter[float]  # chars per font size, for the body-size vote
    image_only_page: int | None  # 1-based page number if image-only, else None


def _page_tables(
    page: pymupdf.Page, page_index: int, words: list[tuple]
) -> tuple[list[Line], list[tuple[float, float, float, float]]]:
    """Detect and render every table on a page. Ruled-grid tables come from
    find_tables (with cluster-merging of zebra fragments and skipped-row
    recovery); borderless tables are recovered from word geometry. Returns the
    table ``Line`` objects (markdown, ``is_table=True``) and the page rects to
    exclude from the prose pass. Shared by extraction and ``inspect`` so the
    diagnostic shows exactly what ingestion produces."""
    table_rects: list[tuple[float, float, float, float]] = []
    tables: list[Line] = []

    def add(text: str, x0: float, y0: float, x1: float, y1: float) -> None:
        if text.count("\n") >= 2:  # at least header + separator + a row
            table_rects.append((x0 - 2, y0 - 2, x1 + 2, y1 + 2))
            tables.append(
                Line(
                    text=text,
                    size=0,
                    bold=False,
                    page=page_index + 1,
                    y=y0,
                    x0=x0,
                    x1=x1,
                    is_table=True,
                )
            )

    try:
        found = list(page.find_tables()) if _has_table_grid(page) else []
        body_h = median([w[3] - w[1] for w in words]) if words else 9.0
        # find_tables sometimes splits one zebra table into several stacked
        # fragments (detecting only the alternating rows); merge those back and
        # recover the rows it skipped. Single-table clusters take the normal path.
        for cluster in _cluster_tables(found, words, body_h):
            merged = _merged_cluster_rows(cluster, words) if len(cluster) > 1 else None
            if merged is not None:
                add(
                    _render_table(merged),
                    min(t.bbox[0] for t in cluster),
                    cluster[0].bbox[1],
                    max(t.bbox[2] for t in cluster),
                    cluster[-1].bbox[3],
                )
            else:  # single table, or merge declined -> per-fragment
                for frag in cluster:
                    add(_table_markdown(frag, words), *frag.bbox)
    except Exception:
        pass  # table detection is best-effort

    # Borderless tables (no ruled grid) are invisible to find_tables, common in
    # rulebooks that set tables with whitespace alignment and zebra shading (e.g.
    # the D&D 4e PHB). Recover them from positioned words and add any that don't
    # overlap a grid table already found above.
    try:
        for bt in tables_mod.find_borderless_tables(words):
            rect = (bt.x0 - 2, bt.y0 - 2, bt.x1 + 2, bt.y1 + 2)
            if not any(_rects_overlap(rect, existing) for existing in table_rects):
                add(bt.markdown(), bt.x0, bt.y0, bt.x1, bt.y1)
    except Exception:
        pass  # best-effort, never block extraction on table recovery

    return tables, table_rects


def _extract_page(doc: pymupdf.Document, page_index: int) -> _PageExtract:
    """Extract a single page. Pure function of the page, so it runs identically
    in-process or in a worker."""
    page = doc[page_index]
    # Positioned words, shared by grid-table re-extraction and borderless detection.
    words = [tuple(word[:5]) for word in page.get_text("words")]
    tables, table_rects = _page_tables(page, page_index, words)

    page_lines: list[Line] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(span.get("text", "") for span in spans)
            # Keep inner spacing: double-drawn display fonts split a line into
            # fragments and the inter-word spaces live at fragment edges, where
            # the baseline merge needs them. Outer whitespace is stripped there.
            text = text.replace("\n", " ").replace("�", "'")  # custom-encoded quotes
            if not text.strip() or _is_decorative_rule(text):
                continue
            x0, y0, x1, _ = line.get("bbox", (0, 0, 0, 0))
            if _point_in_rects(x0 + 1, y0 + 1, table_rects):
                continue  # rendered via the table markdown instead
            sizes = [
                round(span.get("size", 0), 1) for span in spans if span.get("text", "").strip()
            ]
            size = max(sizes) if sizes else 0.0
            bold = any(
                span.get("flags", 0) & BOLD_FLAG for span in spans if span.get("text", "").strip()
            )
            page_lines.append(
                Line(text=text, size=size, bold=bold, page=page_index + 1, y=y0, x0=x0, x1=x1)
            )

    geom = _column_geometry(page_lines, page.rect.width)
    page_lines = _merge_baseline_fragments(page_lines, geom)
    # A page that carries an image but yielded almost no text (and no table) is
    # image-only: a scanned sheet/handout/map whose content extraction can't
    # reach. Record it so ingest can warn rather than drop it silently.
    page_chars = sum(len(ln.text) for ln in page_lines)
    image_only = (
        page_index + 1
        if page_chars < _IMAGE_PAGE_MAX_TEXT_CHARS and not tables and page.get_images()
        else None
    )
    size_weight: Counter[float] = Counter()
    for ln in page_lines:
        size_weight[ln.size] += len(ln.text)
    return _PageExtract(
        lines=_reading_order(page_lines + tables, geom),
        size_weight=size_weight,
        image_only_page=image_only,
    )


# Each worker opens the PDF once (in its initializer) and keeps it here for the
# life of the process, so the many small batches that feed a smooth progress bar
# don't each pay a reopen.
_WORKER_DOC: pymupdf.Document | None = None


def _worker_open(path: Path) -> None:
    """Process-pool initializer: open the PDF once per worker."""
    global _WORKER_DOC
    _WORKER_DOC = pymupdf.open(path)


def _extract_batch(page_indices: list[int]) -> list[_PageExtract]:
    """Worker task: extract a small batch of pages from this worker's open PDF.
    Batches are kept small so the parent reports progress frequently, and the
    pool hands them out as workers free up, balancing uneven per-page cost."""
    if _WORKER_DOC is None:  # pragma: no cover - initializer always runs first
        raise RuntimeError("worker PDF not opened")
    return [_extract_page(_WORKER_DOC, i) for i in page_indices]


def _resolve_workers(requested: int | None, n_pages: int) -> int:
    """How many worker processes to use. An explicit request wins (bounded by the
    page count); otherwise auto-scale, staying in-process for short PDFs."""
    if requested is not None:
        return max(1, min(requested, n_pages))
    if n_pages < _PARALLEL_MIN_PAGES:
        return 1
    return min(_MAX_WORKERS, os.cpu_count() or 1, max(1, n_pages // _PAGES_PER_WORKER))


def _page_batches(items: list[int], n_workers: int) -> list[list[int]]:
    """Split into many small contiguous batches (about ``8 * n_workers`` of them)
    so the pool can balance load and the parent can tick progress per batch
    rather than once per giant chunk. Batches stay in page order for the merge."""
    target = max(1, n_workers * 8)
    size = max(1, -(-len(items) // target))  # ceil division
    return [items[i : i + size] for i in range(0, len(items), size)]


def extract_pdf(
    path: Path,
    *,
    pages: tuple[int, int] | None = None,
    progress: ProgressFn | None = None,
    workers: int | None = None,
) -> PdfContent:
    """Extract a PDF. ``pages`` is an optional 1-based inclusive page range, for
    ingesting one part of a combined book (e.g. the adventure inside a rulebook)
    on its own. Line page numbers stay absolute so footer detection still works.

    Long PDFs are extracted across worker processes; ``workers`` overrides the
    auto-scaled count (1 forces in-process). ``progress`` is reported as pages
    complete (the dominant cost on a large PDF) so the caller can render a bar."""
    doc = pymupdf.open(path)
    content = PdfContent()
    content.toc = [(level, title.strip(), page) for level, title, page in (doc.get_toc() or [])]
    if pages:
        page_range = range(max(0, pages[0] - 1), min(doc.page_count, pages[1]))
    else:
        page_range = range(doc.page_count)
    doc.close()

    page_list = list(page_range)
    content.page_count = len(page_list)
    report(progress, PHASE_EXTRACT, 0, content.page_count)

    n_workers = _resolve_workers(workers, len(page_list))
    extracts: list[_PageExtract] = []
    if n_workers <= 1:
        doc = pymupdf.open(path)
        try:
            for done, page_index in enumerate(page_list, start=1):
                extracts.append(_extract_page(doc, page_index))
                report(progress, PHASE_EXTRACT, done, content.page_count)
        finally:
            doc.close()
    else:
        batches = _page_batches(page_list, n_workers)
        by_batch: list[list[_PageExtract] | None] = [None] * len(batches)
        done = 0
        with ProcessPoolExecutor(
            max_workers=n_workers, initializer=_worker_open, initargs=(path,)
        ) as pool:
            futures = {pool.submit(_extract_batch, b): i for i, b in enumerate(batches)}
            for future in as_completed(futures):
                i = futures[future]
                by_batch[i] = future.result()
                done += len(batches[i])  # batches finish out of order; the count still climbs
                report(progress, PHASE_EXTRACT, done, content.page_count)
        extracts = [pe for batch in by_batch if batch for pe in batch]

    raw_lines: list[Line] = []
    size_weight: Counter[float] = Counter()
    for pe in extracts:  # page order: batches are contiguous and reassembled in order
        raw_lines.extend(pe.lines)
        size_weight.update(pe.size_weight)
        if pe.image_only_page is not None:
            content.image_only_pages.append(pe.image_only_page)

    if size_weight:
        content.body_size = size_weight.most_common(1)[0][0]
    content.lines = _drop_repeating_furniture(raw_lines, len(page_list))
    return content


# Column geometry is ``(splits, margins)``: ``margins`` are the columns' left
# edges; ``splits`` are the x-coordinates that separate them (one fewer). A
# line belongs to column ``bisect_right(splits, x0)``.
ColumnGeometry = tuple[list[float], list[float]]


def _line_starts(lines: list[Line]) -> list[Line]:
    """The fragments that *begin* a visual line, those with no same-baseline
    fragment ending just to their left.

    A column margin is a peak in where lines start, but justified and
    double-drawn text emits each word (or glyph) as its own positioned fragment,
    and only the leftmost begins the line. Counting every fragment lets a densely
    fragmented page (a stat-block appendix runs ~3x the fragments of a prose
    page) inflate the population denominator while the true margins gain none of
    that bulk, pushing a real column's start count below the percentage
    threshold and collapsing the page to one column. A continuation fragment has
    text immediately to its left (a sub-word gap); a line start has only the
    gutter or page margin, so the nearest left neighbour is far away or absent."""
    return [
        ln
        for ln in lines
        if not any(
            g is not ln and abs(g.y - ln.y) <= 2.0 and g.x0 < ln.x0 and g.x1 > ln.x0 - 8.0
            for g in lines
        )
    ]


def _column_geometry(lines: list[Line], page_width: float) -> ColumnGeometry | None:
    """Detect a multi-column layout from where lines start, or ``None`` for a
    single column.

    Derived from the data, not a fixed fraction of the page, so it works when
    columns are shifted off-centre (some modules push columns left of the page
    midpoint) and for two- or three-column pages alike. A column's left margin
    is a *peak*: many lines start at exactly that x. Sparse indents don't clear
    the population threshold, so they can't be mistaken for a column edge. Each
    split is the midpoint between a column's rightmost start and the next
    column's margin, so even a word-fragmented column (each word positioned
    separately) stays wholly within its column."""
    body = [ln for ln in lines if not ln.is_table and ln.text]
    spans = [(ln.x0, ln.x1) for ln in body]
    if len(spans) < 8:
        return None
    # Peaks are measured over line *starts*, not every fragment, so heavy
    # word-fragmentation can't dilute a real margin below the threshold.
    starts = _line_starts(body)
    counts: Counter[int] = Counter(round(ln.x0 / 4) * 4 for ln in starts)
    threshold = max(6, len(starts) * 0.12)
    peaks = sorted(x for x, n in counts.items() if n >= threshold)
    # A real text column is wide; a narrow peak is a right-aligned label or
    # indent (e.g. GURPS point costs) that must not be mistaken for a column.
    min_column_width = page_width * 0.16
    margins: list[float] = []
    for x in peaks:
        if x <= page_width * 0.92 and (not margins or x >= margins[-1] + min_column_width):
            margins.append(float(x))
    if len(margins) < 2:
        return None
    splits: list[float] = []
    for left, right in zip(margins, margins[1:], strict=False):
        # A full-width header or banner that crosses the gutter (a running title,
        # a spanning heading) would otherwise bridge the empty band and collapse
        # the split onto the left margin, folding both columns into one and
        # merging their lines across the gutter. Drop spans that straddle the
        # right column's margin (start well left of it and end well right of it)
        # before measuring; real column body lines stay on their own side.
        pad = max(12.0, (right - left) * 0.08)
        body = [(x0, x1) for x0, x1 in spans if not (x0 < right - pad and x1 > right + pad)]
        splits.append(_gutter_split(body, left, right))
    return (splits, margins)


def _gutter_split(spans: list[tuple[float, float]], left: float, right: float) -> float:
    """Place a column split inside the gutter: the widest x-band between two
    column margins that no line's text crosses. Robust to headers that outdent
    left of their column's body margin (e.g. spell names) and to word-fragmented
    columns, which an x0-based boundary mis-assigns."""
    covered = sorted(
        (max(x0, left), min(x1, right)) for x0, x1 in spans if x0 < right and x1 > left
    )
    best_gap, best_x = 0.0, (left + right) / 2
    cursor = left
    for start, end in covered:
        if start - cursor > best_gap:
            best_gap, best_x = start - cursor, (cursor + start) / 2
        cursor = max(cursor, end)
    if right - cursor > best_gap:
        best_x = (cursor + right) / 2
    return best_x


def _column_of(x0: float, splits: list[float]) -> int:
    return bisect.bisect_right(splits, x0)


def _suffix_prefix_overlap(a: str, b: str, max_k: int = 4) -> int:
    """Length of the longest string that is both a suffix of ``a`` and a prefix
    of ``b`` (capped). Used to splice fragments whose shared boundary glyph a
    double-drawn font redrew in each."""
    for k in range(min(len(a), len(b), max_k), 0, -1):
        if a[-k:] == b[:k]:
            return k
    return 0


def _join_fragments(group: list[Line]) -> str:
    """Concatenate same-baseline fragments left-to-right into one line.

    Two layouts produce multi-fragment baselines. Justified body text emits each
    *word* as its own fragment, separated by a clear gap (or a trailing space).
    Double-drawn display faces (small-caps title/heading fonts) emit *overlapping*
    fragments that redraw the shared boundary glyph in each: "Defia"+"ance" for
    "Defiance", with the first letter sometimes drawn a second time on top. Gap
    geometry tells them apart: a deep overlap means a duplicated boundary glyph to
    splice off; a non-overlap is a word boundary whose space is taken from the
    fragment text, or added when a real gap left none."""
    acc = group[0].text
    acc_x1 = group[0].x1
    size = group[0].size or 0.0
    for cur in group[1:]:
        text = cur.text
        if not text:
            continue
        if cur.x1 <= acc_x1 + 0.5:
            continue  # a duplicate glyph drawn over territory already covered
        gap = cur.x0 - acc_x1
        if size and gap < -0.12 * size:
            acc += text[_suffix_prefix_overlap(acc, text) :]  # drop the redrawn glyph
        else:
            if gap > 0.25 * size and not acc.endswith(" ") and not text.startswith(" "):
                acc += " "
            acc += text
        acc_x1 = max(acc_x1, cur.x1)
    return acc


def _collapse_letter_spacing(text: str) -> str:
    """Collapse tracked-out display type (each glyph spaced apart, as cover
    titles often are: "W a t c h", "T h e  F a l l  o f") back into words. A
    single space separates letters; a wider gap (two or more) separates words.
    Only fires when the line is genuinely letter-spaced (mostly single chars),
    so ordinary prose with the odd "a"/"I" is left alone."""
    tokens = text.split()
    if len(tokens) < 4 or sum(len(t) == 1 for t in tokens) / len(tokens) < 0.7:
        return text
    words = ["".join(part.split()) for part in re.split(r"\s{2,}", text)]
    return " ".join(w for w in words if w)


def _baseline_pieces(group: list[Line], splits: list[float]) -> list[list[Line]]:
    """Split one baseline's fragments into per-column pieces, cutting only at a
    gutter the line's text leaves empty.

    A full-width banner (a chapter title, the "Appendix Mission N" header over a
    two-column stat page) shares a baseline with both columns but its glyphs run
    continuously across the gutter, so it must stay one line; two aligned column
    rows leave a fragment-free band at the split and must be cut apart there.
    ``group`` is sorted by ``x0``. A split is *bridged* when a fragment covers it
    or consecutive fragments straddle it within a hairline gap; bridged splits
    don't cut; only the empty ones do."""
    if not splits:
        return [group]
    cuts: list[float] = []
    for s in splits:
        covered = any(ln.x0 <= s <= ln.x1 for ln in group)
        left = max((ln.x1 for ln in group if ln.x0 <= s), default=None)
        right = min((ln.x0 for ln in group if ln.x0 > s), default=None)
        bridged = covered or (left is not None and right is not None and right - left < 12.0)
        if not bridged:
            cuts.append(s)
    if not cuts:
        return [group]
    pieces: dict[int, list[Line]] = {}
    for ln in group:
        pieces.setdefault(bisect.bisect_right(cuts, ln.x0), []).append(ln)
    return [pieces[k] for k in sorted(pieces)]


def _merge_baseline_fragments(lines: list[Line], geom: ColumnGeometry | None) -> list[Line]:
    """Merge text fragments that share a baseline and font size into a single
    line.

    Some PDFs (notably justified body text and double-drawn display fonts) emit
    each word (or each glyph) as its own positioned text line. Left unmerged,
    common words ("the", "and", stat-block labels) repeat enough to be mistaken
    for running furniture and get dropped, bodies render one word per line, and
    display titles read as garbled, doubled characters. Fragments only merge when
    their font size matches (so a margin display number or a stat-block name on
    the same baseline as adjacent body text is not glued to it). A baseline is
    formed across the whole page width, then cut into per-column pieces at any
    gutter its text leaves empty, so a column's lines never merge across the
    gutter, yet a full-width banner that spans the gutter stays one line. PDFs
    that already emit whole lines have one fragment per baseline, so
    reconstruction is a no-op."""
    if not lines:
        return []
    splits = geom[0] if geom else []
    rows = sorted(lines, key=lambda ln: ln.y)
    baselines: list[list[Line]] = []
    for ln in rows:
        prev = baselines[-1] if baselines else None
        if prev is not None and abs(ln.y - prev[0].y) <= 2.0 and ln.size == prev[0].size:
            prev.append(ln)
        else:
            baselines.append([ln])
    merged: list[Line] = []
    for baseline in baselines:
        baseline.sort(key=lambda ln: ln.x0)
        for group in _baseline_pieces(baseline, splits):
            text = _collapse_letter_spacing(_join_fragments(group).strip())
            if not text:
                continue
            merged.append(
                Line(
                    text=text,
                    size=group[0].size,
                    bold=any(ln.bold for ln in group),
                    page=group[0].page,
                    y=min(ln.y for ln in group),
                    x0=min(ln.x0 for ln in group),
                    x1=max(ln.x1 for ln in group),
                )
            )
    return merged


def _reading_order(lines: list[Line], geom: ColumnGeometry | None) -> list[Line]:
    """Order one page's lines: full-width spans first band-by-band, then each
    column left-to-right, top-to-bottom.

    Pages are split into vertical bands at each full-width element (chapter
    headings, wide tables); within a band: column 0, column 1, ... in order.
    Single-column pages (``geom is None``) just sort top-to-bottom."""
    if not lines:
        return []
    if geom is None:
        return sorted(lines, key=lambda ln: ln.y)
    splits, margins = geom

    def is_spanning(line: Line) -> bool:
        # starts in one column but its text runs into a later column's region
        column = _column_of(line.x0, splits)
        return column < len(margins) - 1 and line.x1 > margins[column + 1] + 5

    lines = sorted(lines, key=lambda ln: ln.y)
    bands: list[list[Line]] = [[]]
    for line in lines:
        if is_spanning(line):
            bands.append([line])
            bands.append([])
        else:
            bands[-1].append(line)

    ordered: list[Line] = []
    for band in bands:
        ordered.extend(ln for ln in band if is_spanning(ln))
        for column_index in range(len(splits) + 1):
            in_column = [
                ln
                for ln in band
                if not is_spanning(ln) and _column_of(ln.x0, splits) == column_index
            ]
            ordered.extend(sorted(in_column, key=lambda ln: ln.y))
    return ordered


def _drop_repeating_furniture(lines: list[Line], page_count: int) -> list[Line]:
    """Remove running headers/footers (same text repeating across many pages)
    and bare page numbers."""
    if page_count < 4:
        return lines
    occurrences: Counter[str] = Counter()
    for line in lines:
        if not line.is_table and len(line.text) < 80:
            occurrences[line.text.casefold()] += 1
    threshold = max(3, int(page_count * 0.3))
    furniture = {text for text, n in occurrences.items() if n >= threshold}

    kept = []
    for line in lines:
        if line.is_table:
            kept.append(line)
            continue
        if line.text.casefold() in furniture:
            continue
        if _is_page_number(line):  # bare, decorated, or spaced folio
            continue
        kept.append(line)
    return kept
