"""
modules/scheduled_tasks.py — run a Miko prompt on a schedule, DM the result.

A workspace-automation scheduler. Tasks support daily / weekly / monthly / once /
interval, plus optional cron (if `croniter` is installed). Natural-language
scheduling ("every morning at 8 summarize my unread emails") is parsed into a
structured task. A daemon polls each minute; due ACTIVE tasks run the prompt
through the chat backend (read-only — allow_actions off) and Discord-DM the
result to the owner. The chat UI exposes a Scheduler panel to add / view / pause /
resume / cancel / run-now. Stored in data/scheduled_tasks.json.
"""

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("miko.scheduled")

_started = False
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

TOOL_DECLARATIONS = [
    {
        "name": "schedule_task",
        "description": (
            "Schedule a recurring task: Miko runs the prompt on a schedule and DMs you "
            "the result. Use for 'every morning summarize my inbox', 'remind me of my "
            "schedule at 8am', 'every 2 hours check X', 'every Monday at 9 plan my week'. "
            "`when` accepts: 'HH:MM' (daily), 'every 30m' / 'every 2h' (interval), "
            "'monday 09:00' (weekly), or plain language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {"type": "STRING", "description": "What Miko should do each time."},
                "when": {"type": "STRING", "description": "'HH:MM' / 'every 30m' / 'monday 09:00' / natural language."},
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


# ── Store ─────────────────────────────────────────────────────────────────────

def _path() -> Path:
    from config import CONFIG
    return CONFIG.data_dir / "scheduled_tasks.json"


def _load() -> dict:
    p = _path()
    if p.exists():
        try:
            reg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
        changed = False
        for tid, t in reg.items():
            if _migrate(t):
                changed = True
        if changed:
            _save(reg)
        return reg
    return {}


def _save(reg: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")


def _migrate(task: dict) -> bool:
    """Upgrade a legacy {prompt, when, last_run, last_day} task to the structured
    model in place. Returns True if it changed."""
    if task.get("schedule"):
        task.setdefault("status", "active")
        return False
    spec = _parse_when(task.get("when", ""))
    if not spec:
        task["schedule"] = "interval"; task["interval_seconds"] = 3600
    elif spec[0] == "daily":
        h, m = spec[1]; task["schedule"] = "daily"; task["time"] = f"{h:02d}:{m:02d}"
    else:
        task["schedule"] = "interval"; task["interval_seconds"] = spec[1]
    task.setdefault("status", "active")
    task.setdefault("run_count", 0)
    task["next_run"] = compute_next_run(task)
    return True


# ── Schedule parsing / next-run computation ───────────────────────────────────

def _parse_when(when: str):
    """Legacy parser kept for back-compat: 'HH:MM' (daily) or 'every Nm/Nh' (interval)."""
    w = (when or "").strip().lower()
    m = re.match(r"^(\d{1,2}):(\d{2})$", w)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return ("daily", (int(m.group(1)), int(m.group(2))))
    m = re.match(r"^every\s+(\d+)\s*(m|min|mins|minutes|h|hr|hour|hours)$", w)
    if m:
        n = int(m.group(1))
        return ("interval", n * 60 if m.group(2).startswith("m") else n * 3600)
    return None


def _hhmm(t: str):
    m = re.match(r"^(\d{1,2}):(\d{2})$", (t or "").strip())
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _next_monthly(now: datetime, dom: int, hh: int, mm: int):
    dom = max(1, min(31, dom))
    y, mo = now.year, now.month
    for _ in range(13):
        # clamp day to the month's length
        if mo == 12:
            last = 31
        else:
            last = (datetime(y, mo + 1, 1) - timedelta(days=1)).day
        day = min(dom, last)
        cand = datetime(y, mo, day, hh, mm)
        if cand > now:
            return cand
        mo += 1
        if mo > 12:
            mo = 1; y += 1
    return None


def compute_next_run(task: dict, now: datetime = None):
    """Next fire time as an epoch float, or None (e.g. a past one-off)."""
    now = now or datetime.now()
    sched = task.get("schedule")
    hh, mm = _hhmm(task.get("time", "09:00"))

    if sched == "interval":
        sec = int(task.get("interval_seconds") or 0)
        if sec <= 0:
            return None
        base = task.get("last_run") or now.timestamp()
        nxt = base + sec
        # don't fire a backlog burst if Miko was off for a while
        return max(nxt, now.timestamp())

    if sched == "once":
        try:
            dt = datetime.strptime(f"{task.get('date')} {task.get('time', '09:00')}", "%Y-%m-%d %H:%M")
        except Exception:
            return None
        return dt.timestamp() if dt > now else None

    if sched == "cron":
        try:
            from croniter import croniter
            return croniter(task.get("cron", ""), now).get_next(float)
        except Exception as e:
            logger.warning(f"cron compute failed: {e}")
            return None

    if sched in ("daily", "weekly", "monthly"):
        if hh is None:
            return None
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if sched == "daily":
            if cand <= now:
                cand += timedelta(days=1)
            return cand.timestamp()
        if sched == "weekly":
            wd = int(task.get("weekday", 0)) % 7
            cand += timedelta(days=(wd - cand.weekday()) % 7)
            if cand <= now:
                cand += timedelta(days=7)
            return cand.timestamp()
        if sched == "monthly":
            cand = _next_monthly(now, int(task.get("day", 1)), hh, mm)
            return cand.timestamp() if cand else None
    return None


def describe(task: dict) -> str:
    """Human-readable schedule string."""
    s = task.get("schedule")
    t = task.get("time", "")
    if s == "daily":
        return f"Daily at {t}"
    if s == "weekly":
        return f"Weekly on {_WEEKDAYS[int(task.get('weekday', 0)) % 7]} at {t}"
    if s == "monthly":
        return f"Monthly on day {int(task.get('day', 1))} at {t}"
    if s == "once":
        return f"Once on {task.get('date', '?')} at {t}"
    if s == "interval":
        sec = int(task.get("interval_seconds") or 0)
        return f"Every {sec // 3600}h" if sec >= 3600 else f"Every {max(1, sec // 60)}m"
    if s == "cron":
        return f"Cron: {task.get('cron', '')}"
    return task.get("when", "(unknown)")


# ── Validation / creation ─────────────────────────────────────────────────────

def _clean_task(fields: dict) -> dict:
    """Build a validated task from raw fields; raise ValueError on bad input."""
    sched = (fields.get("schedule") or "").strip().lower()
    prompt = (fields.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("Tell me what the task should do.")
    if sched not in ("daily", "weekly", "monthly", "once", "interval", "cron"):
        raise ValueError("schedule must be daily/weekly/monthly/once/interval/cron.")
    task = {"prompt": prompt, "name": (fields.get("name") or "").strip(),
            "schedule": sched, "status": "active", "run_count": 0,
            "created": datetime.now().isoformat(), "last_run": 0}
    if sched in ("daily", "weekly", "monthly", "once"):
        hh, mm = _hhmm(fields.get("time", ""))
        if hh is None:
            raise ValueError("Need a valid time as HH:MM.")
        task["time"] = f"{hh:02d}:{mm:02d}"
    if sched == "weekly":
        task["weekday"] = int(fields.get("weekday", 0)) % 7
    if sched == "monthly":
        task["day"] = max(1, min(31, int(fields.get("day", 1))))
    if sched == "once":
        d = (fields.get("date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            raise ValueError("Need a date as YYYY-MM-DD for a one-off task.")
        task["date"] = d
    if sched == "interval":
        sec = int(fields.get("interval_seconds") or 0)
        if sec < 60:
            raise ValueError("Interval must be at least 60 seconds.")
        task["interval_seconds"] = sec
    if sched == "cron":
        cron = (fields.get("cron") or "").strip()
        try:
            from croniter import croniter
            if not croniter.is_valid(cron):
                raise ValueError("Invalid cron expression.")
        except ImportError:
            raise ValueError("Cron needs the croniter package: pip install croniter")
        task["cron"] = cron
    nxt = compute_next_run(task)
    if nxt is None:
        raise ValueError("That schedule has no upcoming run (e.g. a date in the past).")
    task["next_run"] = nxt
    return task


def create_task(**fields) -> dict:
    reg = _load()
    task = _clean_task(fields)
    tid = "task-" + uuid.uuid4().hex[:6]
    reg[tid] = task
    _save(reg)
    start()
    return _view(tid, task)


def _view(tid: str, t: dict) -> dict:
    return {
        "id": tid, "name": t.get("name", ""), "prompt": t.get("prompt", ""),
        "schedule": t.get("schedule", ""), "describe": describe(t),
        "status": t.get("status", "active"), "run_count": t.get("run_count", 0),
        "last_run": t.get("last_run", 0), "next_run": t.get("next_run"),
        "time": t.get("time", ""), "weekday": t.get("weekday"), "day": t.get("day"),
        "date": t.get("date", ""), "interval_seconds": t.get("interval_seconds"),
        "cron": t.get("cron", ""),
    }


def list_tasks() -> list:
    reg = _load()
    return [_view(tid, t) for tid, t in
            sorted(reg.items(), key=lambda kv: kv[1].get("next_run") or 9e18)]


def set_status(tid: str, status: str) -> dict:
    reg = _load()
    t = reg.get(tid)
    if not t:
        return {"error": "Unknown task."}
    t["status"] = "paused" if status == "paused" else "active"
    if t["status"] == "active":
        t["next_run"] = compute_next_run(t)
    _save(reg)
    return _view(tid, t)


def delete_task(tid: str) -> dict:
    reg = _load()
    if tid not in reg:
        return {"error": "Unknown task."}
    reg.pop(tid, None)
    _save(reg)
    return {"deleted": tid}


def run_task_now(tid: str) -> dict:
    reg = _load()
    t = reg.get(tid)
    if not t:
        return {"error": "Unknown task."}
    threading.Thread(target=_run_task, args=(tid, t), daemon=True).start()
    return {"running": tid}


# ── Natural-language → structured task ────────────────────────────────────────

_PARSE_SYS = (
    "You convert a natural-language scheduling request into ONE JSON object describing "
    "a recurring task, and nothing else (no markdown, no prose). Schema:\n"
    '{"prompt": str, "name": short str, "schedule": "daily|weekly|monthly|once|interval|cron",'
    ' "time": "HH:MM" (24h, for daily/weekly/monthly/once), "weekday": 0-6 (Mon=0, weekly),'
    ' "day": 1-31 (monthly), "date": "YYYY-MM-DD" (once), "interval_seconds": int (interval),'
    ' "cron": "m h dom mon dow" (cron)}.\n'
    "Only include the fields relevant to the chosen schedule. `prompt` is what the "
    "assistant should DO each time (imperative, self-contained). Default time 09:00 if "
    "unspecified. 'every morning'->daily 08:00; 'every Monday'->weekly weekday 0; 'every "
    "2 hours'->interval 7200. If a year isn't given for a one-off, assume the next "
    "occurrence. Output ONLY the JSON."
)


def parse_task_nl(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"error": "Describe the task to schedule."}
    try:
        from chat_backend import complete_text
        from config import CONFIG
        raw = complete_text("gemini", "gemini-2.5-flash",
                            api_key=getattr(CONFIG, "gemini_api_key", ""),
                            system=_PARSE_SYS, user=text, max_tokens=400)
    except Exception as e:
        return {"error": f"Could not parse: {e}"}
    raw = (raw or "").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {"error": "Could not understand that schedule."}
    try:
        draft = json.loads(m.group(0))
    except Exception:
        return {"error": "Could not understand that schedule."}
    # Validate by building a task (also computes next_run + describe), but don't persist.
    try:
        task = _clean_task(draft)
    except ValueError as e:
        return {"error": str(e), "draft": draft}
    return {"draft": _view("(preview)", task)}


# ── Legacy tools (voice / chat) ───────────────────────────────────────────────

def schedule_task(prompt: str, when: str) -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        return "What should I do on schedule?"
    fields = {"prompt": prompt}
    w = (when or "").strip().lower()
    spec = _parse_when(w)
    wk = re.match(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+(\d{1,2}):(\d{2})$", w)
    if spec and spec[0] == "daily":
        fields.update(schedule="daily", time=f"{spec[1][0]:02d}:{spec[1][1]:02d}")
    elif spec and spec[0] == "interval":
        fields.update(schedule="interval", interval_seconds=spec[1])
    elif wk:
        idx = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(wk.group(1))
        fields.update(schedule="weekly", weekday=idx, time=f"{int(wk.group(2)):02d}:{int(wk.group(3)):02d}")
    else:
        # fall back to the NL parser for anything richer
        parsed = parse_task_nl(f"{prompt}. Schedule: {when}")
        if parsed.get("error"):
            return "I didn't get the schedule. Try 'HH:MM' (daily), 'every 30m', or 'monday 09:00'."
        d = parsed["draft"]
        fields.update(schedule=d["schedule"], time=d.get("time", ""), weekday=d.get("weekday", 0),
                      day=d.get("day", 1), date=d.get("date", ""),
                      interval_seconds=d.get("interval_seconds", 0), cron=d.get("cron", ""))
    try:
        v = create_task(**fields)
    except ValueError as e:
        return f"Couldn't schedule that: {e}"
    return f"Scheduled ({v['describe']}): \"{prompt[:60]}\". I'll DM you the result. (id {v['id']})"


def list_scheduled_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No scheduled tasks yet."
    lines = ["Scheduled tasks:"]
    for t in tasks:
        flag = "" if t["status"] == "active" else " [paused]"
        lines.append(f"- [{t['id']}] {t['describe']}{flag}: {t['prompt'][:60]}")
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

def _run_task(tid: str, task: dict) -> None:
    try:
        import tool_server
        import chat_backend
        from config import CONFIG
        router = getattr(tool_server, "_router", None)
        if router is None:
            return
        # Scheduled tasks are user-authored and run on the owner's behalf, so actions
        # ARE allowed (a task may need to send/email/run something). The reply is
        # auto-delivered to Discord below, so tell the model not to DM it itself —
        # otherwise a "ping me" task tries send_discord_dm and double-sends.
        note = ("[Scheduled task — your reply is delivered to the user on Discord "
                "automatically, so just produce the requested content/answer directly; "
                "do NOT call a tool to send this message yourself.]\n\n")
        out = chat_backend.chat(
            router=router, session_id=f"scheduled-{tid}", message=note + task["prompt"],
            provider="gemini", model="", allow_actions=True,
            owner_name=CONFIG.owner_name, language=getattr(CONFIG, "language", "en"),
        )
        reply = (out or {}).get("reply") or "(no result)"
        label = task.get("name") or task.get("prompt", "")[:80]
        try:
            from modules.discord_bot import send_dm_direct
            send_dm_direct(recipient_name=CONFIG.owner_name,
                           message=f"⏰ Scheduled: {label}\n\n{reply}")
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
            now_ts = now.timestamp()
            dirty = False
            for tid, task in list(reg.items()):
                if task.get("status") != "active":
                    continue
                nxt = task.get("next_run")
                if nxt is None:
                    continue
                if now_ts >= nxt:
                    task["last_run"] = now_ts
                    task["run_count"] = task.get("run_count", 0) + 1
                    if task.get("schedule") == "once":
                        task["status"] = "done"; task["next_run"] = None
                    else:
                        task["next_run"] = compute_next_run(task, now)
                    dirty = True
                    _save(reg)                 # persist before running (no double-fire)
                    _run_task(tid, task)
            if dirty:
                _save(reg)
        except Exception as e:
            logger.warning(f"scheduled loop error: {e}")
        time.sleep(60)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="ScheduledTasks").start()
