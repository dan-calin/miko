"""
modules/notes.py — Markdown note management.
Notes are stored in the Miko Notes folder on Desktop (or MIKO_NOTES_DIR).
Each note has YAML frontmatter with date, time, and auto-extracted tags.
"""

import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("miko.notes")

TOOL_DECLARATIONS = [
    {
        "name": "create_note",
        "description": (
            "Creează o notiță nouă sau adaugă la una existentă. "
            "Folosește pentru 'notează că', 'scrie', 'ține minte că'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "content": {
                    "type": "STRING",
                    "description": "Conținutul notiței.",
                },
                "title": {
                    "type": "STRING",
                    "description": "Titlul opțional al notiței. Dacă nu e dat, se generează din conținut.",
                },
                "append": {
                    "type": "BOOLEAN",
                    "description": "Dacă True, adaugă la o notiță existentă cu același titlu sau din aceeași zi.",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "read_note",
        "description": "Citește conținutul unei notițe după titlu sau dată.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {
                    "type": "STRING",
                    "description": "Titlul sau data notiței (ex: 'azi', '2024-01-15', 'întâlnire').",
                }
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_notes",
        "description": "Listează notițele recente disponibile.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "search_notes",
        "description": "Caută în toate notițele după cuvinte cheie.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Cuvintele cheie de căutat.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "delete_note",
        "description": "Șterge o notiță după titlu. Necesită confirmare voce.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING", "description": "Titlul notiței de șters."}
            },
            "required": ["title"],
        },
    },
]

# Windows-invalid filename chars
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')

# Common Romanian stop words for tag extraction
_STOP_WORDS = {
    "și", "sau", "dar", "că", "ca", "în", "la", "de", "pe", "cu", "se",
    "am", "ai", "are", "că", "din", "până", "după", "care", "este",
    "mai", "nu", "o", "un", "al", "al", "cel", "ei", "el", "ea",
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "and",
}


