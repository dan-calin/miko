"""
conversation_store.py — Persistent, resumable chat conversations for the web UI.

Each conversation is one JSON file under data/conversations/<id>.json holding the
full transcript (user + assistant messages, plus the tools/files for display).
This replaces the old in-memory history dict, so chats survive restarts and the
user can switch between and continue earlier conversations.
"""

import json
import threading
from datetime import datetime
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "data" / "conversations"
_lock = threading.RLock()
_MAX_MODEL_HISTORY = 30   # neutral messages handed to the model per turn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_id(cid: str) -> str:
    """Sanitise a conversation id so it can never escape the conversations dir."""
    cid = (cid or "").strip()
    out = "".join(c for c in cid if c.isalnum() or c in "-_")
    return out[:64] or "default"


def _path(cid: str) -> Path:
    return _DIR / f"{_safe_id(cid)}.json"


def _title_from(text: str) -> str:
    t = " ".join((text or "").split())
    return (t[:48] + "…") if len(t) > 48 else (t or "New conversation")


def _load(cid: str) -> dict | None:
    p = _path(cid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write(conv: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    _path(conv["id"]).write_text(
        json.dumps(conv, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# ── Public API ────────────────────────────────────────────────────────────────
def append_turn(cid: str, user_msg: str, assistant_msg: str,
                tools: list | None = None, files: list | None = None) -> None:
    """Record one user→assistant exchange, creating the conversation if needed."""
    with _lock:
        conv = _load(cid) or {
            "id": _safe_id(cid), "title": "", "created": _now(), "messages": [],
        }
        if not conv.get("title"):
            conv["title"] = _title_from(user_msg)
        conv["messages"].append({"role": "user", "content": user_msg, "ts": _now()})
        conv["messages"].append({
            "role": "assistant", "content": assistant_msg, "ts": _now(),
            "tools": tools or [], "files": files or [],
        })
        conv["updated"] = _now()
        _write(conv)


def history_for_model(cid: str, limit: int = _MAX_MODEL_HISTORY) -> list:
    """Return the recent {role, content} pairs for the LLM context window."""
    conv = _load(cid)
    if not conv:
        return []
    msgs = [{"role": m["role"], "content": m["content"]} for m in conv["messages"]]
    return msgs[-limit:]


def list_conversations() -> list:
    """All conversations, newest first: id, title, updated time, message count."""
    out = []
    if _DIR.exists():
        for p in _DIR.glob("*.json"):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not c.get("messages"):
                continue  # skip empty/never-used conversations
            out.append({
                "id": c.get("id", p.stem),
                "title": c.get("title") or "Untitled",
                "updated": c.get("updated", ""),
                "count": len(c.get("messages", [])),
            })
    out.sort(key=lambda c: c["updated"], reverse=True)
    return out


def get_conversation(cid: str) -> dict | None:
    """Full conversation (messages with tools/files) for rendering in the UI."""
    return _load(cid)


def rename(cid: str, title: str) -> bool:
    with _lock:
        conv = _load(cid)
        if not conv:
            return False
        title = (title or "").strip()[:120]
        if title:
            conv["title"] = title
            _write(conv)
        return True


def clear(cid: str) -> None:
    """Empty a conversation's messages but keep the (now blank) record."""
    with _lock:
        conv = _load(cid)
        if conv:
            conv["messages"] = []
            conv["updated"] = _now()
            _write(conv)


def delete(cid: str) -> None:
    with _lock:
        p = _path(cid)
        if p.exists():
            p.unlink()
