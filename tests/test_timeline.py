"""Undo + restart end-to-end through the engine."""

import shutil

import pytest

from openadventure.engine.timeline import TimelineError, restart_campaign, undo_turns
from openadventure.providers.base import PToolUse, PTurnDone, Usage
from openadventure.providers.fake import FakeProvider
from openadventure.store import checkpoints, snapshots
from openadventure.store.sheetstore import SheetStore
from tests.conftest import collect
from tests.test_agent_loop import text_turn
from tests.test_compaction import canon_turn

CREATE_KASIMIR = PToolUse(
    id="c1",
    name="create_sheet",
    input={
        "kind": "pc",
        "name": "Kasimir",
        "fields": {"class": "Fighter", "level": 1},
        "resources": {"hp": {"current": 12, "max": 12}},
    },
)


def tool_turn(*calls: PToolUse):
    return [*calls, PTurnDone(stop_reason="tool_use", usage=Usage())]


def damage_turn(amount: int = -7):
    return tool_turn(
        PToolUse(
            id="d1",
            name="modify_resource",
            input={"sheet_id": "kasimir", "resource": "hp", "delta": amount},
        ),
        PToolUse(id="d2", name="update_scene", input={"location": "cursed crypt"}),
        PToolUse(
            id="d3", name="note_canon", input={"category": "threads", "text": "lift the curse"}
        ),
    )


async def play_two_turns(session):
    """Turn 1 creates Kasimir; turn 2 damages him and moves the scene."""
    await collect(session.handle_input("make my character"))
    await collect(session.handle_input("I touch the cursed altar"))


def two_turn_script():
    return [
        tool_turn(CREATE_KASIMIR),
        text_turn("Welcome, Kasimir!"),
        damage_turn(),
        text_turn("The altar bites back. Ouch."),
    ]


def flat_text(session) -> str:
    messages, _ = session.build_messages()
    return "\n".join(b.text for m in messages for b in m.content if b.type == "text")


async def test_undo_one_turn_reverts_everything(make_session, campaign):
    session = make_session(script=two_turn_script())
    await play_two_turns(session)

    store = SheetStore(campaign)
    assert store.load("kasimir").resources["hp"].current == 5
    assert snapshots.load_json(campaign.scene_path)["location"] == "cursed crypt"

    report = undo_turns(campaign, session.log, 1)
    assert report.turns_undone == 1
    assert report.undone_texts == ["I touch the cursed altar"]

    # state reverted to before turn 2
    assert store.load("kasimir").resources["hp"].current == 12
    assert snapshots.load_json(campaign.scene_path) is None  # scene was set in turn 2
    assert not (campaign.notes_dir / "quest.jsonl").exists()

    # log truncated + marker + archive
    entries = session.log.read_all()
    assert entries[-1].type == "undo"
    assert report.archive.is_file()
    text = flat_text(session)
    assert "cursed altar" not in text
    assert "Ouch" not in text
    assert "Welcome, Kasimir!" in text  # turn 1 survived

    # play continues cleanly after the undo
    session.provider = FakeProvider(script=[text_turn("A fresh path opens.")])
    events = await collect(session.handle_input("I step back instead"))
    assert events[-1].type == "turn_completed"


async def test_undo_two_turns_in_one_call(make_session, campaign):
    session = make_session(script=two_turn_script())
    await play_two_turns(session)

    report = undo_turns(campaign, session.log, 2)
    assert report.turns_undone == 2
    assert SheetStore(campaign).load("kasimir") is None  # created in turn 1, gone
    assert "make my character" not in flat_text(session)


async def test_undo_clamps_when_checkpoint_pruned(make_session, campaign):
    session = make_session(script=two_turn_script())
    await play_two_turns(session)

    first_user = next(e for e in session.log.read_all() if e.type == "user_message")
    shutil.rmtree(campaign.checkpoints_dir / str(first_user.seq - 1))

    report = undo_turns(campaign, session.log, 2)
    assert report.turns_undone == 1  # clamped to the reachable depth


