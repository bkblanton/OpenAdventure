"""Checkpoint save/restore: round-trip, deletion of post-checkpoint files, pruning."""

import json

from openadventure.store import checkpoints, snapshots
from openadventure.store.sheetstore import SheetStore
from tests.test_sheets import make_sheet


def test_save_restore_roundtrip(campaign):
    store = SheetStore(campaign)
    store.save(make_sheet())
    snapshots.save_json(campaign.scene_path, {"location": "tavern"})
    (campaign.notes_dir / "quest.jsonl").write_text('{"ts": "t", "text": "find the axe"}\n')

    checkpoints.save(campaign, 5)

    # mutate everything
    damaged = make_sheet()
    damaged.resources["hp"].current = 1
    store.save(damaged)
    snapshots.save_json(campaign.scene_path, {"location": "dungeon"})
    with open(campaign.notes_dir / "quest.jsonl", "a", encoding="utf-8") as f:
        f.write('{"ts": "t", "text": "second note"}\n')

    checkpoints.restore(campaign, 5)
    assert store.load("kasimir").resources["hp"].current == 12
    assert snapshots.load_json(campaign.scene_path)["location"] == "tavern"
    notes = (campaign.notes_dir / "quest.jsonl").read_text().strip().splitlines()
    assert len(notes) == 1
    assert json.loads(notes[0])["text"] == "find the axe"


def test_restore_deletes_files_created_after_checkpoint(campaign):
    store = SheetStore(campaign)
    checkpoints.save(campaign, 1)

    # created after the checkpoint: a monster, a summary, a notes category
    store.save(make_sheet(id="goblin", kind="monster", name="Goblin"))
    snapshots.save_json(campaign.summary_path, {"summary_md": "stuff", "through_seq": 9})
    (campaign.notes_dir / "secret.jsonl").write_text('{"ts": "t", "text": "shh"}\n')

    checkpoints.restore(campaign, 1)
    assert store.load("goblin") is None
    assert snapshots.load_json(campaign.summary_path) is None
    assert not (campaign.notes_dir / "secret.jsonl").exists()
    assert campaign.npcs_dir.is_dir()  # dirs recreated empty


def test_list_has_prune_delete_after(campaign):
    for seq in (3, 1, 8, 5):
        checkpoints.save(campaign, seq)
    assert checkpoints.list_seqs(campaign) == [1, 3, 5, 8]
    assert checkpoints.has(campaign, 5)
    assert not checkpoints.has(campaign, 4)

    checkpoints.prune(campaign, keep=2)
    assert checkpoints.list_seqs(campaign) == [5, 8]

    checkpoints.delete_after(campaign, 5)
    assert checkpoints.list_seqs(campaign) == [5]


def test_prune_keeps_default_30(campaign):
    for seq in range(40):
        checkpoints.save(campaign, seq)
    checkpoints.prune(campaign)
    assert checkpoints.list_seqs(campaign) == list(range(10, 40))
