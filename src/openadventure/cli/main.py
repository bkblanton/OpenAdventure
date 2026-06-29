"""openadventure command-line entry point (argparse subcommands; the REPL is the real UI)."""

from __future__ import annotations

import argparse
import asyncio
import sys


def _cmd_roll(args: argparse.Namespace) -> int:
    import random

    from openadventure.cli.term import make_console
    from openadventure.mechanics import dice

    console = make_console()
    try:
        outcome = dice.roll(" ".join(args.expression), random.Random())
    except dice.DiceError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    console.print(f"[bold cyan]🎲 {outcome.detail()}[/bold cyan]")
    return 0


def _cmd_new(args: argparse.Namespace) -> int:
    from openadventure.cli.term import make_console
    from openadventure.config import load_config
    from openadventure.store.workspace import BookTypeMismatch, Workspace

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)
    try:
        campaign = workspace.create_campaign(
            args.name,
            mode=args.mode,
            sources=args.source,
            modules=args.module,
        )
    except (FileExistsError, BookTypeMismatch) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    meta = campaign.load_meta()
    console.print(
        f"Created campaign [cyan]{meta.slug}[/cyan] ({meta.mode} mode). "
        f"Start playing with: [green]openadventure play {meta.slug}[/green]"
    )
    if meta.system_source:
        from openadventure.engine.prompts import load_character_template

        if load_character_template(meta, workspace) is None:
            console.print(
                f"[dim]Source [cyan]{meta.system_source}[/cyan] has no character template yet "
                "(optional). Generate one ahead of play with "
                f"[green]openadventure template {meta.system_source}[/green].[/dim]"
            )
    return 0


def _delete_campaign(workspace, args, console) -> int:
    from openadventure.store.workspace import slugify

    slug = slugify(args.slug)
    try:
        campaign = workspace.campaign(slug)
    except FileNotFoundError:
        console.print(f"[red]No campaign named {args.slug!r}.[/red]")
        return 1
    meta = campaign.load_meta()
    if not args.yes:
        console.print(
            f"This permanently deletes campaign [cyan]{slug}[/cyan] ({meta.name}) and its whole "
            "story: characters, notes, and history."
        )
        if input("Delete it? [y/N] ").strip().lower() not in ("y", "yes"):
            console.print("Cancelled.")
            return 0
    workspace.delete_campaign(slug)
    console.print(f"[green]Deleted[/green] campaign [cyan]{slug}[/cyan].")
    return 0


def _rename_campaign(workspace, args, console) -> int:
    from openadventure.store.workspace import slugify

    slug = slugify(args.slug)
    try:
        renamed = workspace.rename_campaign(slug, args.rename)
    except FileNotFoundError:
        console.print(f"[red]No campaign named {args.slug!r}.[/red]")
        return 1
    except FileExistsError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    meta = renamed.load_meta()
    console.print(f"[green]Renamed[/green] to [cyan]{meta.slug}[/cyan] ({meta.name}).")
    if meta.slug != slug:
        console.print(f"[dim]Play it with: openadventure play {meta.slug}[/dim]")
    return 0


def _fork_campaign(workspace, args, console) -> int:
    from openadventure.store.workspace import slugify

    slug = slugify(args.slug)
    try:
        forked = workspace.fork_campaign(slug, args.fork)
    except FileNotFoundError:
        console.print(f"[red]No campaign named {args.slug!r}.[/red]")
        return 1
    except FileExistsError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    meta = forked.load_meta()
    console.print(
        f"[green]Forked[/green] [cyan]{slug}[/cyan] into [cyan]{meta.slug}[/cyan] ({meta.name}), "
        "a full copy of the story so far."
    )
    console.print(f"[dim]Play it with: openadventure play {meta.slug}[/dim]")
    return 0


def _cmd_campaigns(args: argparse.Namespace) -> int:
    from openadventure.cli.term import make_console
    from openadventure.config import load_config
    from openadventure.store.workspace import Workspace

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)

    if args.delete or args.rename or args.fork:
        if not args.slug:
            console.print(
                "[red]Specify which campaign:[/red] openadventure campaigns <slug> "
                "--delete/--rename/--fork."
            )
            return 1
        if args.delete:
            return _delete_campaign(workspace, args, console)
        if args.rename:
            return _rename_campaign(workspace, args, console)
        return _fork_campaign(workspace, args, console)
    if args.slug:
        console.print(
            f"[red]Nothing to do with {args.slug!r}.[/red] Add --delete, --rename, or --fork, "
            "or run [green]openadventure campaigns[/green] to list."
        )
        return 1

    metas = workspace.list_campaigns()
    if not metas:
        console.print("No campaigns yet. Create one with: [green]openadventure new <name>[/green]")
        return 0
    for m in metas:
        console.print(f"[cyan]{m.slug}[/cyan]  {m.name}  [dim]{m.mode}, {m.created_at[:10]}[/dim]")
    return 0


