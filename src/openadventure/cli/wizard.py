"""Setup wizard: onboard API keys and core settings for a campaign.

Runs once at the start of a fresh campaign (skippable) and on demand via the
`/setup` slash command. It operates on a live `GameSession`, so every choice
is persisted to the campaign and applied immediately.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from openadventure.cli.firstrun import prompt_and_store_key
from openadventure.config import AppConfig
from openadventure.engine.session import GameSession


class _SkipWizard(Exception):
    """Raised to skip the rest of setup and jump into the game (typed
    skip/q/quit). Setup is marked done, so the wizard won't offer itself again."""


class _CancelWizard(Exception):
    """Raised to cancel setup without finishing it (Ctrl-D / Ctrl-C). Setup is
    left unmarked, so the wizard offers itself again the next time you play."""


# --- prompt helpers -------------------------------------------------------
def _input(prompt_text: str) -> str:
    try:
        raw = input(prompt_text).strip()
    except EOFError, KeyboardInterrupt:
        raise _CancelWizard from None
    if raw.lower() in ("skip", "q", "quit"):
        raise _SkipWizard
    return raw


def _ask_choice(
    label: str, choices: list[str], default: str, console: Console, *, show_choices: bool = True
) -> str:
    # ``show_choices=False`` drops the inline ``[a/b/c]`` list from the prompt for
    # choice sets that would grow too long to sit next to the input (e.g. models,
    # which already print a full table above). Matching still uses ``choices``.
    inline = f" [{'/'.join(choices)}]" if show_choices else ""
    raw = _input(f"{label}{inline} ({default}): ").lower()
    if not raw:
        return default
    matches = [c for c in choices if c == raw] or [c for c in choices if c.startswith(raw)]
    if len(matches) == 1:
        return matches[0]
    console.print(f"[yellow]Not a valid choice; keeping {default}.[/yellow]")
    return default


def _ask_yes_no(label: str, default: bool) -> bool:
    raw = _input(f"{label} [{'Y/n' if default else 'y/N'}]: ").lower()
    if not raw:
        return default
    return raw in ("y", "yes", "on", "true", "1")


def _ask_secret(label: str) -> str:
    # Echo the key visibly (plain input, not getpass) so a paste can be verified.
    try:
        return input(label).strip()
    except EOFError, KeyboardInterrupt:
        raise _CancelWizard from None


def _onoff(value: bool) -> str:
    return "on" if value else "off"


def _change_later(cmd: str) -> str:
    """Trailing dim hint naming the in-play command that changes this setting."""
    return f" [dim]Change it later with [green]{cmd}[/green].[/dim]"


# --- steps ----------------------------------------------------------------
def _attach_provider(session: GameSession, key: str) -> None:
    from openadventure.providers.factory import build_provider

    session.provider = build_provider(session.provider_name(), key, session.models)


def _step_api_key(console: Console, session: GameSession) -> None:
    from openadventure.config import resolve_api_key
    from openadventure.providers.factory import PROVIDER_INFO

    provider = session.provider_name()  # the model picks the backend
    info = PROVIDER_INFO[provider]
    label, env_var, console_url = info["label"], info["env"][0], info["console"]

    existing = resolve_api_key(session.config, provider)
    if session.provider is not None or existing:
        console.print(f"[green]✓[/green] {label} API key already configured.")
        if session.provider is None and existing:
            _attach_provider(session, existing)
        return
    console.print(
        f"\n[bold]{label} API key[/bold]: powers the AI Game Master." + _change_later("/model")
    )
    console.print(
        f"[dim]Create one at {console_url}. Press Enter to skip and play dice-only.[/dim]"
    )
    key = prompt_and_store_key(
        console,
        label=label,
        env_var=env_var,
        secret_prompt=_ask_secret,
        confirm_save=lambda: _ask_yes_no("Save it to .env so you don't paste it again?", True),
    )
    if not key:
        console.print("[yellow]Skipped: no AI for now (add it later with /model).[/yellow]")
        return
    _attach_provider(session, key)
    console.print("[green]✓ AI connected.[/green]")


