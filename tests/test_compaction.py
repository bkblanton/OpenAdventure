"""Compaction triggers, canon chronicler, recap rolling, resume, breakpoints."""

import pytest

from openadventure.engine.compaction import (
    active_encounter_start_seq,
    select_span,
    should_compact,
)
from openadventure.providers.base import (
    Message,
    PThinkingDelta,
    PToolUse,
    PTurnDone,
    TextBlock,
    Usage,
)
from openadventure.providers.fake import FakeProvider
from openadventure.store import canon, snapshots
from openadventure.store.eventlog import LogEntry
from tests.conftest import collect
from tests.test_agent_loop import text_turn


def fill_log(session, turns: int, words_per_turn: int = 120) -> None:
    blob = "lorem ipsum dolor " * (words_per_turn // 3)
    for i in range(turns):
        session.log.append("user_message", {"text": f"turn {i}: {blob}"})
        session.log.append("gm_message", {"text": f"reply {i}: {blob}"})


def canon_turn(summary: str, ops: list[dict] | None = None):
    """A scripted chronicler turn: one record_canon tool call carrying the summary
    and canon ops, then a tool_use stop."""
    return [
        PToolUse(id="c1", name="record_canon", input={"ops": ops or [], "summary": summary}),
        PTurnDone(stop_reason="tool_use", usage=Usage(input_tokens=100, output_tokens=20)),
    ]


def test_should_compact_depends_on_budget(make_session):
    session = make_session(script=[])
    fill_log(session, turns=40)  # ~100k chars ≈ 25k tokens of history
    session.settings = session.settings.model_copy(update={"context_budget": 20_000})
    assert should_compact(session)
    # a 1M-class budget swallows the same log without compacting
    session.settings = session.settings.model_copy(update={"context_budget": 800_000})
    assert not should_compact(session)


def test_select_span_ends_on_gm_message():
    entries = []
    seq = 0
    for i in range(10):
        seq += 1
        entries.append(LogEntry(seq=seq, ts="t", type="user_message", data={"text": f"u{i}"}))
        seq += 1
        entries.append(LogEntry(seq=seq, ts="t", type="gm_message", data={"text": f"d{i}"}))
    cut = select_span(entries, 0)
    assert cut is not None
    cut_entry = next(e for e in entries if e.seq == cut)
    assert cut_entry.type == "gm_message"
    # respects already-compacted prefix
    assert select_span(entries, cut) > cut
    # too little material -> no compaction
    assert select_span(entries[:2], 0) is None


def _enc(seq: int, summary: str) -> LogEntry:
    return LogEntry(
        seq=seq, ts="t", type="state_change", data={"kind": "encounter", "summary": summary}
    )


def test_active_encounter_start_seq():
    entries = [
        LogEntry(seq=1, ts="t", type="user_message", data={"text": "u"}),
        _enc(2, "encounter started: Goblins"),
        LogEntry(seq=3, ts="t", type="gm_message", data={"text": "d"}),
    ]
    # open fight -> its start seq
    assert active_encounter_start_seq(entries) == 2
    # ended fight -> nothing protected
    entries.append(_enc(4, "round 2, Orc's turn; encounter ended: Goblins"))
    assert active_encounter_start_seq(entries) is None
    # a fresh fight after the ended one -> the new start
    entries.append(_enc(5, "encounter started: Orcs"))
    assert active_encounter_start_seq(entries) == 5
    # no encounters at all
    assert active_encounter_start_seq(entries[:1]) is None


def test_select_span_protects_in_progress_combat():
    # 10 turns; a fight opens early enough that the ~60% cut lands inside it
    entries = []
    seq = 0
    enc_start = None
    for i in range(10):
        seq += 1
        entries.append(LogEntry(seq=seq, ts="t", type="user_message", data={"text": f"u{i}"}))
        if i == 3:
            seq += 1
            entries.append(_enc(seq, "encounter started: Goblins"))
            enc_start = seq
        seq += 1
        entries.append(LogEntry(seq=seq, ts="t", type="gm_message", data={"text": f"d{i}"}))

    # without protection the natural cut lands inside the fight
    unguarded = select_span(entries, 0)
    assert unguarded is not None and unguarded >= enc_start

    # with protection the cut is pulled back before the fight began
    guarded = select_span(entries, 0, protect_after_seq=enc_start)
    assert guarded is not None and guarded < enc_start

    # a fight that opened before any compactable boundary -> skip entirely
    assert select_span(entries, 0, protect_after_seq=1) is None


async def test_compaction_pass_rolls_recap_and_canon(make_session):
    session = make_session(script=[])
    fill_log(session, turns=30)
    session.settings = session.settings.model_copy(update={"context_budget": 20_000})
    session.provider = FakeProvider(
        script=[
            canon_turn(
                "The heroes met in a tavern and slew the goblin chief.",
                ops=[
                    {"op": "add", "id": "chief", "category": "threads", "text": "avenge the chief"}
                ],
            )
        ]
    )

    events = await collect(session.compact_now())
    assert [e.type for e in events] == ["compaction_started", "compaction_finished"]

    summary = snapshots.load_json(session.campaign.summary_path)
    assert "goblin chief" in summary["summary_md"]
    assert summary["through_seq"] > 0
    assert session.log.read_all()[-1].type == "compaction"

    # canon got the op, and its through_seq matches the recap's
    c = canon.load(session.campaign)
    assert c.find("chief").category == "threads"
    assert c.through_seq == summary["through_seq"]

    # the chronicler got the transcript, the right system prompt, and the tool
    call = session.provider.calls[0]
    assert "chronicler" in call.system[0].text
    assert "turn 0" in call.messages[0].content[0].text
    assert any(t.name == "record_canon" for t in call.tools)

    # next prompt: recap present, compacted turns excluded
    messages, _ = session.build_messages()
    context_text = messages[0].content[0].text
    assert "goblin chief" in context_text
    flat = "\n".join(b.text for m in messages for b in m.content if b.type == "text")
    assert "turn 0:" not in flat  # compacted away

    # second pass folds the old recap into the new one
    fill_log(session, turns=20)
    session.provider = FakeProvider(script=[canon_turn("Updated recap.")])
    await collect(session.compact_now())
    second_call = session.provider.calls[0]
    assert "goblin chief" in second_call.messages[0].content[0].text  # old recap fed in


async def test_compact_ticks_progress_as_chronicler_thinks(make_session):
    """A manual /compact emits a compaction_progress heartbeat as the chronicler
    reasons (about one per sentence) so the wait can animate, then finishes. The
    tick carries no reasoning text, so GM-only canon never leaks to the spinner."""
    session = make_session(script=[])
    fill_log(session, turns=30)
    session.settings = session.settings.model_copy(update={"context_budget": 20_000})
    session.provider = FakeProvider(
        script=[
            [
                PThinkingDelta(thinking="The party met in a tavern. "),
                PThinkingDelta(thinking="Now I record the goblin chief thread."),
                PToolUse(id="c1", name="record_canon", input={"ops": [], "summary": "Recap."}),
                PTurnDone(stop_reason="tool_use", usage=Usage(input_tokens=100, output_tokens=20)),
            ]
        ]
    )

    events = await collect(session.compact_now())
    types = [e.type for e in events]
    assert types[0] == "compaction_started"
    assert types[-1] == "compaction_finished"
    progress = [e for e in events if e.type == "compaction_progress"]
    # two sentences of reasoning -> two heartbeat ticks, none carrying text
    assert len(progress) == 2
    assert not any(getattr(e, "text", "") for e in progress)


async def test_canon_survives_across_compactions(make_session):
    """The drift regression: a seed added in the first pass stays verbatim in
    canon across later passes that never touch it."""
    session = make_session(script=[])
    session.settings = session.settings.model_copy(update={"context_budget": 20_000})

    fill_log(session, turns=30)
    session.provider = FakeProvider(
        script=[
            canon_turn(
                "Recap 1.",
                ops=[
                    {
                        "op": "add",
                        "id": "raven",
                        "category": "seeds",
                        "text": "a black raven at every murder scene",
                    }
                ],
            )
        ]
    )
    await collect(session.compact_now())
    assert canon.load(session.campaign).find("raven").text == "a black raven at every murder scene"

    # two more passes whose chronicler never mentions the seed
    for i in range(2):
        fill_log(session, turns=30)
        session.provider = FakeProvider(script=[canon_turn(f"Recap {i + 2}.")])
        await collect(session.compact_now())

    raven = canon.load(session.campaign).find("raven")
    assert raven.text == "a black raven at every murder scene"  # verbatim, no erosion
    assert raven.is_open


async def test_chronicler_without_tool_call_keeps_log(make_session):
    # the model produced prose but never called record_canon: commit nothing
    session = make_session(script=[])
    fill_log(session, turns=30)
    session.settings = session.settings.model_copy(update={"context_budget": 20_000})
    session.provider = FakeProvider(script=[text_turn("I forgot to record canon.")])

    events = await collect(session.compact_now())
    assert [e.type for e in events] == ["compaction_started", "engine_error"]
    assert snapshots.load_json(session.campaign.summary_path) is None


async def test_auto_compaction_after_turn_runs_in_background(make_session):
    session = make_session(
        script=[
            text_turn("A reply."),
            canon_turn("Chronicle of everything so far."),
        ]
    )
    fill_log(session, turns=40)
    session.settings = session.settings.model_copy(update={"context_budget": 20_000})

    events = await collect(session.handle_input("what happened so far?"))
    types = [e.type for e in events]
    # the turn no longer blocks on compaction; it just kicks off the background pass
    assert "turn_completed" in types
    assert "compaction_finished" not in types  # not inline anymore
    assert types[-1] == "background_task_started"

    # the pass runs in the background; its result drains afterward
    await session.background.wait_all()
    drained = [e.type for e in session.background.drain()]
    assert "compaction_finished" in drained
    assert snapshots.load_json(session.campaign.summary_path)["through_seq"] > 0


async def test_background_compaction_drops_live_progress_ticks(make_session):
    """The progress heartbeat only drives the foreground /compact spinner. A
    background pass drains after it finishes, so it must not leak stale
    compaction_progress ticks (they would print a frozen spinner frame next to
    'Story compacted')."""
    session = make_session(script=[])
    fill_log(session, turns=40)
    session.settings = session.settings.model_copy(update={"context_budget": 20_000})
    session.provider = FakeProvider(
        script=[
            [
                PThinkingDelta(thinking="First sentence. "),
                PThinkingDelta(thinking="Second sentence."),
                PToolUse(id="c1", name="record_canon", input={"ops": [], "summary": "Recap."}),
                PTurnDone(stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=5)),
            ]
        ]
    )

    assert session._spawn_compaction() is not None
    await session.background.wait_all()
    drained = [e.type for e in session.background.drain()]
    assert "compaction_finished" in drained
    assert "compaction_progress" not in drained


