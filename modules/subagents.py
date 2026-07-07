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
import os

logger = logging.getLogger("miko.subagents")

_MAX = 5   # cap on tasks accepted per spawn_agents call

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


def _provider_config(provider: str, model: str = "", key: str = "", base: str = "") -> tuple[dict | None, str]:
    try:
        import chat_backend
        preset = chat_backend.PROVIDERS.get((provider or "").strip().lower())
    except Exception:
        preset = None
    if not preset:
        return None, f"Unknown sub-agent provider '{provider}'."
    key = (key or "").strip()
    if not key and preset.get("env_key"):
        key = os.getenv(preset["env_key"], "").strip()
    model = (model or "").strip() or ((preset.get("models") or [""])[0] if preset.get("models") else "")
    base = (base or "").strip() or preset.get("base_url", "")
    if not key:
        return None, (
            f"Sub-agents need an API key for {preset.get('label', provider)}. "
            "Set the provider key in Settings, or set MIKO_SUBAGENT_API_KEY for a custom sub-agent model."
        )
    if not model:
        return None, f"Sub-agents need a model for {preset.get('label', provider)}."
    return {"provider": provider, "model": model, "key": key, "base": base}, ""


def _resolve_subagent_model(agent_jobs) -> tuple[dict | None, str]:
    """Choose the model for Miko-launched sub-agents.

    MIKO_SUBAGENT_MODEL_MODE=main (default) inherits the chat/voice model that
    called spawn_agents. MIKO_SUBAGENT_MODEL_MODE=custom uses the dedicated
    MIKO_SUBAGENT_* fields from Settings.
    """
    mode = (os.getenv("MIKO_SUBAGENT_MODEL_MODE", "main") or "main").strip().lower()
    if mode in ("custom", "dedicated", "override", "fixed"):
        provider = os.getenv("MIKO_SUBAGENT_PROVIDER", "").strip().lower()
        model = os.getenv("MIKO_SUBAGENT_MODEL", "").strip()
        key = os.getenv("MIKO_SUBAGENT_API_KEY", "").strip()
        base = os.getenv("MIKO_SUBAGENT_BASE_URL", "").strip()
        if not provider:
            return None, "Set MIKO_SUBAGENT_PROVIDER in Settings, or switch sub-agent model mode back to 'main'."
        return _provider_config(provider, model, key, base)

    active = agent_jobs.current_model()
    provider = (active.get("provider") or "").strip().lower()
    model = (active.get("model") or "").strip()
    key = (active.get("api_key") or "").strip()
    base = (active.get("base_url") or "").strip()
    if provider:
        return _provider_config(provider, model, key, base)

    # If spawn_agents is called outside a chat turn, fall back to the old safe default.
    from config import CONFIG
    return _provider_config("gemini", "gemini-2.5-flash", getattr(CONFIG, "gemini_api_key", ""), "")


def spawn_agents(tasks, context: str = "") -> str:
    """Run up to _MAX focused sub-agents in parallel; return their combined findings.

    Delegates to modules.agent_jobs so the run is recorded + observable in the live
    sub-agent panel (and the per-provider concurrency cap applies), while keeping the
    blocking text contract Miko expects."""
    from modules import agent_jobs
    if agent_jobs.in_subagent():
        return "[Sub-agents cannot spawn further sub-agents.]"

    if isinstance(tasks, str):
        tasks = [tasks]
    tasks = [str(t).strip() for t in (tasks or []) if str(t).strip()][:_MAX]
    if not tasks:
        return "No tasks were given to delegate."

    chosen, err = _resolve_subagent_model(agent_jobs)
    if err:
        return err

    batch = agent_jobs.run_and_wait(tasks, (context or "").strip(),
                                    chosen["provider"], chosen["model"], chosen["key"],
                                    chosen["base"])
    if batch.get("error"):
        return batch["error"]
    out = [f"Delegated to {len(batch['agents'])} sub-agent(s):"]
    for i, a in enumerate(batch["agents"], 1):
        body = a["result"] if a["status"] == "done" else f"({a['status']}: {a.get('error', '') or '—'})"
        out.append(f"\n### Sub-agent {i}: {a['task'][:80]}\n{body}")
    return "\n".join(out)
