"""Text-to-speech backends and local audio playback."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openadventure.media import _elevenlabs

DEFAULT_ELEVENLABS_VOICE_ID = "6FiCmD8eY5VyjOdG5Zjk"
DEFAULT_ELEVENLABS_MODEL_ID = "eleven_flash_v2_5"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"
DEFAULT_TTS_VOLUME = 1.0


def extract_voice_id(raw: str) -> str:
    """Pull an ElevenLabs voice id out of a raw id or a voice-library URL.

    Accepts a bare id (``"6FiCmD8eY5VyjOdG5Zjk"``) or a URL that carries it as a
    ``voiceId`` query parameter (the share/library link the ElevenLabs UI
    copies) or as the final path segment. Returns the id, or the stripped input
    unchanged when there's nothing URL-shaped to unpack."""
    text = raw.strip().strip("\"'")
    if not text or not ("://" in text or "?" in text or "/" in text):
        return text
    parsed = urllib.parse.urlsplit(text if "://" in text else f"//{text}")
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("voiceId", "voice_id"):
        if query.get(key):
            return query[key][0].strip()
    segments = [segment for segment in parsed.path.split("/") if segment]
    return segments[-1].strip() if segments else text


@dataclass(frozen=True)
class VoiceRecord:
    voice_id: str
    name: str
    source: str
    description: str = ""
    public_owner_id: str | None = None
    accent: str | None = None
    gender: str | None = None
    age: str | None = None
    category: str | None = None
    preview_url: str | None = None


# Emoji and other pictographs survive markdown stripping but don't voice, so drop
# them so a "📰 Boston Globe" bullet doesn't leave a dangling silence.
_PICTOGRAPH_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"  # symbols, pictographs, emoji, supplemental
    "\U00002600-\U000027bf"  # misc symbols, dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicator (flags)
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U00002b00-\U00002bff"  # misc symbols and arrows
    "\U00002190-\U000021ff"  # arrows
    "\U00002300-\U000023ff"  # misc technical (incl. ⏏ etc.)
    "]+",
    flags=re.UNICODE,
)


def speech_text(markdown: str) -> str:
    """Make assistant markdown pleasant enough to read aloud.

    Markdown formatting is dropped before TTS, not voiced, so a heading or a
    bulleted list, which a reader sees as separate beats, would otherwise
    collapse into one breathless run-on ("where to next boston globe dig the
    clippings hall of records ..."). We strip the markup but turn each block
    (heading, list item, paragraph) into its own sentence, so the synthesized
    voice pauses where the layout implied one.
    """
    text = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~>#|]", " ", text)
    text = _PICTOGRAPH_RE.sub(" ", text)
    # Each non-empty line was a visual beat; give it terminal punctuation so the
    # voice breaks between beats instead of running them together, then join.
    beats = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            continue
        if line[-1] not in ".!?:;,—-":
            line += "."
        beats.append(line)
    return " ".join(beats).strip()


class LocalAudioPlayer:
    """Play a generated audio file with whatever the host OS already has."""

    def __init__(self, volume: float = 1.0) -> None:
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._stopping = False
        self._volume = max(0.0, min(1.0, float(volume)))

    @property
    def volume(self) -> float:
        return self._volume

    async def play(self, path: Path) -> None:
        await asyncio.to_thread(self._play_sync, path)

    def stop(self) -> None:
        with self._process_lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            self._stopping = True
            process.terminate()

    def _play_sync(self, path: Path) -> None:
        volume = self._volume
        if sys.platform.startswith("win"):
            self._play_windows(path, volume)
            return
        if sys.platform == "darwin" and shutil.which("afplay"):
            self._run_player(["afplay", "-v", f"{volume:.3f}", str(path)])
            return
        for command in ("mpg123", "mpv", "ffplay"):
            executable = shutil.which(command)
            if not executable:
                continue
            if command == "ffplay":
                args = [
                    executable,
                    "-autoexit",
                    "-nodisp",
                    "-volume",
                    str(int(volume * 100)),
                    str(path),
                ]
            elif command == "mpv":
                args = [executable, f"--volume={int(volume * 100)}", str(path)]
            else:  # mpg123
                args = [executable, "-f", str(int(volume * 32768)), str(path)]
            self._run_player(args)
            return
        raise RuntimeError("No local audio player found for generated TTS audio")

    def _run_player(self, args: list[str]) -> None:
        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._process_lock:
            self._process = process
            self._stopping = False
        _, stderr = process.communicate()
        with self._process_lock:
            stopped = self._stopping
            if self._process is process:
                self._process = None
                self._stopping = False
        if process.returncode and not stopped:
            detail = (stderr or "").strip()
            raise RuntimeError(detail or f"Audio player exited with code {process.returncode}")

    def _play_windows(self, path: Path, volume: float = 1.0) -> None:
        uri = path.resolve().as_uri().replace("'", "''")
        script = f"""
Add-Type -AssemblyName PresentationCore
$player = [System.Windows.Media.MediaPlayer]::new()
$player.Open([Uri]::new('{uri}'))
for ($i = 0; $i -lt 200 -and -not $player.NaturalDuration.HasTimeSpan; $i++) {{
    Start-Sleep -Milliseconds 50
}}
if (-not $player.NaturalDuration.HasTimeSpan) {{
    throw 'Audio did not load'
}}
$player.Volume = {volume:.3f}
$player.Play()
Start-Sleep -Milliseconds ([Math]::Ceiling($player.NaturalDuration.TimeSpan.TotalMilliseconds) + 250)
$player.Close()
"""
        self._run_player(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        )