def _book_usage(workspace) -> dict[str, list[str]]:
    """Map each ingested book slug to the campaigns using it and in what role,
    e.g. {'dnd5e': ['curse-of-strahd (rules/system)'], 'death-house': ['curse-of-strahd (module)']}."""
    usage: dict[str, list[str]] = {}
    for meta in workspace.list_campaigns():
        for slug in meta.sources:
            role = "rules/system" if slug == meta.system_source else "rules"
            usage.setdefault(slug, []).append(f"{meta.slug} ({role})")
        for module in meta.modules:
            usage.setdefault(module.slug, []).append(f"{meta.slug} (module)")
    return usage


def _detach_book(meta, slug: str) -> list[str]:
    """Remove ``slug`` from a campaign meta's rules sources and modules, in place,
    keeping the system/active pointers valid. Returns the roles it was detached
    from (``[]`` if the campaign wasn't using it)."""
    roles = []
    if slug in meta.sources:
        meta.sources = [s for s in meta.sources if s != slug]
        if meta.system_source == slug:
            meta.system_source = meta.sources[0] if meta.sources else None
        roles.append("rules source")
    if any(m.slug == slug for m in meta.modules):
        meta.modules = [m for m in meta.modules if m.slug != slug]
        for index, module in enumerate(meta.modules):
            module.order = index
        if meta.active_module == slug:
            nxt = next((m for m in meta.modules if m.status != "completed"), None)
            nxt = nxt or (meta.modules[0] if meta.modules else None)
            meta.active_module = nxt.slug if nxt else None
            if nxt is not None and nxt.status == "pending":
                nxt.status = "active"
        roles.append("module")
    return roles


def _rewrite_book_slug(meta, old: str, new: str) -> bool:
    """Repoint a campaign meta's references from book slug ``old`` to ``new``, in
    place (rules sources, system source, modules, active module). Module titles
    are left untouched. Returns whether anything changed."""
    changed = False
    if old in meta.sources:
        meta.sources = [new if s == old else s for s in meta.sources]
        changed = True
    if meta.system_source == old:
        meta.system_source = new
        changed = True
    for module in meta.modules:
        if module.slug == old:
            module.slug = new
            changed = True
    if meta.active_module == old:
        meta.active_module = new
        changed = True
    return changed


def _rename_book(workspace, args, console) -> int:
    from openadventure.store.workspace import slugify

    old = slugify(args.name)
    new = slugify(args.rename)
    src = workspace.book_dir(old)
    if not src.is_dir():
        console.print(f"[red]No ingested book named {args.name!r}.[/red]")
        return 1
    if new == old:
        console.print(f"[yellow]{old!r} is already named that.[/yellow]")
        return 0
    if workspace.book_dir(new).exists():
        console.print(f"[red]A book named {new!r} already exists in the store.[/red]")
        return 1

    # Move the store dir first, then repoint every campaign that referenced it.
    # The slug is the only link between a campaign and its books, so renaming the
    # folder alone would orphan them.
    src.rename(workspace.book_dir(new))
    updated = []
    for meta in workspace.list_campaigns():
        if _rewrite_book_slug(meta, old, new):
            workspace.campaign(meta.slug).save_meta(meta)
            updated.append(meta.slug)

    console.print(f"[green]Renamed[/green] book [cyan]{old}[/cyan] to [cyan]{new}[/cyan].")
    if updated:
        console.print(f"[dim]Updated references in: {', '.join(updated)}.[/dim]")
    return 0


def _delete_book(workspace, args, console) -> int:
    import shutil

    from openadventure.store.workspace import slugify

    slug = slugify(args.name)
    dest = workspace.book_dir(slug)
    if not dest.is_dir():
        console.print(f"[red]No ingested book named {args.name!r}.[/red]")
        return 1
    users = _book_usage(workspace).get(slug, [])
    if not args.yes:
        console.print(
            f"This permanently deletes the ingested book [cyan]{slug}[/cyan] from the store."
        )
        if users:
            console.print(f"[yellow]In use by:[/yellow] {', '.join(users)} (it will be detached).")
        if input("Delete it? [y/N] ").strip().lower() not in ("y", "yes"):
            console.print("Cancelled.")
            return 0

    detached = []
    for meta in workspace.list_campaigns():
        if _detach_book(meta, slug):
            workspace.campaign(meta.slug).save_meta(meta)
            detached.append(meta.slug)

    shutil.rmtree(dest)
    console.print(f"[green]Deleted[/green] book [cyan]{slug}[/cyan].")
    if detached:
        console.print(f"[dim]Detached from: {', '.join(detached)}.[/dim]")
    return 0