def _step_mode(console: Console, session: GameSession) -> None:
    current = session.meta.mode
    console.print(
        "\n[bold]Mode[/bold]: who runs the game.\n"
        "[dim]gm: the AI is your Game Master and runs the campaign for you.\n"
        "assistant: you are the GM; the AI is your co-GM and bookkeeper.[/dim]"
        + _change_later("/mode")
    )
    choice = _ask_choice("Mode", ["gm", "assistant"], current, console)
    if choice != current:
        session.set_mode(choice)
        session.reload_tools()  # mode gates some tools (e.g. stage_dialogue is GM-only)
    console.print(f"[green]✓ Mode set to {choice}.[/green]")


def _step_model(console: Console, session: GameSession) -> None:
    from openadventure.engine.context import estimate_prompt_cost

    current = session.settings.model
    console.print(
        "\n[bold]Model[/bold]: which AI runs the table (its backend is selected automatically)."
        + _change_later("/model")
    )
    table = Table("model", "backend", "context", "~$/prompt")
    # Deprecated models stay usable when pinned but aren't offered here.
    visible = session.models.visible
    ids = [m.id for m in visible]
    # Measure the real non-tail prompt once (it barely varies by model) and reuse it
    # for every row's cost estimate, so no prompt-size estimate is needed.
    non_tail = session.non_tail_tokens()
    for m in visible:
        marker = " [cyan]←[/cyan]" if m.id == current else ""
        table.add_row(
            m.id + marker,
            m.provider,
            f"{m.context_window // 1000}k",
            f"${estimate_prompt_cost(session.settings, m, non_tail):.2f}",
        )
    console.print(table)
    # No inline [a/b/c] list next to the prompt: the model set grows too long for it,
    # and the full table above already shows the choices.
    choice = _ask_choice("Model", ids, current, console, show_choices=False)
    session.set_override("model", choice)
    console.print(f"[green]✓ Model set to {choice}.[/green]")


def _ingest_wizard(console: Console, session: GameSession, book_type: str) -> str | None:
    """Inline ingestion mini-wizard: ask for a file, optional name, optional page
    range, then run the pipeline. Returns the new book's slug on success, or None
    if the user presses Enter to skip, the file is missing, or ingestion fails.


    ``book_type`` is ``"source"`` or ``"module"``."""
    from pathlib import Path

    from openadventure.cli.progress import ingest_progress
    from openadventure.ingest import embeddings, pipeline
    from openadventure.store.workspace import slugify

    type_label = "rules source" if book_type == "source" else "adventure module"
    console.print(f"\n[bold]Ingest a {type_label}[/bold]: PDF, Markdown, or plain text.")
    console.print("[dim]Press Enter to skip.[/dim]")
    raw_path = _input("File path: ")
    if not raw_path:
        return None
    source = Path(raw_path.strip("\"'"))
    if not source.is_file():
        console.print(f"[red]No such file: {source}[/red]")
        return None

    default_name = slugify(source.stem)
    raw_name = _input(f"Name [{default_name}]: ")
    source_name = slugify(raw_name) if raw_name else default_name

    pages: tuple[int, int] | None = None
    if source.suffix.lower() == ".pdf":
        raw_pages = _input("Page range (e.g. 18-32, or Enter for whole document): ")
        if raw_pages:
            try:
                start_str, _, end_str = raw_pages.partition("-")
                pages = (int(start_str), int(end_str or start_str))
            except ValueError:
                console.print(
                    f"[yellow]Invalid page range {raw_pages!r}; ingesting whole document.[/yellow]"
                )

    dest = session.workspace.book_dir(source_name)
    embed_backend, embed_reason = embeddings.try_load_backend(session.config.embeddings)
    if embed_reason:
        console.print(f"[yellow]Semantic search off:[/yellow] {embed_reason}")

    console.print(
        f"Ingesting [bold]{source.name}[/bold] as {type_label} [cyan]{source_name}[/cyan]…"
    )
    try:
        with ingest_progress(console) as progress:
            pipeline.ingest(
                source,
                dest,
                pages=pages,
                book_type=book_type,
                embed_backend=embed_backend,
                progress=progress,
            )
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        return None

    console.print(f"[green]✓ Ingested[/green] [cyan]{source_name}[/cyan].")
    return source_name


