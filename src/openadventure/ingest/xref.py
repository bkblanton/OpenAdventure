"""Cross-reference graph: which sections mention which named entities.

An *entity* is a section that names a thing worth linking to: a monster stat
block or a spell. Every other section that mentions that name by text gets an
*edge* to it, so a retrieved section can carry the stat block / spell behind the
names it mentions instead of forcing the GM to search again.

Everything here is derived from the stored section rows
``(title, breadcrumb, body, path, kind)``, with no PDF, no LLM, no extra deps, so
``reindex`` can rebuild it offline, and it tolerates a mangled path hierarchy
because resolution is by *name*, not by walking the tree.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

XREF_NAME = "xref.sqlite"

# v1 links the two high-value, low-noise kinds. Locations ("area 14") need
# number-aware matching and are deferred; the `kind` is still recorded upstream.
ENTITY_KINDS = ("monster", "spell")

_MIN_NAME_LEN = 3
_MAX_NAME_TOKENS = 6  # don't treat a whole sentence-titled section as a name
_TOKEN = re.compile(r"[a-z0-9]+")

# Entity detection is per game-system, since each one names its stat blocks and
# spells differently. classify_kind tries every profile (a section is a monster
# or spell if ANY system's tells match). All markers run on the stored body, so
# markdown/txt and hand-edited sections (no font geometry) classify on reindex.
#
# d20 / D&D: a stat block opens with line-anchored AC and HP. Both full-word
# ("Armor Class 15") and abbreviated ("AC 15", the 5.2 SRD) forms count; the
# line anchor + trailing number keeps prose like "your AC is 10" out. The
# leading/inner ``\W*`` absorbs markdown markup so OCR-to-markdown stat blocks
# ("- **Armor Class** 13") classify the same as a PDF's "Armor Class 13".
_STAT_AC = re.compile(r"(?im)^\W*(?:armor class|ac)\b\W*\d")
_STAT_HP = re.compile(r"(?im)^\W*(?:hit points|hp)\b\W*\d")
_DND_SPELL_MARKERS = ("casting time", "components", "duration", "range", "saving throw")
# D&D 4e has "powers" (exploits/prayers/spells/utilities), not 5e-style spells,
# with a wholly different vocabulary that shares nothing with _DND_SPELL_MARKERS.
# A power block pairs a usage tier with a 4e action line and at least one
# stat-block label; that triple is a tight cluster 5e/3.5e/GURPS/CoC text and
# plain prose don't hit (5e dropped "standard"/"minor action" terminology).
_DND4E_USAGE = ("at-will", "encounter", "daily")
_DND4E_ACTIONS = (
    "standard action",
    "move action",
    "minor action",
    "free action",
    "immediate reaction",
    "immediate interrupt",
    "opportunity action",
)
_DND4E_LABELS = ("attack:", "hit:", "miss:", "effect:", "target:", "trigger:", "requirement:")
# GURPS Magic: spell entries carry a cluster of these labels.
_GURPS_SPELL_MARKERS = ("time to cast:", "prerequisite", "duration:", "cost:", "resisted by")

_KIND_ORDER = {"monster": 0, "spell": 1}


@dataclass
class Entity:
    name: str  # canonical, casefolded
    path: str
    kind: str


@dataclass
class Reference:
    path: str
    name: str
    kind: str
    confidence: float


def classify_kind(title: str, body: str) -> str | None:
    """Best-effort entity kind from the body text alone, across game systems.
    Used for rows whose stored ``kind`` is generic (markdown/txt, or pre-`kind`
    sections on reindex)."""
    low = body.casefold()
    # --- monsters / creatures ---
    if _STAT_AC.search(body) and _STAT_HP.search(body):
        return "monster"  # d20 / D&D
    if "damage bonus" in low and "magic points" in low and ("sanity loss" in low or "build" in low):
        return "monster"  # Call of Cthulhu creature stat block
    # --- spells ---
    if sum(m in low for m in _DND_SPELL_MARKERS) >= 3:
        return "spell"  # d20 / D&D
    if (
        any(m in low for m in _DND4E_USAGE)
        and any(m in low for m in _DND4E_ACTIONS)
        and any(m in low for m in _DND4E_LABELS)
    ):
        return "spell"  # D&D 4e power
    if sum(m in low for m in _GURPS_SPELL_MARKERS) >= 3:
        return "spell"  # GURPS Magic
    if "magic points" in low and "cost" in low and ("casting" in low or "sanity points" in low):
        return "spell"  # Call of Cthulhu ("Cost: N magic points", "Casting time")
    return None


def _is_linkable_title(title: str) -> bool:
    """Reject chapter-header / page-artifact titles that classify as an entity
    but shouldn't be linkable names: ALL-CAPS headers ("MAGIC", "INVESTIGATORS"),
    page-number-tagged headers ("ANIMALS AND MONSTERS 455", "190 SKILLS"), and
    anything not starting with a letter. Real spell/creature names are mixed-case
    and digit-free, so this costs nothing on D&D/GURPS/CoC entries."""
    t = title.strip()
    if not t[:1].isalpha():
        return False
    if t.replace(" ", "").isupper():
        return False
    return not any(ch.isdigit() for ch in t)


def _aliases(name: str) -> set[str]:
    """Surface forms to match: the name plus a naive singular/plural variant."""
    n = name.casefold().strip()
    out = {n}
    if n.endswith("s") and len(n) > _MIN_NAME_LEN:
        out.add(n[:-1])
    else:
        out.add(n + "s")
    return out


Row = tuple[str, str, str, str, str]  # (title, breadcrumb, body, path, kind)


def build_entities(rows: list[Row]) -> list[Entity]:
    entities: list[Entity] = []
    seen: set[str] = set()
    ambiguous: set[str] = set()
    for title, _breadcrumb, body, path, kind in rows:
        effective = kind if kind in ENTITY_KINDS else classify_kind(title, body)
        if effective not in ENTITY_KINDS:
            continue
        if not _is_linkable_title(title):
            continue
        name = title.casefold().strip()
        tokens = _TOKEN.findall(name)
        if len(name) < _MIN_NAME_LEN or not tokens or len(tokens) > _MAX_NAME_TOKENS:
            continue
        if name in seen:
            ambiguous.add(name)  # a name pointing at two sections can't be resolved
            continue
        seen.add(name)
        entities.append(Entity(name=name, path=path, kind=effective))
    # drop names that turned out to be ambiguous so we never mislink them
    return [e for e in entities if e.name not in ambiguous]


def build_edges(rows: list[Row], entities: list[Entity]) -> list[tuple[str, str, str, str]]:
    """Edges as (src_path, dst_path, matched_name, kind). One edge per
    (source, target): a section that names a monster twice links once."""
    by_first: dict[str, list[tuple[list[str], Entity]]] = {}
    for entity in entities:
        for alias in _aliases(entity.name):
            atoks = _TOKEN.findall(alias)
            if atoks:
                by_first.setdefault(atoks[0], []).append((atoks, entity))

    edges: list[tuple[str, str, str, str]] = []
    for _title, _breadcrumb, body, path, _kind in rows:
        tokens = _TOKEN.findall(body.casefold())
        linked: set[str] = set()
        i, n = 0, len(tokens)
        while i < n:
            candidates = by_first.get(tokens[i])
            if candidates:
                best: tuple[int, Entity] | None = None
                for atoks, entity in candidates:
                    length = len(atoks)
                    if tokens[i : i + length] == atoks and (best is None or length > best[0]):
                        best = (length, entity)
                if best is not None:
                    entity = best[1]
                    if entity.path != path and entity.path not in linked:
                        edges.append((path, entity.path, entity.name, entity.kind))
                        linked.add(entity.path)
                    i += best[0]
                    continue
            i += 1
    return edges


def write_xref(
    db_path: Path, entities: list[Entity], edges: list[tuple[str, str, str, str]]
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE entities (name TEXT, path TEXT, kind TEXT)")
        con.execute(
            "CREATE TABLE edges (src_path TEXT, dst_path TEXT, name TEXT, kind TEXT, confidence REAL)"
        )
        con.executemany(
            "INSERT INTO entities VALUES (?, ?, ?)",
            [(e.name, e.path, e.kind) for e in entities],
        )
        con.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, 1.0)",
            edges,
        )
        con.execute("CREATE INDEX edges_src ON edges(src_path)")
        con.execute("CREATE INDEX entities_name ON entities(name)")
        con.commit()
    finally:
        con.close()


def build(db_path: Path, rows: list[Row]) -> tuple[int, int]:
    """Build the whole graph for one corpus. Returns (entity_count, edge_count)."""
    entities = build_entities(rows)
    edges = build_edges(rows, entities)
    write_xref(db_path, entities, edges)
    return len(entities), len(edges)


def references_for(db_path: Path, src_path: str, limit: int = 12) -> list[Reference]:
    """Outbound references from a section, monsters/spells first, then by name.
    Returns [] when there's no graph (graceful; pre-xref workspaces)."""
    if not db_path.is_file():
        return []
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT dst_path, name, kind, confidence FROM edges WHERE src_path = ?",
            (src_path,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    refs = [Reference(path=p, name=n, kind=k, confidence=c) for p, n, k, c in rows]
    refs.sort(key=lambda r: (_KIND_ORDER.get(r.kind, 9), r.name))
    return refs[:limit]
