"""Ingestion: markdown/txt section splitting, indexing, search, reindex."""

from pathlib import Path

import pytest

from openadventure.engine.tools import build_registry
from openadventure.ingest import indexer, pipeline
from openadventure.ingest.extract import (
    Line,
    PdfContent,
    _column_geometry,
    _is_decorative_rule,
    _merge_baseline_fragments,
    assess_quality,
)
from openadventure.ingest.sections import (
    _is_heading,
    _is_location_header,
    _merge_wrapped_headings,
    sections_from_markdown,
    sections_from_pdf,
    sections_from_text,
)
from openadventure.store.workspace import ModuleRef
from tests.conftest import collect
from tests.test_agent_loop import text_turn
from tests.test_sheet_tools import make_ctx

FIXTURE_MD = """\
# Rules of the Realm

Welcome, traveler.

## Combat

Roll initiative when a fight breaks out.

### Opportunity Attacks

Leaving a creature's reach provokes an opportunity attack.

## Magic

### Fireball

A 20-foot-radius sphere of flame. 8d6 fire damage, Dexterity save for half.

### Healing Word

Restore 1d4 + spellcasting modifier hit points at range.
"""


def test_markdown_sections():
    sections = sections_from_markdown(FIXTURE_MD)
    titles = [s.title for s in sections]
    assert titles == [
        "Rules of the Realm",
        "Combat",
        "Opportunity Attacks",
        "Magic",
        "Fireball",
        "Healing Word",
    ]
    fireball = next(s for s in sections if s.title == "Fireball")
    assert fireball.breadcrumb == "Rules of the Realm > Magic > Fireball"
    assert "8d6" in fireball.body


def test_text_sections_with_headings():
    text = "INTRODUCTION\n\nSome intro prose here.\n\nTHE DUNGEON\n\nA dark place full of traps.\n"
    sections = sections_from_text(text)
    titles = [s.title for s in sections]
    assert "INTRODUCTION" in titles
    assert "THE DUNGEON" in titles


def test_text_sections_unstructured_chunks():
    text = "word " * 4500
    sections = sections_from_text(text)
    assert len(sections) == 3
    assert sections[0].title == "Part 1"


def test_ingest_markdown_and_search(tmp_path):
    source = tmp_path / "rules.md"
    source.write_text(FIXTURE_MD, encoding="utf-8")
    dest = tmp_path / "source"

    manifest = pipeline.ingest(source, dest)
    assert manifest["section_count"] == 6
    assert pipeline.is_ingested(dest)

    hits = indexer.search(dest / indexer.INDEX_NAME, "fireball", 3)
    assert hits
    assert hits[0].title == "Fireball"
    assert "fireball" in hits[0].path

    # read the file back through the frontmatter parser
    section_file = dest / "sections" / hits[0].path
    title, breadcrumb, body = pipeline.parse_section_file(section_file.read_text(encoding="utf-8"))
    assert title == "Fireball"
    assert "Dexterity save" in body


def test_multiword_search_and_or_fallback(tmp_path):
    source = tmp_path / "rules.md"
    source.write_text(FIXTURE_MD, encoding="utf-8")
    dest = tmp_path / "source"
    pipeline.ingest(source, dest)
    db = dest / indexer.INDEX_NAME

    hits = indexer.search(db, "opportunity attack", 3)
    assert hits and hits[0].title == "Opportunity Attacks"
    # AND fails, OR fallback still finds fireball
    hits = indexer.search(db, "fireball zzznothing", 3)
    assert any(h.title == "Fireball" for h in hits)
    # garbage queries don't crash
    assert indexer.search(db, "!!! ???", 3) == []


def test_reindex_picks_up_hand_edits(tmp_path):
    source = tmp_path / "rules.md"
    source.write_text(FIXTURE_MD, encoding="utf-8")
    dest = tmp_path / "source"
    pipeline.ingest(source, dest)

    fireball = next((dest / "sections").rglob("fireball.md"))
    fireball.write_text(
        fireball.read_text(encoding="utf-8").replace("8d6", "8d6 (errata: now glitterbomb)"),
        encoding="utf-8",
    )
    count = pipeline.reindex(dest)
    assert count == 6
    hits = indexer.search(dest / indexer.INDEX_NAME, "glitterbomb", 3)
    assert hits and hits[0].title == "Fireball"


def test_sections_in_reading_order_survives_reindex(tmp_path):
    """Sections are reported in the source's reading order, not filename order, so a
    sub-section (a handout, a read-aloud box) stays next to the section it belongs to.
    The ordinal is persisted in frontmatter, so a reindex (which globs files
    alphabetically) cannot quietly reset the order to filenames."""
    source = tmp_path / "rules.md"
    source.write_text(FIXTURE_MD, encoding="utf-8")
    dest = tmp_path / "source"
    pipeline.ingest(source, dest)
    db = dest / indexer.INDEX_NAME

    def leaves():
        return [Path(p).stem for p, _ in indexer.sections_in_reading_order(db)]

    expected = [
        "rules-of-the-realm",
        "combat",
        "opportunity-attacks",
        "magic",
        "fireball",
        "healing-word",
    ]
    assert leaves() == expected
    # reading order genuinely differs from the filename sort (alphabetically fireball
    # precedes magic and opportunity-attacks), so this isn't just an alphabetical list
    assert leaves().index("opportunity-attacks") < leaves().index("fireball")

    pipeline.reindex(dest)
    assert leaves() == expected  # the ordinal kept reading order through the reindex


def test_reingest_fully_replaces_old_source(tmp_path):
    # Reingesting the same source must replace it wholesale: a later, smaller
    # document leaves none of the first ingest's sections behind, on disk or in
    # any derived index.
    source = tmp_path / "rules.md"
    source.write_text(FIXTURE_MD, encoding="utf-8")
    dest = tmp_path / "source"

    first = pipeline.ingest(source, dest)
    assert first["section_count"] == 6
    assert indexer.search(dest / indexer.INDEX_NAME, "fireball", 3)

    smaller = "# Rules of the Realm\n\nWelcome, traveler.\n\n## Combat\n\nRoll initiative.\n"
    source.write_text(smaller, encoding="utf-8")
    second = pipeline.ingest(source, dest)

    # Only the new sections survive on disk.
    assert second["section_count"] == 2
    paths = sorted(
        p.relative_to(dest / "sections").as_posix() for p in (dest / "sections").rglob("*.md")
    )
    assert paths == ["rules-of-the-realm.md", "rules-of-the-realm/combat.md"]

    # The dropped sections are gone from the FTS index and the cross-ref graph.
    assert indexer.search(dest / indexer.INDEX_NAME, "fireball", 3) == []
    assert indexer.search(dest / indexer.INDEX_NAME, "glitterbomb", 3) == []
    report = pipeline.index_report(dest)
    assert report["sections"] == 2
    assert report["dangling"] == 0


class _FakeEmbedBackend:
    """Deterministic, dependency-free embedding backend for tests."""

    model_id = "fake-test-model"
    dims = 3

    def embed(self, texts):
        return [[float(len(t) % 7), 1.0, 0.5] for t in texts]

    def embed_query(self, text):
        return [1.0, 1.0, 0.5]


