"""
memory/knowledge_store.py — Miko's semantic knowledge store (the "second brain" index).

A lightweight local vector store over two kinds of content:
  - "fact"  : structured personal facts from long_term.json
  - "note"  : chunks of the Obsidian/Miko Notes vault (Markdown files)

Embeddings come from memory/embeddings.py (local fastembed or a provider API).
Vectors are kept in a single SQLite table and similarity is a NumPy cosine over
the rows — brute force, but instant at personal scale (thousands of chunks). If
no embedding backend is available, search degrades to SQLite keyword matching so
recall still works.

This is deliberately dependency-light: sqlite3 + numpy only (both already present).
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from memory import embeddings

logger = logging.getLogger("miko.knowledge")

_lock = threading.RLock()
_conn = None
_db_path = None

_CHUNK_CHARS = 700          # target characters per note chunk
_CHUNK_MIN = 80             # don't index trivially short chunks


# ── Connection ────────────────────────────────────────────────────────────────

def _path() -> Path:
    global _db_path
    if _db_path is None:
        from config import CONFIG
        _db_path = CONFIG.data_dir / "knowledge.db"
    return _db_path


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(p), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS chunks (
                   id       INTEGER PRIMARY KEY AUTOINCREMENT,
                   kind     TEXT NOT NULL,
                   ref      TEXT NOT NULL,
                   title    TEXT,
                   text     TEXT NOT NULL,
                   vec      BLOB,
                   dim      INTEGER,
                   backend  TEXT,
                   mtime    REAL,
                   updated  REAL,
                   UNIQUE(kind, ref)
               )"""
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_kind ON chunks(kind)")
        _conn.commit()
    return _conn


# ── Write ─────────────────────────────────────────────────────────────────────

def _to_blob(vec) -> bytes | None:
    if vec is None:
        return None
    return np.asarray(vec, dtype=np.float32).tobytes()


def upsert_many(items: list[dict]) -> int:
    """Insert/replace chunks. Each item: {kind, ref, title, text, mtime?}.
    Embeds all texts in one batch when a backend is available."""
    items = [it for it in items if (it.get("text") or "").strip()]
    if not items:
        return 0

    vecs = embeddings.embed([it["text"] for it in items])
    bname = embeddings.backend_name()
    dim = len(vecs[0]) if vecs else None

    now = time.time()
    rows = []
    for i, it in enumerate(items):
        v = vecs[i] if vecs else None
        rows.append((
            it["kind"], it["ref"], it.get("title", ""), it["text"],
            _to_blob(v), dim, bname if v is not None else "",
            it.get("mtime"), now,
        ))
    with _lock:
        db = _db()
        db.executemany(
            """INSERT INTO chunks (kind, ref, title, text, vec, dim, backend, mtime, updated)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(kind, ref) DO UPDATE SET
                   title=excluded.title, text=excluded.text, vec=excluded.vec,
                   dim=excluded.dim, backend=excluded.backend,
                   mtime=excluded.mtime, updated=excluded.updated""",
            rows,
        )
        db.commit()
    return len(rows)


def upsert(kind: str, ref: str, title: str, text: str, mtime: float | None = None) -> int:
    return upsert_many([{"kind": kind, "ref": ref, "title": title, "text": text, "mtime": mtime}])


def delete_ref(kind: str, ref: str) -> None:
    with _lock:
        db = _db()
        db.execute("DELETE FROM chunks WHERE kind=? AND ref=?", (kind, ref))
        db.commit()


def delete_prefix(kind: str, ref_prefix: str) -> None:
    """Drop all chunks of a kind whose ref starts with a prefix (e.g. a note path)."""
    with _lock:
        db = _db()
        db.execute("DELETE FROM chunks WHERE kind=? AND ref LIKE ?", (kind, ref_prefix + "%"))
        db.commit()


# ── Search ────────────────────────────────────────────────────────────────────

def search(query: str, k: int = 6, kinds: list[str] | None = None) -> list[dict]:
    """Return the top-k most relevant chunks for the query.
    Semantic (cosine) when embeddings are available + indexed; keyword otherwise."""
    query = (query or "").strip()
    if not query:
        return []

    qv = embeddings.embed([query])
    if qv:
        sem = _search_semantic(query, qv[0], k, kinds)
        if sem:
            return sem
    return _search_keyword(query, k, kinds)


def _rows(kinds):
    db = _db()
    if kinds:
        ph = ",".join("?" * len(kinds))
        cur = db.execute(
            f"SELECT kind, ref, title, text, vec, dim, backend FROM chunks WHERE kind IN ({ph})",
            list(kinds),
        )
    else:
        cur = db.execute("SELECT kind, ref, title, text, vec, dim, backend FROM chunks")
    return cur.fetchall()


