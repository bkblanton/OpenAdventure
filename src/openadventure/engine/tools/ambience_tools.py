"""Ambience tools (image/music): fire-and-forget background tasks so a slow
render never blocks play. Registered only when a backend is configured."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from openadventure.engine.events import ImageGenerated, MusicStarted, MusicStopped, ShowImage
from openadventure.engine.tools.registry import Tool, ToolContext, ToolOutcome
from openadventure.media.base import ImageBackend, MusicBackend, SoundEffectsBackend
from openadventure.media.narration import SoundEffectCue
from openadventure.media.sound_effects import DEFAULT_SFX_ESTIMATED_DURATION_SECONDS
from openadventure.providers.base import Usage
from openadventure.util import shorten


class GenerateImageArgs(BaseModel):
    subject: str = Field(description="What to depict, e.g. 'the innkeeper Marta'")
    description: str = Field(
        description="Detailed visual description: appearance, mood, lighting, style"
    )
    reference_images: list[str] = Field(
        default_factory=list,
        description=(
            "Optional paths to existing images (e.g. from find_images) to guide the look, "
            "so a recurring character, item, or place stays visually consistent."
        ),
    )


class ShowImageArgs(BaseModel):
    image: str = Field(
        description=(
            "Which already-generated image to display on the player's screen: a path "
            "(from find_images or a prior generate_image) or text matching its caption."
        )
    )


class FindImagesArgs(BaseModel):
    query: str = Field(
        default="",
        description=(
            "Optional text to filter by, matched against image captions. Leave empty to "
            "list every generated image, newest first."
        ),
    )


class PlayMusicArgs(BaseModel):
    prompt: str = Field(
        description=(
            "Music description: mood, genre, instrumentation, tempo, e.g. 'tense "
            "low-string dungeon ambience with distant drums, slow and ominous' or "
            "'warm tavern folk tune, fiddle and lute, cheerful'"
        )
    )
    length_seconds: float | None = Field(
        default=None,
        ge=10,
        le=300,
        description=(
            "Generated track length in seconds before it loops (default ~120). "
            "Longer tracks loop less obviously but take longer to generate."
        ),
    )
    allow_vocals: bool = Field(
        default=False, description="Allow sung vocals. Default is instrumental-only."
    )


class StopMusicArgs(BaseModel):
    pass


class PlaySoundEffectArgs(BaseModel):
    description: str = Field(
        description=(
            "Detailed sound prompt, e.g. 'a rusty iron portcullis grinding open in a "
            "stone dungeon, short and heavy'"
        )
    )
    duration_seconds: float | None = Field(
        default=None,
        ge=0.5,
        le=30,
        description="Optional generated audio duration in seconds, 0.5 to 30.",
    )
    prompt_influence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Optional prompt adherence from 0 to 1. Higher follows the prompt more closely.",
    )
    loop: bool = Field(default=False, description="Generate a smoothly looping sound effect.")


class StageSoundEffectArgs(PlaySoundEffectArgs):
    after_text: str | None = Field(
        default=None,
        description=(
            "The exact sentence from the final visible narration that DESCRIBES this sound's "
            "moment (the line where the door actually opens, the blow actually lands), copied "
            "verbatim with its punctuation. The sound plays right after that sentence, so anchor "
            "it to the beat itself, not an earlier scene-setting line, or the effect fires too "
            "early. Omit only for steady background ambience meant to start at the top of the "
            "narration; an omitted cue is placed after the next sentence in cue order."
        ),
    )


def _generated_images(ctx: ToolContext) -> list[tuple[str, str]]:
    """(caption, path) for each generated image, newest first, de-duplicated by path."""
    seen: set[str] = set()
    images: list[tuple[str, str]] = []
    for entry in reversed(ctx.log.read_all()):
        if entry.type != "media" or entry.data.get("kind") != "image":
            continue
        if entry.data.get("action") == "show":
            continue
        path = entry.data.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        caption = str(entry.data.get("caption") or entry.data.get("subject") or "")
        images.append((caption, path))
    return images


def _resolve_reference_paths(
    ctx: ToolContext, raw_paths: list[str]
) -> tuple[list[Path], list[str]]:
    """Resolve reference image paths against the cwd and the campaign images dir.
    Returns (existing paths, requested-but-missing originals)."""
    found: list[Path] = []
    missing: list[str] = []
    for raw in raw_paths:
        resolved = _resolve_existing_image(ctx, raw)
        if resolved is None:
            missing.append(raw)
        else:
            found.append(resolved)
    return found, missing


def _within_images_dir(path: Path, images_root: Path) -> bool:
    """True if `path` resolves to a file inside the campaign images dir. Keeps the
    model from pointing show_image/reference_images at arbitrary files on disk;
    every legitimate image path it sees comes from find_images, which only ever
    lists images under this directory."""
    try:
        path.resolve().relative_to(images_root.resolve())
    except OSError, ValueError:
        return False
    return True


def _resolve_existing_image(ctx: ToolContext, raw: str) -> Path | None:
    """Find an already-generated image by path, campaign-relative path, or caption.
    Only images inside the campaign images dir are accepted."""
    raw = (raw or "").strip().strip('"')
    if not raw:
        return None
    images_root = ctx.campaign.images_dir
    for candidate in (Path(raw), images_root / raw):
        if _within_images_dir(candidate, images_root) and candidate.is_file():
            return candidate
    lowered = raw.lower()
    for caption, path in _generated_images(ctx):
        if lowered in caption.lower():
            candidate = Path(path)
            if _within_images_dir(candidate, images_root) and candidate.is_file():
                return candidate
    return None


def _caption_for(ctx: ToolContext, path: Path) -> str:
    target = str(path)
    for caption, recorded in _generated_images(ctx):
        if recorded == target and caption:
            return caption
    return path.stem


def _persist_image(ctx: ToolContext, src: Path, subject: str) -> Path:
    """Copy a freshly rendered image into the campaign's images dir so it persists
    and can be found later. Passes the path through unchanged when it can't be read
    (e.g. test fakes that return a placeholder path)."""
    if not src.is_file():
        return src
    from openadventure.store.workspace import slugify

    images_dir = ctx.campaign.images_dir
    images_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:10]
    dest = images_dir / f"{slugify(subject) or 'image'}-{digest}{src.suffix}"
    if not dest.is_file():
        # Copy to a temp file then atomically rename, so a concurrent task (or the
        # viewer about to open `dest`) never sees a partially-copied file.
        fd, tmp_name = tempfile.mkstemp(dir=images_dir, prefix=f".{dest.stem}-", suffix=dest.suffix)
        os.close(fd)
        try:
            shutil.copyfile(src, tmp_name)
            os.replace(tmp_name, dest)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    return dest


def make_ambience_tools(
    images: ImageBackend | None,
    music: MusicBackend | None,
    sound_effects: SoundEffectsBackend | None = None,
    *,
    usage_recorder: Callable[[Usage, str, str, str | None], None] | None = None,
) -> list[Tool]:
    tools: list[Tool] = []

    def record_media_usage(ctx: ToolContext, usage: Usage, kind: str, backend: object) -> None:
        """Record only after a backend has successfully generated its asset.

        Most callers carry the recorder on their ``ToolContext``. The optional
        factory argument keeps standalone registries useful for integrations that
        do not construct a full GameSession.
        """

        backend_name = type(backend).__name__
        model_id = getattr(backend, "model_id", None)
        if ctx.usage_recorder is not None:
            ctx.record_media_usage(
                usage,
                kind=kind,
                backend_name=backend_name,
                model_id=model_id,
            )
        elif usage_recorder is not None:
            usage_recorder(usage, kind, backend_name, model_id)

    if images is not None:

        def generate_image(ctx: ToolContext, args: GenerateImageArgs) -> ToolOutcome:
            if not getattr(images, "ready", True):
                hint = getattr(images, "configuration_hint", "image generation unavailable")
                return ToolOutcome(
                    content=f"Image generation is not configured: {hint}",
                    summary="images unavailable",
                    ok=False,
                )
            refs, missing = _resolve_reference_paths(ctx, args.reference_images)

            async def work():
                path = await images.generate(
                    args.subject, args.description, reference_images=refs or None
                )
                record_media_usage(ctx, Usage(image_count=1), "image", images)
                saved = _persist_image(ctx, path, args.subject)
                ctx.log.append(
                    "media",
                    {
                        "kind": "image",
                        "path": str(saved),
                        "subject": args.subject,
                        "caption": args.subject,
                        "prompt": args.description,
                        "references": [str(r) for r in refs],
                    },
                )
                return [ImageGenerated(path=str(saved), caption=args.subject)]

            started = ctx.background.spawn("image", f"Generating image of {args.subject}…", work())
            note = f" (couldn't find reference: {', '.join(missing)})" if missing else ""
            return ToolOutcome(
                content=(
                    f"Image of {args.subject!r} is rendering in the background "
                    f"({started.task_id}) and will open on the player's screen when "
                    f"ready{note}. Keep playing."
                ),
                events=[started],
                summary=f"image started: {args.subject}",
            )

        def show_image(ctx: ToolContext, args: ShowImageArgs) -> ToolOutcome:
            resolved = _resolve_existing_image(ctx, args.image)
            if resolved is None:
                return ToolOutcome(
                    content=(
                        f"No generated image matches {args.image!r}. Use find_images to "
                        "list what's available, or generate_image to make a new one."
                    ),
                    summary="image not found",
                    ok=False,
                )
            caption = _caption_for(ctx, resolved)
            return ToolOutcome(
                content=f"Showing {caption!r} on the player's screen.",
                events=[ShowImage(path=str(resolved), caption=caption)],
                summary=f"showing image: {caption}",
            )

        def find_images(ctx: ToolContext, args: FindImagesArgs) -> ToolOutcome:
            query = args.query.strip().lower()
            rows = [
                (caption, path)
                for caption, path in _generated_images(ctx)
                if not query or query in caption.lower() or query in path.lower()
            ]
            if not rows:
                msg = (
                    "No images have been generated yet."
                    if not query
                    else f"No generated images match {args.query!r}."
                )
                return ToolOutcome(content=msg, summary="no images")
            lines = []
            for caption, path in rows:
                exists = "" if Path(path).is_file() else " (file missing)"
                lines.append(f"- {caption!r}: {path}{exists}")
            return ToolOutcome(
                content=(
                    "Previously generated images (newest first). Pass a path to show_image "
                    "to display it, or to generate_image's reference_images to reuse the "
                    "look:\n" + "\n".join(lines)
                ),
                summary=f"{len(rows)} image(s)",
            )

        tools.append(
            Tool(
                name="generate_image",
                description=(
                    "Generate an image (an NPC, item, creature, or scene) and show it on the "
                    "player's screen. Write a vivid, concrete description. Optionally pass "
                    "reference_images (paths from find_images) to keep a recurring subject "
                    "visually consistent. Runs in the background; narrate onward, and the image "
                    "opens when ready."
                ),
                args_model=GenerateImageArgs,
                handler=generate_image,
            )
        )
        tools.append(
            Tool(
                name="show_image",
                description=(
                    "Display an already-generated image on the player's screen again, by path "
                    "(from find_images) or by text matching its caption. Use this to re-show "
                    "an earlier NPC, map, or item instead of regenerating it."
                ),
                args_model=ShowImageArgs,
                handler=show_image,
            )
        )
        tools.append(
            Tool(
                name="find_images",
                description=(
                    "List images already generated for this campaign (optionally filtered by "
                    "caption). Use it to find an earlier image to show_image, or to reuse as a "
                    "reference_image when generating a consistent-looking subject."
                ),
                args_model=FindImagesArgs,
                handler=find_images,
                read_only=True,
            )
        )

    if music is not None:

        def play_music(ctx: ToolContext, args: PlayMusicArgs) -> ToolOutcome:
            ready = getattr(music, "ready", True)
            if not ready:
                hint = getattr(music, "configuration_hint", "music unavailable")
                return ToolOutcome(
                    content=f"Music is not configured: {hint}",
                    summary="music unavailable",
                    ok=False,
                )

            async def work():
                from openadventure.media.music import persist_track

                track = await music.generate(
                    args.prompt,
                    length_seconds=args.length_seconds,
                    allow_vocals=args.allow_vocals,
                )
                length_seconds = getattr(track, "length_seconds", None)
                if length_seconds is None:
                    length_seconds = args.length_seconds or getattr(
                        music, "default_length_seconds", 0.0
                    )
                if length_seconds:
                    record_media_usage(
                        ctx,
                        Usage(music_seconds=float(length_seconds)),
                        "music",
                        music,
                    )
                # Persist into the campaign's music dir under a readable name, so
                # the track lives with the campaign and resume can replay it.
                path = persist_track(
                    ctx.campaign.music_dir, Path(getattr(track, "path", "")), args.prompt
                )
                if ctx.media_host is not None:
                    ctx.media_host.play_music(
                        path,
                        prompt=args.prompt,
                        length_seconds=getattr(track, "length_seconds", None),
                    )
                ctx.log.append(
                    "media",
                    {
                        "kind": "music",
                        "prompt": args.prompt,
                        "path": str(path),
                        "length_seconds": length_seconds,
                    },
                )
                return [MusicStarted(track=args.prompt)]

            # a newer request supersedes any still-generating track
            ctx.background.cancel_kind("music")
            label = f"Composing music: {shorten(args.prompt)}…"
            started = ctx.background.spawn("music", label, work())
            return ToolOutcome(
                content=(
                    f"Music {args.prompt!r} is generating in the background "
                    f"({started.task_id}). When ready it will play on loop, replacing "
                    "any current track. Keep playing; do not wait for it."
                ),
                events=[started],
                summary=f"music: {shorten(args.prompt)}",
            )

        def stop_music(ctx: ToolContext, args: StopMusicArgs) -> ToolOutcome:
            ctx.background.cancel_kind("music")

            async def work():
                if ctx.media_host is not None:
                    ctx.media_host.stop_music()
                ctx.log.append("media", {"kind": "music", "action": "stop"})
                return [MusicStopped()]

            started = ctx.background.spawn("music-stop", "Stopping music…", work())
            return ToolOutcome(
                content="Stopping the music.", events=[started], summary="music stopped"
            )

        tools.append(
            Tool(
                name="play_music",
                description=(
                    "Generate looping background music from a text description and play "
                    "it, replacing the current track. Generation takes a minute and runs "
                    "in the background. Use when the scene's mood or location changes "
                    "meaningfully, not every turn."
                ),
                args_model=PlayMusicArgs,
                handler=play_music,
            )
        )
        tools.append(
            Tool(
                name="stop_music",
                description="Stop the looping background music.",
                args_model=StopMusicArgs,
                handler=stop_music,
            )
        )

    if sound_effects is not None:

        def _unavailable() -> ToolOutcome | None:
            if getattr(sound_effects, "ready", True):
                return None
            hint = getattr(sound_effects, "configuration_hint", "sound effects unavailable")
            return ToolOutcome(
                content=f"Sound effects are not configured: {hint}",
                summary="sound effect unavailable",
                ok=False,
            )

        def play_sound_effect(ctx: ToolContext, args: PlaySoundEffectArgs) -> ToolOutcome:
            unavailable = _unavailable()
            if unavailable is not None:
                return unavailable

            async def work():
                path = await sound_effects.generate(
                    args.description,
                    duration_seconds=args.duration_seconds,
                    prompt_influence=args.prompt_influence,
                    loop=args.loop,
                )
                record_media_usage(
                    ctx,
                    Usage(
                        sound_effect_seconds=(
                            args.duration_seconds or DEFAULT_SFX_ESTIMATED_DURATION_SECONDS
                        )
                    ),
                    "sound_effect",
                    sound_effects,
                )
                if ctx.media_host is not None:
                    await ctx.media_host.play_sound_effect(path)
                ctx.log.append(
                    "media",
                    {
                        "kind": "sound_effect",
                        "path": str(path),
                        "description": args.description,
                        "duration_seconds": args.duration_seconds,
                        "loop": args.loop,
                    },
                )
                return []

            label = f"Generating sound effect: {shorten(args.description)}"
            started = ctx.background.spawn("sfx", label, work())
            return ToolOutcome(
                content=(
                    f"Sound effect is generating and will play in the background "
                    f"({started.task_id})."
                ),
                events=[started],
                summary=f"sfx: {shorten(args.description)}",
            )

        def stage_sound_effect(ctx: ToolContext, args: StageSoundEffectArgs) -> ToolOutcome:
            unavailable = _unavailable()
            if unavailable is not None:
                return unavailable
            ctx.sound_effect_cues.append(
                SoundEffectCue(
                    description=args.description,
                    duration_seconds=args.duration_seconds,
                    prompt_influence=args.prompt_influence,
                    loop=args.loop,
                    after_text=args.after_text,
                )
            )
            return ToolOutcome(
                content=(
                    "Sound effect cue saved for the final visible narration. "
                    "Make sure the final response visibly describes the same in-world beat."
                ),
                summary=f"sfx cue: {shorten(args.description)}",
            )

        tools.append(
            Tool(
                name="play_sound_effect",
                description=(
                    "Generate and play a short one-shot sound effect immediately for the current "
                    "scene beat. Use sparingly for concrete sounds like doors, impacts, spells, "
                    "weather, monster calls, or environmental stingers. Runs in the background."
                ),
                args_model=PlaySoundEffectArgs,
                handler=play_sound_effect,
            )
        )
        tools.append(
            Tool(
                name="stage_sound_effect",
                description=(
                    "Stage a short one-shot sound effect to play with the final visible "
                    "narration, synced to a beat in that text. Use sparingly for concrete "
                    "sounds like doors, impacts, spells, weather, monster calls, or "
                    "environmental stingers. Set after_text to the exact sentence that "
                    "describes the sound so it lands on that moment, not an earlier line, "
                    "and make sure the final response visibly describes that same in-world beat."
                ),
                args_model=StageSoundEffectArgs,
                handler=stage_sound_effect,
            )
        )

    return tools