def _cmd_books(args: argparse.Namespace) -> int:
    from rich.table import Table

    from openadventure.cli.term import make_console
    from openadventure.config import load_config
    from openadventure.store import snapshots
    from openadventure.store.workspace import Workspace

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)

    if args.delete and args.rename:
        console.print("[red]Use --delete or --rename, not both.[/red]")
        return 1
    if args.delete:
        if not args.name:
            console.print(
                "[red]Specify a book to delete: openadventure books --delete <name>.[/red]"
            )
            return 1
        return _delete_book(workspace, args, console)
    if args.rename:
        if not args.name:
            console.print(
                "[red]Specify the book to rename: openadventure books <name> --rename <new>.[/red]"
            )
            return 1
        return _rename_book(workspace, args, console)
    if args.name:
        console.print(
            f"[red]Nothing to do with {args.name!r}.[/red] Add --delete or --rename, or run "
            "[green]openadventure books[/green] to list."
        )
        return 1

    available = workspace.list_books()
    if not available:
        console.print(
            "No books ingested yet. Add one with: [green]openadventure ingest <file>[/green]"
        )
        return 0
    usage = _book_usage(workspace)
    type_label = {"source": "rules", "module": "adventure"}
    table = Table("book", "type", "sections", "template", "used by")
    for name in available:
        manifest = snapshots.load_json(workspace.book_dir(name) / "manifest.json") or {}
        has_template = (workspace.book_dir(name) / "templates" / "character.json").is_file()
        table.add_row(
            name,
            type_label.get(manifest.get("type"), "[dim]either[/dim]"),
            str(manifest.get("section_count", "?")),
            "yes" if has_template else "-",
            ", ".join(usage.get(name, [])) or "[dim]unused[/dim]",
        )
    console.print(table)
    return 0


def _ensure_default_config(config) -> None:
    from openadventure.config import DEFAULT_CONFIG_TOML

    path = config.workspace_dir / "config.toml"
    if not path.is_file():
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")


def _pick_campaign(console, workspace) -> str | None:
    metas = workspace.list_campaigns()
    if not metas:
        return None
    if len(metas) == 1:
        return metas[0].slug
    console.print("[bold]Campaigns:[/bold]")
    for i, m in enumerate(metas, 1):
        console.print(f"  {i}. [cyan]{m.slug}[/cyan]: {m.name} [dim]({m.mode})[/dim]")
    while True:
        choice = input("Pick a campaign (number or slug): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(metas):
            return metas[int(choice) - 1].slug
        for m in metas:
            if m.slug == choice:
                return m.slug
        console.print("[red]Not a valid choice.[/red]")


def _cmd_play(args: argparse.Namespace) -> int:
    from openadventure.cli.firstrun import ensure_api_key
    from openadventure.cli.media_host import LocalMediaHost
    from openadventure.cli.repl import Repl
    from openadventure.cli.term import make_console
    from openadventure.cli.wizard import run_setup_wizard
    from openadventure.config import load_config
    from openadventure.engine.session import GameSession
    from openadventure.store.workspace import Workspace

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)
    workspace.ensure()
    _ensure_default_config(config)

    slug = args.slug or _pick_campaign(console, workspace)
    if slug is None:
        console.print(
            "No campaigns in this workspace yet. Create one with: [green]openadventure new <name>[/green]"
        )
        return 1
    try:
        campaign = workspace.campaign(slug)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    # Self-knowledge for read_docs: the README plus this frontend's own --help
    # and slash commands, so the GM can answer out-of-character questions about
    # the app from the real thing instead of guessing.
    from openadventure.cli.repl import commands_help_text
    from openadventure.engine.self_knowledge import build_docs

    docs = build_docs(cli_help=build_parser().format_help(), slash_help=commands_help_text())

    # The console presents media locally (speakers + the default image viewer).
    session = GameSession(
        config,
        workspace,
        campaign,
        provider=None,
        media_host=LocalMediaHost.from_config(config.media),
        docs=docs,
    )
    first_run = not session.has_prior_play and not session.meta.setup_done
    if first_run and sys.stdin.isatty():
        # The wizard onboards the API key (and audio) for a fresh campaign.
        run_setup_wizard(console, session, first_run=True)
    else:
        provider_name = session.provider_name()  # the model picks the backend
        api_key = ensure_api_key(console, config, provider_name)
        if api_key:
            from openadventure.providers.factory import build_provider

            session.provider = build_provider(provider_name, api_key, session.models)

    repl = Repl(console, session, debug=getattr(args, "debug", False))
    _run_repl(repl)
    return 0


def _run_repl(repl) -> None:
    """Drive the REPL so Ctrl+C interrupts the AI instead of killing the session.

    On Windows prompt_toolkit can't route SIGINT through the asyncio loop, so a
    Ctrl+C raises KeyboardInterrupt straight out of ``run_until_complete``; it
    never reaches the REPL's own try/excepts and would unwind out of
    ``asyncio.run``, ending the campaign. So we run the loop ourselves: catch the
    KeyboardInterrupt where it actually lands (outside ``run_until_complete``),
    interrupt whatever is thinking or playing, and resume the *same* REPL task,
    which is merely suspended, not dead.
    """
    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        main_task = loop.create_task(repl.run())
        while True:
            try:
                loop.run_until_complete(main_task)
                return
            except KeyboardInterrupt:
                repl.interrupt()
                if main_task.done():
                    return
            except asyncio.CancelledError:
                return


