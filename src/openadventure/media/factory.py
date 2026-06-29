"""Load media backends from config.toml [media] specs.

Point these at built-ins or your own implementations, e.g.:

    [media]
    image_backend = "gemini"   # or "my_pkg.sd_local:StableDiffusionBackend", "off"
    music_backend = "elevenlabs"
    tts_backend = "elevenlabs"
    sound_effects_backend = "elevenlabs"
    # or: music_backend = "my_pkg.music:CustomMusicBackend"
    # or: tts_backend = "my_pkg.voice:PiperTTS"
    # or: sound_effects_backend = "my_pkg.sfx:LocalSoundEffects"
"""

from __future__ import annotations

import importlib
from typing import Any


def _load(spec: str | None) -> Any | None:
    if not spec:
        return None
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise ValueError(f"media backend spec {spec!r} must look like 'package.module:ClassName'")
    module = importlib.import_module(module_name)
    return getattr(module, attr)()


def _load_image(media_config: dict[str, Any]) -> Any | None:
    spec = media_config.get("image_backend")
    if spec in (None, "", "gemini"):
        from openadventure.media.image import GeminiImageBackend

        return GeminiImageBackend.from_config(media_config)
    if spec in ("none", "off"):
        return None
    return _load(spec)


def _load_tts(media_config: dict[str, Any]) -> Any | None:
    spec = media_config.get("tts_backend")
    if spec in (None, "", "elevenlabs"):
        from openadventure.media.tts import ElevenLabsTTS

        return ElevenLabsTTS.from_config(media_config)
    if spec in ("none", "off"):
        return None
    return _load(spec)


def _load_sound_effects(media_config: dict[str, Any]) -> Any | None:
    spec = media_config.get("sound_effects_backend")
    if spec in (None, "", "elevenlabs"):
        from openadventure.media.sound_effects import ElevenLabsSoundEffects

        return ElevenLabsSoundEffects.from_config(media_config)
    if spec in ("none", "off"):
        return None
    return _load(spec)


def _load_music(media_config: dict[str, Any]) -> Any | None:
    spec = media_config.get("music_backend") or media_config.get("music_library")
    if spec in (None, "", "elevenlabs"):
        from openadventure.media.music import ElevenLabsMusic

        return ElevenLabsMusic.from_config(media_config)
    if spec in ("none", "off"):
        return None
    return _load(spec)


def load_backends(
    media_config: dict[str, Any],
) -> tuple[Any | None, Any | None, Any | None, Any | None]:
    """Returns (image_backend, music_backend, tts_backend, sound_effects_backend)."""
    return (
        _load_image(media_config),
        _load_music(media_config),
        _load_tts(media_config),
        _load_sound_effects(media_config),
    )
