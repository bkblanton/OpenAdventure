"""Borderless-table reconstruction from positioned words.

pymupdf's ``find_tables`` only recovers tables drawn with ruled grid lines. Many
rulebooks (notably the D&D 4e PHB) set their reference tables with no borders at
all; column structure is carried by whitespace alignment and, at most, zebra
row shading. Those fall through to the prose path, where same-baseline fragments
merge across the gutters and the grid is lost.

This module recovers them from the page's positioned words. The signal is purely
geometric and local: a table is a run of consecutive text rows that *agree on
several column-start x positions*. Prose, even multi-column prose, only ever
agrees on the one or two page-column margins, so the "≥3 shared column starts"
test separates a real table from running text without reading any of the words.

Pure functions over ``(x0, y0, x1, y1, text)`` word tuples, so the detector is
unit-testable with synthetic input and never imports pymupdf.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A positioned word as pymupdf's ``page.get_text("words")`` reports it (we only
# use the first five fields).
Word = tuple[float, float, float, float, str]

_Y_TOL = 3.0  # words within this many points of y share a baseline (one row)
_MIN_ROWS = 3
_MIN_COLS = 3
_COL_TOL = 6.0  # column starts within this many points are the same column
_MIN_CELL_GAP = 7.0  # a gap wider than this between words opens a new cell
_MIN_CONSENSUS = 0.5  # a column must begin a cell in this share of the band's rows
_MIN_LANE = 18.0  # an empty vertical band this wide separates a table from prose
_LANE_OCCUPANCY = 0.12  # a lane is "empty" if at most this share of rows cross it
_MERGE_Y_GAP = 24.0  # contiguous same-structure tables this close are one table


_DASH_CELLS = {"—", "–", "-", "*", "✦", "†"}


def _is_numeric_cell(text: str) -> bool:
    """A table-data cell: a dash placeholder, or a token built from digits with at
    most a two-letter unit/ordinal suffix ("2nd", "250 gp", "2/4/3/5", "18–19",
    "+5"). Prose cells ("Dex 15", "ability check 26") carry more letters."""
    cell = text.strip()
    if cell in _DASH_CELLS:
        return True
    if not any(ch.isdigit() for ch in cell):
        return False
    return sum(ch.isalpha() for ch in cell) <= 2


# Scars of a bad column split, both absent (~0) from clean game tables:
#   jam:   a cell that is a number then a Capitalised *word*, e.g. "18 Thundering",
#          "625,000 Any": two side-by-side tables merged so Lvl+Name share a cell.
#          The trailing capital must be followed by a lowercase letter, so a
#          value+unit cell stays clean whether the unit is lowercase ("1 gp",
#          "3 lb.") or an all-caps currency code ("4 CP", "50 GP").
#   soup:  a long, digit-dense cell: a stat block's mini-table fused with its
#          neighbour and surrounding prose into "1,000 gp Lvl 20 +4 125,000 gp...".
_JAMMED_CELL = re.compile(r"^\d[\d,.]*\s+[A-Z][a-z]")


def _digit_density(text: str) -> float:
    dense = [ch for ch in text if not ch.isspace()]
    return sum(ch.isdigit() for ch in dense) / len(dense) if dense else 0.0


def _looks_corrupted(rows: list[list[str]], frac: float = 0.2) -> bool:
    """Whether a detected table is too mangled to keep. When it is, the caller
    drops it and the region falls back to flowing prose, which is more faithful
    than a corrupt grid. Tuned on the 4e PHB: clean tables score ~0, the two-up
    magic-item and stat-block tables score well above the threshold."""
    cells = [c for row in rows for c in row if c]
    if not cells:
        return True
    jam = sum(bool(_JAMMED_CELL.match(c)) for c in cells)
    soup = sum(1 for c in cells if len(c) > 30 and _digit_density(c) > 0.15)
    return jam > frac * len(cells) or soup > frac * len(cells)


def _has_numeric_column(grid: list[list[str]], frac: float = 0.6) -> bool:
    """Whether some column is mostly numeric data: the tell of a real game table
    (level, cost, modifier, dice, page) versus an index or address block that just
    happens to be column-aligned."""
    width = max(len(r) for r in grid)
    for col in range(width):
        cells = [r[col] for r in grid if col < len(r) and r[col]]
        if len(cells) >= _MIN_ROWS and sum(_is_numeric_cell(c) for c in cells) >= frac * len(cells):
            return True
    return False


@dataclass
class Table:
    x0: float
    y0: float
    x1: float
    y1: float
    rows: list[list[str]]  # row-major cell text, ragged rows padded by the caller

    def markdown(self) -> str:
        width = max(len(r) for r in self.rows)
        grid = [r + [""] * (width - len(r)) for r in self.rows]
        out = ["| " + " | ".join(grid[0]) + " |", "|" + "---|" * width]
        for row in grid[1:]:
            out.append("| " + " | ".join(row) + " |")
        return "\n".join(out)


def _group_rows(words: list[Word]) -> list[list[Word]]:
    """Cluster words into rows by baseline, each row sorted left to right."""
    rows: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (w[1], w[0])):
        if rows and abs(word[1] - rows[-1][0][1]) <= _Y_TOL:
            rows[-1].append(word)
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w[0])
    return rows


def _cell_starts(row: list[Word]) -> list[float]:
    """The x0 of each word that opens a cell: the first word, plus any word whose
    gap from the previous word's right edge exceeds the cell-gap threshold. Words
    inside one cell (including the per-glyph fragments a double-drawn font emits)
    sit close together and stay in the same cell."""
    starts = [row[0][0]]
    prev_x1 = row[0][2]
    for x0, _y0, x1, _y1, _text in row[1:]:
        if x0 - prev_x1 > _MIN_CELL_GAP:
            starts.append(x0)
        prev_x1 = max(prev_x1, x1)
    return starts


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _COL_TOL


def _shared_columns(a: list[float], b: list[float]) -> int:
    """How many column starts two rows agree on (within tolerance)."""
    shared = 0
    for x in a:
        if any(_close(x, y) for y in b):
            shared += 1
    return shared


def _consensus_columns(band: list[list[float]]) -> list[float]:
    """Column-start x positions that begin a cell in at least ``_MIN_CONSENSUS``
    of the band's rows, clustered within tolerance and returned left to right."""
    flat = sorted(x for starts in band for x in starts)
    clusters: list[list[float]] = []
    for x in flat:
        if clusters and x - clusters[-1][-1] <= _COL_TOL:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    need = max(2, len(band) * _MIN_CONSENSUS)
    cols = []
    for cluster in clusters:
        rows_hit = sum(1 for starts in band if any(_close(x, s) for s in starts for x in cluster))
        if rows_hit >= need:
            cols.append(sum(cluster) / len(cluster))
    return cols


