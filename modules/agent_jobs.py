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
    """A JSON-safe snapshot (omits internal cancel flags)."""
    return {
        "batch_id": batch["id"], "created": batch["created"], "context": batch["context"],
        "provider": batch["provider"], "model": batch["model"], "cap": batch["cap"],
        "status": batch_status(batch),
        "agents": [{
            "id": a["id"], "task": a["task"], "status": a["status"],
            "steps": list(a["steps"]), "result": a["result"], "error": a["error"],
            "started": a["started"], "finished": a["finished"],
        } for a in batch["agents"]],
    }


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


def launch(tasks, context: str, provider: str, model: str, key: str, base: str = "") -> dict:
    """Create a batch and run its agents in the background (non-blocking).
    Returns the batch view immediately. Respects the per-provider concurrency cap."""
    tasks = [str(t).strip() for t in (tasks or []) if str(t).strip()]
    if not tasks:
        return {"error": "No tasks to run."}
    cap = provider_cap(provider)
    batch = {
        "id": "batch-" + uuid.uuid4().hex[:8], "created": _now(), "context": (context or "").strip(),
        "provider": provider, "model": model, "key": key, "base": base, "cap": cap,
        "agents": [_new_agent(t) for t in tasks],
    }
    with _LOCK:
        _BATCHES[batch["id"]] = batch
        _prune_locked()
    for agent in batch["agents"]:
        _POOL.submit(_run_agent, batch, agent)
    logger.info(f"launched {batch['id']} with {len(tasks)} agent(s), provider={provider} cap={cap}")
    return _batch_view(batch)


def get_batch(batch_id: str) -> dict:
    with _LOCK:
        b = _BATCHES.get(batch_id)
        return _batch_view(b) if b else {}


def list_batches(limit: int = 10) -> list:
    with _LOCK:
        ordered = sorted(_BATCHES.values(), key=lambda b: b["created"], reverse=True)[:limit]
        return [_batch_view(b) for b in ordered]


def cancel_agent(agent_id: str) -> dict:
    with _LOCK:
        for b in _BATCHES.values():
            for a in b["agents"]:
                if a["id"] == agent_id:
                    a["cancel"] = True
                    if a["status"] == "queued":
                        a["status"] = "cancelled"; a["finished"] = _now()
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
    return {"cancelled": batch_id}


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