def _search_semantic(query, qvec, k, kinds):
    bname = embeddings.backend_name()
    with _lock:
        rows = _rows(kinds)
    mats, meta = [], []
    for kind, ref, title, text, vec, dim, backend in rows:
        if vec is None or backend != bname:
            continue   # un-embedded or embedded by a different backend
        mats.append(np.frombuffer(vec, dtype=np.float32))
        meta.append((kind, ref, title, text))
    if not mats:
        return []
    M = np.vstack(mats)
    M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    q = np.asarray(qvec, dtype=np.float32)
    q = q / (np.linalg.norm(q) + 1e-9)
    scores = M @ q
    order = np.argsort(-scores)[:k]
    out = []
    for i in order:
        kind, ref, title, text = meta[i]
        out.append({"kind": kind, "ref": ref, "title": title,
                    "text": text, "score": round(float(scores[i]), 4)})
    return out


def _search_keyword(query, k, kinds):
    terms = [t for t in query.lower().split() if len(t) > 2] or [query.lower()]
    with _lock:
        rows = _rows(kinds)
    scored = []
    for kind, ref, title, text, vec, dim, backend in rows:
        hay = (title + " " + text).lower()
        score = sum(hay.count(t) for t in terms)
        if score:
            scored.append((score, {"kind": kind, "ref": ref, "title": title,
                                   "text": text, "score": float(score)}))
    scored.sort(key=lambda x: -x[0])
    return [s[1] for s in scored[:k]]


# ── Indexing helpers ──────────────────────────────────────────────────────────

def _chunk_text(body: str) -> list[str]:
    """Split a note body into ~_CHUNK_CHARS chunks on paragraph boundaries."""
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 2 <= _CHUNK_CHARS:
            cur = (cur + "\n\n" + p) if cur else p
        else:
            if cur:
                chunks.append(cur)
            # a single huge paragraph → hard-split it
            while len(p) > _CHUNK_CHARS:
                chunks.append(p[:_CHUNK_CHARS])
                p = p[_CHUNK_CHARS:]
            cur = p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if len(c) >= _CHUNK_MIN] or ([body.strip()] if body.strip() else [])


def _strip_frontmatter(raw: str) -> str:
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            return raw[end + 4:].lstrip()
    return raw


def index_note_file(path: Path) -> int:
    """(Re)index a single Markdown note. Skips work if unchanged since last index."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0
    ref_prefix = str(path.resolve())
    with _lock:
        db = _db()
        row = db.execute(
            "SELECT mtime FROM chunks WHERE kind='note' AND ref LIKE ? LIMIT 1",
            (ref_prefix + "#%",),
        ).fetchone()
    if row and row[0] and abs(row[0] - mtime) < 1e-6:
        return 0   # unchanged

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    body = _strip_frontmatter(raw)
    chunks = _chunk_text(body)
    delete_prefix("note", ref_prefix + "#")
    items = [{
        "kind": "note", "ref": f"{ref_prefix}#{i}",
        "title": path.stem, "text": c, "mtime": mtime,
    } for i, c in enumerate(chunks)]
    return upsert_many(items)


def reindex_notes(notes_dir) -> dict:
    """Index every Markdown file in the vault (incrementally — unchanged files skip)."""
    base = Path(notes_dir)
    if not base.exists():
        return {"files": 0, "chunks": 0}
    files = chunks = 0
    for md in base.rglob("*.md"):
        n = index_note_file(md)
        if n:
            files += 1
            chunks += n
    if files:
        logger.info(f"knowledge: indexed {chunks} chunks from {files} note(s)")
    return {"files": files, "chunks": chunks}


def index_facts(memory: dict) -> int:
    """Index structured personal facts so they're recall-able semantically."""
    if not isinstance(memory, dict):
        return 0
    items = []
    for category, entries in memory.items():
        if not isinstance(entries, dict):
            continue
        for key, entry in entries.items():
            val = entry.get("value") if isinstance(entry, dict) else entry
            if not val:
                continue
            label = key.replace("_", " ")
            items.append({
                "kind": "fact", "ref": f"{category}/{key}",
                "title": label, "text": f"{label}: {val}",
            })
    return upsert_many(items) if items else 0


def stats() -> dict:
    with _lock:
        db = _db()
        total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        notes = db.execute("SELECT COUNT(*) FROM chunks WHERE kind='note'").fetchone()[0]
        facts = db.execute("SELECT COUNT(*) FROM chunks WHERE kind='fact'").fetchone()[0]
        embedded = db.execute("SELECT COUNT(*) FROM chunks WHERE vec IS NOT NULL").fetchone()[0]
    return {"total": total, "notes": notes, "facts": facts,
            "embedded": embedded, "backend": embeddings.backend_name()}
