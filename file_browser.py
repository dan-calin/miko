"""
file_browser.py — Safe file explorer + editor backend for the web Chat UI.

Lets the in-browser "Workspace" panel browse folders, open files, and save edits.
This is a self-hosted, single-user tool, so it deliberately exposes the user's own
files — but it is fenced to the home directory and the project folder, and it refuses
the same Windows/system paths Miko's voice tools refuse. No path traversal escapes the
allowed roots; binary and oversized files are refused for editing.
"""

import json
import os
from datetime import datetime
from pathlib import Path

# ── Boundaries ────────────────────────────────────────────────────────────────
_HOME = Path.home().resolve()
_PROJECT = Path(__file__).resolve().parent

# Browsing is fenced to these trees.
_ALLOWED_TREES = [_HOME, _PROJECT]

# Never expose these (case-insensitive prefix match on the resolved path).
_BLOCKED_PREFIXES = (
    "c:\\windows",
    str((_HOME / "AppData").resolve()).lower(),
)

# Quick-jump roots shown in the explorer.
def roots() -> list[dict]:
    out = []
    for name, p in [
        ("Desktop",   _HOME / "Desktop"),
        ("Documents", _HOME / "Documents"),
        ("Downloads", _HOME / "Downloads"),
        ("Miko Notes", _HOME / "Desktop" / "Miko Notes"),
        ("Home",      _HOME),
        ("Project",   _PROJECT),
    ]:
        if p.exists():
            out.append({"name": name, "path": str(p.resolve())})
    return out


_MAX_EDIT_BYTES = 1_000_000  # 1 MB — above this we open read-only / refuse edit

_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "javascript",
    ".jsx": "javascript", ".tsx": "javascript", ".json": "json", ".html": "htmlmixed",
    ".htm": "htmlmixed", ".xml": "xml", ".css": "css", ".scss": "css",
    ".md": "markdown", ".markdown": "markdown", ".sh": "shell", ".bat": "shell",
    ".ps1": "powershell", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".ini": "properties", ".cfg": "properties", ".sql": "sql", ".c": "clike",
    ".cpp": "clike", ".h": "clike", ".java": "clike", ".cs": "clike", ".go": "go",
    ".rs": "rust", ".rb": "ruby", ".php": "php",
}


def _lang(name: str) -> str:
    return _LANG_BY_EXT.get(Path(name).suffix.lower(), "")


# ── Safety ────────────────────────────────────────────────────────────────────
def _resolve(path: str) -> Path:
    """Resolve a user-supplied path and verify it is inside an allowed tree."""
    if not path or not str(path).strip():
        raise ValueError("No path given.")
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except (OSError, RuntimeError):
        raise ValueError("Invalid path.")

    low = str(p).lower()
    for bad in _BLOCKED_PREFIXES:
        if low == bad or low.startswith(bad + os.sep) or low.startswith(bad + "\\"):
            raise ValueError("That location is off-limits.")

    for tree in _ALLOWED_TREES:
        t = str(tree).lower()
        if low == t or low.startswith(t + os.sep) or low.startswith(t + "\\"):
            return p
    raise ValueError("Outside the allowed folders (home directory / project).")


def list_dir(path: str = "") -> dict:
    """List a directory's entries (folders first, then files; both alphabetical)."""
    target = _resolve(path) if path else Path(get_workspace())
    if not target.exists():
        target = _HOME
    target = _resolve(str(target))
    if target.is_file():
        target = target.parent

    dirs, files = [], []
    try:
        for entry in os.scandir(target):
            try:
                is_dir = entry.is_dir()
                st = entry.stat()
            except OSError:
                continue
            if entry.name.startswith("$"):
                continue
            item = {
                "name": entry.name,
                "path": str(Path(entry.path).resolve()),
                "is_dir": is_dir,
                "size": 0 if is_dir else st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            }
            (dirs if is_dir else files).append(item)
    except PermissionError:
        raise ValueError("No permission to read that folder.")

    dirs.sort(key=lambda e: e["name"].lower())
    files.sort(key=lambda e: e["name"].lower())

    parent = None
    try:
        cand = target.parent
        if cand != target:
            _resolve(str(cand))   # only offer "up" if parent is still in-bounds
            parent = str(cand)
    except ValueError:
        parent = None

    return {"path": str(target), "parent": parent, "entries": dirs + files}


def read_file(path: str) -> dict:
    """Read a text file for the editor. Refuses binary; truncates very large files."""
    p = _resolve(path)
    if not p.exists() or not p.is_file():
        raise ValueError("File not found.")
    size = p.stat().st_size

    raw = p.read_bytes()
    if b"\x00" in raw[:8192]:
        return {"path": str(p), "name": p.name, "binary": True,
                "content": "", "language": "", "size": size, "editable": False}

    too_big = size > _MAX_EDIT_BYTES
    text = raw[:_MAX_EDIT_BYTES].decode("utf-8", errors="replace") if too_big \
        else raw.decode("utf-8", errors="replace")

    return {
        "path": str(p), "name": p.name, "binary": False,
        "content": text, "language": _lang(p.name), "size": size,
        "editable": not too_big, "truncated": too_big,
    }


def write_file(path: str, content: str) -> dict:
    """Save edits back to a file (must already resolve to an allowed location)."""
    p = _resolve(path)
    if p.is_dir():
        raise ValueError("That path is a folder.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content if isinstance(content, str) else "", encoding="utf-8")
    return {"ok": True, "path": str(p), "size": p.stat().st_size}


# ── Active workspace ──────────────────────────────────────────────────────────
# The folder the user has chosen to "work in" right now. Drives the explorer's
# default location, is injected into the chat system prompt, and is exported as
# MIKO_WORKSPACE so run_command executes there. Persisted so it survives restarts.
_STATE_PATH = _PROJECT / "data" / "workspace.json"


def get_workspace() -> str:
    """Return the active workspace folder (validated), defaulting to the project dir."""
    try:
        if _STATE_PATH.exists():
            saved = json.loads(_STATE_PATH.read_text(encoding="utf-8")).get("workspace", "")
            if saved:
                try:
                    p = _resolve(saved)
                    if p.is_dir():
                        os.environ.setdefault("MIKO_WORKSPACE", str(p))
                        return str(p)
                except ValueError:
                    pass
    except Exception:
        pass
    return str(_PROJECT)


def set_workspace(path: str) -> dict:
    """Set the active workspace to a folder (must be inside the allowed trees)."""
    p = _resolve(path)
    if not p.is_dir():
        raise ValueError("Workspace must be an existing folder.")
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps({"workspace": str(p)}), encoding="utf-8")
    os.environ["MIKO_WORKSPACE"] = str(p)
    return {"ok": True, "workspace": str(p)}


# Make the saved workspace active on import (sets MIKO_WORKSPACE for run_command).
get_workspace()