def test_reingest_without_backend_drops_stale_embeddings(tmp_path):
    # A vector index built on a first ingest must not survive a later reingest
    # that has no backend to rebuild it: its windows would still point at the
    # replaced (here, removed) sections. FTS and xref are always rebuilt, so the
    # embeddings index is the one place stale sections could linger.
    from openadventure.ingest import embeddings

    source = tmp_path / "rules.md"
    source.write_text(FIXTURE_MD, encoding="utf-8")
    dest = tmp_path / "source"
    embed_db = dest / embeddings.EMBEDDINGS_NAME

    pipeline.ingest(source, dest, embed_backend=_FakeEmbedBackend())
    assert embed_db.is_file()

    # Reingest a smaller doc with no backend available (e.g. fastembed failed to
    # load this run). The now-unrebuildable vector index must be dropped, not left
    # pointing at the sections it just replaced.
    source.write_text("# Rules of the Realm\n\nWelcome, traveler.\n", encoding="utf-8")
    pipeline.ingest(source, dest, embed_backend=None)
    assert not embed_db.exists()
    assert pipeline.index_report(dest)["windows"] == 0


def test_pages_range_ingests_only_that_slice(tmp_path):
    # a combined book ingested as two pieces: this checks the page-range path
    import pymupdf

    pdf = tmp_path / "book.pdf"
    doc = pymupdf.open()
    for n in range(1, 5):
        page = doc.new_page(width=400, height=600)
        page.insert_text((50, 80), f"Chapter {n}", fontsize=20)
        page.insert_text(
            (50, 120), f"Body text for page {n}, long enough to make a section.", fontsize=10
        )
    doc.save(pdf)
    doc.close()

    sections = pipeline.extract_sections(pdf, pages=(2, 3))
    titles = {s.title for s in sections}
    assert "Chapter 2" in titles and "Chapter 3" in titles
    assert "Chapter 1" not in titles and "Chapter 4" not in titles
    # page numbers stay absolute (so footers/cross-refs line up with the book)
    assert {s.start_page for s in sections} <= {2, 3}

    manifest = pipeline.ingest(pdf, tmp_path / "out", pages=(2, 3))
    assert manifest["pages"] == "2-3"


def test_image_only_pages_are_flagged_not_dropped_silently(tmp_path):
    # a page that carries an image but no text (a scanned sheet/handout/map) is
    # recorded so ingest can warn rather than silently lose it
    import pymupdf

    from openadventure.ingest.extract import extract_pdf

    pdf = tmp_path / "mixed.pdf"
    doc = pymupdf.open()
    p1 = doc.new_page(width=400, height=600)
    p1.insert_text((50, 80), "Chapter One", fontsize=20)
    p1.insert_text(
        (50, 120), "Plenty of real body text, comfortably over the threshold.", fontsize=10
    )
    p2 = doc.new_page(width=400, height=600)  # an image, no text
    p2.insert_image(
        pymupdf.Rect(50, 50, 350, 550),
        pixmap=pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 80)),
    )
    p3 = doc.new_page(width=400, height=600)
    p3.insert_text((50, 80), "Chapter Two", fontsize=20)
    p3.insert_text(
        (50, 120), "More real body text, also well over the threshold here.", fontsize=10
    )
    doc.save(pdf)
    doc.close()

    assert extract_pdf(pdf).image_only_pages == [2]  # only the image page, absolute number

    # it rides into the manifest, and the shared note names the page
    manifest = pipeline.ingest(pdf, tmp_path / "out")
    assert manifest["image_only_pages"] == [2]
    note = pipeline.image_only_pages_note(manifest)
    assert note and "2" in note and "no extractable text" in note

    # an all-text ingest flags nothing
    assert pipeline.image_only_pages_note({"section_count": 3}) is None


def test_unsupported_extension(tmp_path):
    source = tmp_path / "rules.docx"
    source.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported"):
        pipeline.ingest(source, tmp_path / "out")


def _table_lines(pdf_path):
    """Run the real extraction pipeline over a PDF and return its rendered table
    rows (markdown data lines, separators excluded) across all sections."""
    from openadventure.ingest.extract import extract_pdf

    body = "\n".join(s.body for s in sections_from_pdf(extract_pdf(pdf_path, workers=1)))
    return [ln for ln in body.splitlines() if ln.startswith("|") and not ln.startswith("|---")]


def test_borderless_zebra_table_extracts_end_to_end(tmp_path):
    # A real-pipeline guard: a borderless, zebra-shaded table (no grid lines) must
    # be recovered whole -- header and every row, no leading/trailing drop, no dups.
    import pymupdf

    pdf = tmp_path / "zebra.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    page.insert_text((50, 60), "Weapon Damage", fontsize=16)
    rows = [
        ("Weapon", "Dice", "Weight"),
        ("Club", "1d4", "2"),
        ("Mace", "1d6", "4"),
        ("Sword", "1d8", "3"),
        ("Axe", "1d10", "5"),
        ("Maul", "2d6", "10"),
    ]
    y = 90
    for i, (a, b, c) in enumerate(rows):
        if i % 2 == 1:  # alternating shaded rows, but no ruled grid
            page.draw_rect(pymupdf.Rect(48, y - 9, 360, y + 3), fill=(0.88, 0.88, 0.88), color=None)
        page.insert_text((50, y), a, fontsize=10)
        page.insert_text((180, y), b, fontsize=10)
        page.insert_text((280, y), c, fontsize=10)
        y += 16
    doc.save(pdf)
    doc.close()

    table = _table_lines(pdf)
    assert "| Weapon | Dice | Weight |" in table  # header kept
    assert "| Club | 1d4 | 2 |" in table  # first data row, no leading drop
    assert "| Maul | 2d6 | 10 |" in table  # last data row, no trailing drop
    assert len(table) == len(rows)  # exactly header + 5 rows, no missing/duplicated


def test_grid_table_extracts_end_to_end(tmp_path):
    # The find_tables (ruled-grid) path, end to end through real pymupdf.
    import pymupdf

    pdf = tmp_path / "grid.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    page.insert_text((50, 60), "Armor", fontsize=16)
    rows = [
        ("Armor", "AC", "Cost"),
        ("Leather", "11", "10"),
        ("Chain", "16", "75"),
        ("Plate", "18", "1500"),
    ]
    cols_x = [48, 160, 250, 360]
    y0, rh = 80, 20
    for r in range(len(rows) + 1):
        page.draw_line((cols_x[0], y0 + r * rh), (cols_x[-1], y0 + r * rh))
    for cx in cols_x:
        page.draw_line((cx, y0), (cx, y0 + len(rows) * rh))
    for r, (a, b, c) in enumerate(rows):
        ty = y0 + r * rh + 14
        page.insert_text((cols_x[0] + 4, ty), a, fontsize=10)
        page.insert_text((cols_x[1] + 4, ty), b, fontsize=10)
        page.insert_text((cols_x[2] + 4, ty), c, fontsize=10)
    doc.save(pdf)
    doc.close()

    table = _table_lines(pdf)
    assert "| Armor | AC | Cost |" in table
    assert "| Plate | 18 | 1500 |" in table  # last row present
    assert len(table) == len(rows)


