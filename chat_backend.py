"""
chat_backend.py — Model-agnostic chat with Miko's tools.

Powers the web Chat UI. Lets the user talk to Miko by text using the provider of
their choice (Gemini, MiniMax/Anthropic, OpenAI, DeepSeek, Kimi, or a custom
OpenAI-compatible endpoint). Miko's full tool set is exposed to the model, so it
can control the PC, Discord, calendars, etc. — the same tools the voice agent uses.

Three wire protocols cover every provider:
  - "gemini"    → google-genai
  - "anthropic" → anthropic SDK (Messages API)         [MiniMax /anthropic]
  - "openai"    → openai SDK (Chat Completions)         [OpenAI, DeepSeek, Kimi, custom]

Tool execution goes through CommandRouter. Read-only tools always run; sensitive
tools (delete, send message, shutdown, …) require the per-session "allow actions"
flag, since a text UI has no voice-confirmation step.
"""

import json
import logging
import os

logger = logging.getLogger("miko.chat")

_MAX_ROUNDS = 6
_MAX_HISTORY = 30  # neutral messages handed to the model per turn

# Conversations are persisted to disk — see conversation_store.py.


# ── Provider presets ──────────────────────────────────────────────────────────
# env_key: the .env variable holding the API key (used if the UI doesn't supply one).
PROVIDERS = {
    "gemini": {
        "label": "Google Gemini",
        "protocol": "gemini",
        "base_url": "",
        "env_key": "LLM_API_KEY",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
    },
    "minimax": {
        "label": "MiniMax",
        "protocol": "anthropic",
        "base_url": "https://api.minimax.io/anthropic",
        "env_key": "MINIMAX_API_KEY",
        "models": ["MiniMax-M2.7", "MiniMax-Text-01"],
    },
    "openai": {
        "label": "OpenAI",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"],
    },
    "deepseek": {
        "label": "DeepSeek",
        "protocol": "openai",
        "base_url": "https://api.deepseek.com",
        "env_key": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "kimi": {
        "label": "Kimi (Moonshot)",
        "protocol": "openai",
        "base_url": "https://api.moonshot.ai/v1",
        "env_key": "MOONSHOT_API_KEY",
        "models": ["kimi-k2-0905-preview", "moonshot-v1-8k", "moonshot-v1-32k"],
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "protocol": "openai",
        "base_url": "",
        "env_key": "",
        "models": [],
    },
}


def list_models() -> dict:
    """Return provider presets + whether each has a key configured in .env."""
    out = {}
    for pid, p in PROVIDERS.items():
        out[pid] = {
            "label": p["label"],
            "protocol": p["protocol"],
            "base_url": p["base_url"],
            "models": p["models"],
            "env_key": p["env_key"],
            "has_env_key": bool(p["env_key"] and os.getenv(p["env_key"], "")),
            "needs_base_url": pid == "custom",
        }
    return out


def complete_text(provider: str, model: str = "", api_key: str = "", base_url: str = "",
                  system: str = "", user: str = "", max_tokens: int = 2048) -> str:
    """One-shot text completion (no tools) for any provider — used by pipelines such
    as deep research that need the model to plan or synthesize. Raises on no key."""
    preset = PROVIDERS.get(provider) or PROVIDERS["gemini"]
    key = (api_key or "").strip() or os.getenv(preset["env_key"], "")
    if not key:
        raise RuntimeError(f"No API key for {preset['label']}.")
    base = (base_url or "").strip() or preset["base_url"]
    model = model or (preset["models"] or [""])[0]
    proto = preset["protocol"]

    if proto == "gemini":
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        cfg = types.GenerateContentConfig(system_instruction=system) if system else None
        resp = client.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text=user)])],
            config=cfg,
        )
        cand = resp.candidates[0] if resp.candidates else None
        if cand and cand.content and cand.content.parts:
            return " ".join(p.text for p in cand.content.parts if p.text).strip()
        return ""

    if proto == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=key, base_url=base or None)
        kwargs = {"model": model, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": user}]}
        if system:
            kwargs["system"] = system
        resp = client.messages.create(**kwargs)
        return " ".join(b.text for b in resp.content if hasattr(b, "text")).strip()

    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base or None)
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": user})
    resp = client.chat.completions.create(model=model, messages=msgs)
    return (resp.choices[0].message.content or "").strip()


