"""Interactive play REPL (prompt_toolkit input, Rich output)."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from openadventure.character_import import IMPORT_MAX_CHARS, prepare_character_import
from openadventure.cli.firstrun import ensure_elevenlabs_api_key, ensure_google_api_key
from openadventure.cli.render import EventRenderer
from openadventure.engine import commands
from openadventure.engine.kickoff import CAMPAIGN_KICKOFF_INSTRUCTION
from openadventure.engine.session import GameSession
from openadventure.mechanics import dice
from openadventure.providers.base import ProviderError
from openadventure.store.eventlog import LogEntry


@dataclass
class SlashCommand:
    name: str
    help: str
    handler: Callable[[Repl, str], Awaitable[None]]
    aliases: tuple[str, ...] = ()


class SlashCompleter(Completer):
    def __init__(self, commands: dict[str, SlashCommand]):
        self.commands = commands

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for name in self.commands:
            if name.startswith(text):
                yield Completion(name, start_position=-len(text))


# Out-of-character framing for /btw, a quick "by the way" aside to the GM. The
# turn runs but isn't logged, so the GM is told to answer directly and not fold
# the exchange into the unfolding story, while still looking things up so the
# answer is grounded, not guessed.
_BTW_INSTRUCTION = (
    '[OUT-OF-CHARACTER aside from the player, a quick "by the way" note or '
    "question for you, the GM, outside the fiction. Answer or acknowledge it "
    "directly and concisely. Still look things up rather than guessing: when the "
    "answer depends on the rules, the module, your notes, or a character sheet, "
    "use your read-only tools to check first, exactly as you would in normal play"
    '; "concisely" governs the length of your reply, not whether you do the '
    "lookup. Do not advance the scene, narrate it as something that happens in the "
    "story, or treat it as a character's action. This exchange is off the record "
    "and is not saved to the campaign log.\n\n{text}]"
)

# One-time, out-of-character kickoff handed to the GM when a campaign that has an
# AI provider holds no characters yet, whether a brand-new campaign or one whose
# party was cleared by /restart reroll. It opens the table: share the premise (drawing
# on the module when one is attached), then ask how the players want to make
# characters. Driven as a normal logged turn so the opening persists and the next
# launch resumes with a recap instead of opening a second time.
_CAMPAIGN_KICKOFF_INSTRUCTION = CAMPAIGN_KICKOFF_INSTRUCTION


class Repl:
    def __init__(self, console: Console, session: GameSession, *, debug: bool = False):
        self.console = console
        self.session = session
        self.renderer = EventRenderer(console, debug=debug, hide_private=session.meta.mode == "gm")
        self.commands = self._build_commands()
        self.running = True
        # The in-flight AI turn (thinking/narrating), so a Ctrl+C can cancel it.
        self._current_turn: asyncio.Task | None = None

    # ------------------------------------------------------------------
    def _build_commands(self) -> dict[str, SlashCommand]:
        commands: dict[str, SlashCommand] = {}
        for spec in _command_specs():
            name, help_text, handler = spec[0], spec[1], spec[2]
            aliases = spec[3] if len(spec) > 3 else ()
            command = SlashCommand(name, help_text, handler, aliases)
            commands[name] = command
            for alias in aliases:
                commands[alias] = command
        return commands

    # ------------------------------------------------------------------
    async def run(self) -> None:
        meta = self.session.meta
        settings = self.session.settings
        ai = settings.model if self.session.provider else "[red]not connected (no API key)[/red]"
        extras = []
        if meta.sources:
            extras.append(f"sources {', '.join(meta.sources)}")
        modules = self.session.meta.modules
        if modules:
            active = self.session.meta.active_module
            names = ", ".join(
                f"[bold]{m.slug}[/bold]" if m.slug == active else m.slug for m in modules
            )
            label = "modules" if len(modules) > 1 else "module"
            extras.append(f"{label} {names}")
        extra_text = f", {', '.join(extras)}" if extras else ""
        self.console.print(
            f"[bold]openadventure[/bold]: campaign [cyan]{meta.name}[/cyan] "
            f"([dim]{meta.mode} mode, model {ai}{extra_text}[/dim]). "
            "Type [green]/help[/green] for commands, [green]Ctrl+D[/green] to quit."
        )
        if self.session.has_prior_play:
            await self._show_recap()
            self.console.print("[dim]Run [green]/recap[/green] anytime to see this again.[/dim]")
            resumed = self.session.resume_music()
            if resumed is not None:
                self.console.print("[dim]🎵 Resuming the last music track.[/dim]")
        elif sys.stdin.isatty() and self._wants_campaign_kickoff():
            # A fresh campaign with no characters: let the GM open the table itself
            # (share the premise, ask how to make characters) instead of staring at
            # an empty prompt. Interactive only; scripted play drives its own input.
            await self._open_campaign()
        prompt: PromptSession[str] | None
        try:
            prompt = PromptSession(
                history=FileHistory(str(self.session.workspace.history_path)),
                completer=SlashCompleter(self.commands),
            )
        except Exception:
            prompt = None  # no real console (piped stdin/CI): plain line input
        try:
            while self.running:
                self.renderer.render_events(self.session.background.drain())
                try:
                    text = (await self._read_line_with_background(prompt)).strip()
                except KeyboardInterrupt:
                    # Ctrl+C surfaced as a key (raw-mode console / non-Windows):
                    # stop any narration still playing, otherwise clear the line.
                    if self.session.interrupt_narration():
                        self.console.print("[yellow](narration stopped)[/yellow]")
                    continue
                except EOFError:
                    break  # Ctrl+D / Ctrl+Z: exit
                self.renderer.render_events(self.session.background.drain())
                if not text:
                    continue
                if text.startswith("/"):
                    await self._dispatch(text)
                else:
                    # Sending a new message cuts off any narration still playing
                    # from the previous turn; the player has moved on.
                    self.session.interrupt_narration()
                    await self.handle_player_input(text)
        finally:
            self.session.close()
            self.console.print("[dim]Campaign saved. Farewell, adventurer.[/dim]")

    def interrupt(self) -> None:
        """Interrupt the current activity, invoked by the runner on Ctrl+C.

        Cancels the in-flight turn (the AI thinking/narrating) if there is one,
        and stops any narration, dialogue, or SFX that is currently playing.
        Music is left alone. The runner resumes the REPL task afterwards, so
        this must not block: ``cancel()`` only flags the turn (it unwinds when
        the loop next runs) and ``interrupt_narration()`` stops the audio
        subprocess synchronously.
        """
        turn = self._current_turn
        if turn is not None and not turn.done():
            turn.cancel()
        self.session.interrupt_narration()

    async def _read_line(self, prompt: PromptSession[str] | None) -> str:
        prompt_text = self._prompt_text()
        if prompt is not None:
            with patch_stdout(raw=True):
                return await prompt.prompt_async(prompt_text)
        line = await asyncio.to_thread(sys.stdin.readline)
        if line == "":
            raise EOFError
        return line.lstrip("\ufeff")  # tolerate a BOM from Windows pipes

    def _prompt_text(self) -> str:
        return "> "

    async def _read_line_with_background(self, prompt: PromptSession[str] | None) -> str:
        read_task = asyncio.ensure_future(self._read_line(prompt))
        try:
            while True:
                done, _pending = await asyncio.wait({read_task}, timeout=0.25)
                self.renderer.render_events(self.session.background.drain())
                if read_task in done:
                    return read_task.result()
        finally:
            if not read_task.done():
                read_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await read_task
            self.renderer.render_events(self.session.background.drain())

    async def _dispatch(self, text: str) -> None:
        name, _, args = text.partition(" ")
        command = self.commands.get(name)
        if command is None:
            self.console.print(f"[red]Unknown command {name}. Try /help.[/red]")
            return
        await command.handler(self, args.strip())

    async def handle_player_input(
        self, text: str, *, steer: bool = False, ephemeral: bool = False, read_only: bool = False
    ) -> None:
        task = asyncio.ensure_future(
            self.renderer.render_turn(
                self.session.handle_input(
                    text,
                    debug=self.renderer.debug,
                    steer=steer,
                    ephemeral=ephemeral,
                    read_only=read_only,
                )
            )
        )
        self._current_turn = task
        try:
            await task
        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            # CancelledError: a Ctrl+C reached the runner, which called interrupt()
            # to cancel this turn. KeyboardInterrupt: the turn itself surfaced one.
            # A CancelledError where the turn wasn't the thing cancelled means we
            # were torn down from outside; let that propagate.
            if isinstance(exc, asyncio.CancelledError) and not task.cancelled():
                raise
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # Cancelling the turn stops the AI stream; also silence any dialogue
            # or SFX it kicked off in the background before being interrupted.
            self.session.interrupt_narration()
            self.console.print("[yellow](turn cancelled)[/yellow]")
        finally:
            self._current_turn = None

    # --- commands -------------------------------------------------------
    # Section headers for /help, paired with the commands shown under each.
    # Mirrors the grouping of `specs` in _build_commands.
    HELP_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("Session", ("/help", "/clear", "/restart", "/quit")),
        ("Play", ("/roll", "/undo", "/retry", "/recap", "/scene", "/sudo", "/btw", "/compact")),
        ("Characters", ("/sheet", "/import", "/party", "/encounter")),
        ("Campaign", ("/mode", "/sources", "/premise", "/instructions", "/modules")),
        ("Rulebooks", ("/ingest", "/template", "/reindex")),
        (
            "AI behavior",
            ("/model", "/effort", "/thinking", "/verbosity", "/context"),
        ),
        ("Audio & video", ("/tts", "/narration", "/voice", "/sfx", "/music", "/images")),
        ("Info", ("/campaigns", "/log", "/usage", "/debug")),
        ("Setup", ("/setup",)),
    )

    async def _cmd_help(self, args: str) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2))
        for index, (header, names) in enumerate(self.HELP_GROUPS):
            if index:
                table.add_row("", "")  # blank line between sections
            table.add_row(f"[bold]{header}[/bold]", "")
            for name in names:
                command = self.commands.get(name)
                if command is None:
                    continue
                label = command.name
                if command.aliases:
                    label += " (" + ", ".join(command.aliases) + ")"
                table.add_row(f"  [green]{label}[/green]", command.help)
        self.console.print(table)

    async def _cmd_clear(self, args: str) -> None:
        """Wipe the visible screen and silence any narration still playing.

        Only the terminal output is cleared; the event log, sheets, and the
        rest of the campaign state are left untouched, so /log, /recap, and
        scrollback-derived data all still show everything.
        """
        cancelled = self.session.interrupt_narration()
        self.console.clear()
        if cancelled:
            self.console.print("[yellow](narration stopped)[/yellow]")

    async def _cmd_roll(self, args: str) -> None:
        if not args:
            self.console.print("[red]Usage: /roll <expression>, e.g. /roll 2d20kh1+5[/red]")
            return
        try:
            outcome = self.session.roll_local(args)
        except dice.DiceError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        self.console.print(f"[bold cyan]🎲 {outcome.detail()}[/bold cyan]")

    async def _cmd_sheet(self, args: str) -> None:
        from openadventure.store.sheetstore import SheetStore

        store = SheetStore(self.session.campaign)
        if not args:
            sheets = store.list()
            if not sheets:
                self.console.print("No sheets yet. Ask the GM to create a character.")
                return
            table = Table("id", "name", "kind", "status", "resources")
            for s in sheets:
                resources = ", ".join(f"{k} {v.current}/{v.max}" for k, v in s.resources.items())
                table.add_row(s.id, s.name, s.kind, s.status, resources)
            self.console.print(table)
            return
        sheet = store.load(args)
        if sheet is None:
            self.console.print(f"[red]No sheet {args!r}. Try /sheet to list.[/red]")
            return
        self._render_sheet(sheet)

    def _render_sheet(self, sheet) -> None:
        title = f"{sheet.name}, {sheet.kind} ({sheet.status})"
        table = Table(title=title, show_header=False, padding=(0, 1))

        def fmt(value):
            if isinstance(value, dict):
                return ", ".join(f"{k}: {fmt(v)}" for k, v in value.items())
            if isinstance(value, list):
                return "; ".join(fmt(v) for v in value)
            return str(value)

        for name, resource in sheet.resources.items():
            table.add_row(f"[bold]{name}[/bold]", f"{resource.current}/{resource.max}")
        if sheet.conditions:
            table.add_row("[bold]conditions[/bold]", ", ".join(sheet.conditions))
        for key, value in sheet.fields.items():
            table.add_row(f"[bold]{key}[/bold]", fmt(value))
        self.console.print(table)

    async def _cmd_import(self, args: str) -> None:
        """Import a character sheet from a Markdown, text, or JSON file.

        Reads the file and drives one AI turn that transcribes it into a sheet
        via create_sheet, following the campaign's character template, so the
        import is system-agnostic, just like sheets the GM creates in play.
        """
        from pathlib import Path

        usage = (
            "Usage: /import <file.md|.txt|.json>. Import a character sheet from a "
            "Markdown, plain-text, or JSON file."
        )
        raw = args.strip()
        if not raw:
            self.console.print(usage)
            return
        # The whole argument is one path (it may contain spaces). Strip a pair of
        # surrounding quotes if present; don't shlex-split, since that mangles the
        # backslashes in Windows paths.
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        source = Path(raw).expanduser()
        if not source.is_file():
            self.console.print(f"[red]No such file: {source}[/red]")
            return
        try:
            content = source.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            self.console.print(f"[red]Couldn't read {source.name}: {exc}[/red]")
            return
        try:
            instruction, truncated = prepare_character_import(source.name, content)
        except ValueError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        if self.session.provider is None:
            self.console.print(
                "[red]Importing a sheet needs an AI provider: set ANTHROPIC_API_KEY "
                "(env or .env) and restart.[/red]"
            )
            return
        if truncated:
            self.console.print(
                f"[yellow]{source.name} is large; importing only the first "
                f"{IMPORT_MAX_CHARS:,} characters.[/yellow]"
            )
        self.console.print(f"[dim]Importing character from [bold]{source.name}[/bold]…[/dim]")
        await self.handle_player_input(instruction)

    async def _cmd_party(self, args: str) -> None:
        roster = self.session.party_roster()
        companions = self.session.companion_roster()
        if roster is None and companions is None:
            self.console.print("No active party members yet.")
            return
        if roster is not None:
            self.console.print(roster)
        if companions is not None:
            self.console.print("\n[bold]Traveling with you:[/bold]")
            self.console.print(companions)

    async def _cmd_scene(self, args: str) -> None:
        summary = self.session.scene_summary()
        if summary is None:
            self.console.print("No scene set yet.")
            return
        self.console.print(summary)

    async def _cmd_encounter(self, args: str) -> None:
        summary = self.session.encounter_summary()
        if summary is None:
            self.console.print("No active encounter.")
            return
        self.console.print(summary)

    async def _cmd_recap(self, args: str) -> None:
        await self._show_recap()

    async def _show_recap(self) -> None:
        """Print the AI 'Previously, on…' recap. Cancellable with Ctrl+C.

        When the only turn so far is the campaign opening, just replay the GM's
        first message verbatim instead of paying for an AI recap. Narration (when
        TTS is on) is kicked off in the background by the session.
        """
        verbatim = self.session.first_gm_message_if_only_turn()
        if verbatim is not None:
            self.console.print(Markdown(verbatim))
            return
        if self.session.provider is None:
            self.console.print(
                "[dim]No model connected, so there's no recap. Connect one with /model.[/dim]"
            )
            return
        # Run as the current turn so the runner's Ctrl+C path (interrupt ->
        # cancel _current_turn) can cancel a slow recap, like any AI turn.
        task = asyncio.ensure_future(self.session.recap())
        self._current_turn = task
        try:
            with self.console.status("[dim]Recalling the story so far… (Ctrl+C to skip)[/dim]"):
                text = await task
        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError) and not task.cancelled():
                raise
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.session.interrupt_narration()
            self.console.print("[yellow](recap skipped)[/yellow]")
            return
        except ProviderError as exc:
            self.console.print(f"[yellow]Couldn't generate a recap ({exc}).[/yellow]")
            return
        finally:
            self._current_turn = None
        self.console.print(Markdown(text) if text else "[dim]Nothing has happened yet.[/dim]")

    def _wants_campaign_kickoff(self) -> bool:
        """True when the GM should open the campaign itself: a provider is
        connected, setup is complete, nothing has been played yet, and there
        are no characters (a brand-new campaign, or one whose party was cleared
        by /restart reroll).

        Setup must be marked done so an interrupted wizard (Ctrl+D) doesn't
        fire the kickoff before the user has finished onboarding."""
        from openadventure.store.sheetstore import SheetStore

        return (
            self.session.provider is not None
            and self.session.meta.setup_done
            and not self.session.has_prior_play
            and not SheetStore(self.session.campaign).party()
        )

    async def _open_campaign(self) -> None:
        """Have the GM open a fresh campaign: share the premise (drawing on the
        attached module when there is one) and ask how the players want to make
        characters. Driven as a normal logged turn, so the opening persists."""
        self.console.print("[dim]Setting the scene…[/dim]")
        await self.handle_player_input(_CAMPAIGN_KICKOFF_INSTRUCTION)

    async def _cmd_campaigns(self, args: str) -> None:
        table = Table("slug", "name", "mode", "sources", "created")
        for m in self.session.workspace.list_campaigns():
            marker = " [cyan]←[/cyan]" if m.slug == self.session.meta.slug else ""
            table.add_row(
                m.slug + marker, m.name, m.mode, ", ".join(m.sources) or "-", m.created_at[:10]
            )
        self.console.print(table)

    # The log entry types that make up the human story: what the table said and
    # saw, oldest to newest. Tool calls, dice mechanics, media cues, and other
    # behind-the-screen bookkeeping are left to `/log --raw`.
    _NARRATIVE_LOG_TYPES = ("user_message", "gm_message", "state_change")

    async def _cmd_log(self, args: str) -> None:
        raw = False
        n = 10
        for token in args.split():
            if token in ("--raw", "--all"):
                raw = True
            elif token.isdigit():
                n = int(token)

        if raw:
            # The full event log, including tool calls and secret rolls: for
            # debugging, not for the table (it can spoil hidden developments).
            for entry in self.session.log.tail(n):
                data = str(entry.data)
                if len(data) > 100:
                    data = data[:100] + "…"
                self.console.print(f"[dim]{entry.seq:>5} {entry.ts} {entry.type:<14}[/dim] {data}")
            return

        beats = [e for e in self.session.log.read_all() if e.type in self._NARRATIVE_LOG_TYPES]
        if not beats:
            self.console.print("[dim]No story beats yet; the adventure hasn't begun.[/dim]")
            return
        for entry in beats[-n:]:
            self._print_log_beat(entry)

    def _print_log_beat(self, entry: LogEntry) -> None:
        """Render one narrative log entry the way it read at the table."""
        text = str(entry.data.get("text", "")).strip()
        if entry.type == "user_message":
            if text:
                self.console.print(f"[bold cyan]You:[/bold cyan] {text}")
        elif entry.type == "gm_message":
            if text:
                self.console.print(Markdown(text))
        elif entry.type == "state_change":
            summary = str(entry.data.get("summary", "")).strip()
            if summary:
                self.console.print(f"[dim]· {summary}[/dim]")

    async def _cmd_usage(self, args: str) -> None:
        report = self.session.usage_report()
        table = Table(
            "scope",
            "input",
            "text",
            "thinking",
            "cache read",
            "cache write",
            "images",
            "audio",
            "est. cost",
        )
        totals = report["totals"]
        session = report["session"]

        def text_tokens(row: dict) -> int:
            # output_tokens is the provider-billed total, including thinking.
            return max(int(row.get("output_tokens", 0)) - int(row.get("thinking_tokens", 0)), 0)

        def audio_usage(row: dict) -> str:
            parts = []
            if chars := row.get("tts_characters", 0):
                parts.append(f"TTS {int(chars):,} ch")
            if seconds := row.get("sound_effect_seconds", 0):
                parts.append(f"SFX {float(seconds):g}s")
            if seconds := row.get("music_seconds", 0):
                parts.append(f"music {float(seconds):g}s")
            return ", ".join(parts) or "â€”"

        table.add_row(
            "campaign",
            f"{totals['input_tokens']:,}",
            f"{text_tokens(totals):,}",
            f"{totals.get('thinking_tokens', 0):,}",
            f"{totals.get('cache_read_input_tokens', 0):,}",
            f"{totals.get('cache_creation_input_tokens', 0):,}",
            f"{totals.get('image_count', 0):,}",
            audio_usage(totals),
            f"${report['cost_usd']:.4f}",
        )
        table.add_row(
            "this session",
            f"{session['input_tokens']:,}",
            f"{text_tokens(session):,}",
            f"{session.get('thinking_tokens', 0):,}",
            f"{session.get('cache_read_input_tokens', 0):,}",
            f"{session.get('cache_creation_input_tokens', 0):,}",
            f"{session.get('image_count', 0):,}",
            audio_usage(session),
            f"${report.get('session_cost_usd', 0.0):.4f}",
        )
        self.console.print(table)
        breakdown = report.get("cost_breakdown", {})
        session_breakdown = report.get("session_cost_breakdown", {})
        if any(
            breakdown.get(key, 0.0) for key in ("text", "images", "tts", "sound_effects", "music")
        ):
            costs = Table("scope", "text", "images", "TTS", "SFX", "music", "total")
            for label, row in (("campaign", breakdown), ("this session", session_breakdown)):
                costs.add_row(
                    label,
                    f"${row.get('text', 0.0):.4f}",
                    f"${row.get('images', 0.0):.4f}",
                    f"${row.get('tts', 0.0):.4f}",
                    f"${row.get('sound_effects', 0.0):.4f}",
                    f"${row.get('music', 0.0):.4f}",
                    f"${row.get('total', 0.0):.4f}",
                )
            self.console.print(costs)
        by_model = report.get("by_model", {})
        if by_model:
            # Per-model breakdown: with the backend chosen by the model, a campaign
            # can span several (e.g. a cheap model for setup, a premium one at the
            # table), so show where the tokens and cost actually went.
            per = Table("model", "input", "text", "thinking", "est. cost", title="by model")
            for model_id, row in sorted(
                by_model.items(), key=lambda kv: kv[1].get("cost_usd", 0.0), reverse=True
            ):
                per.add_row(
                    model_id,
                    f"{row.get('input_tokens', 0):,}",
                    f"{text_tokens(row):,}",
                    f"{row.get('thinking_tokens', 0):,}",
                    f"${row.get('cost_usd', 0.0):.4f}",
                )
            self.console.print(per)
        self.console.print(
            "[dim]Text and thinking are token estimates or provider totals; output includes "
            "thinking. Media and cost use rough built-in list-price estimates. Cache reads bill "
            "at ~10% of input. Actual billing may differ.[/dim]"
        )
        s = self.session.settings
        self.console.print(
            f"[dim]model={s.model} effort={s.effort} verbosity={s.verbosity} "
            f"thinking={s.thinking} "
            f"context_budget={s.context_budget:,} max_tokens={s.max_tokens:,}[/dim]"
        )

    async def _cmd_tts(self, args: str) -> None:
        choice = args.strip().lower() or "status"
        if choice in ("status", "show"):
            self._print_tts_status()
            return
        if choice in ("stop", "interrupt", "cancel"):
            cancelled = self.session.interrupt_narration()
            self.console.print(f"[yellow]Narration interrupted[/yellow] ({cancelled} queued).")
            return
        if choice in ("on", "enable", "enabled", "true", "yes"):
            self._ensure_tts_ready()
            self.session.set_tts_enabled(True)
            self.session.reload_tools()
            if self.session.meta.mode == "assistant":
                self.console.print("[green]TTS voice commands on[/green] (saved to this campaign).")
            else:
                self.console.print("[green]TTS narration on[/green] (saved to this campaign).")
            self._print_tts_status()
            return
        if choice in ("off", "disable", "disabled", "false", "no"):
            self.session.interrupt_narration()
            self.session.set_tts_enabled(False)
            self.session.reload_tools()
            self.console.print("[yellow]TTS narration off[/yellow] (saved to this campaign).")
            return
        self.console.print("Usage: /tts on|off|status|stop")

    async def _cmd_narration(self, args: str) -> None:
        choice = args.strip().split(maxsplit=1)[0].lower() if args.strip() else "status"
        if choice in ("status", "show"):
            self._print_tts_status()
            return
        if choice in ("stop", "interrupt", "cancel"):
            cancelled = self.session.interrupt_narration()
            self.console.print(f"[yellow]Narration interrupted[/yellow] ({cancelled} queued).")
            return
        if choice in ("replay", "repeat", "again"):
            # Stop whatever is playing and re-narrate the last turn from cached
            # audio, so it costs no new API calls.
            started = self.session.replay_narration()
            if started is None:
                self.console.print(
                    "[yellow]Nothing to replay[/yellow] "
                    "(no narration has played yet, or TTS is off)."
                )
            else:
                self.console.print("[dim]Replaying the last narration…[/dim]")
            return
        self.console.print("Usage: /narration stop|status|replay")

    def _cmd_voice_clear(self, args: str) -> None:
        target = args.strip()
        if not target:
            self.console.print("Usage: /voice clear <speaker>|all")
            return
        if target.lower() in ("all", "voices", "cast"):
            count = self.session.narration.clear_voice_cast()
            self.console.print(
                f"[yellow]Cleared {count} remembered narration voice"
                f"{'' if count == 1 else 's'}.[/yellow]"
            )
            return
        removed = self.session.narration.clear_voice(target)
        if removed is None:
            self.console.print(f"[yellow]No remembered voice for {target!r}.[/yellow]")
            return
        self.console.print(
            f"[green]Cleared voice for {removed.speaker}[/green]. "
            "The next line from that speaker will pick a new voice."
        )

    def _cmd_voice_accent(self, args: str) -> None:
        value = args.strip()
        lower = value.lower()
        if not value or lower in ("status", "show"):
            self._print_cast_accent()
            return
        if lower in ("clear", "default", "none", "off"):
            self.session.set_cast_accent(None)
            self.console.print(
                "[yellow]Default cast accent cleared[/yellow]. New NPC voices can use any accent."
            )
            return
        accent = self.session.set_cast_accent(value)
        self.console.print(
            f"[green]Default cast accent set to {accent}[/green]. "
            "New NPC voices will use that accent when available "
            "(the Narrator voice is unaffected)."
        )

    async def _cmd_voice(self, args: str) -> None:
        """The voice hub: set the narrator's voice, and manage the remembered
        cast (show, accent, clear)."""
        raw = args.strip()
        parts = raw.split(maxsplit=1)
        choice = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if not choice or choice in ("status", "show"):
            self._print_voice_status()
            self._print_voice_cast()
            return
        if choice in ("cast", "voices", "list"):
            self._print_voice_cast()
            return
        if choice == "accent":
            self._cmd_voice_accent(rest)
            return
        if choice in ("clear", "reset", "remove", "default", "none"):
            # Bare clear/default resets the narrator's own voice; with a target it
            # forgets a remembered speaker (or 'all' for the whole cast).
            if rest:
                self._cmd_voice_clear(rest)
            else:
                self._reset_narrator_voice()
            return
        if choice == "narrator":
            self._set_narrator_voice(rest)
            return
        # Anything else is taken as the narrator voice id (or its library URL).
        self._set_narrator_voice(raw)

    def _set_narrator_voice(self, raw: str) -> None:
        from openadventure.media.tts import extract_voice_id

        value = raw.strip()
        if not value or value.lower() in ("default", "reset", "clear", "none"):
            self._reset_narrator_voice()
            return
        voice_id = extract_voice_id(value)
        if not voice_id:
            self.console.print(
                "Usage: /voice <id or elevenlabs URL> | cast | accent <a> | clear <speaker>|all"
            )
            return
        self.session.set_narrator_voice_id(voice_id)
        self.session.interrupt_narration()
        self.console.print(
            f"[green]Narrator voice set to {voice_id}[/green] (saved to this campaign)."
        )
        self._print_voice_status()

    def _reset_narrator_voice(self) -> None:
        self.session.set_narrator_voice_id(None)
        self.session.interrupt_narration()
        self.console.print("[yellow]Narrator voice reset to the default.[/yellow]")
        self._print_voice_status()

    def _print_voice_status(self) -> None:
        override = self.session.narrator_voice_id()
        active = getattr(self.session.tts, "voice_id", None)
        if override:
            self.console.print(
                f"[dim]Narrator voice: {override} (custom, saved to this campaign).[/dim]"
            )
        elif active:
            self.console.print(f"[dim]Narrator voice: {active} (default).[/dim]")
        else:
            self.console.print("[dim]Narrator voice: default.[/dim]")

    def _ensure_tts_ready(self) -> None:
        backend = self.session.tts
        if backend is None or getattr(backend, "ready", True):
            return
        if backend.__class__.__name__ != "ElevenLabsTTS":
            return
        key = ensure_elevenlabs_api_key(self.console)
        if key:
            backend.api_key = key

    def _print_tts_status(self) -> None:
        state = "on" if self.session.meta.tts_enabled else "off"
        backend = self.session.tts
        if backend is None:
            detail = "no backend configured"
        else:
            detail = backend.__class__.__name__
            ready = getattr(backend, "ready", True)
            hint = getattr(backend, "configuration_hint", "")
            if not ready and hint:
                detail = f"{detail}, not ready: {hint}"
        accent = self.session.cast_accent() or "any"
        output = (
            "output narration on"
            if self.session.meta.mode == "gm" and self.session.meta.tts_enabled
            else "output narration off"
        )
        voice_tool = (
            "voice commands available"
            if "play_dialogue" in self.session.tools
            else ("voice commands hidden")
        )
        self.console.print(
            f"[dim]TTS is {state}; backend: {detail}; cast accent: {accent}; "
            f"{output}; {voice_tool}[/dim]"
        )

    def _print_cast_accent(self) -> None:
        accent = self.session.cast_accent()
        if accent:
            self.console.print(f"[dim]Default cast accent: {accent}[/dim]")
        else:
            self.console.print("[dim]Default cast accent: any[/dim]")

    def _print_voice_cast(self) -> None:
        cast = self.session.narration.voice_cast()
        if not cast.speakers:
            self.console.print("[dim]No remembered narration voices yet.[/dim]")
            return
        table = Table("speaker", "voice", "accent", "target", "source", "hint")
        for assignment in cast.speakers.values():
            table.add_row(
                assignment.speaker,
                assignment.voice_name,
                assignment.accent or "",
                assignment.target_accent or "",
                assignment.source,
                assignment.voice_hint or "",
            )
        self.console.print(table)

    async def _cmd_sfx(self, args: str) -> None:
        choice = args.strip().lower() or "status"
        if choice in ("status", "show"):
            self._print_sfx_status()
            return
        if choice in ("on", "enable", "enabled", "true", "yes"):
            self._ensure_sfx_ready()
            self.session.set_sound_effects_enabled(True)
            self.console.print("[green]Sound effects on[/green] (saved to this campaign).")
            self._print_sfx_status()
            return
        if choice in ("off", "disable", "disabled", "false", "no"):
            self.session.set_sound_effects_enabled(False)
            self.console.print("[yellow]Sound effects off[/yellow] (saved to this campaign).")
            return
        self.console.print("Usage: /sfx on|off|status")

    def _ensure_sfx_ready(self) -> None:
        backend = self.session.sound_effects
        if backend is None or getattr(backend, "ready", True):
            return
        if backend.__class__.__name__ != "ElevenLabsSoundEffects":
            return
        key = ensure_elevenlabs_api_key(self.console)
        if key:
            backend.api_key = key

    def _print_sfx_status(self) -> None:
        state = "on" if self.session.meta.sound_effects_enabled else "off"
        backend = self.session.sound_effects
        available = (
            "play_sound_effect available"
            if "play_sound_effect" in self.session.tools
            else ("tool hidden")
        )
        if backend is None:
            detail = "no backend configured"
        else:
            detail = backend.__class__.__name__
            ready = getattr(backend, "ready", True)
            hint = getattr(backend, "configuration_hint", "")
            if not ready and hint:
                detail = f"{detail}, not ready: {hint}"
        self.console.print(f"[dim]Sound effects are {state}; backend: {detail}; {available}[/dim]")

    async def _cmd_music(self, args: str) -> None:
        parts = args.strip().split(maxsplit=1)
        choice = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""
        if choice in ("status", "show"):
            self._print_music_status()
            return
        if choice in ("on", "enable", "enabled", "true", "yes"):
            self._ensure_music_ready()
            self.session.set_music_enabled(True)
            self.console.print("[green]Music on[/green] (saved to this campaign).")
            self._print_music_status()
            return
        if choice in ("off", "disable", "disabled", "false", "no"):
            self.session.set_music_enabled(False)
            self.console.print(
                "[yellow]Music off[/yellow]: playback stopped (saved to this campaign)."
            )
            return
        if choice == "auto":
            state = rest.lower()
            if state in ("on", "true", "yes", ""):
                self.session.set_music_auto(True)
                self.console.print(
                    "[green]Auto music on[/green]: in GM mode the AI scores scenes on its own."
                )
            elif state in ("off", "false", "no"):
                self.session.set_music_auto(False)
                self.console.print(
                    "[yellow]Auto music off[/yellow]: the AI changes music only when asked."
                )
            else:
                self.console.print("Usage: /music auto on|off")
            return
        if choice == "play":
            if not rest:
                self.console.print(
                    "Usage: /music play <description>, e.g. /music play eerie crypt ambience"
                )
                return
            self._music_play_local(rest)
            return
        if choice in ("resume", "replay"):
            self._music_resume()
            return
        if choice in ("stop", "silence"):
            self.session.stop_music()
            self.console.print("[yellow]Music stopped.[/yellow]")
            return
        if choice in ("volume", "vol"):
            self._music_set_volume(rest)
            return
        self.console.print(
            "Usage: /music on|off|status|auto on|off|play <desc>|resume|stop|volume <0-100>"
        )

    def _music_resume(self) -> None:
        if not self.session.media_host.capabilities.music:
            self.console.print("[red]This frontend can't play music.[/red]")
            return
        self._ensure_music_ready()
        if not self.session.meta.music_enabled:
            self.session.set_music_enabled(True)
        resumed = self.session.replay_music()
        if resumed is None:
            self.console.print(
                "[yellow]No track to resume[/yellow]: nothing has played yet, or its file "
                "is gone. Start one with /music play <description>."
            )
            return
        self.console.print("[dim]🎵 Resuming the last music track from disk.[/dim]")

    def _music_play_local(self, prompt: str) -> None:
        backend = self.session.music
        if backend is None:
            self.console.print("[red]No music backend configured.[/red]")
            return
        self._ensure_music_ready()
        if not getattr(backend, "ready", True):
            hint = getattr(backend, "configuration_hint", "")
            self.console.print(f"[red]Music backend is not ready.[/red] {hint}")
            return
        started = self.session.start_music(prompt, by="player")
        if started is not None:
            self.console.print(
                f"[dim]⏳ {started.label} It will start looping when ready (takes a minute).[/dim]"
            )

    def _music_set_volume(self, args: str) -> None:
        if not args:
            if self.session.music is None:
                self.console.print("[red]No music backend configured.[/red]")
                return
            volume = self.session.media_host.music_volume()
            self.console.print(
                f"[dim]Music volume: {int(round(volume * 100))}%. "
                "Set with /music volume <0-100>.[/dim]"
            )
            return
        try:
            value = float(args.rstrip("%"))
        except ValueError:
            self.console.print("[red]Couldn't parse that. Try /music volume 60[/red]")
            return
        if value > 1:
            value /= 100
        volume = self.session.set_music_volume(value)
        self.console.print(f"[green]Music volume set to {int(round(volume * 100))}%[/green].")

    def _ensure_music_ready(self) -> None:
        backend = self.session.music
        if backend is None or getattr(backend, "ready", True):
            return
        if backend.__class__.__name__ != "ElevenLabsMusic":
            return
        key = ensure_elevenlabs_api_key(self.console)
        if key:
            backend.api_key = key

    def _print_music_status(self) -> None:
        state = "on" if self.session.meta.music_enabled else "off"
        auto = "auto" if self.session.music_auto() else "manual"
        backend = self.session.music
        if backend is None:
            detail = "no backend configured"
        else:
            detail = backend.__class__.__name__
            ready = getattr(backend, "ready", True)
            hint = getattr(backend, "configuration_hint", "")
            if not ready and hint:
                detail = f"{detail}, not ready: {hint}"
        now = self.session.music_status_line() or "no music playing"
        tools = (
            "play_music available" if "play_music" in self.session.tools else "agent tools hidden"
        )
        volume = f"{int(round(self.session.media_host.music_volume() * 100))}%"
        self.console.print(
            f"[dim]Music is {state} ({auto}); backend: {detail}; volume: {volume}; "
            f"{now}; {tools}[/dim]"
        )

    async def _cmd_images(self, args: str) -> None:
        parts = args.strip().split(maxsplit=1)
        choice = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""
        if choice in ("status", "show"):
            self._print_images_status()
            return
        if choice in ("on", "enable", "enabled", "true", "yes"):
            self._ensure_images_ready()
            self.session.set_images_enabled(True)
            self.console.print("[green]Image generation on[/green] (saved to this campaign).")
            self._print_images_status()
            return
        if choice in ("off", "disable", "disabled", "false", "no"):
            self.session.set_images_enabled(False)
            self.console.print("[yellow]Image generation off[/yellow] (saved to this campaign).")
            return
        if choice == "auto":
            state = rest.lower()
            if state in ("on", "true", "yes", ""):
                self.session.set_images_auto(True)
                self.console.print(
                    "[green]Auto images on[/green]: in GM mode the GM shows images on its own."
                )
            elif state in ("off", "false", "no"):
                self.session.set_images_auto(False)
                self.console.print(
                    "[yellow]Auto images off[/yellow]: the GM shows images only when asked."
                )
            else:
                self.console.print("Usage: /images auto on|off")
            return
        if choice in ("list", "ls"):
            self._list_images()
            return
        self.console.print("Usage: /images on|off|status|auto on|off|list")

    def _ensure_images_ready(self) -> None:
        backend = self.session.images
        if backend is None or getattr(backend, "ready", True):
            return
        if backend.__class__.__name__ != "GeminiImageBackend":
            return
        key = ensure_google_api_key(self.console)
        if key:
            backend.api_key = key

    def _print_images_status(self) -> None:
        state = "on" if self.session.meta.images_enabled else "off"
        auto = "auto" if self.session.images_auto() else "manual"
        backend = self.session.images
        if backend is None:
            detail = "no backend configured"
        else:
            detail = backend.__class__.__name__
            ready = getattr(backend, "ready", True)
            hint = getattr(backend, "configuration_hint", "")
            if not ready and hint:
                detail = f"{detail}, not ready: {hint}"
        tools = (
            "generate_image available"
            if "generate_image" in self.session.tools
            else "image tools hidden"
        )
        self.console.print(f"[dim]Images are {state} ({auto}); backend: {detail}; {tools}[/dim]")

    def _list_images(self) -> None:
        from openadventure.engine.tools.ambience_tools import _generated_images

        rows = _generated_images(self.session.tool_ctx)
        if not rows:
            self.console.print("[dim]No images generated yet.[/dim]")
            return
        table = Table("caption", "path")
        for caption, path in rows:
            table.add_row(caption or "(untitled)", path)
        self.console.print(table)

    def _render_result(self, result: commands.CommandResult) -> None:
        """Render a UI-agnostic CommandResult: map each message's severity to a
        Rich style, then render any typed payload. The engine layer carries no
        markup, so the styling lives here (a Discord/web frontend maps the same
        severities to its own format)."""
        styles = {
            commands.Severity.info: "",
            commands.Severity.success: "green",
            commands.Severity.warning: "yellow",
            commands.Severity.error: "red",
        }
        for message in result.messages:
            style = styles.get(message.severity, "")
            self.console.print(f"[{style}]{message.text}[/{style}]" if style else message.text)
        if isinstance(result.data, commands.ModelList):
            self._render_model_list(result.data)
        elif isinstance(result.data, commands.SourcesView):
            self._render_sources_view(result.data)
        elif isinstance(result.data, commands.ModulesView):
            self._render_modules_view(result.data)

    def _render_model_list(self, data: commands.ModelList) -> None:
        ids = {m.id for m in data.models}
        for m in data.models:
            marker = " [cyan]←[/cyan]" if m.id == data.current else ""
            self.console.print(
                f"[cyan]{m.id}[/cyan]{marker}: {m.display_name} [dim]({m.provider})[/dim], "
                f"{m.context_window:,} ctx, ${m.input_per_mtok}/${m.output_per_mtok} per MTok"
            )
        if data.current not in ids:
            # a custom/unknown id set via /model or config; still the active one
            self.console.print(f"[cyan]{data.current}[/cyan] [cyan]←[/cyan] [dim](current)[/dim]")
        self.console.print(
            f"Current model: [cyan]{data.current}[/cyan]. "
            "[dim]Picking a model selects its backend automatically.[/dim]"
        )

    def _ensure_model_provider(self, provider: str, *, switched: bool) -> None:
        """After a /model change, make sure a provider is connected for the model's
        backend: reconnect from an available key, or prompt for one (offering to
        save it to .env) when none is configured, so /model never silently leaves
        the table disconnected. ``switched`` (did the backend change) only picks the
        message; the prompt fires whenever there's no usable key. Falls back to a
        warning if the player skips the prompt or stdin isn't interactive."""
        from openadventure.cli.firstrun import ensure_api_key
        from openadventure.providers.factory import PROVIDER_INFO

        if self.session.connect_provider():
            if switched:
                self.console.print(f"[dim]Backend switched to {provider}.[/dim]")
            else:
                self.console.print(f"[green]AI connected on the {provider} backend.[/green]")
            return
        api_key = ensure_api_key(self.console, self.session.config, provider)
        if api_key:
            self.session.attach_provider(api_key)
            self.console.print(f"[green]AI connected on the {provider} backend.[/green]")
            return
        env = " / ".join(PROVIDER_INFO[provider]["env"])
        self.console.print(
            f"[yellow]Now on the {provider} backend, but no API key is configured.[/yellow] "
            f"Set {env} (env or .env), then /setup."
        )

    async def _cmd_setup(self, args: str) -> None:
        from openadventure.cli.wizard import run_setup_wizard

        run_setup_wizard(self.console, self.session)

    async def _cmd_model(self, args: str) -> None:
        result = commands.cmd_model(self.session, args)
        self._render_result(result)
        # The model selects the backend: when the command reports a switch (or
        # we're disconnected), reconnect, prompting for a key when needed. That
        # interactive step stays here; the engine layer only flags it.
        data = result.data
        if isinstance(data, commands.ModelChanged) and data.needs_provider:
            self._ensure_model_provider(data.backend, switched=data.switched)

    async def _cmd_effort(self, args: str) -> None:
        self._render_result(commands.cmd_effort(self.session, args))

    async def _cmd_thinking(self, args: str) -> None:
        self._render_result(commands.cmd_thinking(self.session, args))

    async def _cmd_verbosity(self, args: str) -> None:
        self._render_result(commands.cmd_verbosity(self.session, args))

    async def _cmd_context(self, args: str) -> None:
        self._render_result(commands.cmd_context(self.session, args))

    async def _cmd_undo(self, args: str) -> None:
        self._render_result(commands.cmd_undo(self.session, args))

    async def _cmd_retry(self, args: str) -> None:
        plan = self.session.prepare_retry()
        if plan is None:
            self.console.print("Nothing to retry yet.")
            return
        if not plan.undone:
            self.console.print(
                "[yellow]No checkpoint for that turn; retrying without undoing its effects.[/yellow]"
            )
        self.console.print(f"[dim]Retrying: {plan.text[:80]}[/dim]")
        await self.handle_player_input(plan.text)

    async def _cmd_restart(self, args: str) -> None:
        self._render_result(commands.cmd_restart(self.session, args))

    async def _cmd_sudo(self, args: str) -> None:
        directive = args.strip()
        # An optional leading -q/--quiet (or --btw) runs the directive off the
        # record, like /btw: it still steers this turn out of character, but the
        # directive and the GM's reply are never written to the campaign log.
        quiet = False
        flag, _, rest = directive.partition(" ")
        if flag.lower() in ("-q", "--quiet", "--btw", "--off-record"):
            quiet = True
            directive = rest.strip()
        if not directive:
            self.console.print(
                "Usage: /sudo [-q] <instruction>. Steer the AI out of character, e.g. "
                "[dim]/sudo the bandit captain secretly wants to defect[/dim]. "
                "Add [green]-q[/green] to issue it off the record: an out-of-character "
                "command that isn't saved to the story (combines /sudo with /btw)."
            )
            return
        if quiet:
            # An aside cuts off any narration still playing, like a normal message.
            self.session.interrupt_narration()
        await self.handle_player_input(directive, steer=True, ephemeral=quiet)

    async def _cmd_btw(self, args: str) -> None:
        note = args.strip()
        if not note:
            self.console.print(
                "Usage: /btw <message>. Ask the GM something on the side, e.g. "
                "[dim]/btw how much XP is the party sitting on?[/dim]. It runs a normal "
                "turn but isn't saved to the story, so it won't show up in /log, /recap, "
                "or /undo."
            )
            return
        # An aside cuts off any narration still playing from the previous turn,
        # just like a normal message does.
        self.session.interrupt_narration()
        # An aside is read-only as well as off the record: it answers from lookups
        # and changes nothing. (A quiet /sudo directive is ephemeral but not
        # read-only; it is meant to mutate the world.)
        await self.handle_player_input(
            _BTW_INSTRUCTION.format(text=note), ephemeral=True, read_only=True
        )

    async def _cmd_instructions(self, args: str) -> None:
        self._render_result(commands.cmd_instructions(self.session, args))

    async def _cmd_sources(self, args: str) -> None:
        self._render_result(commands.cmd_sources(self.session, args))

    def _render_sources_view(self, view: commands.SourcesView) -> None:
        if view.attached:
            self.console.print(f"[bold]Sources:[/bold] [cyan]{', '.join(view.attached)}[/cyan]")
            if view.system:
                self.console.print(f"[dim]System source: {view.system}.[/dim]")
        else:
            self.console.print(
                "[dim]No sources set; the GM improvises from general TTRPG knowledge.[/dim]"
            )
        if view.available:
            table = Table("ingested sources", "")
            for name in view.available:
                if name == view.system:
                    mark = "[cyan]← system[/cyan]"
                elif name in view.attached:
                    mark = "[cyan]← attached[/cyan]"
                else:
                    mark = ""
                table.add_row(name, mark)
            self.console.print(table)
            self.console.print(
                "[dim]Attach with /sources <names>, /sources add <name>; detach with "
                "/sources remove <name>; set the system source with /sources system <name>; "
                "clear with /sources none.[/dim]"
            )
        else:
            self.console.print(
                "[dim]No sources ingested yet. Add one with "
                "[green]/ingest <book.pdf> --source[/green] "
                "(or [green]openadventure ingest[/green]).[/dim]"
            )

    async def _cmd_premise(self, args: str) -> None:
        self._render_result(commands.cmd_premise(self.session, args))

    async def _cmd_mode(self, args: str) -> None:
        self._render_result(commands.cmd_mode(self.session, args))
        # keep the renderer's private-hiding in step with the (possibly) new mode
        self.renderer.hide_private = self.session.meta.mode == "gm"

    async def _cmd_modules(self, args: str) -> None:
        self._render_result(commands.cmd_modules(self.session, args))

    def _render_modules_view(self, view: commands.ModulesView) -> None:
        if view.arc:
            self.console.print(f"[bold]Arc:[/bold] {view.arc}")
        markers = {
            "completed": "[green]done[/green]",
            "active": "[bold yellow]NOW PLAYING[/bold yellow]",
            "pending": "[dim]upcoming[/dim]",
        }
        for module in view.modules:
            marker = markers.get(module.status, module.status)
            role = f", {module.role}" if module.role else ""
            self.console.print(
                f"  {module.order + 1}. {module.title} ([cyan]{module.slug}[/cyan]) {marker}{role}"
            )

    async def _cmd_ingest(self, args: str) -> None:
        import asyncio as _asyncio
        import shlex
        from pathlib import Path

        from openadventure.ingest import pipeline
        from openadventure.store.workspace import slugify

        usage = (
            "Usage: /ingest <file.pdf|md|txt> (--source | --module) [--name NAME] "
            "[--pages START-END]\n"
            "  Ingests the book into the shared store and attaches it to this campaign. "
            "Pick one: --source records it as a rules source (search_rules); --module records "
            "it as an adventure module (search_campaign). One of the two is required.\n"
            "  --pages restricts a PDF to a 1-based page range, e.g. --pages 18-32."
        )
        if not args.strip():
            self.console.print(usage)
            return
        try:
            tokens = shlex.split(args)
        except ValueError as exc:
            self.console.print(f"[red]Could not parse arguments: {exc}[/red]")
            return

        file_arg: str | None = None
        name: str | None = None
        pages_arg: str | None = None
        as_source = False
        as_module = False
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--name":
                i += 1
                if i >= len(tokens):
                    self.console.print("[red]--name needs a value.[/red]")
                    return
                name = tokens[i]
            elif tok == "--pages":
                i += 1
                if i >= len(tokens):
                    self.console.print("[red]--pages needs a value, e.g. 18-32.[/red]")
                    return
                pages_arg = tokens[i]
            elif tok == "--source":
                as_source = True
            elif tok == "--module":
                as_module = True
            elif tok.startswith("--"):
                self.console.print(f"[red]Unknown option {tok!r}.[/red]\n{usage}")
                return
            elif file_arg is None:
                file_arg = tok
            else:
                self.console.print(f"[red]Unexpected argument {tok!r}.[/red]\n{usage}")
                return
            i += 1

        if file_arg is None:
            self.console.print(usage)
            return
        if as_source and as_module:
            self.console.print(
                "[red]Pick one of --source or --module: a book is either a rules source or "
                "an adventure module, not both.[/red]"
            )
            return
        if not as_source and not as_module:
            self.console.print(
                "[red]Say what the book is:[/red] add [green]--source[/green] for a "
                "rulebook/reference or [green]--module[/green] for an adventure.\n" + usage
            )
            return
        source = Path(file_arg)
        if not source.is_file():
            self.console.print(f"[red]No such file: {source}[/red]")
            return

        pages: tuple[int, int] | None = None
        if pages_arg:
            try:
                start_str, _, end_str = pages_arg.partition("-")
                pages = (int(start_str), int(end_str or start_str))
            except ValueError:
                self.console.print(f"[red]Invalid --pages {pages_arg!r}; use e.g. 18-32.[/red]")
                return

        source_name = slugify(name or source.stem)
        dest = self.session.workspace.book_dir(source_name)
        label = f"book [cyan]{source_name}[/cyan]"
        if pages:
            label += f" [dim](pages {pages[0]}-{pages[1]})[/dim]"

        self.console.print(
            f"Ingesting [bold]{source.name}[/bold] as {label} (PDFs can take a minute)…"
        )
        from openadventure.cli.progress import ingest_progress

        # The required --source/--module flag both records the book's type and
        # attaches it to this campaign in the matching bucket.
        book_type = "source" if as_source else "module"
        try:
            with ingest_progress(self.console) as progress:
                manifest = await _asyncio.to_thread(
                    pipeline.ingest,
                    source,
                    dest,
                    pages=pages,
                    book_type=book_type,
                    embed_backend=self.session.embed_backend,
                    progress=progress,
                )
        except (ValueError, FileNotFoundError) as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        # the required type flag attaches the book to this campaign (and reloads tools)
        if as_source:
            self.session.add_source(source_name)
            role = "a rules source (search_rules)"
        else:
            self.session.add_module(source_name)
            role = "an adventure module (search_campaign)"
        hint = f"attached to this campaign as {role}."
        self.console.print(f"[green]Done[/green]: {manifest['section_count']} sections; {hint}")
        if manifest.get("warning"):
            self.console.print(f"[yellow]Warning:[/yellow] {manifest['warning']}")
            self.console.print(
                "[dim]Run [bold]openadventure inspect[/bold] on the file to see what was extracted.[/dim]"
            )
        note = pipeline.image_only_pages_note(manifest)
        if note:
            self.console.print(f"[yellow]Heads up:[/yellow] {note}")
        if as_source and sys.stdin.isatty():
            template_path = dest / "templates" / "character.json"
            if not template_path.is_file():
                from openadventure.cli.wizard import offer_template_generation_async

                await offer_template_generation_async(
                    self.console,
                    self.session.config,
                    source_name,
                    dest,
                    later_command=f"/template {source_name}",
                    table_model=self.session.settings.model,
                )

    async def _cmd_template(self, args: str) -> None:
        from openadventure.ingest import pipeline
        from openadventure.ingest import template_gen as _tgen

        parts = args.strip().split()
        if not parts:
            self.console.print(
                "Usage: /template <source>. Derive a character-sheet template "
                "from an ingested rules source (AI-powered, runs at high effort)."
            )
            return
        source = parts[0]

        workspace = self.session.workspace
        source_dir = workspace.book_dir(source)
        if not pipeline.is_ingested(source_dir):
            available = ", ".join(workspace.list_books("source")) or "(none ingested)"
            self.console.print(
                f"[red]{source!r} isn't ingested yet.[/red] Ingest it first with "
                f"[green]/ingest <file> --source[/green]. Available sources: {available}."
            )
            return
        if workspace.book_type(source) == "module":
            self.console.print(
                f"[red]{source!r} is an adventure module, not a rules source.[/red] "
                "Templates are only derived from rules sources."
            )
            return

        from openadventure.cli.wizard import run_template_wizard, template_progress_reporter

        prepared = run_template_wizard(
            self.console,
            self.session.config,
            source,
            source_dir=source_dir,
            table_model=self.session.settings.model,
        )
        if prepared is None:
            return
        provider, settings = prepared
        self.console.print(f"Deriving a character template for [cyan]{source}[/cyan]…")
        with self.console.status("The agent is reading the character creation rules…") as status:
            template = await _tgen.derive_template(
                provider,
                settings,
                source_dir,
                source,
                on_progress=template_progress_reporter(status),
            )
        if template is None:
            self.console.print("[red]The agent didn't produce a template. Try again.[/red]")
            return
        self.console.print(
            f"[green]Template saved[/green]: {len(template['fields'])} fields, "
            f"{len(template['resources'])} resources."
        )

    async def _cmd_reindex(self, args: str) -> None:
        """Rebuild the search indexes for every book this campaign uses (rules sources
        and modules) from their stored markdown, picking up hand-edits and a switched
        embedding backend (config [embeddings]). Search keeps working (keyword-only)
        until done."""
        from pathlib import Path

        from openadventure.ingest import pipeline

        session = self.session
        targets: list[tuple[str, Path]] = []
        slugs = list(
            dict.fromkeys([*session.meta.sources, *(m.slug for m in session.meta.modules)])
        )
        for slug in slugs:
            book_dir = session.workspace.book_dir(slug)
            if pipeline.is_ingested(book_dir):
                targets.append((f"book {slug}", book_dir))
        if not targets:
            self.console.print(
                "[yellow]Nothing to reindex (this campaign uses no ingested books).[/yellow]"
            )
            return
        from openadventure.cli.progress import ingest_progress

        for label, dest in targets:
            self.console.print(f"Reindexing [cyan]{label}[/cyan]…")
            with ingest_progress(self.console) as progress:
                await asyncio.to_thread(
                    pipeline.reindex, dest, embed_backend=session.embed_backend, progress=progress
                )
            report = pipeline.index_report(dest)
            line = (
                f"[green]Reindexed[/green] {label}: {report['sections']} sections, "
                f"{report['entities']} entities, {report['edges']} cross-refs"
            )
            if report["windows"]:
                line += f", {report['windows']} windows"
            self.console.print(line)
            if report["dangling"]:
                self.console.print(
                    f"  [yellow]⚠ {report['dangling']} cross-ref target(s) point at a missing "
                    "section[/yellow]"
                )

    async def _cmd_compact(self, args: str) -> None:
        await self.renderer.render_turn(self.session.compact_now())

    async def _cmd_debug(self, args: str) -> None:
        arg = args.strip().lower()
        verb, _, rest = arg.partition(" ")
        rest = rest.strip()
        if verb == "show":
            if rest in ("", "last"):
                self.renderer.show_debug_call(None)
            elif rest == "all":
                self.renderer.show_all_debug_calls()
            elif rest.isdigit():
                self.renderer.show_debug_call(int(rest))
            else:
                self.console.print("[red]Usage: /debug show [n | all][/red]")
            return
        if arg in ("on", "true", "1", "enable", "enabled"):
            self.renderer.debug = True
        elif arg in ("off", "false", "0", "disable", "disabled"):
            self.renderer.debug = False
        elif arg == "":
            self.renderer.debug = not self.renderer.debug
        else:
            self.console.print("[red]Usage: /debug on | off | show [n][/red] (or /debug to toggle)")
            return
        state = "on" if self.renderer.debug else "off"
        self.console.print(f"Debug mode [bold]{state}[/bold].")
        if self.renderer.debug:
            self.console.print(
                "[dim]Each tool call is tagged with a faint [grey42 italic]⟨n⟩[/]; "
                "expand it with [green]/debug show <n>[/green] "
                "(or [green]/debug show all[/green]).[/dim]"
            )

    async def _cmd_quit(self, args: str) -> None:
        self.running = False