def test_inspect_tables_diagnostic(tmp_path):
    # the `inspect --tables` view shows detection internals + the rendered table
    import pymupdf

    from openadventure.ingest import inspect

    pdf = tmp_path / "t.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    rows = [("Armor", "AC", "Cost"), ("Leather", "11", "10"), ("Chain", "16", "75")]
    cols_x = [48, 160, 250, 360]
    y0, rh = 80, 20
    for r in range(len(rows) + 1):
        page.draw_line((cols_x[0], y0 + r * rh), (cols_x[-1], y0 + r * rh))
    for cx in cols_x:
        page.draw_line((cx, y0), (cx, y0 + len(rows) * rh))
    for r, (a, b, c) in enumerate(rows):
        ty = y0 + r * rh + 14
        page.insert_text((cols_x[0] + 4, ty), a, fontsize=10)
        page.insert_text((cols_x[1] + 4, ty), b, fontsize=10)
        page.insert_text((cols_x[2] + 4, ty), c, fontsize=10)
    doc.save(pdf)
    doc.close()

    detail = inspect.tables(pdf, 1)
    assert "has_table_grid: True" in detail
    assert "find_tables fragments:" in detail
    assert "| Armor | AC | Cost |" in detail  # the rendered table is shown
    assert "| Chain | 16 | 75 |" in detail

    census = inspect.tables(pdf)  # whole-document census
    assert "table census" in census
    assert "p1:" in census


# --- PDF geometry helpers: justified-text / stat-block layouts --------------
# Some adventure PDFs (e.g. the_wild_sheep_chase) position each word of a
# justified line separately and use dash rules + wrapped display headings.


def test_merge_baseline_fragments_rejoins_justified_words():
    # one visual line emitted as separate per-word fragments at the same y
    words = ["Can", "the", "heroes", "put", "an", "end"]
    frags = [
        Line(text=w, size=8.0, bold=False, page=1, y=100.0, x0=320 + i * 25, x1=340 + i * 25)
        for i, w in enumerate(words)
    ]
    merged = _merge_baseline_fragments(frags, None)  # single column
    assert len(merged) == 1
    assert merged[0].text == "Can the heroes put an end"


def test_merge_baseline_fragments_keeps_columns_apart():
    # left-column and right-column lines share a baseline but must not merge
    geom = ([299.0], [44.0, 308.0])  # (splits, margins): gutter at the page centre
    left = Line(text="left column text", size=8.0, bold=False, page=1, y=100.0, x0=44, x1=280)
    right = Line(text="right column text", size=8.0, bold=False, page=1, y=100.0, x0=308, x1=553)
    merged = _merge_baseline_fragments([left, right], geom)
    assert {m.text for m in merged} == {"left column text", "right column text"}


def test_merge_baseline_fragments_three_columns():
    # three same-baseline lines, one per column, must stay separate (not glue
    # columns 2 and 3 together as a 2-column split would)
    geom = ([120.0, 296.0], [30.0, 207.0, 385.0])
    cols = [
        Line(text="Charm Monster", size=11.0, bold=False, page=1, y=53.0, x0=25, x1=106),
        Line(text="Chill Metal", size=11.0, bold=False, page=1, y=53.0, x0=203, x1=260),
        Line(text="Chill Touch", size=11.0, bold=False, page=1, y=53.0, x0=381, x1=439),
    ]
    merged = _merge_baseline_fragments(cols, geom)
    assert {m.text for m in merged} == {"Charm Monster", "Chill Metal", "Chill Touch"}


def test_merge_baseline_fragments_keeps_sizes_apart():
    # a margin display number and body text on the same baseline must not glue
    big = Line(text="16", size=23.0, bold=False, page=1, y=100.0, x0=89, x1=115)
    body = Line(
        text="looks like the skeleton", size=10.0, bold=False, page=1, y=100.0, x0=126, x1=341
    )
    merged = _merge_baseline_fragments([big, body], None)
    assert {m.text for m in merged} == {"16", "looks like the skeleton"}


def _two_column_lines(left_x, right_x, page=1):
    rows = []
    for i in range(12):
        y = 100 + i * 12
        rows.append(
            Line(
                text=f"left {i}", size=10.0, bold=False, page=page, y=y, x0=left_x, x1=left_x + 200
            )
        )
        rows.append(
            Line(
                text=f"right {i}",
                size=10.0,
                bold=False,
                page=page,
                y=y,
                x0=right_x,
                x1=right_x + 200,
            )
        )
    return rows


def test_column_geometry_offcentre_columns():
    # both columns sit left of the page midpoint (e.g. Tomb of Horrors); the
    # split must land in the gutter, not at width/2
    geom = _column_geometry(_two_column_lines(44, 270), page_width=612.0)
    assert geom is not None
    splits, margins = geom
    assert len(margins) == 2
    assert abs(margins[1] - 270) <= 4  # peak, modulo bin quantisation
    # split separates the columns and is well left of width/2 = 306 (off-centre)
    assert 44 < splits[0] <= margins[1] < 306


def test_column_geometry_three_columns():
    # a three-column page (e.g. PHB spell lists) yields three margins / two splits
    rows = []
    for i in range(12):
        y = 100 + i * 10
        for x in (30, 207, 385):
            rows.append(Line(text=f"c{x}", size=10.0, bold=False, page=1, y=y, x0=x, x1=x + 150))
    geom = _column_geometry(rows, page_width=612.0)
    assert geom is not None
    splits, margins = geom
    assert len(margins) == 3
    assert all(abs(m - e) <= 4 for m, e in zip(margins, [30, 207, 385], strict=True))
    assert len(splits) == 2


def test_column_geometry_ignores_sparse_indent():
    # a handful of indented lines between the columns must not be mistaken for
    # a column edge; the right column is the populated peak
    lines = _two_column_lines(56, 304)
    lines += [
        Line(text="indent", size=10.0, bold=False, page=1, y=300 + i, x0=238, x1=430)
        for i in range(3)
    ]
    geom = _column_geometry(lines, page_width=594.0)
    assert geom is not None
    splits, margins = geom
    assert len(margins) == 2
    assert abs(margins[1] - 304) <= 4  # the peak, not the sparse 238 indent


def test_column_geometry_ignores_gutter_spanning_header():
    # a full-width running header/banner that crosses the gutter must not bridge
    # it and collapse the split onto the left margin, which would fold both
    # columns into one and merge their lines across the gutter (the CHA23131
    # 'Haunting' bug, where "WELCOME TO CALL OF CTHULHU" spanned the columns)
    rows = []
    for i in range(12):
        y = 100 + i * 12
        rows.append(Line(text=f"left {i}", size=10.0, bold=False, page=1, y=y, x0=66, x1=284))
        rows.append(Line(text=f"right {i}", size=10.0, bold=False, page=1, y=y, x0=306, x1=520))
    rows.append(
        Line(
            text="A HEADER ACROSS THE GUTTER", size=12.0, bold=True, page=1, y=40.0, x0=227, x1=360
        )
    )
    geom = _column_geometry(rows, page_width=594.0)
    assert geom is not None
    splits, margins = geom
    assert len(margins) == 2
    # the split lands in the gutter between the columns, so each visual column
    # maps to its own index (collapsed-left would put the split near 66)
    assert 284 <= splits[0] <= 306