def _step_sources(console: Console, session: GameSession) -> None:
    available = session.workspace.list_books("source")
    current = session.meta.sources
    console.print(
        "\n[bold]Sources[/bold]: the ingested books this campaign can search (rulebook, "
        "monster manual, setting guide…), picked from your ingested books. The first is "
        "the system source that defines the rules and character template."
        + _change_later("/sources")
    )
    if not available:
        console.print("[dim]No sources ingested yet.[/dim]")
        if not _ask_yes_no("Ingest a new source now?", True):
            console.print("[dim]Skipping; the GM will improvise rules.[/dim]")
            return
        slug = _ingest_wizard(console, session, "source")
        if not slug:
            console.print("[dim]No source ingested; the GM will improvise rules.[/dim]")
            return
        available = session.workspace.list_books("source")

    while True:
        table = Table("source", "")
        for name in available:
            if name == session.meta.system_source:
                mark = "[cyan]← system[/cyan]"
            elif name in current:
                mark = "[cyan]← attached[/cyan]"
            else:
                mark = ""
            table.add_row(name, mark)
        console.print(table)
        if current:
            console.print(
                f"[dim]Current: {', '.join(current)}. Press Enter to keep, type a comma-separated "
                "list to replace (first = system source), 'none' to clear, or 'ingest' to add a "
                "new source.[/dim]"
            )
        else:
            console.print(
                "[dim]Type one or more sources, comma-separated (first = system source); "
                "press Enter to skip, or 'ingest' to add a new source.[/dim]"
            )
        choice = _input("Sources: ")
        if choice.lower() == "ingest":
            slug = _ingest_wizard(console, session, "source")
            if slug:
                available = session.workspace.list_books("source")
                current = session.meta.sources
            continue
        break

    if choice and choice.lower() in ("none", "clear"):
        session.clear_sources()
        console.print("[yellow]No sources set; the GM will improvise rules.[/yellow]")
        return
    if choice:
        chosen: list[str] = []
        unmatched: list[str] = []
        for raw in choice.split(","):
            name = raw.strip().lower()
            if not name:
                continue
            matches = [s for s in available if s == name] or [
                s for s in available if s.startswith(name)
            ]
            if len(matches) == 1 and matches[0] not in chosen:
                chosen.append(matches[0])
            elif not matches or len(matches) > 1:
                unmatched.append(raw.strip())
        if chosen:
            session.set_sources(chosen)
            console.print(
                f"[green]✓ Sources set to {', '.join(chosen)}[/green] "
                f"(system: {session.meta.system_source})."
            )
        if unmatched:
            console.print(f"[yellow]No single match for: {', '.join(unmatched)}; skipped.[/yellow]")


def template_progress_reporter(status):
    """An ``on_progress`` callback for `derive_template` that updates a live
    spinner in place, so a long derivation stays one self-updating status line
    instead of scrolling its full reasoning. ``status`` is the object returned by
    ``console.status(...)``.

    Uses `rich.text.Text` rather than markup so brackets in the model's thinking
    (`[STR 15]`, dice notation, etc.) can't break console rendering."""
    from rich.text import Text

    def report(message: str) -> None:
        status.update(Text(message, style="dim"))

    return report


async def offer_template_generation_async(
    console: Console,
    config: AppConfig,
    source_name: str,
    source_dir: Path,
    *,
    later_command: str | None = None,
    table_model: str | None = None,
) -> None:
    """Offer to derive a missing character template for ``source_name`` now.

    The same offer the ingest pipelines make: prompt [Y/n], and on yes run
    `run_template_wizard` + `derive_template`, reporting the outcome; on no, hint
    at the command that generates it later (``later_command``, defaulting to the
    CLI; the in-play caller passes ``/template``). The caller is responsible for
    checking the template is actually missing and that stdin is interactive.

    ``table_model`` is the campaign's table model when offering from inside the
    game; it makes the derivation reuse that model (at high effort) without a
    model prompt. Out-of-game callers leave it None to be asked.

    The async form is the source of truth so it works inside the play loop;
    `offer_template_generation` wraps it for synchronous callers."""
    later = later_command or f"openadventure template {source_name}"
    console.print(
        f"\n[dim]No character template for [cyan]{source_name}[/cyan] yet: "
        "generate one now so the GM creates consistent sheets?[/dim]"
    )
    try:
        answer = input("Generate template? [Y/n]: ").strip().lower()
    except EOFError, KeyboardInterrupt:
        answer = "n"
    if answer and answer not in ("y", "yes"):
        console.print(f"[dim]Generate it later with [green]{later}[/green].[/dim]")
        return
    from openadventure.ingest import template_gen

    prepared = run_template_wizard(console, config, source_name, table_model=table_model)
    if prepared is None:
        return
    provider, settings = prepared
    console.print(f"Deriving a character template for [cyan]{source_name}[/cyan]…")
    with console.status("The agent is reading the character creation rules…") as status:
        template = await template_gen.derive_template(
            provider,
            settings,
            source_dir,
            source_name,
            on_progress=template_progress_reporter(status),
        )
    if template is None:
        console.print(
            "[red]The agent didn't produce a template.[/red] Try again with "
            f"[green]openadventure template {source_name}[/green]."
        )
    else:
        console.print(
            f"[green]Saved[/green] {len(template['fields'])} fields, "
            f"{len(template['resources'])} resources to "
            f"[dim]{source_dir / 'templates' / 'character.json'}[/dim]"
        )


