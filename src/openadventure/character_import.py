"""Frontend-neutral preparation of character-sheet imports."""

from __future__ import annotations

import json
from pathlib import Path

IMPORT_SUFFIXES = (".md", ".markdown", ".txt", ".text", ".json")
IMPORT_MAX_CHARS = 40_000
IMPORT_MAX_BYTES = IMPORT_MAX_CHARS * 4
IMPORT_PREFIX = "[OUT-OF-CHARACTER: The player is importing an existing character sheet"

_IMPORT_INSTRUCTION = (
    "[OUT-OF-CHARACTER: The player is importing an existing character sheet from a "
    "file. Read the character described below and create a player-character sheet for "
    "them by calling create_sheet, following this campaign's character template as "
    "closely as the source allows. Transcribe every name, class/role, level, ability "
    "score, skill, item, and numeric resource (hp and the like) you can find; map them "
    "onto the template and keep anything that doesn't fit under fields. Do not invent "
    "details the source doesn't provide. After the sheet exists, briefly confirm what "
    "you imported and note anything that was missing or couldn't be mapped.\n\n"
    "--- IMPORTED CHARACTER SHEET ({filename}) ---\n{content}\n"
    "--- END IMPORTED CHARACTER SHEET ---]"
)


def prepare_character_import(filename: str, content: str) -> tuple[str, bool]:
    """Validate and frame a text-based character sheet for an engine turn."""

    suffix = Path(filename).suffix.casefold()
    if suffix not in IMPORT_SUFFIXES:
        raise ValueError(
            f"Unsupported file type {suffix or '(none)'!r}; import a .md, .txt, or .json file."
        )

    normalized = content.strip()
    if not normalized:
        raise ValueError(f"{filename} is empty; nothing to import.")
    if suffix == ".json":
        try:
            normalized = json.dumps(json.loads(normalized), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{filename} isn't valid JSON: {exc}") from exc

    truncated = len(normalized) > IMPORT_MAX_CHARS
    if truncated:
        normalized = normalized[:IMPORT_MAX_CHARS]
    return _IMPORT_INSTRUCTION.format(filename=filename, content=normalized), truncated
