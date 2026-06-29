"""Music generation backend (ElevenLabs Music) and looping local playback."""

from __future__ import annotations

import asyncio
import hashlib
import os
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
from openadventure.media.tts import DEFAULT_OUTPUT_FORMAT

DEFAULT_ELEVENLABS_MUSIC_MODEL_ID = "music_v1"
DEFAULT_MUSIC_LENGTH_SECONDS = 120.0
MIN_MUSIC_LENGTH_SECONDS = 10.0
MAX_MUSIC_LENGTH_SECONDS = 300.0
DEFAULT_MUSIC_VOLUME = 0.2


@dataclass(frozen=True)
class MusicTrack:
    prompt: str
    path: Path
    length_seconds: float


def clamp_volume(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def persist_track(dest_dir: Path, src: Path, prompt: str) -> Path:
    """Copy a freshly generated track into the campaign's music dir under a
    readable, prompt-derived name, mirroring how images persist (see
    ambience_tools._persist_image). The generative backend caches by hash in a
    shared temp dir the OS may clear; this keeps the track that's actually
    playing with the campaign, so resume can replay it from disk later.

    Returns the new path, or the source unchanged when it can't be read (e.g. a
    test fake's placeholder path that points at no real file)."""
    if not src.is_file():
        return src
    from openadventure.store.workspace import slugify

    # Music prompts are long descriptive sentences; cap the slug so filenames
    # stay sane, then add a content digest to keep distinct tracks from colliding.
    slug = slugify(prompt)[:60].strip("-") or "music"
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:10]
    dest = dest_dir / f"{slug}-{digest}{src.suffix}"
    if dest.is_file():
        return dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Copy to a temp file then atomically rename, so a concurrent task (or a
    # resume about to open `dest`) never sees a half-copied file.
    fd, tmp_name = tempfile.mkstemp(dir=dest_dir, prefix=f".{dest.stem}-", suffix=dest.suffix)
    os.close(fd)
    try:
        shutil.copyfile(src, tmp_name)
        os.replace(tmp_name, dest)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return dest


class LoopingAudioPlayer:
    """Loop one audio file until stopped or replaced, with volume control.

    Windows uses a persistent PowerShell MediaPlayer that takes live
    `volume <0-1>` commands on stdin. Elsewhere a worker thread replays the
    file with whatever player is installed; a volume change restarts the
    current pass at the new level.
    """

    def __init__(self, volume: float = DEFAULT_MUSIC_VOLUME) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._volume = clamp_volume(volume)
        self._path: Path | None = None

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def playing(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop_flag.is_set()

    def play(self, path: Path) -> None:
        """Start looping `path`, replacing whatever is currently playing."""
        self.stop()
        self._stop_flag.clear()
        self._path = path
        thread = threading.Thread(target=self._loop, name="openadventure-music", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            self._send_command(process, "quit")
            process.terminate()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None

    def set_volume(self, value: float) -> float:
        volume = clamp_volume(value)
        self._volume = volume
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            if sys.platform.startswith("win"):
                self._send_command(process, f"volume {volume:.3f}")
            else:
                # the loop thread respawns the player with the new volume
                process.terminate()
        return volume

    # --- worker ----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop_flag.is_set():
            path = self._path
            if path is None:
                return
            try:
                process = self._spawn(path, self._volume)
            except RuntimeError, OSError:
                return
            with self._lock:
                self._process = process
            process.wait()
            with self._lock:
                if self._process is process:
                    self._process = None
            if sys.platform.startswith("win"):
                return  # the PowerShell script loops internally

    def _send_command(self, process: subprocess.Popen[str], command: str) -> None:
        stdin = process.stdin
        if stdin is None:
            return
        try:
            stdin.write(command + "\n")
            stdin.flush()
        except OSError, ValueError:
            pass

    def _spawn(self, path: Path, volume: float) -> subprocess.Popen[str]:
        if sys.platform.startswith("win"):
            args = [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                self._windows_script(path, volume),
            ]
            stdin = subprocess.PIPE
        else:
            args = self._posix_args(path, volume)
            stdin = subprocess.DEVNULL
        return subprocess.Popen(
            args,
            stdin=stdin,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def _posix_args(self, path: Path, volume: float) -> list[str]:
        if sys.platform == "darwin" and shutil.which("afplay"):
            return ["afplay", "-v", f"{volume:.3f}", str(path)]
        if shutil.which("mpv"):
            return [
                "mpv",
                "--no-video",
                "--really-quiet",
                f"--volume={int(volume * 100)}",
                str(path),
            ]
        if shutil.which("ffplay"):
            return [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                "-volume",
                str(int(volume * 100)),
                str(path),
            ]
        if shutil.which("mpg123"):
            return ["mpg123", "-q", "-f", str(int(volume * 32768)), str(path)]
        raise RuntimeError("No local audio player found for generated music")

    def _windows_script(self, path: Path, volume: float) -> str:
        uri = path.resolve().as_uri().replace("'", "''")
        return f"""
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
$duration = $player.NaturalDuration.TimeSpan.TotalMilliseconds
$player.Play()
$watch = [System.Diagnostics.Stopwatch]::StartNew()
$reader = [System.IO.StreamReader]::new([Console]::OpenStandardInput())
$pending = $reader.ReadLineAsync()
while ($true) {{
    if ($pending.IsCompleted) {{
        $line = $pending.Result
        if ($null -eq $line -or $line -eq 'quit') {{ break }}
        if ($line.StartsWith('volume ')) {{
            try {{ $player.Volume = [double]$line.Substring(7) }} catch {{ }}
        }}
        $pending = $reader.ReadLineAsync()
    }}
    if ($watch.ElapsedMilliseconds -ge $duration) {{
        $player.Position = [TimeSpan]::Zero
        $player.Play()
        $watch.Restart()
    }}
    Start-Sleep -Milliseconds 100
}}
$player.Stop()
$player.Close()
"""


class ElevenLabsMusic(_elevenlabs.ElevenLabsAuth):
    """ElevenLabs Music backend: compose (and cache) a track from a prompt. The
    host loops the returned file; the backend does not play it."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_id: str = DEFAULT_ELEVENLABS_MUSIC_MODEL_ID,
        output_format: str = DEFAULT_OUTPUT_FORMAT,
        cache_dir: str | Path | None = None,
        default_length_seconds: float = DEFAULT_MUSIC_LENGTH_SECONDS,
        request_timeout: float = 300.0,
    ):
        self._api_key = api_key
        self.model_id = model_id
        self.output_format = output_format
        self.cache_dir = Path(cache_dir or Path(tempfile.gettempdir()) / "openadventure-music")
        self.default_length_seconds = default_length_seconds
        self.request_timeout = request_timeout

    @classmethod
    def from_config(cls, media_config: dict[str, Any]) -> ElevenLabsMusic:
        return cls(
            api_key=media_config.get("elevenlabs_api_key"),
            model_id=media_config.get(
                "elevenlabs_music_model_id", DEFAULT_ELEVENLABS_MUSIC_MODEL_ID
            ),
            output_format=media_config.get("elevenlabs_music_output_format", DEFAULT_OUTPUT_FORMAT),
            cache_dir=media_config.get("music_cache_dir"),
            default_length_seconds=float(
                media_config.get("music_length_seconds", DEFAULT_MUSIC_LENGTH_SECONDS)
            ),
        )

    async def generate(
        self,
        prompt: str,
        *,
        length_seconds: float | None = None,
        allow_vocals: bool = False,
    ) -> MusicTrack:
        prompt = prompt.strip()
        if not prompt:
            raise RuntimeError("Music prompt cannot be blank")
        if not self.api_key:
            raise RuntimeError(self.configuration_hint)
        length = min(
            MAX_MUSIC_LENGTH_SECONDS,
            max(MIN_MUSIC_LENGTH_SECONDS, length_seconds or self.default_length_seconds),
        )
        path = self._cache_path(prompt, length, allow_vocals)
        if not path.is_file():
            audio = await asyncio.to_thread(
                self._request_audio, prompt, int(length * 1000), allow_vocals
            )
            await asyncio.to_thread(_elevenlabs.write_audio, audio, path=path)
        return MusicTrack(prompt=prompt, path=path, length_seconds=length)

    # --- internals ---------------------------------------------------------
    def _cache_path(self, prompt: str, length_seconds: float, allow_vocals: bool) -> Path:
        key = hashlib.sha256(
            f"{self.model_id}|{self.output_format}|{int(length_seconds)}|"
            f"{int(allow_vocals)}|{prompt}".encode()
        ).hexdigest()[:24]
        return self.cache_dir / f"{key}{_elevenlabs.suffix_for(self.output_format)}"

    def _request_audio(self, prompt: str, length_ms: int, allow_vocals: bool) -> bytes:
        query = urllib.parse.urlencode({"output_format": self.output_format})
        url = f"{_elevenlabs.BASE_URL}/v1/music?{query}"
        payload: dict[str, Any] = {
            "prompt": prompt,
            "model_id": self.model_id,
            "music_length_ms": length_ms,
            "force_instrumental": not allow_vocals,
        }
        return _elevenlabs.post_audio(
            url,
            payload,
            api_key=self.api_key,
            timeout=self.request_timeout,
            label="ElevenLabs music failed",
        )
