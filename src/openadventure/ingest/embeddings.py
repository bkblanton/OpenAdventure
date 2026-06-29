"""Optional embedding-based retrieval, fused with FTS5 into hybrid search.

Design points that matter:

* **Optional & graceful.** No backend configured (or its dependency missing) →
  everything falls back to FTS5-only. Embeddings are never required.
* **Structure-independent windows.** We embed fixed-size overlapping windows of
  the body, not whole sections, so a mis-split section (bad heading parse) can't
  poison a vector and a rule straddling a boundary survives in ≥1 window.
* **Model identity is checked.** Vectors from a different model are unusable; a
  stored index tagged with another model is ignored (FTS5-only) until reindex.
* **No heavy deps.** Vectors are stored as float32 bytes and scored with a
  brute-force cosine in pure Python, fine for a few thousand windows. The
  embedding model itself (``fastembed``) is an optional extra, imported lazily.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from array import array
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from openadventure.ingest import indexer
from openadventure.ingest.progress import PHASE_EMBED, ProgressFn, report

EMBEDDINGS_NAME = "embeddings.sqlite"
DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
# Persistent model cache, deliberately NOT %TEMP% (which Windows can purge,
# silently forcing a re-download) and NOT the workspace (which may be OneDrive-
# synced). Override with [embeddings] cache_dir.
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "openadventure" / "models"

WINDOW_WORDS = 200  # below bge-small's 512-token limit
WINDOW_STRIDE = 150  # ~50-word overlap so boundary-straddling rules stay intact
RRF_K = 60  # reciprocal-rank-fusion constant
# fastembed's internal batch size. 256 keeps the ONNX matmuls large and
# efficient; the whole corpus streams through one embed() call and progress is
# reported as each vector comes back, so a big batch costs nothing in UX.
EMBED_BATCH = 256


@runtime_checkable
class EmbeddingBackend(Protocol):
    model_id: str
    dims: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...  # batch, for indexing
    def embed_query(self, text: str) -> list[float]: ...


# --- windowing --------------------------------------------------------------


@dataclass
class Window:
    path: str
    char_start: int
    char_end: int
    text: str


_NONSPACE = re.compile(r"\S+")


def windows_for_body(
    body: str, path: str, *, window_words: int = WINDOW_WORDS, stride: int = WINDOW_STRIDE
) -> list[Window]:
    """Overlapping word-windows over a body, independent of headings. A body
    shorter than one window yields a single window covering all of it."""
    spans = [(m.start(), m.end()) for m in _NONSPACE.finditer(body)]
    if not spans:
        return []
    out: list[Window] = []
    i = 0
    while i < len(spans):
        chunk = spans[i : i + window_words]
        start, end = chunk[0][0], chunk[-1][1]
        out.append(Window(path=path, char_start=start, char_end=end, text=body[start:end]))
        if i + window_words >= len(spans):
            break
        i += stride
    return out


# --- vector encoding / scoring (dependency-free) ----------------------------


def _to_bytes(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _from_bytes(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _hash(model_id: str, text: str) -> str:
    return hashlib.sha1(f"{model_id}\n{text}".encode()).hexdigest()


# --- storage ----------------------------------------------------------------


def stored_identity(db_path: Path) -> tuple[str, int] | None:
    """(model_id, dims) the index was built with, or None if absent/unreadable."""
    if not db_path.is_file():
        return None
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT model_id, dims FROM meta").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    return (row[0], row[1]) if row else None


def is_compatible(db_path: Path, backend: EmbeddingBackend) -> bool:
    identity = stored_identity(db_path)
    return identity is not None and identity[0] == backend.model_id


def build_embeddings(
    db_path: Path,
    rows: list[tuple[str, str, str, str, str]],
    backend: EmbeddingBackend,
    *,
    window_words: int = WINDOW_WORDS,
    stride: int = WINDOW_STRIDE,
    progress: ProgressFn | None = None,
) -> int:
    """(Re)build the window-vector index. Incremental: a window whose
    (model, text) hash already exists reuses its vector, so a reindex after a
    few edits re-embeds only what changed. A model switch invalidates every
    hash (the model id is folded in), forcing a full re-embed. Returns the
    window count."""
    windows: list[Window] = []
    for _title, _breadcrumb, body, path, _kind in rows:
        windows.extend(windows_for_body(body, path, window_words=window_words, stride=stride))

    prior: dict[str, bytes] = {}
    if db_path.is_file():
        con = sqlite3.connect(db_path)
        try:
            for h, vec in con.execute("SELECT text_hash, vector FROM windows"):
                prior[h] = vec
        except sqlite3.OperationalError:
            pass
        finally:
            con.close()

    records: list[tuple[str, int, int, str, bytes | None]] = []
    pending_text: list[str] = []
    pending_at: list[int] = []
    for w in windows:
        h = _hash(backend.model_id, w.text)
        if h in prior:
            records.append((w.path, w.char_start, w.char_end, h, prior[h]))
        else:
            pending_at.append(len(records))
            records.append((w.path, w.char_start, w.char_end, h, None))
            pending_text.append(w.text)

    if pending_text:
        total = len(pending_text)
        report(progress, PHASE_EMBED, 0, total)
        # Stream the whole list through the backend in one call: fastembed keeps
        # its session hot and batches at EMBED_BATCH internally, which is far
        # cheaper than re-entering embed() per 64 windows. A backend that streams
        # (iter_embed) lets us report progress as vectors arrive; a plain one
        # (the test fake) just returns a list we iterate the same way.
        stream = getattr(backend, "iter_embed", None)
        produced = stream(pending_text) if stream is not None else iter(backend.embed(pending_text))
        for done, (at, vec) in enumerate(zip(pending_at, produced, strict=True), start=1):
            path, cs, ce, h, _ = records[at]
            records[at] = (path, cs, ce, h, _to_bytes(vec))
            if done % EMBED_BATCH == 0 or done == total:
                report(progress, PHASE_EMBED, done, total)

    _write(db_path, backend.model_id, backend.dims, records)
    return len(records)


def _write(
    db_path: Path,
    model_id: str,
    dims: int,
    records: list[tuple[str, int, int, str, bytes | None]],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE meta (model_id TEXT, dims INTEGER)")
        con.execute(
            "CREATE TABLE windows "
            "(path TEXT, char_start INTEGER, char_end INTEGER, text_hash TEXT, vector BLOB)"
        )
        con.execute("INSERT INTO meta VALUES (?, ?)", (model_id, dims))
        con.executemany("INSERT INTO windows VALUES (?, ?, ?, ?, ?)", records)
        con.execute("CREATE INDEX windows_hash ON windows(text_hash)")
        con.commit()
    finally:
        con.close()


@dataclass
class VectorHit:
    path: str
    char_start: int
    char_end: int
    score: float


def vector_search(
    db_path: Path, query_vec: list[float], k: int, *, model_id: str
) -> list[VectorHit]:
    """Nearest windows by cosine, deduped to one (best) hit per section path.
    Returns [] on identity mismatch or missing index (the graceful path)."""
    identity = stored_identity(db_path)
    if identity is None or identity[0] != model_id:
        return []
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT path, char_start, char_end, vector FROM windows").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    q = _normalize(query_vec)
    best: dict[str, VectorHit] = {}
    for path, cs, ce, blob in rows:
        v = _normalize(_from_bytes(blob))
        score = sum(a * b for a, b in zip(q, v, strict=False))
        cur = best.get(path)
        if cur is None or score > cur.score:
            best[path] = VectorHit(path=path, char_start=cs, char_end=ce, score=score)
    ranked = sorted(best.values(), key=lambda h: h.score, reverse=True)
    return ranked[:k]


# --- fusion -----------------------------------------------------------------


def rrf_merge(*ranked_lists: list[str], k: int = RRF_K) -> list[str]:
    """Reciprocal Rank Fusion: combine ranked key lists without needing to
    normalize their scores. Ties break by first appearance."""
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    for ranked in ranked_lists:
        for rank, key in enumerate(ranked):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            first_seen.setdefault(key, len(first_seen))
    return sorted(scores, key=lambda key: (-scores[key], first_seen[key]))


def hybrid_search(
    root: Path, query: str, k: int, backend: EmbeddingBackend | None
) -> list[indexer.SearchHit]:
    """FTS5 + vector retrieval fused with RRF. With no backend (or an
    incompatible/missing vector index) this is exactly FTS5 search (same
    results, same order), so it's a safe drop-in."""
    fts = indexer.search(root / indexer.INDEX_NAME, query, k=k * 3)
    emb_db = root / EMBEDDINGS_NAME
    if backend is None or not is_compatible(emb_db, backend):
        return fts[:k]

    vec = vector_search(emb_db, backend.embed_query(query), k=k * 3, model_id=backend.model_id)
    if not vec:
        return fts[:k]

    by_path = {h.path: h for h in fts}
    order = rrf_merge([h.path for h in fts], [v.path for v in vec])
    out: list[indexer.SearchHit] = []
    for path in order[:k]:
        hit = by_path.get(path) or indexer.fetch(root / indexer.INDEX_NAME, path)
        if hit is not None:
            out.append(hit)
    return out