def offer_template_generation(
    console: Console,
    config: AppConfig,
    source_name: str,
    source_dir: Path,
    *,
    later_command: str | None = None,
    table_model: str | None = None,
) -> None:
    """Synchronous wrapper around `offer_template_generation_async` for callers
    outside an event loop (the CLI ingest command and the setup wizard)."""
    asyncio.run(
        offer_template_generation_async(
            console,
            config,
            source_name,
            source_dir,
            later_command=later_command,
            table_model=table_model,
        )
    )


def _maybe_offer_template(console: Console, session: GameSession) -> None:
    """Handle a missing (optional) character template for the system source.

    Templates are optional (the GM improvises creation from the rules without
    one), but having one keeps generated sheets consistent, and nothing derives
    it automatically. Under an interactive tty, offer to generate it now (so a
    campaign whose source was attached before setup still gets the offer);
    otherwise just point at the CLI command."""
    source = session.meta.system_source
    if not source or session.has_character_template():
        return
    if sys.stdin.isatty():
        offer_template_generation(
            console,
            session.config,
            source,
            session.workspace.book_dir(source),
            table_model=session.settings.model,
        )
    else:
        console.print(
            f"[dim]No character-sheet template for [cyan]{source}[/cyan] yet. It's optional, "
            "but generating one keeps created sheets consistent. Make it ahead of play with "
            f"[green]openadventure template {source}[/green].[/dim]"
        )


def _step_premise(console: Console, session: GameSession) -> None:
    current = session.meta.premise
    console.print(
        "\n[bold]Premise[/bold]: the seed idea or pitch the GM builds the campaign on "
        "(optional)." + _change_later("/premise")
    )
    if current:
        console.print(f"[dim]Current: {current}[/dim]")
        console.print(
            "[dim]Press Enter to keep it, type a new premise to replace it, or 'clear' to "
            "remove it.[/dim]"
        )
    else:
        console.print('[dim]e.g. "a heist in a drowned elven city." Press Enter to skip.[/dim]')
    text = _input("Premise: ")
    if not text:
        return
    if text.lower() in ("clear", "none", "remove"):
        session.set_premise(None)
        console.print("[yellow]Premise cleared.[/yellow]")
        return
    session.set_premise(text)
    console.print("[green]✓ Premise saved.[/green]")


def _step_modules(console: Console, session: GameSession) -> None:
    available = session.workspace.list_books("module")
    current = [m.slug for m in session.meta.modules]
    console.print(
        "\n[bold]Adventure modules[/bold]: published adventures for the GM to run, picked "
        "from your ingested books (the first is the one play starts in)."
        + _change_later("/modules")
    )
    if not available:
        console.print("[dim]No modules ingested yet.[/dim]")
        if not _ask_yes_no("Ingest a new module now?", True):
            console.print("[dim]Skipping; the GM runs without a module.[/dim]")
            return
        slug = _ingest_wizard(console, session, "module")
        if not slug:
            console.print("[dim]No module ingested; the GM runs without a module.[/dim]")
            return
        available = session.workspace.list_books("module")

    while True:
        table = Table("source", "")
        for name in available:
            if name == session.meta.active_module:
                mark = "[cyan]← now playing[/cyan]"
            elif name in current:
                mark = "[cyan]← attached[/cyan]"
            else:
                mark = ""
            table.add_row(name, mark)
        console.print(table)
        if current:
            console.print(
                f"[dim]Current: {', '.join(current)}. Press Enter to keep, type a comma-separated "
                "list to replace (first = where play starts), 'none' to clear, or 'ingest' to add "
                "a new module.[/dim]"
            )
        else:
            console.print(
                "[dim]Type one or more modules, comma-separated (first = where play starts); "
                "press Enter to skip, or 'ingest' to add a new module.[/dim]"
            )
        choice = _input("Modules: ")
        if choice.lower() == "ingest":
            slug = _ingest_wizard(console, session, "module")
            if slug:
                available = session.workspace.list_books("module")
                current = [m.slug for m in session.meta.modules]
            continue
        break

    if choice and choice.lower() in ("none", "clear"):
        session.set_modules([])
        console.print("[yellow]No modules set; the GM runs without an adventure module.[/yellow]")
        return
    if not choice:
        return
    chosen: list[str] = []
    unmatched: list[str] = []
    for raw in choice.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        matches = [s for s in available if s == name] or [
            s for s in available if s.startswith(name)
        ]
        if len(matches) == 1 and matches[0] not in chosen:
            chosen.append(matches[0])
        elif not matches or len(matches) > 1:
            unmatched.append(raw.strip())
    if chosen:
        session.set_modules(chosen)
        console.print(
            f"[green]✓ Modules set to {', '.join(chosen)}[/green] "
            f"(starting in {session.meta.active_module})."
        )
    if unmatched:
        console.print(f"[yellow]No single match for: {', '.join(unmatched)}; skipped.[/yellow]")


