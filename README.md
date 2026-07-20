# OpenAdventure

**Play any tabletop RPG, even when you don't have a Game Master.** OpenAdventure is an open-source AI harness for running detailed TTRPG campaigns, so a table that's short a GM (or a solo player) can still sit down and play. It isn't here to replace Game Masters; it's here for the nights you don't have one.

Under the hood, an LLM plays the Game Master (or your co-GM) while deterministic Python owns the numbers: dice, character sheets, HP, conditions, initiative. Rulebooks and adventure modules are ingested once and searched during play, so the AI quotes the actual book instead of hallucinating the rules. Bring any system (D&D, Pathfinder, Call of Cthulhu, GURPS, FATE, CY_BORG) by ingesting its book; nothing is hard-coded to dungeons or to one game system.

## Quick start

The local web app is the fastest way to get started. You need:

- [uv](https://docs.astral.sh/uv/). It fetches the right Python for you (3.14, pinned in `.python-version`), so you don't need it pre-installed.
- An API key for the model you play on. The default model is GPT-5.6 Luna and uses OpenAI (`OPENAI_API_KEY`). The app prompts for whichever key the selected model needs, or you can set it ahead of time in `.env` or the environment.
- Optional: an ElevenLabs key for audio and a Google key for images (both prompted when you turn that media on).

```powershell
uv sync
uv run openadventure web
```

For a one-click start after installing `uv`, use the launcher for your system:

- **Windows:** double-click `launch-web.bat`.
- **macOS:** double-click `launch-web.command`.

Both launchers run from the project folder, prepare the environment through `uv`, start the web app, and open it in your default browser.

OpenAdventure opens `http://127.0.0.1:8000` in your browser. Use **Game library** to upload any rulebooks or adventures you want as `.pdf`, `.md`, or `.txt` files, then choose **New campaign**, attach your books, and start playing. The app prompts for missing API keys when it needs them and saves them only to your local `.env` file.

Use `--port` to choose another localhost port or `--no-open` to keep the browser from opening automatically:

```powershell
uv run openadventure web --port 8765 --no-open
```

The app stays on your machine and listens only on localhost. Prefer a terminal interface? See [Detailed setup](#detailed-setup).

## Two ways to play

- **GM mode** (default): the AI runs the campaign and you play. Secret rolls and GM-only canon stay hidden from you.
- **Assistant mode**: *you* are the GM running a table; the AI answers rules questions, pulls module content (including secrets), rolls dice, tracks initiative and HP, and keeps the campaign canon. Everything is visible to you. Choose this mode when creating a campaign, or switch with `/mode assistant`.

## Detailed setup

Everything in this section is also available in the web app. If you prefer the CLI, its setup wizards can create and configure a campaign as you go:

```powershell
uv run openadventure new "Saturday Night"   # prints the slug: saturday-night
uv run openadventure play saturday-night
```

Then just talk: *"roll me a dwarf fighter"*, *"I kick open the door"*, or *"give us a random fight for our level"*. Quit at any time; `openadventure play <slug>` resumes where you left off and shows a recap. Running `uv run openadventure` without a subcommand lets you choose a campaign interactively, while `uv run openadventure campaigns` lists them.

To prepare your books and campaign explicitly, follow the complete CLI workflow below. Ingestion can take time, so it is worth doing before a session:

```powershell
# 1. Ingest a rulebook into the shared store (one-time per book; .pdf, .md, or .txt).
#    The type flag is required: --source for a rulebook, --module for an adventure.
uv run openadventure ingest path/to/your-rulebook.pdf --name dnd5e --source

# 2. Derive that source's character-sheet template from its creation and
#    advancement chapters. Without one the GM improvises character creation and
#    leveling. One-time per system.
uv run openadventure template dnd5e

# 3. Create a campaign and attach dnd5e as its rules source.
uv run openadventure new "Curse of Strahd"
uv run openadventure sources curse-of-strahd --system dnd5e

# 4. Optional: ingest a published adventure and attach it as the campaign's module.
uv run openadventure ingest path/to/your-adventure.pdf `
    --name death-house --module
uv run openadventure modules curse-of-strahd --add death-house

# 5. Run setup to configure your model, API key, play mode, GM style, verbosity,
#    and optional audio/images -- after the steps above so it skips what's already
#    set. Each setting is changeable later with its own slash command (or /setup).
uv run openadventure setup curse-of-strahd

# 6. Play.
uv run openadventure play curse-of-strahd
```

`openadventure new` prints the campaign's slug (`"Curse of Strahd"` → `curse-of-strahd`); use it for the later commands.

## Features

- **The AI can't fudge the numbers.** Every roll goes through the dice engine, every HP change and sheet edit through a tool, all logged. Dice you roll physically at the table are accepted as-is.
- **Grounded in the real book.** Ingested rulebooks and adventures are split into sections and indexed; during play the GM searches them and quotes the actual text instead of relying on its memory. Ingestion also cross-links each section to the monsters and spells it names, so reading an encounter pulls the referenced stat blocks inline. Optional local embeddings add semantic search fused with keyword results, so a query finds the right rule even in different words (`uv sync --extra embeddings`); a small model downloads once, then runs cached and offline, falling back to keyword-only if unavailable.
- **Runs the whole table.** Beyond rules lookups: initiative-ordered encounters with HP tracking, visible or hidden progress clocks for off-screen threats, and a yes/no oracle and random tables for improvised outcomes. All deterministic and logged, same as the dice.
- **Remembers the campaign.** A background "chronicler" reads each stretch of play as it scrolls out of the live context window and distills the durable facts into a structured canon that's fed back into every turn, so the GM stays consistent over a long campaign without holding the whole transcript in memory. Canon entries can be GM-only secrets or pinned as the campaign's spine so they are never dropped, and they carry across modules.
- **Bring any system.** Ingest any game's book into one shared library, typed at ingestion as a **rules source** (rulebook, monster manual, setting guide) or an **adventure module**, so the two can never be confused. A campaign can attach several sources at once, one flagged as the "system" source that defines the rules and character template. Derive a character-sheet template from a source's creation and advancement chapters so PC creation, leveling, and higher-level builds follow the book (without one the GM improvises); that one-time job runs off the table at high effort.
- **Multi-module campaigns.** Chain several adventures into one arc; party, sheets, canon, and story summary carry across all of them. Only the module marked **NOW PLAYING** is canonical, so an unreached module can't leak its twists. The same ingested adventure can be a module in any number of campaigns.
- **Undo & restart.** Every turn is checkpointed, so you can rewind the last ~30 turns completely (state and conversation), even across restarts. You can also archive the whole story and begin the campaign again, restoring or rerolling the party. Use the web app controls or `/undo` and `/restart` in the CLI.
- **Optional media.** TTS narration and sound effects (ElevenLabs), background music, and scene illustrations (Google Gemini "Nano Banana"). Each is opt-in through the web app's campaign settings or a CLI slash command, which prompts for the relevant API key. Backends are swappable in `workspace/config.toml`.
- **Tunable per campaign.** Every campaign starts on a capable default (GPT-5.6 Luna at high effort, thinking on, with a 100k-token context) tuned for depth at the table. Dial in the AI model, reasoning effort, thinking, narration verbosity, and context budget from the web app's campaign settings or the corresponding CLI slash commands.

## Getting help

```powershell
uv run openadventure --help            # all subcommands
uv run openadventure ingest --help     # options for any subcommand
```

In-game, type `/help` for the full slash-command list.

## License

OpenAdventure is released under the [MIT License](LICENSE).

## Development

```powershell
uv run pytest            # test suite
uv run ruff check .      # lint
```