def _cmd_setup(args: argparse.Namespace) -> int:
    from openadventure.cli.term import make_console
    from openadventure.cli.wizard import run_setup_wizard
    from openadventure.config import load_config
    from openadventure.engine.session import GameSession
    from openadventure.store.workspace import Workspace

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)
    workspace.ensure()
    _ensure_default_config(config)

    slug = args.slug or _pick_campaign(console, workspace)
    if slug is None:
        console.print("No campaigns yet. Create one with: [green]openadventure new <name>[/green]")
        return 1
    try:
        campaign = workspace.campaign(slug)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    session = GameSession(config, workspace, campaign, provider=None)
    run_setup_wizard(console, session)
    session.close()
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from pathlib import Path

    from openadventure.cli.progress import ingest_progress
    from openadventure.cli.term import make_console
    from openadventure.config import load_config
    from openadventure.ingest import embeddings, pipeline
    from openadventure.store.workspace import Workspace, slugify

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)
    workspace.ensure()
    source = Path(args.file)
    embed_backend, embed_reason = (None, None)
    if not args.no_embeddings:
        embed_backend, embed_reason = embeddings.try_load_backend(config.embeddings)
    if embed_reason:
        console.print(
            f"[yellow]Semantic search off:[/yellow] {embed_reason}\n"
            "[dim]Building FTS5 + cross-refs only.[/dim]"
        )

    pages: tuple[int, int] | None = None
    if args.pages:
        try:
            start_str, _, end_str = args.pages.partition("-")
            pages = (int(start_str), int(end_str or start_str))
        except ValueError:
            console.print(f"[red]Invalid --pages {args.pages!r}; use e.g. 18-32.[/red]")
            return 1

    # The book's type (--source/--module) is recorded in its manifest, so a
    # campaign can only attach it in the matching bucket. It still lands in the
    # one shared store; the type just gates how it can be attached later.
    source_name = slugify(args.name or source.stem)
    dest = workspace.book_dir(source_name)
    type_label = "rules source" if args.as_type == "source" else "adventure module"
    label = f"{type_label} [cyan]{source_name}[/cyan]"

    if pages:
        label += f" [dim](pages {pages[0]}-{pages[1]})[/dim]"
    console.print(f"Ingesting [bold]{source.name}[/bold] as {label}…")
    try:
        with ingest_progress(console) as progress:
            manifest = pipeline.ingest(
                source,
                dest,
                pages=pages,
                book_type=args.as_type,
                embed_backend=embed_backend,
                progress=progress,
            )
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    report = pipeline.index_report(dest)
    extra = f", {report['entities']} cross-ref entities"
    if report["windows"]:
        extra += f", {report['windows']} embedding windows ({report['embed_model']})"
    console.print(
        f"[green]Done[/green]: {manifest['section_count']} sections{extra}, "
        f"index at [dim]{dest / 'index.sqlite'}[/dim]"
    )
    if args.as_type == "source":
        console.print(
            f"[dim]Attach it to a campaign as a rules source: "
            f"[green]new --source {source_name}[/green] or [green]/sources add {source_name}[/green]."
            "[/dim]"
        )
    else:
        console.print(
            f"[dim]Attach it to a campaign as an adventure module: "
            f"[green]modules <campaign> --add {source_name}[/green] or "
            f"[green]/modules add {source_name}[/green].[/dim]"
        )
    if manifest.get("warning"):
        console.print(f"[yellow]Warning:[/yellow] {manifest['warning']}")
        console.print(
            "[dim]Run [bold]openadventure inspect[/bold] on the file to see what was extracted.[/dim]"
        )
    note = pipeline.image_only_pages_note(manifest)
    if note:
        console.print(f"[yellow]Heads up:[/yellow] {note}")
    if args.as_type == "source" and sys.stdin.isatty():
        template_path = dest / "templates" / "character.json"
        if not template_path.is_file():
            from openadventure.cli.wizard import offer_template_generation

            offer_template_generation(console, config, source_name, dest)
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    import sys
    from pathlib import Path

    from openadventure.ingest import inspect

    source = Path(args.file)
    if not source.is_file():
        print(f"No such file: {source}")
        return 1
    if source.suffix.lower() != ".pdf":
        print("Inspect only supports PDFs (it diagnoses page layout extraction).")
        return 1
    if args.tables:
        report = inspect.tables(source, args.page)
    elif args.page is not None:
        report = inspect.page(source, args.page)
    elif args.bodies:
        report = inspect.bodies(source)
    else:
        report = inspect.summary(source)
    # reports echo arbitrary PDF text; write tolerantly so a stray glyph the
    # console can't encode doesn't abort the dump
    encoding = sys.stdout.encoding or "utf-8"
    sys.stdout.buffer.write((report + "\n").encode(encoding, errors="replace"))
    return 0