def _step_verbosity(console: Console, session: GameSession) -> None:
    from openadventure.providers.base import Verbosity

    current = session.settings.verbosity.value
    console.print(
        "\n[bold]Verbosity[/bold]: how wordy the narration is." + _change_later("/verbosity")
    )
    choice = _ask_choice("Verbosity", [v.value for v in Verbosity], current, console)
    session.set_override("verbosity", choice)
    console.print(f"[green]✓ Verbosity set to {choice}.[/green]")


def _step_special_instructions(console: Console, session: GameSession) -> None:
    current = session.custom_instructions()
    console.print(
        "\n[bold]Special instructions[/bold]: set the GM's tone, personality, and style "
        "(e.g. forgiving vs. punishing, sandbox vs. guided)." + _change_later("/instructions")
    )
    if current:
        console.print(f"[dim]Current: {current}[/dim]")
        console.print("[dim]Press Enter to keep, or type new instructions to replace.[/dim]")
    else:
        console.print("[dim]Press Enter to skip.[/dim]")
    text = _input("Special instructions: ")
    if not text:
        return
    session.set_custom_instructions(text)
    console.print("[green]✓ Special instructions saved.[/green]")


def _ensure_elevenlabs(console: Console, session: GameSession) -> None:
    if os.environ.get("ELEVENLABS_API_KEY") or session.config.media.get("elevenlabs_api_key"):
        console.print("[green]✓[/green] ElevenLabs API key already configured.")
        return
    console.print(
        "[dim]Audio uses ElevenLabs (https://elevenlabs.io). "
        "Press Enter to enable now and add a key later.[/dim]"
    )
    key = prompt_and_store_key(
        console,
        label="ElevenLabs",
        env_var="ELEVENLABS_API_KEY",
        secret_prompt=_ask_secret,
        confirm_save=lambda: _ask_yes_no("Save it to .env?", True),
    )
    if not key:
        console.print(
            "[yellow]No key yet: audio is on but silent until you add one "
            "(/setup or ELEVENLABS_API_KEY).[/yellow]"
        )


def _ensure_google(console: Console, session: GameSession) -> None:
    if (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or session.config.media.get("google_api_key")
    ):
        console.print("[green]✓[/green] Google API key already configured.")
        return
    console.print(
        "[dim]Images use Google Gemini (https://aistudio.google.com/apikey). "
        "Press Enter to enable now and add a key later.[/dim]"
    )
    key = prompt_and_store_key(
        console,
        label="Google AI",
        env_var="GOOGLE_API_KEY",
        secret_prompt=_ask_secret,
        confirm_save=lambda: _ask_yes_no("Save it to .env?", True),
    )
    if not key:
        console.print(
            "[yellow]No key yet: images are on but won't render until you add one "
            "(/setup or GOOGLE_API_KEY).[/yellow]"
        )


def _step_images(console: Console, session: GameSession) -> None:
    meta = session.meta
    console.print(
        "\n[bold]Images[/bold]: AI-generated illustrations of scenes, NPCs, and items, "
        "shown on your screen (optional, needs a Google API key)." + _change_later("/images")
    )
    if not _ask_yes_no("Set up image generation?", meta.images_enabled):
        return
    _ensure_google(console, session)
    session.set_images_enabled(True)
    session.reload_tools()
    console.print("[green]✓ Images set[/green]: image generation on.")


