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
         ".next", "out", "target", ".idea", ".vscode", "bin", "obj", ".gradle",
         ".expo", ".turbo", "coverage", ".pytest_cache", ".mypy_cache", "vendor"}
_MANIFESTS = ["package.json", "requirements.txt", "pyproject.toml", "go.mod",
              "Cargo.toml", "pom.xml", "build.gradle", "Gemfile", "composer.json",
              "pubspec.yaml", "tsconfig.json", "Dockerfile", "docker-compose.yml",
              ".env.example", "Makefile", "README.md", "README.txt"]
# Likely entry points worth excerpting so the profile (and pair mode) know where
# execution starts.
_ENTRY_CANDIDATES = [
    "main.py", "app.py", "manage.py", "__main__.py", "run.py", "wsgi.py", "asgi.py",
    "index.ts", "index.tsx", "index.js", "main.ts", "main.tsx", "main.js", "App.tsx",
    "App.js", "server.js", "server.ts", "main.go", "main.rs", "Program.cs",
    "src/index.ts", "src/index.tsx", "src/index.js", "src/main.ts", "src/main.tsx",
    "src/main.py", "src/App.tsx", "src/app.py",
]

# Deep but bounded — enough that the note captures the real shape of the repo without
# dumping node_modules-scale trees.
_TREE_MAX_DEPTH = 4
_TREE_MAX_PER_DIR = 40
_TREE_MAX_LINES = 600
_SCAN_BUDGET = 22000   # chars handed to the model
_FILE_EXCERPT = 2400

