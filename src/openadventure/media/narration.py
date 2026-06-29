"""Narration queue, voice casting, and interrupt support."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import re
from typing import Any

from pydantic import BaseModel, Field

from openadventure.media.host import MediaCapabilities, MediaHost, NullMediaHost
from openadventure.media.tts import DEFAULT_ELEVENLABS_VOICE_ID, VoiceRecord
from openadventure.store import snapshots
from openadventure.store.workspace import Campaign, slugify
from openadventure.util import shorten

NARRATOR = "Narrator"
# Persisted key kept as "narration_accent" for back-compat with saved campaigns.
CAST_ACCENT_SETTING = "narration_accent"
NARRATOR_VOICE_SETTING = "narrator_voice_id"


class VoiceAssignment(BaseModel):
    speaker: str
    voice_id: str = ""
    voice_name: str = "Default"
    source: str = "default"
    voice_hint: str | None = None
    accent: str | None = None
    target_accent: str | None = None
    gender: str | None = None
    target_gender: str | None = None
    age: str | None = None
    target_age: str | None = None
    public_owner_id: str | None = None
    description: str = ""


class VoiceCast(BaseModel):
    speakers: dict[str, VoiceAssignment] = Field(default_factory=dict)
    # Slug variants the GM has used for an already-cast speaker, mapped onto that
    # speaker's canonical key. Lets "Mr. Dooley" resolve to the same entry the
    # first "Dooley" line created, so a character is never voiced twice.
    aliases: dict[str, str] = Field(default_factory=dict)


class SoundEffectCue(BaseModel):
    description: str
    duration_seconds: float | None = None
    prompt_influence: float | None = None
    loop: bool = False
    after_text: str | None = None


class VoiceCue(BaseModel):
    text: str
    speaker: str = NARRATOR
    role: str = "dialogue"
    voice_hint: str | None = None
    accent: str | None = None
    gender: str | None = None
    age: str | None = None


_GENDER_ALIASES = {
    "male": "male",
    "m": "male",
    "man": "male",
    "masculine": "male",
    "female": "female",
    "f": "female",
    "woman": "female",
    "feminine": "female",
    "neutral": "neutral",
    "nonbinary": "neutral",
    "non-binary": "neutral",
    "androgynous": "neutral",
}


def normalize_gender(value: str | None) -> str | None:
    """Map a free-form gender hint onto the directory's gender vocabulary."""
    if not value:
        return None
    return _GENDER_ALIASES.get(value.strip().lower())


def _genders_match(candidate: str | None, target: str) -> bool:
    return normalize_gender(candidate) == target


def normalize_accent(value: str | None) -> str | None:
    """Lowercase and trim a free-form accent label for comparison."""
    if not value:
        return None
    return " ".join(value.split()).lower() or None


def _accents_match(candidate: str | None, target: str) -> bool:
    """Loose accent match: equal, or one label contained in the other.

    Directory accent labels are free-form ("southern us", "american southern"),
    so a substring test in either direction catches obvious matches without a
    brittle exact-equality check.
    """
    cand = normalize_accent(candidate)
    want = normalize_accent(target)
    if not cand or not want:
        return False
    return cand == want or want in cand or cand in want


def _speaker_key(speaker: str) -> str:
    return slugify(speaker or NARRATOR)


# Honorifics the GM may prepend on one appearance but not the next ("Dooley"
# vs "Mr. Dooley"). Stripping a leading honorific collapses both spellings onto
# one cast key so a character isn't cast twice.
_HONORIFICS = frozenset(
    {
        "mr", "mrs", "ms", "miss", "mx", "dr", "doc", "doctor", "sir", "dame",
        "madam", "madame", "master", "mistress", "lord", "lady", "captain",
        "capt", "sergeant", "sgt", "lieutenant", "lt", "colonel", "col", "major",
        "general", "father", "fr", "brother", "sister", "reverend", "rev", "prof",
        "professor", "officer", "saint", "st",
    }
)  # fmt: skip


