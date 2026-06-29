"""campaigns CLI: list the workspace, delete a campaign, rename one (moving its
directory and re-slugging), and fork one (copying the whole story to a new slug)."""

import argparse

import pytest

import openadventure.cli.term as term
from openadventure.cli.main import _cmd_campaigns


@pytest.fixture(autouse=True)
def _fresh_console(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(term, "_console", None)


def _ns(workspace, **over):
    base = dict(
        workspace=str(workspace.root), slug=None, delete=False, rename=None, fork=None, yes=False
    )
    base.update(over)
    return argparse.Namespace(**base)


# --- list -------------------------------------------------------------------
def test_campaigns_list_shows_each_slug(workspace, campaign, capsys):
    rc = _cmd_campaigns(_ns(workspace))
    out = capsys.readouterr().out
    assert rc == 0
    assert "test-quest" in out and "Test Quest" in out


def test_campaigns_list_when_empty(workspace, capsys):
    rc = _cmd_campaigns(_ns(workspace))
    assert rc == 0
    assert "No campaigns yet" in capsys.readouterr().out


def test_campaigns_slug_without_an_action_errors(workspace, campaign, capsys):
    rc = _cmd_campaigns(_ns(workspace, slug="test-quest"))
    assert rc == 1
    assert "Nothing to do" in capsys.readouterr().out


# --- delete -----------------------------------------------------------------
def test_campaigns_delete_removes_the_campaign(workspace, campaign, capsys):
    rc = _cmd_campaigns(_ns(workspace, delete=True, slug="test-quest", yes=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Deleted" in out
    assert not (workspace.campaigns_dir / "test-quest").exists()


def test_campaigns_delete_unknown_errors(workspace, capsys):
    rc = _cmd_campaigns(_ns(workspace, delete=True, slug="nope", yes=True))
    assert rc == 1
    assert "No campaign" in capsys.readouterr().out


def test_campaigns_delete_without_a_slug_errors(workspace, capsys):
    rc = _cmd_campaigns(_ns(workspace, delete=True))
    assert rc == 1
    assert "Specify which campaign" in capsys.readouterr().out


def test_campaigns_delete_cancelled_keeps_it(workspace, campaign, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    rc = _cmd_campaigns(_ns(workspace, delete=True, slug="test-quest", yes=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Cancelled" in out
    assert (workspace.campaigns_dir / "test-quest").is_dir()


def test_campaigns_delete_confirmed_via_prompt(workspace, campaign, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "y")
    rc = _cmd_campaigns(_ns(workspace, delete=True, slug="test-quest", yes=False))
    assert rc == 0
    assert not (workspace.campaigns_dir / "test-quest").exists()


# --- rename -----------------------------------------------------------------
def test_campaigns_rename_moves_dir_and_updates_meta(workspace, campaign, capsys):
    rc = _cmd_campaigns(_ns(workspace, slug="test-quest", rename="Grand Adventure"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Renamed" in out
    assert not (workspace.campaigns_dir / "test-quest").exists()
    moved = workspace.campaign("grand-adventure")
    meta = moved.load_meta()
    assert meta.slug == "grand-adventure"
    assert meta.name == "Grand Adventure"


def test_campaigns_rename_same_slug_updates_display_name_only(workspace, campaign, capsys):
    # a new name that slugs to the same value keeps the directory in place
    rc = _cmd_campaigns(_ns(workspace, slug="test-quest", rename="Test  Quest!"))
    assert rc == 0
    meta = workspace.campaign("test-quest").load_meta()
    assert meta.slug == "test-quest"
    assert meta.name == "Test  Quest!"


def test_campaigns_rename_rejects_existing_target(workspace, campaign, capsys):
    workspace.create_campaign("Other Quest")
    rc = _cmd_campaigns(_ns(workspace, slug="test-quest", rename="Other Quest"))
    assert rc == 1
    assert "already exists" in capsys.readouterr().out
    assert (workspace.campaigns_dir / "test-quest").is_dir()  # untouched


def test_campaigns_rename_unknown_errors(workspace, capsys):
    rc = _cmd_campaigns(_ns(workspace, slug="nope", rename="whatever"))
    assert rc == 1
    assert "No campaign" in capsys.readouterr().out


# --- fork -------------------------------------------------------------------
def test_campaigns_fork_copies_the_whole_story(workspace, campaign, capsys):
    # seed some story state so we can prove the fork is a deep copy
    (campaign.notes_dir / "secret.md").write_text("the butler did it", encoding="utf-8")

    rc = _cmd_campaigns(_ns(workspace, slug="test-quest", fork="Test Quest Redux"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Forked" in out

    forked = workspace.campaign("test-quest-redux")
    meta = forked.load_meta()
    assert meta.slug == "test-quest-redux"
    assert meta.name == "Test Quest Redux"
    assert (forked.notes_dir / "secret.md").read_text(encoding="utf-8") == "the butler did it"
    # the original is left intact
    assert (workspace.campaigns_dir / "test-quest").is_dir()


def test_campaigns_fork_gets_a_fresh_created_at(workspace, campaign, capsys):
    original = campaign.load_meta().created_at
    rc = _cmd_campaigns(_ns(workspace, slug="test-quest", fork="Branch"))
    assert rc == 0
    # forking doesn't mutate the source's timestamp; the fork has its own
    assert workspace.campaign("branch").load_meta().created_at  # fork has one
    assert workspace.campaign("test-quest").load_meta().created_at == original


def test_campaigns_fork_rejects_existing_target(workspace, campaign, capsys):
    workspace.create_campaign("Existing")
    rc = _cmd_campaigns(_ns(workspace, slug="test-quest", fork="Existing"))
    assert rc == 1
    assert "already exists" in capsys.readouterr().out


def test_campaigns_fork_unknown_errors(workspace, capsys):
    rc = _cmd_campaigns(_ns(workspace, slug="nope", fork="whatever"))
    assert rc == 1
    assert "No campaign" in capsys.readouterr().out


# --- parser -----------------------------------------------------------------
def test_parser_campaigns_accepts_slug_and_fork():
    from openadventure.cli.main import build_parser

    args = build_parser().parse_args(["campaigns", "test-quest", "--fork", "Redux"])
    assert args.slug == "test-quest"
    assert args.fork == "Redux"
    assert args.delete is False


def test_parser_campaigns_delete_and_rename_are_mutually_exclusive():
    from openadventure.cli.main import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["campaigns", "x", "--delete", "--rename", "y"])