def test_open_canon_is_injected_into_context(make_session):
    session = make_session(script=[])
    c, _ = canon.apply_ops(
        canon.empty(),
        [
            {
                "op": "add",
                "id": "raven",
                "category": "seeds",
                "text": "a black raven at each scene",
            },
            {
                "op": "add",
                "id": "spy",
                "category": "world",
                "text": "the innkeeper is a spy",
                "visibility": "hidden",
            },
            {"op": "add", "id": "old", "category": "threads", "text": "a closed thread"},
        ],
        at_seq=1,
    )
    c, _ = canon.apply_ops(c, [{"op": "resolve", "id": "old"}], at_seq=2)
    canon.save(session.campaign, c)

    context = session.build_messages()[0][0].content[0].text
    assert "a black raven at each scene" in context and "[raven]" in context
    assert "the innkeeper is a spy" in context  # the GM agent sees GM-only canon
    assert "a closed thread" not in context  # resolved entries are not injected


async def test_compact_now_joins_inflight_background_pass(make_session):
    # single-flight: /compact while a background pass is underway must not launch
    # a duplicate chronicler call (the FakeProvider has only one canon turn).
    session = make_session(script=[])
    fill_log(session, turns=40)
    session.settings = session.settings.model_copy(update={"context_budget": 20_000})
    session.provider = FakeProvider(script=[canon_turn("One pass only.")])

    started = session._spawn_compaction()
    assert started is not None
    assert session._compacting is True
    # /compact now joins rather than starting a second pass (would exhaust script)
    assert await collect(session.compact_now()) == []
    await session.background.wait_all()
    assert snapshots.load_json(session.campaign.summary_path)["summary_md"] == "One pass only."


