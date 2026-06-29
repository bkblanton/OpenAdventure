# OpenAdventure

**Play any tabletop RPG, even when you don't have a Game Master.** OpenAdventure is an open-source AI harness that runs detailed TTRPGs in your terminal, so a table that's short a GM (or a solo player) can still sit down and play. It isn't here to replace Game Masters; it's here for the nights you don't have one.

Under the hood, an LLM plays the Game Master (or your co-GM) while deterministic Python owns the numbers: dice, character sheets, HP, conditions, initiative. Rulebooks and adventure modules are ingested once and searched during play, so the AI quotes the actual book instead of hallucinating the rules. Bring any system (D&D, Pathfinder, Call of Cthulhu, GURPS, FATE, CY_BORG) by ingesting its book; nothing is hard-coded to dungeons or to one game system.

## Two ways to play

- **GM mode** (default): the AI runs the campaign and you play. Secret rolls and GM-only canon stay hidden from you.
- **Assistant mode** (`openadventure new … --mode assistant`, or `/mode assistant`): *you* are the GM running a table; the AI answers rules questions, pulls module content (including secrets), rolls dice, tracks initiative and HP, and keeps the campaign canon. Everything is visible to you.

## Requirements

- [uv](https://docs.astral.sh/uv/). It fetches the right Python for you (3.14, pinned in `.python-version`), so you don't need it pre-installed.
- An API key for the model you play on. The default model runs on Google Gemini (`GEMINI_API_KEY` or `GOOGLE_API_KEY`); pick a Claude model to run on Anthropic instead (`ANTHROPIC_API_KEY`). The setup wizard prompts for whichever it needs, or set it ahead of time in `.env` or the environment to skip that prompt.
- Optional: an ElevenLabs key for audio and a Google key for images (both prompted when you turn that media on).

```powershell
uv sync
```

To play, bring your own rulebooks and adventure modules as `.pdf`, `.md`, or `.txt` files and ingest them (see [Detailed setup](#detailed-setup)).

## Quick start

These two commands are all you need. Setup wizards inside `new` and `play` ask about sources, modules, model, and preferences on the fly:

```powershell
uv run openadventure new "Saturday Night"   # prints the slug: saturday-night
uv run openadventure play saturday-night
```

Then just talk: *"roll me a dwarf fighter"*, *"I kick open the door"*, *"give us a random fight for our level"*. Quit any time; `openadventure play <slug>` resumes exactly where you left off and shows a recap.

Bare `uv run openadventure` drops you into the play flow and lets you pick a campaign interactively. `uv run openadventure campaigns` lists them.

## Detailed setup

The steps below are optional; the wizards handle everything either way. We recommend doing them before sitting down to play, because ingestion takes time and it's faster to have it done than to wait mid-session:

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
- **Undo & restart.** Every turn is checkpointed, so `/undo` rewinds the last ~30 turns completely (state and conversation), even across restarts. `/restart` archives the whole story and begins the campaign again, restoring or rerolling the party.
- **Optional media.** TTS narration and sound effects (ElevenLabs), background music, and scene illustrations (Google Gemini "Nano Banana"). Each is opt-in via its slash command, which prompts for the relevant API key, and backends are swappable in `workspace/config.toml`.
- **Tunable per campaign.** Every campaign starts on a fast, cheap default (Gemini 3.5 Flash, thinking off, 100k-token context) tuned for a snappy real-time table. Trade up for capability or depth with five knobs, each applied from the next turn: the AI model (`/model`), reasoning effort (`/effort`), extended thinking (`/thinking`), narration verbosity (`/verbosity`), and the context budget (`/context`).

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
