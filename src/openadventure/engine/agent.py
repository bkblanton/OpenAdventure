"""The GM agent turn loop: assemble -> stream -> dispatch tools -> repeat."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from openadventure.engine.context import (
    REPLAY_CONTENT_CHARS,
    REPLAY_TOOLS,
    est_tokens,
    tool_schema_tokens,
)
from openadventure.engine.events import (
    AssistantTextDelta,
    DebugChatter,
    EngineError,
    EngineEvent,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    TurnStarted,
)
from openadventure.engine.tools.registry import summarize_args
from openadventure.providers.base import (
    Message,
    ProviderError,
    PToolUse,
    PTurnDone,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

if TYPE_CHECKING:
    from openadventure.engine.session import GameSession

MAX_TOOL_ROUNDS = 15
# GM-mode pre-tool chatter is hidden from the player to protect immersion (see the
# suppression at suppress_tool_round_text below), but the model's own planning is
# still useful within a long multi-tool turn (character creation, scene changes can
# chain many rounds). We feed it back through the *user* channel, not as assistant
# text, so the narration register the model imitates stays clean. It is never logged,
# so it is forgotten once the turn ends, exactly like thinking.
WORKING_NOTES_TEMPLATE = "[your working notes before these tool calls: {text}]"
WRAP_UP_NUDGE = (
    "[harness: you have hit the tool-call limit for this turn. "
    "Wrap up your narration now using what you already know; "
    "you can use more tools next turn.]"
)
MUSIC_DECISION_TOOLS = {"play_music", "stop_music"}

SUDO_DIRECTIVE_TEMPLATE = (
    "[OUT-OF-CHARACTER /sudo DIRECTIVE from the player, an authoritative, "
    "out-of-character instruction to you, the AI, about how to run things. It is "
    "not in-character speech. Honor it for this turn and going forward, even when "
    "it overrides your own plans, the module text, or what an NPC 'would' do. Do "
    "not quote the directive or break the fourth wall at the table; fold its "
    "effect into the narration. Directive: {text}]"
)


def _tool_finished_event(session, tu, args_summary, outcome) -> ToolFinished:
    hide_private = outcome.private and session.meta.mode == "gm"
    if hide_private:
        public_summary = outcome.public_result_summary
        return ToolFinished(
            call_id=tu.id,
            name=tu.name,
            args_summary=outcome.public_args_summary or "private",
            result_summary=public_summary,
            ok=outcome.ok,
            args={},
            result=public_summary,
            private=True,
        )
    return ToolFinished(
        call_id=tu.id,
        name=tu.name,
        args_summary=args_summary,
        result_summary=outcome.result_summary,
        ok=outcome.ok,
        args=tu.input,
        result=outcome.content,
        private=outcome.private,
    )


def _auto_music_check(
    session: GameSession, tool_uses: list[PToolUse], outcomes: list
) -> str | None:
    if (
        session.meta.mode != "gm"
        or not session.meta.music_enabled
        or "play_music" not in session.tools
        or any(tu.name in MUSIC_DECISION_TOOLS for tu in tool_uses)
    ):
        return None

    reasons: list[str] = []
    for tu, outcome in zip(tool_uses, outcomes, strict=True):
        if not outcome.ok:
            continue
        if tu.name == "update_scene":
            reasons.append("the scene changed")
        elif tu.name == "start_encounter":
            reasons.append("combat started")
        elif tu.name == "update_encounter" and tu.input.get("end"):
            reasons.append("combat ended")

    if not reasons:
        return None

    current = session.music_status_line() or "no music playing"
    reason_text = "; ".join(dict.fromkeys(reasons))
    return (
        f"[harness: auto music check: {reason_text}. Current background music: {current}. "
        "If the current music is absent or no longer fits, call play_music now with a "
        "concise instrumental prompt before final narration. If it still fits, continue "
        "without a music tool.]"
    )


def _scene_drift_check(session: GameSession, tool_uses: list[PToolUse]) -> str | None:
    """A mid-round nudge when the GM recalled an NPC this round but neither staged
    them nor moved the scene. Mirrors _auto_music_check: it appends a [harness] note
    to the tool-result message so the model reconsiders before its final narration,
    while the scene snapshot it would otherwise leave stale is still fixable. The
    trigger is the GM's own tool calls (a get_sheet / search_sheets for an unstaged
    NPC), not text heuristics, and the note is conditional so a pure lore recall can
    be ignored. Suppressed when update_scene fired this round (the GM is handling it)."""
    if any(tu.name == "update_scene" for tu in tool_uses):
        return None
    get_sheet_ids = [
        tu.input["id"]
        for tu in tool_uses
        if tu.name == "get_sheet" and isinstance(tu.input.get("id"), str)
    ]
    queries = " ".join(tu.input.get("query", "") for tu in tool_uses if tu.name == "search_sheets")
    if not get_sheet_ids and not queries.strip():
        return None
    hits = session.npcs_referenced_unstaged(queries, get_sheet_ids)
    if not hits:
        return None
    listed = "; ".join(f"{sheet.name} (id {sheet.id})" for sheet in hits)
    return (
        f"[harness: scene check: you looked up {listed}, not staged in the current scene, "
        "without updating the scene this round. If they are now present, add their id to "
        "npcs_present when you update_scene; if you only recalled them and they are not "
        "actually here, ignore this.]"
    )


async def run_turn(
    session: GameSession,
    user_text: str,
    *,
    debug: bool = False,
    steer: bool = False,
    ephemeral: bool = False,
    read_only: bool = False,
) -> AsyncIterator[EngineEvent]:
    """Run one GM turn over ``user_text``.

    Two independent off-the-record modes layer onto a turn:

    - ``ephemeral`` (both ``/btw`` and ``/sudo --quiet``): the player's message
      and the GM's reply are NOT written to the campaign log; the turn runs and
      the reply is shown, but it leaves no conversational trace. The message is
      fed into this turn's conversation directly rather than read back from the log.
    - ``read_only`` (a ``/btw`` aside only): the turn may use only read-only
      tools, so it can ground its answer in lookups without changing anything. A
      ``/sudo --quiet`` directive is ``ephemeral`` but NOT ``read_only``; it is
      meant to mutate state quietly, so it keeps the full toolset.
    """
    log = session.log
    # A read-only turn (/btw) is restricted to read-only tools (dispatch enforces
    # it via ctx.read_only) and its lookups leave no tool_call trace.
    session.tool_ctx.read_only = read_only
    logged_text = SUDO_DIRECTIVE_TEMPLATE.format(text=user_text) if steer else user_text
    if not ephemeral:
        log.append("user_message", {"text": logged_text, **({"sudo": True} if steer else {})})
    turn_id = f"turn-{log.last_seq}"
    yield TurnStarted(turn_id=turn_id)

    system = session.build_system()
    # A read-only aside sees only the read-only tools; everything else (including a
    # quiet /sudo directive) keeps the full toolset so it can change the world.
    tool_defs = session.tools.read_only_defs() if read_only else session.tools.defs()
    # Pass the already-built system and tool-schema sizes so build_messages sizes the
    # tail from this turn's real non-tail input without rebuilding either.
    system_tokens = est_tokens("\n".join(block.text for block in system))
    tool_tokens = tool_schema_tokens(tool_defs)
    convo, prompt_tokens_est = session.build_messages(
        system_tokens=system_tokens, tool_tokens=tool_tokens
    )
    # The live player message follows the current-state foot block that build_messages
    # ends on. For a logged turn build_messages held it back from the history so it
    # lands here, after the foot; for an ephemeral turn it was never logged. Either
    # way it is appended once, so current-state sits between history and message.
    convo.append(Message(role="user", content=[TextBlock(text=logged_text)]))

    narration_parts: list[str] = []
    total_usage = Usage()
    rounds = 0

    while True:
        text_acc = ""
        text_deltas: list[str] = []
        thinking_deltas: list[str] = []
        thinking: list[ThinkingBlock | RedactedThinkingBlock] = []
        tool_uses: list[PToolUse] = []
        stop: PTurnDone | None = None
        try:
            async for pe in session.provider.stream_turn(
                system=system, messages=convo, tools=tool_defs, settings=session.settings
            ):
                match pe.type:
                    case "text_delta":
                        text_acc += pe.text
                        text_deltas.append(pe.text)
                    case "thinking_delta":
                        # The final PThinking block is preferred below because
                        # it avoids counting streamed summaries twice. Retain
                        # deltas as a fallback for providers that never emit a
                        # completed block with their usage metadata.
                        thinking_deltas.append(pe.thinking)
                    case "thinking":
                        thinking.append(ThinkingBlock(thinking=pe.thinking, signature=pe.signature))
                    case "redacted_thinking":
                        thinking.append(RedactedThinkingBlock(data=pe.data))
                    case "tool_use_start":
                        yield ToolStarted(call_id=pe.id, name=pe.name)
                    case "tool_use":
                        tool_uses.append(pe)
                    case "turn_done":
                        stop = pe
        except ProviderError as exc:
            if not ephemeral:
                log.append("engine_error", {"message": str(exc)})
            yield EngineError(
                message=str(exc),
                recoverable=exc.recoverable,
                suggest_retry=exc.recoverable,
                suggest_model=exc.suggest_model,
            )
            return

        wants_tools = bool(stop is not None and stop.stop_reason == "tool_use" and tool_uses)
        suppress_tool_round_text = wants_tools and session.meta.mode == "gm"
        if suppress_tool_round_text and debug and text_acc.strip():
            yield DebugChatter(text=text_acc.strip(), reason="suppressed GM-mode pre-tool chatter")
        if not suppress_tool_round_text:
            for delta in text_deltas:
                yield AssistantTextDelta(text=delta)
        if text_acc.strip() and not suppress_tool_round_text:
            narration_parts.append(text_acc)
        if stop is not None:
            usage = stop.usage
            if usage.thinking_tokens == 0:
                completed_thinking = "".join(
                    block.thinking for block in thinking if isinstance(block, ThinkingBlock)
                )
                estimated_thinking = est_tokens(completed_thinking or "".join(thinking_deltas))
                if completed_thinking or thinking_deltas:
                    # Reasoning is a subset of total output. When a provider
                    # has no final usage at all, retain the best live estimate
                    # rather than erasing thought work from the usage report.
                    output_tokens = max(usage.output_tokens, estimated_thinking)
                    usage = usage.model_copy(
                        update={
                            "output_tokens": output_tokens,
                            "thinking_tokens": min(estimated_thinking, output_tokens),
                        }
                    )
            total_usage = total_usage.add(usage)
        for background_event in session.background.drain():
            yield background_event

        if not wants_tools or rounds >= MAX_TOOL_ROUNDS:
            break

        # Thinking blocks lead the turn so the API can verify reasoning continuity
        # when tool results follow with thinking enabled (e.g. /thinking on).
        assistant_content: list[
            ThinkingBlock | RedactedThinkingBlock | TextBlock | ToolUseBlock
        ] = [*thinking]
        if text_acc and not suppress_tool_round_text:
            assistant_content.append(TextBlock(text=text_acc))
        assistant_content.extend(
            ToolUseBlock(id=tu.id, name=tu.name, input=tu.input) for tu in tool_uses
        )
        convo.append(Message(role="assistant", content=assistant_content))

        result_blocks: list[ToolResultBlock | TextBlock] = []
        # Dispatch the whole round at once: read-only tools run concurrently off
        # the event loop, the rest inline in order (see dispatch_batch). Logging
        # and frontend events stay in tool-call order below.
        outcomes = await session.tools.dispatch_batch(
            session.tool_ctx, [(tu.name, tu.input) for tu in tool_uses]
        )
        for tu, outcome in zip(tool_uses, outcomes, strict=True):
            args_summary = summarize_args(tu.input)
            # A read-only aside (/btw) leaves no tool_call trace. A quiet /sudo
            # directive still records its tool activity; its mutations are real,
            # and the state_change entries its handlers write must stay consistent
            # with them. Only the conversation is off the record.
            if not read_only:
                entry_data = {
                    "name": tu.name,
                    "args": tu.input,
                    "result_summary": outcome.result_summary,
                    "ok": outcome.ok,
                }
                # Persist the result of a replay-eligible read-only tool (corpus
                # retrieval or a lookup) so it can be replayed as a structured
                # tool_result block in later turns (see context.py). Capped well
                # below what the live turn saw: the replayed copy only has to keep
                # the GM oriented, and it persists across turns.
                if tu.name in REPLAY_TOOLS and outcome.ok and outcome.content:
                    content = outcome.content
                    if len(content) > REPLAY_CONTENT_CHARS:
                        content = (
                            content[:REPLAY_CONTENT_CHARS]
                            + "\n[…truncated; search/read again for the rest]"
                        )
                    entry_data["content"] = content
                log.append("tool_call", entry_data)
            for extra in outcome.events:
                yield extra
            yield _tool_finished_event(session, tu, args_summary, outcome)
            result_blocks.append(
                ToolResultBlock(tool_use_id=tu.id, content=outcome.content, is_error=not outcome.ok)
            )

        rounds += 1
        # Suppressed GM-mode chatter is the model's only planning trace across rounds
        # when thinking is off (the real-time default). Carry it forward hidden, on the
        # user side, so the GM keeps its intent through a long multi-tool turn (e.g.
        # character creation, scene changes) without the player ever seeing it and
        # without it polluting the narration register. Trailing text, like the blocks
        # below, so it never precedes the tool_result blocks.
        if suppress_tool_round_text and text_acc.strip():
            result_blocks.append(
                TextBlock(text=WORKING_NOTES_TEMPLATE.format(text=text_acc.strip()))
            )
        auto_music_check = _auto_music_check(session, tool_uses, outcomes)
        if auto_music_check is not None:
            result_blocks.append(TextBlock(text=auto_music_check))
        scene_drift_check = _scene_drift_check(session, tool_uses)
        if scene_drift_check is not None:
            result_blocks.append(TextBlock(text=scene_drift_check))
        if rounds >= MAX_TOOL_ROUNDS:
            result_blocks.append(TextBlock(text=WRAP_UP_NUDGE))
        convo.append(Message(role="user", content=result_blocks))

    for background_event in session.background.drain():
        yield background_event

    narration = "\n\n".join(narration_parts).strip()
    if narration:
        if not ephemeral:
            log.append("gm_message", {"text": narration})
        voice_cues = list(session.tool_ctx.voice_cues)
        sound_effect_cues = list(session.tool_ctx.sound_effect_cues)
        started = session.queue_narration(
            narration, voice_cues=voice_cues, sound_effect_cues=sound_effect_cues
        )
        session.tool_ctx.voice_cues.clear()
        session.tool_ctx.sound_effect_cues.clear()
        if started is not None:
            yield started
    else:
        session.tool_ctx.voice_cues.clear()
        session.tool_ctx.sound_effect_cues.clear()
    session.accrue_usage(total_usage)
    yield TurnCompleted(
        turn_id=turn_id,
        usage=total_usage,
        tool_rounds=rounds,
        prompt_tokens_est=prompt_tokens_est,
    )