def _step_narrator_voice(console: Console, session: GameSession) -> None:
    """Pick the ElevenLabs voice the GM narrator reads with. Offered only when
    spoken narration is on; accepts a bare voice id or a voice-library URL."""
    from openadventure.media.tts import extract_voice_id

    current = session.narrator_voice_id()
    console.print(
        "\n[bold]Narrator voice[/bold]: the ElevenLabs voice that reads the GM's narration. "
        "Paste a voice ID or its voice-library URL." + _change_later("/voice")
    )
    if current:
        console.print(
            f"[dim]Current: {current}. Press Enter to keep it, or type 'default' to reset.[/dim]"
        )
    else:
        console.print("[dim]Press Enter to keep the default voice.[/dim]")
    raw = _input("Narrator voice: ")
    if not raw:
        return
    if raw.lower() in ("default", "reset", "clear", "none"):
        session.set_narrator_voice_id(None)
        console.print("[green]✓ Narrator voice reset to the default.[/green]")
        return
    voice_id = extract_voice_id(raw)
    session.set_narrator_voice_id(voice_id)
    console.print(f"[green]✓ Narrator voice set to {voice_id}.[/green]")


def _step_media(console: Console, session: GameSession) -> None:
    meta = session.meta
    console.print(
        "\n[bold]Audio[/bold]: spoken narration, background music, and sound effects "
        "(optional, needs an ElevenLabs key)."
        " [dim]Change it later with [green]/tts[/green], [green]/music[/green], "
        "[green]/sfx[/green].[/dim]"
    )
    any_on = meta.tts_enabled or meta.music_enabled or meta.sound_effects_enabled
    if not _ask_yes_no("Set up audio?", any_on):
        return
    narration = _ask_yes_no("Spoken narration (TTS)?", meta.tts_enabled)
    sfx = _ask_yes_no("Sound effects?", meta.sound_effects_enabled)
    music = _ask_yes_no("Background music?", meta.music_enabled)
    if narration or sfx or music:
        _ensure_elevenlabs(console, session)
    session.set_tts_enabled(narration)
    session.set_sound_effects_enabled(sfx)
    session.set_music_enabled(music)
    session.reload_tools()
    if narration:
        _step_narrator_voice(console, session)
    console.print(
        f"[green]✓ Audio set[/green]: narration {_onoff(narration)}, "
        f"music {_onoff(music)}, sfx {_onoff(sfx)}."
    )


def _print_summary(console: Console, session: GameSession) -> None:
    s = session.settings
    meta = session.meta
    ai = "connected" if session.provider else "not connected"
    modules = [m.slug for m in meta.modules]
    console.print(
        f"\n[bold]Setup complete.[/bold] [dim]AI {ai}, "
        f"mode {meta.mode}, model {s.model}, "
        f"sources {', '.join(meta.sources) or 'none'}, premise {'set' if meta.premise else 'none'}, "
        f"modules {', '.join(modules) if modules else 'none'}; "
        f"verbosity {s.verbosity.value}; narration {_onoff(meta.tts_enabled)}, "
        f"music {_onoff(meta.music_enabled)}, "
        f"sfx {_onoff(meta.sound_effects_enabled)}, "
        f"images {_onoff(meta.images_enabled)}.[/dim]"
    )
    console.print("[dim]Re-run this anytime with [green]/setup[/green][/dim]")
    console.print()


