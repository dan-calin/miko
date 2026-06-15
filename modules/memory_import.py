"""
modules/memory_import.py — import a user's memory from another AI into Miko.

The user exports their memory from another assistant (e.g. Google Takeout for
Gemini, ChatGPT's "Export data", a Claude memory dump, or just pasted text) and
Miko absorbs it so it starts useful instead of learning from scratch.

Pipeline:
  1. extract_text()  — pull readable text out of a paste or an export file
                       (.zip / .json / .html / .txt / .md). Format-agnostic.
  2. normalize()     — one LLM pass maps the raw text into Miko's own schema:
                       structured facts (identity / preferences / relationships /
                       notes) + a list of free-form durable memories.
  3. commit()        — merge facts into long_term.json (same reconcile semantics
                       as live learning, so they overwrite cleanly), re-index the
                       fact vectors, and write a provenance note into the vault
                       that gets semantically indexed so recall can surface it.

The UI runs preview (1+2) → user reviews/deselects → commit (3). The
`import_memories` tool runs the whole thing from a file path in one shot for the
chat / voice agent and reports exactly what it added.
"""

import html
import io
import json
import logging
import re
import zipfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("miko.memory_import")

_CATEGORIES = ("identity", "preferences", "relationships", "notes")
_TEXT_EXTS = {".txt", ".md", ".json", ".html", ".htm", ".csv"}
_INPUT_BUDGET = 80000      # chars of extracted text handed to the model
_PER_FILE_CAP = 300000     # don't read absurdly large single entries
_DIR_TOTAL_BUDGET = 2000000  # cap when walking an extracted Takeout folder
_DIR_MAX_FILES = 400
# A Takeout export is a long chat history; durable facts are scattered through it,
# so sample across the whole text in several passes rather than reading only the top.
_PASS_CHARS = 80000        # chars analysed per pass
_MAX_PASSES = 8            # cap LLM calls for one import

TOOL_DECLARATIONS = [
    {
        "name": "import_memories",
        "description": (
            "Import the user's memory exported from ANOTHER AI assistant (e.g. Gemini "
            "via Google Takeout, ChatGPT's data export, a saved memory dump) so Miko "
            "absorbs what that assistant knew about the user. Give a file path (a "
            ".zip / .json / .html / .txt / .md export) OR paste the memory text "
            "directly. Miko extracts durable facts + memories, merges them into its "
            "long-term memory, and reports what it added. Use when the user says "
            "things like 'import my Gemini memories' or 'here's my ChatGPT memory, "
            "remember this'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Absolute path to the exported memory file."},
                "text": {"type": "STRING", "description": "Memory text pasted directly (use instead of path)."},
                "source": {"type": "STRING", "description": "Optional label for where it came from, e.g. 'Gemini' or 'ChatGPT'."},
            },
        },
    },
]


# ── 1. Extraction ───────────────────────────────────────────────────────────

