"""The agent's knowledge of OpenAdventure itself, surfaced on demand.

The GM (and the assistant) carry no meta-knowledge of the app in their context;
when a player steps out of character to ask how OpenAdventure works ("how do I
undo?", "what models can I run?", "what is this?"), they fetch it with the
``read_docs`` tool instead of guessing. This mirrors how rules are handled:
don't memorize the book, look it up.

Frontend-neutral content (the README/product description) is sourced here. The
frontend-specific parts (its terminal ``--help`` and in-game slash commands)
differ per frontend, so the frontend assembles those strings and passes them to
``build_docs``; the engine never imports the frontend.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from openadventure.engine.tools.registry import Tool, ToolContext, ToolOutcome

Section = Literal["about", "commands", "cli"]

# Display order and human titles for the doc sections. 'commands' (the in-game
# slash commands) comes before 'cli' (terminal commands) because during play the
# player is typing slash commands, so that is the section asked about most.
SECTION_TITLES: dict[str, str] = {
    "about": "About OpenAdventure",
    "commands": "In-game slash commands (typed with a leading / during play)",
    "cli": "Terminal commands (run as 'openadventure ...' outside the game)",
}
SECTION_ORDER: tuple[str, ...] = ("about", "commands", "cli")


def _readme() -> str | None:
    """The product README, the frontend-neutral 'what is this' section.

    Primary source is the long description baked into package metadata at build
    time (``pyproject`` declares ``readme = "README.md"``), which is present even
    in an editable install. Falls back to a repo-root ``README.md`` on disk for
    the case where metadata is unavailable.
    """
    try:
        meta = metadata.metadata("openadventure")
        text = meta.get("Description") or meta.get_payload()
        if isinstance(text, str) and text.strip():
            return text.strip()
    except metadata.PackageNotFoundError:
        pass
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "README.md"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    return None


def build_docs(*, cli_help: str | None = None, slash_help: str | None = None) -> dict[str, str]:
    """Assemble the doc sections ``read_docs`` serves.

    ``cli_help`` and ``slash_help`` are the frontend's own ``--help`` and
    slash-command listing; omit them (e.g. headless/tests) and only the README
    section is available.
    """
    docs: dict[str, str] = {}
    about = _readme()
    if about:
        docs["about"] = about
    if cli_help and cli_help.strip():
        docs["cli"] = cli_help.strip()
    if slash_help and slash_help.strip():
        docs["commands"] = slash_help.strip()
    return docs


class ReadDocsArgs(BaseModel):
    section: Section | None = Field(
        default=None,
        description=(
            "Which part to read: 'about' (what OpenAdventure is and its features), "
            "'commands' (the in-game slash commands a player types during play, like "
            "/model, /undo, /sheet), or 'cli' (terminal commands run outside the game, "
            "like 'openadventure setup'). A 'command' the player can use mid-game is "
            "almost always an in-game slash command, so prefer 'commands' unless they "
            "clearly mean the terminal. Omit for an overview."
        ),
    )


_DESCRIPTION = (
    "Look up how OpenAdventure itself works (its features, terminal commands, and "
    "in-game slash commands) to answer a player's out-of-character question about the "
    "app or how to do something in it (saving, undo, changing settings or model, "
    "ingesting books). This is the software's own help, not in-world game content; for "
    "rules and lore use search_rules instead. Translate what you find into a plain "
    "out-of-character reply."
)


def make_read_docs_tool(docs: dict[str, str]) -> Tool:
    """The ``read_docs`` tool over the assembled doc ``sections``."""
    available = [k for k in SECTION_ORDER if k in docs]

    def handler(ctx: ToolContext, args: ReadDocsArgs) -> ToolOutcome:
        if not docs:
            return ToolOutcome(
                content="OpenAdventure's own documentation isn't available in this session.",
                summary="no docs",
                ok=False,
            )
        if args.section is not None:
            if args.section not in docs:
                return ToolOutcome(
                    content=(f"No '{args.section}' docs here. Available: {', '.join(available)}."),
                    summary="no such section",
                    ok=False,
                )
            return ToolOutcome(
                content=f"# {SECTION_TITLES[args.section]}\n\n{docs[args.section]}",
                summary=f"docs: {args.section}",
            )
        # No section: lead with About (the most likely "what is this"), then point
        # at the rest so a follow-up can pull the specific command help.
        parts: list[str] = []
        if "about" in docs:
            parts.append(f"# {SECTION_TITLES['about']}\n\n{docs['about']}")
        others = [k for k in available if k != "about"]
        if others:
            menu = "; ".join(f"section='{k}' for {SECTION_TITLES[k].lower()}" for k in others)
            parts.append(f"More OpenAdventure docs: call read_docs again with {menu}.")
        return ToolOutcome(content="\n\n".join(parts), summary="docs: overview")

    return Tool(
        name="read_docs",
        description=_DESCRIPTION,
        args_model=ReadDocsArgs,
        handler=handler,
        parallel_safe=True,
        read_only=True,
    )
