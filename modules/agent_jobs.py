"""
modules/agent_jobs.py — observable, async sub-agent jobs with provider-aware limits.

The engine behind the live sub-agent panel (user-launchable + observable). Where
modules/subagents.spawn_agents() blocks and returns prose, this runs agents in the
BACKGROUND and records each one's progress as a structured step log
({action, status, detail}) — the "thinking accordion" the UI renders, instead of
streaming token-heavy prose. The UI (or Miko) launches a batch and then polls/streams
its state.

Concurrency is capped PER PROVIDER and enforced globally by a semaphore, because cheap
plans throttle hard — e.g. MiniMax API Starter allows only 1-2 concurrent agents. Extra
agents queue (status 'queued') and start as slots free up, so a batch never exceeds the
provider's limit no matter how many are launched.
"""

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("miko.agent_jobs")

# Max agents that may run AT ONCE per provider (extra ones queue). Cheap plans throttle.
_CAPS = {"minimax": 2, "gemini": 5, "openai": 4, "anthropic": 4,
         "deepseek": 3, "kimi": 3, "xai": 3}
_DEFAULT_CAP = 3
_PER_AGENT_CHARS = 2000
_MAX_BATCHES_KEPT = 30      # ring-buffer the registry so it can't grow unbounded

_BATCHES = {}              # batch_id -> batch dict (see _new_batch)
_LOCK = threading.RLock()
_SEMAPHORES = {}           # provider -> BoundedSemaphore(cap)
_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="subagent")
_local = threading.local()
_session_local = threading.local()   # the conversation a spawn belongs to (per thread)


# ── Session attribution ───────────────────────────────────────────────────────
# A batch is tied to the chat session that launched it, so the UI can show each
# conversation's own sub-agents. The UI passes session_id explicitly; Miko's own
# spawn_agents() runs inside a chat turn, so chat_backend stamps the active session
# on this thread and launch() picks it up.

def set_current_session(session_id: str) -> None:
    _session_local.sid = (session_id or "").strip()


def current_session() -> str:
    return getattr(_session_local, "sid", "") or ""


