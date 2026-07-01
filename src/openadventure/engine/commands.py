"""UI-agnostic slash-command logic.

A command here parses its text argument, drives :class:`GameSession`, and returns
a :class:`CommandResult`: severity-tagged messages (no Rich markup) plus an
optional typed payload for richer display. Every frontend renders the result its
own way (the Rich console today; Discord/web later), so the parsing and
orchestration live once.

Out of scope by design: media (`/tts`, `/music`, `/sfx`, `/images`,
`/narration`), `/setup`, and `/ingest`/`/import`. Those need interactive API-key
prompts, local audio/image playback, and progress UI that is inherently
frontend-specific, so each frontend owns them and calls the relevant
``GameSession`` methods directly.

Display-only views (`/model` with no args, `/sources` show, `/modules` list)
return a typed payload; the frontend renders the table. Interactive follow-ups a
command can't do headless (connecting a provider after `/model` switches its
backend) are signalled back in the payload (``ModelChanged``) for the frontend
to handle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openadventure.engine.session import GameSession
    from openadventure.providers.base import GenerationSettings, ModelInfo


class Severity(StrEnum):
    info = "info"
    success = "success"
    warning = "warning"
    error = "error"


@dataclass
class Message:
    severity: Severity
    text: str


# --- display payloads (frontend renders these) ------------------------------


@dataclass
class ModelList:
    """`/model` with no args: the registry plus the active id, for the frontend
    to render as a list."""

    models: list[ModelInfo]
    current: str


@dataclass
class ModelChanged:
    """`/model <id>` succeeded. ``backend`` is the model's provider; ``switched``
    is True when it differs from the previous model's backend; ``needs_provider``
    is True when the frontend should (re)connect a provider (possibly prompting
    for an API key) because the backend changed or none is connected."""

    backend: str
    switched: bool
    needs_provider: bool


@dataclass
class SourcesView:
    """`/sources` show: what's attached, which is the system source, and every
    book in the store, for the frontend to render as a table."""

    attached: list[str]
    system: str | None
    available: list[str]


@dataclass
class ModuleRow:
    order: int
    title: str
    slug: str
    status: str  # completed | active | pending
    role: str | None


@dataclass
class ModulesView:
    """`/modules` list: the arc blurb and every attached module in order."""

    arc: str | None
    modules: list[ModuleRow]


@dataclass
class CommandResult:
    """Messages to show plus an optional typed payload (one of the ``*List`` /
    ``*Changed`` / ``*View`` dataclasses) for richer rendering."""

    messages: list[Message] = field(default_factory=list)
    data: object | None = None

    def add(self, severity: Severity, text: str) -> CommandResult:
        self.messages.append(Message(severity, text))
        return self

    def info(self, text: str) -> CommandResult:
        return self.add(Severity.info, text)

    def ok(self, text: str) -> CommandResult:
        return self.add(Severity.success, text)

    def warn(self, text: str) -> CommandResult:
        return self.add(Severity.warning, text)

    def error(self, text: str) -> CommandResult:
        return self.add(Severity.error, text)


def info(text: str, data: object | None = None) -> CommandResult:
    return CommandResult([Message(Severity.info, text)], data)


def ok(text: str, data: object | None = None) -> CommandResult:
    return CommandResult([Message(Severity.success, text)], data)


def warn(text: str) -> CommandResult:
    return CommandResult([Message(Severity.warning, text)])


def err(text: str) -> CommandResult:
    return CommandResult([Message(Severity.error, text)])


# --- generation settings ----------------------------------------------------


def parse_context_size(text: str) -> int:
    """Parse a context budget like ``200k`` or ``1m`` into a token count."""
    text = text.strip().lower().replace(",", "")
    if text.endswith("m"):
        return int(float(text[:-1]) * 1_000_000)
    if text.endswith("k"):
        return int(float(text[:-1]) * 1_000)
    return int(text)


def _settings_summary(settings: GenerationSettings) -> str:
    return (
        f"OK: model={settings.model} effort={settings.effort} "
        f"verbosity={settings.verbosity} thinking={settings.thinking} "
        f"context={settings.context_budget:,} "
        "(applies from the next turn; saved to the campaign)"
    )


def _apply_setting(
    session: GameSession, key: str, value: object, usage_hint: str = ""
) -> CommandResult:
    try:
        settings = session.set_override(key, value)
    except ValueError as exc:
        return err(f"{exc} {usage_hint}".rstrip())
    return ok(_settings_summary(settings))


def cmd_effort(session: GameSession, args: str) -> CommandResult:
    if not args.strip():
        return info(
            f"Current effort: {session.settings.effort}. Set with /effort low|medium|high|max"
        )
    return _apply_setting(session, "effort", args.strip(), "Usage: /effort low|medium|high|max")


def cmd_thinking(session: GameSession, args: str) -> CommandResult:
    arg = args.strip().lower()
    if arg not in ("on", "off"):
        current = "on" if session.settings.thinking else "off"
        return info(
            f"Thinking is {current}. Turning it on deepens the GM's reasoning but makes "
            "each turn slower; it trades time for depth. Set with /thinking on|off"
        )
    return _apply_setting(session, "thinking", arg == "on", "Usage: /thinking on|off")


def cmd_verbosity(session: GameSession, args: str) -> CommandResult:
    if not args.strip():
        return info(
            f"Current verbosity: {session.settings.verbosity.value}. "
            "Set with /verbosity low|medium|high"
        )
    return _apply_setting(session, "verbosity", args.strip(), "Usage: /verbosity low|medium|high")


def cmd_context(session: GameSession, args: str) -> CommandResult:
    if not args.strip():
        return info("Usage: /context <tokens>, e.g. /context 200k or /context 1m")
    try:
        size = parse_context_size(args)
    except ValueError:
        return err("Couldn't parse that size; try 80000, 200k, or 1m.")
    return _apply_setting(session, "context_budget", size)


def cmd_model(session: GameSession, args: str) -> CommandResult:
    if not args.strip():
        # Only advertise non-deprecated models; a deprecated current id still shows
        # via the renderer's "current not in list" fallback.
        return CommandResult(
            data=ModelList(models=session.models.visible, current=session.settings.model)
        )
    before = session.provider_name()
    try:
        settings = session.set_override("model", args.strip())
    except ValueError as exc:
        return err(str(exc))
    after = session.provider_name()
    result = ok(_settings_summary(settings))
    result.data = ModelChanged(
        backend=after,
        switched=after != before,
        needs_provider=after != before or session.provider is None,
    )
    return result


# --- campaign knobs ---------------------------------------------------------


def cmd_mode(session: GameSession, args: str) -> CommandResult:
    arg = "gm" if args.strip() == "dm" else args.strip()  # legacy alias for the renamed mode
    if arg not in ("gm", "assistant"):
        return info(
            f"Current mode: {session.meta.mode}. "
            "Usage: /mode gm (AI runs the game) or /mode assistant (you are the GM)."
        )
    session.set_mode(arg)
    label = (
        "the AI runs the campaign for you"
        if arg == "gm"
        else "you are the GM; the AI is your co-GM and bookkeeper"
    )
    return ok(f"Mode set to {arg}: {label}.")


def cmd_premise(session: GameSession, args: str) -> CommandResult:
    raw = args.strip()
    lower = raw.lower()
    if not raw or lower in ("show", "status"):
        current = session.meta.premise
        if current:
            return info(f"Premise\n{current}")
        return info(
            "No premise set. Seed the campaign with /premise <text>, e.g. "
            "/premise a heist in a drowned elven city. Clear it with /premise clear."
        )
    if lower in ("clear", "reset", "none", "off", "remove"):
        session.set_premise(None)
        return warn("Premise cleared.")
    value = session.set_premise(raw)
    return ok(f"Premise saved (applies from the next turn; saved to the campaign):\n{value}")


def cmd_instructions(session: GameSession, args: str) -> CommandResult:
    raw = args.strip()
    lower = raw.lower()
    if not raw or lower in ("show", "status"):
        current = session.custom_instructions()
        if current:
            return info(f"Custom GM instructions\n{current}")
        return info(
            "No custom instructions set. Shape the GM's personality and style with "
            "/instructions <text>, e.g. /instructions be a forgiving, sandbox-style GM who "
            "never railroads. Clear them with /instructions clear."
        )
    if lower in ("clear", "reset", "none", "off", "remove"):
        session.set_custom_instructions(None)
        return warn("Custom GM instructions cleared.")
    value = session.set_custom_instructions(raw)
    return ok(
        "Custom GM instructions saved (applies from the next turn; saved to the "
        f"campaign):\n{value}"
    )


# --- timeline ---------------------------------------------------------------


def cmd_undo(session: GameSession, args: str) -> CommandResult:
    from openadventure.engine.timeline import TimelineError

    n = int(args.strip()) if args.strip().isdigit() else 1
    try:
        report = session.undo(n)
    except TimelineError as exc:
        return err(str(exc))
    result = CommandResult()
    for text in report.undone_texts:
        shown = text if len(text) <= 60 else text[:57] + "…"
        result.info(f"↶ took back: {shown}")
    if report.turns_undone < n:
        result.warn(
            f"Only {report.turns_undone} turn(s) could be undone "
            "(older checkpoints have been pruned)."
        )
    archive = report.archive.name if report.archive else "the archive"
    result.ok(
        f"Undone. The story is back to where it was before that turn (archived to {archive})."
    )
    return result


def cmd_restart(session: GameSession, args: str) -> CommandResult:
    parts = args.split()
    mode = parts[0] if parts else ""
    confirmed = len(parts) > 1 and parts[1] == "confirm"
    if mode not in ("original", "reroll"):
        return info(
            "Start the campaign over from the beginning. Two flavors:\n"
            "  /restart original: restore each character to its freshly-created sheet "
            "(undo every level, wound, and bit of loot)\n"
            "  /restart reroll:   clear the party so you can roll brand-new characters\n"
            "The old story is archived, never deleted. Nothing happens until you confirm."
        )
    if not confirmed:
        what = (
            "characters go back to their as-created sheets"
            if mode == "original"
            else "the party is cleared so you can roll new characters"
        )
        return info(
            f"This will archive the whole story so far and start fresh; {what}.\n"
            f"To proceed, type: /restart {mode} confirm"
        )
    report = session.restart(characters=mode)  # type: ignore[arg-type]
    result = CommandResult()
    if report.missing_originals:
        result.warn(
            f"No original sheet for: {', '.join(report.missing_originals)} "
            "(created before restart support); rested to full instead."
        )
    if report.rerolled:
        result.ok(
            f"Campaign restarted. Cleared {len(report.rerolled)} "
            f"character(s) ({', '.join(report.rerolled)}). "
            f"Old story archived to {report.archive_dir}. Roll a new party to begin anew!"
        )
        return result
    party = ", ".join(report.pcs) if report.pcs else "no characters yet"
    result.ok(
        f"Campaign restarted. Party: {party}. "
        f"Old story archived to {report.archive_dir}. Say something to begin anew!"
    )
    return result


# --- sources ----------------------------------------------------------------


def _template_note(session: GameSession) -> Message | None:
    """An info note when the system source has no character template yet."""
    source = session.meta.system_source
    if not source or session.has_character_template():
        return None
    return Message(
        Severity.info,
        f"No character-sheet template for {source} yet (optional). "
        f"Generate one with: openadventure template {source}.",
    )


def cmd_sources(session: GameSession, args: str) -> CommandResult:
    """Attach/detach the ingested books this campaign can search. With no args
    (or ``show``) returns a SourcesView; otherwise mutates and reports."""
    from openadventure.store.workspace import slugify

    # Only rules/reference books (and untyped, grandfathered books) can attach as
    # sources; adventure modules are filtered out so they can't be matched here.
    available = session.workspace.list_books("source")
    raw = args.strip()
    verb, _, rest = raw.partition(" ")
    verb, rest = verb.lower(), rest.strip()

    def match_one(name: str) -> str | None:
        lowered = name.strip().lower()
        hits = [s for s in available if s == lowered] or [
            s for s in available if s.startswith(lowered)
        ]
        return hits[0] if len(hits) == 1 else None

    if not raw or verb in ("show", "status", "list"):
        return CommandResult(
            data=SourcesView(
                attached=list(session.meta.sources),
                system=session.meta.system_source,
                available=available,
            )
        )

    if verb in ("none", "clear", "off"):
        session.clear_sources()
        return warn("Sources cleared; the GM will improvise rules.")

    if not available:
        return err(
            "No sources ingested yet. Add one with /ingest <book.pdf> --source "
            "(or openadventure ingest)."
        )

    if verb in ("add", "remove", "drop", "system"):
        if not rest:
            return err(f"/sources {verb} needs a source name.")
        if verb in ("remove", "drop"):
            if session.remove_source(rest):
                return warn(f"Detached {slugify(rest)}.")
            return err(f"{rest!r} isn't attached.")
        chosen = match_one(rest)
        if chosen is None:
            return err(f"No single source matches {rest!r}. Available: {', '.join(available)}.")
        if verb == "system":
            session.set_system_source(chosen)
            result = ok(f"System source set to {chosen}.")
        else:
            session.add_source(chosen)
            result = ok(f"Attached {chosen} (the GM can search it with search_rules).")
        note = _template_note(session)
        if note:
            result.messages.append(note)
        return result

    # bare comma-separated list: replace all sources, first = system
    chosen_list: list[str] = []
    unmatched: list[str] = []
    for token in raw.split(","):
        name = token.strip()
        if not name:
            continue
        hit = match_one(name)
        if hit and hit not in chosen_list:
            chosen_list.append(hit)
        elif not hit:
            unmatched.append(name)
    if not chosen_list:
        return err(f"No source matches {raw!r}. Available: {', '.join(available)}.")
    session.set_sources(chosen_list)
    result = ok(
        f"Sources set to {', '.join(chosen_list)} "
        f"(system: {session.meta.system_source}; the GM can search them with search_rules)."
    )
    if unmatched:
        result.warn(f"No single match for: {', '.join(unmatched)}; skipped.")
    note = _template_note(session)
    if note:
        result.messages.append(note)
    return result


# --- modules ----------------------------------------------------------------


def cmd_modules(session: GameSession, args: str) -> CommandResult:
    """Manage the campaign's adventure modules. With no args (or ``list``)
    returns a ModulesView; otherwise mutates and reports."""
    from openadventure.store.workspace import slugify

    meta = session.meta
    if session.campaign.sync_modules(meta, set(session.workspace.list_books())):
        session.campaign.save_meta(meta)
    verb, _, rest = args.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()

    if verb == "add" and rest:
        # Only adventure modules (and untyped, grandfathered books) can attach
        # here; rules/reference sources are filtered out so they can't match.
        available = set(session.workspace.list_books("module"))
        lowered = rest.lower()
        matches = [s for s in available if s == lowered] or [
            s for s in available if s.startswith(lowered)
        ]
        if len(matches) != 1:
            return err(
                f"No single ingested adventure module matches {rest!r}. "
                f"Available: {', '.join(sorted(available)) or '(none)'}."
            )
        slug = session.add_module(matches[0])
        return ok(f"Attached module {slug} (the GM can search it with search_campaign).")

    if verb in ("remove", "drop") and rest:
        if session.remove_module(rest):
            return warn(f"Detached module {slugify(rest)}.")
        return err(f"Module {rest!r} isn't attached.")

    if verb in ("activate", "switch", "goto") and rest:
        target = next((m for m in meta.modules if m.slug == rest), None)
        if target is None:
            known = ", ".join(m.slug for m in meta.modules) or "(none)"
            return err(f"No module named {rest!r}. Known: {known}.")
        session.set_active_module(target.slug)
        return ok(f"Now playing {target.title}.")

    if verb == "arc":
        arc = session.set_arc(rest)
        return ok("Arc updated." if arc else "Arc cleared.")

    if verb and verb not in ("list", "show"):
        return err(
            "Usage: /modules (list) | /modules add <name> | /modules remove <slug> "
            "| /modules activate <slug> | /modules arc <text>"
        )

    if not meta.modules:
        available = ", ".join(sorted(session.workspace.list_books("module"))) or "(none ingested)"
        return info(
            "No modules attached. Attach an ingested adventure with /modules add <name>. "
            f"Available: {available}; ingest more with /ingest <file> --module."
        )
    rows = [
        ModuleRow(order=m.order, title=m.title, slug=m.slug, status=m.status, role=m.role or None)
        for m in meta.modules
    ]
    return CommandResult(data=ModulesView(arc=meta.arc, modules=rows))


# --- registry (for frontends that dispatch by name) -------------------------

# Maps a slash-command name (without the leading "/") to its handler. The Rich
# CLI calls the handlers directly from its own command methods; a headless
# frontend can dispatch through ``run``. Media/setup/ingest are intentionally
# absent (frontend-owned; see the module docstring).
COMMANDS: dict[str, Callable[[GameSession, str], CommandResult]] = {
    "model": cmd_model,
    "effort": cmd_effort,
    "thinking": cmd_thinking,
    "verbosity": cmd_verbosity,
    "context": cmd_context,
    "mode": cmd_mode,
    "premise": cmd_premise,
    "instructions": cmd_instructions,
    "sources": cmd_sources,
    "modules": cmd_modules,
    "undo": cmd_undo,
    "restart": cmd_restart,
}


def run(session: GameSession, name: str, args: str) -> CommandResult | None:
    """Dispatch ``name`` (with or without a leading slash) through the registry,
    or return None when it isn't a UI-agnostic command."""
    handler = COMMANDS.get(name.lstrip("/"))
    return handler(session, args) if handler else None