def test_column_geometry_single_column_returns_none():
    lines = [
        Line(text=f"line {i}", size=10.0, bold=False, page=1, y=100 + i * 12, x0=54, x1=520)
        for i in range(15)
    ]
    assert _column_geometry(lines, page_width=594.0) is None


def test_assess_quality_healthy_pdf():
    # one dominant body size carrying most text -> no warning
    lines = [Line(text="word " * 20, size=10.0, bold=False, page=p, y=10) for p in range(1, 11)]
    lines += [Line(text="A Heading", size=18.0, bold=False, page=p, y=5) for p in range(1, 11)]
    assert assess_quality(PdfContent(page_count=10, body_size=10.0, lines=lines)) is None


def test_assess_quality_allows_jittered_body_size():
    # a clean digital PDF (e.g. the 4e Monster Manual) renders its body font at a
    # spread of near-identical sizes: prose at 8.5, stat-block text at 8.0, plus
    # sub-point jitter at 8.4, none a majority alone, but all one body font once
    # bucketed to whole points, so no warning should fire.
    lines = []
    for p in range(1, 21):
        for size, n in [(8.0, 8), (8.4, 1), (8.5, 8)]:
            for i in range(n):
                lines.append(Line(text="real body prose here", size=size, bold=False, page=p, y=i))
    lines += [Line(text="Monster Name", size=14.0, bold=False, page=p, y=200) for p in range(1, 21)]
    assert assess_quality(PdfContent(page_count=20, body_size=8.5, lines=lines)) is None


def test_assess_quality_flags_scattered_sizes():
    # OCR-style: lots of text but spread across many sizes, no dominant body font
    lines = []
    for p in range(1, 21):
        for i, size in enumerate([8.1, 9.3, 10.4, 11.6, 12.7, 13.9, 15.1, 16.2]):
            lines.append(
                Line(text="some scanned words here", size=size, bold=False, page=p, y=i * 10)
            )
    warning = assess_quality(PdfContent(page_count=20, body_size=10.4, lines=lines))
    assert warning is not None
    assert "dominant body font" in warning


def test_assess_quality_flags_empty_text_layer():
    # a scanned image with no real text layer
    lines = [Line(text="3", size=10.0, bold=False, page=p, y=10) for p in range(1, 31)]
    warning = assess_quality(PdfContent(page_count=30, body_size=10.0, lines=lines))
    assert warning is not None
    assert "scanned image" in warning


def test_assess_quality_flags_gibberish_text_layer():
    # a stripped-ToUnicode Type0 font extracts as a substitution cipher: plenty
    # of text at one dominant size, but each "word" is studded with punctuation
    # and digits, so few of the non-space characters are letters.
    cipher = ":6)!13$4),#!&:<5:68$%O36I:$()$%*+ -./0%#$ #3IS7IZ$ "
    lines = [
        Line(text=cipher, size=11.0, bold=False, page=p, y=i * 10)
        for p in range(1, 11)
        for i in range(3)
    ]
    warning = assess_quality(PdfContent(page_count=10, body_size=11.0, lines=lines))
    assert warning is not None
    assert "gibberish" in warning


def test_assess_quality_allows_number_dense_body():
    # stat blocks and dice notation push up the digit/symbol count, but real
    # prose still clears the letter-share gate (must not false-positive).
    body = "The ogre has AC 11 and 59 hit points; it deals 2d8 plus 4 bludgeoning damage. "
    lines = [
        Line(text=body, size=10.0, bold=False, page=p, y=i * 10)
        for p in range(1, 11)
        for i in range(3)
    ]
    assert assess_quality(PdfContent(page_count=10, body_size=10.0, lines=lines)) is None


def test_is_location_header():
    def loc(text):
        return _is_location_header(Line(text=text, size=10.0, bold=False, page=1, y=0))

    assert loc("1. False Entrance Tunnel (EL 10)")
    assert loc("0. In Front of the Hill")
    assert loc("3. Entrance to the Tomb of Horrors (EL Varies)")  # 9 words
    assert not loc("Trap: If the roof is prodded, the tunnel collapses.")  # no leading N.
    assert not loc("1. Roll a d20 and add your modifier to the result.")  # sentence, ends "."
    assert not loc("1d4 bandits attack")  # not "N." form


def test_is_heading_rejects_glyph_specimen():
    # A calligraphy specimen on a credits page, set in the book's largest face,
    # must not read as a heading (it would parent every later section).
    specimen = Line(
        text="abcdefghijklmnopqrst uvwxyz•••1234567890", size=40.0, bold=False, page=1, y=10
    )
    assert not _is_heading(specimen, body_size=10.0)
    # Real titles at the same size still count.
    assert _is_heading(Line(text="Making Characters", size=40.0, bold=False, page=1, y=10), 10.0)
    assert _is_heading(Line(text="Armor and Shields", size=14.0, bold=True, page=1, y=10), 10.0)


def test_is_heading_accepts_display_type_with_trailing_punctuation():
    body_size = 9.0
    big = 25.0  # display size, well over body * DISPLAY_HEADING_RATIO

    def h(text, size=big):
        return _is_heading(Line(text=text, size=size, bold=False, page=1, y=10), body_size)

    # A display-set location label keeps heading status despite its colon or
    # semicolon ("LOCATION 8:", "HIGHER COURTS;"); display type isn't prose. A
    # stylized ellipsis title counts too.
    assert h("LOCATION 8:")
    assert h("HIGHER COURTS;")
    assert h("Not Like This...")
    # But a sentence period/comma is prose even at display size, and a body-sized
    # colon line is a lead-in sentence, not a heading.
    assert not h("appear cultured.")
    assert not h("Explore the ruins of an ancient drow city,")
    assert not h("Read the following to the players:", size=9.0)


def test_merge_wrapped_headings_chains_three_display_lines():
    # A display heading wrapped over three lines ("LOCATION 8:" / "THE CHAPEL
    # OF" / "CONTEMPLATION") must chain the whole way down: the gap is measured
    # from the last line merged, not the first, so the third line still fits.
    body_size = 9.0
    l1 = Line(text="LOCATION 8:", size=25.0, bold=False, page=5, y=206.4, x0=312, x1=427)
    l2 = Line(text="THE CHAPEL OF", size=25.0, bold=False, page=5, y=230.4, x0=312, x1=446)
    l3 = Line(text="CONTEMPLATION", size=25.0, bold=False, page=5, y=254.4, x0=312, x1=457)
    body = Line(text="Read the following...", size=9.0, bold=False, page=5, y=285.0, x0=312)
    merged = _merge_wrapped_headings([l1, l2, l3, body], body_size)
    assert [m.text for m in merged] == ["LOCATION 8: THE CHAPEL OF CONTEMPLATION", body.text]


