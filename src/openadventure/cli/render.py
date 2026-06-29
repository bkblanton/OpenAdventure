"""Rich renderer: EngineEvent stream -> live terminal output."""

from __future__ import annotations

import json
import random
from collections.abc import AsyncIterator

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from openadventure.engine.events import EngineEvent

# The glyph vocabulary for "special" (non-narration) lines. Every such line is
# left-aligned and starts with one of these followed by a single space; the GM's
# own narration carries no glyph and no indent. Keeping them in one place keeps
# the prefixes consistent across the event stream.
GLYPH_TOOL = "⚙"
GLYPH_OK = "✓"
GLYPH_FAIL = "✗"
GLYPH_DICE = "🎲"
GLYPH_MODULE = "📖"
GLYPH_IMAGE = "🖼"
GLYPH_MUSIC = "🎵"
GLYPH_DEBUG = "💭"
GLYPH_COMPACT = "⋯"

# Faint reference printed after a tool line. The number is the handle for
# `/debug show <n>`; it's styled distinctly (and dimmer) and wrapped in ⟨⟩ so
# it reads as a meta annotation rather than part of the tool's result.
HANDLE_STYLE = "grey42 italic"


def _handle(num: int) -> str:
    return f"⟨{num}⟩"


# The "thinking" spinner stands in for real work happening behind the screen,
# tool calls the player can't see. Rather than a flat "The GM is thinking…", it
# narrates that work without naming the tool or its args (in hidden mode those
# can spoil which rule, which scene, what the dice said). A turn opens on a
# random one of these, and each tool that runs swaps in another at random, so
# the spinner keeps changing as the turn goes on.
DM_PHRASES = [
    "The GM is thinking…",
    "The GM consults the ancient tomes…",
    "The GM rolls behind the screen…",
    "The GM thumbs through the lore…",
    "The GM plots the next twist…",
    "The GM weighs your fate…",
    "The GM confers with the gods of the realm…",
    "The GM peers into the fog of war…",
    "The GM works behind the screen…",
    "The GM checks their notes…",
    "The GM flips through the books…",
]

# Cycled while the canon chronicler compacts the story. Flavor only: the real
# reasoning may touch GM-only canon, so we never show it (see CompactionProgress).
CHRONICLER_PHRASES = [
    "The chronicler sifts the session…",
    "The chronicler files away what happened…",
    "The chronicler updates the canon…",
    "The chronicler cross-references old threads…",
    "The chronicler tidies the open mysteries…",
    "The chronicler inks the story so far…",
    "The chronicler shelves the settled plots…",
    "The chronicler weighs what to remember…",
]


class _TurnView:
    """Ordered segments of one turn: markdown text interleaved with tool lines."""

    def __init__(self) -> None:
        self.segments: list[
            tuple[str, object]
        ] = []  # ("md", str) | ("line", Text) | ("panel", Panel)
        self._tool_lines: dict[str, int] = {}  # call_id -> segment index
        # A "thinking" spinner shown at the bottom of the turn while the engine
        # is working but producing no visible narration (e.g. running tools).
        self.spinner_active = False
        self.spinner = Spinner("dots", text=Text("Thinking…", style="dim"))
        self.thinking_label = "Thinking…"  # spinner text to restore after a labelled task
        self.template_tasks: set[str] = set()  # task_ids of in-flight template derivations

    def add_text(self, delta: str) -> None:
        if self.segments and self.segments[-1][0] == "md":
            self.segments[-1] = ("md", self.segments[-1][1] + delta)
        else:
            self.segments.append(("md", delta))

    def add_tool_started(self, call_id: str, name: str) -> None:
        line = Text(f"{GLYPH_TOOL} {name} …", style="dim")
        self._tool_lines[call_id] = len(self.segments)
        self.segments.append(("line", line))

    def finish_tool(
        self, call_id: str, name: str, args: str, result: str, ok: bool, handle: str = ""
    ) -> None:
        glyph = GLYPH_OK if ok else GLYPH_FAIL
        style = "dim" if ok else "dim red"
        # Build with spans so the handle keeps its own muted style instead of
        # inheriting the line's; it should recede, not look like result data.
        line = Text(f"{GLYPH_TOOL} {name}({args}) {glyph} {result}", style=style)
        if handle:
            line.append(f"  {handle}", style=HANDLE_STYLE)
        idx = self._tool_lines.get(call_id)
        if idx is None:
            self.segments.append(("line", line))
        else:
            self.segments[idx] = ("line", line)

    def add_line(self, text: str, style: str = "") -> None:
        self.segments.append(("line", Text(text, style=style)))

    def add_text_line(self, text: Text) -> None:
        self.segments.append(("line", text))

    def add_panel(self, panel: Panel) -> None:
        self.segments.append(("panel", panel))

    def renderable(self) -> Group:
        parts = []
        for kind, payload in self.segments:
            parts.append(Markdown(payload) if kind == "md" else payload)
        if self.spinner_active:
            parts.append(self.spinner)
        return Group(*parts)


