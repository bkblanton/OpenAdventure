"""sources CLI: list and manage a campaign's rules sources (parallels modules)."""

import argparse

import pytest

import openadventure.cli.term as term
from openadventure.cli.main import _cmd_sources
from openadventure.ingest import pipeline

BOOK = "# Rulebook\n\n## Combat\n\nRoll a d20 to attack.\n"


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(term, "_console", None)


def _ns(campaign, **over):
    base = dict(
        workspace=str(campaign.root.parent.parent),
        campaign=campaign.root.name,
        add=None,
        remove=None,
        system=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _ingest(workspace, name, tmp_path, *, book_type=None):
    src = tmp_path / f"{name}.md"
    src.write_text(BOOK, encoding="utf-8")
    pipeline.ingest(src, workspace.book_dir(name), book_type=book_type)


def test_sources_empty_lists_help(workspace, campaign, capsys):
    rc = _cmd_sources(_ns(campaign))
    assert rc == 0
    assert "no rules sources yet" in capsys.readouterr().out


def test_sources_add_attaches_and_sets_system(workspace, campaign, tmp_path, capsys):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    rc = _cmd_sources(_ns(campaign, add="dnd5e"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Rules sources" in out and "system" in out
    reloaded = campaign.load_meta()
    assert reloaded.sources == ["dnd5e"]
    assert reloaded.system_source == "dnd5e"


def test_sources_add_second_keeps_first_as_system(workspace, campaign, tmp_path):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    _ingest(workspace, "monster-manual", tmp_path, book_type="source")
    _cmd_sources(_ns(campaign, add="dnd5e"))
    _cmd_sources(_ns(campaign, add="monster-manual"))
    reloaded = campaign.load_meta()
    assert reloaded.sources == ["dnd5e", "monster-manual"]
    assert reloaded.system_source == "dnd5e"


def test_sources_add_rejects_a_module_book(workspace, campaign, tmp_path, capsys):
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    rc = _cmd_sources(_ns(campaign, add="death-house"))
    assert rc == 1
    assert "rules source" in capsys.readouterr().out
    assert campaign.load_meta().sources == []


def test_sources_add_unknown_errors(workspace, campaign, capsys):
    rc = _cmd_sources(_ns(campaign, add="nope"))
    assert rc == 1
    assert "No ingested book" in capsys.readouterr().out


def test_sources_remove_detaches_and_repoints_system(workspace, campaign, tmp_path):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    _ingest(workspace, "monster-manual", tmp_path, book_type="source")
    _cmd_sources(_ns(campaign, add="dnd5e"))
    _cmd_sources(_ns(campaign, add="monster-manual"))

    _cmd_sources(_ns(campaign, remove="dnd5e"))
    reloaded = campaign.load_meta()
    assert reloaded.sources == ["monster-manual"]
    assert reloaded.system_source == "monster-manual"  # repointed off the removed source


def test_sources_remove_unattached_errors(workspace, campaign, capsys):
    rc = _cmd_sources(_ns(campaign, remove="dnd5e"))
    assert rc == 1
    assert "isn't attached" in capsys.readouterr().out


def test_sources_system_attaches_and_designates(workspace, campaign, tmp_path):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    _ingest(workspace, "monster-manual", tmp_path, book_type="source")
    _cmd_sources(_ns(campaign, add="dnd5e"))

    # --system on a not-yet-attached source attaches it, then designates it
    _cmd_sources(_ns(campaign, system="monster-manual"))
    reloaded = campaign.load_meta()
    assert set(reloaded.sources) == {"dnd5e", "monster-manual"}
    assert reloaded.system_source == "monster-manual"


def test_sources_system_rejects_a_module_book(workspace, campaign, tmp_path, capsys):
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    rc = _cmd_sources(_ns(campaign, system="death-house"))
    assert rc == 1
    assert "rules source" in capsys.readouterr().out
    assert campaign.load_meta().system_source is None


def test_sources_unknown_campaign_errors(workspace, capsys):
    args = argparse.Namespace(
        workspace=str(workspace.root), campaign="nope", add=None, remove=None, system=None
    )
    rc = _cmd_sources(args)
    assert rc == 1
    assert "no campaign named" in capsys.readouterr().out.lower()


def test_sources_drops_a_source_whose_book_is_gone(workspace, campaign, tmp_path):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    _cmd_sources(_ns(campaign, add="dnd5e"))
    # remove the book from the store, then make any change so the prune persists
    import shutil

    shutil.rmtree(workspace.book_dir("dnd5e"))
    _ingest(workspace, "pf", tmp_path, book_type="source")
    _cmd_sources(_ns(campaign, add="pf"))

    reloaded = campaign.load_meta()
    assert reloaded.sources == ["pf"]
    assert reloaded.system_source == "pf"


def test_parser_sources_accepts_campaign_and_flags():
    from openadventure.cli.main import build_parser

    args = build_parser().parse_args(["sources", "my-camp", "--add", "dnd5e"])
    assert args.campaign == "my-camp"
    assert args.add == "dnd5e"
    assert args.func is _cmd_sources