def _cmd_restart(args: argparse.Namespace) -> int:
    from openadventure.cli.term import make_console
    from openadventure.config import load_config
    from openadventure.engine.timeline import restart_campaign
    from openadventure.store.workspace import Workspace

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)
    try:
        campaign = workspace.campaign(args.slug)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    what = (
        "characters go back to their as-created sheets"
        if args.characters == "original"
        else "characters are cleared so you can roll new ones"
    )
    if not args.yes:
        console.print(
            f"This archives the whole story of [cyan]{args.slug}[/cyan] and starts fresh; {what}."
        )
        answer = input("Restart this campaign? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            console.print("Cancelled.")
            return 1
    report = restart_campaign(campaign, characters=args.characters)
    if report.missing_originals:
        console.print(
            f"[yellow]No original sheet for: {', '.join(report.missing_originals)}; "
            "rested to full instead.[/yellow]"
        )
    if report.rerolled:
        console.print(
            f"[green]Campaign restarted.[/green] Cleared {len(report.rerolled)} character(s) "
            f"({', '.join(report.rerolled)}). Make a new party to begin. "
            f"Old story archived to [dim]{report.archive_dir}[/dim]."
        )
        return 0
    party = ", ".join(report.pcs) if report.pcs else "no characters yet"
    console.print(
        f"[green]Campaign restarted.[/green] Party: {party}. "
        f"Old story archived to [dim]{report.archive_dir}[/dim]."
    )
    return 0


def _cmd_template(args: argparse.Namespace) -> int:
    from openadventure.cli.term import make_console
    from openadventure.cli.wizard import run_template_wizard, template_progress_reporter
    from openadventure.config import load_config
    from openadventure.ingest import pipeline, template_gen
    from openadventure.store.workspace import BookTypeMismatch, Workspace, ensure_book_type

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)
    source_dir = workspace.book_dir(args.source)
    if not pipeline.is_ingested(source_dir):
        console.print(
            f"[red]Source {args.source!r} isn't ingested yet (openadventure ingest …).[/red]"
        )
        return 1
    try:
        ensure_book_type(workspace, args.source, "source")
    except BookTypeMismatch as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    prepared = run_template_wizard(console, config, args.source, source_dir=source_dir)
    if prepared is None:
        return 1
    provider, settings = prepared
    console.print(f"Deriving a character template for [cyan]{args.source}[/cyan]…")
    with console.status("The agent is reading the character creation rules…") as status:
        template = asyncio.run(
            template_gen.derive_template(
                provider,
                settings,
                source_dir,
                args.source,
                on_progress=template_progress_reporter(status),
            )
        )
    if template is None:
        console.print("[red]The agent didn't produce a template. Try again.[/red]")
        return 1
    console.print(
        f"[green]Saved[/green] {len(template['fields'])} fields, "
        f"{len(template['resources'])} resources to "
        f"[dim]{source_dir / 'templates' / 'character.json'}[/dim]"
    )
    return 0


def _reindex_targets(workspace, args, console) -> list[tuple[str, object]] | None:
    """Resolve (label, dest) pairs to rebuild, or None on a usage error."""
    from openadventure.ingest import pipeline

    targets: list[tuple[str, object]] = []
    if args.all:
        # both rules sources and modules live in the one library store
        for book in workspace.list_books():
            targets.append((f"book {book}", workspace.book_dir(book)))
        return targets
    if args.campaign:
        try:
            camp = workspace.campaign(args.campaign)
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            return None
        meta = camp.load_meta()
        slugs = list(dict.fromkeys([*meta.sources, *(m.slug for m in meta.modules)]))
        for slug in slugs:
            dest = workspace.book_dir(slug)
            if pipeline.is_ingested(dest):
                targets.append((f"book {slug}", dest))
        if not targets:
            console.print(f"[red]Campaign {args.campaign!r} uses no ingested books.[/red]")
            return None
        return targets
    if args.book:
        dest = workspace.book_dir(args.book)
        if not (dest / "sections").is_dir():
            console.print(f"[red]No ingested book named {args.book!r}.[/red]")
            return None
        return [(f"book {args.book}", dest)]
    console.print("[red]Specify a book, --campaign <slug>, or --all.[/red]")
    return None


def _cmd_reindex(args: argparse.Namespace) -> int:
    from openadventure.cli.progress import ingest_progress
    from openadventure.cli.term import make_console
    from openadventure.config import load_config
    from openadventure.ingest import embeddings, pipeline
    from openadventure.store.workspace import Workspace

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)

    backend, embed_reason = (None, None)
    if not args.no_embeddings:
        backend, embed_reason = embeddings.try_load_backend(config.embeddings)
    if embed_reason:
        console.print(
            f"[yellow]Semantic search off:[/yellow] {embed_reason}\n"
            "[dim]Rebuilding FTS5 + cross-refs only.[/dim]"
        )

    targets = _reindex_targets(workspace, args, console)
    if targets is None:
        return 1
    if not targets:
        console.print("[yellow]Nothing ingested to reindex.[/yellow]")
        return 0

    for label, dest in targets:
        console.print(f"Reindexing [cyan]{label}[/cyan]…")
        with ingest_progress(console) as progress:
            pipeline.reindex(dest, embed_backend=backend, progress=progress)
        report = pipeline.index_report(dest)
        line = (
            f"[green]Reindexed[/green] [cyan]{label}[/cyan]: {report['sections']} sections, "
            f"{report['entities']} entities, {report['edges']} cross-refs"
        )
        if report["windows"]:
            line += f", {report['windows']} windows ([dim]{report['embed_model']}[/dim])"
        console.print(line)
        if report["dangling"]:
            console.print(
                f"  [yellow]⚠ {report['dangling']} cross-ref target(s) point at a missing "
                "section[/yellow]"
            )
    return 0