class EventRenderer:
    def __init__(self, console: Console, *, debug: bool = False, hide_private: bool = True):
        self.console = console
        self.debug = debug
        self.hide_private = hide_private
        # Collapsed debug boxes: each tool call's full args/result, expandable
        # on demand with `/debug show <n>`. (name, args_json, result)
        self.debug_calls: list[tuple[str, str, str]] = []

    def _debug_panel(self, index: int) -> Panel:
        name, body, result = self.debug_calls[index - 1]
        return Panel(
            f"[bold]args[/bold]\n{body}\n\n[bold]result[/bold]\n{result}",
            title=f"⟨{index}⟩ {name}",
            border_style="yellow",
            expand=False,
        )

    def show_debug_call(self, index: int | None) -> None:
        """Print a previously-collapsed debug box (the latest if index is None)."""
        if not self.debug_calls:
            self.console.print("[yellow dim]No tool calls recorded yet.[/yellow dim]")
            return
        if index is None:
            index = len(self.debug_calls)
        if not 1 <= index <= len(self.debug_calls):
            self.console.print(
                f"[red]No tool call ⟨{index}⟩ (have ⟨1⟩ to ⟨{len(self.debug_calls)}⟩).[/red]"
            )
            return
        self.console.print(self._debug_panel(index))

    def show_all_debug_calls(self) -> None:
        if not self.debug_calls:
            self.console.print("[yellow dim]No tool calls recorded yet.[/yellow dim]")
            return
        for index in range(1, len(self.debug_calls) + 1):
            self.console.print(self._debug_panel(index))

    def _open_image(self, path: str) -> None:
        """Open a generated image in the OS default viewer. No-op when output is
        not a real terminal (piped/CI/tests): there's no screen to show it on."""
        if not self.console.is_terminal:
            return
        from openadventure.cli.media_host import open_image_file

        open_image_file(path)

    def render_events(self, events: list[EngineEvent]) -> None:
        """Render already-completed events (e.g. background results between turns)."""
        if not events:
            return
        view = _TurnView()
        for event in events:
            self._apply(view, event)
        self.console.print(view.renderable())

    async def render_turn(self, events: AsyncIterator[EngineEvent]) -> None:
        view = _TurnView()
        # A "thinking" spinner stands in for the work happening behind the
        # scenes. Outside debug mode it replaces the hidden tool lines; in debug
        # mode it sits just below the visible tool lines. Live auto-refreshes, so
        # it keeps animating even while a tool runs without emitting events.
        # Pick a fresh opener each turn so the spinner doesn't read the same way
        # twice in a row. In debug mode the real tool lines are visible, so the
        # spinner stays a plain stand-in rather than competing with them.
        label = random.choice(DM_PHRASES) if self.hide_private else "Thinking…"
        view.thinking_label = label
        view.spinner.update(text=Text(label, style="dim"))
        view.spinner_active = True
        with Live(
            view.renderable(),
            console=self.console,
            refresh_per_second=8,
            vertical_overflow="visible",
        ) as live:
            async for event in events:
                self._apply(view, event)
                live.update(view.renderable())
            # Never leave a frozen spinner frame in the final render.
            view.spinner_active = False
            live.update(view.renderable())

    def _apply(self, view: _TurnView, event: EngineEvent) -> None:
        match event.type:
            case "assistant_text_delta":
                view.add_text(event.text)
                # Visible narration replaces the "thinking" spinner.
                view.spinner_active = False
            case "debug_chatter":
                if self.debug:
                    label = event.reason or "debug chatter"
                    view.add_line(f"{GLYPH_DEBUG} [{label}] {event.text}", style="yellow dim")
            case "tool_started":
                view.spinner_active = True  # keep the spinner up while work runs
                if not self.debug:
                    # Tools are hidden; let the spinner say the GM is busy with
                    # a fresh phrase rather than naming the tool.
                    phrase = random.choice(DM_PHRASES)
                    view.spinner.update(text=Text(phrase, style="dim"))
                    return  # hide tool details; the spinner stands in for them
                view.add_tool_started(event.call_id, event.name)
            case "tool_finished":
                secret = event.private and self.hide_private
                num = None
                if not secret:
                    # Stash the full args/result as a collapsed box, expandable
                    # with `/debug show <n>`, captured in both modes so the
                    # numbers stay consistent if debug is toggled mid-session.
                    body = json.dumps(event.args, indent=2, ensure_ascii=False)
                    result = (
                        event.result if len(event.result) <= 2000 else event.result[:2000] + "…"
                    )
                    self.debug_calls.append((event.name, body, result))
                    num = len(self.debug_calls)
                # A finished tool usually means more work is coming (another tool
                # or the narration); keep the spinner up until text arrives.
                view.spinner_active = True
                if self.debug:
                    args_summary = "private" if secret else event.args_summary
                    if secret:
                        result_summary = (
                            "secret roll" if event.name == "roll_dice" else "noted (secret)"
                        )
                    else:
                        result_summary = event.result_summary
                    handle = _handle(num) if num is not None else ""
                    view.finish_tool(
                        event.call_id, event.name, args_summary, result_summary, event.ok, handle
                    )
                else:
                    # Debug off: show the tool name and handle, but never the
                    # args or result, since those can carry spoilers (e.g. the module
                    # names being loaded).
                    if num is not None:
                        line = Text(f"{GLYPH_TOOL} {event.name}", style="dim")
                        line.append(f"  {_handle(num)}", style=HANDLE_STYLE)
                        view.add_text_line(line)
            case "roll_result":
                if event.private and self.hide_private:
                    view.add_line(f"{GLYPH_DICE} (secret roll)", style="magenta dim")
                else:
                    line = Text(f"{GLYPH_DICE} {event.detail}", style="bold cyan")
                    if event.outcome:  # engine-decided verdict from target/success_when
                        line.append(
                            f" → {event.outcome}",
                            style="bold green" if "success" in event.outcome else "bold red",
                        )
                    # natural max/min dice (crits/fumbles, or pool hits/glitches);
                    # neutral since which one is "good" is the game system's to say
                    extremes = []
                    if event.max_rolls:
                        extremes.append(f"max ×{event.max_rolls}")
                    if event.min_rolls:
                        extremes.append(f"min ×{event.min_rolls}")
                    if extremes:
                        line.append(f"  ({', '.join(extremes)})", style="dim")
                    if event.reason:
                        line.append(f" - {event.reason}", style="bold cyan")
                    if event.private:
                        line.append(" [secret]", style="magenta dim")
                    view.add_text_line(line)
            case "state_changed":
                # Downstream-of-tool bookkeeping (scene changed, sheet updated, …).
                # The tool call line already shows the action happened; this is
                # just chatter, so don't render it.
                return
            case "module_transition":
                done = f"Completed module: {event.completed_title}"
                if event.active_title:
                    view.add_line(
                        f"{GLYPH_MODULE} {done} → Now playing: {event.active_title}",
                        style="bold green",
                    )
                else:
                    view.add_line(
                        f"{GLYPH_MODULE} {done}. Campaign arc complete!", style="bold green"
                    )
            case "background_task_started":
                # Template derivation blocks the turn and has no landing event of
                # its own, so it would otherwise sit silently behind the "thinking"
                # spinner for a minute. Announce it and relabel the spinner.
                if event.kind == "template":
                    view.template_tasks.add(event.task_id)
                    view.add_line(
                        f"{GLYPH_MODULE} {event.label} (one-time, ~a minute)…", style="cyan"
                    )
                    view.spinner.update(text=Text("Deriving character template…", style="dim"))
                    view.spinner_active = True
                    return
                # Other kicks ("narrating turn…", "composing music…", "generating
                # image…") are chatter; the result announces itself when it lands
                # (music_started, image_generated/show_image), so stay quiet.
                return
            case "background_task_finished":
                # Template derivation has no landing event, so report it explicitly
                # and restore the normal "thinking" spinner for the rest of the turn.
                if event.task_id in view.template_tasks:
                    if event.ok:
                        view.add_line(f"{GLYPH_OK} Character template ready", style="cyan")
                    else:
                        view.add_line(
                            f"{GLYPH_FAIL} Template derivation failed: {event.message}",
                            style="yellow",
                        )
                    view.spinner.update(text=Text(view.thinking_label, style="dim"))
                    view.spinner_active = True
                    return
                # Other tasks: success is silent (each announces its own result via
                # a dedicated event); only surface failures, which nothing else reports.
                if event.ok:
                    return
                view.add_line(f"{GLYPH_FAIL} {event.message}", style="yellow dim")
            case "image_generated":
                self._open_image(event.path)
                label = event.caption or "image"
                view.add_line(f"{GLYPH_IMAGE} {label}: {event.path}", style="yellow")
            case "show_image":
                self._open_image(event.path)
                label = event.caption or "image"
                view.add_line(f"{GLYPH_IMAGE} Showing {label}: {event.path}", style="yellow")
            case "music_started":
                mood = f" ({event.mood})" if event.mood else ""
                view.add_line(
                    f"{GLYPH_MUSIC} Now playing on loop: {event.track}{mood}", style="yellow"
                )
            case "music_stopped":
                view.add_line(f"{GLYPH_MUSIC} Music stopped", style="yellow dim")
            case "compaction_started":
                view.add_line(f"{GLYPH_COMPACT} Compacting the story so far…", style="dim")
            case "compaction_progress":
                # A heartbeat tick while the chronicler works: cycle a random
                # flavor phrase so the wait shows progress, without surfacing the
                # actual reasoning (it may reference GM-only canon).
                view.spinner_active = True
                view.spinner.update(text=Text(random.choice(CHRONICLER_PHRASES), style="dim"))
            case "compaction_finished":
                view.add_line(f"{GLYPH_COMPACT} Story compacted.", style="dim")
            case "engine_error":
                tips = []
                if event.suggest_retry:
                    tips.append("/retry to try again")
                if event.suggest_model:
                    tips.append("/model to switch to a different model")
                suffix = f". Use {' or '.join(tips)}." if tips else ""
                view.add_line(f"{GLYPH_FAIL} {event.message}{suffix}", style="bold red")
            case "turn_completed":
                if self.debug:
                    u = event.usage
                    view.add_line(
                        f"{GLYPH_DEBUG} prompt≈{event.prompt_tokens_est}tok in={u.input_tokens} "
                        f"out={u.output_tokens} cache_read={u.cache_read_input_tokens} "
                        f"cache_write={u.cache_creation_input_tokens} rounds={event.tool_rounds}",
                        style="dim",
                    )
            case _:
                pass
