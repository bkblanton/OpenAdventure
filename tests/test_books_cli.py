"""books CLI: list the store, delete a book (detaching it everywhere), and
rename a book (moving the store dir and repointing every campaign)."""

import argparse

import pytest

import openadventure.cli.term as term
from openadventure.cli.main import _cmd_books
from openadventure.ingest import pipeline
from openadventure.store.workspace import ModuleRef

BOOK = "# Rulebook\n\n## Combat\n\nRoll a d20 to attack.\n"


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    # a fresh, wide, non-tty console so table cells don't wrap under capsys
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(term, "_console", None)


def _ns(workspace, **over):
    base = dict(workspace=str(workspace.root), name=None, delete=False, rename=None, yes=False)
    base.update(over)
    return argparse.Namespace(**base)


def _ingest(workspace, name, tmp_path):
    src = tmp_path / f"{name}.md"
    src.write_text(BOOK, encoding="utf-8")
    pipeline.ingest(src, workspace.book_dir(name))


def _use_as_rules_and_module(campaign):
    meta = campaign.load_meta()
    meta.sources = ["dnd5e"]
    meta.system_source = "dnd5e"
    meta.modules = [ModuleRef(slug="death-house", title="Death House", order=0, status="active")]
    meta.active_module = "death-house"
    campaign.save_meta(meta)


def test_books_global_list_shows_usage(workspace, campaign, tmp_path, capsys):
    _ingest(workspace, "dnd5e", tmp_path)
    _ingest(workspace, "death-house", tmp_path)
    _ingest(workspace, "orphan", tmp_path)  # ingested but unused
    _use_as_rules_and_module(campaign)

    rc = _cmd_books(_ns(workspace))
    out = capsys.readouterr().out
    assert rc == 0
    assert "dnd5e" in out and "death-house" in out and "orphan" in out
    assert "rules/system" in out  # how the campaign uses dnd5e
    assert "module" in out  # how the campaign uses death-house
    assert "unused" in out  # orphan is in the store but no campaign uses it


# --- delete -----------------------------------------------------------------
def test_books_delete_removes_and_detaches_module(workspace, campaign, tmp_path, capsys):
    _ingest(workspace, "dnd5e", tmp_path)
    _ingest(workspace, "death-house", tmp_path)
    _use_as_rules_and_module(campaign)

    rc = _cmd_books(_ns(workspace, delete=True, name="death-house", yes=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Deleted" in out
    assert not workspace.book_dir("death-house").exists()  # gone from the store
    reloaded = campaign.load_meta()
    assert [m.slug for m in reloaded.modules] == []  # detached
    assert reloaded.active_module is None
    assert reloaded.sources == ["dnd5e"]  # the rules source is untouched


def test_books_delete_clears_system_source(workspace, campaign, tmp_path, capsys):
    _ingest(workspace, "dnd5e", tmp_path)
    meta = campaign.load_meta()
    meta.sources = ["dnd5e"]
    meta.system_source = "dnd5e"
    campaign.save_meta(meta)

    rc = _cmd_books(_ns(workspace, delete=True, name="dnd5e", yes=True))
    assert rc == 0
    reloaded = campaign.load_meta()
    assert reloaded.sources == []
    assert reloaded.system_source is None


def test_books_delete_without_a_name_errors(workspace, capsys):
    rc = _cmd_books(_ns(workspace, delete=True))
    assert rc == 1
    assert "Specify a book to delete" in capsys.readouterr().out


def test_books_name_without_an_action_errors(workspace, tmp_path, capsys):
    _ingest(workspace, "dnd5e", tmp_path)
    rc = _cmd_books(_ns(workspace, name="dnd5e"))
    assert rc == 1
    assert "Nothing to do" in capsys.readouterr().out


def test_books_delete_unknown_errors(workspace, capsys):
    rc = _cmd_books(_ns(workspace, delete=True, name="nope", yes=True))
    assert rc == 1
    assert "No ingested book" in capsys.readouterr().out


def test_books_delete_cancelled_keeps_everything(
    workspace, campaign, tmp_path, capsys, monkeypatch
):
    _ingest(workspace, "death-house", tmp_path)
    meta = campaign.load_meta()
    meta.modules = [ModuleRef(slug="death-house", title="Death House", order=0, status="active")]
    meta.active_module = "death-house"
    campaign.save_meta(meta)
    monkeypatch.setattr("builtins.input", lambda _: "n")  # decline the confirmation

    rc = _cmd_books(_ns(workspace, delete=True, name="death-house", yes=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Cancelled" in out
    assert workspace.book_dir("death-house").exists()  # still in the store
    assert [m.slug for m in campaign.load_meta().modules] == ["death-house"]  # still attached


# --- rename -----------------------------------------------------------------
def test_books_rename_moves_store_and_updates_references(workspace, campaign, tmp_path, capsys):
    _ingest(workspace, "dnd5e", tmp_path)
    _ingest(workspace, "death-house", tmp_path)
    _use_as_rules_and_module(campaign)

    rc = _cmd_books(_ns(workspace, name="dnd5e", rename="dnd5e-srd"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Renamed" in out
    assert not workspace.book_dir("dnd5e").exists()
    assert workspace.book_dir("dnd5e-srd").is_dir()
    reloaded = campaign.load_meta()
    assert reloaded.sources == ["dnd5e-srd"]
    assert reloaded.system_source == "dnd5e-srd"

    # a module rename repoints the module ref and the active pointer
    rc = _cmd_books(_ns(workspace, name="death-house", rename="the-manor"))
    assert rc == 0
    reloaded = campaign.load_meta()
    assert [m.slug for m in reloaded.modules] == ["the-manor"]
    assert reloaded.active_module == "the-manor"


def test_books_rename_slugifies_the_new_name(workspace, tmp_path, capsys):
    _ingest(workspace, "dnd5e", tmp_path)
    rc = _cmd_books(_ns(workspace, name="dnd5e", rename="D&D 5e SRD"))
    assert rc == 0
    assert workspace.book_dir("d-d-5e-srd").is_dir()


def test_books_rename_rejects_existing_target(workspace, tmp_path, capsys):
    _ingest(workspace, "dnd5e", tmp_path)
    _ingest(workspace, "pathfinder", tmp_path)
    rc = _cmd_books(_ns(workspace, name="dnd5e", rename="pathfinder"))
    assert rc == 1
    assert "already exists" in capsys.readouterr().out
    assert workspace.book_dir("dnd5e").is_dir()  # untouched


def test_books_rename_unknown_errors(workspace, capsys):
    rc = _cmd_books(_ns(workspace, name="nope", rename="whatever"))
    assert rc == 1
    assert "No ingested book" in capsys.readouterr().out


def test_books_rename_without_a_name_errors(workspace, capsys):
    rc = _cmd_books(_ns(workspace, rename="whatever"))
    assert rc == 1
    assert "Specify the book to rename" in capsys.readouterr().out


def test_books_delete_and_rename_together_errors(workspace, tmp_path, capsys):
    _ingest(workspace, "dnd5e", tmp_path)
    rc = _cmd_books(_ns(workspace, name="dnd5e", delete=True, rename="x"))
    assert rc == 1
    assert "not both" in capsys.readouterr().out


# --- parser -----------------------------------------------------------------
def test_parser_books_accepts_name_and_rename():
    from openadventure.cli.main import build_parser

    args = build_parser().parse_args(["books", "dnd5e", "--rename", "dnd5e-srd"])
    assert args.name == "dnd5e"
    assert args.rename == "dnd5e-srd"
    assert args.delete is False
