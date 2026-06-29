"""The Rich console's MediaHost: present media on the local machine.

Plays audio through whatever player the host OS has (the players moved here from
the backends in spirit; they live in media/ but are driven only from a host)
and opens images in the default viewer. A localhost web or Discord frontend
would implement its own MediaHost instead; this one assumes local speakers and a
local display.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from openadventure.media.host import MediaCapabilities
from openadventure.media.music import (
    DEFAULT_MUSIC_VOLUME,
    LoopingAudioPlayer,
    MusicTrack,
)
from openadventure.media.sound_effects import DEFAULT_SFX_VOLUME
from openadventure.media.tts import DEFAULT_TTS_VOLUME, LocalAudioPlayer


def open_image_file(path: str | Path) -> bool:
    """Open an image in the OS default viewer. Returns False if the file is
    missing; swallows launch errors (a missing viewer must never break play)."""
    target = Path(path)
    if not target.is_file():
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            subprocess.Popen(
                ["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
    except OSError:
        pass
    return True


class LocalMediaHost:
    """Present generated media locally: speakers for audio, the default viewer
    for images. The players are injectable so tests can drive the host without
    spawning real audio subprocesses."""

    def __init__(
        self,
        *,
        capabilities: MediaCapabilities | None = None,
        speech_player: LocalAudioPlayer | None = None,
        sfx_player: LocalAudioPlayer | None = None,
        music_player: LoopingAudioPlayer | None = None,
        tts_volume: float = DEFAULT_TTS_VOLUME,
        sfx_volume: float = DEFAULT_SFX_VOLUME,
        music_volume: float = DEFAULT_MUSIC_VOLUME,
    ) -> None:
        self._capabilities = capabilities or MediaCapabilities.all()
        self._speech_player = speech_player or LocalAudioPlayer(volume=tts_volume)
        self._sfx_player = sfx_player or LocalAudioPlayer(volume=sfx_volume)
        self._music_player = music_player or LoopingAudioPlayer(volume=music_volume)
        self._now_playing: MusicTrack | None = None

    @classmethod
    def from_config(
        cls, media_config: dict, *, capabilities: MediaCapabilities | None = None
    ) -> LocalMediaHost:
        return cls(
            capabilities=capabilities,
            tts_volume=float(media_config.get("tts_volume", DEFAULT_TTS_VOLUME)),
            sfx_volume=float(media_config.get("sfx_volume", DEFAULT_SFX_VOLUME)),
            music_volume=float(media_config.get("music_volume", DEFAULT_MUSIC_VOLUME)),
        )

    @property
    def capabilities(self) -> MediaCapabilities:
        return self._capabilities

    async def play_speech(self, path: Path) -> None:
        await self._speech_player.play(path)

    async def play_sound_effect(self, path: Path) -> None:
        await self._sfx_player.play(path)

    def play_music(
        self, path: Path, *, prompt: str = "", length_seconds: float | None = None
    ) -> None:
        self._music_player.play(path)
        self._now_playing = MusicTrack(
            prompt=prompt, path=Path(path), length_seconds=length_seconds or 0.0
        )

    def stop_music(self) -> None:
        self._now_playing = None
        self._music_player.stop()

    def set_music_volume(self, value: float) -> float:
        return self._music_player.set_volume(value)

    def music_volume(self) -> float:
        return self._music_player.volume

    def music_status_line(self) -> str | None:
        track = self._now_playing
        if track is None or not self._music_player.playing:
            return None
        return (
            f"looping {track.prompt!r} ({int(track.length_seconds)}s track) "
            f"at volume {int(round(self._music_player.volume * 100))}%"
        )

    def stop_audio(self) -> None:
        self._speech_player.stop()
        self._sfx_player.stop()

    def present_image(self, path: Path, *, caption: str = "") -> None:
        open_image_file(path)

    def present_handout(self, title: str, body: str, *, path: Path | None = None) -> None:
        return None  # reserved: capability exists, no terminal presentation yet