_PROFILE_SYS = (
    "You are a senior codebase analyst onboarding an AI teammate that will edit this "
    "repository. From the directory tree and key files, write a THOROUGH project "
    "profile the teammate can rely on for full context. Cover, with headings:\n"
    "- Overview: what the project is and does.\n"
    "- Tech stack: languages, frameworks, key libraries, package manager, runtime.\n"
    "- Architecture & structure: walk the important directories and say what each is "
    "for; how the pieces fit together; data flow or module boundaries if visible.\n"
    "- Key files: the files that matter most and why.\n"
    "- Entry points: where execution starts.\n"
    "- Build & run: how to install, run, test, and build.\n"
    "- Conventions & notable patterns: anything a contributor must know.\n"
    "Be factual and specific (name real files/dirs). Don't pad, but don't truncate "
    "real detail — this is reference material, not a summary. If something is unclear "
    "from the files given, say so rather than guessing."
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


def _build_tree(root: Path) -> str:
    """A real, indented directory tree (skip dirs pruned), bounded in depth/width/size
    so even a large repo yields a readable map rather than a dump."""
    lines: list = []
    truncated = False

    def walk(d: Path, prefix: str, depth: int) -> None:
        nonlocal truncated
        if depth > _TREE_MAX_DEPTH or truncated:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        entries = [e for e in entries if e.name not in _SKIP]
        shown = entries[:_TREE_MAX_PER_DIR]
        for e in shown:
            if len(lines) >= _TREE_MAX_LINES:
                truncated = True
                return
            lines.append(f"{prefix}{e.name}{'/' if e.is_dir() else ''}")
            if e.is_dir():
                walk(e, prefix + "  ", depth + 1)
        if len(entries) > len(shown):
            lines.append(f"{prefix}… (+{len(entries) - len(shown)} more)")

    walk(root, "", 1)
    if truncated:
        lines.append("… (tree truncated)")
    return "\n".join(lines)


def _key_files(p: Path) -> list:
    """(label, text) excerpts of manifests + detected entry points."""
    out: list = []
    seen: set = set()

    def add(rel: str, f: Path):
        if not f.is_file() or str(f) in seen:
            return
        seen.add(str(f))
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")[:_FILE_EXCERPT]
            out.append((rel, txt))
        except OSError:
            pass

    for m in _MANIFESTS:
        add(m, p / m)
    for rel in _ENTRY_CANDIDATES:
        add(rel, p / rel)
    return out


def _scan(p: Path, tree: str) -> str:
    """The model-facing bundle: directory tree + key file excerpts, size-bounded."""
    parts = [f"PROJECT FOLDER: {p}", "", "DIRECTORY TREE:", tree, ""]
    used = sum(len(x) for x in parts)
    for label, txt in _key_files(p):
        block = f"--- {label} ---\n{txt}\n"
        if used + len(block) > _SCAN_BUDGET:
            parts.append("… (further files omitted for size)")
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


_INDEX_NAME = "Projects"   # the MOC note that links every project (graph hub)


def _projects_folder(notes_dir) -> Path:
    try:
        import vault
        return vault.folder_for(notes_dir, "projects")
    except Exception:
        f = Path(notes_dir) / "Projects"
        f.mkdir(parents=True, exist_ok=True)
        return f


def _rebuild_index(notes_dir) -> None:
    """(Re)write Projects/Projects.md — a map-of-content that wikilinks every mapped
    project, so they form a connected cluster in Obsidian's graph instead of orphans."""
    reg = _load()
    folder = _projects_folder(notes_dir)
    idx = folder / f"{_INDEX_NAME}.md"
    lines = ["---", "type: index", "tags: [project, moc]", "---", "",
             "# Projects", "", "Projects Miko has mapped. Each links back here.", ""]
    if reg:
        for name, d in sorted(reg.items(), key=lambda kv: kv[0].lower()):
            stem = Path(d.get("note", "")).stem or _slug(name)
            lines.append(f"- [[{stem}|{name}]] — `{d.get('path', '')}`")
    else:
        lines.append("_No projects mapped yet._")
    idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        from memory import knowledge_store as KS
        KS.index_note_file(idx)
    except Exception:
        pass


def add_project(path: str, name: str = "") -> str:
    p = Path((path or "").strip()).expanduser()
    if not p.is_dir():
        return f"That's not a folder I can find: {path}"
    name = (name or "").strip() or p.name
    slug = _slug(name)

    from config import CONFIG
    tree = _build_tree(p)
    try:
        from chat_backend import complete_text
        profile = complete_text(
            "gemini", "gemini-2.5-flash", api_key=getattr(CONFIG, "gemini_api_key", ""),
            system=_PROFILE_SYS, user=_scan(p, tree), max_tokens=2200,
        )
    except Exception as e:
        logger.warning(f"project profile failed: {e}")
        profile = "(could not analyze automatically)"

    folder = _projects_folder(CONFIG.notes_dir)
    note = folder / f"{slug}.md"
    now = datetime.now()

    # Connect to genuinely related vault notes (semantic) + the Projects hub. Exclude
    # OTHER project notes + the index: every project looks alike to a semantic search
    # (they're all code repos), so they'd cross-link as "related" when their only real
    # connection is being projects — which the "Part of [[Projects]]" hub already says.
    try:
        import vault
        related = vault.related_links(f"{name}\n{profile}", exclude_path=str(note), k=6)
        proj_stems = {Path(d.get("note", "")).stem for d in _load().values()}
        proj_stems.add(_INDEX_NAME)
        related = [r for r in related if r not in proj_stems][:4]
        related_block = vault.related_section(related)
    except Exception:
        related_block = ""

    note.write_text(
        "---\n"
        f"date: {now:%Y-%m-%d}\n"
        "type: project\n"
        "tags: [project]\n"
        f'name: "{name[:60]}"\n'
        f"path: '{str(p)}'\n"          # single-quoted: a Windows path is not a YAML escape
        "---\n\n"
        f"# Project: {name}\n\n"
        f"**Path:** `{p}`\n\n"
        f"{profile.strip()}\n\n"
        "## Structure\n\n"
        "```text\n"
        f"{tree}\n"
        "```\n"
        f"{related_block}\n"
        f"Part of [[{_INDEX_NAME}]]\n",
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
    _rebuild_index(CONFIG.notes_dir)
    n_lines = tree.count("\n") + 1
    return (f"Mapped '{name}'. I walked its full structure ({n_lines} entries), wrote a "
            f"detailed profile + the directory tree to the vault, and linked it into the "
            f"Projects graph.")


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
    try:
        from config import CONFIG
        _rebuild_index(CONFIG.notes_dir)
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