def run_setup_wizard(console: Console, session: GameSession, *, first_run: bool = False) -> None:
    """Walk through API keys, model, verbosity, and audio. Skippable.

    No-op on a non-interactive stdin so scripted/CI play never blocks.
    Steps that are already configured (either set via CLI before setup, or
    completed in a prior interrupted run) are auto-confirmed and skipped.
    """
    if not sys.stdin.isatty():
        return
    if first_run:
        console.print(
            f"[bold]Welcome to openadventure.[/bold] Let's set up [cyan]{session.meta.name}[/cyan]."
        )
    else:
        console.print(f"[bold]Setup[/bold]: configuring [cyan]{session.meta.name}[/cyan].")
    console.print(
        "[dim]Press Enter at any step to keep the shown default; "
        "type [green]skip[/green] to jump straight into the game; "
        "press [green]Ctrl+D[/green] to cancel and be asked again next time.[/dim]"
    )

    # Steps completed in this or a prior interrupted run. Empty when setup_done
    # is True so an explicit /setup re-asks everything; otherwise loaded from
    # meta.settings so a cancel-and-resume picks up where it left off.
    _done: set[str] = (
        set() if session.meta.setup_done else set(session.meta.settings.get("_wizard_steps", []))
    )

    def _mark(name: str) -> None:
        _done.add(name)
        session.meta.settings["_wizard_steps"] = sorted(_done)
        session.campaign.save_meta(session.meta)

    def _already(label: str, summary: str) -> None:
        console.print(f"[green]✓[/green] {label}: {summary}.")

    try:
        # Model first so the API-key step resolves the key for the model's own
        # backend; getting the key out of the way up front.
        if "model" in _done:
            _already("Model", session.settings.model)
        else:
            _step_model(console, session)
            _mark("model")

        # API key: always run. It re-attaches the provider on each process
        # start and has its own "already configured" fast-path.
        _step_api_key(console, session)
        _mark("api_key")

        # Mode: skip if wizard already visited it, or if it was explicitly set
        # to a non-default value via CLI (e.g. openadventure new --mode assistant).
        if "mode" in _done or session.meta.mode != "gm":
            _already("Mode", session.meta.mode)
            _mark("mode")
        else:
            _step_mode(console, session)
            _mark("mode")

        # Sources and modules: also skip if already attached via CLI before setup.
        if "sources" in _done or session.meta.sources:
            src = ", ".join(session.meta.sources) if session.meta.sources else "none"
            _already("Sources", src)
            _mark("sources")
        else:
            _step_sources(console, session)
            _mark("sources")
        # Offer to derive a missing template whether sources were just chosen or
        # attached beforehand, so a pre-configured campaign still gets the offer.
        _maybe_offer_template(console, session)

        if "premise" in _done or session.meta.premise is not None:
            _already("Premise", "set" if session.meta.premise else "none")
            _mark("premise")
        else:
            _step_premise(console, session)
            _mark("premise")

        if "modules" in _done or session.meta.modules:
            mods = (
                ", ".join(m.slug for m in session.meta.modules) if session.meta.modules else "none"
            )
            _already("Modules", mods)
            _mark("modules")
        else:
            _step_modules(console, session)
            _mark("modules")

        if "special_instructions" in _done or session.custom_instructions():
            ci = session.custom_instructions()
            _already("Special instructions", "set" if ci else "none")
            _mark("special_instructions")
        else:
            _step_special_instructions(console, session)
            _mark("special_instructions")

        if "verbosity" in _done:
            _already("Verbosity", session.settings.verbosity.value)
        else:
            _step_verbosity(console, session)
            _mark("verbosity")

        meta = session.meta
        any_audio = meta.tts_enabled or meta.music_enabled or meta.sound_effects_enabled
        if "media" in _done or any_audio:
            _already(
                "Audio",
                f"narration {_onoff(meta.tts_enabled)}, "
                f"music {_onoff(meta.music_enabled)}, "
                f"sfx {_onoff(meta.sound_effects_enabled)}",
            )
            _mark("media")
        else:
            _step_media(console, session)
            _mark("media")

        if "images" in _done or meta.images_enabled:
            _already("Images", "on" if meta.images_enabled else "off")
            _mark("images")
        else:
            _step_images(console, session)
            _mark("images")

    except _SkipWizard:
        console.print("[dim]Setup skipped. Run [green]/setup[/green] anytime to finish.[/dim]")
    except _CancelWizard:
        # Ctrl+D / Ctrl+C: bail out without finishing. Leave setup_done unset so
        # the wizard offers itself again next launch. Anything chosen before the
        # cancel was already saved by its own step.
        console.print(
            "\n[dim]Setup cancelled; it'll come back next time "
            "(or run [green]/setup[/green] when you're ready).[/dim]"
        )
        return

    # Clear step tracking: setup is complete, /setup should re-ask everything.
    session.meta.settings.pop("_wizard_steps", None)
    if not session.meta.setup_done:
        session.meta.setup_done = True
        session.campaign.save_meta(session.meta)
    _print_summary(console, session)