class NotesManager:
    def __init__(self, notes_dir: Path):
        self._dir = notes_dir
        self._lock = threading.Lock()
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            import vault
            vault.ensure_structure(notes_dir)   # PARA folders (Inbox/Projects/…)
        except Exception:
            pass

    # ── Public API ────────────────────────────────────────────────────────────

    def create_note(
        self, content: str, title: Optional[str] = None, append: bool = False
    ) -> str:
        if not content.strip():
            return "Conținutul notiței este gol, sefu."

        now       = datetime.now()
        date_str  = now.strftime("%Y-%m-%d")
        time_str  = now.strftime("%H:%M")
        tags      = self._extract_tags(content)
        slug      = self._make_slug(title or content)
        filename  = f"{date_str}_{slug}.md"
        try:
            import vault
            filepath = vault.folder_for(self._dir, "capture") / filename   # → Inbox/
        except Exception:
            filepath = self._dir / filename

        # If append=True and file exists for today, append to it
        if append:
            existing = self._find_today_note(title)
            if existing:
                filepath = existing

        frontmatter = (
            f"---\n"
            f"date: {date_str}\n"
            f"time: {time_str}\n"
            f"tags: [{', '.join(tags)}]\n"
            f"---\n\n"
        )

        with self._lock:
            if filepath.exists() and append:
                current = filepath.read_text(encoding="utf-8")
                filepath.write_text(
                    current + f"\n---\n\n**{time_str}** — {content}\n",
                    encoding="utf-8",
                )
                logger.info(f"Appended to note: {filepath.name}")
                _index_note(filepath)
                return f"Am adăugat la notița '{filepath.stem}', sefu."
            else:
                body = frontmatter
                if title:
                    body += f"# {title}\n\n"
                body += content + "\n"
                filepath.write_text(body, encoding="utf-8")
                logger.info(f"Created note: {filepath.name}")
                _index_note(filepath)
                return f"Am creat notița '{filename}', sefu."

    def read_note(self, title: str) -> str:
        if not title:
            return "Spune-mi ce notiță vrei să citesc, sefu."

        # Handle "azi" / "today"
        if title.lower() in ("azi", "today", "astazi", "astăzi"):
            today_str = datetime.now().strftime("%Y-%m-%d")
            matches = sorted(self._dir.rglob(f"{today_str}_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        else:
            # Fuzzy match by filename or content
            matches = self._fuzzy_find(title)

        if not matches:
            return f"Nu am găsit nicio notiță pentru '{title}', sefu."

        filepath = matches[0]
        try:
            text = filepath.read_text(encoding="utf-8")
            # Strip frontmatter for reading
            if text.startswith("---"):
                _, _, body = text.split("---", 2)
                text = body.strip()
            # Truncate long notes
            if len(text) > 1000:
                text = text[:1000] + "\n… (notiță mai lungă, am citit primele 1000 de caractere)"
            return f"Notița '{filepath.stem}':\n\n{text}"
        except Exception as e:
            return f"N-am putut citi notița: {e}"

    def list_notes(self, limit: int = 10) -> str:
        notes = sorted(self._dir.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not notes:
            return "Nu ai nicio notiță salvată momentan, sefu."
        lines = ["Notițele tale recente:"]
        for i, p in enumerate(notes[:limit], 1):
            stat = p.stat()
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M")
            lines.append(f"{i}. {p.stem} ({modified})")
        if len(notes) > limit:
            lines.append(f"… și încă {len(notes) - limit} notițe.")
        return "\n".join(lines)

    def search_notes(self, query: str) -> str:
        if not query.strip():
            return "Spune-mi ce caut în notițe, sefu."
        query_lower = query.lower()
        matches = []
        for p in self._dir.rglob("*.md"):
            try:
                content = p.read_text(encoding="utf-8").lower()
                if query_lower in content:
                    matches.append(p)
            except Exception:
                continue
        if not matches:
            return f"Nu am găsit nimic despre '{query}' în notițele tale, sefu."
        lines = [f"Am găsit {len(matches)} notiță/notițe despre '{query}':"]
        for p in sorted(matches, key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
            lines.append(f"- {p.stem}")
        return "\n".join(lines)

    def delete_note(self, title: str) -> str:
        matches = self._fuzzy_find(title)
        if not matches:
            return f"Nu am găsit nicio notiță cu titlul '{title}', sefu."
        filepath = matches[0]
        try:
            import send2trash
            send2trash.send2trash(str(filepath))
            return f"Am trimis notița '{filepath.stem}' la coșul de gunoi, sefu."
        except Exception as e:
            return f"N-am putut șterge notița: {e}"

    def open_folder(self) -> str:
        try:
            import os
            os.startfile(str(self._dir))
            return "Am deschis folderul cu notițe, sefu."
        except Exception as e:
            return f"N-am putut deschide folderul: {e}"

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_slug(self, text: str, max_len: int = 40) -> str:
        """Convert text to a Windows-safe filename slug."""
        text  = _INVALID_CHARS.sub("", text.lower())
        text  = re.sub(r"\s+", "-", text.strip())
        text  = re.sub(r"-+", "-", text)
        return text[:max_len].rstrip("-") or "notiță"

    def _extract_tags(self, text: str, max_tags: int = 5) -> list[str]:
        """Extract the most frequent meaningful words as tags."""
        words = re.findall(r"\b[a-zA-ZăâîșțĂÂÎȘȚ]{4,}\b", text.lower())
        freq: dict[str, int] = {}
        for w in words:
            if w not in _STOP_WORDS:
                freq[w] = freq.get(w, 0) + 1
        sorted_words = sorted(freq, key=lambda w: freq[w], reverse=True)
        return sorted_words[:max_tags]

    def _fuzzy_find(self, query: str) -> list[Path]:
        """Find notes whose filename or content partially matches query."""
        query_lower = query.lower()
        results = []
        for p in self._dir.rglob("*.md"):
            if query_lower in p.stem.lower():
                results.append((0, p))  # Filename match — highest priority
                continue
            try:
                content = p.read_text(encoding="utf-8").lower()
                if query_lower in content:
                    results.append((1, p))
            except Exception:
                continue
        results.sort(key=lambda x: (x[0], -x[1].stat().st_mtime))
        return [p for _, p in results]

    def _find_today_note(self, title: Optional[str]) -> Optional[Path]:
        """Find a note from today, optionally matching title."""
        today = datetime.now().strftime("%Y-%m-%d")
        candidates = sorted(
            self._dir.rglob(f"{today}_*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None
        if title:
            slug = self._make_slug(title)
            for p in candidates:
                if slug in p.stem:
                    return p
        return candidates[0]


# ── Module-level singleton factory ────────────────────────────────────────────

def _index_note(path: Path) -> None:
    """Add a freshly written note to the semantic index so recall finds it now."""
    try:
        from memory import knowledge_store as KS
        KS.index_note_file(path)
    except Exception:
        pass


_manager_instance: Optional[NotesManager] = None
_manager_lock = threading.Lock()


def _notes_manager() -> NotesManager:
    global _manager_instance
    if _manager_instance is None:
        with _manager_lock:
            if _manager_instance is None:
                from config import CONFIG
                _manager_instance = NotesManager(CONFIG.notes_dir)
    return _manager_instance
