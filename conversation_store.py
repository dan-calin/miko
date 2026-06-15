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
def append_user(cid: str, user_msg: str, attachments: list | None = None) -> None:
    """Persist the user's message at the START of a turn, creating the conversation
    if needed. This makes the conversation appear (and survive) immediately, even if
    the turn runs long (e.g. a pair-programming session) or errors before finishing."""
    with _lock:
        conv = _load(cid) or {
            "id": _safe_id(cid), "title": "", "created": _now(), "messages": [],
        }
        if not conv.get("title"):
            conv["title"] = _title_from(user_msg)
        turn = {"role": "user", "content": user_msg, "ts": _now()}
        if attachments:
            turn["attachments"] = attachments
        conv["messages"].append(turn)
        conv["updated"] = _now()
        _write(conv)


def append_assistant(cid: str, assistant_msg: str,
                     tools: list | None = None, files: list | None = None) -> None:
    """Attach the assistant's reply to the conversation when the turn completes."""
    with _lock:
        conv = _load(cid) or {
            "id": _safe_id(cid), "title": _title_from(assistant_msg),
            "created": _now(), "messages": [],
        }
        conv["messages"].append({
            "role": "assistant", "content": assistant_msg, "ts": _now(),
            "tools": tools or [], "files": files or [],
        })
        conv["updated"] = _now()
        _write(conv)


def append_turn(cid: str, user_msg: str, assistant_msg: str,
                tools: list | None = None, files: list | None = None,
                attachments: list | None = None) -> None:
    """Record one user→assistant exchange, creating the conversation if needed.

    `attachments` is per-file display metadata ({name, mime, kind, overview}); the
    user `content` stays the typed message, so a long extracted document shows as a
    chip rather than filling the transcript.
    """
    with _lock:
        conv = _load(cid) or {
            "id": _safe_id(cid), "title": "", "created": _now(), "messages": [],
        }
        if not conv.get("title"):
            conv["title"] = _title_from(user_msg)
        user_turn = {"role": "user", "content": user_msg, "ts": _now()}
        if attachments:
            user_turn["attachments"] = attachments
        conv["messages"].append(user_turn)
        conv["messages"].append({
            "role": "assistant", "content": assistant_msg, "ts": _now(),
            "tools": tools or [], "files": files or [],
        })
        conv["updated"] = _now()
        _write(conv)


def history_for_model(cid: str, limit: int = _MAX_MODEL_HISTORY) -> list:
    """Return the recent {role, content} pairs for the LLM context window.

    Assistant turns that ran tools are tagged with the tool names. Without this the
    replayed history is text-only, so the model sees past action-confirmations as
    bare prose ('Sent.', 'Done.') and starts imitating that — narrating actions
    instead of calling tools. The tag reminds it: confirmations come WITH a tool call.
    """
    conv = _load(cid)
    if not conv:
        return []
    msgs = []
    for m in conv["messages"]:
        content = m["content"]
        if m.get("role") == "assistant":
            names = list(dict.fromkeys(t for t in (m.get("tools") or []) if isinstance(t, str)))
            if names:
                content = f"{content}\n[Done by calling: {', '.join(names)}]"
        elif m.get("role") == "user" and m.get("attachments"):
            # The visible content omits extracted file text; fold the overviews back in
            # so the model keeps the document context on follow-up turns.
            for a in m["attachments"]:
                ov = (a.get("overview") or "").strip()
                if ov:
                    content += (f"\n\n[Attached {a.get('kind') or 'file'} "
                                f"'{a.get('name') or 'file'}']\n```\n{ov}\n```")
        msgs.append({"role": m["role"], "content": content})
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
