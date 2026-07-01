"""Application configuration.

Load order (later wins for the same key): workspace/config.toml < .env < process env.
The workspace location itself comes from OPENADVENTURE_WORKSPACE or defaults to ./workspace.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

DEFAULT_WORKSPACE = "workspace"


class AppConfig(BaseModel):
    workspace_dir: Path
    model: str | None = None  # default model id; selects the backend too. None -> built-in default
    api_key: str | None = None  # from config.toml [auth]; env var preferred
    media: dict[str, Any] = Field(default_factory=dict)
    # [high_effort] overrides for out-of-game character-template derivation (the
    # CLI `openadventure template`/`ingest` paths, where no campaign model exists
    # to borrow). In-game, off-table work uses the campaign's table model instead.
    # Reads a legacy [template] section too.
    high_effort: dict[str, Any] = Field(default_factory=dict)
    embeddings: dict[str, Any] = Field(default_factory=dict)  # [embeddings] hybrid-search backend
    raw: dict[str, Any] = Field(default_factory=dict)  # full parsed config.toml


def load_config(workspace: str | Path | None = None) -> AppConfig:
    load_dotenv()  # project .env -> process env (no-op if absent)

    root = Path(workspace or os.environ.get("OPENADVENTURE_WORKSPACE") or DEFAULT_WORKSPACE)
    root = root.expanduser().resolve()

    raw: dict[str, Any] = {}
    config_path = root / "config.toml"
    if config_path.is_file():
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))

    return AppConfig(
        workspace_dir=root,
        model=raw.get("provider", {}).get("model"),
        api_key=raw.get("auth", {}).get("api_key"),
        media=raw.get("media", {}),
        high_effort=raw.get("high_effort", raw.get("template", {})),
        embeddings=raw.get("embeddings", {}),
        raw=raw,
    )


def resolve_api_key(config: AppConfig, provider: str) -> str | None:
    """API key for ``provider``, from env (includes .env) or config.toml
    ``[auth].api_key``.

    Anthropic reads ANTHROPIC_API_KEY; Gemini reads GEMINI_API_KEY then
    GOOGLE_API_KEY (matching the image backend)."""
    from openadventure.providers.factory import api_key_env_vars

    for var in api_key_env_vars(provider):
        value = os.environ.get(var)
        if value:
            return value
    return config.api_key


DEFAULT_CONFIG_TOML = """\
# openadventure workspace configuration

[provider]
# The model selects the backend automatically (see /model). Games default to
# claude-sonnet-5 (Anthropic backend, high effort, thinking on — for Claude
# models thinking maps to adaptive thinking). Set this to pin a different default
# model; tune effort/thinking/context per campaign at play time with /effort,
# /thinking, /context.
# model = "gemini-3.5-flash"    # claude-* -> Anthropic, gemini-* -> Gemini

# [auth]
# api_key = "sk-ant-..."        # prefer ANTHROPIC_API_KEY (or, for a gemini model,
#                               # GEMINI_API_KEY / GOOGLE_API_KEY) in env or .env

# [high_effort]                   # out-of-game character-template derivation only
#                                 # (the CLI `openadventure template`/`ingest` paths,
#                                 # where no campaign is loaded). The wizard offers
#                                 # this as the default and asks each run. In-game,
#                                 # off-table work (templates + the canon chronicler)
#                                 # uses the campaign's table model at high effort.
# model = "claude-sonnet-5"       # defaults: Claude Sonnet 5, thinking on at high
#                                 # effort. Off the real-time path. Same Anthropic
#                                 # key as the in-game default; pin a gemini-* model
#                                 # to run on Gemini.
# effort = "high"                 # low | medium | high | max
# thinking = true                 # deeper reasoning, fine here since it isn't real-time
# max_tokens = 32000

# [embeddings]                     # hybrid (semantic + keyword) rules/module search.
#                                  # 'local' is the default and turns on automatically
#                                  # once `uv sync --extra embeddings` is installed; it
#                                  # runs offline and falls back to keyword-only if not.
# backend = "local"               # local | none  (none = FTS5 keyword search only)
# model = "BAAI/bge-small-en-v1.5" # switching the model re-embeds on the next reindex
# cache_dir = ""                  # where the model is cached (default: ~/.cache/openadventure/models)
# model_path = ""                 # point at a pre-downloaded model dir to skip the HF
#                                  # download entirely (fully offline; see README)
# parallel = 0                    # fan embedding batches across worker processes for
#                                  # big corpora: 0 = all cores, N = N workers. Omit
#                                  # (single process) unless reindex is CPU-bound.

