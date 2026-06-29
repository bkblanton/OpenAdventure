"""Append-only JSONL event log: the campaign's source of truth.

Each line: {"seq": int, "ts": iso8601, "type": str, "data": {...}}.
Appends flush+fsync; reads tolerate a torn final line (crash mid-append).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class LogEntry(BaseModel):
    seq: int
    ts: str
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class EventLog:
    def __init__(self, path: Path):
        self.path = path
        self._next_seq = self._scan_next_seq()

    def _scan_next_seq(self) -> int:
        last = 0
        for entry in self.read_all():
            last = entry.seq
        return last + 1

    @property
    def last_seq(self) -> int:
        return self._next_seq - 1

    def append(self, type: str, data: dict[str, Any] | None = None) -> LogEntry:
        entry = LogEntry(
            seq=self._next_seq,
            ts=datetime.now(UTC).isoformat(timespec="seconds"),
            type=type,
            data=data or {},
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._next_seq += 1
        return entry

    def read_all(self) -> list[LogEntry]:
        """Read every entry, skipping a torn (unparseable) final line."""
        if not self.path.is_file():
            return []
        entries: list[LogEntry] = []
        with open(self.path, encoding="utf-8") as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(LogEntry.model_validate(json.loads(line)))
            except json.JSONDecodeError, ValueError:
                if i == len(lines) - 1:
                    break  # torn final line from a crash mid-append
                raise
        return entries

    def tail(self, n: int) -> list[LogEntry]:
        return self.read_all()[-n:]

    def read_since(self, seq: int) -> list[LogEntry]:
        """Entries with seq strictly greater than `seq`."""
        return [e for e in self.read_all() if e.seq > seq]

    def truncate_to(self, seq: int, *, archive: Path | None = None) -> list[LogEntry]:
        """Remove entries with seq > `seq` (for undo). Removed lines are first
        appended to `archive` (never lost), then the log is atomically
        rewritten. Returns the removed entries. No-op when seq >= last_seq."""
        if seq >= self.last_seq:
            return []
        entries = self.read_all()
        kept = [e for e in entries if e.seq <= seq]
        removed = [e for e in entries if e.seq > seq]

        if archive is not None and removed:
            archive.parent.mkdir(parents=True, exist_ok=True)
            with open(archive, "a", encoding="utf-8") as f:
                for entry in removed:
                    f.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for entry in kept:
                f.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._next_seq = seq + 1
        return removed

    def refresh(self) -> None:
        """Re-scan the file for the next seq (after external log replacement)."""
        self._next_seq = self._scan_next_seq()
