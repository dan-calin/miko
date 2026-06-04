"""
vault.py — Obsidian/PARA structure + wikilinking for Miko's notes vault.

The vault is CONFIG.notes_dir (the "Miko Notes" folder). We lay a light PARA
structure over it — Inbox / Projects / Areas / Resources / Archives / Daily —
and connect related notes with Obsidian [[wikilinks]] (links over folders, per
the "second brain" approach). Existing flat notes keep working because the note
tools search recursively (rglob).
"""

import logging
from pathlib import Path

logger = logging.getLogger("miko.vault")

# PARA + capture folders (key → on-disk folder name).
FOLDERS = {
    "inbox": "Inbox",
    "projects": "Projects",
    "areas": "Areas",
    "resources": "Resources",
    "archives": "Archives",
    "daily": "Daily",
}

# What kind of note lands where.
_KIND_FOLDER = {
    "research": "resources",
    "daily": "daily",
    "project": "projects",
    "projects": "projects",
    "capture": "inbox",
    "note": "inbox",
}

_README = """# Miko Vault

This folder is Miko's second brain — plain Markdown you can open directly in
[Obsidian](https://obsidian.md) (point a vault at this folder).

- **Inbox/** — quick captures and general notes
- **Projects/** — active, goal-driven work
- **Areas/** — ongoing responsibilities
- **Resources/** — reference material and research reports
- **Archives/** — finished or old material
- **Daily/** — daily notes and brain dumps

Notes connect with `[[wikilinks]]`. Open the graph view in Obsidian to see how
your knowledge links together.
"""


def ensure_structure(notes_dir) -> None:
    """Create the PARA folders + a README, idempotently (non-destructive)."""
    base = Path(notes_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
        for name in FOLDERS.values():
            (base / name).mkdir(exist_ok=True)
        readme = base / "README.md"
        if not readme.exists():
            readme.write_text(_README, encoding="utf-8")
    except Exception as e:
        logger.warning(f"ensure_structure failed: {e}")


def folder_for(notes_dir, kind: str) -> Path:
    """Return (and create) the subfolder a given note kind belongs in."""
    base = Path(notes_dir)
    sub = FOLDERS.get(_KIND_FOLDER.get(kind, "inbox"), "Inbox")
    p = base / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


def wikilink_name(path) -> str:
    """Obsidian wikilink target for a note path (its filename without .md)."""
    return Path(path).stem


def related_links(text: str, exclude_path: str = "", k: int = 4) -> list:
    """Find existing vault notes related to `text` via the semantic store and
    return their wikilink names (deduped by file, excluding `exclude_path`)."""
    try:
        from memory import knowledge_store as KS
        hits = KS.search((text or "")[:1000], k=k * 3, kinds=["note"])
    except Exception as e:
        logger.warning(f"related_links search failed: {e}")
        return []
    exclude = str(Path(exclude_path)).lower() if exclude_path else ""
    seen, out = set(), []
    for h in hits:
        fp = h["ref"].split("#")[0]
        low = fp.lower()
        if low == exclude or low in seen:
            continue
        seen.add(low)
        out.append(Path(fp).stem)
        if len(out) >= k:
            break
    return out


def related_section(names: list) -> str:
    """Format a '## Related' wikilink block ('' when there are no links)."""
    if not names:
        return ""
    return "\n## Related\n" + "\n".join(f"- [[{n}]]" for n in names) + "\n"
