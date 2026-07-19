"""Workspace: campaign create/list/load, slugs, atomic snapshots."""

import pytest

from openadventure.store import snapshots
from openadventure.store.workspace import Campaign, Workspace, slugify


def test_slugify():
    assert slugify("Death House!") == "death-house"
    assert slugify("  Curse of Strahd  ") == "curse-of-strahd"
    assert slugify("???") == "campaign"


def test_create_and_load_campaign(tmp_path):
    ws = Workspace(tmp_path)
    campaign = ws.create_campaign(
        "Death House",
        mode="assistant",
        sources=["dnd5e"],
        premise="gothic horror",
        settings={"verbosity": "high"},
    )
    meta = campaign.load_meta()
    assert meta.slug == "death-house"
    assert meta.mode == "assistant"
    assert meta.sources == ["dnd5e"]
    assert meta.system_source == "dnd5e"
    assert meta.settings["verbosity"] == "high"
    assert campaign.characters_dir.is_dir()
    assert campaign.notes_dir.is_dir()

    loaded = ws.campaign("death-house").load_meta()
    assert loaded == meta


def test_load_meta_migrates_legacy_ruleset(tmp_path):
    """A campaign.json from before the sources rename (single ``ruleset`` field)
    loads as a one-entry sources list with that source as the system source."""
    ws = Workspace(tmp_path)
    campaign = ws.create_campaign("Old Campaign")
    snapshots.save_json(
        campaign.meta_path,
        {"name": "Old Campaign", "slug": "old-campaign", "mode": "gm", "ruleset": "dnd5e"},
    )
    meta = campaign.load_meta()
    assert meta.sources == ["dnd5e"]
    assert meta.system_source == "dnd5e"


def test_duplicate_campaign_rejected(tmp_path):
    ws = Workspace(tmp_path)
    ws.create_campaign("Foo")
    with pytest.raises(FileExistsError):
        ws.create_campaign("foo")


def test_failed_campaign_initialization_releases_slug(tmp_path, monkeypatch):
    ws = Workspace(tmp_path)

    def fail_save(_campaign, _meta):
        raise OSError("simulated metadata write failure")

    monkeypatch.setattr(Campaign, "save_meta", fail_save)
    with pytest.raises(OSError, match="simulated metadata write failure"):
        ws.create_campaign("Recoverable Name")

    assert not (ws.campaigns_dir / "recoverable-name").exists()


def test_list_campaigns(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.list_campaigns() == []
    ws.create_campaign("Alpha")
    ws.create_campaign("Beta")
    assert [m.slug for m in ws.list_campaigns()] == ["alpha", "beta"]


def test_missing_campaign(tmp_path):
    with pytest.raises(FileNotFoundError):
        Workspace(tmp_path).campaign("nope")


def test_snapshot_roundtrip_and_atomicity(tmp_path):
    path = tmp_path / "scene.json"
    snapshots.save_json(path, {"location": "Barovia", "flags": {"fog": True}})
    assert snapshots.load_json(path)["location"] == "Barovia"
    # overwrite leaves no tmp file behind
    snapshots.save_json(path, {"location": "Death House"})
    assert snapshots.load_json(path)["location"] == "Death House"
    assert not path.with_suffix(".json.tmp").exists()
    assert snapshots.load_json(tmp_path / "missing.json") is None
