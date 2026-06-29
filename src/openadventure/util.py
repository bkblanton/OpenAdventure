"""Small cross-cutting helpers."""

from __future__ import annotations


def shorten(text: str, limit: int = 60) -> str:
    """Collapse runs of whitespace and truncate to ``limit`` characters with a
    trailing ellipsis. Used for one-line labels and summaries."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "..."
