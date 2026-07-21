"""Engine tests against FakeProvider: event sequences AND resulting log state."""

import asyncio

import pytest

from openadventure.providers.base import (
    ProviderError,
    PTextDelta,
    PThinking,
    PToolUse,
    PToolUseStart,
    PTurnDone,
    Usage,
)
from tests.conftest import collect


def text_turn(*deltas, stop="end_turn", usage=None):
    return [
        *[PTextDelta(text=d) for d in deltas],
        PTurnDone(stop_reason=stop, usage=usage or Usage(input_tokens=100, output_tokens=20)),
    ]


def types(events):
    return [e.type for e in events]


async def test_text_only_turn(make_session):
    session = make_session(script=[text_turn("Welcome ", "to the dungeon.")])
    events = await collect(session.handle_input("hello"))

    assert types(events) == [
        "turn_started",
        "assistant_text_delta",
        "assistant_text_delta",
        "turn_completed",
    ]
    assert events[-1].usage.input_tokens == 100

    log_types = [e.type for e in session.log.read_all()]
    assert log_types == ["session_start", "user_message", "gm_message"]
    dm = session.log.read_all()[-1]
    assert dm.data["text"] == "Welcome to the dungeon."


async def test_sudo_wraps_user_message_as_directive(make_session):
    session = make_session(script=[text_turn("As you wish.")])
    await collect(session.handle_input("the bandit secretly defects", steer=True))

    user = next(e for e in session.log.read_all() if e.type == "user_message")
    assert user.data["sudo"] is True
    assert "/sudo DIRECTIVE" in user.data["text"]
    assert "the bandit secretly defects" in user.data["text"]


async def test_normal_input_is_not_a_directive(make_session):
    session = make_session(script=[text_turn("Ok.")])
    await collect(session.handle_input("I look around"))

    user = next(e for e in session.log.read_all() if e.type == "user_message")
    assert "sudo" not in user.data
    assert user.data["text"] == "I look around"


async def test_ephemeral_turn_runs_but_logs_nothing(make_session):
    session = make_session(script=[text_turn("You have 1,200 XP.")])
    events = await collect(session.handle_input("by the way, how much XP?", ephemeral=True))

    # The turn still runs and the reply still streams to the player...
    assert types(events) == [
        "turn_started",
        "assistant_text_delta",
        "turn_completed",
    ]
    # ...but neither the question nor the answer is written to the campaign log.
    log_types = [e.type for e in session.log.read_all()]
    assert log_types == ["session_start"]
    assert session.has_prior_play is False


async def test_ephemeral_question_reaches_the_provider(make_session):
    session = make_session(script=[text_turn("answer")])
    await collect(session.handle_input("secret aside", ephemeral=True))

    # The unlogged message is fed into the turn's conversation by hand.
    last_messages = session.provider.calls[-1].messages
    rendered = " ".join(
        block.text
        for message in last_messages
        for block in message.content
        if getattr(block, "type", None) == "text"
    )
    assert "secret aside" in rendered


async def test_ephemeral_sudo_steers_without_logging(make_session):
    session = make_session(script=[text_turn("Done, quietly.")])
    await collect(session.handle_input("the vault is already open", steer=True, ephemeral=True))

    # Off the record: nothing about the directive or the reply hits the log...
    log_types = [e.type for e in session.log.read_all()]
    assert log_types == ["session_start"]

    # ...but the GM still received it as an authoritative /sudo directive.
    last_messages = session.provider.calls[-1].messages
    rendered = " ".join(
        block.text
        for message in last_messages
        for block in message.content
        if getattr(block, "type", None) == "text"
    )
    assert "/sudo DIRECTIVE" in rendered
    assert "the vault is already open" in rendered