def _cmd_migrate_logs(args: argparse.Namespace) -> int:
    from openadventure.cli.term import make_console
    from openadventure.config import load_config
    from openadventure.store.migrations import backfill_tool_content
    from openadventure.store.workspace import Workspace

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)

    if args.campaign:
        try:
            campaigns = [workspace.campaign(args.campaign)]
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
    elif args.all:
        campaigns = [workspace.campaign(m.slug) for m in workspace.list_campaigns()]
    else:
        console.print("[red]Specify a campaign slug or --all.[/red]")
        return 1

    total = 0
    for campaign in campaigns:
        slug = campaign.load_meta().slug
        count = backfill_tool_content(workspace, campaign)
        total += count
        console.print(f"[cyan]{slug}[/cyan]: backfilled {count} tool result(s)")
    console.print(f"Done. {total} entr{'y' if total == 1 else 'ies'} updated.")
    return 0


def _cmd_sources(args: argparse.Namespace) -> int:
    from openadventure.cli.term import make_console
    from openadventure.config import load_config
    from openadventure.store.workspace import Workspace, slugify

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)
    try:
        campaign = workspace.campaign(args.campaign)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    meta = campaign.load_meta()
    ingested = set(workspace.list_books())
    # drop attached sources whose book has left the store (display only; persisted
    # alongside an explicit change, mirroring how modules sync)
    meta.sources = [s for s in meta.sources if s in ingested]
    if meta.system_source is not None and meta.system_source not in meta.sources:
        meta.system_source = meta.sources[0] if meta.sources else None
    changed = False

    def _resolve_attachable(value: str) -> str | None:
        """Slugify ``value`` and verify it can attach as a rules source, printing
        an error and returning None if not."""
        slug = slugify(value)
        if slug not in ingested:
            available = ", ".join(sorted(workspace.list_books("source"))) or "(none)"
            console.print(
                f"[red]No ingested book named {value!r}.[/red] Ingest it first with "
                "[green]openadventure ingest <file> --source[/green]; available sources: "
                f"{available}."
            )
            return None
        if workspace.book_type(slug) == "module":
            console.print(
                f"[red]{slug!r} was ingested as an adventure module, so it can't be attached as "
                "a rules source.[/red] Re-ingest it with --source to use it that way."
            )
            return None
        return slug

    if args.add:
        slug = _resolve_attachable(args.add)
        if slug is None:
            return 1
        if slug not in meta.sources:
            meta.sources.append(slug)
            if meta.system_source is None:
                meta.system_source = slug
        changed = True

    if args.remove:
        slug = slugify(args.remove)
        if slug not in meta.sources:
            console.print(f"[red]Source {args.remove!r} isn't attached to this campaign.[/red]")
            return 1
        meta.sources = [s for s in meta.sources if s != slug]
        if meta.system_source == slug:
            meta.system_source = meta.sources[0] if meta.sources else None
        changed = True

    if args.system:
        slug = slugify(args.system)
        if slug not in meta.sources:
            slug = _resolve_attachable(args.system)
            if slug is None:
                return 1
            meta.sources.append(slug)
        meta.system_source = slug
        changed = True

    if changed:
        campaign.save_meta(meta)

    if not meta.sources:
        available = ", ".join(sorted(workspace.list_books("source"))) or "(none ingested yet)"
        console.print(
            f"Campaign [cyan]{meta.name}[/cyan] has no rules sources yet. Attach an ingested "
            f"source with: [green]openadventure sources {meta.slug} --add <name>[/green]\n"
            f"[dim]Available sources: {available}. Ingest more with "
            "openadventure ingest <file> --source.[/dim]"
        )
        return 0

    console.print(f"[bold]Rules sources in [cyan]{meta.name}[/cyan]:[/bold]")
    for index, slug in enumerate(meta.sources, 1):
        marker = " [bold yellow](system)[/bold yellow]" if slug == meta.system_source else ""
        console.print(f"  {index}. [cyan]{slug}[/cyan]{marker}")
    return 0


