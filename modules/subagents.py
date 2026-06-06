"""
modules/subagents.py — let Miko summon parallel sub-agents.

Miko can delegate a big job to several focused sub-agents that run *concurrently*,
each an independent chat loop with the full READ-ONLY tool set (web search,
deep_research, recall, file read, browser, …). Each returns its findings, and the
parent model synthesizes them. This is how Miko parallelizes work — e.g. research
five angles of a topic at once, or investigate several files simultaneously.

Safety:
  - Sub-agents run with allow_actions=False — they cannot make changes, send, or
    delete. They observe and report.
  - A per-thread depth guard prevents a sub-agent from spawning further sub-agents
    (no runaway recursion / fork bombs).
  - At most _MAX sub-agents per call.
"""

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("miko.subagents")

_local = threading.local()
_MAX = 5
_PER_AGENT_CHARS = 2000

TOOL_DECLARATIONS = [
    {
        "name": "spawn_agents",
        "description": (
            "Delegate to parallel sub-agents to do a big job faster. Provide 1-5 focused, "
            "self-contained task prompts; each runs as an INDEPENDENT agent at the same "
            "time, with full read-only tools (web_search, deep_research, recall, read "
            "files, browser), and returns its findings. Use to research several angles at "
            "once, investigate multiple files/sources in parallel, or split a complex "
            "question into parts. Sub-agents are read-only (they cannot edit, send, or "
            "delete) and cannot spawn their own sub-agents. Returns every sub-agent's "
            "result for you to synthesize."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "tasks": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "1-5 focused, self-contained task prompts (one per sub-agent).",
                },
                "context": {
                    "type": "STRING",
                    "description": "Optional shared background handed to every sub-agent.",
                },
            },
            "required": ["tasks"],
        },
    }
]


def _get_router():
    try:
        import tool_server
        if tool_server._router is not None:
            return tool_server._router
    except Exception:
        pass
    from config import CONFIG
    from core.command_router import CommandRouter
    return CommandRouter(CONFIG)


def _run_one(task: str, context: str, model: str, key: str) -> str:
    _local.depth = 1   # mark this worker thread so nested spawn_agents is refused
    try:
        import chat_backend
        router = _get_router()
        prompt = (f"{context}\n\n---\n\nTask: {task}" if context else task)
        prompt += ("\n\nYou are a focused sub-agent. Use your tools to complete ONLY this "
                   "task, then reply with a concise, self-contained findings summary.")
        res = chat_backend.chat(
            router, f"subagent-{uuid.uuid4().hex[:8]}", prompt,
            "gemini", model, key, allow_actions=False, effort="standard",
        )
        reply = (res.get("reply") or "").strip()
        return reply[:_PER_AGENT_CHARS] or "(no findings)"
    except Exception as e:
        logger.warning(f"sub-agent failed: {e}")
        return f"(sub-agent error: {e})"
    finally:
        _local.depth = 0


def spawn_agents(tasks, context: str = "") -> str:
    """Run up to _MAX focused sub-agents in parallel; return their combined findings."""
    if getattr(_local, "depth", 0) >= 1:
        return "[Sub-agents cannot spawn further sub-agents.]"

    if isinstance(tasks, str):
        tasks = [tasks]
    tasks = [str(t).strip() for t in (tasks or []) if str(t).strip()][:_MAX]
    if not tasks:
        return "No tasks were given to delegate."

    from config import CONFIG
    key = getattr(CONFIG, "gemini_api_key", "")
    if not key:
        return "Sub-agents need the Gemini key (LLM_API_KEY) configured."
    model = "gemini-2.5-flash"
    context = (context or "").strip()

    results = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=min(_MAX, len(tasks))) as ex:
        futs = {ex.submit(_run_one, t, context, model, key): i for i, t in enumerate(tasks)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                results[i] = f"(sub-agent error: {e})"

    out = [f"Delegated to {len(tasks)} sub-agent(s):"]
    for i, (task, res) in enumerate(zip(tasks, results), 1):
        out.append(f"\n### Sub-agent {i}: {task[:80]}\n{res}")
    return "\n".join(out)
