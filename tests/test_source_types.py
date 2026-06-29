"""Book typing: a book is ingested as a rules source or an adventure module,
and can only be attached in the matching bucket. Untyped books (ingested before
types existed) are grandfathered and attach as either."""

import argparse

import pytest

from openadventure.cli.main import _cmd_modules, _cmd_new, _cmd_template, build_parser
from openadventure.engine import commands
from openadventure.ingest import pipeline
from openadventure.store.workspace import BookTypeMismatch, ensure_book_type

BOOK = "# Rulebook\n\n## Combat\n\nRoll a d20 to attack.\n"


def _ingest(workspace, name, tmp_path, *, book_type):
    src = tmp_path / f"{name}.md"
    src.write_text(BOOK, encoding="utf-8")
    return pipeline.ingest(src, workspace.book_dir(name), book_type=book_type)


def _ingest_untyped(workspace, name, tmp_path):
    src = tmp_path / f"{name}.md"
    src.write_text(BOOK, encoding="utf-8")
    return pipeline.ingest(src, workspace.book_dir(name))


# --- manifest + lookup ------------------------------------------------------
def test_ingest_records_type_in_manifest(workspace, tmp_path):
    manifest = _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    assert manifest["type"] == "source"
    assert workspace.book_type("dnd5e") == "source"


def test_untyped_ingest_has_no_type(workspace, tmp_path):
    manifest = _ingest_untyped(workspace, "old-book", tmp_path)
    assert "type" not in manifest
    assert workspace.book_type("old-book") is None


def test_list_books_filters_by_kind_and_grandfathers_untyped(workspace, tmp_path):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    _ingest_untyped(workspace, "legacy", tmp_path)

    assert workspace.list_books() == ["death-house", "dnd5e", "legacy"]
    # a kind keeps its own type plus untyped books
    assert workspace.list_books("source") == ["dnd5e", "legacy"]
    assert workspace.list_books("module") == ["death-house", "legacy"]


# --- ensure_book_type -------------------------------------------------------
def test_ensure_book_type_rejects_wrong_type(workspace, tmp_path):
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    with pytest.raises(BookTypeMismatch):
        ensure_book_type(workspace, "death-house", "source")
    # the matching bucket and not-yet-ingested books pass
    ensure_book_type(workspace, "death-house", "module")
    ensure_book_type(workspace, "never-ingested", "source")


# --- session attach ---------------------------------------------------------
def test_add_source_rejects_a_module_book(workspace, campaign, make_session, tmp_path):
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    session = make_session(script=[])
    with pytest.raises(BookTypeMismatch):
        session.add_source("death-house")
    assert session.meta.sources == []


def test_add_module_rejects_a_source_book(workspace, campaign, make_session, tmp_path):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    session = make_session(script=[])
    with pytest.raises(BookTypeMismatch):
        session.add_module("dnd5e")
    assert session.meta.modules == []


def test_typed_books_attach_in_their_bucket(workspace, campaign, make_session, tmp_path):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    session = make_session(script=[])
    assert session.add_source("dnd5e") == "dnd5e"
    assert session.add_module("death-house") == "death-house"
    assert session.meta.sources == ["dnd5e"]
    assert [m.slug for m in session.meta.modules] == ["death-house"]


def test_untyped_book_attaches_as_either(workspace, campaign, make_session, tmp_path):
    _ingest_untyped(workspace, "legacy", tmp_path)
    session = make_session(script=[])
    assert session.add_source("legacy") == "legacy"
    assert session.add_module("legacy") == "legacy"


# --- create_campaign --------------------------------------------------------
def test_create_campaign_rejects_module_as_source(workspace, tmp_path):
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    with pytest.raises(BookTypeMismatch):
        workspace.create_campaign("Bad", sources=["death-house"])


def test_create_campaign_rejects_source_as_module(workspace, tmp_path):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    with pytest.raises(BookTypeMismatch):
        workspace.create_campaign("Bad", modules=["dnd5e"])


# --- commands (REPL-facing) -------------------------------------------------
def test_cmd_sources_add_wont_match_a_module(workspace, campaign, make_session, tmp_path):
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    session = make_session(script=[])
    result = commands.cmd_sources(session, "add death-house")
    assert any(m.severity == commands.Severity.error for m in result.messages)
    assert session.meta.sources == []


def test_cmd_modules_add_wont_match_a_source(workspace, campaign, make_session, tmp_path):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    session = make_session(script=[])
    result = commands.cmd_modules(session, "add dnd5e")
    assert any(m.severity == commands.Severity.error for m in result.messages)
    assert session.meta.modules == []


def test_sources_view_lists_only_source_books(workspace, campaign, make_session, tmp_path):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    session = make_session(script=[])
    view = commands.cmd_sources(session, "show").data
    assert view.available == ["dnd5e"]


# --- CLI --------------------------------------------------------------------
def test_ingest_parser_requires_a_type_flag():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["ingest", "book.pdf"])  # neither --source nor --module
    assert parser.parse_args(["ingest", "book.pdf", "--source"]).as_type == "source"
    assert parser.parse_args(["ingest", "book.pdf", "--module"]).as_type == "module"


def test_ingest_parser_rejects_both_type_flags():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["ingest", "book.pdf", "--source", "--module"])


def test_cli_new_rejects_wrong_typed_source(workspace, tmp_path, capsys):
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    args = argparse.Namespace(
        workspace=str(workspace.root),
        name="My Game",
        mode="gm",
        source=["death-house"],
        module=None,
    )
    rc = _cmd_new(args)
    assert rc == 1
    assert "adventure module" in capsys.readouterr().out


def test_cli_modules_add_rejects_a_source_book(workspace, campaign, tmp_path, capsys):
    _ingest(workspace, "dnd5e", tmp_path, book_type="source")
    args = argparse.Namespace(
        workspace=str(workspace.root),
        campaign=campaign.load_meta().slug,
        add="dnd5e",
        remove=None,
        activate=None,
        reorder=None,
        arc=None,
    )
    rc = _cmd_modules(args)
    assert rc == 1
    out = capsys.readouterr().out
    assert "rules source" in out
    # nothing attached
    assert [m.slug for m in campaign.load_meta().modules] == []


def test_cli_template_rejects_a_module_book(workspace, tmp_path, capsys):
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    args = argparse.Namespace(
        workspace=str(workspace.root),
        source="death-house",
    )
    rc = _cmd_template(args)
    assert rc == 1
    assert "adventure module" in capsys.readouterr().out


def test_cli_modules_add_accepts_a_module_book(workspace, campaign, tmp_path, capsys):
    _ingest(workspace, "death-house", tmp_path, book_type="module")
    args = argparse.Namespace(
        workspace=str(workspace.root),
        campaign=campaign.load_meta().slug,
        add="death-house",
        remove=None,
        activate=None,
        reorder=None,
        arc=None,
    )
    rc = _cmd_modules(args)
    assert rc == 0
    assert [m.slug for m in campaign.load_meta().modules] == ["death-house"]