def _command_specs() -> list[tuple]:
    """The slash-command table: ``(name, help, handler[, aliases])`` per command.

    Grouped roughly by how often they're reached for during play; the same
    grouping drives the section headers in /help (see ``Repl.HELP_GROUPS``) and
    the in-game command list that ``read_docs`` serves."""
    return [
        # -- Session --
        ("/help", "show this help", Repl._cmd_help, ("/h",)),
        (
            "/clear",
            "clear the screen (logs are kept) and stop narration",
            Repl._cmd_clear,
            ("/cls",),
        ),
        (
            "/restart",
            "start the campaign over: /restart for the options",
            Repl._cmd_restart,
        ),
        ("/quit", "save and exit", Repl._cmd_quit, ("/exit", "/q", "/x")),
        # -- Play --
        ("/roll", "roll dice locally, e.g. /roll 4d6kh3", Repl._cmd_roll),
        ("/undo", "take back the last turn (or /undo 3 for the last three)", Repl._cmd_undo),
        ("/retry", "take back the last turn and try the same message again", Repl._cmd_retry),
        ("/recap", "show the resume recap for this campaign", Repl._cmd_recap),
        ("/scene", "show the current scene", Repl._cmd_scene),
        (
            "/sudo",
            "steer the AI out of character: /sudo [-q] <instruction> "
            "(-q = off the record, not saved to the story)",
            Repl._cmd_sudo,
        ),
        (
            "/btw",
            "ask the GM something on the side: off the record, not saved to the "
            "story: /btw <message>",
            Repl._cmd_btw,
        ),
        ("/compact", "compact the story so far into the rolling summary", Repl._cmd_compact),
        # -- Characters --
        ("/sheet", "show a character sheet: /sheet <id> (or list all)", Repl._cmd_sheet),
        (
            "/import",
            "import a character sheet from a .md, .txt, or .json file: /import <file>",
            Repl._cmd_import,
        ),
        ("/party", "show the active party", Repl._cmd_party),
        ("/encounter", "show the active encounter tracker", Repl._cmd_encounter),
        # -- Campaign --
        ("/mode", "switch campaign mode: /mode gm | /mode assistant", Repl._cmd_mode),
        (
            "/sources",
            "attach ingested books to search: /sources <names> | add N | remove N "
            "| system N | show | clear",
            Repl._cmd_sources,
        ),
        (
            "/premise",
            "set the campaign premise: /premise <text> | show | clear",
            Repl._cmd_premise,
        ),
        (
            "/instructions",
            "set the GM's personality/style: /instructions <text> | show | clear",
            Repl._cmd_instructions,
            ("/personality",),
        ),
        (
            "/modules",
            "campaign modules: /modules (list) | add <name> | remove <slug> "
            "| activate <slug> | arc <text>",
            Repl._cmd_modules,
        ),
        # -- Rulebooks --
        (
            "/ingest",
            "ingest a book and attach it here; pick its type: "
            "/ingest <file> (--source | --module) [--name N] [--pages 18-32]",
            Repl._cmd_ingest,
        ),
        (
            "/template",
            "derive a character-sheet template from a source: /template <source>",
            Repl._cmd_template,
        ),
        (
            "/reindex",
            "rebuild search indexes (keyword, cross-refs, embeddings) for the "
            "sources + active module",
            Repl._cmd_reindex,
        ),
        # -- AI behavior --
        (
            "/model",
            "set the AI model (its backend is selected automatically), "
            "e.g. /model gemini-3.5-flash",
            Repl._cmd_model,
        ),
        ("/effort", "set effort: low|medium|high|max", Repl._cmd_effort),
        ("/thinking", "deeper reasoning, slower turns: /thinking on|off", Repl._cmd_thinking),
        (
            "/verbosity",
            "set response verbosity: low|medium|high",
            Repl._cmd_verbosity,
        ),
        (
            "/context",
            "set context budget, e.g. /context 200k or /context 1m",
            Repl._cmd_context,
        ),
        # -- Audio & video --
        ("/tts", "toggle AI narration: /tts on|off|status|stop", Repl._cmd_tts),
        (
            "/narration",
            "control spoken narration: /narration stop|status|replay",
            Repl._cmd_narration,
        ),
        (
            "/voice",
            "the narrator voice and remembered cast: /voice <id or elevenlabs URL> "
            "| cast | accent <a> | clear <speaker>|all | default",
            Repl._cmd_voice,
            ("/voices",),
        ),
        ("/sfx", "toggle AI sound effects: /sfx on|off|status", Repl._cmd_sfx),
        (
            "/music",
            "background music: /music on|off|auto on|off|play <desc>|resume|stop|volume <0-100>",
            Repl._cmd_music,
        ),
        (
            "/images",
            "AI images: /images on|off|auto on|off|status|list",
            Repl._cmd_images,
        ),
        # -- Info --
        ("/campaigns", "list campaigns in the workspace", Repl._cmd_campaigns),
        (
            "/log",
            "recap the last N story beats (default 10); --raw for the full event log",
            Repl._cmd_log,
        ),
        ("/usage", "token usage and estimated cost", Repl._cmd_usage),
        (
            "/debug",
            "debug mode: /debug on | off | show <n> (expand a tool call)",
            Repl._cmd_debug,
        ),
        # -- Setup --
        (
            "/setup",
            "re-run the setup wizard (API keys, verbosity, audio)",
            Repl._cmd_setup,
        ),
    ]


def commands_help_text() -> str:
    """Plain-text rendering of the slash commands for ``read_docs`` (the same
    table /help shows, grouped by section)."""
    by_name: dict[str, tuple[str, tuple[str, ...]]] = {}
    for spec in _command_specs():
        aliases = spec[3] if len(spec) > 3 else ()
        by_name[spec[0]] = (spec[1], aliases)
    lines: list[str] = []
    for header, names in Repl.HELP_GROUPS:
        lines.append(header)
        for name in names:
            entry = by_name.get(name)
            if entry is None:
                continue
            help_text, aliases = entry
            label = name + (" (" + ", ".join(aliases) + ")" if aliases else "")
            lines.append(f"  {label}  {help_text}")
        lines.append("")
    return "\n".join(lines).strip()
