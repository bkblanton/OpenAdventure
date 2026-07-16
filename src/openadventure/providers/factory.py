"""Construct a provider backend by name, and hold per-backend metadata.

The backend is *derived from the model* (``ModelRegistry.provider_for``), not
chosen separately; the rest of the app picks a model and this maps that
model's provider name onto a concrete adapter and its API-key env vars.
"""

from __future__ import annotations

from openadventure.providers.base import ModelRegistry, Provider

# Per-backend metadata: API-key env vars (priority order) and onboarding copy.
PROVIDER_INFO: dict[str, dict[str, object]] = {
    "anthropic": {
        "label": "Anthropic",
        "env": ("ANTHROPIC_API_KEY",),
        "console": "https://console.anthropic.com",
    },
    "gemini": {
        "label": "Google AI",
        "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "console": "https://aistudio.google.com/apikey",
    },
    "openai": {
        "label": "OpenAI",
        "env": ("OPENAI_API_KEY",),
        "console": "https://platform.openai.com/api-keys",
    },
}

PROVIDER_NAMES = tuple(PROVIDER_INFO)


def api_key_env_vars(provider: str) -> tuple[str, ...]:
    """The env vars, in priority order, that hold ``provider``'s API key."""
    return tuple(PROVIDER_INFO.get(provider, {}).get("env", ()))  # type: ignore[arg-type]


def build_provider(name: str, api_key: str, registry: ModelRegistry | None = None) -> Provider:
    """Construct the adapter for backend ``name`` (as named by a model's
    ``provider`` field; see ``ModelRegistry.provider_for``)."""
    if name == "gemini":
        from openadventure.providers.gemini_provider import GeminiProvider

        return GeminiProvider(api_key, registry)
    if name == "anthropic":
        from openadventure.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key, registry)
    if name == "openai":
        from openadventure.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key, registry)
    raise ValueError(f"Unknown provider {name!r}; known backends: {', '.join(PROVIDER_NAMES)}.")
