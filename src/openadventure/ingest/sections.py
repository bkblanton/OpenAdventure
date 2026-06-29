"""Split extracted content into markdown sections.

Every detected heading starts a section; a section's body runs until the next
heading of any level. Heading levels come from font-size clusters (PDF) or
``#`` depth (markdown). Breadcrumbs record the hierarchy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from openadventure.ingest.extract import Line, PdfContent
from openadventure.store.workspace import slugify

MAX_LEVEL = 4
MAX_HEADING_CHARS = 90
CHUNK_WORDS = 2000  # plain-text fallback chunk size
# A line at least this many times the body size is display type. Such a line may
# end in a colon and still be a heading ("LOCATION 8:"), where a body-sized colon
# line is a lead-in sentence ("Read the following to the players:").
DISPLAY_HEADING_RATIO = 1.5


@dataclass
class Section:
    title: str
    level: int
    breadcrumb: str
    body: str
    start_page: int = 0
    end_page: int = 0
    path: str = ""  # relative path within sections/, set by the pipeline
    kind: str = "section"  # section | monster | spell | location; drives cross-refs
    order: int = 0  # 0-based position in the source's reading order, set by the pipeline

    def word_count(self) -> int:
        return len(self.body.split())


_ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def _is_glyph_specimen(text: str) -> bool:
    """A font/calligraphy specimen line prints the alphabet in sequence
    ("abcdefghij...uvwxyz 1234567890"), often on a credits or sample page. It is
    set in the book's largest display face, so size alone reads it as the top
    heading and parents every later section under it. A run of the alphabet in
    order is the unmistakable tell; real prose and titles never carry one."""
    letters = "".join(ch for ch in text.casefold() if ch.isalpha())
    return any(_ALPHABET[i : i + 6] in letters for i in range(len(_ALPHABET) - 5))


def _is_heading(line: Line, body_size: float) -> bool:
    if line.is_table or not line.text:
        return False
    if len(line.text.strip()) <= 1 or len(line.text) > MAX_HEADING_CHARS:
        return False  # a lone large glyph is a drop cap, not a heading
    if _is_glyph_specimen(line.text):
        return False
    # bold headings need +1.5pt over body text; non-bold display fonts
    # (e.g. WotC adventure modules) need to be clearly larger (+2.5pt)
    threshold = body_size + (1.5 if line.bold else 2.5)
    if line.size < threshold:
        return False
    # Trailing punctuation usually marks a sentence (a lead-in like "Read the
    # following to the players:" or a body line), not a heading. Display type set
    # far larger than the body is not running prose, so a label colon/semicolon
    # ("LOCATION 8:", "HIGHER COURTS;") or a stylized ellipsis ("Not Like
    # This...") still heads a section; a sentence period or comma does not.
    if line.size >= body_size * DISPLAY_HEADING_RATIO:
        return not (line.text.endswith((".", ",")) and not line.text.endswith("..."))
    return not line.text.endswith((".", ":", ";", ","))


# Keyed-location header: "1. False Entrance Tunnel (EL 10)", "0. In Front of the
# Hill". Old modules number their room entries in a display font set at body
# size, so size alone can't see them, but the leading "N." on a short,
# title-cased line is a reliable tell.
_LOCATION_HEADER = re.compile(r"^\d{1,3}\.\s+[A-Za-z]")
_TITLE_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")
LOCATION_LEVEL = 3


def _is_location_header(line: Line) -> bool:
    if line.is_table:
        return False
    text = line.text.strip()
    if not _LOCATION_HEADER.match(text):
        return False
    if len(text) > 55 or len(text.split()) > 10:
        return False
    if text.endswith((".", ":", ";", ",")):
        return False
    # A title is title-cased ("False Entrance Tunnel"); a numbered list item
    # buried in prose ("2. The size change also affects...") is sentence-cased.
    # Require at least half the meaningful words to be capitalised.
    words = [w for w in _TITLE_WORD.findall(text[text.index(".") + 1 :]) if len(w) >= 3]
    if not words:
        return False
    return sum(w[0].isupper() for w in words) / len(words) >= 0.5


def _level_map(sizes: list[float]) -> dict[float, int]:
    """Cluster distinct heading sizes (within 1pt) into levels 1..MAX_LEVEL."""
    clusters: list[list[float]] = []
    for size in sorted(set(sizes), reverse=True):
        if clusters and clusters[-1][-1] - size <= 1.0:
            clusters[-1].append(size)
        else:
            clusters.append([size])
    mapping: dict[float, int] = {}
    for i, cluster in enumerate(clusters):
        for size in cluster:
            mapping[size] = min(i + 1, MAX_LEVEL)
    return mapping


def _merge_wrapped_headings(lines: list[Line], body_size: float) -> list[Line]:
    """Join a heading that wrapped onto further lines (e.g. a display title or
    magic-item name too wide for its column). Consecutive heading lines merge
    when they share a font size, weight, and column and sit one line apart with
    no body text between them. The gap is measured from the last line merged, not
    the first, so a heading spanning three or more lines ("LOCATION 8: THE CHAPEL
    OF CONTEMPLATION") chains the whole way down. Differently-styled lines (a bold
    section header above a non-bold sub-heading, or a title over a subtitle) and
    headings separated by body text are left alone."""
    merged: list[Line] = []
    for line in lines:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and _is_heading(line, body_size)
            and _is_heading(prev, body_size)
            and line.size == prev.size
            and line.bold == prev.bold
            and line.page == prev.page
            and 0 <= line.y - prev.y <= line.size * 1.8
        ):
            merged[-1] = Line(
                text=f"{prev.text} {line.text}",
                size=prev.size,
                bold=prev.bold,
                page=prev.page,
                y=line.y,  # anchor the next gap check on the last line merged
                x0=min(prev.x0, line.x0),
                x1=max(prev.x1, line.x1),
            )
        else:
            merged.append(line)
    return merged


# A creature/NPC stat block opens with a name, then these labelled lines. Their
# names are often set in a larger display font than the surrounding section
# headings; without special handling they would hijack the heading hierarchy and
# parent every later section. "Actions"/"Reactions"/etc. are stat-block-internal
# labels printed at heading size; they belong to the block, not the document.
_STATBLOCK_MARKERS = ("armor class", "hit points")
_STATBLOCK_LOOKAHEAD = 14
_STATBLOCK_LABELS = {
    "actions",
    "reactions",
    "bonus actions",
    "legendary actions",
    "lair actions",
    "villain actions",
    "traits",
}


def _statblock_name_indices(lines: list[Line], body_size: float) -> set[int]:
    """Indices of heading lines that open a stat block: a heading immediately
    followed (within a short window) by the tell-tale ``Armor Class`` and
    ``Hit Points`` lines."""
    names: set[int] = set()
    for i, line in enumerate(lines):
        if not _is_heading(line, body_size):
            continue
        window = " \n".join(
            ln.text.casefold() for ln in lines[i + 1 : i + 1 + _STATBLOCK_LOOKAHEAD]
        )
        if all(marker in window for marker in _STATBLOCK_MARKERS):
            names.add(i)
    return names


# A d20 spell entry opens with its name, then a stat block of labelled lines.
# Spell names are set just above body size (below the heading threshold) and
# come in dense multi-column lists, so size and hierarchy alone miss them; the
# stat block right below the name is the reliable tell.
_SPELL_MARKERS = ("casting time", "components", "duration", "range", "saving throw")
_SPELL_LOOKAHEAD = 12


def _spell_name_indices(lines: list[Line], body_size: float) -> set[int]:
    """Indices of lines that open a spell entry: a short, larger-than-body name
    line followed within a few lines by at least three spell-stat labels."""
    names: set[int] = set()
    for i, line in enumerate(lines):
        if line.is_table or line.size <= body_size:
            continue
        text = line.text.strip()
        if not text or len(text) > 50 or not text[0].isalpha():
            continue
        window = " \n".join(ln.text.casefold() for ln in lines[i + 1 : i + 1 + _SPELL_LOOKAHEAD])
        if sum(marker in window for marker in _SPELL_MARKERS) >= 3:
            names.add(i)
    return names


# A D&D 4e power opens with its name, then a right-aligned "<Class/Path>
# Attack|Utility <level>" header line, then usage/action/effect lines. Power-name
# font size drifts across the book (10-11pt vs an 8.8pt body), so size alone
# classifies some as headings and buries the rest in prose; the header line right
# below the name is the size-independent tell. Anchoring on it (rather than on the
# name's own geometry) makes every power a leaf section titled with its real name.
_POWER_HEADER = re.compile(r"(?i)\b(?:attack|utility)\s+\d{1,2}$")
_POWER_HEADER_MAX_CHARS = 40


def _power_name_indices(lines: list[Line], body_size: float) -> set[int]:
    """Indices of D&D 4e power-name lines: a short, larger-than-body title sitting
    directly above an ``... Attack|Utility N`` header line."""
    names: set[int] = set()
    for i, line in enumerate(lines):
        if i == 0 or line.is_table:
            continue
        header = line.text.strip()
        if len(header) > _POWER_HEADER_MAX_CHARS or not _POWER_HEADER.search(header):
            continue
        name = lines[i - 1]
        text = name.text.strip()
        if (
            not name.is_table
            and name.size > body_size
            and text
            and text[0].isalpha()
            and len(text) <= 50
            and not text.endswith((".", ":", ";", ","))
        ):
            names.add(i - 1)
    return names


def sections_from_pdf(content: PdfContent) -> list[Section]:
    content.lines = _merge_wrapped_headings(content.lines, content.body_size)
    statblock_names = _statblock_name_indices(content.lines, content.body_size)
    # 4e power names are leaves like spell names (kind "spell", never a parent),
    # so fold them into the same set and the same handling below.
    spell_names = _spell_name_indices(content.lines, content.body_size) | _power_name_indices(
        content.lines, content.body_size
    )
    # A leaf entity name (stat block, spell, or 4e power) must not feed the
    # heading-size clusters: a power name set larger than the body would otherwise
    # mint a spurious heading level.
    leaf_names = statblock_names | spell_names
    heading_sizes = [
        line.size
        for i, line in enumerate(content.lines)
        if _is_heading(line, content.body_size) and i not in leaf_names
    ]
    levels = _level_map(heading_sizes)

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    current: Section | None = None
    buffer: list[str] = []
    last_page = 1
    in_statblock = False

    def flush() -> None:
        nonlocal current, buffer
        if current is not None:
            current.body = _tidy_body(buffer)
            current.end_page = last_page
            sections.append(current)
        buffer = []

    for i, line in enumerate(content.lines):
        last_page = line.page
        is_statblock_name = i in statblock_names
        is_spell_name = i in spell_names
        size_heading = _is_heading(line, content.body_size)
        location_heading = not size_heading and _is_location_header(line)
        if (size_heading or location_heading or is_spell_name) and not (
            in_statblock
            and not is_statblock_name
            and line.text.strip().casefold() in _STATBLOCK_LABELS
        ):
            flush()
            # A stat block or spell name is a leaf, never a structural parent,
            # even when (stat block) its font is larger than the section
            # headings around it or (spell) smaller. A numbered location header
            # sits just under the section it falls in.
            if is_statblock_name or is_spell_name:
                level = MAX_LEVEL
            elif location_heading:
                level = LOCATION_LEVEL
            else:
                level = levels.get(line.size, MAX_LEVEL)
            if is_statblock_name:
                kind = "monster"
            elif is_spell_name:
                kind = "spell"
            elif location_heading:
                kind = "location"
            else:
                kind = "section"
            in_statblock = is_statblock_name
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, line.text))
            breadcrumb = " > ".join(title for _, title in stack)
            current = Section(
                title=line.text,
                level=level,
                breadcrumb=breadcrumb,
                body="",
                start_page=line.page,
                kind=kind,
            )
        else:
            if current is None:
                current = Section(
                    title="Front Matter",
                    level=1,
                    breadcrumb="Front Matter",
                    body="",
                    start_page=line.page,
                )
            buffer.append(line.text if not line.is_table else "\n" + line.text + "\n")
    flush()
    return [s for s in sections if s.body.strip() or s.level < MAX_LEVEL]


def _tidy_body(lines: list[str]) -> str:
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --- markdown ingestion -----------------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def sections_from_markdown(text: str) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    current: Section | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current, buffer
        if current is not None:
            current.body = "\n".join(buffer).strip()
            sections.append(current)
        buffer = []

    for line in text.splitlines():
        match = _MD_HEADING.match(line)
        if match:
            flush()
            level = min(len(match.group(1)), MAX_LEVEL)
            title = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current = Section(
                title=title,
                level=level,
                breadcrumb=" > ".join(t for _, t in stack),
                body="",
            )
        else:
            if current is None and line.strip():
                current = Section(title="Front Matter", level=1, breadcrumb="Front Matter", body="")
            buffer.append(line)
    flush()
    return [s for s in sections if s.body.strip() or s.title != "Front Matter"]


# --- plain-text ingestion ----------------------------------------------------


def _txt_is_heading(line: str, next_line: str | None) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return False
    if stripped.endswith((".", ":", ";", ",", "!", "?")):
        return False
    words = stripped.split()
    if len(words) > 8:
        return False
    if stripped.isupper():
        return True
    # Title Case line followed by a blank or body line
    return stripped.istitle() and (next_line is None or not next_line.strip().istitle())


def sections_from_text(text: str) -> list[Section]:
    lines = text.splitlines()
    has_structure = sum(
        1
        for i, line in enumerate(lines)
        if _txt_is_heading(line, lines[i + 1] if i + 1 < len(lines) else None)
    )
    if has_structure >= 2:
        sections: list[Section] = []
        current = Section(title="Front Matter", level=1, breadcrumb="Front Matter", body="")
        buffer: list[str] = []
        for i, line in enumerate(lines):
            if _txt_is_heading(line, lines[i + 1] if i + 1 < len(lines) else None):
                current.body = "\n".join(buffer).strip()
                if current.body or current.title != "Front Matter":
                    sections.append(current)
                buffer = []
                title = line.strip()
                current = Section(title=title, level=1, breadcrumb=title, body="")
            else:
                buffer.append(line)
        current.body = "\n".join(buffer).strip()
        if current.body:
            sections.append(current)
        return sections

    # unstructured: fixed-size chunks
    words = text.split()
    sections = []
    for i in range(0, len(words), CHUNK_WORDS):
        n = i // CHUNK_WORDS + 1
        sections.append(
            Section(
                title=f"Part {n}",
                level=1,
                breadcrumb=f"Part {n}",
                body=" ".join(words[i : i + CHUNK_WORDS]),
            )
        )
    return sections


# --- paths -------------------------------------------------------------------


def assign_paths(sections: list[Section]) -> None:
    """Give each section a unique relative path: <top-slug>/<leaf-slug>.md."""
    seen: set[str] = set()
    for section in sections:
        crumbs = section.breadcrumb.split(" > ")
        top = slugify(crumbs[0]) if crumbs else "misc"
        leaf = slugify(section.title)
        candidate = f"{top}/{leaf}.md" if section.level > 1 else f"{top}.md"
        base = candidate[:-3]
        n = 2
        while candidate in seen:
            candidate = f"{base}-{n}.md"
            n += 1
        seen.add(candidate)
        section.path = candidate
