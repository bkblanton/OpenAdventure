"""Embeddings: windowing, vector store, RRF fusion, hybrid search, fallbacks.

Uses a deterministic concept-based fake backend so the suite never needs the
optional `fastembed` dependency (which has no Python 3.14 wheels yet)."""

import re

import pytest

from openadventure.engine.tools import build_registry
from openadventure.ingest import embeddings, pipeline
from tests.test_sheet_tools import make_ctx

# token -> concept dimension. Synonyms share a dimension, so "hard to hit"
# embeds near "high armor class" without sharing any literal token (which is
# exactly what FTS5 keyword search cannot do).
_CONCEPTS = {
    "hard": 0,
    "hit": 0,
    "high": 0,
    "armor": 0,
    "class": 0,
    "tough": 0,
    "defense": 0,
    "fire": 1,
    "flame": 1,
    "burn": 1,
    "fireball": 1,
    "blaze": 1,
    "heal": 2,
    "restore": 2,
    "cure": 2,
    "mend": 2,
}
_TOKEN = re.compile(r"[a-z]+")


class FakeEmbeddingBackend:
    """Multi-hot over a tiny concept space. Records every text embedded so tests
    can assert the incremental path only re-embeds what changed."""

    def __init__(self, model_id="fake-v1"):
        self.model_id = model_id
        self.dims = 3
        self.embedded: list[str] = []

    def _vec(self, text: str) -> list[float]:
        v = [0.0, 0.0, 0.0]
        for tok in _TOKEN.findall(text.lower()):
            if tok in _CONCEPTS:
                v[_CONCEPTS[tok]] += 1.0
        return v

    def embed(self, texts):
        self.embedded.extend(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


# --- windowing --------------------------------------------------------------


def test_windows_cover_body_with_overlap():
    body = " ".join(f"w{i}" for i in range(25))
    wins = embeddings.windows_for_body(body, "p.md", window_words=10, stride=7)
    assert [w.text.split()[0] for w in wins] == ["w0", "w7", "w14", "w21"]
    # each window's char slice round-trips to its stored text
    assert all(body[w.char_start : w.char_end] == w.text for w in wins)
    # last window reaches the final word; consecutive windows overlap
    assert wins[-1].text.split()[-1] == "w24"
    assert set(wins[0].text.split()) & set(wins[1].text.split())


def test_window_boundary_phrase_survives_in_some_window():
    # a key phrase straddling a stride boundary must appear intact in ≥1 window
    body = " ".join(f"w{i}" for i in range(20))
    wins = embeddings.windows_for_body(body, "p.md", window_words=8, stride=6)
    assert any("w7 w8 w9" in w.text for w in wins)


def test_short_and_empty_bodies():
    assert embeddings.windows_for_body("", "p.md") == []
    one = embeddings.windows_for_body("just three words", "p.md", window_words=10, stride=7)
    assert len(one) == 1 and one[0].text == "just three words"


# --- fusion -----------------------------------------------------------------


def test_rrf_merge_rewards_agreement_and_breaks_ties_by_first_seen():
    order = embeddings.rrf_merge(["a", "b", "c"], ["c", "d", "a"])
    assert order[0] == "a"  # in both lists, and seen first vs. the symmetric "c"
    assert set(order) == {"a", "b", "c", "d"}


# --- store + vector search --------------------------------------------------

ROWS = [
    (
        "Defense",
        "Defense",
        "The dragon is tough and has high armor class.",
        "rules/defense.md",
        "section",
    ),
    (
        "Conflagration",
        "Spells > Conflagration",
        "A roaring flame and fire engulfs the area.",
        "spells/conflagration.md",
        "section",
    ),
    (
        "Mending",
        "Spells > Mending",
        "Words that restore and cure wounds.",
        "spells/mending.md",
        "section",
    ),
]


def test_vector_search_finds_nearest_and_checks_identity(tmp_path):
    db = tmp_path / embeddings.EMBEDDINGS_NAME
    backend = FakeEmbeddingBackend()
    embeddings.build_embeddings(db, ROWS, backend)

    # "hard to hit" -> defense concept -> the armor-class window, despite zero
    # shared tokens with the stored text
    hits = embeddings.vector_search(db, backend.embed_query("hard to hit"), 3, model_id="fake-v1")
    assert hits and hits[0].path == "rules/defense.md"

    # a different model can't read these vectors -> nothing (graceful)
    assert embeddings.vector_search(db, [1.0, 0, 0], 3, model_id="other-model") == []


def test_incremental_reembed_only_touches_changed_windows(tmp_path):
    db = tmp_path / embeddings.EMBEDDINGS_NAME
    backend = FakeEmbeddingBackend()
    embeddings.build_embeddings(db, ROWS, backend)
    first = len(backend.embedded)
    assert first == len(ROWS)  # one window each (short bodies)

    backend.embedded.clear()
    changed = list(ROWS)
    changed[1] = (*changed[1][:2], "An inferno of blaze and fire.", *changed[1][3:])
    embeddings.build_embeddings(db, changed, backend)
    assert backend.embedded == ["An inferno of blaze and fire."]  # only the edited body


# --- hybrid search ----------------------------------------------------------

HYBRID_MD = """\
# Defense

The dragon is tough and has high armor class. Blows glance off its scales.

# Conflagration

A roaring fire spell. Flame engulfs everything and things burn.
"""


def test_hybrid_with_no_backend_equals_fts(tmp_path):
    dest = tmp_path / "rs"
    src = tmp_path / "r.md"
    src.write_text(HYBRID_MD, encoding="utf-8")
    pipeline.ingest(src, dest)  # no embeddings built

    from openadventure.ingest import indexer

    fts = indexer.search(dest / indexer.INDEX_NAME, "fire spell", 5)
    hybrid = embeddings.hybrid_search(dest, "fire spell", 5, backend=None)
    assert [h.path for h in hybrid] == [h.path for h in fts]


def test_hybrid_recovers_semantic_match_fts_misses(tmp_path):
    dest = tmp_path / "rs"
    src = tmp_path / "r.md"
    src.write_text(HYBRID_MD, encoding="utf-8")
    backend = FakeEmbeddingBackend()
    pipeline.ingest(src, dest, embed_backend=backend)

    from openadventure.ingest import indexer

    # keyword search finds nothing: neither "hard" nor "hit" appears in the text
    assert indexer.search(dest / indexer.INDEX_NAME, "hard to hit", 5) == []
    # hybrid recovers the armor-class section via the vector leg
    hybrid = embeddings.hybrid_search(dest, "hard to hit", 5, backend)
    assert hybrid and hybrid[0].path.endswith("defense.md")


def test_hybrid_falls_back_when_model_switched(tmp_path):
    dest = tmp_path / "rs"
    src = tmp_path / "r.md"
    src.write_text(HYBRID_MD, encoding="utf-8")
    pipeline.ingest(src, dest, embed_backend=FakeEmbeddingBackend("fake-v1"))

    # a different model can't use the stored vectors -> FTS5-only, no crash
    other = FakeEmbeddingBackend("fake-v2")
    assert embeddings.hybrid_search(dest, "hard to hit", 5, other) == []  # FTS5 misses it
    assert embeddings.hybrid_search(dest, "fire", 5, other)  # FTS5 still works


def test_load_backend_default_local_falls_back_gracefully(monkeypatch):
    # disabled is None
    assert embeddings.load_backend({"backend": "none"}) is None

    # the default 'local' backend must fall back to None on ANY construction
    # failure (missing dep, model download error), never raise into a session
    def _raise(cls, cfg):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(embeddings.LocalEmbeddingBackend, "from_config", classmethod(_raise))
    assert embeddings.load_backend({}) is None  # default is local
    assert embeddings.load_backend({"backend": "local"}) is None


def test_try_load_backend_distinguishes_not_installed_from_load_failure(monkeypatch):
    assert embeddings.try_load_backend({"backend": "none"}) == (None, None)

    # installed but the model failed to load -> reason says installed + the error,
    # NOT a misleading "install the extra"
    def _model_fail(cls, cfg):
        raise RuntimeError("connection blocked")

    monkeypatch.setattr(embeddings.LocalEmbeddingBackend, "from_config", classmethod(_model_fail))
    backend, reason = embeddings.try_load_backend({"backend": "local"})
    assert backend is None
    assert "installed" in reason and "connection blocked" in reason
    assert "uv sync" not in reason

    # genuinely not installed -> the install hint
    def _import_fail(cls, cfg):
        raise ImportError("No module named 'fastembed'")

    monkeypatch.setattr(embeddings.LocalEmbeddingBackend, "from_config", classmethod(_import_fail))
    _, reason = embeddings.try_load_backend({"backend": "local"})
    assert "uv sync --extra embeddings" in reason


def test_load_backend_custom_spec_is_validated():
    # an explicit (non-default) backend with a bad spec raises; typos aren't
    # silently swallowed the way the default's missing dependency is
    with pytest.raises(ValueError):
        embeddings.load_backend({"backend": "notaspec"})


def test_available_returns_bool():
    assert isinstance(embeddings.available(), bool)


def test_local_backend_routes_cache_dir_and_model_path(monkeypatch):
    # inject a fake fastembed so this needs no real model / no network
    import sys
    import types

    captured: dict = {}

    class FakeTextEmbedding:
        def __init__(self, **kwargs):
            captured.clear()
            captured.update(kwargs)

        def embed(self, texts):
            return [[0.1, 0.2, 0.3]]

    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake)

    # default: cache_dir is set (persistent, not %TEMP%), no specific_model_path
    b = embeddings.LocalEmbeddingBackend.from_config({"model": "m"})
    assert captured["model_name"] == "m"
    assert "cache_dir" in captured and "specific_model_path" not in captured
    assert b.dims == 3

    # model_path -> fully offline: specific_model_path, no cache_dir/HF fetch
    embeddings.LocalEmbeddingBackend.from_config({"model_path": "/models/bge"})
    assert "specific_model_path" in captured and "cache_dir" not in captured


def test_search_rules_tool_uses_hybrid_backend(workspace, campaign, tmp_path):
    src = tmp_path / "r.md"
    src.write_text(HYBRID_MD, encoding="utf-8")
    backend = FakeEmbeddingBackend()
    pipeline.ingest(src, workspace.book_dir("dnd5e"), embed_backend=backend)
    meta = campaign.load_meta()
    meta.sources = ["dnd5e"]
    meta.system_source = "dnd5e"
    campaign.save_meta(meta)

    registry = build_registry(workspace, campaign, meta, embed_backend=backend)
    ctx = make_ctx(workspace, campaign)
    out = registry.dispatch(ctx, "search_rules", {"query": "hard to hit"})
    assert out.ok and "defense" in out.content.lower()
