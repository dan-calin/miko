"""
modules/file_indexer.py — SQLite-based filesystem index for fast file search.
Indexes user directories on first launch, then incrementally every 30 minutes.
Uses WAL journal mode for crash safety.
"""

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("miko.indexer")

TOOL_DECLARATIONS = [
    {
        "name": "find_file",
        "description": (
            "Caută un fișier pe disc după nume sau extensie. "
            "Folosește pentru 'unde e fișierul X', 'găsește documentul Y', 'caută PDF-urile mele'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {
                    "type": "STRING",
                    "description": "Numele parțial sau complet al fișierului.",
                },
                "extension": {
                    "type": "STRING",
                    "description": "Extensia fișierului (ex: .pdf, .py, .docx, .exe).",
                },
                "path_filter": {
                    "type": "STRING",
                    "description": "Restrânge căutarea la un anumit director (ex: Desktop, Documents).",
                },
            },
        },
    },
    {
        "name": "rebuild_file_index",
        "description": "Reconstruiește complet indexul de fișiere al sistemului. Durează câteva minute.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
]

# Directories to index — user personal dirs only, not system dirs
def _get_index_roots() -> list[Path]:
    home = Path.home()
    roots = [
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "Pictures",
        home / "Videos",
        home / "Music",
        home,
    ]
    # Add Program Files for .exe discovery (apps)
    for pf in (r"C:\Program Files", r"C:\Program Files (x86)"):
        p = Path(pf)
        if p.exists():
            roots.append(p)
    return [r for r in roots if r.exists()]


# Directory names to skip entirely
SKIP_DIRS: frozenset = frozenset({
    ".git",
    "__pycache__",
    "node_modules",
    ".obsidian",
    "$RECYCLE.BIN",
    "System Volume Information",
    "Windows",
    "WinSxS",
    "Temp",
    "temp",
})

# How often to run incremental updates (seconds)
UPDATE_INTERVAL = 1800  # 30 minutes


