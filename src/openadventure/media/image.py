"""Image generation backend (Google Gemini, a.k.a. "Nano Banana").

The default model is ``gemini-3.1-flash-image`` ("Nano Banana 2"). Calls the
REST API directly (no SDK dependency) and caches each render by prompt so the
same scene isn't billed twice. Reference images are passed inline to keep a
recurring character, item, or place visually consistent across renders.

See https://ai.google.dev/gemini-api/docs/image-generation
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# extension <- response mime type (Gemini returns PNG by default)
_MIME_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}
# mime type -> for outgoing reference images, keyed by file extension
_SUFFIX_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class GeminiImageBackend:
    """Text-to-image (and image+reference-to-image) via the Gemini REST API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_id: str = DEFAULT_GEMINI_IMAGE_MODEL,
        cache_dir: str | Path | None = None,
        aspect_ratio: str | None = None,
        request_timeout: float = 180.0,
    ):
        self._api_key = api_key
        self.model_id = model_id
        self.cache_dir = Path(cache_dir or Path(tempfile.gettempdir()) / "openadventure-images")
        self.aspect_ratio = aspect_ratio or None
        self.request_timeout = request_timeout

    @classmethod
    def from_config(cls, media_config: dict[str, Any]) -> GeminiImageBackend:
        return cls(
            api_key=media_config.get("google_api_key"),
            model_id=media_config.get("image_model", DEFAULT_GEMINI_IMAGE_MODEL),
            cache_dir=media_config.get("image_cache_dir"),
            aspect_ratio=media_config.get("image_aspect_ratio"),
        )

    @property
    def api_key(self) -> str | None:
        return self._api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")

    @api_key.setter
    def api_key(self, value: str | None) -> None:
        self._api_key = value

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    @property
    def configuration_hint(self) -> str:
        if self.ready:
            return ""
        return "Set GEMINI_API_KEY or GOOGLE_API_KEY (env or .env) or [media].google_api_key."

    async def generate(
        self,
        subject: str,
        description: str,
        *,
        reference_images: list[Path] | None = None,
    ) -> Path:
        """Render an image and return the path to the saved file."""
        prompt = self._build_prompt(subject, description)
        if not prompt:
            raise RuntimeError("Image prompt cannot be blank")
        if not self.api_key:
            raise RuntimeError(self.configuration_hint)
        refs = await asyncio.to_thread(self._read_references, reference_images or [])
        key = self._cache_key(prompt, refs)
        cached = next(self.cache_dir.glob(f"{key}.*"), None)
        if cached is not None:
            return cached
        image, mime = await asyncio.to_thread(self._request_image, prompt, refs)
        return await asyncio.to_thread(self._write_image, image, key, mime)

    # --- internals ---------------------------------------------------------
    def _build_prompt(self, subject: str, description: str) -> str:
        subject = (subject or "").strip()
        description = (description or "").strip()
        if subject and description:
            return f"{subject}. {description}"
        return description or subject

    def _read_references(self, paths: list[Path]) -> list[tuple[str, bytes]]:
        blobs: list[tuple[str, bytes]] = []
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                continue
            mime = _SUFFIX_MIME.get(path.suffix.lower(), "image/png")
            blobs.append((mime, path.read_bytes()))
        return blobs

    def _cache_key(self, prompt: str, refs: list[tuple[str, bytes]]) -> str:
        hasher = hashlib.sha256()
        hasher.update(f"{self.model_id}|{self.aspect_ratio}|{prompt}".encode())
        for mime, blob in refs:
            hasher.update(b"|ref|")
            hasher.update(mime.encode())
            hasher.update(hashlib.sha256(blob).digest())
        return hasher.hexdigest()[:24]

    def _request_image(self, prompt: str, refs: list[tuple[str, bytes]]) -> tuple[bytes, str]:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for mime, blob in refs:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(blob).decode("ascii"),
                    }
                }
            )
        generation_config: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
        if self.aspect_ratio:
            generation_config["imageConfig"] = {"aspectRatio": self.aspect_ratio}
        payload = {"contents": [{"parts": parts}], "generationConfig": generation_config}
        url = f"{GEMINI_API_BASE}/models/{self.model_id}:generateContent"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key or "",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Gemini image generation failed: HTTP {exc.code}: {detail[:500] or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini image generation failed: {exc.reason}") from exc
        return self._extract_image(body)

    def _extract_image(self, body: bytes) -> tuple[bytes, str]:
        data = json.loads(body.decode("utf-8"))
        texts: list[str] = []
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inline_data") or part.get("inlineData")
                if inline and inline.get("data"):
                    mime = str(inline.get("mime_type") or inline.get("mimeType") or "image/png")
                    if mime.startswith("image/"):
                        return base64.b64decode(inline["data"]), mime
                if isinstance(part.get("text"), str):
                    texts.append(part["text"])
        reason = " ".join(t.strip() for t in texts if t.strip())
        feedback = data.get("promptFeedback", {}).get("blockReason")
        if feedback:
            reason = f"{reason} (blocked: {feedback})".strip()
        raise RuntimeError(f"Gemini returned no image{f': {reason}' if reason else ''}")

    def _write_image(self, image: bytes, key: str, mime: str) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        suffix = _MIME_SUFFIX.get(mime, ".png")
        path = self.cache_dir / f"{key}{suffix}"
        # Write to a temp file in the same dir, then atomically swap it into place.
        # A concurrent generate() for the same prompt globs this dir for the cache
        # key; a plain write_bytes() leaves a half-written file that the glob would
        # return as a cache hit, and the viewer would then open a truncated image.
        # The temp name starts with a dot so it never matches the `{key}.*` glob.
        fd, tmp_name = tempfile.mkstemp(dir=self.cache_dir, prefix=f".{key}-", suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(image)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return path
