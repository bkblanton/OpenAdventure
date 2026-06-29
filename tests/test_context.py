"""History rendering for the table agent: assistant *text* stays pure narration,
and every tool call the GM makes renders as a typed tool_use/tool_result block.

Two invariants. (1) No assistant-role *text* block may contain tool/roll/bracket
syntax; that prose pattern is what an older build taught the model to imitate.
(2) The GM's actions appear as its own tool calls (retrieval, mutation, rolls,
media), so the transcript never shows state, rolls, or media happening on their
own. The one ambient exception is a *player's* roll, which stays a user note.
"""

from __future__ import annotations

from openadventure.engine.context import render_history, uncompacted_span_tokens
from openadventure.providers.base import TextBlock, ToolResultBlock, ToolUseBlock
from openadventure.store.eventlog import LogEntry


def _entries(rows) -> list[LogEntry]:
    return [LogEntry(seq=i + 1, ts="t", type=t, data=d) for i, (t, d) in enumerate(rows)]


def _turn_with_search() -> list[LogEntry]:
    """One turn: player input, two corpus retrievals carrying stored content, a
    player's own roll, then narration."""
    return _entries(
        [
            ("user_message", {"text": "I search the desk."}),
            (
                "tool_call",
                {
                    "name": "search_campaign",
                    "args": {"query": "desk"},
                    "result_summary": "results found",
                    "ok": True,
                    "content": "SEARCH: the study desk has a hidden drawer.",
                },
            ),
            (
                "tool_call",
                {
                    "name": "read_campaign",
                    "args": {"section_path": "the-haunting/room-1.md"},
                    "result_summary": "read the-haunting/room-1.md",
                    "ok": True,
                    "content": "ROOM 1: a dusty study; a locked desk against the wall.",
                },
            ),
            (
                "roll",
                {
                    "expression": "1d20",
                    "total": 17,
                    "reason": "player physical die",
                    "by": "player",
                },
            ),
            ("gm_message", {"text": "You find a brass key taped under the drawer."}),
        ]
    )


def _text(message) -> str:
    return "".join(b.text for b in message.content if isinstance(b, TextBlock))


def _tool_uses(messages) -> list[ToolUseBlock]:
    return [b for m in messages for b in m.content if isinstance(b, ToolUseBlock)]


def _tool_results(messages) -> list[ToolResultBlock]:
    return [b for m in messages for b in m.content if isinstance(b, ToolResultBlock)]


def _stream(messages):
    """Linearize every block across messages in order, tagged by kind, so a test can
    assert relative ordering of tool_use / tool_result / text."""
    out = []
    for m in messages:
        for b in m.content:
            if isinstance(b, ToolUseBlock):
                out.append(("use", b.name))
            elif isinstance(b, ToolResultBlock):
                out.append(("result", b.content))
            else:
                out.append(("text", b.text))
    return out


def test_assistant_text_is_pure_narration():
    """The load-bearing invariant: no assistant-role *text* block carries tool /
    roll / bracket syntax. (tool_use blocks are typed, not text, so they're fine.)"""
    messages, _ = render_history(_turn_with_search(), tail_budget=10_000)
    for message in (m for m in messages if m.role == "assistant"):
        body = _text(message)
        if not body:
            continue  # a tool_use-only assistant message has no text, which is correct
        assert body == "You find a brass key taped under the drawer."
        for needle in ("[", "search_campaign", "read_campaign", "rolled", "engine note"):
            assert needle not in body, f"assistant narration leaked {needle!r}: {body!r}"


def test_corpus_retrievals_replay_as_structured_blocks():
    """search_campaign / read_campaign come back as real tool_use blocks paired with
    tool_result blocks carrying the stored content."""
    messages, _ = render_history(_turn_with_search(), tail_budget=10_000)
    uses = _tool_uses(messages)
    results = _tool_results(messages)
    assert [u.name for u in uses] == ["search_campaign", "read_campaign"]
    assert any("hidden drawer" in r.content for r in results)
    assert any("dusty study" in r.content for r in results)


def test_read_only_lookups_replay_as_structured_blocks():
    """Read-only lookups (get_sheet, search_canon) replay too: they carry detail the
    always-fresh blocks drop (full sheet, a recalled hidden canon entry)."""
    entries = _entries(
        [
            ("user_message", {"text": "what's in my pack, and rumors about the house?"}),
            (
                "tool_call",
                {
                    "name": "get_sheet",
                    "args": {"id": "pc-1"},
                    "ok": True,
                    "content": "Booker: .38 revolver (2d6, 6 rounds), lockpicks, Library Use 60%.",
                },
            ),
            (
                "tool_call",
                {
                    "name": "search_canon",
                    "args": {"query": "corbitt"},
                    "ok": True,
                    "content": "[corbitt-curse] (hidden) Walter Corbitt rises if disturbed.",
                },
            ),
            ("gm_message", {"text": "Your pack holds the revolver and picks."}),
        ]
    )
    messages, _ = render_history(entries, tail_budget=10_000)
    assert [u.name for u in _tool_uses(messages)] == ["get_sheet", "search_canon"]
    results = "\n".join(r.content for r in _tool_results(messages))
    assert ".38 revolver" in results and "Walter Corbitt" in results


