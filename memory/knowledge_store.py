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
import math
import re
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
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   kind        TEXT NOT NULL,
                   ref         TEXT NOT NULL,
                   title       TEXT,
                   text        TEXT NOT NULL,
                   vec         BLOB,
                   dim         INTEGER,
                   backend     TEXT,
                   mtime       REAL,
                   updated     REAL,
                   importance  REAL DEFAULT 1,
                   created     REAL,
                   last_used   REAL,
                   source      TEXT,
                   UNIQUE(kind, ref)
               )"""
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_kind ON chunks(kind)")
        # Migrate older DBs (v1 lacked the scoring columns).
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(chunks)").fetchall()}
        for col, decl in (("importance", "REAL DEFAULT 1"), ("created", "REAL"),
                          ("last_used", "REAL"), ("source", "TEXT")):
            if col not in cols:
                _conn.execute(f"ALTER TABLE chunks ADD COLUMN {col} {decl}")
        _conn.commit()
    return _conn


# ── Write ─────────────────────────────────────────────────────────────────────

def _to_blob(vec) -> bytes | None:
    if vec is None:
        return None
    return np.asarray(vec, dtype=np.float32).tobytes()


def upsert_many(items: list[dict]) -> int:
    """Insert/replace chunks. Each item: {kind, ref, title, text, mtime?,
    importance?, source?}. Embeds all texts in one batch when a backend is
    available. `created` is preserved across updates; `last_used` is refreshed."""
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
            it.get("mtime"), now, float(it.get("importance", 1)), now, now,
            it.get("source", ""),
        ))
    with _lock:
        db = _db()
        db.executemany(
            """INSERT INTO chunks
                   (kind, ref, title, text, vec, dim, backend, mtime, updated,
                    importance, created, last_used, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(kind, ref) DO UPDATE SET
                   title=excluded.title, text=excluded.text, vec=excluded.vec,
                   dim=excluded.dim, backend=excluded.backend,
                   mtime=excluded.mtime, updated=excluded.updated,
                   importance=excluded.importance,
                   created=COALESCE(chunks.created, excluded.created),
                   last_used=excluded.last_used, source=excluded.source""",
            rows,
        )
        db.commit()
    return len(rows)


def upsert(kind: str, ref: str, title: str, text: str, mtime: float | None = None,
           importance: float = 1, source: str = "") -> int:
    return upsert_many([{"kind": kind, "ref": ref, "title": title, "text": text,
                         "mtime": mtime, "importance": importance, "source": source}])


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

_RECENCY_TAU_DAYS = 45.0
_W_KEYWORD = 0.15       # keyword overlap boost on top of semantic relevance
_W_RECENCY = 0.25       # exp-decayed recency
_W_IMPORTANCE = 0.20    # normalised importance (1-5 → 0.2-1.0)


def _recency(ts: float, now: float) -> float:
    if not ts:
        return 0.0
    age_days = max(0.0, (now - ts) / 86400.0)
    return math.exp(-age_days / _RECENCY_TAU_DAYS)


def _kw_overlap(terms, title, text) -> float:
    if not terms:
        return 0.0
    hay = (str(title) + " " + str(text)).lower()
    return sum(1 for t in terms if t in hay) / len(terms)


def _rows(kinds):
    db = _db()
    sel = ("SELECT id, kind, ref, title, text, vec, backend, importance, created, last_used "
           "FROM chunks")
    if kinds:
        ph = ",".join("?" * len(kinds))
        cur = db.execute(sel + f" WHERE kind IN ({ph})", list(kinds))
    else:
        cur = db.execute(sel)
    return cur.fetchall()


def search(query: str, k: int = 6, kinds: list[str] | None = None) -> list[dict]:
    """Top-k chunks scored by relevance (semantic + keyword) + recency + importance.
    Falls back to keyword-only relevance when no embedding backend is available."""
    query = (query or "").strip()
    if not query:
        return []
    with _lock:
        rows = _rows(kinds)
    if not rows:
        return []

    now = time.time()
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
    bname = embeddings.backend_name()
    qv = embeddings.embed([query])
    q = None
    if qv:
        q = np.asarray(qv[0], dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-9)

    scored = []
    for (rid, kind, ref, title, text, vec, backend, importance, created, last_used) in rows:
        rel = 0.0
        if q is not None and vec is not None and backend == bname:
            v = np.frombuffer(vec, dtype=np.float32)
            v = v / (np.linalg.norm(v) + 1e-9)
            rel = float(v @ q)
        kw = _kw_overlap(terms, title, text)
        relevance = (rel + _W_KEYWORD * kw) if q is not None else kw
        if relevance <= 0:
            continue
        score = (relevance
                 + _W_RECENCY * _recency(created or last_used, now)
                 + _W_IMPORTANCE * (float(importance or 1) / 5.0))
        scored.append((score, rid, {"kind": kind, "ref": ref, "title": title,
                                    "text": text, "score": round(score, 4)}))

    scored.sort(key=lambda x: -x[0])
    top = scored[:k]
    if top:   # keep frequently-recalled memories "fresh"
        try:
            with _lock:
                db = _db()
                db.executemany("UPDATE chunks SET last_used=? WHERE id=?",
                               [(now, t[1]) for t in top])
                db.commit()
        except Exception:
            pass
    return [t[2] for t in top]


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


def clear_kind(kind: str) -> None:
    with _lock:
        db = _db()
        db.execute("DELETE FROM chunks WHERE kind=?", (kind,))
        db.commit()


def index_facts(memory: dict) -> int:
    """Re-index structured personal facts (full refresh) so the store mirrors
    long_term.json — removed/renamed facts are purged, not left as stale vectors."""
    if not isinstance(memory, dict):
        return 0
    clear_kind("fact")
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
                "importance": 3, "source": "fact",
            })
    return upsert_many(items) if items else 0


def recent(kind: str, limit: int = 20) -> list:
    """Return the text of the newest rows of a kind (most recent first)."""
    with _lock:
        db = _db()
        cur = db.execute(
            "SELECT text FROM chunks WHERE kind=? ORDER BY updated DESC LIMIT ?",
            (kind, limit),
        )
        return [r[0] for r in cur.fetchall()]


def prune(kind: str, keep: int = 40) -> int:
    """Keep only the newest `keep` rows of a kind; delete older ones."""
    with _lock:
        db = _db()
        cur = db.execute(
            """DELETE FROM chunks WHERE kind=? AND id NOT IN
                   (SELECT id FROM chunks WHERE kind=? ORDER BY updated DESC LIMIT ?)""",
            (kind, kind, keep),
        )
        db.commit()
        return cur.rowcount


def stats() -> dict:
    with _lock:
        db = _db()
        total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        notes = db.execute("SELECT COUNT(*) FROM chunks WHERE kind='note'").fetchone()[0]
        facts = db.execute("SELECT COUNT(*) FROM chunks WHERE kind='fact'").fetchone()[0]
        embedded = db.execute("SELECT COUNT(*) FROM chunks WHERE vec IS NOT NULL").fetchone()[0]
    return {"total": total, "notes": notes, "facts": facts,
            "embedded": embedded, "backend": embeddings.backend_name()}
