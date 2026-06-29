"""First-run setup: resolve an API key, offering to store it in .env."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from openadventure.config import AppConfig, resolve_api_key


def _read_key(prompt_text: str) -> str | None:
    """Read an API key with a visible echo, so a paste can be eyeballed before
    Enter. None on Ctrl+D / Ctrl+C (treated as skip)."""
    try:
        return input(prompt_text).strip()
    except EOFError, KeyboardInterrupt:
        return None


def _confirm_save_to_env() -> bool:
    """Ask whether to persist the key to .env; default yes, no on Ctrl+D / Ctrl+C."""
    try:
        raw = input("Save it to .env so you don't have to paste it again? [Y/n] ")
    except EOFError, KeyboardInterrupt:
        return False
    return raw.strip().lower() in ("", "y", "yes")


def prompt_and_store_key(
    console: Console,
    *,
    label: str,
    env_var: str,
    secret_prompt: Callable[[str], str | None] = _read_key,
    confirm_save: Callable[[], bool] = _confirm_save_to_env,
    set_env_on_decline: bool = True,
) -> str | None:
    """Prompt for a "{label} API key", then offer to persist it.

    ``secret_prompt`` reads the key (a falsy return means skip); ``confirm_save``
    decides whether to write it to .env. When the player declines, the key is put
    in ``os.environ`` if ``set_env_on_decline``, so the running process still sees
    it. The defaults are the plain visible-echo prompts; the setup wizard passes
    its own, which raise to cancel the whole wizard on Ctrl+D. Returns the key, or
    None when skipped.
    """
    key = secret_prompt(f"{label} API key: ")
    if not key:
        return None
    if confirm_save():
        save_env_key(console, env_var, key)
    elif set_env_on_decline:
        os.environ[env_var] = key
    return key


def ensure_api_key(console: Console, config: AppConfig, provider: str) -> str | None:
    """Return ``provider``'s API key, prompting interactively when it's missing.
    None = play without AI (slash commands only). Used on first run, on resume, by
    /model when switching backends, and by the template wizard."""
    from openadventure.providers.factory import PROVIDER_INFO

    key = resolve_api_key(config, provider)
    if key:
        return key
    if not sys.stdin.isatty():
        return None

    info = PROVIDER_INFO[provider]
    label, env_var, url = info["label"], info["env"][0], info["console"]
    console.print(
        f"[yellow]No {env_var} found.[/yellow] The AI Game Master needs a {label} key "
        f"(create one at {url}). Press Enter to skip and play dice-only."
    )
    # The caller builds the provider from the returned key, so on decline we leave
    # os.environ untouched (unlike the media keys, whose backends read it).
    return prompt_and_store_key(console, label=label, env_var=env_var, set_env_on_decline=False)


def _ensure_media_key(
    console: Console, *, check_vars: tuple[str, ...], save_var: str, label: str, intro: str
) -> str | None:
    """Resolve a media-service key (ElevenLabs, Google): return the first one
    already in the environment, else prompt and (on decline) stash it in os.environ
    so the backend picks it up this session."""
    for var in check_vars:
        existing = os.environ.get(var)
        if existing:
            return existing
    if not sys.stdin.isatty():
        return None
    console.print(intro)
    return prompt_and_store_key(console, label=label, env_var=save_var)


def ensure_elevenlabs_api_key(console: Console) -> str | None:
    """Return an ElevenLabs API key, prompting when TTS/SFX is enabled."""
    return _ensure_media_key(
        console,
        check_vars=("ELEVENLABS_API_KEY",),
        save_var="ELEVENLABS_API_KEY",
        label="ElevenLabs",
        intro=(
            "[yellow]No ELEVENLABS_API_KEY found.[/yellow] TTS narration needs an "
            "ElevenLabs API key. Press Enter to enable TTS without saving a key yet."
        ),
    )


def ensure_google_api_key(console: Console) -> str | None:
    """Return a Google AI (Gemini) API key, prompting when image generation is enabled."""
    return _ensure_media_key(
        console,
        check_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        save_var="GOOGLE_API_KEY",
        label="Google AI",
        intro=(
            "[yellow]No GOOGLE_API_KEY found.[/yellow] Image generation uses Google Gemini "
            "(create a key at https://aistudio.google.com/apikey). Press Enter to enable "
            "images without saving a key yet."
        ),
    )


def save_env_key(console: Console, name: str, value: str) -> None:
    os.environ[name] = value
    env_path = Path(".env")
    with open(env_path, "a", encoding="utf-8") as f:
        f.write(f"\n{name}={value}\n")
    _ensure_gitignored(env_path)
    console.print(f"[dim]Saved to {env_path.resolve()}[/dim]")


def _ensure_gitignored(env_path: Path) -> None:
    gitignore = Path(".gitignore")
    if not gitignore.is_file():
        return
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    if ".env" not in [line.strip() for line in lines]:
        with open(gitignore, "a", encoding="utf-8") as f:
            f.write("\n.env\n")
