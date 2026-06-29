"""SQLite FTS5 search index over ingested sections."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

INDEX_NAME = "index.sqlite"


@dataclass
class SearchHit:
    path: str
    title: str
    breadcrumb: str
    snippet: str
    score: float


def create_index(db_path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """rows: (title, breadcrumb, body, path)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE VIRTUAL TABLE sections USING fts5("
            "title, breadcrumb, body, path UNINDEXED, "
            "tokenize='porter unicode61')"
        )
        con.executemany("INSERT INTO sections VALUES (?, ?, ?, ?)", rows)
        con.commit()
    finally:
        con.close()


def _fts_query(query: str) -> str:
    """Sanitize free text into an FTS5 query: quoted terms, AND semantics,
    falling back to OR when AND finds nothing is handled by the caller."""
    terms = re.findall(r"[A-Za-z0-9']+", query)
    return " ".join(f'"{t}"' for t in terms)


def fetch(db_path: Path, path: str) -> SearchHit | None:
    """Look a section up by exact path, used to dress a vector-only hit (which
    knows only its path) with the title/breadcrumb/snippet a SearchHit carries."""
    if not db_path.is_file():
        return None
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT path, title, breadcrumb, substr(body, 1, 240) FROM sections WHERE path = ?",
            (path,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    if row is None:
        return None
    p, title, crumb, body = row
    snippet = " ".join(body.split()[:18])
    return SearchHit(path=p, title=title, breadcrumb=crumb, snippet=snippet, score=0.0)


def sections_in_reading_order(db_path: Path) -> list[tuple[str, str]]:
    """(path, breadcrumb) for every section in the order it was ingested, which is
    the source document's own reading order: rows are inserted top-to-bottom by the
    pipeline, so the FTS rowid recovers that sequence. Used to show the GM the
    module's sections as an ordered outline with hierarchy (a handout or read-aloud
    box sits right under the section it belongs to), instead of an alphabetized file
    list that scatters children away from their parents. Empty if the index is
    missing or unreadable."""
    if not db_path.is_file():
        return []
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT path, breadcrumb FROM sections ORDER BY rowid").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    return [(path, breadcrumb or "") for path, breadcrumb in rows]


def search(db_path: Path, query: str, k: int = 5) -> list[SearchHit]:
    fts = _fts_query(query)
    if not fts:
        return []
    con = sqlite3.connect(db_path)
    try:
        sql = (
            "SELECT path, title, breadcrumb, "
            "snippet(sections, 2, '[', ']', ' … ', 12) AS snip, bm25(sections) AS score "
            "FROM sections WHERE sections MATCH ? ORDER BY score LIMIT ?"
        )
        try:
            rows = con.execute(sql, (fts, k)).fetchall()
        except sqlite3.OperationalError:
            return []
        if not rows and " " in fts:
            rows = con.execute(sql, (fts.replace(" ", " OR "), k)).fetchall()
        return [
            SearchHit(path=path, title=title, breadcrumb=crumb, snippet=snip, score=score)
            for path, title, crumb, snip, score in rows
        ]
    finally:
        con.close()
