"""Shared ElevenLabs plumbing for the audio backends (TTS, sound effects, music).

The three backends generate audio the same way (same auth, same HTTP request and
error shape, same write-to-cache), so that lives here once: an ``ElevenLabsAuth``
mixin for the API key / readiness, and free functions for the audio POST, the
JSON requests (voice directory), and writing bytes to the cache.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

BASE_URL = "https://api.elevenlabs.io"
CONFIG_HINT = "Set ELEVENLABS_API_KEY or [media].elevenlabs_api_key."


class ElevenLabsAuth:
    """API-key handling shared by every ElevenLabs backend. Subclasses set
    ``self._api_key`` in their ``__init__``; the env var is the fallback."""

    _api_key: str | None

    @property
    def api_key(self) -> str | None:
        return self._api_key or os.environ.get("ELEVENLABS_API_KEY")

    @api_key.setter
    def api_key(self, value: str | None) -> None:
        self._api_key = value

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    @property
    def configuration_hint(self) -> str:
        return "" if self.ready else CONFIG_HINT


def _headers(api_key: str | None, accept: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Accept": accept, "xi-api-key": api_key or ""}


def post_audio(
    url: str, payload: dict[str, Any], *, api_key: str | None, timeout: float, label: str
) -> bytes:
    """POST a JSON payload and return the audio bytes. Raises ``RuntimeError``
    prefixed with ``label`` on any HTTP/URL error, so callers surface a clean
    message instead of a urllib traceback."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(api_key, "audio/mpeg"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body[:500] if body else exc.reason
        raise RuntimeError(f"{label}: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{label}: {exc.reason}") from exc


def request_json(
    url: str,
    payload: dict[str, Any] | None = None,
    method: str = "GET",
    *,
    api_key: str | None,
    timeout: float,
    label: str,
) -> dict[str, Any]:
    """Make a JSON request (the voice-directory calls) and return the decoded body."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers=_headers(api_key, "application/json"), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body[:500] if body else exc.reason
        raise RuntimeError(f"{label}: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{label}: {exc.reason}") from exc


def write_audio(
    audio: bytes, *, cache_dir: Path | None = None, path: Path | None = None, suffix: str = ".mp3"
) -> Path:
    """Write audio bytes to ``path`` (music's content-addressed cache) or to a
    fresh uuid file under ``cache_dir`` (TTS/sfx). Returns the path written."""
    if path is None:
        if cache_dir is None:
            raise ValueError("write_audio needs either path or cache_dir")
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{uuid4().hex}{suffix}"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio)
    return path


def suffix_for(output_format: str) -> str:
    """The file suffix for an ElevenLabs output_format like ``mp3_44100_128``."""
    return "." + output_format.split("_", 1)[0]