def _strip_honorific_key(speaker: str) -> str:
    """Speaker key with any leading honorific tokens removed.

    Returns the de-honorified key, or the plain key when stripping would leave
    nothing (a bare "Sir" stays "sir")."""
    words = [w for w in re.split(r"[\s.]+", speaker.strip()) if w]
    while len(words) > 1 and re.sub(r"[^a-z]", "", words[0].lower()) in _HONORIFICS:
        words = words[1:]
    return _speaker_key(" ".join(words))


def load_voice_cast(campaign: Campaign) -> VoiceCast:
    data = snapshots.load_json(campaign.voice_cast_path) or {}
    return VoiceCast.model_validate(data)


def save_voice_cast(campaign: Campaign, cast: VoiceCast) -> None:
    snapshots.save_json(campaign.voice_cast_path, cast.model_dump())


def clear_voice(campaign: Campaign, speaker: str) -> VoiceAssignment | None:
    cast = load_voice_cast(campaign)
    removed = cast.speakers.pop(_speaker_key(speaker), None)
    if removed is not None:
        save_voice_cast(campaign, cast)
    return removed


def clear_voice_cast(campaign: Campaign) -> int:
    cast = load_voice_cast(campaign)
    count = len(cast.speakers)
    if count:
        save_voice_cast(campaign, VoiceCast())
    return count


def cast_accent(campaign: Campaign) -> str | None:
    """Default accent applied when casting NPC voices that declare none of their
    own. Does not affect the Narrator, whose voice is pinned by voice id."""
    value = campaign.load_meta().settings.get(CAST_ACCENT_SETTING)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