async def test_undo_errors(make_session, campaign):
    session = make_session(script=[])
    with pytest.raises(TimelineError, match="no turns"):
        undo_turns(campaign, session.log, 1)

    session.provider = FakeProvider(script=[text_turn("Hi.")])
    await collect(session.handle_input("hello"))
    shutil.rmtree(campaign.checkpoints_dir)
    with pytest.raises(TimelineError, match="checkpoint"):
        undo_turns(campaign, session.log, 1)


async def test_undo_across_sessions(config, workspace, campaign):
    from openadventure.engine.session import GameSession

    first = GameSession(
        config, workspace, campaign, FakeProvider(script=two_turn_script()), session_seed=1
    )
    await play_two_turns(first)
    first.close()

    second = GameSession(
        config, workspace, campaign, FakeProvider(script=[]), session_seed=2
    )  # appends its own session_start after the turns
    report = undo_turns(campaign, second.log, 1)
    assert report.turns_undone == 1
    assert SheetStore(campaign).load("kasimir").resources["hp"].current == 12
    # the live log instance is coherent: next append continues after the marker
    entry = second.log.append("note", {"check": True})
    assert entry.seq == second.log.read_all()[-1].seq


async def test_undo_after_compaction_keeps_invariant(make_session, campaign):
    session = make_session(script=two_turn_script())
    await play_two_turns(session)

    # force a compaction, then another turn (its checkpoint includes the summary)
    session.provider = FakeProvider(script=[canon_turn("Chronicle: Kasimir got hurt.")])
    await collect(session.compact_now())
    summary_before = snapshots.load_json(campaign.summary_path)

    session.provider = FakeProvider(script=[text_turn("Onward.")])
    await collect(session.handle_input("we press on"))

    undo_turns(campaign, session.log, 1)
    summary_after = snapshots.load_json(campaign.summary_path)
    assert summary_after == summary_before  # restored from the checkpoint
    assert summary_after["through_seq"] <= session.log.last_seq


async def test_restart_reroll(make_session, campaign, workspace):
    session = make_session(
        script=[
            tool_turn(CREATE_KASIMIR),
            text_turn("Welcome!"),
            tool_turn(
                PToolUse(
                    id="x1",
                    name="modify_resource",
                    input={"sheet_id": "kasimir", "resource": "hp", "delta": -9},
                ),
                PToolUse(
                    id="x2",
                    name="set_conditions",
                    input={"sheet_id": "kasimir", "add": ["poisoned"]},
                ),
                PToolUse(
                    id="x3",
                    name="create_sheet",
                    input={
                        "kind": "monster",
                        "name": "Goblin",
                        "resources": {"hp": {"current": 7, "max": 7}},
                    },
                ),
                PToolUse(
                    id="x4",
                    name="update_sheet",
                    input={
                        "id": "kasimir",
                        "ops": [{"op": "set", "path": "fields.level", "value": 3}],
                    },
                ),
            ),
            text_turn("A rough fight."),
        ]
    )
    await collect(session.handle_input("begin"))
    await collect(session.handle_input("fight the goblin"))

    usage_before = campaign.usage_path.read_bytes()
    docs_file = campaign.docs_dir / "module.txt"
    docs_file.write_text("module data")

    report = restart_campaign(campaign, characters="reroll")
    session.log.refresh()

    store = SheetStore(campaign)
    assert store.load("kasimir") is None  # PCs cleared for a fresh roll
    assert store.load_original("kasimir") is None  # original archived too
    assert store.party() == []
    assert store.load("goblin") is None  # NPCs/monsters wiped
    assert not campaign.log_path.exists() or session.log.read_all() == []
    assert snapshots.load_json(campaign.summary_path) is None
    assert checkpoints.list_seqs(campaign) == []

    # archive holds the old story + the cleared PC; usage + docs untouched
    assert (report.archive_dir / "log.jsonl").is_file()
    assert (report.archive_dir / "characters" / "kasimir.json").is_file()
    assert (report.archive_dir / "originals" / "kasimir.json").is_file()
    assert campaign.usage_path.read_bytes() == usage_before
    assert docs_file.read_text() == "module data"
    assert report.rerolled == ["kasimir"]
    assert report.pcs == []

    # play can resume from scratch
    assert session.log.append("session_start").seq == 1


