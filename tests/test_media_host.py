"""MediaCapabilities + the capability gating in the tool registry."""

from pathlib import Path

from openadventure.cli.media_host import LocalMediaHost
from openadventure.engine.tools import build_registry
from openadventure.media.host import MediaCapabilities, NullMediaHost


class _Backend:
    """Stand-in media backend: registration only checks it's non-None."""

    ready = True


def _backends():
    return (_Backend(), _Backend(), _Backend(), _Backend())  # images, music, tts, sfx


def _gm_meta(campaign):
    meta = campaign.load_meta()
    meta.mode = "gm"
    meta.tts_enabled = True
    meta.music_enabled = True
    meta.images_enabled = True
    meta.sound_effects_enabled = True
    return meta


def test_capabilities_helpers():
    assert MediaCapabilities.all().audio is True
    assert MediaCapabilities.none().audio is False
    assert MediaCapabilities(images=True).audio is False  # images aren't audio
    assert MediaCapabilities(music=True).audio is True


def test_all_capabilities_register_every_media_tool(workspace, campaign):
    meta = _gm_meta(campaign)
    reg = build_registry(
        workspace, campaign, meta, media_backends=_backends(), capabilities=MediaCapabilities.all()
    )
    for name in ("play_dialogue", "play_music", "play_sound_effect", "generate_image"):
        assert name in reg


def test_no_capabilities_hide_all_media_tools(workspace, campaign):
    meta = _gm_meta(campaign)  # every campaign toggle on, backends present...
    reg = build_registry(
        workspace, campaign, meta, media_backends=_backends(), capabilities=MediaCapabilities.none()
    )
    # ...but a text-only frontend presents none of it, so no media tool registers
    for name in (
        "play_dialogue",
        "stage_dialogue",
        "play_music",
        "play_sound_effect",
        "generate_image",
    ):
        assert name not in reg


def test_images_only_frontend_keeps_images_drops_audio(workspace, campaign):
    meta = _gm_meta(campaign)
    reg = build_registry(
        workspace,
        campaign,
        meta,
        media_backends=_backends(),
        capabilities=MediaCapabilities(images=True),
    )
    assert "generate_image" in reg
    for name in ("play_dialogue", "play_music", "play_sound_effect"):
        assert name not in reg


def test_session_capabilities_default_to_all_without_a_host(make_session):
    session = make_session(script=[])
    assert session.media_capabilities == MediaCapabilities.all()
    # No host supplied → a no-op host with every surface on (tools still register,
    # presentation is silently dropped). Preserves the headless/test behavior.
    assert isinstance(session.media_host, NullMediaHost)


# --- the real LocalMediaHost (with no-op players so nothing actually plays) --


class _RecAudioPlayer:
    """Stands in for LocalAudioPlayer (async play, used for speech + sfx)."""

    def __init__(self):
        self.played: list = []
        self.stopped = 0

    async def play(self, path):
        self.played.append(path)

    def stop(self):
        self.stopped += 1


class _RecLoopPlayer:
    """Stands in for LoopingAudioPlayer (sync play/loop, with volume)."""

    def __init__(self, volume: float = 0.4):
        self._volume = volume
        self.played: list = []
        self.stopped = 0

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def playing(self) -> bool:
        return bool(self.played) and self.stopped == 0

    def play(self, path):
        self.played.append(path)

    def stop(self):
        self.stopped += 1

    def set_volume(self, value):
        self._volume = max(0.0, min(1.0, float(value)))
        return self._volume


def _local_host():
    return LocalMediaHost(
        speech_player=_RecAudioPlayer(),
        sfx_player=_RecAudioPlayer(),
        music_player=_RecLoopPlayer(),
    )


async def test_local_host_routes_audio_to_its_players():
    host = _local_host()
    await host.play_speech(Path("a.mp3"))
    await host.play_sound_effect(Path("b.mp3"))
    assert host._speech_player.played == [Path("a.mp3")]
    assert host._sfx_player.played == [Path("b.mp3")]

    host.play_music(Path("loop.mp3"), prompt="tense drums", length_seconds=90)
    assert host._music_player.played == [Path("loop.mp3")]
    assert "tense drums" in host.music_status_line()

    host.stop_audio()
    assert host._speech_player.stopped == 1 and host._sfx_player.stopped == 1

    host.stop_music()
    assert host._music_player.stopped == 1
    assert host.music_status_line() is None  # nothing playing after a stop


def test_local_host_volume_and_capabilities():
    host = _local_host()
    assert host.capabilities == MediaCapabilities.all()
    assert host.set_music_volume(0.25) == 0.25
    assert host.music_volume() == 0.25


def test_local_host_from_config_reads_volumes():
    host = LocalMediaHost.from_config({"music_volume": 0.15})
    assert host.music_volume() == 0.15


def test_session_takes_capabilities_from_its_host(make_session):
    host = NullMediaHost(MediaCapabilities(images=True, handouts=True))
    session = make_session(script=[], media_host=host)
    assert session.media_host is host
    assert session.media_capabilities == MediaCapabilities(images=True, handouts=True)
