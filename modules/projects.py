"""
modules/projects.py — let Miko map the user's projects (user-controlled).

The user points Miko at a project directory; Miko scans the structure + key
files, asks the model for a concise profile (what it is, stack, structure, entry
points, how to run), saves it as a `Projects/<name>.md` note in the vault (so it's
recall-able), and registers it in data/projects.json. Only directories the user
explicitly adds are mapped — Miko never scans on its own.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("miko.projects")

_SKIP = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build",
         ".next", "target", ".idea", ".vscode", "bin", "obj"}
_MANIFESTS = ["package.json", "requirements.txt", "pyproject.toml", "go.mod",
              "Cargo.toml", "pom.xml", "build.gradle", "Gemfile", "composer.json",
              "pubspec.yaml", "README.md", "README.txt"]

_PROFILE_SYS = (
    "You are a codebase analyst. From the folder structure and key files, write a "
    "CONCISE profile of this project for an assistant to remember: what it is/does, "
    "the tech stack, the main structure (key directories → purpose), the entry "
    "points, and how to run it. 6-12 short lines, factual, no preamble. If something "
    "is unclear, say so rather than guessing."
)

TOOL_DECLARATIONS = [
    {
        "name": "add_project",
        "description": (
            "Map a project directory so Miko understands what the user is working on. "
            "Scans the folder, builds a profile (what it is, stack, structure, how it "
            "works), saves it to the vault, and remembers it. Use when the user says "
            "'I'm working on X at <path>' or 'map this project'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Absolute path to the project directory."},
                "name": {"type": "STRING", "description": "Optional short name (defaults to the folder name)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_projects",
        "description": "List the projects Miko has mapped (name + path).",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "forget_project",
        "description": "Remove a mapped project from Miko's memory and vault.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"name": {"type": "STRING", "description": "The project name to forget."}},
            "required": ["name"],
        },
    },
]


def _registry_path() -> Path:
    from config import CONFIG
    return CONFIG.data_dir / "projects.json"


def _load() -> dict:
    p = _registry_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(reg: dict) -> None:
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "project"


def _scan(p: Path) -> str:
    """Bounded scan: top-level tree (2 levels) + key manifests/README (truncated)."""
    lines = [f"PROJECT FOLDER: {p}", "STRUCTURE (top levels):"]
    try:
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        entries = []
    for entry in entries[:40]:
        if entry.name in _SKIP:
            continue
        if entry.is_dir():
            lines.append(f"  {entry.name}/")
            try:
                subs = [c.name for c in sorted(entry.iterdir(), key=lambda e: e.name.lower())
                        if c.name not in _SKIP][:12]
                lines += [f"    {s}" for s in subs]
            except OSError:
                pass
        else:
            lines.append(f"  {entry.name}")
    for m in _MANIFESTS:
        f = p / m
        if f.is_file():
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")[:1200]
                lines.append(f"--- {m} ---\n{txt}")
            except OSError:
                pass
    return "\n".join(lines)[:8000]


def add_project(path: str, name: str = "") -> str:
    p = Path((path or "").strip()).expanduser()
    if not p.is_dir():
        return f"That's not a folder I can find: {path}"
    name = (name or "").strip() or p.name
    slug = _slug(name)

    from config import CONFIG
    try:
        from chat_backend import complete_text
        profile = complete_text(
            "gemini", "gemini-2.5-flash", api_key=getattr(CONFIG, "gemini_api_key", ""),
            system=_PROFILE_SYS, user=_scan(p), max_tokens=700,
        )
    except Exception as e:
        logger.warning(f"project profile failed: {e}")
        profile = "(could not analyze automatically)"

    try:
        import vault
        folder = vault.folder_for(CONFIG.notes_dir, "projects")
    except Exception:
        folder = Path(CONFIG.notes_dir) / "Projects"
        folder.mkdir(parents=True, exist_ok=True)
    note = folder / f"{slug}.md"
    now = datetime.now()
    note.write_text(
        "---\n"
        f"date: {now:%Y-%m-%d}\n"
        "type: project\n"
        "tags: [project]\n"
        f'name: "{name[:60]}"\n'
        f'path: "{str(p)}"\n'
        "---\n\n"
        f"# Project: {name}\n\n"
        f"**Path:** `{p}`\n\n{profile.strip()}\n",
        encoding="utf-8",
    )
    try:
        from memory import knowledge_store as KS
        KS.index_note_file(note)
    except Exception as e:
        logger.warning(f"index project note failed: {e}")

    reg = _load()
    reg[name] = {"path": str(p), "note": str(note), "mapped_at": now.isoformat()}
    _save(reg)
    return f"Mapped '{name}'. I scanned its structure and saved a profile to the vault."


def list_projects() -> str:
    reg = _load()
    if not reg:
        return "I haven't mapped any projects yet. Tell me a folder to map."
    lines = ["Projects you're working on:"]
    for name, d in reg.items():
        lines.append(f"- {name} — {d.get('path', '')}")
    return "\n".join(lines)


def forget_project(name: str) -> str:
    reg = _load()
    name = (name or "").strip()
    # case-insensitive match
    key = next((k for k in reg if k.lower() == name.lower()), None)
    if not key:
        return f"I don't have a project called '{name}'."
    entry = reg.pop(key)
    _save(reg)
    note = entry.get("note", "")
    if note:
        try:
            from memory import knowledge_store as KS
            KS.delete_prefix("note", note + "#")
        except Exception:
            pass
        try:
            import send2trash
            if Path(note).exists():
                send2trash.send2trash(note)
        except Exception:
            pass
    return f"Forgot the project '{key}'."


def get_active_projects_line(max_chars: int = 300) -> str:
    """Compact one-liner of mapped projects for prompt injection ('' if none)."""
    reg = _load()
    if not reg:
        return ""
    names = "; ".join(f"{n} ({d.get('path','')})" for n, d in reg.items())
    return ("Active projects: " + names)[:max_chars]