async def test_restart_original(make_session, campaign):
    session = make_session(
        script=[
            tool_turn(CREATE_KASIMIR),
            text_turn("Welcome!"),
            tool_turn(
                PToolUse(
                    id="l1",
                    name="update_sheet",
                    input={
                        "id": "kasimir",
                        "ops": [
                            {"op": "set", "path": "fields.level", "value": 5},
                            {"op": "append", "path": "fields.inventory", "value": "magic axe"},
                        ],
                    },
                )
            ),
            text_turn("Level up!"),
        ]
    )
    await collect(session.handle_input("begin"))
    await collect(session.handle_input("train hard"))

    store = SheetStore(campaign)
    # a second PC created outside the tools; has no original
    from tests.test_sheets import make_sheet

    store.save(make_sheet(id="legacy-pc", name="Legacy Pc"))

    assert store.load("kasimir").fields["level"] == 5
    report = restart_campaign(campaign, characters="original")

    assert store.load("kasimir").fields["level"] == 1  # back to creation
    assert "inventory" not in store.load("kasimir").fields
    assert report.missing_originals == ["legacy-pc"]
    assert store.load("legacy-pc") is not None  # kept, not deleted


async def test_retry_flow_undoes_then_replays(make_session, campaign):
    session = make_session(
        script=[
            tool_turn(CREATE_KASIMIR),
            text_turn("Welcome!"),
            damage_turn(-7),
            text_turn("Ouch, -7."),
        ]
    )
    await play_two_turns(session)
    store = SheetStore(campaign)
    assert store.load("kasimir").resources["hp"].current == 5

    # the /retry flow: capture text, undo, re-send
    last = next(e for e in reversed(session.log.read_all()) if e.type == "user_message")
    undo_turns(campaign, session.log, 1)
    session.provider = FakeProvider(script=[damage_turn(-3), text_turn("Just a scratch, -3.")])
    await collect(session.handle_input(last.data["text"]))

    assert store.load("kasimir").resources["hp"].current == 9  # only the retry applied
    text = flat_text(session)
    assert text.count("I touch the cursed altar") == 1
    assert "Just a scratch" in text
    assert "Ouch, -7." not in text


# --- the frontend-agnostic GameSession wrappers -----------------------------


async def test_session_undo_delegates(make_session, campaign):
    session = make_session(script=two_turn_script())
    await play_two_turns(session)

    report = session.undo(1)
    assert report.turns_undone == 1
    assert SheetStore(campaign).load("kasimir").resources["hp"].current == 12
    assert session.log.read_all()[-1].type == "undo"


async def test_session_prepare_retry(make_session, campaign):
    session = make_session(script=[])
    assert session.prepare_retry() is None  # nothing played yet

    session.provider = FakeProvider(script=two_turn_script())
    await play_two_turns(session)
    plan = session.prepare_retry()
    assert plan is not None and plan.undone is True
    assert plan.text == "I touch the cursed altar"
    assert SheetStore(campaign).load("kasimir").resources["hp"].current == 12  # turn 2 undone

    # no surviving checkpoint: still hand back the text, flagged not-undone
    shutil.rmtree(campaign.checkpoints_dir)
    fallback = session.prepare_retry()
    assert fallback is not None and fallback.undone is False
    assert fallback.text == "make my character"


async def test_session_restart_resets_live_log(make_session, campaign):
    session = make_session(script=two_turn_script())
    await play_two_turns(session)

    report = session.restart(characters="reroll")
    assert report.rerolled == ["kasimir"]
    # the live log is refreshed and marked, so play resumes coherently
    entries = session.log.read_all()
    assert entries[-1].type == "session_start"
    assert entries[-1].data.get("restarted") is True
    assert session.log.append("note", {"check": True}).seq == session.log.read_all()[-1].seq