class FileIndexer:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock    = threading.Lock()
        self._started = False
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Called once at startup. Checks if DB is new/empty → runs full index.
        Starts periodic updater daemon thread.
        """
        if self._started:
            return
        self._started = True

        self._init_schema()

        count = self._count_files()
        if count == 0:
            logger.info("Empty index — starting full index in background...")
            threading.Thread(
                target=self._full_index,
                daemon=True,
                name="FileIndexer-Full",
            ).start()
        else:
            logger.info(f"File index loaded: {count} files")

        threading.Thread(
            target=self._periodic_updater,
            daemon=True,
            name="FileIndexer-Periodic",
        ).start()

    def find_file(
        self,
        name: str = "",
        extension: str = "",
        path_filter: str = "",
    ) -> str:
        """SQL LIKE query against the index. Falls back to live walk if index empty."""
        if not name and not extension:
            return "Spune-mi ce fișier cauți — nume sau extensie, sefu."

        conn = self._connect()
        try:
            clauses   = []
            params: list = []

            if name:
                clauses.append("LOWER(name) LIKE ?")
                params.append(f"%{name.lower()}%")
            if extension:
                ext = extension if extension.startswith(".") else f".{extension}"
                clauses.append("LOWER(extension) = ?")
                params.append(ext.lower())
            if path_filter:
                clauses.append("LOWER(path) LIKE ?")
                params.append(f"%{path_filter.lower()}%")

            where = " AND ".join(clauses) if clauses else "1=1"
            sql   = f"SELECT path, name, extension, size FROM files WHERE {where} LIMIT 15"

            with self._lock:
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            # Fallback to live search if index is empty
            if self._count_files() == 0:
                return self._live_search(name, extension)
            return f"Nu am găsit niciun fișier cu criteriile date, sefu."

        lines = [f"Am găsit {len(rows)} fișier/fișiere:\n"]
        for path, fname, ext, size in rows:
            size_str = _fmt_size(size) if size else ""
            lines.append(f"• {fname} — {path}" + (f" ({size_str})" if size_str else ""))

        return "\n".join(lines)

    def rebuild_index(self) -> str:
        """Triggers a full re-index in the background."""
        threading.Thread(
            target=self._full_index,
            daemon=True,
            name="FileIndexer-Rebuild",
        ).start()
        return "Am pornit reconstruirea indexului de fișiere în fundal, sefu. Poate dura câteva minute."

    # ── SQLite helpers ────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")  # Crash-safe
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    id        INTEGER PRIMARY KEY,
                    path      TEXT    UNIQUE NOT NULL,
                    name      TEXT    NOT NULL,
                    extension TEXT,
                    size      INTEGER,
                    modified  REAL,
                    indexed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_name ON files(name);
                CREATE INDEX IF NOT EXISTS idx_ext  ON files(extension);
            """)
            conn.commit()
        finally:
            conn.close()

    def _count_files(self) -> int:
        conn = self._connect()
        try:
            with self._lock:
                (count,) = conn.execute("SELECT COUNT(*) FROM files").fetchone()
            return count
        finally:
            conn.close()

    # ── Indexing ──────────────────────────────────────────────────────────────

    def _full_index(self) -> None:
        logger.info("Starting full filesystem index...")
        start   = time.time()
        total   = 0
        roots   = _get_index_roots()
        conn    = self._connect()

        try:
            for root in roots:
                indexed = self._index_directory(root, conn)
                total  += indexed
                logger.info(f"Indexed {indexed} files in {root}")
            conn.commit()
        except Exception as e:
            logger.error(f"Full index error: {e}")
            conn.rollback()
        finally:
            conn.close()

        elapsed = time.time() - start
        logger.info(f"Full index complete: {total} files in {elapsed:.1f}s")

    def _index_directory(self, root: Path, conn: sqlite3.Connection) -> int:
        count   = 0
        now     = time.time()

        try:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                # Prune skipped directories in-place so os.walk doesn't descend
                dirnames[:] = [
                    d for d in dirnames
                    if d not in SKIP_DIRS and not d.startswith(".")
                ]

                for fname in filenames:
                    try:
                        fpath = Path(dirpath) / fname
                        stat  = fpath.stat()
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO files
                                (path, name, extension, size, modified, indexed_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(fpath),
                                fname,
                                fpath.suffix.lower(),
                                stat.st_size,
                                stat.st_mtime,
                                now,
                            ),
                        )
                        count += 1
                        # Commit in batches to avoid holding a huge transaction
                        if count % 1000 == 0:
                            conn.commit()
                    except (OSError, PermissionError):
                        pass
        except (OSError, PermissionError) as e:
            logger.debug(f"Walk error in {root}: {e}")

        return count

    def _incremental_update(self) -> None:
        """Re-index directories where any file modification time changed."""
        logger.info("Running incremental file index update...")
        conn  = self._connect()
        total = 0
        try:
            for root in _get_index_roots():
                total += self._index_directory(root, conn)
            conn.commit()
            logger.info(f"Incremental update: {total} files processed")
        except Exception as e:
            logger.error(f"Incremental update error: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _periodic_updater(self) -> None:
        while True:
            time.sleep(UPDATE_INTERVAL)
            try:
                self._incremental_update()
            except Exception as e:
                logger.error(f"Periodic updater error: {e}")

    def _live_search(self, name: str, extension: str) -> str:
        """Fallback: live os.walk search when index is empty."""
        results = []
        for root in _get_index_roots():
            try:
                for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    for fname in filenames:
                        if name and name.lower() not in fname.lower():
                            continue
                        if extension:
                            ext = extension if extension.startswith(".") else f".{extension}"
                            if not fname.lower().endswith(ext.lower()):
                                continue
                        results.append(str(Path(dirpath) / fname))
                        if len(results) >= 10:
                            break
                    if len(results) >= 10:
                        break
            except (OSError, PermissionError):
                pass
        if not results:
            return "Nu am găsit nimic (indexul este gol — în curs de construire)."
        return "Am găsit:\n" + "\n".join(f"• {r}" for r in results)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ── Module-level singleton ────────────────────────────────────────────────────

_indexer_instance: Optional[FileIndexer] = None
_indexer_lock = threading.Lock()


def _indexer() -> FileIndexer:
    global _indexer_instance
    if _indexer_instance is None:
        with _indexer_lock:
            if _indexer_instance is None:
                from config import CONFIG
                _indexer_instance = FileIndexer(CONFIG.db_path)
    return _indexer_instance