# ── .env read/write (settings panel) ──────────────────────────────────────────
# The Chat UI can read and persist API keys straight into the project's .env so
# they survive restarts. This is a self-hosted, single-user tool — keys live in
# plain text in .env (already git-ignored), same as if you edited the file by hand.

from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent / ".env"

# Only these keys are exposed to the UI editor — the chat provider API keys.
EDITABLE_ENV_KEYS = [
    "LLM_API_KEY", "MINIMAX_API_KEY", "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY",
]


def read_env_keys() -> dict:
    """Return current values for the editable env keys (from the live process env)."""
    return {k: os.getenv(k, "") for k in EDITABLE_ENV_KEYS}


def write_env_keys(updates: dict) -> dict:
    """
    Persist the given {KEY: value} pairs to .env (creating it if needed) and update
    the live process env so the change takes effect immediately. Only keys in
    EDITABLE_ENV_KEYS are honoured. Returns the updated values.
    """
    clean = {k: str(v) for k, v in (updates or {}).items() if k in EDITABLE_ENV_KEYS}
    if not clean:
        return read_env_keys()

    # Read existing lines (preserve comments / ordering / unrelated keys).
    lines = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()

    seen = set()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name in clean:
            lines[i] = f"{name}={clean[name]}"
            seen.add(name)

    # Append any keys that weren't already present.
    missing = [k for k in clean if k not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        for k in missing:
            lines.append(f"{k}={clean[k]}")

    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Reflect immediately in the running process.
    for k, v in clean.items():
        os.environ[k] = v

    logger.info(f"[chat] wrote {len(clean)} key(s) to .env: {list(clean.keys())}")
    return read_env_keys()


# ── System prompt ─────────────────────────────────────────────────────────────

def _system_prompt(owner_name: str, language: str, workspace: str = "") -> str:
    if language == "ro":
        base = (
            f"Ești Miko, asistentul personal al lui {owner_name}, accesibil printr-un chat text. "
            "Ai acces la unelte care controlează PC-ul Windows al userului, Discord, calendarele, "
            "căutarea web, notițe și fișiere. Folosește uneltele când e nevoie; nu inventa rezultate. "
            "Răspunde scurt și la obiect. Răspunde în română dacă userul scrie în română."
        )
        if workspace:
            base += (
                f" Lucrezi acum în folderul ales de user (workspace-ul curent): {workspace}. "
                "Când creezi, citești sau rulezi fișiere fără o cale completă, folosește acest folder."
            )
        return base
    base = (
        f"You are Miko, {owner_name}'s personal assistant, reachable through a text chat. "
        "You have tools that control the user's Windows PC, Discord, calendars, web search, "
        "notes, and files. Use the tools when needed; never make up results. Be concise and "
        "direct. Reply in the language the user writes in."
    )
    if workspace:
        base += (
            f" You are currently working in the user's selected workspace folder: {workspace}. "
            "When creating, reading, or running files without an explicit full path, use this folder."
        )
    return base


# ── Long-term memory + semantic recall (the "second brain") ───────────────────

def _memory_context(message: str) -> str:
    """Build the memory addendum for the system prompt: the user's known facts,
    plus any vault notes semantically relevant to this message. Best-effort."""
    from config import CONFIG
    from memory.memory_manager import load_memory, format_memory_for_prompt

    parts = []
    facts = format_memory_for_prompt(load_memory(CONFIG.memory_file))
    if facts:
        parts.append(facts.strip())

    # Ranked recall across notes/episodes/insights, capped to a tight token budget
    # (~350 tokens ≈ 1400 chars) so per-turn cost stays small regardless of vault size.
    try:
        from memory import knowledge_store as KS
        hits = KS.search(message, k=6, kinds=["note", "episode", "insight"])
        budget, used, lines = 1400, 0, []
        for h in hits:
            snippet = h["text"][:220].strip()
            src = ""
            if h["kind"] == "note":
                src = " (" + os.path.basename(h["ref"].split("#")[0]) + ")"
            line = f"- {snippet}{src}"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line)
        if lines:
            parts.append("[RELEVANT MEMORY]\n" + "\n".join(lines))
    except Exception as e:
        logger.warning(f"recall failed: {e}")

    try:   # today's schedule (cached by the briefs daemon — no live calendar call)
        import schedule_briefs
        brief = schedule_briefs.get_today_brief()
        if brief:
            parts.append("[TODAY'S SCHEDULE]\n" + brief)
    except Exception:
        pass

    try:   # what the user is building (details are recall-able vault notes)
        import modules.projects as PR
        pl = PR.get_active_projects_line()
        if pl:
            parts.append("[" + pl + "]")
    except Exception:
        pass

    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def _learn_async(user_msg: str, assistant_msg: str, session_id: str = "") -> None:
    """Extract durable facts + an episodic summary from a chat turn (throttled, in a
    daemon thread) — the same learner the voice agent uses, now wired into chat too."""
    try:
        from config import CONFIG
        from memory.memory_manager import update_from_conversation_async
        update_from_conversation_async(
            CONFIG.memory_file, CONFIG.gemini_api_key, user_msg, assistant_msg,
            minimax_api_key=getattr(CONFIG, "minimax_api_key", ""),
            minimax_base_url=getattr(CONFIG, "minimax_base_url", ""),
            minimax_model=getattr(CONFIG, "minimax_model", ""),
            session_id=session_id,
        )
    except Exception as e:
        logger.warning(f"chat learn failed: {e}")