# --- backend loading --------------------------------------------------------


class LocalEmbeddingBackend:
    """fastembed (ONNX, CPU), fully offline. ``fastembed`` is an optional extra
    (`uv sync --extra embeddings`); imported lazily so the base install and the
    test suite never require it."""

    def __init__(self, model_id: str, dims: int, model, *, parallel: int | None = None) -> None:
        self.model_id = model_id
        self.dims = dims
        self._model = model
        # fastembed data-parallelism for embed(): None = single process (onnxruntime
        # still uses every core for the matmuls), 0 = fan batches across all cores
        # via worker processes, N = N workers. Off by default: on an already
        # core-saturated box the extra processes can oversubscribe; expose it via
        # [embeddings] parallel so a big-corpus box can opt in.
        self._parallel = parallel

    @classmethod
    def from_config(cls, cfg: dict) -> LocalEmbeddingBackend:
        import os

        # quieter logs on Windows without Developer Mode (copies instead of symlinks)
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from fastembed import TextEmbedding  # lazy: optional dependency

        model_id = cfg.get("model") or DEFAULT_LOCAL_MODEL
        kwargs: dict = {}
        model_path = cfg.get("model_path")
        if model_path:
            # fully offline: point at a pre-downloaded model dir, no HF fetch
            kwargs["specific_model_path"] = str(Path(model_path).expanduser())
        else:
            kwargs["cache_dir"] = str(Path(cfg.get("cache_dir") or DEFAULT_CACHE_DIR).expanduser())
        model = TextEmbedding(model_name=model_id, **kwargs)
        probe = next(iter(model.embed(["dimension probe"])))
        parallel = cfg.get("parallel")
        return cls(model_id, len(list(probe)), model, parallel=parallel)

    def iter_embed(self, texts: list[str]) -> Iterator[list[float]]:
        """Stream vectors as fastembed produces them so indexing can report
        progress without buffering the whole corpus. ``tolist()`` converts each
        numpy row in one C call instead of element-by-element in Python."""
        for vec in self._model.embed(list(texts), batch_size=EMBED_BATCH, parallel=self._parallel):
            yield vec.tolist()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return list(self.iter_embed(texts))

    def embed_query(self, text: str) -> list[float]:
        query_embed = getattr(self._model, "query_embed", self._model.embed)
        return [float(x) for x in next(iter(query_embed([text])))]