class ElevenLabsTTS(_elevenlabs.ElevenLabsAuth):
    """ElevenLabs implementation of the TTSBackend protocol: synthesize speech to
    a cached file. Playback is the host's job (see media/host.py)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        voice_id: str = DEFAULT_ELEVENLABS_VOICE_ID,
        model_id: str = DEFAULT_ELEVENLABS_MODEL_ID,
        output_format: str = DEFAULT_OUTPUT_FORMAT,
        cache_dir: str | Path | None = None,
        request_timeout: float = 60.0,
    ):
        self._api_key = api_key
        self.voice_id = voice_id
        self.model_id = model_id
        self.output_format = output_format
        self.cache_dir = Path(cache_dir or Path(tempfile.gettempdir()) / "openadventure-tts")
        self.request_timeout = request_timeout

    @classmethod
    def from_config(cls, media_config: dict[str, Any]) -> ElevenLabsTTS:
        return cls(
            api_key=media_config.get("elevenlabs_api_key"),
            voice_id=media_config.get("elevenlabs_voice_id", DEFAULT_ELEVENLABS_VOICE_ID),
            model_id=media_config.get("elevenlabs_model_id", DEFAULT_ELEVENLABS_MODEL_ID),
            output_format=media_config.get("elevenlabs_output_format", DEFAULT_OUTPUT_FORMAT),
            cache_dir=media_config.get("tts_cache_dir"),
        )

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        voice_settings: dict[str, Any] | None = None,
    ) -> Path | None:
        """Fetch and cache the audio for ``text``, returning its file path
        (None when there's nothing speakable).

        Generation only: the host plays the returned file. Returning the path
        (rather than playing) lets the narrator pre-fetch the next clip while the
        current one is still playing: the ElevenLabs round-trip is the gap you'd
        otherwise hear before a voice starts.
        """
        clean_text = speech_text(text)
        if not clean_text:
            return None
        if not self.api_key:
            raise RuntimeError(self.configuration_hint)
        audio = await asyncio.to_thread(self._request_audio, clean_text, voice_id, voice_settings)
        return await asyncio.to_thread(
            _elevenlabs.write_audio,
            audio,
            cache_dir=self.cache_dir,
            suffix=_elevenlabs.suffix_for(self.output_format),
        )

    async def list_voices(self) -> list[VoiceRecord]:
        if not self.api_key:
            raise RuntimeError(self.configuration_hint)
        payload = await asyncio.to_thread(self._voices_json, f"{_elevenlabs.BASE_URL}/v2/voices")
        voices = []
        for raw in payload.get("voices", []):
            record = self._voice_record(raw, source="owned")
            if record is not None:
                voices.append(record)
        return voices

    def _voices_json(
        self, url: str, payload: dict[str, Any] | None = None, method: str = "GET"
    ) -> dict[str, Any]:
        return _elevenlabs.request_json(
            url,
            payload,
            method,
            api_key=self.api_key,
            timeout=self.request_timeout,
            label="ElevenLabs voices failed",
        )

    def _voice_record(self, raw: dict[str, Any], *, source: str) -> VoiceRecord | None:
        voice_id = raw.get("voice_id")
        name = raw.get("name")
        if not voice_id or not name:
            return None
        sharing = raw.get("sharing") if isinstance(raw.get("sharing"), dict) else {}
        labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
        return VoiceRecord(
            voice_id=str(voice_id),
            name=str(name),
            source=source,
            description=str(raw.get("description") or sharing.get("description") or ""),
            public_owner_id=raw.get("public_owner_id") or sharing.get("public_owner_id"),
            accent=raw.get("accent") or labels.get("accent") or sharing.get("accent"),
            gender=raw.get("gender") or labels.get("gender") or sharing.get("gender"),
            age=raw.get("age") or labels.get("age") or sharing.get("age"),
            category=raw.get("category") or sharing.get("category"),
            preview_url=raw.get("preview_url") or sharing.get("preview_url"),
        )

    def _request_audio(
        self,
        text: str,
        voice_id: str | None,
        voice_settings: dict[str, Any] | None,
    ) -> bytes:
        query = urllib.parse.urlencode({"output_format": self.output_format})
        selected_voice_id = urllib.parse.quote(voice_id or self.voice_id, safe="")
        url = f"{_elevenlabs.BASE_URL}/v1/text-to-speech/{selected_voice_id}?{query}"
        payload: dict[str, Any] = {"text": text, "model_id": self.model_id}
        if voice_settings:
            payload["voice_settings"] = voice_settings
        return _elevenlabs.post_audio(
            url,
            payload,
            api_key=self.api_key,
            timeout=self.request_timeout,
            label="ElevenLabs TTS failed",
        )