# ── History helpers (persistent — see conversation_store.py) ──────────────────

def _get_history(session_id: str) -> list:
    import conversation_store as convo
    return convo.history_for_model(session_id, _MAX_HISTORY)


def _save_turn(session_id: str, user_msg: str, assistant_msg: str,
               tools: list, files: list) -> None:
    import conversation_store as convo
    convo.append_turn(session_id, user_msg, assistant_msg, tools, files)


def reset_session(session_id: str) -> None:
    import conversation_store as convo
    convo.clear(session_id)


# ── Tool execution ────────────────────────────────────────────────────────────

# Matches an absolute Windows path ending in a file extension, e.g. C:\Users\me\a.py
import re as _re
_PATH_RE = _re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]+\.[A-Za-z0-9]{1,8}")
# Tool-argument keys that commonly hold a file path.
_PATH_ARG_KEYS = ("path", "destination", "filepath", "file", "filename", "dest")


def _collect_files(name: str, args: dict, result: str, files: list) -> None:
    """Record real on-disk files this tool touched, so the UI can link/open them.

    Looks at both the tool's arguments (the reliable source — the model passed the
    path) and any absolute path echoed in the result string. Only paths that exist
    as files are kept; results are de-duplicated, order-preserving.
    """
    candidates: list[str] = []
    for k in _PATH_ARG_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())
    if isinstance(result, str):
        candidates.extend(_PATH_RE.findall(result))

    seen = {f["path"].lower() for f in files}
    for c in candidates:
        try:
            if not os.path.isfile(c):
                continue
            full = os.path.abspath(c)
        except (OSError, ValueError):
            continue
        if full.lower() in seen:
            continue
        seen.add(full.lower())
        files.append({"path": full, "name": os.path.basename(full)})


def _run_tool(router, name: str, args: dict, allow_actions: bool,
              used: list, files: list) -> str:
    from core.command_router import REQUIRES_CONFIRMATION

    used.append(name)
    safe, reason = router._safety_check(name, args)
    if not safe:
        return f"[blocked for security: {reason}]"
    if name in REQUIRES_CONFIRMATION and not allow_actions:
        return (
            f"[blocked] '{name}' is a sensitive action. The user must enable "
            "'Allow actions' in the chat UI before this can run."
        )
    try:
        result = str(router._dispatch_module(name, args))
        _collect_files(name, args, result, files)
        return result
    except Exception as e:
        logger.error(f"chat tool error {name}: {e}", exc_info=True)
        return f"[error running {name}: {e}]"


