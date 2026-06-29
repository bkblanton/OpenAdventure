"""Terminal/console setup shared by every CLI entry point."""

from __future__ import annotations

import contextlib
import sys

from rich.console import Console

_console: Console | None = None


def make_console() -> Console:
    """Return the shared Rich console, with Windows-safe UTF-8 output.

    Legacy Windows consoles (and non-TTY pipes) default to cp1252, which
    crashes on emoji/dice glyphs. Reconfiguring to UTF-8 with replacement
    keeps output working everywhere; Windows Terminal renders it natively.
    """
    global _console
    if _console is None:
        if sys.platform == "win32":
            for stream in (sys.stdout, sys.stderr):
                reconfigure = getattr(stream, "reconfigure", None)
                if reconfigure is not None:
                    with contextlib.suppress(OSError, ValueError):
                        reconfigure(encoding="utf-8", errors="replace")
            # piped stdin from PowerShell often carries a UTF-8 BOM
            stdin_reconfigure = getattr(sys.stdin, "reconfigure", None)
            if stdin_reconfigure is not None and not sys.stdin.isatty():
                with contextlib.suppress(OSError, ValueError):
                    stdin_reconfigure(encoding="utf-8-sig", errors="replace")
        _console = Console()
    return _console