def test_glyph_specimen_does_not_parent_sections():
    body_size = 10.0
    content = PdfContent(body_size=body_size)
    content.lines = [
        # specimen in the largest face on a credits page
        Line(text="abcdefghijklmnopqrst uvwxyz 1234567890", size=40.0, bold=False, page=1, y=10),
        Line(text="Chapter 2: Making Characters", size=24.0, bold=False, page=2, y=10),
        Line(text="Ability Scores", size=16.0, bold=False, page=2, y=40),
        Line(
            text="Choose your ability scores and record them.", size=10.0, bold=False, page=2, y=70
        ),
    ]
    sections = sections_from_pdf(content)
    crumbs = [s.breadcrumb for s in sections]
    assert not any("abcdefghij" in c for c in crumbs)
    assert "Chapter 2: Making Characters > Ability Scores" in crumbs


def test_is_decorative_rule():
    assert _is_decorative_rule("-" * 40)
    assert _is_decorative_rule("———")
    assert not _is_decorative_rule("Armor Class 14 (leather armour)")
    assert not _is_decorative_rule("--")  # too short to be a divider
    assert not _is_decorative_rule("...")  # leader dots are not a rule


def test_merge_wrapped_headings():
    body_size = 8.0
    # a display heading that wrapped onto two lines (same size, one line apart)
    line1 = Line(
        text="Modified Wand of True", size=20.0, bold=False, page=6, y=39.0, x0=306, x1=522
    )
    line2 = Line(text="Polymorph", size=20.0, bold=False, page=6, y=59.0, x0=306, x1=407)
    body = Line(text="Formed of a long, thin twig...", size=8.0, bold=False, page=6, y=82.0, x0=306)
    merged = _merge_wrapped_headings([line1, line2, body], body_size)
    assert [m.text for m in merged] == ["Modified Wand of True Polymorph", body.text]


def test_merge_wrapped_headings_leaves_title_and_subtitle():
    # different sizes (title over subtitle) must not merge
    body_size = 8.0
    title = Line(text="The Wild Sheep Chase", size=50.0, bold=False, page=1, y=570.0, x0=44, x1=547)
    subtitle = Line(text="A fifth level adventure", size=20.0, bold=False, page=1, y=625.0, x0=97)
    merged = _merge_wrapped_headings([title, subtitle], body_size)
    assert [m.text for m in merged] == ["The Wild Sheep Chase", "A fifth level adventure"]


def test_merge_wrapped_headings_keeps_distinct_weights():
    # a bold section header above a non-bold sub-heading is two headings, not a
    # wrapped one; same size and adjacent, but the weight differs
    body_size = 12.0
    header = Line(text="Characters", size=20.0, bold=True, page=10, y=38.0, x0=39, x1=136)
    npc = Line(text="Tillus Merrion", size=20.0, bold=False, page=10, y=67.0, x0=39, x1=160)
    merged = _merge_wrapped_headings([header, npc], body_size)
    assert [m.text for m in merged] == ["Characters", "Tillus Merrion"]


def test_decorated_page_number_dropped():
    from openadventure.ingest.extract import _drop_repeating_furniture

    lines = [
        Line(text="Inside the Cave", size=20.0, bold=True, page=7, y=10),
        Line(text="Hit Points 47 (7d10+10)", size=12.0, bold=False, page=7, y=20),
        Line(text="~ 7 ~", size=12.0, bold=False, page=7, y=800),  # decorated page no.
        Line(text="42", size=12.0, bold=False, page=8, y=800),  # bare page no.
    ]
    kept = {ln.text for ln in _drop_repeating_furniture(lines, page_count=8)}
    assert "~ 7 ~" not in kept
    assert "42" not in kept
    assert "Hit Points 47 (7d10+10)" in kept  # real stat line survives


def _statblock_doc():
    from openadventure.ingest.extract import PdfContent

    # a stat block whose name is in a *larger* font than the section headings
    return PdfContent(
        page_count=1,
        body_size=10.0,
        lines=[
            Line(text="Scene 1: Ambush", size=20.0, bold=True, page=1, y=10),
            Line(text="Wolves pour from the trees.", size=10.0, bold=False, page=1, y=20),
            Line(text="Flame", size=24.0, bold=True, page=1, y=30),  # stat-block name
            Line(text="Large beast, chaotic neutral", size=10.0, bold=False, page=1, y=40),
            Line(text="Armor Class 14 (natural armour)", size=10.0, bold=False, page=1, y=50),
            Line(text="Hit Points 47 (7d10+10)", size=10.0, bold=False, page=1, y=60),
            Line(text="Actions", size=20.0, bold=False, page=1, y=80),  # stat-internal label
            Line(text="Bite. Melee Weapon Attack: +5 to hit.", size=10.0, bold=False, page=1, y=90),
            Line(text="Scene 2: Aftermath", size=20.0, bold=True, page=1, y=110),
            Line(text="The dust settles.", size=10.0, bold=False, page=1, y=120),
        ],
    )


def test_statblock_name_does_not_parent_later_sections():
    from openadventure.ingest.sections import MAX_LEVEL, sections_from_pdf

    sections = {s.title: s for s in sections_from_pdf(_statblock_doc())}

    # the stat block is a leaf, carrying its whole body including the folded
    # "Actions" label, and "Actions" is not split off into its own section
    flame = sections["Flame"]
    assert flame.level == MAX_LEVEL
    assert "Armor Class" in flame.body
    assert "Actions" in flame.body and "Bite." in flame.body
    assert "Actions" not in sections

    # the section after the stat block is NOT nested beneath it
    assert "Flame" not in sections["Scene 2: Aftermath"].breadcrumb


def test_spell_entries_split_into_sections():
    from openadventure.ingest.extract import PdfContent
    from openadventure.ingest.sections import MAX_LEVEL, sections_from_pdf

    # spell names sit just above body size (below the heading threshold) and are
    # only recognisable from the spell stat block right below them
    content = PdfContent(
        page_count=1,
        body_size=9.0,
        lines=[
            Line(text="4TH-LEVEL SPELLS", size=14.0, bold=True, page=1, y=5),
            Line(text="Charm Monster", size=11.0, bold=False, page=1, y=20),
            Line(text="Enchantment (Charm)", size=9.0, bold=False, page=1, y=30),
            Line(text="Casting Time: 1 standard action", size=9.0, bold=True, page=1, y=40),
            Line(text="Components: V, S", size=9.0, bold=True, page=1, y=50),
            Line(text="Range: Close", size=9.0, bold=True, page=1, y=60),
            Line(text="Duration: One day/level", size=9.0, bold=True, page=1, y=70),
            Line(
                text="This spell functions like charm person.", size=9.0, bold=False, page=1, y=80
            ),
            Line(text="Fireball", size=11.0, bold=False, page=1, y=100),
            Line(text="Evocation [Fire]", size=9.0, bold=False, page=1, y=110),
            Line(text="Casting Time: 1 standard action", size=9.0, bold=True, page=1, y=120),
            Line(text="Components: V, S, M", size=9.0, bold=True, page=1, y=130),
            Line(text="Range: Long", size=9.0, bold=True, page=1, y=140),
            Line(text="Duration: Instantaneous", size=9.0, bold=True, page=1, y=150),
            Line(text="A fireball is a burst of flame.", size=9.0, bold=False, page=1, y=160),
        ],
    )
    sections = {s.title: s for s in sections_from_pdf(content)}
    assert "Charm Monster" in sections and "Fireball" in sections
    assert sections["Fireball"].level == MAX_LEVEL
    assert "burst of flame" in sections["Fireball"].body
    # each spell is its own leaf, not swallowed by the previous one
    assert "burst of flame" not in sections["Charm Monster"].body


