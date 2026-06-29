"""Log migration: backfilling replayable tool-result content on old logs."""

from __future__ import annotations

from openadventure.engine.context import render_history
from openadventure.ingest import pipeline
from openadventure.providers.base import ToolResultBlock
from openadventure.store.migrations import backfill_tool_content

MODULE_MD = """\
# Death House

## Rose and Thorn

Read-aloud: Two children stand in the street. "There's a monster in our house!"

The children are illusions. A ghoul lurks in the cellar below.
"""


def _campaign_with_module(workspace, tmp_path):
    source = tmp_path / "death-house.md"
    source.write_text(MODULE_MD, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir("death-house"))
    return workspace.create_campaign("Haunting", modules=["death-house"])


def _tool_results(messages):
    return [b for m in messages for b in m.content if isinstance(b, ToolResultBlock)]


def test_backfill_rederives_read_campaign_content(workspace, tmp_path):
    campaign = _campaign_with_module(workspace, tmp_path)
    log = campaign.open_log()
    log.append("user_message", {"text": "I read the room."})
    log.append(
        "tool_call",
        {
            "name": "read_campaign",
            "args": {"section_path": "death-house/rose-and-thorn.md"},
            "result_summary": "read death-house/rose-and-thorn.md",
            "ok": True,
        },  # no content: a pre-replay log
    )
    log.append("gm_message", {"text": "Two children block your path."})

    updated = backfill_tool_content(workspace, campaign)
    assert updated == 1

    entry = next(e for e in campaign.open_log().read_all() if e.type == "tool_call")
    assert "There's a monster in our house" in entry.data["content"]

    # and it now replays as a structured tool_result block
    messages, _ = render_history(campaign.open_log().read_all(), tail_budget=10_000)
    results = _tool_results(messages)
    assert results and "There's a monster in our house" in results[0].content


def test_backfill_is_idempotent(workspace, tmp_path):
    campaign = _campaign_with_module(workspace, tmp_path)
    log = campaign.open_log()
    log.append(
        "tool_call",
        {
            "name": "read_campaign",
            "args": {"section_path": "death-house/rose-and-thorn.md"},
            "ok": True,
        },
    )
    assert backfill_tool_content(workspace, campaign) == 1
    # a second pass finds the content already present and changes nothing
    assert backfill_tool_content(workspace, campaign) == 0


def test_backfill_skips_unresolvable_non_eligible_and_lookups(workspace, tmp_path):
    campaign = _campaign_with_module(workspace, tmp_path)
    log = campaign.open_log()
    log.append(
        "tool_call",
        {"name": "read_campaign", "args": {"section_path": "death-house/no-such.md"}, "ok": True},
    )
    log.append(
        "tool_call",
        {"name": "roll_dice", "args": {"expression": "1d20"}, "ok": True},  # not replay-eligible
    )
    # a lookup IS replayed in live play, but is NOT backfilled: re-running it now
    # would stamp current state onto a historical turn.
    log.append("tool_call", {"name": "get_sheet", "args": {"id": "pc-1"}, "ok": True})
    assert backfill_tool_content(workspace, campaign) == 0
    for entry in campaign.open_log().read_all():
        assert "content" not in entry.data