class NarrationAgent:
    """Serializes spoken lines and SFX while keeping character voices stable."""

    def __init__(
        self,
        campaign: Campaign,
        log,
        background,
        tts=None,
        sound_effects=None,
        host: MediaHost | None = None,
    ):
        self.campaign = campaign
        self.log = log
        self.background = background
        self.tts = tts  # generation backend (synthesize)
        self.sound_effects = sound_effects  # generation backend (generate)
        # The host presents what the backends generate; default to a no-op host
        # so a directly-constructed agent (tests, headless) never crashes.
        self.host: MediaHost = host or NullMediaHost(MediaCapabilities.all())
        self._play_lock = asyncio.Lock()
        self._cast_lock = asyncio.Lock()
        # Audio clips, in play order, of the most recent narration turn, so it can
        # be replayed without regenerating anything: (kind, path) where kind is
        # "speech" or "sound". ``_recording`` is the buffer for the turn currently
        # playing; it commits onto ``_last_clips`` on its first clip (see
        # ``_record_clip``), so an aborted turn that produced nothing never wipes
        # the turn before it.
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

    def queue_line(
        self,
        text: str,
        *,
        speaker: str = NARRATOR,
        role: str = "narrator",
        voice_hint: str | None = None,
        accent: str | None = None,
        gender: str | None = None,
        age: str | None = None,
        interrupt: bool = False,
    ):
        if interrupt:
            self.interrupt()

        async def work():
            await self.play_line(
                text,
                speaker=speaker,
                role=role,
                voice_hint=voice_hint,
                accent=accent,
                gender=gender,
                age=age,
            )
            return []

        label = f"narrating {speaker or NARRATOR}: {shorten(text)}"
        return self.background.spawn("tts", label, work())

    def queue_turn(
        self,
        text: str,
        *,
        voice_cues: list[VoiceCue | dict[str, Any]] | None = None,
        sound_effects: list[SoundEffectCue | dict[str, Any]] | None = None,
        interrupt: bool = False,
    ):
        if interrupt:
            self.interrupt()
        voices = [self._coerce_voice_cue(cue) for cue in voice_cues or []]
        sounds = [self._coerce_sound_effect(cue) for cue in sound_effects or []]

        async def work():
            await self.play_turn(text, voice_cues=voices, sound_effects=sounds)
            return []

        label = f"narrating turn: {shorten(text)}"
        return self.background.spawn("tts", label, work())

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
        """Re-narrate the latest turn from its cached audio, making no new API
        calls. Stops any narration still playing first (when ``interrupt``);
        returns the spawned task, or None when there's nothing to replay."""
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
        # Playback lives in the host now, so stopping audio is the host's job.
        self.host.stop_audio()
        return cancelled

    def voice_cast(self) -> VoiceCast:
        return load_voice_cast(self.campaign)

    def clear_voice(self, speaker: str) -> VoiceAssignment | None:
        return clear_voice(self.campaign, speaker)

    def clear_voice_cast(self) -> int:
        return clear_voice_cast(self.campaign)

    async def play_line(
        self,
        text: str,
        *,
        speaker: str = NARRATOR,
        role: str = "narrator",
        voice_hint: str | None = None,
        accent: str | None = None,
        gender: str | None = None,
        age: str | None = None,
    ) -> None:
        if self.tts is None:
            raise RuntimeError("No TTS backend configured.")
        if not getattr(self.tts, "ready", True):
            hint = getattr(self.tts, "configuration_hint", "TTS is not configured.")
            raise RuntimeError(hint)
        async with self._play_lock, self._capture():
            assignment = await self.assign_voice(
                speaker, role=role, voice_hint=voice_hint, accent=accent, gender=gender, age=age
            )
            await self._speak(text, assignment)
            self.log.append(
                "media",
                {
                    "kind": "narration",
                    "speaker": assignment.speaker,
                    "role": role,
                    "voice_id": assignment.voice_id,
                    "voice_name": assignment.voice_name,
                    "accent": assignment.accent,
                    "target_accent": assignment.target_accent,
                    "gender": assignment.gender,
                    "target_gender": assignment.target_gender,
                    "chars": len(text),
                },
            )

    async def play_turn(
        self,
        text: str,
        *,
        voice_cues: list[VoiceCue] | None = None,
        sound_effects: list[SoundEffectCue] | None = None,
    ) -> None:
        voices = voice_cues or []
        sounds = sound_effects or []
        playable_sounds = sounds if self.sound_effects is not None else []
        if not voices and not playable_sounds:
            await self.play_line(text, speaker=NARRATOR, role="narrator")
            return
        if self.tts is None:
            raise RuntimeError("No TTS backend configured.")
        if not getattr(self.tts, "ready", True):
            hint = getattr(self.tts, "configuration_hint", "TTS is not configured.")
            raise RuntimeError(hint)
        if playable_sounds and not getattr(self.sound_effects, "ready", True):
            hint = getattr(
                self.sound_effects, "configuration_hint", "Sound effects are not configured."
            )
            raise RuntimeError(hint)
        async with self._play_lock, self._capture():
            voice_spans = self._voice_spans(text, voices)
            sound_points = self._sound_effect_points(text, playable_sounds)
            sounds_by_position: dict[int, list[SoundEffectCue]] = {}
            for position, cue in sound_points:
                sounds_by_position.setdefault(position, []).append(cue)

            boundaries = {0, len(text), *sounds_by_position.keys()}
            for start, end, _cue in voice_spans:
                boundaries.add(start)
                boundaries.add(end)
            ordered_boundaries = sorted(boundaries)

            assignment_cache: dict[
                tuple[str, str, str | None, str | None, str | None, str | None], VoiceAssignment
            ] = {}

            async def assignment_for(cue: VoiceCue) -> VoiceAssignment:
                key = (cue.speaker, cue.role, cue.voice_hint, cue.accent, cue.gender, cue.age)
                if key not in assignment_cache:
                    assignment_cache[key] = await self.assign_voice(
                        cue.speaker,
                        role=cue.role,
                        voice_hint=cue.voice_hint,
                        accent=cue.accent,
                        gender=cue.gender,
                        age=cue.age,
                    )
                return assignment_cache[key]

            narrator = VoiceCue(text="", speaker=NARRATOR, role="narrator")

            # Flatten the turn into an ordered list of voice/sound actions so the
            # next voice clip can be synthesized while the current one plays.
            # Otherwise each segment's ElevenLabs round-trip only starts once the
            # previous clip finishes; that round-trip is the audible gap before a
            # character's first line.
            actions: list[tuple[Any, ...]] = []
            for cue in sounds_by_position.get(0, []):
                actions.append(("sound", cue))
            for start, end in zip(ordered_boundaries, ordered_boundaries[1:], strict=False):
                if end > start:
                    cue = self._voice_cue_for_interval(voice_spans, start, end) or narrator
                    actions.append(("voice", text[start:end], cue))
                for cue in sounds_by_position.get(end, []):
                    actions.append(("sound", cue))

            prefetched = await self._play_actions(actions, assignment_for)
            self.log.append(
                "media",
                {
                    "kind": "narration",
                    "speaker": NARRATOR,
                    "role": "narrator",
                    "chars": len(text),
                    "voice_cues": len(voice_spans),
                    "sound_effect_cues": len(playable_sounds),
                    "prefetched": prefetched,
                },
            )

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
            path = await self.sound_effects.generate(
                description,
                duration_seconds=duration_seconds,
                prompt_influence=prompt_influence,
                loop=loop,
            )
            self._record_clip("sound", path)
            await self.host.play_sound_effect(path)
            self._log_sound_effect(
                path,
                SoundEffectCue(
                    description=description,
                    duration_seconds=duration_seconds,
                    prompt_influence=prompt_influence,
                    loop=loop,
                ),
            )

    def _npc_speaker_keys(self) -> dict[str, str]:
        """Slug variants of NPC/monster sheet names mapped to their stable id.

        Casting keys a speaker by sheet id whenever the name matches a sheet, so
        the GM saying "Dooley" then "Mr. Dooley" both land on the one entry tied
        to that NPC. PCs are excluded; players voice themselves."""
        from openadventure.store.sheetstore import SheetStore

        mapping: dict[str, str] = {}
        try:
            sheets = SheetStore(self.campaign).list()
        except OSError, ValueError:
            return mapping
        for sheet in sheets:
            if sheet.kind == "pc":
                continue
            for variant in (
                _speaker_key(sheet.name),
                _strip_honorific_key(sheet.name),
                _speaker_key(sheet.id),
            ):
                if variant:
                    mapping.setdefault(variant, sheet.id)
        return mapping

    def _resolve_speaker_key(self, speaker: str, cast: VoiceCast) -> str:
        """Resolve a free-text speaker onto a stable cast key.

        Order: the narrator is fixed; an exact existing key or a known alias
        wins; otherwise bind to a matching NPC sheet id; otherwise collapse
        honorific variants onto the de-honorified key. Any new alias is written
        into ``cast``; the caller decides whether to persist it."""
        raw = _speaker_key(speaker)
        if raw == _speaker_key(NARRATOR) or raw in cast.speakers:
            return raw
        aliased = cast.aliases.get(raw)
        if aliased:
            return aliased
        stripped = _strip_honorific_key(speaker)
        npc_keys = self._npc_speaker_keys()
        for variant in (raw, stripped):
            sheet_key = npc_keys.get(variant)
            if sheet_key:
                if sheet_key != raw:
                    cast.aliases[raw] = sheet_key
                return sheet_key
        if stripped and stripped != raw:
            cast.aliases[raw] = stripped
            return stripped
        return raw

    def find_cast_entry(self, speaker: str) -> VoiceAssignment | None:
        """Resolve a speaker to its saved entry without persisting the cast.

        Read-only helper for the cast_lookup tool; alias resolution stays in
        memory so a lookup never writes."""
        cast = load_voice_cast(self.campaign)
        return cast.speakers.get(self._resolve_speaker_key(speaker, cast))

    async def assign_voice(
        self,
        speaker: str,
        *,
        role: str = "dialogue",
        voice_hint: str | None = None,
        accent: str | None = None,
        gender: str | None = None,
        age: str | None = None,
    ) -> VoiceAssignment:
        async with self._cast_lock:
            cast = load_voice_cast(self.campaign)
            original_aliases = dict(cast.aliases)
            key = self._resolve_speaker_key(speaker, cast)
            existing = cast.speakers.get(key)
            if existing is not None:
                # Resolution may have learned a new alias even on a cache hit.
                if cast.aliases != original_aliases:
                    save_voice_cast(self.campaign, cast)
                return existing
            assignment = await self._choose_voice(
                speaker, role=role, voice_hint=voice_hint, accent=accent, gender=gender, age=age
            )
            cast.speakers[key] = assignment
            save_voice_cast(self.campaign, cast)
            return assignment

    async def _choose_voice(
        self,
        speaker: str,
        *,
        role: str,
        voice_hint: str | None,
        accent: str | None,
        gender: str | None,
        age: str | None,
    ) -> VoiceAssignment:
        speaker = speaker.strip() or NARRATOR
        # Prefer the per-speaker accent; fall back to the campaign-wide cast
        # default only when the speaker did not declare one of their own.
        target_accent = normalize_accent(accent) or cast_accent(self.campaign)
        target_gender = normalize_gender(gender)
        target_age = (age or "").strip().lower() or None
        if role == "narrator" or _speaker_key(speaker) == _speaker_key(NARRATOR):
            return VoiceAssignment(
                speaker=NARRATOR,
                voice_id=getattr(self.tts, "voice_id", DEFAULT_ELEVENLABS_VOICE_ID),
                voice_name=NARRATOR,
                source="default",
                voice_hint=voice_hint,
                target_accent=target_accent,
                target_gender=target_gender,
                target_age=target_age,
            )

        voices = await self._directory_candidates(
            voice_hint, accent=target_accent, gender=target_gender, age=target_age
        )
        # Never let a wrong-gender actor through: when the speaker has a declared
        # gender, keep only voices that match it (falling back to the full list
        # only if the directory labelled none of them).
        if target_gender:
            matching = [v for v in voices if _genders_match(v.gender, target_gender)]
            voices = matching or voices
        # Likewise keep only voices whose labelled accent matches the request, so a
        # wrong-accent actor can't win on a free-text hint match. Fall back to the
        # full list when the directory labelled none of them.
        if target_accent:
            matching = [v for v in voices if _accents_match(v.accent, target_accent)]
            voices = matching or voices
        if not voices:
            return VoiceAssignment(
                speaker=speaker,
                voice_id=getattr(self.tts, "voice_id", DEFAULT_ELEVENLABS_VOICE_ID),
                voice_name=f"Default for {speaker}",
                source="default",
                voice_hint=voice_hint,
                target_accent=target_accent,
                target_gender=target_gender,
                target_age=target_age,
            )

        used = {a.voice_id for a in load_voice_cast(self.campaign).speakers.values()}
        unused = [voice for voice in voices if voice.voice_id not in used]
        pool = unused or voices
        index = int(hashlib.sha256(_speaker_key(speaker).encode("utf-8")).hexdigest(), 16)
        selected = pool[index % len(pool)]
        voice_id = selected.voice_id
        source = selected.source
        if selected.source == "shared" and selected.public_owner_id:
            add_shared_voice = getattr(self.tts, "add_shared_voice", None)
            if callable(add_shared_voice):
                try:
                    voice_id = await add_shared_voice(
                        selected.public_owner_id,
                        selected.voice_id,
                        new_name=f"OpenAdventure {speaker}"[:100],
                    )
                    source = "shared-added"
                except RuntimeError:
                    source = "shared"
        return VoiceAssignment(
            speaker=speaker,
            voice_id=voice_id,
            voice_name=selected.name,
            source=source,
            voice_hint=voice_hint,
            accent=selected.accent,
            target_accent=target_accent,
            gender=selected.gender,
            target_gender=target_gender,
            age=selected.age,
            target_age=target_age,
            public_owner_id=selected.public_owner_id,
            description=selected.description,
        )

    async def _directory_candidates(
        self,
        voice_hint: str | None,
        *,
        accent: str | None,
        gender: str | None = None,
        age: str | None = None,
    ) -> list[VoiceRecord]:
        search_voice_directory = getattr(self.tts, "search_voice_directory", None)
        if not callable(search_voice_directory):
            return []
        searches = [voice_hint] if voice_hint else []
        searches.append(None)
        for search in searches:
            kwargs: dict[str, Any] = {"limit": 50, "use_cases": ["characters_animation"]}
            if search:
                kwargs["search"] = search
            if accent:
                kwargs["accent"] = accent
            if gender:
                kwargs["gender"] = gender
            if age:
                kwargs["age"] = age
            try:
                voices = await search_voice_directory(**kwargs)
            except RuntimeError:
                voices = []
            if voices:
                return voices
        return []

    async def _play_actions(self, actions, assignment_for) -> bool:
        """Play an ordered list of voice/sound actions, pre-fetching voice clips.

        Each clip is synthesized by the backend and played by the host; the next
        clip is synthesized while the current one plays, so a character's first
        line doesn't wait on its own ElevenLabs round-trip. Always returns True
        (the prefetch path is the only path now that generation and playback are
        split)."""
        prefetch: dict[int, asyncio.Task] = {}

        async def synth(index: int):
            _, seg_text, cue = actions[index]
            return await self._synthesize(seg_text, await assignment_for(cue))

        def ensure(index: int) -> None:
            if index not in prefetch:
                prefetch[index] = asyncio.create_task(synth(index))

        def prefetch_next(after: int) -> None:
            for j in range(after + 1, len(actions)):
                if actions[j][0] == "voice":
                    ensure(j)
                    break

        try:
            prefetch_next(-1)  # prime the first voice clip
            for i, action in enumerate(actions):
                if action[0] == "voice":
                    ensure(i)
                    path = await prefetch.pop(i)
                    prefetch_next(i)  # synthesize the next clip while this one plays
                    if path is not None:
                        self._record_clip("speech", path)
                        await self.host.play_speech(path)
                else:
                    await self._play_sound_effect_unlocked(action[1])
        finally:
            for task in prefetch.values():
                task.cancel()
        return True

    async def _synthesize(self, text: str, assignment: VoiceAssignment):
        synthesize = self.tts.synthesize
        parameters = inspect.signature(synthesize).parameters
        if "voice_id" in parameters and assignment.voice_id:
            return await synthesize(text, voice_id=assignment.voice_id)
        return await synthesize(text)

    async def _speak(self, text: str, assignment: VoiceAssignment) -> None:
        """Synthesize one line via the backend, then play it through the host."""
        path = await self._synthesize(text, assignment)
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
        self._record_clip("sound", path)
        await self.host.play_sound_effect(path)
        self._log_sound_effect(path, cue)

    @contextlib.asynccontextmanager
    async def _capture(self):
        """Record the audio clips played within this block as one narration turn.

        Clips append to a fresh buffer that commits onto ``_last_clips`` the
        moment it gets its first clip (see ``_record_clip``); a turn cut off
        before it produced anything leaves the previous turn replayable."""
        self._recording = []
        try:
            yield
        finally:
            self._recording = None

    def _record_clip(self, kind: str, path: Any) -> None:
        """Note one played clip for the in-progress turn (no-op outside a turn,
        or for an empty path). The first clip commits the buffer as the latest
        turn, so ``_last_clips`` always tracks the newest turn that made sound."""
        if self._recording is None or path is None:
            return
        if not self._recording:
            self._last_clips = self._recording
        self._recording.append((kind, path))

    async def replay_last(self) -> bool:
        """Re-play the most recent narration turn from its cached audio, with no
        new generation. Returns False when there's nothing to replay."""
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

    def _coerce_voice_cue(self, cue: VoiceCue | dict[str, Any]) -> VoiceCue:
        if isinstance(cue, VoiceCue):
            return cue
        return VoiceCue.model_validate(cue)

    def _coerce_sound_effect(self, cue: SoundEffectCue | dict[str, Any]) -> SoundEffectCue:
        if isinstance(cue, SoundEffectCue):
            return cue
        return SoundEffectCue.model_validate(cue)

    def _voice_spans(self, text: str, cues: list[VoiceCue]) -> list[tuple[int, int, VoiceCue]]:
        spans: list[tuple[int, int, VoiceCue]] = []
        cursor = 0
        for cue in cues:
            if not cue.text:
                continue
            start = text.find(cue.text, cursor)
            if start < 0:
                continue
            end = start + len(cue.text)
            spans.append((start, end, cue))
            cursor = end
        return spans

    def _voice_cue_for_interval(
        self, spans: list[tuple[int, int, VoiceCue]], start: int, end: int
    ) -> VoiceCue | None:
        for span_start, span_end, cue in spans:
            if span_start <= start and end <= span_end:
                return cue
        return None

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