def _row_cells(row: list[Word], columns: list[float]) -> list[str]:
    """Bucket a row's words into the consensus columns: each word joins the last
    column whose x position is at or left of its start (within tolerance)."""
    cells = [""] * len(columns)
    for x0, _y0, _x1, _y1, text in row:
        idx = 0
        for i, cx in enumerate(columns):
            if x0 >= cx - _COL_TOL:
                idx = i
        cells[idx] = (cells[idx] + " " + text).strip() if cells[idx] else text
    return cells


def _empty_lanes(band: list[list[Word]], columns: list[float]) -> list[float]:
    """Inter-column x positions that almost no row's text crosses: a page gutter
    between a table and adjacent prose, as opposed to an ordinary narrow cell gap.
    Returned as the lane mid-x, one per wide empty band between consensus columns."""
    lanes: list[float] = []
    for left, right in zip(columns, columns[1:], strict=False):
        if right - left < _MIN_LANE:
            continue
        # the widest x-strip between these two columns that stays empty across rows
        crossing = sum(
            1 for row in band if any(w[0] < right and w[2] > left + _COL_TOL for w in row)
        )
        if crossing <= _LANE_OCCUPANCY * len(band):
            lanes.append((left + right) / 2)
    return lanes


def _is_prose_column(grid: list[list[str]], idx: int) -> bool:
    """Whether a column holds running prose rather than table cells, judged by
    its cells being long and multi-word. Used only on edge columns, where a table
    abuts a page-column of text that shares its rows."""
    cells = [row[idx] for row in grid if idx < len(row) and row[idx]]
    if len(cells) < _MIN_ROWS:
        return False
    prose = sum(1 for c in cells if len(c) > 24 or len(c.split()) >= 4)
    return prose >= 0.5 * len(cells)