# ── Public entry point ────────────────────────────────────────────────────────

def chat(router, session_id: str, message: str, provider: str, model: str,
         api_key: str = "", base_url: str = "", allow_actions: bool = False,
         owner_name: str = "Roxan", language: str = "en", workspace: str = "",
         agent: str = "", skills=None) -> dict:
    """Run one chat turn. Returns {"reply": str, "tools_used": [...], "error": str|None}."""
    preset = PROVIDERS.get(provider)
    if not preset:
        return {"reply": "", "tools_used": [], "error": f"Unknown provider '{provider}'."}

    key = (api_key or "").strip() or os.getenv(preset["env_key"], "")
    if not key:
        return {"reply": "", "tools_used": [],
                "error": f"No API key for {preset['label']}. Enter one in the UI or set "
                         f"{preset['env_key']} in .env."}

    base = (base_url or "").strip() or preset["base_url"]
    if not model:
        model = (preset["models"] or [""])[0]
    if not model:
        return {"reply": "", "tools_used": [], "error": "No model specified."}

    # "Whose assistant am I" follows the remembered identity name, so the base
    # prompt agrees with memory after the user corrects their name.
    try:
        from config import CONFIG as _C
        from memory.memory_manager import load_memory as _lm
        _nm = _lm(_C.memory_file).get("identity", {}).get("name", {}).get("value")
        if _nm:
            owner_name = _nm
    except Exception:
        pass

    system = _system_prompt(owner_name, language, workspace)
    if agent or skills:
        try:
            import agent_skills
            overlay = agent_skills.build_overlay(agent, skills)
            if overlay:
                system += overlay
        except Exception as e:
            logger.error(f"agent/skills overlay failed: {e}")

    # Inject long-term memory + semantically relevant vault notes.
    try:
        system += _memory_context(message)
    except Exception as e:
        logger.warning(f"memory context failed: {e}")

    history = _get_history(session_id)
    used: list = []
    files: list = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    try:
        if preset["protocol"] == "gemini":
            reply = _run_gemini(router, key, model, system, history, message, allow_actions, used, files, usage)
        elif preset["protocol"] == "anthropic":
            reply = _run_anthropic(router, key, base, model, system, history, message, allow_actions, used, files, usage)
        else:
            reply = _run_openai(router, key, base, model, system, history, message, allow_actions, used, files, usage)
    except Exception as e:
        logger.error(f"chat() error ({provider}/{model}): {e}", exc_info=True)
        return {"reply": "", "tools_used": used, "files": files, "usage": usage, "error": str(e)}

    _save_turn(session_id, message, reply, used, files)
    _learn_async(message, reply, session_id)   # learn facts + episode (throttled)
    return {"reply": reply, "tools_used": used, "files": files, "usage": usage, "error": None}


def _accum_usage(usage: dict, resp, proto: str) -> None:
    """Add a provider response's token counts into the running per-turn total."""
    try:
        if proto == "openai":
            u = getattr(resp, "usage", None)
            if u:
                usage["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                usage["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0
        elif proto == "anthropic":
            u = getattr(resp, "usage", None)
            if u:
                usage["prompt_tokens"] += getattr(u, "input_tokens", 0) or 0
                usage["completion_tokens"] += getattr(u, "output_tokens", 0) or 0
        elif proto == "gemini":
            u = getattr(resp, "usage_metadata", None)
            if u:
                usage["prompt_tokens"] += getattr(u, "prompt_token_count", 0) or 0
                usage["completion_tokens"] += getattr(u, "candidates_token_count", 0) or 0
    except Exception:
        pass
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]


# ── OpenAI-compatible (OpenAI, DeepSeek, Kimi, custom) ────────────────────────

