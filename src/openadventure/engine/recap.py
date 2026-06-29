"""AI-generated "Previously, on…" recap for resuming play."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openadventure.providers.base import Message, SystemBlock, TextBlock, Usage
from openadventure.store import canon, snapshots
from openadventure.store.eventlog import LogEntry

if TYPE_CHECKING:
    from openadventure.engine.session import GameSession

# How many of the most recent narrative beats to feed the recap model. The
# model condenses these; the cap keeps the prompt bounded on long campaigns.
RECENT_TRANSCRIPT_LIMIT = 40

# Verbosity drives both the length guidance handed to the model and the output
# token ceiling, so a "low" recap can't quietly cost as much as a "high" one.
_RECAP_LENGTH = {
    "low": "Just a sentence or two covering only the essentials.",
    "medium": "A single tight paragraph.",
    "high": "One or two flowing paragraphs.",
}
_RECAP_MAX_TOKENS = {"low": 250, "medium": 450, "high": 850}


def _open_canon_lines(session: GameSession, *, include_secret: bool) -> str | None:
    """The open canon (threads, setups, standing facts) for the resume recap.
    Hidden entries appear only in assistant mode (the human GM is behind the
    screen); in gm mode this recap is for players, so they are withheld."""
    rendered, _dropped = canon.render_open(
        canon.load(session.campaign), include_hidden=include_secret
    )
    return rendered or None


def _recent_transcript(entries: list[LogEntry], *, limit: int) -> list[str]:
    """Full-text recent narrative beats, oldest to newest, for the recap model.

    Unlike the display recap these are not clipped; the model needs the actual
    prose to condense. Pure mechanics (dice rolls) are dropped; what's left is
    the story as the table experienced it.
    """
    lines: list[str] = []
    for entry in entries:
        data = entry.data
        if entry.type == "user_message":
            text = str(data.get("text", "")).strip()
            if text:
                lines.append(f"Player: {text}")
        elif entry.type == "gm_message":
            text = str(data.get("text", "")).strip()
            if text:
                lines.append(f"GM: {text}")
        elif entry.type == "state_change":
            summary = str(data.get("summary", "")).strip()
            if summary:
                lines.append(f"[event] {summary}")
    return lines[-limit:]


def _recap_system(mode: str, verbosity: str) -> str:
    length = _RECAP_LENGTH.get(verbosity, _RECAP_LENGTH["medium"])
    parts = [
        'You are the narrator of a tabletop RPG campaign, delivering a spoken "Previously, '
        'on…" recap to the players as they sit back down to continue the adventure.',
        "Focus on the MOST RECENT developments: what just happened, where the party now "
        "stands, and the unresolved threads or cliffhanger they are about to step back into. "
        "Touch older history only lightly, for continuity.",
        'Write flowing narration prose in the second person, addressing the party as "you". '
        "No headings, no bullet points, no markdown, no dice, rules, stats, or "
        "out-of-character meta. This text will be read aloud.",
        f"Length: {length}",
    ]
    if mode == "gm":
        parts.append(
            "This recap is for the players. Never reveal secrets, hidden plot, GM notes, or "
            "anything the party has not yet discovered in the fiction."
        )
    else:
        parts.append(
            "This recap is for the game master, behind the screen. You may reference secrets "
            "and hidden developments that the GM knows."
        )
    return "\n\n".join(parts)


def _recap_source(
    session: GameSession,
    *,
    story_so_far: str | None,
    open_canon: str | None,
    transcript: list[str],
    scene: dict[str, Any] | None,
) -> str:
    parts = [f"Campaign: {session.meta.name}"]
    roster = session.party_roster()
    if roster:
        parts.append(f"Party:\n{roster}")
    if scene:
        situ = ", ".join(str(scene[key]) for key in ("location", "time") if scene.get(key))
        if situ:
            parts.append(f"Where things stand now: {situ}")
    if story_so_far:
        parts.append(
            "Story so far (older background, summarize lightly, do not dwell):\n" + story_so_far
        )
    else:
        parts.append("Story so far: this is still early in the campaign.")
    if open_canon:
        parts.append("Unresolved threads and setups (what is still open):\n" + open_canon)
    parts.append(
        "Most recent events (oldest to newest; THIS is what your recap should focus on):\n"
        + "\n".join(transcript)
    )
    return "\n\n".join(parts)


async def _complete(
    session: GameSession, *, system_text: str, user_text: str, max_tokens: int
) -> tuple[str, Usage]:
    """One-shot, tool-free completion using the campaign's configured model."""
    settings = session.settings.merged({"thinking": False, "max_tokens": max_tokens})
    pieces: list[str] = []
    usage = Usage()
    async for event in session.provider.stream_turn(
        system=[SystemBlock(text=system_text)],
        messages=[Message(role="user", content=[TextBlock(text=user_text)])],
        tools=[],
        settings=settings,
    ):
        if event.type == "text_delta":
            pieces.append(event.text)
        elif event.type == "turn_done":
            usage = event.usage
    return "".join(pieces).strip(), usage


async def generate_recap(session: GameSession) -> str | None:
    """AI "Previously, on…" recap focused on recent play, sized to verbosity.

    Returns the recap text, or None when it can't be generated (no provider, or
    nothing has happened yet) so the caller can fall back to the data summary.
    Accrues token usage; narration and display are the caller's job.
    """
    if session.provider is None:
        return None
    transcript = _recent_transcript(session.log.read_all(), limit=RECENT_TRANSCRIPT_LIMIT)
    if not transcript:
        return None

    summary_data = snapshots.load_json(session.campaign.summary_path) or {}
    scene = snapshots.load_json(session.campaign.scene_path)
    verbosity = session.settings.verbosity.value

    text, usage = await _complete(
        session,
        system_text=_recap_system(session.meta.mode, verbosity),
        user_text=_recap_source(
            session,
            story_so_far=summary_data.get("summary_md"),
            open_canon=_open_canon_lines(session, include_secret=session.meta.mode == "assistant"),
            transcript=transcript,
            scene=scene,
        ),
        max_tokens=_RECAP_MAX_TOKENS.get(verbosity, 850),
    )
    session.accrue_usage(usage)
    return text or None