def test_dnd4e_powers_split_into_named_sections():
    from openadventure.ingest.extract import PdfContent
    from openadventure.ingest.sections import MAX_LEVEL, sections_from_pdf

    # 4e powers: a bold name line (font drifts above an 8.8pt body), then a
    # right-aligned "<Class> Attack|Utility N" header, then usage/action/effect.
    # The header is the tell; the name becomes a leaf section titled with it.
    content = PdfContent(
        page_count=1,
        body_size=8.8,
        lines=[
            Line(text="Level 1 At-Will Exploits", size=14.0, bold=False, page=1, y=5),
            Line(text="Deft Strike", size=11.0, bold=True, page=1, y=20),
            Line(text="Rogue Attack 1", size=8.8, bold=False, page=1, y=20, x0=193),
            Line(
                text="A final lunge brings you into position.", size=8.8, bold=False, page=1, y=30
            ),
            Line(text="At-Will ✦ Martial, Weapon", size=9.0, bold=True, page=1, y=40),
            Line(text="Standard Action Melee or Ranged", size=8.8, bold=True, page=1, y=50),
            Line(text="Attack: Dexterity vs. AC", size=8.8, bold=True, page=1, y=60),
            Line(text="Hit: 1[W] + Dexterity modifier damage.", size=8.8, bold=True, page=1, y=70),
            Line(text="Good Omens", size=11.0, bold=True, page=1, y=100),
            Line(text="Divine Oracle Utility 12", size=8.8, bold=False, page=1, y=100, x0=193),
            Line(text="You predict good fortune.", size=8.8, bold=False, page=1, y=110),
            Line(text="Daily ✦ Divine", size=9.0, bold=True, page=1, y=120),
            Line(text="Standard Action Ranged 10", size=8.8, bold=True, page=1, y=130),
            Line(text="Effect: Allies gain a +5 power bonus.", size=8.8, bold=True, page=1, y=140),
        ],
    )
    sections = {s.title: s for s in sections_from_pdf(content)}
    assert "Deft Strike" in sections and "Good Omens" in sections
    assert sections["Deft Strike"].kind == "spell"
    assert sections["Deft Strike"].level == MAX_LEVEL
    # each power is its own leaf, carrying its full body, not swallowing the next
    assert "Dexterity vs. AC" in sections["Deft Strike"].body
    assert "+5 power bonus" not in sections["Deft Strike"].body
    assert "+5 power bonus" in sections["Good Omens"].body


def test_prose_ending_in_attack_n_is_not_a_power():
    from openadventure.ingest.sections import _power_name_indices

    # a header-shaped phrase ("... attack 5") at body size, with no larger-than-body
    # name line above it, must not be mistaken for a power
    lines = [
        Line(text="Normal prose runs on here", size=10.0, bold=False, page=1, y=5),
        Line(text="and then resolves the attack 5", size=10.0, bold=False, page=1, y=15),
    ]
    assert _power_name_indices(lines, body_size=10.0) == set()


class _FakeRow:
    def __init__(self, cells):
        self.cells = cells  # list of (x0, y0, x1, y1) bboxes or None


class _FakeTable:
    """Minimal stand-in for a pymupdf find_tables table, exposing what the table
    helpers read: col_count, row_count, rows[i].cells bboxes, the overall bbox
    (defaulted from the cells), and extract() (the cell text find_tables found)."""

    def __init__(self, col_count, rows, bbox=None, extract_text=None):
        self.col_count = col_count
        self.row_count = len(rows)
        self._raw_rows = rows
        self._extract = extract_text
        self.rows = [_FakeRow(c) for c in rows]
        boxes = [b for row in rows for b in row if b]
        self.bbox = bbox or (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )

    def extract(self):
        if self._extract is not None:
            return self._extract
        return [["" for _ in row] for row in self._raw_rows]


def test_reextract_table_unjams_from_word_geometry():
    from openadventure.ingest.extract import _reextract_table

    # row 0 split correctly (two cell bboxes); row 1 is jammed -- find_tables put
    # the whole row in cell 0 and left a degenerate cell 1. The words, though, sit
    # at the right x positions, so re-bucketing by the column edges recovers it.
    table = _FakeTable(
        2,
        [
            [(10, 0, 50, 10), (60, 0, 90, 10)],  # good row: real column geometry
            [(10, 12, 90, 22), (90, 12, 90, 22)],  # jammed: cell 0 spans, cell 1 degenerate
        ],
    )
    words = [
        (10, 2, 18, 9, "Ale"),
        (60, 2, 68, 9, "4"),
        (12, 14, 30, 21, "Bread"),
        (62, 14, 70, 21, "2"),
    ]
    grid = _reextract_table(table, words)
    assert grid == [["Ale", "4"], ["Bread", "2"]]


def test_reextract_clips_adjacent_column_prose():
    from openadventure.ingest.extract import _reextract_table

    # a word from an adjacent page column (far left of the table) shares the row's
    # y but lies outside the table's x span, so it must not leak into a cell
    table = _FakeTable(2, [[(300, 0, 360, 10), (360, 0, 400, 10)]])
    words = [
        (40, 2, 90, 9, "prose-from-other-column"),
        (300, 2, 330, 9, "Item"),
        (362, 2, 380, 9, "5"),
    ]
    grid = _reextract_table(table, words)
    assert grid == [["Item", "5"]]


def _two_row_table():
    # a 2-column table occupying y 0-22, x 10-90 (two body-font rows)
    table = _FakeTable(
        2, [[(10, 0, 50, 11), (60, 0, 90, 11)], [(10, 11, 50, 22), (60, 11, 90, 22)]]
    )
    words = [
        (10, 2, 40, 9, "Tiny"),
        (60, 2, 85, 9, "small"),
        (10, 13, 40, 20, "Small"),
        (60, 13, 85, 20, "medium"),
    ]
    return table, words


def test_recover_trailing_row_appends_dropped_row():
    from openadventure.ingest.extract import _recover_trailing_row

    table, words = _two_row_table()
    # a body-font row just below the table, aligned to the columns
    words += [(10, 24, 45, 31, "Large"), (60, 24, 88, 31, "big")]
    assert _recover_trailing_row(table, words) == [["Large", "big"]]


def test_recover_skips_heading_below():
    from openadventure.ingest.extract import _recover_trailing_row

    table, words = _two_row_table()
    # the next section heading is set in a clearly larger font (tall word boxes)
    words += [(10, 24, 70, 40, "Section"), (72, 24, 95, 40, "Title")]
    assert _recover_trailing_row(table, words) == []