def _trim_prose_edges(sub: list[list[Word]], group: list[float]) -> list[float]:
    """Drop leading/trailing prose columns (adjacent page text) while more than
    ``_MIN_COLS`` columns remain. Interior wide columns (e.g. a real "Features"
    column) are never edges, so they are safe."""
    while len(group) > _MIN_COLS:
        # clip words to the current column span, else a dropped prose column's
        # words spill into the new edge column and it reads as prose too
        lo = group[0] - _COL_TOL
        clipped = [[w for w in r if w[0] >= lo] for r in sub]
        grid = [_row_cells(r, group) for r in clipped]
        if _is_prose_column(grid, 0):
            group = group[1:]
        elif _is_prose_column(grid, len(group) - 1):
            group = group[:-1]
        else:
            break
    return group


def _emit_band(rows: list[list[Word]]) -> list[Table]:
    """Turn one row-band into zero or more tables: split off any prose page-column
    at an empty lane, then emit each remaining column group of width >= _MIN_COLS."""
    columns = _consensus_columns([_cell_starts(r) for r in rows])
    if len(columns) < _MIN_COLS:
        return []
    lanes = _empty_lanes(rows, columns)
    # partition consensus columns into groups separated by the empty lanes
    groups: list[list[float]] = [[]]
    for col in columns:
        if any(groups[-1] and lane > groups[-1][-1] and lane < col for lane in lanes):
            groups.append([])
        groups[-1].append(col)

    tables: list[Table] = []
    for group in groups:
        if len(group) < _MIN_COLS:
            continue  # a narrow side group is adjacent prose, not a table
        sub = [[w for w in row if w[0] >= group[0] - _COL_TOL] for row in rows]
        sub = [r for r in sub if r]
        if len(sub) < _MIN_ROWS:
            continue
        group = _trim_prose_edges(sub, group)
        sub = [[w for w in row if w[0] >= group[0] - _COL_TOL] for row in sub]
        sub = [r for r in sub if r]
        if len(group) < _MIN_COLS or len(sub) < _MIN_ROWS:
            continue
        grid = [_row_cells(r, group) for r in sub]
        flat = [w for r in sub for w in r]
        tables.append(
            Table(
                min(w[0] for w in flat),
                min(w[1] for w in flat),
                max(w[2] for w in flat),
                max(w[3] for w in flat),
                grid,
            )
        )
    return tables


def _merge_adjacent(tables: list[Table]) -> list[Table]:
    """Coalesce tables that are really one: same column count, overlapping x-span,
    and stacked within a small y-gap (a wrapped cell briefly broke the band)."""
    merged: list[Table] = []
    for table in sorted(tables, key=lambda t: t.y0):
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and len(prev.rows[0]) == len(table.rows[0])
            and table.y0 - prev.y1 <= _MERGE_Y_GAP
            and table.x0 < prev.x1
            and table.x1 > prev.x0
        ):
            prev.rows.extend(table.rows)
            prev.x0, prev.y1 = min(prev.x0, table.x0), max(prev.y1, table.y1)
            prev.x1 = max(prev.x1, table.x1)
        else:
            merged.append(table)
    return merged


def find_borderless_tables(words: list[Word]) -> list[Table]:
    """Recover borderless tables from a page's positioned words.

    Walks rows top to bottom, growing a band while consecutive rows keep agreeing
    on at least ``_MIN_COLS`` column starts. Each band is split off from adjacent
    prose at empty lanes, emitted as a table when it has ``_MIN_ROWS`` rows and
    ``_MIN_COLS`` columns, and contiguous same-shape tables are merged."""
    rows = [r for r in _group_rows(words) if r]
    if len(rows) < _MIN_ROWS:
        return []
    starts = [_cell_starts(r) for r in rows]

    tables: list[Table] = []
    i = 0
    n = len(rows)
    while i < n:
        j = i + 1
        while j < n and _shared_columns(starts[j - 1], starts[j]) >= _MIN_COLS:
            j += 1
        if j - i >= _MIN_ROWS:
            new = _emit_band(rows[i:j])
            if new:
                tables.extend(new)
                i = j
                continue
        i += 1
    # keep tables with a numeric data column (drops column-aligned non-tables: the
    # book index, the credits/address block, ritual key-value prose) and discard
    # the ones a bad column split corrupted (they fall back to prose instead).
    return [
        t
        for t in _merge_adjacent(tables)
        if _has_numeric_column(t.rows) and not _looks_corrupted(t.rows)
    ]


