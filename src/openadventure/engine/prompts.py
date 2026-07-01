"""System prompt + synthetic context block assembly.

The context splits by time, not just by stability. The byte-stable system prompt
(mode + sources + house rules) leads and caches across a campaign. The volatile
context then divides in two: ``build_context_head`` (premise, arc, canon, the
story so far) is long-term reference and rides BEFORE the replayed history, while
``build_context_foot`` (scene, prepped location, roster, encounter, clocks, NPCs,
music, settings) is point-in-time state and rides AFTER the history, immediately
before the player's live message, so it reads as "now" rather than as a stale
header above a history that climbs toward it.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import TYPE_CHECKING

from openadventure.providers.base import SystemBlock

if TYPE_CHECKING:
    from openadventure.store.workspace import CampaignMeta, Workspace

_PROMPT_FILES = {
    "gm": "gm_system_prompt.md",
    "assistant": "assistant_system_prompt.md",
}

_VERBOSITY_INSTRUCTIONS = {
    "low": (
        "- Response verbosity: low. Answer in one or two sentences on average. "
        "Don't recap what the player already knows; state only what's new, then the "
        "next hook. Fold any list of options into a single sentence rather than "
        "enumerating them. Keep game facts, rules, numbers, names, and player "
        "choices exact."
    ),
    "medium": (
        "- Response verbosity: medium. A short paragraph on average. Give enough scene "
        "(a sensory detail or two, a line of NPC voice where it fits) for the player "
        "to picture the moment and act, then end with a clear hook. Don't re-narrate "
        "context the player already has, and keep it tight enough to follow when read aloud."
    ),
    "high": (
        "- Response verbosity: high. Balance vivid tabletop narration with forward "
        "motion. Give enough detail to play from without lingering."
    ),
}

# Earlier turns in the transcript may have been written under different verbosity or
# style settings (a campaign's settings can change mid-game). The model imitates its
# own past narration, so without this it keeps matching the old length/tone instead of
# the current one. This pins the live settings as authoritative over the transcript.
_SETTINGS_DRIFT_NOTE = (
    "- Some of your own earlier GM narration in the transcript may have been written "
    "under different verbosity, tone, or interactivity settings than the ones now in "
    "force, since a campaign's settings can change mid-game. The instructions here and "
    "the current Session settings are authoritative: match the length, tone, and "
    "interactivity they call for. Do not treat the style or length of your past messages "
    "as the target to imitate; where they differ from the current settings, that is "
    "stale style to correct, not a pattern to continue."
)


def _load_prompt(mode: str) -> str:
    name = _PROMPT_FILES.get(mode, _PROMPT_FILES["gm"])
    path = resources.files("openadventure.data") / name
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (resources.files("openadventure.data") / _PROMPT_FILES["gm"]).read_text(
            encoding="utf-8"
        )


def load_character_template(meta: CampaignMeta, workspace: Workspace | None) -> dict | None:
    """The system source's derived character template, or None if not generated yet.

    Templates are derived from a campaign's ``system_source`` (see
    ``ingest.template_gen``). There is no shipped baseline, so a campaign with no
    system source (or an as-yet-underived one) simply has no template.
    """
    if workspace is None or not meta.system_source:
        return None
    path = workspace.book_dir(meta.system_source) / "templates" / "character.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return None


def build_system(meta: CampaignMeta, workspace: Workspace | None = None) -> list[SystemBlock]:
    parts = [_load_prompt(meta.mode)]
    parts.append("\n# This campaign\n")
    parts.append(f"- Campaign name: {meta.name}")
    if meta.sources:
        listed = ", ".join(f"{s} (system)" if s == meta.system_source else s for s in meta.sources)
        parts.append(
            f"- Sources: {listed} (ingested; search them all with search_rules/read_rules)"
        )
    else:
        parts.append(
            "- Sources: none ingested yet. Run the game with general TTRPG knowledge "
            "and improvise fair rulings."
        )
    house_rules = meta.settings.get("house_rules")
    if house_rules:
        parts.append(f"- House rules: {house_rules}")
    custom_instructions = meta.settings.get("custom_instructions")
    if isinstance(custom_instructions, str) and custom_instructions.strip():
        parts.append(
            "\n# Custom instructions from the player\n"
            "These set the tone, personality, and style for how you run the game "
            "(e.g. forgiving vs. punishing, sandbox vs. guided). Honor them "
            "throughout, except where they would conflict with the hard honesty "
            "and state rules above, which always win:\n"
            f"{custom_instructions.strip()}"
        )
    verbosity = meta.settings.get("verbosity")
    if isinstance(verbosity, str):
        instruction = _VERBOSITY_INSTRUCTIONS.get(verbosity)
        if instruction:
            parts.append(instruction)
    parts.append(_SETTINGS_DRIFT_NOTE)
    if meta.mode == "gm" and meta.tts_enabled:
        cast_accent = meta.settings.get("narration_accent")
        accent_text = (
            f" The default accent for character voices is {cast_accent}; set a speaker's "
            "accent explicitly when their character would sound different."
            if cast_accent
            else ""
        )
        parts.append(
            "- Narration audio: enabled. The final visible GM response is the narration "
            "script, read aloud by text-to-speech, so write it to be spoken, not skimmed. "
            "Markdown and emoji are stripped before narration (not voiced), so use plain, "
            "flowing prose and complete sentences. Don't rely on headers, lists, tables, or "
            "emoji to carry meaning. Spell things out as a narrator would say them: write "
            '"roll a d20" or "twelve plus five", not "1d20+5", and expand abbreviations. The '
            "whole response is voiced in the Narrator voice by default, so do not stage plain "
            "descriptive narration; it is already spoken. Use stage_dialogue only to give a "
            "non-narrator speaker (an NPC or creature) their own voice for a quoted line that "
            "appears verbatim in the final response; when a new recurring speaker first "
            "appears, set their gender, age, and accent so the cast voice matches, plus "
            'voice_hint for finer direction. Reserve speaker "Narrator" for the rare '
            "narration line needing a different voice. Do not stage hidden or throwaway prose. "
            "Use play_dialogue only for a line that should be spoken immediately and need not "
            "appear in the visible response (e.g. an off-screen voice)."
            f"{accent_text}"
        )
    elif meta.mode == "assistant" and meta.tts_enabled:
        cast_accent = meta.settings.get("narration_accent")
        accent_text = f" Prefer voices with a {cast_accent} accent." if cast_accent else ""
        parts.append(
            "- Output narration: disabled in assistant mode. Do not narrate ordinary "
            "assistant replies. Voice commands: enabled. Use play_dialogue only when the "
            "GM asks you to play or speak a specific line aloud. That line is spoken "
            "immediately as audio and does not need to appear in your final visible "
            "response. Keep your normal assistant answer visible and concise."
            f"{accent_text}"
        )
    else:
        parts.append("- Narration audio: disabled. Do not attempt to queue spoken lines.")
    if meta.sound_effects_enabled:
        if meta.mode == "gm":
            parts.append(
                "- Sound effects: enabled. Use sparingly for concrete audio beats that "
                "enhance the scene (doors, impacts, spells, weather, monster calls). "
                "Prefer stage_sound_effect to sync the effect to a beat in the final "
                "visible narration, so the final response visibly describes the same "
                "in-world beat. Use play_sound_effect for a one-off sound that should "
                "fire immediately, independent of the narration text."
            )
        else:
            parts.append(
                "- Sound effects: enabled. Use play_sound_effect sparingly for concrete "
                "audio beats when the GM asks (doors, impacts, spells, weather, monster "
                "calls). It plays immediately in the background."
            )
    else:
        parts.append("- Sound effects: disabled. Do not attempt to create sound effects.")
    if meta.music_enabled:
        music_auto = bool(meta.settings.get("music_auto", True))
        if meta.mode == "gm" and music_auto:
            parts.append(
                "- Background music: enabled (auto). Proactively keep a looping music bed "
                "that matches the scene with play_music: set one when play begins, and "
                "change it when the location or mood shifts meaningfully (entering or ending "
                "combat, a tavern, a tense reveal). Reassess after update_scene, "
                "start_encounter, and any update_encounter that ends a fight, but do not "
                "churn tracks every turn. Generation takes a minute and runs in the "
                "background, so cue music a beat early when you can. Use stop_music when "
                "silence serves the scene. The campaign context shows what is currently "
                "playing; consult it "
                "before changing anything and answer music questions from it. Manage music "
                "silently: never narrate or comment on music decisions (no 'the music fits "
                "the mood', no announcing a change) unless the player asks out of character."
            )
        elif meta.mode == "gm":
            parts.append(
                "- Background music: enabled (manual). Use play_music and stop_music "
                "only when the player asks for music out of character. "
                "Do not start or change music on your own. The campaign context shows "
                "what is currently playing."
            )
        else:
            parts.append(
                "- Background music: enabled. Use play_music and stop_music "
                "when the GM asks (e.g. 'play some tavern music'). Generation runs in "
                "the background. The campaign context shows what is currently playing."
            )
    else:
        parts.append("- Background music: disabled. Do not attempt to play music.")
    if meta.images_enabled:
        images_auto = bool(meta.settings.get("images_auto", True))
        if meta.mode == "gm" and images_auto:
            parts.append(
                "- Images: enabled (auto). Show the player a fresh illustration with "
                "generate_image every time the scene changes: whenever you call update_scene "
                "for a newly entered location, generate an image of that location in the same "
                "turn. Also show one for an important NPC on first appearance, a notable item "
                "or creature, or a dramatic reveal. The image opens on the player's screen "
                "automatically. Write a vivid, concrete description (appearance, mood, "
                "lighting, style). For a recurring character, item, or place, call find_images "
                "first and pass the prior image through reference_images so the look stays "
                "consistent; use show_image to redisplay an earlier image rather than "
                "regenerating it. Generation runs in the background, so do not wait for it. "
                "Don't show several images for the same unchanged scene, but a scene change "
                "always warrants a new image."
            )
        elif meta.mode == "gm":
            parts.append(
                "- Images: enabled (manual). Use generate_image and show_image only when the "
                "player asks to see something; do not show images on your own. find_images "
                "lists earlier images and reference_images keeps a subject consistent."
            )
        else:
            parts.append(
                "- Images: enabled. Use generate_image and show_image when the GM asks to see "
                "something. find_images lists earlier images; reference_images keeps a subject "
                "visually consistent. Generation runs in the background."
            )
    else:
        parts.append("- Images: disabled. Do not attempt to generate or show images.")

    template = load_character_template(meta, workspace)
    if template is not None:
        advancement_note = (
            " When a character levels up or advances, or when a player asks to start "
            "above 1st level, follow the advancement_guide the same way: work its steps "
            "in order and apply the results with update_sheet/modify_resource (raise "
            "fields.level, add hit points, record new abilities). Higher-level creation "
            "is the creation_guide followed by the advancement_guide repeated up to the "
            "target level."
            if template.get("advancement_guide")
            else ""
        )
        parts.append(
            "\n# Character template\n"
            "This is also the shape of a sheet you get back from get_sheet. "
            "When creating PC sheets, follow this template (paths/resources for "
            "create_sheet) and its creation guide. Work through every step of the "
            "guide in order before calling create_sheet; don't skip ahead to "
            "generating numbers. Record the FINAL values, with every adjustment the "
            "guide calls for already applied (modifiers, origin/background/heritage "
            "bonuses, derived stats); never store a raw die roll as a finished value, "
            "and assign generated values where the guide directs rather than in the "
            "order they happened to be rolled."
            + advancement_note
            + "\n```json\n"
            + json.dumps(template, indent=1)
            + "\n```"
        )
    # one block, cache breakpoint here: stable per campaign
    return [SystemBlock(text="\n".join(parts), cache=True)]


def _context_value(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_CONTEXT_HEAD_MARKER = "[CAMPAIGN CONTEXT - assembled by the harness, not written by the player]"
_CONTEXT_FOOT_MARKER = (
    "[CURRENT STATE - assembled by the harness; the live situation as of the player's "
    "message that follows. This is where the story stands right now.]"
)


def build_context_head(
    meta: CampaignMeta,
    *,
    canon_open: str | None = None,
    summary_md: str | None = None,
    modules: str | None = None,
) -> str:
    """The stable, long-term context, placed BEFORE the replayed history: premise,
    the module arc, open canon, and the story so far. These are reference and
    narrative-historical, not present-moment state, so they read as context for the
    history that follows and stay byte-stable between compactions. Always non-empty
    (carries the marker)."""
    parts = [_CONTEXT_HEAD_MARKER]
    if meta.premise:
        parts.append(f"## Premise\n{meta.premise}")
    if modules:
        parts.append(
            "## Campaign arc & adventure modules - THE CANONICAL SOURCE for this campaign\n"
            f"{modules}\n"
            "Run the module marked NOW PLAYING; follow the Module content rules in your "
            "instructions (look it up, never substitute your own version, complete_module to "
            "advance)."
        )
    if canon_open:
        parts.append(
            "## Canon (open threads, setups, and standing facts)\n"
            "Keep faith with these and pay them off through play. Entries marked "
            "GM-only are secret: act on them, but never state them outright.\n" + canon_open
        )
    if summary_md:
        parts.append(f"## The story so far\n{summary_md}")
    return "\n\n".join(parts)


def build_context_foot(
    meta: CampaignMeta,
    *,
    scene: dict | None = None,
    location_prep: str | None = None,
    roster: str | None = None,
    encounter: str | None = None,
    clocks: str | None = None,
    npcs: str | None = None,
    npc_recall: str | None = None,
    music: str | None = None,
    settings: str | None = None,
    extra: str | None = None,
) -> str:
    """The point-in-time context, placed AFTER the replayed history and immediately
    before the player's live message: scene, prepped location, scene notes, party,
    NPCs, encounter, clocks, music, settings. This is the world as it stands when the
    player speaks, so it sits next to their message rather than at the top, where it
    would read as stale against the history that climbs toward it. Returns "" when
    there is no live state yet (a brand-new campaign)."""
    parts: list[str] = []
    if scene:
        # npcs_present renders as briefs in its own section below; extra_paths feeds the
        # prepped-location text; prep_notes and hidden_notes each get their own section.
        # None belong inline here.
        skip = {"npcs_present", "extra_paths", "prep_notes", "hidden_notes"}
        bits = [f"{k}: {_context_value(v)}" for k, v in scene.items() if v and k not in skip]
        if bits:
            parts.append(
                "## Current scene\n"
                "The live scene state, and the source of truth for it. Make it match where "
                "the party will be once this turn's response lands, looking both back and "
                "forward: (1) if it is ALREADY out of date from earlier play, fix it; and "
                "(2) if your own narration this turn WILL move things on, call update_scene in "
                "the SAME turn so it reflects the end state, do not narrate the change now and "
                "fix the scene next turn (that leaves it a turn stale, with the Prepped "
                "location below loaded for the wrong room). It does not update itself, and "
                "narrating a change does not update it. It is (or becomes) out of date whenever "
                "the party changes location, doubles BACK to a place they already visited, "
                "time passes, an NPC joins the scene or leaves it (add or remove their id in "
                "npcs_present), a visible exit or unresolved option resolves or a new one "
                "appears, or a scene flag changes. Backtracking is the easy one to miss: "
                "returning to an earlier location still needs update_scene to point back to "
                "it.\n" + "\n".join(bits)
            )
    if location_prep:
        parts.append(
            "## Prepped location (canonical module text for where the party is now)\n"
            "Keep player-facing and GM-only text strictly apart: only read-aloud/boxed text "
            "may be quoted verbatim; everything else (keyed descriptions, placements, hidden "
            "doors, mechanics, twists) is GM-only; reveal it only through play. Already "
            "loaded: narrate from it directly without re-searching; use "
            "search_campaign/read_campaign only for other locations or detail beyond this.\n"
            + location_prep
        )
    if scene and scene.get("prep_notes"):
        parts.append(
            "## Scene notes (your working notes for this location)\n"
            "Notes you recorded for the current scene: reconstructed tables, stat blocks whose "
            "cross-references didn't resolve, details the module spreads across sections. GM-only "
            "working memory; they clear when the party moves on.\n" + scene["prep_notes"]
        )
    if scene and scene.get("hidden_notes"):
        parts.append(
            "## Scene secrets (GM-only, reveal through play)\n"
            "Secrets you set for this location that the players do not know yet: a hidden door or "
            "trap, an ambush, concealed treasure, an NPC's hidden agenda in this scene. Act on "
            "them and pay them off through play, but never state them outright or show them to "
            "the table. They clear when the party moves on; a secret that outlives this location "
            "belongs in hidden canon instead.\n" + scene["hidden_notes"]
        )
    if roster:
        parts.append(
            "## Party\n"
            "The live character-sheet state, and the source of truth for it. When play "
            "changes anything shown here - an item gained, dropped, consumed, or changed "
            "state (worn gear stowed, a vial emptied), HP, a resource, a condition - record "
            "it with the matching tool in the same turn so this stays in sync with the "
            "fiction. Narrating the change does not update the sheet.\n"
            "This is an at-a-glance summary, not the whole sheet: a character's skills, "
            "characteristics, backstory, and gear and weapon stats (damage, range, ammo, "
            "properties) live on the full sheet, not here. The sheet is the source of truth "
            "for that character's own capabilities. For a question about a specific "
            "character's gear, weapon, skill, or derived numbers, call get_sheet with their "
            "id and answer from it before the rulebook or your own general knowledge, since a "
            "character's recorded values can differ from the generic rules. Never assume a "
            "detail is absent just because it is not in this summary.\n" + roster
        )
    if npcs:
        parts.append(
            "## NPCs on stage\nVoice these consistently from their goal, bond, and attitude; "
            "play out secrets through the scene, never stating them outright. When one of them "
            "leaves the scene (walks out, is killed, splits off from the party), remove their "
            "id from npcs_present with update_scene that turn, so they stop riding here as "
            "present; narrating their exit does not remove them.\n" + npcs
        )
    if npc_recall:
        parts.append(
            "## Possible NPCs to stage (named in the scene or your last narration, "
            "have sheets, not staged)\n"
            "Likely matches by name, for you to confirm; you know from the conversation who "
            "is actually here. They are in your live context now, but once this scene scrolls "
            "out of memory their sheet (voice, motives, secrets) is gone unless staged. So for "
            "each one who is genuinely present, add their id to npcs_present in your next "
            "update_scene call (no search needed); skip any only mentioned, or a wrong match "
            "(a shared surname, a common name).\n" + npc_recall
        )
    if encounter:
        parts.append(
            "## Active encounter\n"
            "The live combat tracker, and the source of truth for the fight. Keep it in sync "
            "with the action: advance the turn, set initiative, add reinforcements, mark "
            "combatants defeated, or end the fight with update_encounter, and apply damage or "
            "healing with modify_resource on the combatant's sheet. Narrating a hit, a death, "
            "or a new turn does not update the tracker.\n" + encounter
        )
    if clocks:
        parts.append(
            "## Clocks (pressing threats & countdowns)\nLet them bite (see The living world): "
            "advance the relevant clock as time passes or pressure builds; never reveal hidden "
            "clocks.\n" + clocks
        )
    if music:
        parts.append(f"## Background music\n{music}")
    if settings:
        parts.append(
            "## Session settings (out of character)\n"
            "How this campaign is currently configured. Use it only to answer the "
            "player's out-of-character questions about the setup; never narrate it.\n" + settings
        )
    if extra:
        parts.append(extra)
    if not parts:
        return ""
    return "\n\n".join([_CONTEXT_FOOT_MARKER, *parts])


def build_context_block(
    meta: CampaignMeta,
    *,
    canon_open: str | None = None,
    summary_md: str | None = None,
    modules: str | None = None,
    **foot: object,
) -> str:
    """Back-compat combined block (head then foot) for callers that don't place the
    two halves around the history (tests, single-block use). ``build_messages`` uses
    the head and foot builders directly so it can position them around the tail."""
    head = build_context_head(meta, canon_open=canon_open, summary_md=summary_md, modules=modules)
    foot_text = build_context_foot(meta, **foot)  # type: ignore[arg-type]
    if head == _CONTEXT_HEAD_MARKER and not foot_text:
        return f"{_CONTEXT_HEAD_MARKER}\n\n(new campaign, nothing has happened yet)"
    return "\n\n".join(b for b in (head, foot_text) if b)
