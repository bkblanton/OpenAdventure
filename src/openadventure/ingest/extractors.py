"""Source-type extractors: map a document (or folder) to ``Section`` objects.

Each extractor owns one input shape: a PDF, a Markdown file, plain text, and
later AI-OCR for scanned PDFs or an Obsidian vault folder. The pipeline asks
each extractor whether it ``matches`` a source, then calls ``extract`` on the
first that does; everything downstream (writing sections to disk, building the
indexes, the manifest) is shared and type-agnostic.

To add a new source type, implement :class:`Extractor` and append it to
:data:`EXTRACTORS`. No change to ``pipeline.ingest`` is needed: type-specific
signals (a quality ``warning``, ``image_only_pages``, extra manifest fields)
ride back on :class:`ExtractionResult`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from openadventure.ingest import sections as sections_mod
from openadventure.ingest.progress import ProgressFn
from openadventure.ingest.sections import Section


@dataclass
class ExtractionResult:
    """One extractor's output.

    ``sections`` is the extracted content. ``warning`` is a one-line note when
    the extraction looks low-quality (surfaced to the user, e.g. a scanned PDF
    with no real text layer). ``image_only_pages`` lists 1-based pages whose
    image content could not be read as text. ``manifest`` holds any extra
    type-specific fields to merge verbatim into the stored manifest.
    """

    sections: list[Section]
    warning: str | None = None
    image_only_pages: list[int] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Extractor(Protocol):
    #: Human-readable name of the source type, for the unsupported-source error.
    name: str

    def matches(self, source: Path) -> bool:
        """Whether this extractor handles ``source``, by suffix, by being a
        directory of a known shape, etc."""
        ...

    def extract(
        self,
        source: Path,
        *,
        pages: tuple[int, int] | None = None,
        progress: ProgressFn | None = None,
    ) -> ExtractionResult:
        """Read ``source`` into sections. ``pages`` (a 1-based inclusive range)
        and ``progress`` are honoured by extractors that can (PDF); others
        ignore them."""
        ...


class PdfExtractor:
    """Extract a PDF via the pymupdf-backed text/table pipeline, carrying its
    quality assessment and any image-only pages back to the manifest."""

    name = "PDF (.pdf)"

    def matches(self, source: Path) -> bool:
        return source.suffix.lower() == ".pdf"

    def extract(
        self,
        source: Path,
        *,
        pages: tuple[int, int] | None = None,
        progress: ProgressFn | None = None,
    ) -> ExtractionResult:
        # pymupdf stays behind this seam: imported lazily, only when a PDF is
        # actually being ingested.
        from openadventure.ingest.extract import assess_quality, extract_pdf

        content = extract_pdf(source, pages=pages, progress=progress)
        return ExtractionResult(
            sections=sections_mod.sections_from_pdf(content),
            warning=assess_quality(content),
            image_only_pages=content.image_only_pages,
        )


class MarkdownExtractor:
    name = "Markdown (.md, .markdown)"

    def matches(self, source: Path) -> bool:
        return source.suffix.lower() in (".md", ".markdown")

    def extract(
        self,
        source: Path,
        *,
        pages: tuple[int, int] | None = None,
        progress: ProgressFn | None = None,
    ) -> ExtractionResult:
        text = source.read_text(encoding="utf-8", errors="replace")
        return ExtractionResult(sections=sections_mod.sections_from_markdown(text))


class TextExtractor:
    name = "plain text (.txt)"

    def matches(self, source: Path) -> bool:
        return source.suffix.lower() == ".txt"

    def extract(
        self,
        source: Path,
        *,
        pages: tuple[int, int] | None = None,
        progress: ProgressFn | None = None,
    ) -> ExtractionResult:
        text = source.read_text(encoding="utf-8", errors="replace")
        return ExtractionResult(sections=sections_mod.sections_from_text(text))


# Tried in order; the first whose ``matches`` returns True wins. Append new
# source types here (AI-OCR for scanned PDFs, an Obsidian vault folder, ...).
EXTRACTORS: list[Extractor] = [PdfExtractor(), MarkdownExtractor(), TextExtractor()]


def select_extractor(source: Path) -> Extractor:
    """The extractor that handles ``source``, or raise ``ValueError`` naming the
    supported source types."""
    for extractor in EXTRACTORS:
        if extractor.matches(source):
            return extractor
    supported = ", ".join(e.name for e in EXTRACTORS)
    raise ValueError(f"unsupported source {source.name!r} (supported: {supported})")