async def test_close_cancels_inflight_compaction(make_session):
    # close() (on /quit) and undo() both cancel an in-flight chronicler via
    # cancel_kind("compaction"); test the deterministic close() path here.
    session = make_session(script=[])
    fill_log(session, turns=40)
    session.settings = session.settings.model_copy(update={"context_budget": 20_000})
    session.provider = FakeProvider(script=[canon_turn("Should be cancelled.")])

    session._spawn_compaction()
    session.close()  # cancels the in-flight pass before it can commit
    await session.background.wait_all()
    assert snapshots.load_json(session.campaign.summary_path) is None


async def test_compaction_failure_is_survivable(make_session):
    from openadventure.providers.base import ProviderError

    session = make_session(script=[])
    fill_log(session, turns=30)
    session.settings = session.settings.model_copy(update={"context_budget": 20_000})
    session.provider = FakeProvider(script=[])
    session.provider.error = ProviderError("boom")

    events = await collect(session.compact_now())
    assert [e.type for e in events] == ["compaction_started", "engine_error"]
    assert snapshots.load_json(session.campaign.summary_path) is None  # nothing written


async def test_resume_sees_history_and_summary(config, workspace, campaign):
    from openadventure.engine.session import GameSession

    first = GameSession(
        config,
        workspace,
        campaign,
        FakeProvider(script=[text_turn("You enter the crypt.")]),
        session_seed=1,
    )
    await collect(first.handle_input("I open the crypt door"))
    first.close()

    # fresh process: new session over the same campaign
    second = GameSession(
        config,
        workspace,
        campaign,
        FakeProvider(script=[text_turn("It is dark here.")]),
        session_seed=2,
    )
    messages, _ = second.build_messages()
    flat = "\n".join(b.text for m in messages for b in m.content if b.type == "text")
    assert "I open the crypt door" in flat
    assert "You enter the crypt." in flat


def test_anthropic_cache_breakpoints():
    from openadventure.providers.anthropic_provider import _convert_messages, _convert_system
    from openadventure.providers.base import SystemBlock

    system = _convert_system([SystemBlock(text="be a gm", cache=True)])
    assert system[0]["cache_control"] == {"type": "ephemeral"}

    messages = _convert_messages(
        [
            Message(role="user", content=[TextBlock(text="context")]),
            Message(role="assistant", content=[TextBlock(text="reply")]),
            Message(role="user", content=[TextBlock(text="latest input")]),
        ],
        cache_last=True,
    )
    assert messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in messages[0]["content"][0]


@pytest.mark.parametrize("budget,expect", [(20_000, True), (800_000, False)])
def test_trigger_thresholds(make_session, budget, expect):
    session = make_session(script=[])
    fill_log(session, turns=40)
    session.settings = session.settings.model_copy(update={"context_budget": budget})
    assert should_compact(session) is expect