def test_mutations_render_as_tool_blocks_not_ambient_notes():
    """A state mutation renders as the GM's own tool_use/tool_result (so the model
    sees itself *doing* the change), from the call's short result summary. The
    state_change log entry is no longer rendered as an ambient engine note."""
    entries = _entries(
        [
            ("user_message", {"text": "we go upstairs"}),
            (
                "tool_call",
                {
                    "name": "update_scene",
                    "args": {"location": "upper floor"},
                    "result_summary": "scene: upper floor",
                    "ok": True,
                },  # no stored content: replays from result_summary
            ),
            ("state_change", {"kind": "scene", "summary": "scene: upper floor"}),
            ("gm_message", {"text": "The stairs creak underfoot."}),
        ]
    )
    messages, _ = render_history(entries, tail_budget=10_000)
    uses = _tool_uses(messages)
    assert [u.name for u in uses] == ["update_scene"]
    assert uses[0].input == {"location": "upper floor"}
    assert any("scene: upper floor" in r.content for r in _tool_results(messages))
    assert "[engine note · scene: upper floor]" not in "\n".join(_text(m) for m in messages)


def test_gm_roll_renders_as_its_tool_block_not_a_note():
    """A GM roll renders as its roll_dice tool block (carrying the verdict), not as a
    'die roll' engine note. The separate roll log entry is not also rendered."""
    entries = _entries(
        [
            ("user_message", {"text": "Pattis looks under the bed"}),
            ("roll", {"expression": "1d100", "total": 88, "outcome": "failure", "by": "gm"}),
            (
                "tool_call",
                {
                    "name": "roll_dice",
                    "args": {"expression": "1d100", "target": 55, "reason": "Spot Hidden"},
                    "result_summary": "1d100 -> 88 (failure)",
                    "ok": True,
                },
            ),
            ("gm_message", {"text": "The angle is wrong; she sees nothing."}),
        ]
    )
    messages, _ = render_history(entries, tail_budget=10_000)
    assert [u.name for u in _tool_uses(messages)] == ["roll_dice"]
    assert any("88 (failure)" in r.content for r in _tool_results(messages))
    assert "die roll" not in "\n".join(_text(m) for m in messages)


def test_media_calls_render_as_tool_blocks():
    """Media is the GM's tool call too: play_music / generate_image render as blocks,
    and the out-of-band media log entries are not separately rendered."""
    entries = _entries(
        [
            ("user_message", {"text": "I open the door."}),
            (
                "tool_call",
                {
                    "name": "play_music",
                    "args": {"prompt": "low droning horror"},
                    "result_summary": "music: low droning horror",
                    "ok": True,
                },
            ),
            ("media", {"kind": "music", "prompt": "low droning horror"}),
            (
                "tool_call",
                {
                    "name": "generate_image",
                    "args": {"subject": "The Corbitt House"},
                    "result_summary": "image: The Corbitt House",
                    "ok": True,
                },
            ),
            ("media", {"kind": "image", "subject": "The Corbitt House"}),
            ("gm_message", {"text": "The door groans open."}),
        ]
    )
    messages, _ = render_history(entries, tail_budget=10_000)
    assert [u.name for u in _tool_uses(messages)] == ["play_music", "generate_image"]
    # the media byproduct entries are not rendered as engine notes
    user_text = "\n".join(_text(m) for m in messages if m.role == "user")
    assert "engine note" not in user_text
    assert _text(next(m for m in messages if m.role == "assistant" and _text(m))) == (
        "The door groans open."
    )


def test_player_roll_stays_a_user_note():
    """A player's own roll has no tool call, so it stays a user-side note (like
    player input); a GM roll in the log does not render as a note."""
    messages, _ = render_history(_turn_with_search(), tail_budget=10_000)
    user_blob = "\n".join(_text(m) for m in messages if m.role == "user")
    assert "[engine note · player rolled 1d20 → 17 (player physical die)]" in user_blob
    assert "die roll" not in user_blob


def test_tool_use_and_result_blocks_are_paired():
    """Every tool_use has a matching tool_result, and each tool_use assistant message
    is immediately followed by a user message covering its ids (API validity)."""
    messages, _ = render_history(_turn_with_search(), tail_budget=10_000)
    use_ids = {u.id for u in _tool_uses(messages)}
    result_ids = {r.tool_use_id for r in _tool_results(messages)}
    assert use_ids == result_ids and use_ids
    for i, message in enumerate(messages):
        ids = [b.id for b in message.content if isinstance(b, ToolUseBlock)]
        if not ids:
            continue
        assert message.role == "assistant"
        nxt = messages[i + 1]
        assert nxt.role == "user"
        answered = {b.tool_use_id for b in nxt.content if isinstance(b, ToolResultBlock)}
        assert set(ids) <= answered


