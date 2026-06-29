"""Narration tools for GM-mode audio."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from openadventure.engine.tools.registry import Tool, ToolContext, ToolOutcome
from openadventure.media.narration import VoiceCue


class DialogueArgs(BaseModel):
    text: str = Field(description="The exact line to speak aloud.")
    speaker: str = Field(
        default="Narrator",
        description="Narrator for descriptive narration, or a stable NPC/creature name.",
    )
    role: Literal["narrator", "dialogue"] = Field(
        default="dialogue",
        description="Use narrator for descriptive prose and dialogue for character speech.",
    )
    voice_hint: str | None = Field(
        default=None,
        description=(
            "Optional first-use voice direction for tone and texture, such as old, gravelly, "
            "whispering, or warm. Do not put accents here; use the accent field instead. "
            "Use the same speaker name later for the remembered voice."
        ),
    )
    accent: str | None = Field(
        default=None,
        description=(
            "The speaker's accent, such as italian, british, southern us, cockney, or french. "
            "Set this when a new non-narrator speaker first appears so casting matches a voice "
            "actor with that accent. Omit for the Narrator or to use the campaign default accent."
        ),
    )
    gender: Literal["male", "female", "neutral"] | None = Field(
        default=None,
        description=(
            "The speaker's voice gender. Set this when a new non-narrator speaker first "
            "appears so we cast a voice actor of the correct gender. Omit for the Narrator."
        ),
    )
    age: Literal["young", "middle_aged", "old"] | None = Field(
        default=None,
        description=(
            "The speaker's apparent vocal age. Set this when a new non-narrator speaker "
            "first appears to bias casting toward a fitting actor."
        ),
    )
    interrupt: bool = Field(
        default=False,
        description="Stop currently queued narration before speaking this line.",
    )


def _narration_unavailable(ctx: ToolContext) -> ToolOutcome | None:
    if ctx.narration is None or not ctx.meta.tts_enabled:
        return ToolOutcome(
            content="Narration is not enabled. Use visible text instead.",
            summary="narration unavailable",
            ok=False,
        )
    if not ctx.narration.ready:
        hint = ctx.narration.configuration_hint
        return ToolOutcome(
            content=f"Narration is not configured: {hint}",
            summary="narration unavailable",
            ok=False,
        )
    return None


def play_dialogue(ctx: ToolContext, args: DialogueArgs) -> ToolOutcome:
    """Speak a line immediately in the background."""
    unavailable = _narration_unavailable(ctx)
    if unavailable is not None:
        return unavailable
    started = ctx.narration.queue_line(
        args.text,
        speaker=args.speaker,
        role=args.role,
        voice_hint=args.voice_hint,
        accent=args.accent,
        gender=args.gender,
        age=args.age,
        interrupt=args.interrupt,
    )
    return ToolOutcome(
        content=f"Voice line for {args.speaker!r} is playing in the background.",
        events=[started],
        summary=f"voice: {args.speaker}",
    )


def stage_dialogue(ctx: ToolContext, args: DialogueArgs) -> ToolOutcome:
    """Stage voice metadata for exact text that will appear in the final visible response."""
    unavailable = _narration_unavailable(ctx)
    if unavailable is not None:
        return unavailable
    if args.interrupt:
        ctx.narration.interrupt()
    ctx.voice_cues.append(
        VoiceCue(
            text=args.text,
            speaker=args.speaker,
            role=args.role,
            voice_hint=args.voice_hint,
            accent=args.accent,
            gender=args.gender,
            age=args.age,
        )
    )
    ctx.narration_cues += 1
    return ToolOutcome(
        content=(
            f"Voice cue for {args.speaker!r} is saved. It will only be spoken with this "
            "speaker if the final visible response contains the exact cue text."
        ),
        summary=f"voice cue: {args.speaker}",
    )


class CastLookupArgs(BaseModel):
    speaker: str | None = Field(
        default=None,
        description=(
            "A speaker name to check, such as Dooley. Omit to list every speaker already cast."
        ),
    )


def cast_lookup(ctx: ToolContext, args: CastLookupArgs) -> ToolOutcome:
    """Report a speaker's saved voice, or list the cast, so a returning NPC isn't re-cast."""
    unavailable = _narration_unavailable(ctx)
    if unavailable is not None:
        return unavailable
    if args.speaker:
        entry = ctx.narration.find_cast_entry(args.speaker)
        if entry is None:
            return ToolOutcome(
                content=(
                    f"{args.speaker!r} is not cast yet. Introduce them with gender, age, and "
                    "accent so casting matches the character."
                ),
                summary=f"cast: {args.speaker} (new)",
            )
        bits = [f'voice "{entry.voice_name}"']
        if entry.target_accent:
            bits.append(f"accent {entry.target_accent}")
        if entry.target_gender:
            bits.append(f"gender {entry.target_gender}")
        if entry.voice_hint:
            bits.append(f"hint {entry.voice_hint}")
        return ToolOutcome(
            content=(
                f"{entry.speaker!r} is already cast ({', '.join(bits)}). Reuse speaker "
                f'"{entry.speaker}" and do not re-set gender, age, or accent.'
            ),
            summary=f"cast: {entry.speaker}",
        )
    names = [a.speaker for a in ctx.narration.voice_cast().speakers.values()]
    if not names:
        return ToolOutcome(content="No speakers are cast yet.", summary="cast: empty")
    shown = names[:40]
    more = len(names) - len(shown)
    listing = ", ".join(shown) + (f", plus {more} more" if more > 0 else "")
    return ToolOutcome(content=f"Cast speakers: {listing}.", summary=f"cast: {len(names)} speakers")


PLAY_DIALOGUE = Tool(
    name="play_dialogue",
    description=(
        "Speak one line aloud immediately in the background. Use speaker='Narrator' for "
        "narration, otherwise a stable NPC or creature name so the saved voice cast stays "
        "consistent. A speaker keeps the voice from their first line automatically, so reuse "
        "the exact same name for a returning character and do not re-set their gender, age, or "
        "accent. When introducing a new speaker, set gender, age, and accent so the cast voice "
        "matches, and use voice_hint for finer tone direction. The spoken line does not need "
        "to appear in your visible response."
    ),
    args_model=DialogueArgs,
    handler=play_dialogue,
)

STAGE_DIALOGUE = Tool(
    name="stage_dialogue",
    description=(
        "Stage speaker and voice metadata for exact text that will appear in the final "
        "visible response, so that text is spoken in the chosen voice. The response is "
        "already read aloud in the default Narrator voice, so stage only quoted lines from a "
        "non-narrator speaker (use a stable NPC or creature name so the saved voice cast "
        "stays consistent), and do not stage plain narration. A speaker keeps the voice from "
        "their first line automatically, so reuse the exact same name for a returning "
        "character and do not re-set their gender, age, or accent. When introducing a new "
        "speaker, set gender, age, and accent so the cast voice matches, and use voice_hint "
        "for finer tone direction. Use speaker='Narrator' only to override the default "
        "narrator voice for a specific line. The cue is only spoken if the final visible "
        "response contains the exact text."
    ),
    args_model=DialogueArgs,
    handler=stage_dialogue,
)

CAST_LOOKUP = Tool(
    name="cast_lookup",
    description=(
        "Check whether a speaker already has a saved voice before introducing them, so a "
        "returning NPC keeps one voice instead of being cast twice. Pass speaker to see that "
        "character's saved voice and the exact name to reuse, or omit speaker to list every "
        "cast speaker. On-stage NPCs already show their cast status in context, so use this "
        "mainly for off-stage or uncertain names."
    ),
    args_model=CastLookupArgs,
    handler=cast_lookup,
    parallel_safe=True,
    read_only=True,
)

NARRATION_TOOLS = [PLAY_DIALOGUE, STAGE_DIALOGUE, CAST_LOOKUP]