def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _flatten_json(obj, out: list, depth: int = 0) -> None:
    """Pull human-readable strings out of arbitrary exported JSON."""
    if depth > 12 or len(out) > 4000:
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _flatten_json(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_json(v, out, depth + 1)
    elif isinstance(obj, str):
        s = obj.strip()
        if len(s) >= 3:
            out.append(s)


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _text_from_bytes(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    raw = _decode(data[:_PER_FILE_CAP])
    if ext == ".json":
        try:
            parsed = json.loads(raw)
            out: list = []
            _flatten_json(parsed, out)
            return "\n".join(out)
        except Exception:
            return raw
    if ext in (".html", ".htm"):
        return _strip_html(raw)
    return raw


def _text_from_zip(z: zipfile.ZipFile) -> str:
    chunks: list = []
    for info in z.infolist():
        if info.is_dir() or Path(info.filename).suffix.lower() not in _TEXT_EXTS:
            continue
        try:
            chunks.append(f"=== {info.filename} ===")
            chunks.append(_text_from_bytes(info.filename, z.read(info)))
        except Exception:
            continue
    return "\n\n".join(c for c in chunks if c.strip())


def extract_text(filename: str, data: bytes) -> str:
    """Best-effort plain text from an export file's bytes (paste callers pass text
    straight through). Walks a Takeout-style .zip, reading only text-bearing entries."""
    if not data:
        return ""
    ext = Path(filename or "").suffix.lower()
    if ext == ".zip" or data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return _text_from_zip(z)
        except zipfile.BadZipFile:
            return _decode(data)
    return _text_from_bytes(filename or "memory.txt", data)


def _text_from_dir(root: Path) -> str:
    """Walk an extracted Takeout-style folder and concatenate its text-bearing files
    (.html / .json / .txt / .md / .csv), bounded in count and total size."""
    chunks: list = []
    used = files = 0
    for f in sorted(root.rglob("*")):
        if files >= _DIR_MAX_FILES or used >= _DIR_TOTAL_BUDGET:
            break
        if not f.is_file() or f.suffix.lower() not in _TEXT_EXTS:
            continue
        try:
            piece = _text_from_bytes(f.name, f.read_bytes())
        except Exception:
            continue
        if not piece.strip():
            continue
        rel = f.relative_to(root)
        block = f"=== {rel} ===\n{piece}"
        chunks.append(block)
        used += len(block)
        files += 1
    return "\n\n".join(chunks)


def extract_from_path(path: str) -> str:
    """Extract text from a file OR folder on disk. A .zip streams its entries; a
    folder (an already-extracted Google Takeout) is walked recursively — both read
    straight from disk so a large export never has to go through the browser."""
    p = Path(str(path).strip().strip('"')).expanduser()
    if p.is_dir():
        return _text_from_dir(p)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    if p.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(p) as z:
                return _text_from_zip(z)
        except zipfile.BadZipFile:
            pass
    return _text_from_bytes(p.name, p.read_bytes())


# ── 2. Normalization (LLM) ───────────────────────────────────────────────────

_NORMALIZE_SYS = (
    "You import a user's memory exported from another AI assistant into a new "
    "assistant. From the raw export text, extract everything DURABLE and TRUE about "
    "the user that the new assistant should remember. Ignore one-off chatter, "
    "timestamps, system noise, deleted/contradicted items, and anything about the old "
    "assistant itself.\n"
    "Return ONLY JSON, no prose:\n"
    '{"facts": [{"category": "identity|preferences|relationships|notes", '
    '"key": "short_snake_case", "value": "the fact"}], '
    '"notes": ["a durable memory that does not fit a single key, in one sentence"]}\n'
    "Rules:\n"
    "- category 'identity' = name, age, location, job, etc.; 'preferences' = likes, "
    "style, tools, habits; 'relationships' = people/pets and who they are; 'notes' = "
    "durable facts that are none of the above.\n"
    "- Keep values concise (under ~200 chars) and self-contained.\n"
    "- Put a memory in 'notes' (the list) only when it is durable but does not map to "
    "a clean key/value fact.\n"
    "- Deduplicate. If the export is empty of durable info, return "
    '{"facts": [], "notes": []}.'
)


def _windows(text: str) -> list:
    """Slices of `text` to analyse. Short text → one pass. Longer text is split into
    contiguous windows; very long text is sampled with evenly-spaced windows so the
    whole history is covered rather than just the front."""
    n = len(text)
    if n <= _PASS_CHARS:
        return [text]
    full = -(-n // _PASS_CHARS)   # ceil
    if full <= _MAX_PASSES:
        return [text[i * _PASS_CHARS:(i + 1) * _PASS_CHARS] for i in range(full)]
    step = (n - _PASS_CHARS) / (_MAX_PASSES - 1)
    return [text[int(i * step):int(i * step) + _PASS_CHARS] for i in range(_MAX_PASSES)]


def _normalize_chunk(chunk: str, complete_text, gemini_key: str) -> tuple:
    raw = complete_text(
        "gemini", "gemini-2.5-flash", api_key=gemini_key,
        system=_NORMALIZE_SYS, user=chunk, max_tokens=2200,
    )
    data = _parse_json_obj(raw)
    facts = data.get("facts") if isinstance(data.get("facts"), list) else []
    notes = [str(n).strip() for n in (data.get("notes") or []) if str(n).strip()]
    return facts, notes


def _dedup_notes(notes: list) -> list:
    out, seen = [], set()
    for n in notes:
        key = re.sub(r"\s+", " ", n.lower()).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(n)
    return out[:80]


def normalize(raw_text: str, source: str = "") -> dict:
    """raw_text → {facts, notes, source, chars, passes}. Samples across the whole text
    in several passes (a long Takeout history isn't all near the top), then merges and
    deduplicates. No write."""
    text = (raw_text or "").strip()
    if not text:
        return {"facts": [], "notes": [], "source": source, "chars": 0, "passes": 0}
    from config import CONFIG
    from chat_backend import complete_text
    gemini_key = getattr(CONFIG, "gemini_api_key", "")

    windows = _windows(text)
    all_facts, all_notes = [], []
    passes = 0
    for w in windows:
        try:
            f, n = _normalize_chunk(w, complete_text, gemini_key)
        except Exception as e:
            logger.warning(f"import pass failed: {e}")
            continue
        all_facts.extend(f)
        all_notes.extend(n)
        passes += 1

    return {"facts": _clean_facts(all_facts), "notes": _dedup_notes(all_notes),
            "source": (source or "").strip(), "chars": len(text), "passes": passes}


def _parse_json_obj(raw: str) -> dict:
    raw = re.sub(r"```(?:json)?", "", raw or "").strip().rstrip("`").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _slug_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")[:48]


def _clean_facts(facts) -> list:
    out, seen = [], set()
    for f in (facts or []):
        if not isinstance(f, dict):
            continue
        cat = str(f.get("category", "")).strip().lower()
        key = _slug_key(f.get("key", ""))
        val = str(f.get("value", "")).strip()[:280]
        if cat not in _CATEGORIES or not key or not val:
            continue
        sig = (cat, key)
        if sig in seen:
            continue
        seen.add(sig)
        out.append({"category": cat, "key": key, "value": val})
    return out[:120]


# ── 3. Commit (write into Miko's memory) ──────────────────────────────────────

def commit(facts: list, notes: list, source: str = "") -> dict:
    """Merge reviewed facts into long_term.json, re-index fact vectors, and write +
    index a provenance note holding the free-form memories. Returns counts."""
    from config import CONFIG
    facts = _clean_facts(facts)
    notes = [str(n).strip() for n in (notes or []) if str(n).strip()]
    src = (source or "").strip() or "another assistant"

    fact_count = 0
    if facts:
        norm: dict = {}
        for f in facts:
            norm.setdefault(f["category"], {})[f["key"]] = {"value": f["value"]}
        from memory.memory_manager import update_memory, load_memory
        update_memory(CONFIG.memory_file, norm)
        fact_count = sum(len(v) for v in norm.values())
        try:
            from memory import knowledge_store as KS
            KS.index_facts(load_memory(CONFIG.memory_file))
        except Exception as e:
            logger.warning(f"fact reindex after import failed: {e}")

    note_path = ""
    if notes or facts:
        note_path = _write_note(CONFIG.notes_dir, src, facts, notes)
        _rebuild_memory_index(CONFIG.notes_dir)

    return {"facts": fact_count, "notes": len(notes), "source": src, "note": note_path}


_MEMORY_INDEX = "Memory"   # the MOC hub that connects every memory note in the graph


def _memory_index_path(notes_dir) -> Path:
    return Path(notes_dir) / f"{_MEMORY_INDEX}.md"


def _note_source(note: Path) -> str:
    """Read an imported-memory note's source label (frontmatter, else the H1, else slug)."""
    try:
        head = note.read_text(encoding="utf-8", errors="replace")[:600]
    except OSError:
        head = ""
    m = re.search(r'(?m)^source:\s*"?([^"\n]+)"?', head)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?m)^#\s*Imported memory from\s+(.+)$", head)
    if m:
        return m.group(1).strip()
    return note.stem.replace("imported-memory-", "").replace("-", " ")


def _rebuild_memory_index(notes_dir) -> None:
    """(Re)write Memory.md — a map-of-content that wikilinks every memory note, so
    they form one connected 'Memory' cluster in Obsidian's graph instead of orphans."""
    base = Path(notes_dir)
    idx = _memory_index_path(notes_dir)
    notes = sorted(p for p in base.rglob("imported-memory-*.md") if p.resolve() != idx.resolve())
    lines = ["---", "type: index", "tags: [memory, moc]", "---", "",
             "# Memory", "",
             "Miko's memory hub. Everything Miko remembers about you links back here.", ""]
    if notes:
        lines.append("## Imported memory")
        lines.append("")
        for n in notes:
            lines.append(f"- [[{n.stem}|{_note_source(n)}]]")
    else:
        lines.append("_No memory notes yet._")
    idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        from memory import knowledge_store as KS
        KS.index_note_file(idx)
    except Exception as e:
        logger.warning(f"index Memory hub failed: {e}")


def _imported_folder(notes_dir) -> Path:
    """Imported memory is reference material → the Resources PARA folder."""
    try:
        import vault
        sub = vault.FOLDERS.get("resources", "Resources")
    except Exception:
        sub = "Resources"
    f = Path(notes_dir) / sub
    f.mkdir(parents=True, exist_ok=True)
    return f


def _write_note(notes_dir, source: str, facts: list, notes: list) -> str:
    folder = _imported_folder(notes_dir)
    now = datetime.now()
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-") or "ai"
    note = folder / f"imported-memory-{slug}.md"
    lines = ["---", f"date: {now:%Y-%m-%d}", "type: imported-memory", "tags: [memory, imported]",
             f'source: "{source[:60]}"', "---", "",
             f"# Imported memory from {source}", "",
             f"Imported {now:%Y-%m-%d %H:%M}. {len(facts)} fact(s), {len(notes)} note(s).", ""]
    if facts:
        lines.append("## Facts")
        lines.append("")
        for f in facts:
            lines.append(f"- **{f['key'].replace('_', ' ')}** ({f['category']}): {f['value']}")
        lines.append("")
    if notes:
        lines.append("## Memories")
        lines.append("")
        for n in notes:
            lines.append(f"- {n}")
        lines.append("")
    lines.append(f"Part of [[{_MEMORY_INDEX}]]")
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        from memory import knowledge_store as KS
        KS.index_note_file(note)
    except Exception as e:
        logger.warning(f"index imported-memory note failed: {e}")
    return str(note)


# ── Tool entry (chat / voice — one shot) ──────────────────────────────────────

def import_memories(path: str = "", text: str = "", source: str = "") -> str:
    raw = (text or "").strip()
    src = (source or "").strip()
    if not raw and path:
        p = Path(path.strip().strip('"')).expanduser()
        if not p.exists():
            return f"I can't find anything at: {path}"
        try:
            raw = extract_from_path(str(p))
        except Exception as e:
            return f"I couldn't read that export: {e}"
        if not src:
            src = p.stem or p.name
    if not raw.strip():
        return "There was no readable memory in that export."

    try:
        res = normalize(raw, src)
    except Exception as e:
        return f"I couldn't analyze the export: {e}"
    facts, notes = res["facts"], res["notes"]
    if not facts and not notes:
        return "I read the export but found nothing durable worth remembering."

    out = commit(facts, notes, res["source"] or src)
    bits = []
    if out["facts"]:
        bits.append(f"{out['facts']} fact(s)")
    if out["notes"]:
        bits.append(f"{out['notes']} memory note(s)")
    return (f"Imported {' and '.join(bits)} from {out['source']} into my long-term "
            f"memory. Ask me to recall any of it, or tell me to forget something if it's wrong.")