def run_template_wizard(
    console: Console,
    config: AppConfig,
    source: str,
    *,
    source_dir=None,
    table_model: str | None = None,
) -> tuple | None:
    """Prepare a character-template derivation for ``source``.

    Returns ``(provider, settings)`` ready to derive, or None if the player backs
    out or no key is available. Either way the settings run thinking-on at high
    effort (a one-time, off-the-table job).

    The model is resolved in one of two ways:

    * In-game (``table_model`` given): the campaign's table model is used as-is,
      run at high effort. No prompt, and nothing is persisted to the workspace.
    * Out-of-game (``table_model`` None): no campaign is loaded, so the wizard
      asks for the model each run, defaulting to the saved workspace
      ``[utility]`` model, and persists the pick as that default.

    If ``source_dir`` is given and a template already exists there, the wizard
    warns interactively and asks for confirmation before proceeding.
    """
    from openadventure.cli.firstrun import ensure_api_key
    from openadventure.config import set_utility_model
    from openadventure.engine.session import resolve_utility_settings
    from openadventure.providers.base import HIGH_EFFORT_SETTINGS, ModelRegistry
    from openadventure.providers.factory import build_provider

    if source_dir is not None:
        template_path = source_dir / "templates" / "character.json"
        if template_path.is_file():
            console.print(
                f"[yellow]{source!r} already has a character template.[/yellow] "
                "Regenerate and overwrite it?"
            )
            if sys.stdin.isatty():
                try:
                    answer = input("Overwrite? [y/N]: ").strip().lower()
                except EOFError, KeyboardInterrupt:
                    console.print("\n[dim]Cancelled.[/dim]")
                    return None
                if answer not in ("y", "yes"):
                    console.print("[dim]Kept the existing template.[/dim]")
                    return None
            else:
                console.print("[dim]Non-interactive; overwriting.[/dim]")

    models = ModelRegistry.load_default()

    if table_model is not None:
        # In-game: reuse the campaign's table model, just at high effort. No model
        # prompt and no workspace write; the table model is the single setting.
        settings = HIGH_EFFORT_SETTINGS.merged({"model": table_model})
        chosen = table_model
        console.print(
            f"\n[bold]Character template for [cyan]{source}[/cyan][/bold]\n"
            "[dim]A one-time, accuracy-first job: the agent reads the creation rules and runs "
            f"the table model ([cyan]{chosen}[/cyan]) at high effort with thinking on.[/dim]"
        )
    else:
        # Out-of-game: no campaign model to borrow, so ask, defaulting to the saved
        # workspace [utility] model.
        settings = resolve_utility_settings(config)
        default_model = settings.model
        console.print(
            f"\n[bold]Character template for [cyan]{source}[/cyan][/bold]\n"
            "[dim]A one-time, accuracy-first job: the agent reads the creation rules and runs "
            "at high effort with thinking on, on whichever model you pick.[/dim]"
        )
        chosen = default_model
        if sys.stdin.isatty():
            # Deprecated models stay usable when pinned but aren't offered here.
            visible = models.visible
            table = Table("model", "backend", "context")
            for m in visible:
                label = m.id + (" [cyan](default)[/cyan]" if m.id == default_model else "")
                table.add_row(label, m.provider, f"{m.context_window // 1000}k")
            console.print(table)
            try:
                raw = input(f"Model [{default_model}]: ").strip()
            except EOFError, KeyboardInterrupt:
                console.print("\n[dim]Cancelled.[/dim]")
                return None
            if raw:
                lowered = raw.lower()
                ids = [m.id for m in visible]
                matches = [i for i in ids if i == lowered] or [
                    i for i in ids if i.startswith(lowered)
                ]
                if len(matches) == 1:
                    chosen = matches[0]
                else:
                    console.print(
                        f"[yellow]No single match for {raw!r}; using {default_model}.[/yellow]"
                    )
        if chosen != default_model:
            settings = settings.merged({"model": chosen})

    provider_name = models.provider_for(chosen)
    console.print(f"[dim]Deriving on [cyan]{chosen}[/cyan] (backend: {provider_name}).[/dim]")

    # Out-of-game only: persist the pick as the workspace default for the next
    # out-of-game derivation. In-game we never write the workspace from here.
    if table_model is None and sys.stdin.isatty() and set_utility_model(config, chosen):
        console.print(
            f"[dim]Saved [cyan]{chosen}[/cyan] as the default model for out-of-game jobs.[/dim]"
        )

    api_key = ensure_api_key(console, config, provider_name)
    if not api_key:
        console.print("[red]Template derivation needs an API key.[/red]")
        return None
    return build_provider(provider_name, api_key, models), settings