def available() -> bool:
    """Whether the optional local embedding stack is importable. Lets the UI
    hint that semantic search is one `uv sync --extra embeddings` away."""
    try:
        import fastembed  # noqa: F401

        return True
    except ImportError:
        return False


def try_load_backend(cfg: dict | None) -> tuple[EmbeddingBackend | None, str | None]:
    """Resolve the configured embedding backend, returning (backend, reason).

    On success: (backend, None). When disabled: (None, None). When the default
    ``local`` backend can't be built, (None, reason) where reason distinguishes
    "not installed" from "installed but the model failed to load" (network,
    proxy/SSL, corrupt cache) and includes the underlying error, so a caller
    can tell the user what's actually wrong instead of guessing "install it"."""
    cfg = cfg or {}
    spec = (cfg.get("backend") or "local").strip().lower()
    if spec in ("none", "off", ""):
        return None, None
    if spec == "local":
        try:
            return LocalEmbeddingBackend.from_config(cfg), None
        except ImportError:
            return None, "fastembed is not installed. Run: uv sync --extra embeddings"
        except Exception as exc:
            model = cfg.get("model") or DEFAULT_LOCAL_MODEL
            return None, (
                f"fastembed is installed but the embedding model {model!r} failed to "
                f"load ({type(exc).__name__}: {exc}). This is usually the first-run "
                "model download being blocked (network/proxy/SSL) or a corrupt cache."
            )
    # custom 'package.module:Class' backend: explicit, so surface failures
    import importlib

    module_name, _, attr = spec.partition(":")
    if not attr:
        raise ValueError(f"embedding backend spec {spec!r} must be 'package.module:ClassName'")
    backend_cls = getattr(importlib.import_module(module_name), attr)
    return backend_cls.from_config(cfg), None


def load_backend(cfg: dict | None) -> EmbeddingBackend | None:
    """Resolve the configured embedding backend, or None (FTS5-only).

    ``local`` is the default, so hybrid search turns on automatically wherever
    the optional dependency is installed. When it isn't (or the model fails to
    load on first use), the default falls back to None rather than breaking a
    session. An explicitly-configured custom backend, by contrast, raises so a
    typo isn't silently ignored. Use :func:`try_load_backend` when you want the
    fallback reason."""
    return try_load_backend(cfg)[0]