def _cmd_modules(args: argparse.Namespace) -> int:
    from openadventure.cli.term import make_console
    from openadventure.config import load_config
    from openadventure.store.workspace import ModuleRef, Workspace, slugify, titleize

    console = make_console()
    config = load_config(args.workspace)
    workspace = Workspace(config.workspace_dir)
    try:
        campaign = workspace.campaign(args.campaign)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    meta = campaign.load_meta()
    ingested = set(workspace.list_books())
    campaign.sync_modules(meta, ingested)  # drop modules whose source is gone
    changed = False

    if args.add:
        slug = slugify(args.add)
        if slug not in ingested:
            available = ", ".join(sorted(workspace.list_books("module"))) or "(none)"
            console.print(
                f"[red]No ingested book named {args.add!r}.[/red] Ingest it first with "
                "[green]openadventure ingest <file> --module[/green]; available adventures: "
                f"{available}."
            )
            return 1
        declared = workspace.book_type(slug)
        if declared == "source":
            console.print(
                f"[red]{slug!r} was ingested as a rules source, so it can't be attached as an "
                "adventure module.[/red] Re-ingest it with --module to use it that way."
            )
            return 1
        if slug not in {m.slug for m in meta.modules}:
            order = max((m.order for m in meta.modules), default=-1) + 1
            meta.modules.append(ModuleRef(slug=slug, title=titleize(slug), order=order))
            if meta.active_module is None:
                meta.active_module = slug
        changed = True

    if args.remove:
        slug = slugify(args.remove)
        if slug not in {m.slug for m in meta.modules}:
            console.print(f"[red]Module {args.remove!r} isn't attached to this campaign.[/red]")
            return 1
        meta.modules = [m for m in meta.modules if m.slug != slug]
        for i, module in enumerate(meta.modules):
            module.order = i
        if meta.active_module == slug:
            nxt = next((m for m in meta.modules if m.status != "completed"), None)
            meta.active_module = nxt.slug if nxt else None
        changed = True

    if args.arc is not None:
        meta.arc = args.arc.strip() or None
        changed = True

    if args.reorder:
        wanted = [s.strip() for s in args.reorder.split(",") if s.strip()]
        known = {m.slug for m in meta.modules}
        unknown = [s for s in wanted if s not in known]
        if unknown:
            console.print(f"[red]Unknown module(s): {', '.join(unknown)}.[/red]")
            return 1
        rank = {slug: i for i, slug in enumerate(wanted)}
        meta.modules.sort(key=lambda m: (rank.get(m.slug, len(wanted)), m.order))
        for i, module in enumerate(meta.modules):
            module.order = i
        changed = True

    if args.activate:
        target = next((m for m in meta.modules if m.slug == args.activate), None)
        if target is None:
            known = ", ".join(m.slug for m in meta.modules) or "(none)"
            console.print(f"[red]No module named {args.activate!r}. Known: {known}.[/red]")
            return 1
        meta.active_module = target.slug
        target.status = "active"
        changed = True

    if changed:
        campaign.save_meta(meta)

    if not meta.modules:
        available = ", ".join(sorted(workspace.list_books("module"))) or "(none ingested yet)"
        console.print(
            f"Campaign [cyan]{meta.name}[/cyan] has no modules yet. Attach an ingested adventure "
            f"with: [green]openadventure modules {meta.slug} --add <name>[/green]\n"
            f"[dim]Available adventures: {available}. Ingest more with "
            "openadventure ingest <file> --module.[/dim]"
        )
        return 0

    if meta.arc:
        console.print(f"[bold]Arc:[/bold] {meta.arc}")
    console.print(f"[bold]Modules in [cyan]{meta.name}[/cyan]:[/bold]")
    markers = {
        "completed": "[green]done[/green]",
        "active": "[bold yellow]NOW PLAYING[/bold yellow]",
        "pending": "[dim]upcoming[/dim]",
    }
    for module in meta.modules:
        marker = markers.get(module.status, module.status)
        role = f": {module.role}" if module.role else ""
        console.print(
            f"  {module.order + 1}. {module.title} ([cyan]{module.slug}[/cyan]) {marker}{role}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openadventure", description="AI harness for running TTRPGs."
    )
    parser.add_argument("--workspace", help="workspace directory (default ./workspace)")
    sub = parser.add_subparsers(dest="command")

    play = sub.add_parser("play", help="play a campaign (default command)")
    play.add_argument("slug", nargs="?", help="campaign slug; omit to pick interactively")
    play.add_argument("--debug", action="store_true", help="show full tool calls and diagnostics")
    play.set_defaults(func=_cmd_play)

    new = sub.add_parser("new", help="create a new campaign")
    new.add_argument("name", help="campaign name")
    new.add_argument("--mode", choices=["gm", "assistant"], default="gm")
    new.add_argument(
        "--source",
        action="append",
        help="ingested source to attach as rules (see openadventure ingest); repeat for "
        "several, the first becomes the system source",
    )
    new.add_argument(
        "--module",
        action="append",
        help="ingested book to attach as an adventure module; repeat for several, the "
        "first is the one play starts in",
    )
    # premise and verbosity are set in play (/premise, /verbosity) or the setup wizard
    new.set_defaults(func=_cmd_new)

    campaigns = sub.add_parser("campaigns", help="list campaigns, or delete/rename/fork one")
    campaigns.add_argument("slug", nargs="?", help="campaign to delete, rename, or fork")
    action = campaigns.add_mutually_exclusive_group()
    action.add_argument(
        "--delete", action="store_true", help="delete the named campaign and its whole story"
    )
    action.add_argument("--rename", metavar="NEW", help="rename the campaign (and its slug) to NEW")
    action.add_argument(
        "--fork",
        metavar="NEW",
        help="copy the campaign to a new one named NEW, branching the story so far",
    )
    campaigns.add_argument("--yes", action="store_true", help="skip the delete confirmation")
    campaigns.set_defaults(func=_cmd_campaigns)

    books = sub.add_parser("books", help="list ingested books, or delete/rename one")
    books.add_argument("name", nargs="?", help="book to delete or rename")
    books.add_argument(
        "--delete",
        action="store_true",
        help="delete the named book from the store (detaching it from every campaign)",
    )
    books.add_argument(
        "--rename",
        metavar="NEW",
        help="rename the named book to NEW, updating every campaign that references it",
    )
    books.add_argument("--yes", action="store_true", help="skip the delete confirmation")
    books.set_defaults(func=_cmd_books)

    restart = sub.add_parser(
        "restart", help="start a campaign over (story archived; characters restored or rerolled)"
    )
    restart.add_argument("slug", help="campaign slug")
    restart.add_argument(
        "--characters",
        choices=["original", "reroll"],
        default="original",
        help="original = restore each PC to its as-created sheet; "
        "reroll = clear the party so new characters can be made (default: original)",
    )
    restart.add_argument("--yes", action="store_true", help="skip the confirmation question")
    restart.set_defaults(func=_cmd_restart)

    setup = sub.add_parser("setup", help="run the setup wizard (API keys, model, audio)")
    setup.add_argument("slug", nargs="?", help="campaign slug; omit to pick interactively")
    setup.set_defaults(func=_cmd_setup)

    roll = sub.add_parser("roll", help="roll dice, e.g. openadventure roll 4d6kh3")
    roll.add_argument("expression", nargs="+", help="dice expression like 6d6 or d20+5")
    roll.set_defaults(func=_cmd_roll)

    ingest = sub.add_parser(
        "ingest", help="ingest a book (rulebook or adventure) into the shared store (.pdf/.md/.txt)"
    )
    ingest.add_argument("file", help="source document")
    ingest.add_argument("--name", help="store it under this name (default: filename slug)")
    kind = ingest.add_mutually_exclusive_group(required=True)
    kind.add_argument(
        "--source",
        dest="as_type",
        action="store_const",
        const="source",
        help="ingest as a rules/reference source (rulebook, monster manual, setting guide); "
        "a campaign can attach it with new --source or /sources add",
    )
    kind.add_argument(
        "--module",
        dest="as_type",
        action="store_const",
        const="module",
        help="ingest as an adventure module; a campaign can attach it with new --module or "
        "/modules add",
    )
    ingest.add_argument(
        "--pages",
        metavar="START-END",
        help="only ingest this 1-based page range of a PDF (e.g. 18-32), for "
        "splitting a combined book into separate books",
    )
    ingest.add_argument(
        "--no-embeddings",
        action="store_true",
        help="build FTS5 + cross-refs only, skip the embedding index",
    )
    ingest.set_defaults(func=_cmd_ingest)

    sources = sub.add_parser("sources", help="list or manage a campaign's rules sources")
    sources.add_argument("campaign", help="campaign slug")
    sources.add_argument(
        "--add", metavar="NAME", help="attach an ingested book as a rules source for this campaign"
    )
    sources.add_argument(
        "--remove", metavar="SLUG", help="detach a rules source from this campaign"
    )
    sources.add_argument(
        "--system", metavar="SLUG", help="make this the system source (attaching it if needed)"
    )
    sources.set_defaults(func=_cmd_sources)

    modules = sub.add_parser("modules", help="list or manage a campaign's adventure modules")
    modules.add_argument("campaign", help="campaign slug")
    modules.add_argument(
        "--add", metavar="NAME", help="attach an ingested book as a module for this campaign"
    )
    modules.add_argument("--remove", metavar="SLUG", help="detach a module from this campaign")
    modules.add_argument("--activate", metavar="SLUG", help="set the module currently in play")
    modules.add_argument(
        "--reorder", metavar="A,B,C", help="set module order by comma-separated slugs"
    )
    modules.add_argument(
        "--arc", metavar="TEXT", help="set the overarching arc text (empty clears)"
    )
    modules.set_defaults(func=_cmd_modules)

    reindex = sub.add_parser(
        "reindex",
        help="rebuild search indexes (FTS5, cross-refs, embeddings) from stored markdown",
    )
    reindex.add_argument("book", nargs="?", help="book name (rules source or module)")
    reindex.add_argument("--campaign", help="reindex every book a campaign uses")
    reindex.add_argument("--all", action="store_true", help="reindex every ingested book")
    reindex.add_argument(
        "--no-embeddings",
        action="store_true",
        help="rebuild FTS5 + cross-refs only, skip the embedding index",
    )
    reindex.set_defaults(func=_cmd_reindex)

    migrate = sub.add_parser(
        "migrate-logs",
        help="backfill replayable tool-result content on existing campaign logs",
    )
    migrate.add_argument("campaign", nargs="?", help="campaign slug (omit with --all)")
    migrate.add_argument("--all", action="store_true", help="migrate every campaign")
    migrate.set_defaults(func=_cmd_migrate_logs)

    template = sub.add_parser(
        "template", help="derive a character-sheet template from an ingested source (uses the AI)"
    )
    template.add_argument("source", help="source name")
    template.set_defaults(func=_cmd_template)

    inspect = sub.add_parser("inspect", help="diagnose how a PDF extracts (debugging ingestion)")
    inspect.add_argument("file", help="source PDF")
    inspect.add_argument("--bodies", action="store_true", help="dump every section's full text")
    inspect.add_argument(
        "--page", type=int, metavar="N", help="raw line geometry + column split for page N"
    )
    inspect.add_argument(
        "--tables",
        action="store_true",
        help="table detection diagnostics (whole-doc census, or detail with --page N)",
    )
    inspect.set_defaults(func=_cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # bare `openadventure` -> play flow
        args = parser.parse_args([*argv, "play"])
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
