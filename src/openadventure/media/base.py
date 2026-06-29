"""Media generation protocols.

A backend *generates* media by calling a provider's API and returning a file on
disk; a ``MediaHost`` (see ``media/host.py``) *presents* that file. These two
concerns are split so the generation runs identically on every frontend while
presentation is the frontend's job; a backend never plays audio or opens an
image itself.

v1 ships ElevenLabs (audio) and Gemini (images) backends; implementing one of
these protocols and naming it in config.toml [media] is all an alternative needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ImageBackend(Protocol):
    async def generate(
        self,
        subject: str,
        description: str,
        *,
        reference_images: list[Path] | None = None,
    ) -> Path:
        """Render an image and return the path to the saved file.

        ``reference_images`` are existing image files used to guide the look
        (e.g. keep a recurring NPC visually consistent)."""
        ...


@runtime_checkable
class MusicBackend(Protocol):
    async def generate(
        self,
        prompt: str,
        *,
        length_seconds: float | None = None,
        allow_vocals: bool = False,
    ) -> Any:
        """Generate (or fetch from cache) a track for the prompt and return it
        (a ``MusicTrack`` with ``.path`` and ``.length_seconds``). The host loops
        the returned file; the backend does not play it."""
        ...


@runtime_checkable
class TTSBackend(Protocol):
    async def synthesize(self, text: str, *, voice_id: str | None = None) -> Path | None:
        """Synthesize ``text`` to a cached audio file and return its path (None
        when the text has nothing speakable). The host plays the file."""
        ...


@runtime_checkable
class SoundEffectsBackend(Protocol):
    async def generate(
        self,
        description: str,
        *,
        duration_seconds: float | None = None,
        prompt_influence: float | None = None,
        loop: bool = False,
    ) -> Path:
        """Generate a one-shot sound effect and return the audio path. The host
        plays it."""
        ...
