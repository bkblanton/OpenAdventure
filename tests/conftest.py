"""Shared fixtures: a campaign in a tmp workspace + a session factory."""

import asyncio

import pytest

from openadventure.config import AppConfig
from openadventure.engine.session import GameSession
from openadventure.media.host import MediaCapabilities
from openadventure.providers.fake import FakeProvider
from openadventure.store.workspace import Workspace


class FakeMediaHost:
    """A MediaHost that records what it was asked to present instead of touching
    speakers or a screen. The tests' stand-in for a real frontend host, so the
    backends (which generate) can be exercised without playing anything.

    ``speech_delay`` / ``sfx_delay`` make playback slow and cancellable for
    interrupt tests; ``started`` fires when the first speech/sfx playback begins.
    """

    def __init__(self, capabilities=None, *, speech_delay: float = 0.0, sfx_delay: float = 0.0):
        self._capabilities = capabilities or MediaCapabilities.all()
        self.speech: list = []  # paths handed to play_speech, in order
        self.sfx: list = []  # paths handed to play_sound_effect
        self.music: list = []  # (path, prompt) handed to play_music
        self.stopped_audio = 0
        self.music_stopped = 0
        self._volume = 0.6
        self._speech_delay = speech_delay
        self._sfx_delay = sfx_delay
        self.started = asyncio.Event()

    @property
    def capabilities(self):
        return self._capabilities

    async def play_speech(self, path):
        self.started.set()
        if self._speech_delay:
            await asyncio.sleep(self._speech_delay)
        self.speech.append(path)

    async def play_sound_effect(self, path):
        self.started.set()
        if self._sfx_delay:
            await asyncio.sleep(self._sfx_delay)
        self.sfx.append(path)

    def play_music(self, path, *, prompt="", length_seconds=None):
        self.music.append((str(path), prompt))

    def stop_music(self):
        self.music_stopped += 1

    def set_music_volume(self, value):
        self._volume = max(0.0, min(1.0, float(value)))
        return self._volume

    def music_volume(self):
        return self._volume

    def music_status_line(self):
        return f"looping {self.music[-1][1]!r}" if self.music else None

    def stop_audio(self):
        self.stopped_audio += 1

    def present_image(self, path, *, caption=""):
        return None

    def present_handout(self, title, body, *, path=None):
        return None


@pytest.fixture
def workspace(tmp_path):
    ws = Workspace(tmp_path / "workspace")
    ws.ensure()
    return ws


@pytest.fixture
def config(workspace):
    # disable embeddings in tests so building a session never loads/downloads a
    # real embedding model; hybrid-search behavior is covered with a fake backend
    return AppConfig(workspace_dir=workspace.root, embeddings={"backend": "none"})


@pytest.fixture
def campaign(workspace):
    return workspace.create_campaign("Test Quest", premise="a one-room dungeon")


@pytest.fixture
def make_session(config, workspace, campaign):
    def _make(script=None, provider=None, **kwargs):
        if provider is None and script is not None:
            provider = FakeProvider(script=script)
        return GameSession(
            config,
            workspace,
            campaign,
            provider,
            session_seed=kwargs.pop("session_seed", 42),
            **kwargs,
        )

    return _make


async def collect(aiter):
    return [event async for event in aiter]