def _run_openai(router, key, base, model, system, history, message, allow_actions, used, files, usage=None) -> str:
    from openai import OpenAI
    from tools import ALL_TOOL_DECLARATIONS_OPENAI

    client = OpenAI(api_key=key, base_url=base or None)
    messages = [{"role": "system", "content": system}]
    messages += [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": message})
    tools = ALL_TOOL_DECLARATIONS_OPENAI or None

    for _ in range(_MAX_ROUNDS):
        kwargs = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = client.chat.completions.create(**kwargs)
        if usage is not None:
            _accum_usage(usage, resp, "openai")
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            return (msg.content or "").strip() or "(no response)"

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            result = _run_tool(router, tc.function.name, args, allow_actions, used, files)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    final = client.chat.completions.create(model=model, messages=messages)
    if usage is not None:
        _accum_usage(usage, final, "openai")
    return (final.choices[0].message.content or "").strip() or "(done)"


# ── Anthropic-compatible (MiniMax /anthropic) ────────────────────────────────

def _run_anthropic(router, key, base, model, system, history, message, allow_actions, used, files, usage=None) -> str:
    import anthropic
    from tools import ALL_TOOL_DECLARATIONS_ANTHROPIC

    client = anthropic.Anthropic(api_key=key, base_url=base or None)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": message})
    tools = ALL_TOOL_DECLARATIONS_ANTHROPIC or None

    for _ in range(_MAX_ROUNDS):
        kwargs = {"model": model, "max_tokens": 4096, "system": system, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        resp = client.messages.create(**kwargs)
        if usage is not None:
            _accum_usage(usage, resp, "anthropic")

        tool_use = [b for b in resp.content if b.type == "tool_use"]
        if not tool_use:
            texts = [b.text for b in resp.content if hasattr(b, "text")]
            return " ".join(texts).strip() or "(no response)"

        assistant_content = []
        for b in resp.content:
            if b.type == "text":
                assistant_content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use", "id": b.id, "name": b.name,
                    "input": dict(b.input) if b.input else {},
                })
        messages.append({"role": "assistant", "content": assistant_content})

        results = []
        for block in tool_use:
            args = dict(block.input) if block.input else {}
            result = _run_tool(router, block.name, args, allow_actions, used, files)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": results})

    final = client.messages.create(model=model, max_tokens=1024, system=system, messages=messages)
    if usage is not None:
        _accum_usage(usage, final, "anthropic")
    texts = [b.text for b in final.content if hasattr(b, "text")]
    return " ".join(texts).strip() or "(done)"


# ── Gemini ────────────────────────────────────────────────────────────────────

def _run_gemini(router, key, model, system, history, message, allow_actions, used, files, usage=None) -> str:
    from google import genai
    from google.genai import types
    from tools import ALL_TOOL_DECLARATIONS

    client = genai.Client(api_key=key)
    contents = [
        types.Content(
            role=("model" if m["role"] == "assistant" else "user"),
            parts=[types.Part(text=m["content"])],
        )
        for m in history
    ]
    contents.append(types.Content(role="user", parts=[types.Part(text=message)]))

    gen_config = types.GenerateContentConfig(
        system_instruction=system,
        tools=[types.Tool(function_declarations=ALL_TOOL_DECLARATIONS)] if ALL_TOOL_DECLARATIONS else [],
    )

    for _ in range(_MAX_ROUNDS):
        resp = client.models.generate_content(model=model, contents=contents, config=gen_config)
        if usage is not None:
            _accum_usage(usage, resp, "gemini")
        candidate = resp.candidates[0] if resp.candidates else None
        if not candidate or not candidate.content or not candidate.content.parts:
            break

        parts = candidate.content.parts
        fc_parts = [p for p in parts if p.function_call is not None]
        if not fc_parts:
            texts = [p.text for p in parts if p.text]
            return " ".join(texts).strip() or "(no response)"

        contents.append(candidate.content)
        response_parts = []
        for part in fc_parts:
            fc = part.function_call
            args = dict(fc.args) if fc.args else {}
            result = _run_tool(router, fc.name, args, allow_actions, used, files)
            response_parts.append(
                types.Part(function_response=types.FunctionResponse(
                    name=fc.name, response={"result": result}))
            )
        contents.append(types.Content(role="user", parts=response_parts))

    return "(done)"