def provider_cap(provider: str) -> int:
    """Max concurrent agents for a provider (env override wins)."""
    env = os.getenv("MIKO_SUBAGENT_CONCURRENCY", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    return _CAPS.get((provider or "").lower(), _DEFAULT_CAP)


def _semaphore(provider: str, cap: int) -> threading.BoundedSemaphore:
    with _LOCK:
        sem = _SEMAPHORES.get(provider)
        if sem is None:
            sem = threading.BoundedSemaphore(cap)
            _SEMAPHORES[provider] = sem
        return sem


def _now() -> float:
    return time.time()


# ── Registry ────────────────────────────────────────────────────────────────────

def _new_agent(task: str) -> dict:
    return {"id": "a-" + uuid.uuid4().hex[:8], "task": task, "status": "queued",
            "steps": [], "result": "", "error": "", "started": 0.0, "finished": 0.0,
            "cancel": False}


def _prune_locked() -> None:
    if len(_BATCHES) > _MAX_BATCHES_KEPT:
        for bid in sorted(_BATCHES, key=lambda b: _BATCHES[b]["created"])[:-_MAX_BATCHES_KEPT]:
            _BATCHES.pop(bid, None)


def _batch_view(batch: dict) -> dict:
    """A JSON-safe snapshot (omits the API key + internal cancel flags)."""
    return {
        "batch_id": batch["id"], "created": batch["created"], "context": batch["context"],
        "provider": batch["provider"], "model": batch["model"], "cap": batch["cap"],
        "session_id": batch.get("session_id", ""), "status": batch_status(batch),
        "agents": [{
            "id": a["id"], "task": a["task"], "status": a["status"],
            "steps": list(a["steps"]), "result": a["result"], "error": a["error"],
            "started": a["started"], "finished": a["finished"],
        } for a in batch["agents"]],
    }


# ── Disk persistence ──────────────────────────────────────────────────────────
# Batches live in memory, but the UI panel must survive a page refresh AND a server
# restart. We persist a key-free snapshot to data/agent_batches.json; the API key and
# live cancel flags are never written.

def _path():
    from config import CONFIG
    return CONFIG.data_dir / "agent_batches.json"


def _save() -> None:
    """Write all batches (key-free) to disk. Best-effort, atomic."""
    try:
        with _LOCK:
            views = [_batch_view(b) for b in
                     sorted(_BATCHES.values(), key=lambda b: b["created"])]
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(views, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        logger.debug(f"agent_batches save skipped: {e}")


def _load() -> None:
    """Restore persisted batches on startup. Any agent left mid-flight by a crash/
    restart is marked errored — its worker thread is gone, so it can't resume."""
    try:
        p = _path()
        if not p.exists():
            return
        views = json.loads(p.read_text(encoding="utf-8")) or []
    except Exception as e:
        logger.debug(f"agent_batches load skipped: {e}")
        return
    with _LOCK:
        for v in views:
            agents = []
            for a in v.get("agents", []):
                st = a.get("status", "done")
                err = a.get("error", "")
                if st in ("running", "queued"):
                    st = "error"
                    err = err or "Interrupted (Miko restarted)"
                agents.append({
                    "id": a.get("id") or "a-" + uuid.uuid4().hex[:8],
                    "task": a.get("task", ""), "status": st,
                    "steps": list(a.get("steps", [])), "result": a.get("result", ""),
                    "error": err, "started": a.get("started", 0.0),
                    "finished": a.get("finished", 0.0), "cancel": False,
                })
            bid = v.get("batch_id") or "batch-" + uuid.uuid4().hex[:8]
            _BATCHES[bid] = {
                "id": bid, "created": v.get("created", _now()),
                "context": v.get("context", ""), "provider": v.get("provider", ""),
                "model": v.get("model", ""), "key": "", "base": "",
                "cap": v.get("cap", _DEFAULT_CAP), "session_id": v.get("session_id", ""),
                "agents": agents,
            }
        _prune_locked()


def batch_status(batch: dict) -> str:
    sts = [a["status"] for a in batch["agents"]]
    if any(s in ("running", "queued") for s in sts):
        return "running"
    if all(s == "cancelled" for s in sts):
        return "cancelled"
    if any(s == "error" for s in sts):
        return "partial" if any(s == "done" for s in sts) else "error"
    return "done"


# ── Execution ───────────────────────────────────────────────────────────────────

def _make_emit(agent: dict):
    """Translate chat()'s tool_start/tool_end/round events into structured steps."""
    def emit(ev: dict):
        try:
            t = ev.get("type")
            with _LOCK:
                if t == "tool_start":
                    agent["steps"].append({"t": _now(), "action": ev.get("name", "tool"),
                                           "status": "running", "detail": ev.get("args", "")})
                elif t == "tool_end":
                    # close the most recent matching running step
                    for s in reversed(agent["steps"]):
                        if s["action"] == ev.get("name") and s["status"] == "running":
                            s["status"] = "ok" if ev.get("status") == "ok" else "warning"
                            s["detail"] = ev.get("summary", s["detail"])
                            break
                elif t == "round":
                    agent["steps"].append({"t": _now(), "action": f"round {ev.get('n')}",
                                           "status": "info", "detail": ""})
        except Exception:
            pass
    return emit


def _run_agent(batch: dict, agent: dict) -> None:
    provider, model, key, base = batch["provider"], batch["model"], batch["key"], batch["base"]
    sem = _semaphore(provider, batch["cap"])
    if agent["cancel"]:
        agent["status"] = "cancelled"; agent["finished"] = _now(); return
    sem.acquire()
    try:
        if agent["cancel"]:
            agent["status"] = "cancelled"; agent["finished"] = _now(); return
        agent["status"] = "running"; agent["started"] = _now()
        _local.depth = 1   # refuse nested spawns from inside a sub-agent
        import chat_backend
        from modules.subagents import _get_router
        ctx = batch["context"]
        prompt = (f"{ctx}\n\n---\n\nTask: {agent['task']}" if ctx else agent["task"])
        prompt += ("\n\nYou are a focused sub-agent. Use your tools to complete ONLY this "
                   "task, then reply with a concise, self-contained findings summary.")
        res = chat_backend.chat(
            _get_router(), f"subagent-{agent['id']}", prompt, provider, model, key,
            base_url=base, allow_actions=False, effort="standard",
            emit=_make_emit(agent), should_cancel=lambda: agent["cancel"],
        )
        if agent["cancel"]:
            agent["status"] = "cancelled"
        elif res.get("error"):
            agent["status"] = "error"; agent["error"] = res["error"]
        else:
            agent["status"] = "done"
            agent["result"] = (res.get("reply") or "").strip()[:_PER_AGENT_CHARS] or "(no findings)"
    except Exception as e:
        logger.warning(f"sub-agent {agent['id']} failed: {e}")
        agent["status"] = "error"; agent["error"] = str(e)
    finally:
        _local.depth = 0
        agent["finished"] = _now()
        try:
            sem.release()
        except ValueError:
            pass
        _save()   # persist the terminal state so a refresh/restart keeps it


def launch(tasks, context: str, provider: str, model: str, key: str, base: str = "",
           session_id: str = "") -> dict:
    """Create a batch and run its agents in the background (non-blocking).
    Returns the batch view immediately. Respects the per-provider concurrency cap.
    The batch is tied to session_id (or the active chat session) so the UI panel can
    group each conversation's sub-agents."""
    tasks = [str(t).strip() for t in (tasks or []) if str(t).strip()]
    if not tasks:
        return {"error": "No tasks to run."}
    cap = provider_cap(provider)
    batch = {
        "id": "batch-" + uuid.uuid4().hex[:8], "created": _now(), "context": (context or "").strip(),
        "provider": provider, "model": model, "key": key, "base": base, "cap": cap,
        "session_id": (session_id or "").strip() or current_session(),
        "agents": [_new_agent(t) for t in tasks],
    }
    with _LOCK:
        _BATCHES[batch["id"]] = batch
        _prune_locked()
    _save()
    for agent in batch["agents"]:
        _POOL.submit(_run_agent, batch, agent)
    logger.info(f"launched {batch['id']} with {len(tasks)} agent(s), provider={provider} cap={cap}")
    return _batch_view(batch)


def in_subagent() -> bool:
    """True when called from inside a running sub-agent thread (blocks nested spawns)."""
    return getattr(_local, "depth", 0) >= 1


def run_and_wait(tasks, context: str, provider: str, model: str, key: str,
                 base: str = "", timeout: float = 600) -> dict:
    """Launch a batch and block until every agent is terminal; return the batch view.
    Used by the blocking spawn_agents tool so Miko's own spawns are observable too."""
    view = launch(tasks, context, provider, model, key, base)
    if view.get("error"):
        return view
    bid = view["batch_id"]
    start = _now()
    while _now() - start < timeout:
        b = get_batch(bid)
        if b.get("status") != "running":
            return b
        time.sleep(0.4)
    return get_batch(bid)


def get_batch(batch_id: str) -> dict:
    with _LOCK:
        b = _BATCHES.get(batch_id)
        return _batch_view(b) if b else {}


def list_batches(limit: int = 10, session_id: str = "") -> list:
    """Most-recent batches, newest first. If session_id is given, only that
    conversation's batches are returned (the UI panel groups by conversation)."""
    sid = (session_id or "").strip()
    with _LOCK:
        vals = _BATCHES.values()
        if sid:
            vals = [b for b in vals if b.get("session_id", "") == sid]
        ordered = sorted(vals, key=lambda b: b["created"], reverse=True)[:limit]
        return [_batch_view(b) for b in ordered]


def cancel_agent(agent_id: str) -> dict:
    with _LOCK:
        for b in _BATCHES.values():
            for a in b["agents"]:
                if a["id"] == agent_id:
                    a["cancel"] = True
                    if a["status"] == "queued":
                        a["status"] = "cancelled"; a["finished"] = _now()
                    _save()
                    return {"cancelled": agent_id}
    return {"error": "Unknown agent."}


def cancel_batch(batch_id: str) -> dict:
    with _LOCK:
        b = _BATCHES.get(batch_id)
        if not b:
            return {"error": "Unknown batch."}
        for a in b["agents"]:
            a["cancel"] = True
            if a["status"] == "queued":
                a["status"] = "cancelled"; a["finished"] = _now()
    _save()
    return {"cancelled": batch_id}


def delete_batch(batch_id: str) -> dict:
    """Remove a batch from the registry + disk (used to clear a session from the
    panel). Any still-running agents are signalled to cancel first; their workers
    hold their own references, so they wind down cleanly."""
    with _LOCK:
        b = _BATCHES.pop(batch_id, None)
        if not b:
            return {"error": "Unknown batch."}
        for a in b["agents"]:
            a["cancel"] = True
    _save()
    return {"deleted": batch_id}


def stream_batch(batch_id: str, interval: float = 0.6, timeout: float = 1800):
    """Yield batch snapshots until every agent is terminal (for an NDJSON endpoint)."""
    start = _now()
    last = None
    while True:
        view = get_batch(batch_id)
        if not view:
            yield {"type": "error", "error": "Unknown batch."}
            return
        # only emit when something changed, to keep the stream quiet
        sig = [(a["id"], a["status"], len(a["steps"])) for a in view["agents"]]
        if sig != last:
            yield {"type": "batch", **view}
            last = sig
        if view["status"] != "running" or (_now() - start) > timeout:
            yield {"type": "done", "batch_id": batch_id, "status": view["status"]}
            return
        time.sleep(interval)


# Restore any batches from a previous run so the UI panel persists across restarts.
_load()
