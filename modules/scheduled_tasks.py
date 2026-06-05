"""
modules/scheduled_tasks.py — run a Miko prompt on a schedule, DM the result.

Lets the user set up recurring agent actions ("every morning, summarize my inbox"),
stored in data/scheduled_tasks.json. A daemon polls every minute; due tasks run the
prompt through the chat backend (read-only — allow_actions off) and Discord-DM the
result to the owner. Reuses the calendar_reminders daemon pattern.
"""

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("miko.scheduled")

_started = False

TOOL_DECLARATIONS = [
    {
        "name": "schedule_task",
        "description": (
            "Schedule a recurring task: Miko runs the prompt on a schedule and DMs you "
            "the result. Use for 'every morning summarize my inbox', 'remind me of my "
            "schedule at 8am', 'every 2 hours check X'. Schedule format: 'HH:MM' (daily) "
            "or 'every 30m' / 'every 2h'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {"type": "STRING", "description": "What Miko should do each time."},
                "when": {"type": "STRING", "description": "'HH:MM' (daily) or 'every 30m' / 'every 2h'."},
            },
            "required": ["prompt", "when"],
        },
    },
    {
        "name": "list_scheduled_tasks",
        "description": "List the recurring tasks Miko has scheduled.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "cancel_scheduled_task",
        "description": "Cancel a scheduled task by its id or a keyword from its prompt.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"which": {"type": "STRING", "description": "Task id or keyword."}},
            "required": ["which"],
        },
    },
]


def _path() -> Path:
    from config import CONFIG
    return CONFIG.data_dir / "scheduled_tasks.json"


def _load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(reg: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")


def _parse_when(when: str):
    w = (when or "").strip().lower()
    m = re.match(r"^(\d{1,2}):(\d{2})$", w)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return ("daily", (int(m.group(1)), int(m.group(2))))
    m = re.match(r"^every\s+(\d+)\s*(m|min|mins|minutes|h|hr|hour|hours)$", w)
    if m:
        n = int(m.group(1))
        return ("interval", n * 60 if m.group(2).startswith("m") else n * 3600)
    return None


# ── Tools ─────────────────────────────────────────────────────────────────────

def schedule_task(prompt: str, when: str) -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        return "What should I do on schedule?"
    if not _parse_when(when):
        return "I didn't get the schedule. Use 'HH:MM' (daily) or 'every 30m' / 'every 2h'."
    import uuid
    tid = "task-" + uuid.uuid4().hex[:6]
    reg = _load()
    reg[tid] = {"prompt": prompt, "when": when.strip(),
                "created": datetime.now().isoformat(), "last_run": 0, "last_day": ""}
    _save(reg)
    return f"Scheduled ({when.strip()}): \"{prompt[:60]}\". I'll DM you each time. (id {tid})"


def list_scheduled_tasks() -> str:
    reg = _load()
    if not reg:
        return "No scheduled tasks yet."
    lines = ["Scheduled tasks:"]
    for tid, t in reg.items():
        lines.append(f"- [{tid}] {t.get('when')}: {t.get('prompt', '')[:60]}")
    return "\n".join(lines)


def cancel_scheduled_task(which: str) -> str:
    reg = _load()
    which = (which or "").strip()
    key = which if which in reg else next(
        (k for k in reg if which.lower() in reg[k].get("prompt", "").lower()), None)
    if not key:
        return f"No scheduled task matching '{which}'."
    reg.pop(key, None)
    _save(reg)
    return f"Cancelled scheduled task {key}."


# ── Daemon ────────────────────────────────────────────────────────────────────

def _due(task: dict, now: datetime) -> bool:
    spec = _parse_when(task.get("when", ""))
    if not spec:
        return False
    kind, val = spec
    if kind == "interval":
        return (now.timestamp() - (task.get("last_run") or 0)) >= val
    h, m = val   # daily HH:MM — fire once within a ~2-min window
    return now.hour == h and abs(now.minute - m) <= 1 and task.get("last_day") != now.date().isoformat()


def _run_task(tid: str, task: dict) -> None:
    try:
        import tool_server
        import chat_backend
        from config import CONFIG
        router = getattr(tool_server, "_router", None)
        if router is None:
            return
        out = chat_backend.chat(
            router=router, session_id=f"scheduled-{tid}", message=task["prompt"],
            provider="gemini", model="", allow_actions=False,
            owner_name=CONFIG.owner_name, language=getattr(CONFIG, "language", "en"),
        )
        reply = (out or {}).get("reply") or "(no result)"
        try:
            from modules.discord_bot import send_dm_direct
            send_dm_direct(recipient_name=CONFIG.owner_name,
                           message=f"⏰ Scheduled: {task['prompt'][:80]}\n\n{reply}")
        except Exception as e:
            logger.debug(f"scheduled DM failed: {e}")
        logger.info(f"ran scheduled task {tid}")
    except Exception as e:
        logger.warning(f"scheduled task {tid} error: {e}")


def _loop() -> None:
    logger.info("Scheduled-tasks daemon started")
    while True:
        try:
            reg = _load()
            now = datetime.now()
            for tid, task in list(reg.items()):
                if _due(task, now):
                    task["last_run"] = now.timestamp()
                    task["last_day"] = now.date().isoformat()
                    _save(reg)            # persist before running, so we don't double-fire
                    _run_task(tid, task)
        except Exception as e:
            logger.warning(f"scheduled loop error: {e}")
        time.sleep(60)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="ScheduledTasks").start()
