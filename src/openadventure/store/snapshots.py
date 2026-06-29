"""Atomic JSON snapshot files (write-tmp then os.replace)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def save_json(path: Path, data: BaseModel | dict[str, Any] | list[Any]) -> None:
    """Atomically write JSON: a crash never leaves a half-written snapshot."""
    payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path: Path) -> Any | None:
    """Load a JSON snapshot, or None if the file doesn't exist."""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
