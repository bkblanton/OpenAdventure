"""The media presentation seam between the engine and a frontend.

Generation stays in the engine: the media backends call the generative APIs
(ElevenLabs, Gemini) and produce a file on disk, the same on every frontend. A
``MediaHost`` is what a frontend supplies to *present* that file (play the
audio, show the image, deliver a handout) and to declare, via
``MediaCapabilities``, which of those it can do at all.

That declaration is the point of the seam: a Discord or Telegram client might
present images and text handouts but have nowhere to play looping music, so it
sets ``music=False`` and the engine never offers the music tool to the GM or
spends an API call generating a track no one can hear. The Rich console sets
everything on and plays locally; a web frontend streams to the browser.

Presentation methods take an already-generated path; the host never calls a
generative API. Stage-2 work routes the backends' playback through here; this
module defines the contract and two ready hosts (terminal-agnostic ``NullMediaHost``
for headless/test runs, and whatever a frontend implements).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class MediaCapabilities:
    """What media surfaces a frontend can present. The engine intersects these
    with the per-campaign toggles and backend readiness, so a capability set to
    False hard-disables that media: its tool is never registered and nothing is
    generated for it."""

    speech: bool = False  # spoken narration (TTS)
    sound_effects: bool = False
    music: bool = False  # looping background music
    images: bool = False  # generated scene/character images
    handouts: bool = False  # text/document handouts to the player (reserved)

    @property
    def audio(self) -> bool:
        """Whether any audio surface is available at all."""
        return self.speech or self.sound_effects or self.music

    @classmethod
    def all(cls) -> MediaCapabilities:
        """Every surface on: the local Rich console, and the back-compat default
        when no host is supplied."""
        return cls(speech=True, sound_effects=True, music=True, images=True, handouts=True)

    @classmethod
    def none(cls) -> MediaCapabilities:
        """No media surfaces: a text-only frontend."""
        return cls()


@runtime_checkable
class MediaHost(Protocol):
    """A frontend's media presenter. Implementations present already-generated
    artifacts (a path on disk); they must never call a generative API."""

    @property
    def capabilities(self) -> MediaCapabilities: ...

    async def play_speech(self, path: Path) -> None:
        """Play a narration clip, awaiting until it finishes (so dialogue and
        sound-effect cues can be sequenced after it)."""
        ...

    async def play_sound_effect(self, path: Path) -> None: ...

    def play_music(
        self, path: Path, *, prompt: str = "", length_seconds: float | None = None
    ) -> None:
        """Start looping a track, replacing whatever is playing. Returns at once."""
        ...

    def stop_music(self) -> None: ...

    def set_music_volume(self, value: float) -> float:
        """Set the music volume (0.0-1.0); returns the clamped value."""
        ...

    def music_volume(self) -> float: ...

    def music_status_line(self) -> str | None:
        """A one-line description of the looping track, or None when silent."""
        ...

    def stop_audio(self) -> None:
        """Interrupt any speech / sound effect currently playing (music aside)."""
        ...

    def present_image(self, path: Path, *, caption: str = "") -> None: ...

    def present_handout(self, title: str, body: str, *, path: Path | None = None) -> None:
        """Deliver a text/document handout to the player (reserved feature)."""
        ...


class NullMediaHost:
    """A media host that presents nothing. Its capabilities default to all-off,
    so the engine offers no media tools and generates no media: the right host
    for headless runs, tests, and a text-only frontend. Pass an explicit
    ``capabilities`` to advertise surfaces a caller will drain from the event
    stream itself."""

    def __init__(self, capabilities: MediaCapabilities | None = None) -> None:
        self._capabilities = capabilities or MediaCapabilities.none()

    @property
    def capabilities(self) -> MediaCapabilities:
        return self._capabilities

    async def play_speech(self, path: Path) -> None:
        return None

    async def play_sound_effect(self, path: Path) -> None:
        return None

    def play_music(
        self, path: Path, *, prompt: str = "", length_seconds: float | None = None
    ) -> None:
        return None

    def stop_music(self) -> None:
        return None

    def set_music_volume(self, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    def music_volume(self) -> float:
        return 0.0

    def music_status_line(self) -> str | None:
        return None

    def stop_audio(self) -> None:
        return None

    def present_image(self, path: Path, *, caption: str = "") -> None:
        return None

    def present_handout(self, title: str, body: str, *, path: Path | None = None) -> None:
        return None