# --- alternating-merge repair ------------------------------------------------
# pymupdf's find_tables (and, rarely, the detector above) jams alternating rows
# of an otherwise-clean table: one row splits correctly into cells, the next puts
# the whole row in the first cell and leaves the rest empty -- "02 A crystal..."
# beside an empty cell. The good rows still show the real column layout, so the
# jammed rows can be re-split against it.


_DASH_ONLY = {"—", "–", "-", "*", "†"}


def _looks_free_column(cells: list[str]) -> bool:
    """A free-text column (a description / spell list) rather than an atomic value
    column. Judged by length and word count over the cleanly-parsed cells."""
    filled = [c for c in cells if c]
    if not filled:
        return False
    wide = sum(1 for c in filled if len(c.split()) >= 3 or len(c) > 15)
    return wide >= 0.5 * len(filled)


def _is_rigid_key(cells: list[str]) -> bool:
    """A rigid single-token numeric key column: every cleanly-parsed cell is one
    token that carries a digit or is a dash (a d100 roll, a level, a range). This
    is the only column shape we trust to re-split a jammed row, because variable
    multi-word cells (spell names, "C, M" flags) make token allocation ambiguous
    and would mis-split."""
    filled = [c for c in cells if c]
    if not filled:
        return False
    return all(
        len(c.split()) == 1 and (c in _DASH_ONLY or any(ch.isdigit() for ch in c)) for c in filled
    )


def repair_merged_rows(rows: list[list[str]]) -> list[list[str]]:
    """Re-split rows a bad table parse jammed into the first cell (one filled
    cell, the rest empty -- pymupdf's find_tables does this to alternating rows).

    Deliberately narrow: it only fires on the one shape it can split without
    guessing -- a table of rigid single-token numeric key columns plus exactly one
    free-text column (a d100/level table beside a description). Each jammed row's
    leading and trailing key tokens peel off to their columns and the rest becomes
    the free cell. Anything else (variable-width cells, multiple free columns) is
    returned byte-for-byte unchanged, so the repair can never mangle a table it
    doesn't understand."""
    if len(rows) < 3:
        return rows
    ncol = max(len(r) for r in rows)
    if ncol < 2:
        return rows
    norm = [[(c or "").strip() for c in r] + [""] * (ncol - len(r)) for r in rows]

    jammed = [i for i, r in enumerate(norm) if r[0] and sum(bool(c) for c in r) == 1]
    good = [r for r in norm if sum(bool(c) for c in r) >= 2]
    if len(good) < 2 or len(jammed) < max(1, 0.2 * len(norm)):
        return rows

    free = [_looks_free_column([r[c] for r in good]) for c in range(ncol)]
    if sum(free) != 1:
        return rows  # need exactly one free column to absorb the middle
    fcol = free.index(True)
    # every other column must be a rigid single-token numeric key, or we can't
    # trust where to cut the jammed text
    if not all(c == fcol or _is_rigid_key([r[c] for r in good]) for c in range(ncol)):
        return rows

    out = [r[:] for r in norm]
    for i in jammed:
        toks = norm[i][0].split()
        cells = [""] * ncol
        lo, hi = 0, len(toks)
        ok = True
        for c in range(fcol):  # key columns left of the free column: one token each
            if lo >= hi or not any(ch.isdigit() for ch in toks[lo]) and toks[lo] not in _DASH_ONLY:
                ok = False
                break
            cells[c] = toks[lo]
            lo += 1
        for c in range(ncol - 1, fcol, -1):  # key columns right of it: one token from the end
            if (
                hi - 1 <= lo
                or not any(ch.isdigit() for ch in toks[hi - 1])
                and (toks[hi - 1] not in _DASH_ONLY)
            ):
                ok = False
                break
            cells[c] = toks[hi - 1]
            hi -= 1
        if not ok or hi <= lo:
            continue
        cells[fcol] = " ".join(toks[lo:hi])
        out[i] = cells
    return out