def test_recover_skips_prose_below():
    from openadventure.ingest.extract import _recover_trailing_row

    table, words = _two_row_table()
    # a prose line flows across the column gap (a word straddles the col-1 edge)
    words += [(10, 24, 58, 31, "flowing"), (40, 24, 88, 31, "sentence")]
    assert _recover_trailing_row(table, words) == []


def _one_row_frag(y):
    # a single-row 2-column fragment occupying [y, y+10], columns at x10 and x60
    return _FakeTable(2, [[(10, y, 50, y + 10), (60, y, 90, y + 10)]])


def test_mergeable_single_row_fragments():
    from openadventure.ingest.extract import _mergeable

    top, bot = _one_row_frag(0), _one_row_frag(20)
    body = [(12, 2, 40, 9, "a"), (62, 2, 88, 9, "b")]  # body words inside top, none in gap
    assert _mergeable(top, bot, body, 9.0)


def test_not_mergeable_heading_in_gap():
    from openadventure.ingest.extract import _mergeable

    top, bot = _one_row_frag(0), _one_row_frag(20)
    # a clearly taller line sits in the gap -> a heading between two tables
    words = [(12, 2, 40, 9, "a"), (62, 2, 88, 9, "b"), (12, 13, 80, 27, "HEADING")]
    assert not _mergeable(top, bot, words, 9.0)


def test_not_mergeable_multirow_or_far_or_misaligned():
    from openadventure.ingest.extract import _mergeable

    body = [(12, 2, 40, 9, "a"), (62, 2, 88, 9, "b")]
    multi = _FakeTable(
        2, [[(10, 0, 50, 10), (60, 0, 90, 10)], [(10, 10, 50, 20), (60, 10, 90, 20)]]
    )
    assert not _mergeable(multi, _one_row_frag(20), body, 9.0)  # detected table has >1 row
    assert not _mergeable(_one_row_frag(0), _one_row_frag(100), body, 9.0)  # gap too large
    misaligned = _FakeTable(2, [[(110, 20, 150, 30), (160, 20, 190, 30)]])
    assert not _mergeable(_one_row_frag(0), misaligned, body, 9.0)  # different columns


def test_gap_row_reads_multiline_cell():
    from openadventure.ingest.extract import _gap_row

    # a skipped row whose first cell wraps to two lines
    words = [
        (10, 12, 40, 19, "Alpha"),
        (10, 22, 55, 29, "continues"),
        (60, 12, 80, 19, "1D6"),
    ]
    assert _gap_row(10, 35, [10.0, 60.0], 10.0, 90.0, words) == [["Alpha continues", "1D6"]]


def test_cluster_tables_groups_mergeable_and_splits_far():
    from openadventure.ingest.extract import _cluster_tables

    body = [(12, 2, 40, 9, "x"), (62, 2, 88, 9, "y")]  # body words inside the first fragment
    top, mid, far = _one_row_frag(0), _one_row_frag(20), _one_row_frag(100)
    clusters = _cluster_tables([top, mid, far], body, 9.0)
    assert [len(c) for c in clusters] == [2, 1]  # top+mid merge; far is its own cluster

    # a heading sitting between two otherwise-mergeable fragments splits them
    with_heading = body + [(12, 13, 80, 27, "HEADING")]
    split = _cluster_tables([_one_row_frag(0), _one_row_frag(20)], with_heading, 9.0)
    assert [len(c) for c in split] == [1, 1]


def test_merged_cluster_assembles_fragments_with_gap_rows():
    from openadventure.ingest.extract import _merged_cluster_rows

    # three single-row fragments find_tables caught (header + two data rows); the
    # rows it skipped sit in the gaps as positioned words and must be filled in
    def frag(y, text):
        cells = [(10, y, 50, y + 10), (60, y, 100, y + 10), (110, y, 200, y + 10)]
        return _FakeTable(3, [cells], extract_text=[text])

    cluster = [
        frag(0, ["Injury", "Damage", "Examples"]),
        frag(40, ["Moderate", "1D6", "Falling onto grass"]),
        frag(80, ["Deadly", "2D10", "Hit by a car"]),
    ]
    words = [
        # "Minor" gap row (between header and Moderate); its first cell wraps
        (10, 15, 45, 23, "Minor"),
        (10, 25, 55, 33, "survives"),
        (60, 15, 80, 23, "1D3"),
        (110, 15, 190, 23, "Punch"),
        # "Severe" gap row (between Moderate and Deadly)
        (10, 55, 45, 63, "Severe"),
        (60, 55, 82, 63, "1D10"),
        (110, 55, 195, 63, "Bullet"),
    ]
    assert _merged_cluster_rows(cluster, words) == [
        ["Injury", "Damage", "Examples"],
        ["Minor survives", "1D3", "Punch"],
        ["Moderate", "1D6", "Falling onto grass"],
        ["Severe", "1D10", "Bullet"],
        ["Deadly", "2D10", "Hit by a car"],
    ]


def test_prose_above_label_is_not_a_spell():
    from openadventure.ingest.sections import _spell_name_indices

    # a larger line followed by only one spell-ish word must not be a spell
    lines = [
        Line(text="A Bigger Heading", size=14.0, bold=True, page=1, y=5),
        Line(text="The range of options is broad.", size=10.0, bold=False, page=1, y=15),
        Line(text="More prose continues here.", size=10.0, bold=False, page=1, y=25),
    ]
    assert _spell_name_indices(lines, body_size=10.0) == set()


def test_oversized_heading_without_stats_is_not_a_statblock():
    from openadventure.ingest.extract import Line, PdfContent
    from openadventure.ingest.sections import MAX_LEVEL, sections_from_pdf

    # a large-font heading that is NOT followed by Armor Class / Hit Points must
    # keep its structural level (it is a real section, not a creature)
    content = PdfContent(
        page_count=1,
        body_size=10.0,
        lines=[
            Line(text="Appendix", size=24.0, bold=True, page=1, y=10),
            Line(text="Some closing notes for the GM.", size=10.0, bold=False, page=1, y=20),
        ],
    )
    appendix = sections_from_pdf(content)[0]
    assert appendix.title == "Appendix"
    assert appendix.level < MAX_LEVEL


# --- campaign module ingestion + tools --------------------------------------

MODULE_MD = """\
# Death House

## Rose and Thorn

Read-aloud: Two children stand in the street. "There's a monster in our house!"

The children are illusions created by the house.

## Secret Stairs

A hidden staircase in the attic leads to the dungeon level.

## 13. Bathroom

A bathroom on the third floor contains a claw-foot tub.
"""


async def test_module_announced_in_context(make_session, workspace, campaign, tmp_path):
    source = tmp_path / "death-house.md"
    source.write_text(MODULE_MD, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir("death-house"))

    session = make_session(script=[text_turn("Welcome to Death House.")])
    session.add_module("death-house")  # attach the ingested book as a module
    overview = session.campaign_arc_overview()
    assert overview is not None
    assert "in reading order" in overview
    assert "death-house/13-bathroom.md" in overview
    # listed in reading order, not filename order: "13. Bathroom" reads last but
    # sorts first alphabetically
    assert overview.index("rose-and-thorn") < overview.index("13-bathroom")
    assert session.meta.active_module == "death-house"  # first attached becomes active
    await collect(session.handle_input("let's begin"))
    context = session.provider.calls[0].messages[0].content[0].text
    assert "CANONICAL SOURCE" in context
    assert "death-house" in context
    assert "rose-and-thorn" in context  # section names listed
    # detaching the module drops the announcement
    session.remove_module("death-house")
    messages, _ = session.build_messages()
    assert "CANONICAL SOURCE" not in messages[0].content[0].text