# [media]
# image_backend = "gemini"        # or "my_pkg.images:CustomImages", "off"
# google_api_key = ""             # prefer GOOGLE_API_KEY / GEMINI_API_KEY in env or .env
# image_model = "gemini-3.1-flash-image"  # "Nano Banana 2"
# image_aspect_ratio = ""         # e.g. "16:9", "1:1", "9:16" (blank = model default)
# music_backend = "elevenlabs"    # or "my_pkg.music:CustomMusic", "off"
# tts_backend = "elevenlabs"      # or "my_pkg.voice:CustomTTS"
# sound_effects_backend = "elevenlabs"
# elevenlabs_api_key = ""         # prefer ELEVENLABS_API_KEY in env or .env
# elevenlabs_voice_id = "6FiCmD8eY5VyjOdG5Zjk" # narrator fallback voice (also /voice)
# elevenlabs_model_id = "eleven_flash_v2_5"
# elevenlabs_sfx_model_id = "eleven_text_to_sound_v2"
# elevenlabs_music_model_id = "music_v1"
# music_volume = 0.2              # default loop volume, 0.0-1.0
# sfx_volume = 1.0                # default sound-effect volume, 0.0-1.0
# tts_volume = 1.0                # default narration volume, 0.0-1.0
# music_length_seconds = 120      # default generated track length before looping
"""


def _set_toml_string(text: str, table: str, key: str, value: str) -> str:
    """Set ``[table] key = "value"`` in TOML ``text``, preserving everything else.

    Patches an active ``key`` inside an active ``[table]`` when both exist,
    inserts the key under an existing header, or appends a fresh table. Commented
    lines (``# [table]``, ``# key = …``) are documentation and never matched, so
    the default config's example block stays intact. Deliberately minimal: every
    openadventure config key is a flat string scalar, so this earns its keep
    without a TOML-writer dependency."""
    encoded = f'{key} = "{value}"'
    header = f"[{table}]"
    lines = text.splitlines()

    def _is_active_header(line: str) -> bool:
        return line.strip() == header

    def _is_any_active_header(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("[") and stripped.endswith("]")

    def _is_active_key(line: str) -> bool:
        stripped = line.strip()
        if stripped.startswith("#"):
            return False
        name, sep, _ = stripped.partition("=")
        return sep == "=" and name.strip() == key

    trailing = "\n" if text.endswith("\n") else ""
    start = next((i for i, ln in enumerate(lines) if _is_active_header(ln)), None)
    if start is None:
        prefix = text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"  # blank line before the appended table
        return f"{prefix}{header}\n{encoded}\n"

    end = next(
        (i for i in range(start + 1, len(lines)) if _is_any_active_header(lines[i])),
        len(lines),
    )
    key_line = next((i for i in range(start + 1, end) if _is_active_key(lines[i])), None)
    if key_line is not None:
        lines[key_line] = encoded
    else:
        lines.insert(start + 1, encoded)
    return "\n".join(lines) + trailing


def set_high_effort_model(config: AppConfig, model_id: str) -> bool:
    """Persist the default model for out-of-game character-template derivation to
    the workspace ``config.toml`` ``[high_effort]`` table and update ``config`` in
    memory.

    This is the default the out-of-game wizard offers (and saves the pick back to)
    when no campaign is loaded; only the model is set here, so the other fields
    keep their accuracy-first defaults. Returns True if the file was changed, False
    when ``model_id`` was already configured (a no-op). Creates ``config.toml``
    from the documented default when missing.
    """
    if config.high_effort.get("model") == model_id:
        return False
    path = config.workspace_dir / "config.toml"
    text = path.read_text(encoding="utf-8") if path.is_file() else DEFAULT_CONFIG_TOML
    path.write_text(_set_toml_string(text, "high_effort", "model", model_id), encoding="utf-8")
    # Reflect the change in the live config so the running session picks it up
    # (resolve_high_effort_settings reads config.high_effort, not the file).
    config.high_effort = {**config.high_effort, "model": model_id}
    config.raw.setdefault("high_effort", {})["model"] = model_id
    return True