def test_tool_round_groups_retrievals_and_roll_then_narrates():
    """Retrievals and the GM roll in one round group into one tool_use message; the
    GM roll's log entry (logged before its tool_call) does not split the round or
    leave a stray note. Order: tool calls, then narration."""
    entries = _entries(
        [
            ("user_message", {"text": "I force the lock and search."}),
            ("roll", {"expression": "1d100", "total": 30, "outcome": "success", "by": "gm"}),
            (
                "tool_call",
                {
                    "name": "read_campaign",
                    "args": {"section_path": "h/lock.md"},
                    "ok": True,
                    "content": "The lock is rusted shut.",
                },
            ),
            (
                "tool_call",
                {
                    "name": "roll_dice",
                    "args": {"expression": "1d100"},
                    "result_summary": "1d100 -> 30 (success)",
                    "ok": True,
                },
            ),
            ("gm_message", {"text": "The lock gives with a snap."}),
        ]
    )
    stream = _stream(render_history(entries, tail_budget=10_000)[0])
    assert [k for k, _ in stream] == ["text", "use", "use", "result", "result", "text"]
    assert stream[1][1] == "read_campaign" and stream[2][1] == "roll_dice"
    assert stream[-1][1] == "The lock gives with a snap."


def test_long_mutation_args_are_capped():
    """A verbose mutation (a long update_scene description) clips its string args in
    the replayed tool_use block so it doesn't bloat the tail."""
    entries = _entries(
        [
            (
                "tool_call",
                {
                    "name": "update_scene",
                    "args": {"location": "hall", "description": "x" * 1000},
                    "result_summary": "scene set",
                    "ok": True,
                },
            ),
            ("gm_message", {"text": "ok"}),
        ]
    )
    messages, _ = render_history(entries, tail_budget=10_000)
    desc = _tool_uses(messages)[0].input["description"]
    assert len(desc) < 1000 and desc.endswith("…")


def test_failed_or_content_less_retrievals_do_not_replay():
    """A failed call, and a (pre-migration) corpus call with no stored content, both
    render nothing rather than an empty/invalid tool block."""
    entries = _entries(
        [
            ("user_message", {"text": "look around"}),
            (
                "tool_call",
                {"name": "search_campaign", "args": {"query": "x"}, "ok": False, "content": "err"},
            ),
            ("tool_call", {"name": "read_campaign", "result_summary": "read x", "ok": True}),
            ("gm_message", {"text": "Nothing stands out."}),
        ]
    )
    messages, _ = render_history(entries, tail_budget=10_000)
    assert not _tool_uses(messages)
    assert not _tool_results(messages)


def test_history_alternates_roles():
    """The rendered tail alternates user/assistant: player input, the tool round
    (assistant tool_use, user tool_result + the player-roll note), then the reply."""
    messages, _ = render_history(_turn_with_search(), tail_budget=10_000)
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]


def test_tool_round_is_atomic_under_budget():
    """A tiny budget drops the whole tool round rather than half of it, and still
    keeps the newest unit (the narration)."""
    messages, _used = render_history(_turn_with_search(), tail_budget=1)
    assert len(_tool_uses(messages)) == len(_tool_results(messages))
    assert messages[-1].role == "assistant"
    assert "brass key" in _text(messages[-1])


def test_uncompacted_span_counts_replayed_overlay():
    """The compaction trigger sees the replayed overlay: stored content adds to the
    span, so a search-heavy stretch compacts sooner."""
    entries = _turn_with_search()
    full = uncompacted_span_tokens(entries, after_seq=0)
    stripped = [
        LogEntry(
            seq=e.seq,
            ts=e.ts,
            type=e.type,
            data={k: v for k, v in e.data.items() if k != "content"},
        )
        for e in entries
    ]
    assert full > uncompacted_span_tokens(stripped, after_seq=0)


def test_content_less_corpus_calls_contribute_no_tokens():
    """A pre-migration corpus call with no content renders nothing and counts for
    nothing, matching the tail."""
    entries = _entries(
        [
            ("user_message", {"text": "hi"}),
            ("tool_call", {"name": "search_campaign", "args": {"query": "x"}, "ok": True}),
            ("gm_message", {"text": "ok"}),
        ]
    )
    with_calls = uncompacted_span_tokens(entries, after_seq=0)
    without = uncompacted_span_tokens([e for e in entries if e.type != "tool_call"], after_seq=0)
    assert with_calls == without