def test_nonbold_display_headings_detected():
    from openadventure.ingest.extract import Line, PdfContent
    from openadventure.ingest.sections import sections_from_pdf

    content = PdfContent(
        page_count=1,
        body_size=9.0,
        lines=[
            Line(text="Death House", size=24.0, bold=False, page=1, y=10),
            Line(text="An old row house looms.", size=9.0, bold=False, page=1, y=20),
            Line(text="1. Entrance", size=15.0, bold=False, page=1, y=30),
            Line(text="A rusty gate hangs open.", size=9.0, bold=False, page=1, y=40),
            # marginally larger non-bold body text must NOT become a heading
            Line(text="A dramatic callout line", size=10.5, bold=False, page=1, y=50),
        ],
    )
    sections = sections_from_pdf(content)
    titles = [s.title for s in sections]
    assert titles == ["Death House", "1. Entrance"]
    assert "dramatic callout" in sections[1].body


def test_campaign_module_search_and_read(workspace, campaign, tmp_path):
    source = tmp_path / "death-house.md"
    source.write_text(MODULE_MD, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir("death-house"))

    meta = campaign.load_meta()
    meta.modules = [ModuleRef(slug="death-house", title="Death House", order=0)]
    meta.active_module = "death-house"
    registry = build_registry(workspace, campaign, meta)
    assert "search_campaign" in registry
    ctx = make_ctx(workspace, campaign)

    hits = registry.dispatch(ctx, "search_campaign", {"query": "monster in our house"})
    assert hits.ok and "death-house/" in hits.content

    path_line = next(line for line in hits.content.splitlines() if line.startswith("death-house/"))
    section_path = path_line.split(" — ")[0].strip()
    body = registry.dispatch(ctx, "read_campaign", {"section_path": section_path})
    assert "illusions" in body.content

    bathroom = registry.dispatch(ctx, "read_campaign", {"section_path": "death-house/13-bathroom"})
    assert bathroom.ok
    assert "third floor" in bathroom.content
    assert bathroom.summary == "read death-house/13-bathroom.md"

    bare_bathroom = registry.dispatch(ctx, "read_campaign", {"section_path": "13-bathroom"})
    assert bare_bathroom.ok
    assert "third floor" in bare_bathroom.content

    bad = registry.dispatch(ctx, "read_campaign", {"section_path": "no-module-prefix.md"})
    assert not bad.ok


def test_read_campaign_links_adjacent_sections_in_reading_order(workspace, campaign, tmp_path):
    # Reading a section ends with pointers to the previous/next section in reading
    # order, so the GM can step through the module even past the context outline.
    source = tmp_path / "death-house.md"
    source.write_text(MODULE_MD, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir("death-house"))
    meta = campaign.load_meta()
    meta.modules = [ModuleRef(slug="death-house", title="Death House", order=0)]
    meta.active_module = "death-house"
    registry = build_registry(workspace, campaign, meta)
    ctx = make_ctx(workspace, campaign)

    # "Secret Stairs" reads before "13. Bathroom" although 13-bathroom sorts first
    stairs = registry.dispatch(ctx, "read_campaign", {"section_path": "death-house/secret-stairs"})
    assert "Adjacent sections in reading order" in stairs.content
    assert "13-bathroom.md" in stairs.content.split("next → ")[1].splitlines()[0]
    assert "rose-and-thorn.md" in stairs.content.split("previous → ")[1].splitlines()[0]

    # the last section in reading order offers a previous but no next
    bathroom = registry.dispatch(ctx, "read_campaign", {"section_path": "death-house/13-bathroom"})
    assert "next →" not in bathroom.content
    assert "secret-stairs.md" in bathroom.content.split("previous → ")[1].splitlines()[0]


def test_read_rules_links_adjacent_sections_in_reading_order(tmp_path):
    from openadventure.engine.tools.rules_tools import ReadArgs, make_rules_tools

    source = tmp_path / "rules.md"
    source.write_text(FIXTURE_MD, encoding="utf-8")
    dest = tmp_path / "dnd"
    pipeline.ingest(source, dest)
    read = {t.name: t for t in make_rules_tools([dest])}["read_rules"]

    # reading order is ...opportunity-attacks, magic, fireball...; alphabetically
    # "fireball" precedes "magic", so next=fireball proves it follows reading order
    out = read.handler(None, ReadArgs(section_path="dnd/rules-of-the-realm/magic.md"))
    assert out.ok
    assert "fireball.md" in out.content.split("next → ")[1].splitlines()[0]
    assert "opportunity-attacks.md" in out.content.split("previous → ")[1].splitlines()[0]


def test_outline_rules_lists_sections_in_reading_order(tmp_path):
    from openadventure.engine.tools.rules_tools import OutlineArgs, make_rules_tools

    source = tmp_path / "rules.md"
    source.write_text(FIXTURE_MD, encoding="utf-8")
    dest = tmp_path / "dnd"
    pipeline.ingest(source, dest)
    outline = {t.name: t for t in make_rules_tools([dest])}["outline_rules"]

    full = outline.handler(None, OutlineArgs())
    assert full.ok
    assert full.content.count("- dnd/") == 6  # every section, as a read_rules path
    # reading order, not alphabetical: opportunity-attacks before fireball
    assert full.content.index("opportunity-attacks.md") < full.content.index("fireball.md")

    # 'under' restricts to a sub-tree by breadcrumb
    magic = outline.handler(None, OutlineArgs(under="Magic"))
    assert "fireball.md" in magic.content and "healing-word.md" in magic.content
    assert "combat.md" not in magic.content

    # start/limit pages a long book
    page = outline.handler(None, OutlineArgs(start=0, limit=2))
    assert page.content.count("- dnd/") == 2
    assert "call again with start=2" in page.content


def test_outline_campaign_lists_active_module_in_reading_order(workspace, campaign, tmp_path):
    source = tmp_path / "death-house.md"
    source.write_text(MODULE_MD, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir("death-house"))
    meta = campaign.load_meta()
    meta.modules = [ModuleRef(slug="death-house", title="Death House", order=0)]
    meta.active_module = "death-house"
    campaign.save_meta(meta)
    registry = build_registry(workspace, campaign, meta)
    assert "outline_campaign" in registry
    ctx = make_ctx(workspace, campaign)

    out = registry.dispatch(ctx, "outline_campaign", {})
    assert out.ok
    assert out.content.count("- death-house/") == 4  # root + three sections
    # reading order: "Rose and Thorn" precedes "13. Bathroom" though 13 sorts first
    assert out.content.index("rose-and-thorn.md") < out.content.index("13-bathroom.md")
