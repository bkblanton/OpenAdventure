"""Sound effect backends."""

from __future__ import annotations

import asyncio
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from openadventure.media import _elevenlabs
from openadventure.media.tts import DEFAULT_OUTPUT_FORMAT

DEFAULT_ELEVENLABS_SOUND_EFFECTS_MODEL_ID = "eleven_text_to_sound_v2"
DEFAULT_SFX_VOLUME = 1.0  # default sfx playback volume, read by the media host
# ElevenLabs chooses a duration when callers omit one. Keep usage estimates
# stable and explicit rather than pretending we know the generated file length.
DEFAULT_SFX_ESTIMATED_DURATION_SECONDS = 5.0


class ElevenLabsSoundEffects(_elevenlabs.ElevenLabsAuth):
    """ElevenLabs text-to-sound-effects backend: generate a one-shot effect to a
    file. Playback is the host's job (see media/host.py)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_id: str = DEFAULT_ELEVENLABS_SOUND_EFFECTS_MODEL_ID,
        output_format: str = DEFAULT_OUTPUT_FORMAT,
        cache_dir: str | Path | None = None,
        request_timeout: float = 60.0,
    ):
        self._api_key = api_key
        self.model_id = model_id
        self.output_format = output_format
        self.cache_dir = Path(cache_dir or Path(tempfile.gettempdir()) / "openadventure-sfx")
        self.request_timeout = request_timeout

    @classmethod
    def from_config(cls, media_config: dict[str, Any]) -> ElevenLabsSoundEffects:
        return cls(
            api_key=media_config.get("elevenlabs_api_key"),
            model_id=media_config.get(
                "elevenlabs_sfx_model_id", DEFAULT_ELEVENLABS_SOUND_EFFECTS_MODEL_ID
            ),
            output_format=media_config.get("elevenlabs_sfx_output_format", DEFAULT_OUTPUT_FORMAT),
            cache_dir=media_config.get("sfx_cache_dir"),
        )

    async def generate(
        self,
        description: str,
        *,
        duration_seconds: float | None = None,
        prompt_influence: float | None = None,
        loop: bool = False,
    ) -> Path:
        if not description.strip():
            raise RuntimeError("Sound effect description cannot be blank")
        if not self.api_key:
            raise RuntimeError(self.configuration_hint)
        audio = await asyncio.to_thread(
            self._request_audio,
            description.strip(),
            duration_seconds,
            prompt_influence,
            loop,
        )
        return await asyncio.to_thread(
            _elevenlabs.write_audio,
            audio,
            cache_dir=self.cache_dir,
            suffix=_elevenlabs.suffix_for(self.output_format),
        )

    def _request_audio(
        self,
        description: str,
        duration_seconds: float | None,
        prompt_influence: float | None,
        loop: bool,
    ) -> bytes:
        query = urllib.parse.urlencode({"output_format": self.output_format})
        url = f"{_elevenlabs.BASE_URL}/v1/sound-generation?{query}"
        payload: dict[str, Any] = {"text": description, "model_id": self.model_id, "loop": loop}
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        if prompt_influence is not None:
            payload["prompt_influence"] = prompt_influence
        return _elevenlabs.post_audio(
            url,
            payload,
            api_key=self.api_key,
            timeout=self.request_timeout,
            label="ElevenLabs sound effect failed",
        )
