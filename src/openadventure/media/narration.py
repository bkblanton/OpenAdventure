"""Narration queue, sound-effect timing, and interrupt support."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from openadventure.media.host import MediaCapabilities, MediaHost, NullMediaHost
from openadventure.media.sound_effects import DEFAULT_SFX_ESTIMATED_DURATION_SECONDS
from openadventure.media.tts import speech_text
from openadventure.providers.base import Usage
from openadventure.store.workspace import Campaign
from openadventure.util import shorten

NARRATOR = "Narrator"
NARRATOR_VOICE_SETTING = "narrator_voice_id"


class SoundEffectCue(BaseModel):
    description: str
    duration_seconds: float | None = None
    prompt_influence: float | None = None
    loop: bool = False
    after_text: str | None = None


class NarrationAgent:
    """Serialize narration and SFX while using one narrator voice throughout."""

    def __init__(
        self,
        campaign: Campaign,
        log,
        background,
        tts=None,
        sound_effects=None,
        host: MediaHost | None = None,
        usage_recorder: Callable[[Usage, str, str, str | None], None] | None = None,
    ):
        self.campaign = campaign
        self.log = log
        self.background = background
        self.tts = tts
        self.sound_effects = sound_effects
        self.usage_recorder = usage_recorder
        self.host: MediaHost = host or NullMediaHost(MediaCapabilities.all())
        self._play_lock = asyncio.Lock()
        self._last_clips: list[tuple[str, Any]] = []
        self._recording: list[tuple[str, Any]] | None = None

    @property
    def ready(self) -> bool:
        return self.tts is not None and getattr(self.tts, "ready", True)

    @property
    def configuration_hint(self) -> str:
        if self.tts is None:
            return "No TTS backend configured."
        return getattr(self.tts, "configuration_hint", "")

    def queue_line(self, text: str, *, interrupt: bool = False):
        if interrupt:
            self.interrupt()

        async def work():
            await self.play_line(text)
            return []

        return self.background.spawn("tts", f"narrating: {shorten(text)}", work())

    def queue_turn(
        self,
        text: str,
        *,
        sound_effects: list[SoundEffectCue | dict[str, Any]] | None = None,
        interrupt: bool = False,
    ):
        if interrupt:
            self.interrupt()
        sounds = [self._coerce_sound_effect(cue) for cue in sound_effects or []]

        async def work():
            await self.play_turn(text, sound_effects=sounds)
            return []

        return self.background.spawn("tts", f"narrating turn: {shorten(text)}", work())

    def queue_sound_effect(
        self,
        description: str,
        *,
        duration_seconds: float | None = None,
        prompt_influence: float | None = None,
        loop: bool = False,
        interrupt: bool = False,
    ):
        if interrupt:
            self.interrupt()

        async def work():
            await self.play_sound_effect(
                description,
                duration_seconds=duration_seconds,
                prompt_influence=prompt_influence,
                loop=loop,
            )
            return []

        label = f"sound effect: {shorten(description)}"
        return self.background.spawn("sfx", label, work())

    def queue_replay(self, *, interrupt: bool = True):
        """Replay the latest narration turn from cached audio."""
        if not self._last_clips:
            return None
        if interrupt:
            self.interrupt()

        async def work():
            await self.replay_last()
            return []

        return self.background.spawn("tts", "replaying narration", work())

    def interrupt(self) -> int:
        cancelled = 0
        cancel_kind = getattr(self.background, "cancel_kind", None)
        if callable(cancel_kind):
            cancelled += cancel_kind("tts")
            cancelled += cancel_kind("sfx")
        self.host.stop_audio()
        return cancelled

    async def play_line(self, text: str) -> None:
        self._require_tts()
        async with self._play_lock, self._capture():
            await self._speak(text)
            self._log_narration(text, sound_effect_cues=0)

    async def play_turn(
        self,
        text: str,
        *,
        sound_effects: list[SoundEffectCue] | None = None,
    ) -> None:
        sounds = sound_effects or []
        playable_sounds = sounds if self.sound_effects is not None else []
        if not playable_sounds:
            await self.play_line(text)
            return
        self._require_tts()
        if not getattr(self.sound_effects, "ready", True):
            hint = getattr(
                self.sound_effects, "configuration_hint", "Sound effects are not configured."
            )
            raise RuntimeError(hint)

        async with self._play_lock, self._capture():
            sound_points = self._sound_effect_points(text, playable_sounds)
            sounds_by_position: dict[int, list[SoundEffectCue]] = {}
            for position, cue in sound_points:
                sounds_by_position.setdefault(position, []).append(cue)

            boundaries = sorted({0, len(text), *sounds_by_position.keys()})
            actions: list[tuple[str, Any]] = []
            for cue in sounds_by_position.get(0, []):
                actions.append(("sound", cue))
            for start, end in zip(boundaries, boundaries[1:], strict=False):
                if end > start:
                    actions.append(("speech", text[start:end]))
                for cue in sounds_by_position.get(end, []):
                    actions.append(("sound", cue))

            await self._play_actions(actions)
            self._log_narration(text, sound_effect_cues=len(playable_sounds))

    async def play_sound_effect(
        self,
        description: str,
        *,
        duration_seconds: float | None = None,
        prompt_influence: float | None = None,
        loop: bool = False,
    ) -> None:
        if self.sound_effects is None:
            raise RuntimeError("No sound effects backend configured.")
        if not getattr(self.sound_effects, "ready", True):
            hint = getattr(
                self.sound_effects, "configuration_hint", "Sound effects are not configured."
            )
            raise RuntimeError(hint)
        async with self._play_lock, self._capture():
            await self._play_sound_effect_unlocked(
                SoundEffectCue(
                    description=description,
                    duration_seconds=duration_seconds,
                    prompt_influence=prompt_influence,
                    loop=loop,
                )
            )

    def _require_tts(self) -> None:
        if self.tts is None:
            raise RuntimeError("No TTS backend configured.")
        if not getattr(self.tts, "ready", True):
            hint = getattr(self.tts, "configuration_hint", "TTS is not configured.")
            raise RuntimeError(hint)

    async def _play_actions(self, actions: list[tuple[str, Any]]) -> None:
        """Play ordered speech and sound actions with speech prefetching."""
        prefetch: dict[int, asyncio.Task] = {}

        def ensure(index: int) -> None:
            if index not in prefetch:
                prefetch[index] = asyncio.create_task(self._synthesize(actions[index][1]))

        def prefetch_next(after: int) -> None:
            for index in range(after + 1, len(actions)):
                if actions[index][0] == "speech":
                    ensure(index)
                    break

        try:
            prefetch_next(-1)
            for index, (kind, value) in enumerate(actions):
                if kind == "speech":
                    ensure(index)
                    path = await prefetch.pop(index)
                    prefetch_next(index)
                    if path is not None:
                        self._record_clip("speech", path)
                        await self.host.play_speech(path)
                else:
                    await self._play_sound_effect_unlocked(value)
        finally:
            for task in prefetch.values():
                task.cancel()

    async def _synthesize(self, text: str):
        path = await self.tts.synthesize(text)
        if path is not None:
            self._record_media_usage(Usage(tts_characters=len(speech_text(text))), "tts", self.tts)
        return path

    async def _speak(self, text: str) -> None:
        path = await self._synthesize(text)
        if path is not None:
            self._record_clip("speech", path)
            await self.host.play_speech(path)

    async def _play_sound_effect_unlocked(self, cue: SoundEffectCue) -> None:
        path = await self.sound_effects.generate(
            cue.description,
            duration_seconds=cue.duration_seconds,
            prompt_influence=cue.prompt_influence,
            loop=cue.loop,
        )
        self._record_media_usage(
            Usage(
                sound_effect_seconds=(
                    cue.duration_seconds or DEFAULT_SFX_ESTIMATED_DURATION_SECONDS
                )
            ),
            "sound_effect",
            self.sound_effects,
        )
        self._record_clip("sound", path)
        await self.host.play_sound_effect(path)
        self._log_sound_effect(path, cue)

    @contextlib.asynccontextmanager
    async def _capture(self):
        self._recording = []
        try:
            yield
        finally:
            self._recording = None

    def _record_clip(self, kind: str, path: Any) -> None:
        if self._recording is None or path is None:
            return
        if not self._recording:
            self._last_clips = self._recording
        self._recording.append((kind, path))

    async def replay_last(self) -> bool:
        clips = list(self._last_clips)
        if not clips:
            return False
        async with self._play_lock:
            for kind, path in clips:
                if path is None:
                    continue
                if kind == "sound":
                    await self.host.play_sound_effect(path)
                else:
                    await self.host.play_speech(path)
        return True

    def _log_narration(self, text: str, *, sound_effect_cues: int) -> None:
        self.log.append(
            "media",
            {
                "kind": "narration",
                "speaker": NARRATOR,
                "role": "narrator",
                "voice_id": getattr(self.tts, "voice_id", ""),
                "chars": len(text),
                "sound_effect_cues": sound_effect_cues,
            },
        )

    def _log_sound_effect(self, path, cue: SoundEffectCue) -> None:
        self.log.append(
            "media",
            {
                "kind": "sound_effect",
                "path": str(path),
                "description": cue.description,
                "duration_seconds": cue.duration_seconds,
                "loop": cue.loop,
                "queued_by": "narration",
                "after_text": cue.after_text,
            },
        )

    def _record_media_usage(self, usage: Usage, kind: str, backend: object) -> None:
        if self.usage_recorder is None:
            return
        self.usage_recorder(
            usage,
            kind,
            type(backend).__name__,
            getattr(backend, "model_id", None),
        )

    def _coerce_sound_effect(self, cue: SoundEffectCue | dict[str, Any]) -> SoundEffectCue:
        if isinstance(cue, SoundEffectCue):
            return cue
        return SoundEffectCue.model_validate(cue)

    def _sound_effect_points(
        self, text: str, cues: list[SoundEffectCue]
    ) -> list[tuple[int, SoundEffectCue]]:
        points: list[tuple[int, SoundEffectCue]] = []
        cursor = 0
        for cue in cues:
            cut = self._sound_effect_cut(text, cursor, cue)
            points.append((cut, cue))
            cursor = cut
        return points

    def _sound_effect_cut(self, text: str, start: int, cue: SoundEffectCue) -> int:
        if cue.after_text:
            anchor = text.find(cue.after_text, start)
            if anchor >= 0:
                return anchor + len(cue.after_text)
        return self._next_sentence_cut(text, start)

    def _next_sentence_cut(self, text: str, start: int) -> int:
        match = re.search(r"(?<=[.!?])(?:\s+|$)", text[start:])
        if match is None:
            return len(text)
        return max(start + match.end(), start)
