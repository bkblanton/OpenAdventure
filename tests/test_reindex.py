"""reindex rebuilds every derived index from markdown; backend switching."""

import argparse

from openadventure.cli.main import _cmd_reindex, _reindex_targets
from openadventure.cli.term import make_console
from openadventure.ingest import embeddings, pipeline, xref
from openadventure.store.workspace import ModuleRef
from tests.test_embeddings import FakeEmbeddingBackend
from tests.test_xref import BESTIARY_MD


def test_reindex_rebuilds_fts_xref_and_embeddings(tmp_path):
    src = tmp_path / "rules.md"
    src.write_text(BESTIARY_MD, encoding="utf-8")
    dest = tmp_path / "rs"
    pipeline.ingest(src, dest)  # FTS5 + xref, no embeddings yet
    assert not (dest / embeddings.EMBEDDINGS_NAME).is_file()

    # wipe the derived indexes; reindex rebuilds them all from the markdown
    (dest / xref.XREF_NAME).unlink()
    backend = FakeEmbeddingBackend()
    pipeline.reindex(dest, embed_backend=backend)

    assert (dest / xref.XREF_NAME).is_file()
    assert (dest / embeddings.EMBEDDINGS_NAME).is_file()
    refs = xref.references_for(dest / xref.XREF_NAME, "encounters/cragmaw-ambush.md")
    assert {r.name for r in refs} == {"goblin", "fire elemental"}


def test_index_report_counts_and_flags_dangling(tmp_path):
    src = tmp_path / "rules.md"
    src.write_text(BESTIARY_MD, encoding="utf-8")
    dest = tmp_path / "rs"
    pipeline.ingest(src, dest, embed_backend=FakeEmbeddingBackend())

    report = pipeline.index_report(dest)
    assert report["sections"] == 6
    assert report["entities"] == 2  # goblin, fire elemental
    assert report["edges"] >= 2
    assert report["windows"] == 4  # one window per non-empty section body
    assert report["embed_model"] == "fake-v1"
    assert report["dangling"] == 0

    # a cross-ref whose target section vanished is surfaced
    next((dest / "sections").rglob("goblin.md")).unlink()
    assert pipeline.index_report(dest)["dangling"] >= 1


def test_switch_embedding_backend_reembeds_and_old_model_falls_back(tmp_path):
    src = tmp_path / "rules.md"
    src.write_text("# Defense\n\nThe dragon is tough and has high armor class.\n", encoding="utf-8")
    dest = tmp_path / "rs"
    v1 = FakeEmbeddingBackend("fake-v1")
    pipeline.ingest(src, dest, embed_backend=v1)
    assert embeddings.stored_identity(dest / embeddings.EMBEDDINGS_NAME) == ("fake-v1", 3)
    assert embeddings.hybrid_search(dest, "hard to hit", 5, v1)  # works under v1

    # switch the backend and reindex -> the vector space is rebuilt for v2
    v2 = FakeEmbeddingBackend("fake-v2")
    pipeline.reindex(dest, embed_backend=v2)
    assert embeddings.stored_identity(dest / embeddings.EMBEDDINGS_NAME) == ("fake-v2", 3)
    assert embeddings.hybrid_search(dest, "hard to hit", 5, v2)

    # the now-stale v1 backend can't read the v2 index -> graceful FTS5-only
    # ("hard to hit" shares no token with the text, so FTS5 returns nothing)
    assert embeddings.hybrid_search(dest, "hard to hit", 5, v1) == []


def _ns(workspace, **over):
    base = dict(
        workspace=str(workspace.root),
        book=None,
        campaign=None,
        all=False,
        no_embeddings=True,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_reindex_targets_resolve_source_campaign_and_all(workspace, campaign, tmp_path):
    rules = tmp_path / "rules.md"
    rules.write_text(BESTIARY_MD, encoding="utf-8")
    pipeline.ingest(rules, workspace.book_dir("dnd5e"))
    module = tmp_path / "mod.md"
    module.write_text(BESTIARY_MD, encoding="utf-8")
    pipeline.ingest(module, workspace.book_dir("death-house"))
    # the campaign uses dnd5e as a rules source and death-house as a module
    meta = campaign.load_meta()
    meta.sources = ["dnd5e"]
    meta.modules = [ModuleRef(slug="death-house", title="Death House", order=0)]
    campaign.save_meta(meta)

    console = make_console()
    only_book = _reindex_targets(workspace, _ns(workspace, book="dnd5e"), console)
    assert [label for label, _ in only_book] == ["book dnd5e"]

    by_campaign = _reindex_targets(
        workspace, _ns(workspace, campaign=campaign.meta_path.parent.name), console
    )
    assert {label for label, _ in by_campaign} == {"book dnd5e", "book death-house"}

    everything = {
        label for label, _ in _reindex_targets(workspace, _ns(workspace, all=True), console)
    }
    assert "book dnd5e" in everything
    assert "book death-house" in everything


def test_cli_reindex_all_rebuilds(workspace, campaign, tmp_path, capsys):
    rules = tmp_path / "rules.md"
    rules.write_text(BESTIARY_MD, encoding="utf-8")
    pipeline.ingest(rules, workspace.book_dir("dnd5e"))
    (workspace.book_dir("dnd5e") / xref.XREF_NAME).unlink()

    rc = _cmd_reindex(_ns(workspace, all=True))
    assert rc == 0
    assert (workspace.book_dir("dnd5e") / xref.XREF_NAME).is_file()
    assert "Reindexed" in capsys.readouterr().out