async def test_read_only_aside_offers_only_read_only_tools(make_session):
    # a /btw aside (ephemeral + read_only) is handed the read-only lookups and
    # nothing that mutates state, while a normal turn still gets the full toolset
    session = make_session(script=[text_turn("No clocks ticking."), text_turn("Blade out.")])
    await collect(session.handle_input("any clocks running?", ephemeral=True, read_only=True))
    aside_tools = {t.name for t in session.provider.calls[-1].tools}
    assert {"list_clocks", "search_canon", "get_sheet"} <= aside_tools
    assert not aside_tools & {"update_scene", "roll_dice", "create_clock", "note_canon"}

    await collect(session.handle_input("I draw my sword"))
    full_tools = {t.name for t in session.provider.calls[-1].tools}
    assert {"roll_dice", "update_scene"} <= full_tools


async def test_read_only_aside_lookup_leaves_no_trace(make_session):
    # a /btw aside may call a read-only tool, but it logs nothing, not even a
    # tool_call entry, and the result still feeds back to the model
    script = [
        [
            PToolUseStart(id="t1", name="list_clocks"),
            PToolUse(id="t1", name="list_clocks", input={}),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("No clocks are ticking."),
    ]
    session = make_session(script=script)
    await collect(session.handle_input("btw any clocks?", ephemeral=True, read_only=True))

    assert [e.type for e in session.log.read_all()] == ["session_start"]  # nothing logged
    fed_back = session.provider.calls[1].messages[-1]
    assert fed_back.content[0].type == "tool_result"
    assert fed_back.content[0].tool_use_id == "t1"


async def test_quiet_sudo_is_ephemeral_but_still_mutates(make_session):
    # /sudo --quiet is ephemeral (off the record) but NOT read-only: it keeps the
    # full toolset and is meant to change the world. The mutation runs and is
    # recorded, but the directive and reply stay out of the story log.
    script = [
        [
            PToolUseStart(id="s1", name="update_scene"),
            PToolUse(id="s1", name="update_scene", input={"location": "the open vault"}),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("Done, quietly."),
    ]
    session = make_session(script=script)
    events = await collect(
        session.handle_input("the vault is already open", steer=True, ephemeral=True)
    )

    # the mutating tool was offered and ran (not blocked)...
    offered = {t.name for t in session.provider.calls[0].tools}
    assert "update_scene" in offered
    assert next(e for e in events if e.type == "tool_finished").ok
    # ...and the scene state actually changed on disk
    from openadventure.store import snapshots

    assert snapshots.load_json(session.campaign.scene_path)["location"] == "the open vault"
    # the conversation is off the record (no user/dm message), but the mechanical
    # effect IS recorded so the log stays consistent with the changed state
    log_types = [e.type for e in session.log.read_all()]
    assert "user_message" not in log_types and "gm_message" not in log_types
    assert "state_change" in log_types and "tool_call" in log_types


def test_read_only_dispatch_blocks_mutating_tools(workspace, campaign):
    import random

    from openadventure.engine.tools import build_registry
    from openadventure.engine.tools.registry import ToolContext

    registry = build_registry(workspace, campaign, campaign.load_meta())
    ctx = ToolContext(
        workspace=workspace,
        campaign=campaign,
        meta=campaign.load_meta(),
        log=campaign.open_log(),
        rng=random.Random(7),
        read_only=True,
    )
    # a mutating tool is refused before it can run...
    blocked = registry.dispatch(ctx, "update_scene", {"location": "nowhere"})
    assert not blocked.ok and "read-only" in blocked.content
    # ...a read-only lookup goes through
    assert registry.dispatch(ctx, "list_clocks", {}).ok
    # read_only_defs is a strict subset that excludes every mutator
    read_only = {d.name for d in registry.read_only_defs()}
    assert {"list_clocks", "search_canon", "get_sheet", "list_sheets"} <= read_only
    assert not read_only & {"update_scene", "roll_dice", "create_clock", "modify_inventory"}


async def test_roll_then_narrate_turn(make_session):
    script = [
        [
            PTextDelta(text="Let me get the encounter set up. "),
            PToolUseStart(id="tc1", name="roll_dice"),
            PToolUse(
                id="tc1", name="roll_dice", input={"expression": "1d20+2", "reason": "perception"}
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage(input_tokens=50, output_tokens=10)),
        ],
        text_turn("You spot a tripwire."),
    ]
    session = make_session(script=script)
    events = await collect(session.handle_input("I look around"))

    assert types(events) == [
        "turn_started",
        "tool_started",
        "roll_result",
        "tool_finished",
        "assistant_text_delta",
        "turn_completed",
    ]
    roll = next(e for e in events if e.type == "roll_result")
    assert roll.expression == "1d20+2"
    assert roll.reason == "perception"
    finished = next(e for e in events if e.type == "tool_finished")
    assert finished.ok

    completed = events[-1]
    assert completed.tool_rounds == 1
    assert completed.usage.input_tokens == 150  # both rounds summed

    log_types = [e.type for e in session.log.read_all()]
    assert log_types == [
        "session_start",
        "user_message",
        "roll",
        "tool_call",
        "gm_message",
    ]
    dm = session.log.read_all()[-1]
    assert dm.data["text"] == "You spot a tripwire."
    visible_text = "".join(e.text for e in events if e.type == "assistant_text_delta")
    assert "Let me get the encounter set up" not in visible_text

    # second provider call got the tool result fed back
    provider = session.provider
    second_call = provider.calls[1]
    last = second_call.messages[-1]
    assert last.role == "user"
    assert last.content[0].type == "tool_result"
    assert last.content[0].tool_use_id == "tc1"
    assert not last.content[0].is_error
    assistant_content = second_call.messages[-2].content
    assert [block.type for block in assistant_content] == ["tool_use"]


async def test_batched_tool_round_dispatches_all_in_order(make_session):
    # Several tool calls in one assistant turn resolve as a single round: both
    # results feed back, matched to their ids in call order, and tool_rounds == 1.
    script = [
        [
            PToolUseStart(id="tc1", name="roll_dice"),
            PToolUse(
                id="tc1", name="roll_dice", input={"expression": "1d20+1", "reason": "init A"}
            ),
            PToolUseStart(id="tc2", name="roll_dice"),
            PToolUse(
                id="tc2", name="roll_dice", input={"expression": "1d20+2", "reason": "init B"}
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("Initiative is set."),
    ]
    session = make_session(script=script)
    events = await collect(session.handle_input("roll initiative for both"))

    finished = [e for e in events if e.type == "tool_finished"]
    assert len(finished) == 2
    result_msg = session.provider.calls[1].messages[-1]
    ids = [b.tool_use_id for b in result_msg.content if b.type == "tool_result"]
    assert ids == ["tc1", "tc2"]  # fed back in call order, one round
    assert events[-1].tool_rounds == 1


async def test_thinking_blocks_are_threaded_back_on_tool_rounds(make_session):
    # With thinking on, the API requires the prior assistant turn's thinking
    # block back when tool results follow; the loop must preserve it (leading
    # the turn) instead of dropping it.
    script = [
        [
            PThinking(
                thinking="The party is searching; a perception check fits.", signature="sig1"
            ),
            PToolUseStart(id="tc1", name="roll_dice"),
            PToolUse(id="tc1", name="roll_dice", input={"expression": "1d20+2"}),
            PTurnDone(stop_reason="tool_use", usage=Usage(input_tokens=50, output_tokens=10)),
        ],
        text_turn("You spot a tripwire."),
    ]
    session = make_session(script=script)
    await collect(session.handle_input("I look around"))

    assistant_content = session.provider.calls[1].messages[-2].content
    assert [block.type for block in assistant_content] == ["thinking", "tool_use"]
    thinking = assistant_content[0]
    assert thinking.thinking == "The party is searching; a perception check fits."
    assert thinking.signature == "sig1"


async def test_debug_chatter_surfaces_suppressed_tool_round_text(make_session):
    script = [
        [
            PTextDelta(text="Let me get the encounter set up. "),
            PToolUseStart(id="tc1", name="roll_dice"),
            PToolUse(id="tc1", name="roll_dice", input={"expression": "1d20+2"}),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("Steel flashes in the dark."),
    ]
    session = make_session(script=script)
    events = await collect(session.handle_input("start the fight", debug=True))

    assert types(events) == [
        "turn_started",
        "tool_started",
        "debug_chatter",
        "roll_result",
        "tool_finished",
        "assistant_text_delta",
        "turn_completed",
    ]
    chatter = next(e for e in events if e.type == "debug_chatter")
    assert chatter.text == "Let me get the encounter set up."
    assert chatter.reason == "suppressed GM-mode pre-tool chatter"

    visible_text = "".join(e.text for e in events if e.type == "assistant_text_delta")
    assert visible_text == "Steel flashes in the dark."
    assert session.log.read_all()[-1].data["text"] == "Steel flashes in the dark."


async def test_suppressed_chatter_threads_back_as_user_notes_within_turn(make_session):
    # GM-mode pre-tool chatter is hidden from the player, but the model's own
    # planning is carried forward within the turn so it keeps its intent across a
    # long multi-tool sequence. It rides the *user* message (so it never pollutes
    # the narration register) and trails the tool_result blocks (so it never
    # precedes them). It is never logged: forgotten once the turn ends.
    script = [
        [
            PTextDelta(text="Roll stats, then pick a fitting class. "),
            PToolUseStart(id="tc1", name="roll_dice"),
            PToolUse(id="tc1", name="roll_dice", input={"expression": "4d6"}),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("Your scores are set."),
    ]
    session = make_session(script=script)
    await collect(session.handle_input("make me a character"))

    result_msg = session.provider.calls[1].messages[-1]
    block_types = [b.type for b in result_msg.content]
    # tool_result first, working-notes text after (never before the tool result).
    assert block_types == ["tool_result", "text"]
    note = result_msg.content[-1]
    assert (
        note.text
        == "[your working notes before these tool calls: Roll stats, then pick a fitting class.]"
    )

    # The chatter never reaches the player and never reaches the durable log.
    assert session.log.read_all()[-1].data["text"] == "Your scores are set."
    full_log = " ".join(str(e.data) for e in session.log.read_all())
    assert "Roll stats" not in full_log
    assert "working notes" not in full_log


async def test_private_roll_redacts_frontend_but_not_model_result(make_session):
    script = [
        [
            PToolUseStart(id="tc1", name="roll_dice"),
            PToolUse(
                id="tc1",
                name="roll_dice",
                input={
                    "expression": "1d20+2",
                    "reason": "hidden door check",
                    "private": True,
                },
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("The hallway remains quiet."),
    ]
    session = make_session(script=script)
    events = await collect(session.handle_input("I wait"))

    roll = next(e for e in events if e.type == "roll_result")
    assert roll.private
    assert roll.total == 0
    assert roll.detail == ""
    assert roll.reason is None

    finished = next(e for e in events if e.type == "tool_finished")
    assert finished.private
    assert finished.args_summary == "private=true"
    assert finished.result_summary == "secret roll"
    assert finished.args == {}
    assert finished.result == "secret roll"
    assert "hidden door" not in finished.model_dump_json()

    result_block = session.provider.calls[1].messages[-1].content[0]
    assert result_block.type == "tool_result"
    assert "=" in result_block.content
    assert not result_block.is_error


async def test_hidden_canon_redacts_frontend_payload(make_session):
    secret = "The secret stairs to the dungeon only appear after midnight"
    script = [
        [
            PToolUseStart(id="tc1", name="note_canon"),
            PToolUse(
                id="tc1",
                name="note_canon",
                input={"category": "world", "text": secret, "visibility": "hidden"},
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("You hear stone settle somewhere nearby."),
    ]
    session = make_session(script=script)
    events = await collect(session.handle_input("I listen"))

    changed = next(e for e in events if e.type == "state_changed")
    assert changed.private
    assert changed.summary == "canon (GM-only)"

    finished = next(e for e in events if e.type == "tool_finished")
    assert finished.private
    assert finished.args_summary == "visibility='hidden'"
    assert finished.result_summary == "canon (GM-only)"
    assert finished.args == {}
    assert finished.result == "canon (GM-only)"
    assert secret not in changed.model_dump_json()
    assert secret not in finished.model_dump_json()

    # the secret lives in canon (GM-only), never in the frontend payload
    from openadventure.store import canon

    assert any(
        e.text == secret and e.visibility == "hidden" for e in canon.load(session.campaign).entries
    )


async def test_tool_error_recovery(make_session):
    script = [
        [
            PToolUseStart(id="tc1", name="roll_dice"),
            PToolUse(id="tc1", name="roll_dice", input={"expression": "not-dice"}),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("Let me try that differently."),
    ]
    session = make_session(script=script)
    events = await collect(session.handle_input("roll something"))

    finished = next(e for e in events if e.type == "tool_finished")
    assert not finished.ok
    result_block = session.provider.calls[1].messages[-1].content[0]
    assert result_block.is_error
    assert "Error" in result_block.content
    assert events[-1].type == "turn_completed"


async def test_unknown_tool_is_survivable(make_session):
    script = [
        [
            PToolUse(id="tc1", name="cast_fireball", input={}),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("Apologies, no such power."),
    ]
    session = make_session(script=script)
    events = await collect(session.handle_input("do magic"))
    finished = next(e for e in events if e.type == "tool_finished")
    assert not finished.ok
    assert events[-1].type == "turn_completed"


async def test_max_tool_rounds_cap(make_session):
    from openadventure.engine.agent import MAX_TOOL_ROUNDS, WRAP_UP_NUDGE

    tool_round = [
        PToolUse(id="t", name="roll_dice", input={"expression": "1d6"}),
        PTurnDone(stop_reason="tool_use", usage=Usage()),
    ]
    script = [list(tool_round) for _ in range(MAX_TOOL_ROUNDS)] + [text_turn("Done at last.")]
    session = make_session(script=script)
    events = await collect(session.handle_input("go wild"))

    assert events[-1].type == "turn_completed"
    assert events[-1].tool_rounds == MAX_TOOL_ROUNDS
    # the nudge was injected into the final tool-result message
    last_call = session.provider.calls[-1]
    nudge_blocks = [
        b for b in last_call.messages[-1].content if b.type == "text" and WRAP_UP_NUDGE in b.text
    ]
    assert nudge_blocks


async def test_provider_error_surfaces_as_engine_error(make_session):
    provider_error = ProviderError("API error 500: boom")
    session = make_session(script=[])
    session.provider.error = provider_error
    events = await collect(session.handle_input("hello"))
    assert types(events) == ["turn_started", "engine_error"]
    assert "boom" in events[-1].message
    assert not events[-1].suggest_model
    assert [e.type for e in session.log.read_all()] == [
        "session_start",
        "user_message",
        "engine_error",
    ]


async def test_provider_error_suggest_model_propagates(make_session):
    session = make_session(script=[])
    session.provider.error = ProviderError("API error 429: slow down", suggest_model=True)
    events = await collect(session.handle_input("hello"))
    assert types(events) == ["turn_started", "engine_error"]
    assert events[-1].suggest_model
    assert events[-1].suggest_retry


async def test_no_provider_yields_error(make_session):
    session = make_session(provider=None)
    events = await collect(session.handle_input("hello"))
    assert types(events) == ["engine_error"]
    assert not events[0].recoverable


async def test_cancellation_logs_turn_aborted(make_session):
    class HangingProvider:
        calls = []

        async def stream_turn(self, **kwargs):
            yield PTextDelta(text="thinking…")
            await asyncio.sleep(60)

    session = make_session(provider=HangingProvider())

    async def consume():
        async for _ in session.handle_input("hello"):
            pass

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.log.read_all()[-1].type == "turn_aborted"


async def test_context_block_and_history_flow_to_provider(make_session):
    session = make_session(script=[text_turn("Hi."), text_turn("Again.")])
    await collect(session.handle_input("first message"))
    await collect(session.handle_input("second message"))

    second_call = session.provider.calls[1]
    first_message = second_call.messages[0]
    assert first_message.role == "user"
    assert "CAMPAIGN CONTEXT" in first_message.content[0].text
    assert "a one-room dungeon" in first_message.content[0].text  # premise

    flat = "\n".join(
        block.text
        for message in second_call.messages
        for block in message.content
        if block.type == "text"
    )
    assert "first message" in flat
    assert "Hi." in flat
    assert "second message" in flat
    # system prompt separate from messages, cache-marked
    assert second_call.system[0].cache
    assert "Game Master" in second_call.system[0].text


async def test_usage_accrual_and_report(make_session):
    session = make_session(
        script=[
            text_turn("one", usage=Usage(input_tokens=1000, output_tokens=200)),
            text_turn("two", usage=Usage(input_tokens=2000, output_tokens=300)),
        ]
    )
    await collect(session.handle_input("a"))
    await collect(session.handle_input("b"))

    report = session.usage_report()
    assert report["totals"]["input_tokens"] == 3000
    assert report["totals"]["output_tokens"] == 500
    assert report["session"]["input_tokens"] == 3000
    # default model is gpt-5.6-terra: 3000/1M*2.5 + 500/1M*15 = 0.0075 + 0.0075
    assert report["cost_usd"] == pytest.approx(0.015, abs=1e-6)
    assert "gpt-5.6-terra" in report["by_model"]
    # one session, one model -> the session cost matches the campaign cost
    assert report["session_cost_usd"] == pytest.approx(0.015, abs=1e-6)


def test_assistant_mode_system_prompt(config, workspace):
    campaign = workspace.create_campaign("Helper Game", mode="assistant")
    from openadventure.engine.session import GameSession

    session = GameSession(config, workspace, campaign, None, session_seed=1)
    system = session.build_system()[0].text
    assert "co-GM" in system
    assert "user you are talking to IS the Game Master" in system
    assert "Check roll math first" in system
    assert "Perception" in system

    session.set_mode("gm")
    system = session.build_system()[0].text
    assert "You are the Game Master" in system
    assert "Build the whole roll before rolling" in system
    assert "1d20+5" in system
    assert "Let the engine decide success" in system
    assert "success_when" in system
    assert "do not canonically know" in system
    assert "rules question" in system
    assert "No process narration" in system
    assert "only when the player asks out of character" in system
    assert "Do not silently move through or past" in system
    assert "obvious_exits" in system
    assert campaign.load_meta().mode == "gm"

    with pytest.raises(ValueError):
        session.set_mode("spectator")


def _create_dooley_npc(workspace, campaign):
    from openadventure.engine.tools import build_registry
    from tests.test_sheet_tools import make_ctx

    registry = build_registry(workspace, campaign, campaign.load_meta())
    registry.dispatch(
        make_ctx(workspace, campaign),
        "create_sheet",
        {"kind": "npc", "name": "Mr Dooley", "fields": {"attitude": "friendly"}},
    )


def _harness_hints(session) -> list[str]:
    return [b.text for b in session.provider.calls[1].messages[-1].content if b.type == "text"]


async def test_scene_drift_nudge_when_npc_recalled_but_not_staged(
    make_session, workspace, campaign
):
    # The Dooley regression: the GM recalls an NPC (search_sheets) and narrates him
    # on stage, but never stages him or moves the scene. The mid-round nudge should
    # fire before the final narration, naming his sheet id.
    _create_dooley_npc(workspace, campaign)
    script = [
        [
            PToolUseStart(id="s1", name="search_sheets"),
            PToolUse(id="s1", name="search_sheets", input={"query": "Dooley vendor"}),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("Dooley sets up his stand and greets you."),
    ]
    session = make_session(script=script)
    await collect(session.handle_input("we go outside and wait for dooley"))

    hints = _harness_hints(session)
    assert any("scene check" in t and "mr-dooley" in t for t in hints)


async def test_scene_drift_nudge_suppressed_when_scene_updated(make_session, workspace, campaign):
    # When the same round also calls update_scene, the GM is already handling the
    # scene, so the nudge stays silent.
    _create_dooley_npc(workspace, campaign)
    script = [
        [
            PToolUse(id="s1", name="search_sheets", input={"query": "Dooley"}),
            PToolUse(
                id="s2",
                name="update_scene",
                input={"location": "French Hill street", "npcs_present": ["mr-dooley"]},
            ),
            PTurnDone(stop_reason="tool_use", usage=Usage()),
        ],
        text_turn("Dooley greets you."),
    ]
    session = make_session(script=script)
    await collect(session.handle_input("we go wait for dooley"))

    assert not any("scene check" in t for t in _harness_hints(session))
