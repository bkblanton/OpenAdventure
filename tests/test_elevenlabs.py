"""The shared ElevenLabs plumbing (auth mixin, HTTP, cache write)."""

import io
import urllib.error

import pytest

from openadventure.media import _elevenlabs


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_auth_mixin_env_fallback_and_readiness(monkeypatch):
    class Backend(_elevenlabs.ElevenLabsAuth):
        def __init__(self, api_key=None):
            self._api_key = api_key

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    backend = Backend()
    assert backend.api_key is None
    assert backend.ready is False
    assert "ELEVENLABS_API_KEY" in backend.configuration_hint

    monkeypatch.setenv("ELEVENLABS_API_KEY", "env-key")
    assert backend.api_key == "env-key"  # env is the fallback
    assert backend.ready and backend.configuration_hint == ""

    backend.api_key = "explicit"
    assert backend.api_key == "explicit"  # explicit wins over env


def test_post_audio_returns_bytes(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["key"] = request.get_header("Xi-api-key")
        return _FakeResponse(b"audio")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = _elevenlabs.post_audio(
        f"{_elevenlabs.BASE_URL}/v1/x", {"a": 1}, api_key="k", timeout=5, label="X failed"
    )
    assert out == b"audio"
    assert seen["key"] == "k"


def test_post_audio_wraps_http_errors_with_label(monkeypatch):
    url = f"{_elevenlabs.BASE_URL}/v1/x"

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"no key"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="My label: HTTP 401: no key"):
        _elevenlabs.post_audio(url, {}, api_key="k", timeout=5, label="My label")


def test_write_audio_uuid_in_cache_dir_and_explicit_path(tmp_path):
    cached = _elevenlabs.write_audio(b"x", cache_dir=tmp_path / "c", suffix=".mp3")
    assert cached.parent == tmp_path / "c" and cached.suffix == ".mp3"
    assert cached.read_bytes() == b"x"

    target = tmp_path / "deep" / "named.mp3"
    written = _elevenlabs.write_audio(b"y", path=target)
    assert written == target and target.read_bytes() == b"y"

    with pytest.raises(ValueError, match="path or cache_dir"):
        _elevenlabs.write_audio(b"z")


def test_suffix_for():
    assert _elevenlabs.suffix_for("mp3_44100_128") == ".mp3"
    assert _elevenlabs.suffix_for("pcm_16000") == ".pcm"
