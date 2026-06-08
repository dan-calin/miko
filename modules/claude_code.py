"""
modules/claude_code.py — Miko's pair-programming companion: drive Claude Code (CLI).

Miko = boss / instructor, Claude Code = the coder. Miko researches and plans, then
runs a back-and-forth dev session with Claude Code inside a target repo: Miko gives an
instruction, Claude implements it and reports back, Miko reviews and pushes back, and
they iterate until a TWO-WAY HANDSHAKE — both sides agree the goal is met.

Modes:
  - autonomous : Miko drives every round automatically until the handshake.
  - controlled : the loop pauses after each Claude turn for the user to approve the
                 next round (or revert that turn).

Every round is git-checkpointed, so any change Claude makes is revertible from the UI
(like /undo). Transport is the installed `claude` CLI in headless mode
(-p --output-format json) with a fixed --session-id so turns resume one conversation.

This module is the engine (CLI driver + checkpoints + session state). The orchestrator
loop and tools are in the second half.
"""

import json
import logging
import os
import shutil
import subprocess
import uuid

logger = logging.getLogger("miko.claudecode")


# ── Claude Code CLI driver ──────────────────────────────────────────────────────

def _claude_bin() -> str:
    return shutil.which("claude") or "claude"


def cli_available() -> bool:
    return shutil.which("claude") is not None


def _run_claude(repo: str, prompt: str, session_id: str, resume: bool,
                model: str = "", timeout: int = 1200, effort: str = "") -> dict:
    """One headless Claude Code turn in `repo`. Prompt is fed via stdin to avoid any
    argv-quoting issues (important on Windows). Returns a normalized dict.
    `model`: alias (haiku/sonnet/opus) or full id — Haiku is much cheaper.
    `effort`: low/medium/high/xhigh/max — the coder's thinking budget for the turn."""
    argv = [_claude_bin(), "-p", "--output-format", "json",
            "--dangerously-skip-permissions", "--add-dir", repo]
    if resume:
        argv += ["--resume", session_id]
    else:
        argv += ["--session-id", session_id]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]

    try:
        proc = subprocess.run(
            argv, cwd=repo, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace",   # Claude emits UTF-8; Windows defaults
            timeout=timeout,                       # to cp1252 and crashes the reader thread
            # On Windows `claude` is a .cmd shim; shell=True lets it resolve.
            shell=(os.name == "nt"),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "result": f"(Claude Code timed out after {timeout}s)", "session_id": session_id}
    except Exception as e:
        return {"ok": False, "result": f"(failed to launch Claude Code: {e})", "session_id": session_id}

    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip()[:400]
        return {"ok": False, "result": f"(Claude Code returned nothing. {err})", "session_id": session_id}
    try:
        d = json.loads(out)
    except Exception:
        return {"ok": True, "result": out[:6000], "session_id": session_id}
    return {
        "ok": not d.get("is_error", False),
        "result": str(d.get("result", "")).strip(),
        "session_id": d.get("session_id", session_id),
        "cost": d.get("total_cost_usd", 0),
        "denials": d.get("permission_denials", []),
    }


# ── Alternate coder: Aider (multi-provider) ─────────────────────────────────────

def aider_available() -> bool:
    return shutil.which("aider") is not None


def _provider_env(model: str) -> dict:
    """Map Miko's .env keys to the env vars Aider expects, by the chosen model's provider —
    so the user doesn't enter an API key twice. Returns overrides merged over os.environ."""
    from config import CONFIG
    env, m = {}, (model or "").lower()
    if "gemini" in m or m.startswith("google"):
        if CONFIG.gemini_api_key:
            env["GEMINI_API_KEY"] = CONFIG.gemini_api_key
    elif "minimax" in m:                       # MiniMax via its OpenAI-compatible endpoint
        if getattr(CONFIG, "minimax_api_key", ""):
            env["OPENAI_API_KEY"] = CONFIG.minimax_api_key
            env["OPENAI_API_BASE"] = getattr(CONFIG, "minimax_base_url", "")
    # anthropic/openai/deepseek/etc.: Aider reads ANTHROPIC_API_KEY / OPENAI_API_KEY /
    # DEEPSEEK_API_KEY straight from the environment if you've exported them in .env.
    return env


def _run_aider(repo, prompt, model="", effort="", resume=False, timeout=1200) -> dict:
    """One headless Aider turn — the multi-provider 'Custom Provider' coder. Requires
    `pip install aider-chat`. Pulls provider keys from .env via _provider_env."""
    import tempfile
    aider = shutil.which("aider")
    if not aider:
        return {"ok": False, "result": "(Aider not installed — run: pip install aider-chat)",
                "session_id": ""}
    tf = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    tf.write(prompt); tf.close()
    argv = [aider, "--message-file", tf.name, "--yes-always", "--no-auto-commit",
            "--no-pretty", "--no-stream"]
    if model:
        argv += ["--model", model]
    if effort:                                 # aider maps to the model's reasoning effort
        argv += ["--reasoning-effort", "high" if effort in ("xhigh", "max") else effort]
    if resume:
        argv += ["--restore-chat-history"]     # reload the repo's .aider chat history
    env = {**os.environ, **_provider_env(model)}
    try:
        proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              shell=(os.name == "nt"), env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "result": f"(Aider timed out after {timeout}s)", "session_id": ""}
    except Exception as e:
        return {"ok": False, "result": f"(failed to launch Aider: {e})", "session_id": ""}
    finally:
        try:
            os.unlink(tf.name)
        except Exception:
            pass
    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": False, "result": f"(Aider returned nothing. {(proc.stderr or '')[:400]})",
                "session_id": ""}
    return {"ok": True, "result": out[:8000], "session_id": ""}


def _run_coder(state, prompt, resume, timeout=1200) -> dict:
    """Dispatch a coder turn to the session's chosen CLI (Claude Code or Aider), with its
    chosen model + effort."""
    model, effort = state.get("coder_model", ""), state.get("coder_effort", "")
    if state.get("coder") == "aider":
        return _run_aider(state["repo"], prompt, model, effort, resume, timeout)
    return _run_claude(state["repo"], prompt, state["claude_session"], resume,
                       model, timeout, effort)


# ── Git checkpoints / revert ────────────────────────────────────────────────────

def _git(repo: str, *args, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          shell=(os.name == "nt"), check=check)


def is_git_repo(repo: str) -> bool:
    r = _git(repo, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and "true" in (r.stdout or "")


def ensure_git(repo: str) -> bool:
    """Make `repo` a git repo (so checkpoints/revert work). Returns True if usable."""
    if is_git_repo(repo):
        return True
    if _git(repo, "init").returncode != 0:
        return False
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=miko@local", "-c", "user.name=Miko",
         "commit", "-m", "miko: initial checkpoint")
    return is_git_repo(repo)


def checkpoint(repo: str) -> str:
    """Snapshot the full working tree (incl. new files) as a dangling commit, WITHOUT
    disturbing the working tree or HEAD. Returns the snapshot SHA ('' on failure)."""
    if not is_git_repo(repo):
        return ""
    if _git(repo, "add", "-A").returncode != 0:
        return ""
    tree = _git(repo, "write-tree").stdout.strip()
    if not tree:
        _git(repo, "reset", "-q")
        return ""
    head = _git(repo, "rev-parse", "HEAD")
    parent = ["-p", head.stdout.strip()] if head.returncode == 0 and head.stdout.strip() else []
    snap = _git(repo, "-c", "user.email=miko@local", "-c", "user.name=Miko",
                "commit-tree", tree, *parent, "-m", "miko-checkpoint").stdout.strip()
    _git(repo, "reset", "-q")   # restore index; working tree untouched
    return snap


def revert_to(repo: str, snap: str) -> bool:
    """Restore the working tree to a checkpoint snapshot (undo Claude's changes).
    Verifies success by re-diffing, since `checkout` exits non-zero when the only
    change was a new untracked file (which `clean` is what actually removes)."""
    if not snap or not is_git_repo(repo):
        return False
    _git(repo, "checkout", "--force", snap, "--", ".")
    _git(repo, "clean", "-fdq")   # remove files created after the snapshot (respects .gitignore)
    return not changed_since(repo, snap)   # success = working tree now matches the snapshot


def changed_since(repo: str, snap: str) -> list:
    """List files changed (added/modified/deleted) since a checkpoint snapshot."""
    if not snap or not is_git_repo(repo):
        return []
    _git(repo, "add", "-A")
    out = _git(repo, "diff", "--cached", "--name-status", snap).stdout
    _git(repo, "reset", "-q")
    files = []
    for line in (out or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            files.append(f"{parts[0]} {parts[1]}")
    return files


# ── Session registry ────────────────────────────────────────────────────────────

_SESSIONS: dict = {}   # token -> state


def _registry_path():
    from config import CONFIG
    return CONFIG.data_dir / "code_sessions.json"


_DEFAULTS = {"history": list, "checkpoints": list, "round": lambda: 0,
             "max_rounds": lambda: 6, "provider": lambda: "gemini", "model": lambda: "",
             "key": lambda: "", "base": lambda: "", "research": lambda: "",
             "mode": lambda: "autonomous", "status": lambda: "ready",
             "coder": lambda: "claude", "coder_model": lambda: "", "coder_effort": lambda: "",
             "framed": lambda: False, "notified": lambda: False}


def _normalize(state: dict, token: str) -> dict:
    """Fill any keys a session might be missing (e.g. loaded from an older on-disk
    registry that didn't persist them) so resuming it can never KeyError."""
    state.setdefault("token", token)
    for k, factory in _DEFAULTS.items():
        if k not in state:
            state[k] = factory()
    return state


def _persist():
    """Persist lightweight session state (so Revert + resume survive restarts)."""
    try:
        from config import CONFIG
        CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
        keys = ("repo", "goal", "mode", "round", "max_rounds", "status",
                "checkpoints", "claude_session", "history",
                "coder", "coder_model", "coder_effort", "framed", "notified")
        slim = {t: {k: s.get(k) for k in keys} for t, s in _SESSIONS.items()}
        _registry_path().write_text(json.dumps(slim, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"persist code sessions failed: {e}")


def _load_registry():
    if _SESSIONS:
        return
    try:
        p = _registry_path()
        if p.exists():
            for tok, s in json.loads(p.read_text(encoding="utf-8")).items():
                _SESSIONS[tok] = _normalize(s, tok)
    except Exception:
        pass


# ── Miko (the instructor) ───────────────────────────────────────────────────────

def _miko(state: dict, user_msg: str) -> str:
    """One reasoning turn for Miko, the lead engineer directing Claude Code."""
    from chat_backend import complete_text
    sys = (
        "You are Miko, the ARCHITECT pairing with Claude Code — a coder that is STRONGER "
        "than you at implementation. You are NOT a superior handing down orders; you are an "
        "ideas-and-direction partner, and this is a DEBATE between peers. You propose "
        "direction, approaches and trade-offs and raise concerns; Claude writes the actual "
        "code and is the final authority on implementation quality. When Claude pushes back "
        "or improves your idea, engage its reasoning honestly and defer to the better call — "
        "don't insist just because you proposed it.\n"
        "You MAY sketch an idea or a little pseudocode to convey INTENT, but present it as a "
        "PROPOSAL, never as finished code to copy-paste verbatim. Pasting code from you (a "
        "smaller model) would lower quality — let the stronger coder write the real thing and "
        "critique your proposal. You do NOT do the implementation yourself.\n"
        f"GOAL: {state['goal']}\n"
        f"PROJECT REPO (the coder already runs commands INSIDE this directory): {state['repo']}\n"
        "Use this real path in any command. NEVER invent a path like /home/user/..., and do NOT "
        "prefix instructions with 'cd' — the coder is already in the repo. The coder CAN run "
        "terminal commands itself (tests, backtests, scripts) and report their output: tell it to "
        "RUN the thing and paste the real result, then react to the ACTUAL output — not to a "
        "hypothetical one.\n"
        + (f"RESEARCH/CONTEXT:\n{state['research'][:4000]}\n" if state.get("research") else "")
        + "Give EXACTLY ONE focused next step or proposal per turn — never bundle multiple "
        "numbered steps (there is always a next turn for the next step). Keep each one "
        "specific, bounded, and COMPLETE (never trail off). "
        "You will be shown a ledger of the instructions you have ALREADY given and the "
        "files already changed. NEVER repeat an instruction that is in that ledger — do "
        "not ask Claude to re-read a file it has already read, or re-locate files it has "
        "already located. If the information already exists in the conversation, USE it "
        "and move the work forward to the next concrete step. "
        "In particular: the checklist, priorities, and any reference document already read "
        "are SETTLED and remain in context — never ask to read, print, list, or re-state "
        "them again. Every turn must be a NEW concrete action (locate something not yet "
        "found, or write/modify code), not a re-confirmation of what is already known. "
        + ("AUTONOMOUS mode: YOU are the decision-maker — the user is NOT in the loop and will "
           "NOT answer questions. If the coder asks you to choose between options or for a "
           "go-ahead (e.g. real vs synthetic data, default a flag), DECIDE it yourself and give "
           "a concrete instruction to proceed; never defer the choice to the user or stall "
           "waiting for approval. "
           if state.get("mode") == "autonomous" else "")
        + "When (and only when) you are convinced the goal is fully and correctly met, reply "
        "with exactly 'DONE: <one-line summary>'."
    )
    try:
        return (complete_text(state.get("provider", "gemini"), state.get("model", ""),
                              state.get("key", ""), state.get("base", ""),
                              sys, user_msg, max_tokens=2000) or "").strip()
    except Exception as e:
        logger.warning(f"miko turn failed: {e}")
        return f"(Miko could not respond: {e})"


def _history_str(state, last_n=6) -> str:
    return "\n\n".join(f"{h['role']}: {h['text'][:1200]}" for h in state["history"][-last_n:])


def _miko_ledger(state, last_n=8) -> str:
    """Compact self-memory for Miko: the instructions it has ALREADY issued, plus the
    files actually changed per round.

    Without this, _miko only sees Claude's latest report and — having no memory of its
    own prior turns — keeps re-issuing settled instructions ('re-read the checklist',
    're-locate the files'), burning an expensive Claude round each time on pure
    re-orientation. The ledger lets Miko see it already covered that ground and advance.
    """
    instrs = [h["text"] for h in state.get("history", []) if h.get("role") == "Miko"]
    parts = []
    if instrs:
        shown = instrs[-last_n:]
        base = len(instrs) - len(shown) + 1
        lines = [f"  {base + i}. {' '.join(t.split())[:160]}" for i, t in enumerate(shown)]
        parts.append(
            "[INSTRUCTIONS YOU HAVE ALREADY GIVEN — do NOT repeat any of these; build on "
            "them and advance the work]\n" + "\n".join(lines)
        )
    done = [f"  R{cp.get('round')}: {', '.join(cp['files'])}"
            for cp in state.get("checkpoints", []) if cp.get("files")]
    if done:
        parts.append("[FILES ALREADY CHANGED]\n" + "\n".join(done[-last_n:]))
    return "\n\n".join(parts)


def _is_redundant(instr: str, state: dict, threshold: float = 0.82, last_n: int = 6) -> bool:
    """Deterministic seatbelt behind the ledger: True if `instr` is a near-verbatim
    repeat of a recent Miko instruction. Catches the literal broken-record case (e.g.
    'read and display the checklist' issued again) so we can re-prompt instead of
    spending an expensive Claude round on it. Lexical only (stdlib difflib) — the
    semantic 'stop re-confirming priorities' case is handled by the system prompt."""
    import difflib
    if not instr or instr.strip().upper().startswith("DONE:"):
        return False
    norm = lambda s: " ".join(s.lower().split())
    cur = norm(instr)
    if len(cur) < 12:
        return False
    prior = [norm(h["text"]) for h in state.get("history", []) if h.get("role") == "Miko"]
    return any(
        difflib.SequenceMatcher(None, cur, p).ratio() >= threshold
        for p in prior[-last_n:]
    )


def start_session(repo, goal, mode="autonomous", research="", provider="gemini",
                  model="", api_key="", base_url="", max_rounds=6,
                  coder="claude", coder_model="", coder_effort="") -> dict:
    """Create a pair-programming session (does not run rounds yet)."""
    repo = os.path.abspath(os.path.expanduser((repo or "").strip()))
    if not os.path.isdir(repo):
        return {"error": f"Not a directory: {repo}"}
    coder = "aider" if coder == "aider" else "claude"
    if coder == "aider" and not aider_available():
        return {"error": "Aider not installed. Install it: pip install aider-chat"}
    if coder == "claude" and not cli_available():
        return {"error": "Claude Code CLI not found. Install it: npm i -g @anthropic-ai/claude-code"}
    if not ensure_git(repo):
        return {"error": f"Could not initialise git in {repo} (needed for revert)."}
    token = "cc-" + uuid.uuid4().hex[:10]
    _SESSIONS[token] = {
        "token": token, "repo": repo, "goal": (goal or "").strip(),
        "mode": ("controlled" if mode == "controlled" else "autonomous"),
        "claude_session": str(uuid.uuid4()), "round": 0, "history": [],
        "checkpoints": [], "status": "ready",
        "provider": provider, "model": model, "key": api_key, "base": base_url,
        "research": research or "", "max_rounds": int(max_rounds or 6),
        "coder": coder, "coder_model": coder_model or "", "coder_effort": coder_effort or "",
        "framed": False,
    }
    _persist()
    return {"token": token, "repo": repo, "mode": _SESSIONS[token]["mode"]}


_CLAUDE_FRAME = (
    "[Pairing context] You are the SENIOR IMPLEMENTER, paired with Miko — an architect on a "
    "smaller model who sets direction and proposes ideas. You are the stronger coder. Treat "
    "Miko's instructions and any code sketches as PROPOSALS, not gospel: write your own clean "
    "implementation rather than pasting Miko's snippets, and when a proposal is wrong, "
    "suboptimal, or risky, push back — decline or improve it and explain WHY. It's a debate "
    "between peers; the goal is the best code, not obedience. When you run tests or commands, "
    "report the REAL output so Miko reasons over actual results.\n\n"
)


def _step(state, should_cancel=None):
    """Run one round as a GENERATOR, yielding each event the moment it happens — so the
    checkpoint and Miko's instruction stream LIVE, before the slow Claude call, instead of
    all flushing together at the end of the round. Sets state['status'] on a terminal round."""
    repo = state["repo"]
    rnd = state["round"] + 1
    state["round"] = rnd

    snap = checkpoint(repo)
    state["checkpoints"].append({"round": rnd, "snap": snap, "files": []})
    yield {"type": "checkpoint", "round": rnd, "snap": snap}

    # The user (Miko's boss) may inject direction for this round.
    guide = (state.pop("guidance", "") or "").strip()
    guide_block = (f"\n\nThe user (your boss) gives this direction for THIS step — follow "
                   f"it: {guide}") if guide else ""

    # 1) Miko's instruction for this round.
    if rnd == 1:
        instr = _miko(state, "Begin. Give Claude its first concrete instruction toward the goal." + guide_block)
    else:
        last = state["history"][-1]["text"] if state["history"] else ""
        files = state["checkpoints"][-2]["files"] if len(state["checkpoints"]) > 1 else []
        ledger = _miko_ledger(state)
        ledger_block = f"{ledger}\n\n" if ledger else ""
        base_msg = (
            f"{ledger_block}"
            f"Claude's last report:\n{last[:2500]}\n\nFiles changed last round: {files}\n\n"
            "Review it. Give the next concrete instruction that ADVANCES the work (not one "
            "already in your ledger above), or reply 'DONE: ...' if the goal is fully met."
            + guide_block)
        instr = _miko(state, base_msg)
        # Seatbelt behind the ledger: if Miko still produced a near-verbatim repeat,
        # push it once for a distinct next step before spending a Claude round on it.
        if _is_redundant(instr, state):
            logger.info("Miko instruction was redundant — re-prompting for a distinct step.")
            instr = _miko(state, base_msg + "\n\nNOTE: that repeats an instruction you "
                          "already gave. Do NOT re-read, re-list, or re-locate anything "
                          "already covered. Give a DIFFERENT concrete action that moves the "
                          "work FORWARD, or 'DONE: ...'.")
    state["history"].append({"role": "Miko", "text": instr})
    yield {"type": "miko", "round": rnd, "text": instr}

    # Two-way handshake: Miko proposes completion → Claude must AGREE. Both agreeing
    # ends the session; if Claude objects, Miko addresses it and the loop continues.
    if instr.strip().upper().startswith("DONE:"):
        summary = instr.split(":", 1)[1].strip()
        confirm = _run_coder(state,
            f"Miko (the lead) considers the goal complete: {summary}\n\nDo you AGREE the goal "
            f"'{state['goal']}' is fully and correctly met? Start your reply with 'AGREED' if "
            "yes, or 'NOT DONE:' followed by exactly what is still missing. Do not change code "
            "unless truly needed.", resume=(rnd > 1))
        state["history"].append({"role": "Claude", "text": confirm["result"]})
        yield {"type": "claude", "round": rnd, "text": confirm["result"], "files": []}
        if confirm["result"].strip().upper().startswith("AGREED"):
            state["status"] = "done"
            yield {"type": "done", "rounds": rnd, "summary": summary}
            return
        # Claude objects — Miko turns the objection into the next instruction.
        instr = _miko(state,
            f"Claude does NOT agree it's done:\n{confirm['result'][:2000]}\n\n"
            "Give the next concrete instruction to address what's missing.")
        state["history"].append({"role": "Miko", "text": instr})
        yield {"type": "miko", "round": rnd, "text": instr}

    if should_cancel and should_cancel():
        state["status"] = "cancelled"
        yield {"type": "cancelled"}
        return

    # 2) Claude implements. Prepend the collaboration frame ONCE per session (Claude keeps it
    # via --resume) — keyed off a flag, not round 1, so a resumed/old session still gets it.
    if not state.get("framed"):
        prompt = _CLAUDE_FRAME + instr
        state["framed"] = True
    else:
        prompt = instr
    res = _run_coder(state, prompt, resume=(rnd > 1))
    state["history"].append({"role": "Claude", "text": res["result"]})
    files = changed_since(repo, snap)
    state["checkpoints"][-1]["files"] = files
    yield {"type": "claude", "round": rnd, "text": res["result"], "files": files}


def _notify_done(state: dict, summary: str = "") -> None:
    """Ping the owner on Discord when an AUTONOMOUS pair session reaches a terminal
    state — so the user can walk away and get told when it's finished. Fires at most
    once per session (guarded by state['notified']); best-effort, never raises."""
    if state.get("mode") != "autonomous" or state.get("notified"):
        return
    state["notified"] = True
    try:
        from config import CONFIG
        from modules.discord_bot import send_dm_direct
        repo = state.get("repo", "")
        files = sorted({f for cp in state.get("checkpoints", []) for f in cp.get("files", [])})
        status = state.get("status", "done")
        verb = "finished" if status == "done" else status
        head = f"🤝 Pair session {verb} — {os.path.basename(repo.rstrip('/\\')) or repo}"
        lines = [head, f"Goal: {state.get('goal', '')[:160]}",
                 f"Rounds: {state.get('round', 0)}"]
        if summary and summary not in ("round limit reached", "already complete"):
            lines.append(f"Summary: {summary[:300]}")
        elif summary:
            lines.append(f"Ended: {summary}")
        if files:
            shown = ", ".join(files[:8]) + (f" (+{len(files) - 8} more)" if len(files) > 8 else "")
            lines.append(f"Files changed ({len(files)}): {shown}")
        else:
            lines.append("Files changed: none")
        send_dm_direct(recipient_name=CONFIG.owner_name, message="\n".join(lines))
        logger.info(f"Pair-done DM sent to {CONFIG.owner_name} (status={status})")
    except Exception as e:
        logger.warning(f"pair-done notify failed: {e}")


def run(token, should_cancel=None, guidance=""):
    """Generator yielding the live pair-programming events. Autonomous mode loops to the
    handshake; controlled mode runs one round then pauses for approval. `guidance` is
    optional user direction injected into the next round."""
    _load_registry()
    state = _SESSIONS.get(token)
    if not state:
        yield {"type": "error", "error": "Unknown session."}
        return
    if guidance:
        state["guidance"] = guidance
        # Reopen a finished/maxed-out session when the user gives a fresh instruction —
        # the Claude session still remembers the repo, so we just extend the round budget.
        # Clear the notify latch so the next completion pings again.
        state["notified"] = False
        if state.get("round", 0) >= state.get("max_rounds", 6):
            state["max_rounds"] = state.get("round", 0) + 6
    elif state.get("status") == "done":
        # No new instruction and already finished → nothing to do.
        yield {"type": "done", "rounds": state.get("round", 0), "summary": "already complete"}
        return
    state["status"] = "running"
    yield {"type": "status", "text": f"Pairing on: {state['goal'][:80]}"}

    last_summary = ""
    while True:
        if should_cancel and should_cancel():
            state["status"] = "cancelled"; _notify_done(state, "cancelled"); _persist()
            yield {"type": "cancelled"}; return
        for ev in _step(state, should_cancel):
            yield ev
            if ev.get("type") == "done":
                last_summary = ev.get("summary", "")
            _persist()   # persist after each event so a mid-round reload sees Miko's message
        if state["status"] in ("done", "cancelled"):
            _notify_done(state, last_summary); _persist()
            return
        if state["round"] >= state["max_rounds"]:
            state["status"] = "done"; _notify_done(state, "round limit reached"); _persist()
            yield {"type": "done", "rounds": state["round"], "summary": "round limit reached"}
            return
        if state["mode"] == "controlled":
            state["status"] = "awaiting"; _persist()
            yield {"type": "awaiting", "round": state["round"]}
            return   # wait for the user to approve (a /continue call resumes)


def revert_round(token, snap="") -> str:
    """Revert the repo to a checkpoint (the given snap, or the latest)."""
    _load_registry()
    state = _SESSIONS.get(token)
    if not state:
        return "Unknown session."
    cps = state.get("checkpoints", [])
    if not cps:
        return "No checkpoints to revert to."
    target = snap or cps[-1]["snap"]
    ok = revert_to(state["repo"], target)
    return ("Reverted the repo to the checkpoint." if ok else "Revert failed.")


def _session_view(tok: str, s: dict) -> dict:
    return {
        "token": s.get("token", tok), "repo": s["repo"], "goal": s["goal"],
        "mode": s["mode"], "status": s["status"], "round": s.get("round", 0),
        "history": s.get("history", []), "checkpoints": s.get("checkpoints", []),
    }


def get_active_session() -> dict:
    """The most recent resumable session (ready/running/awaiting) whose repo still exists,
    so the UI can re-render and continue it after a page refresh. {} if none."""
    _load_registry()
    best_tok, best = None, None
    for tok, s in _SESSIONS.items():   # dict preserves insertion order → last started wins
        if s.get("status") in ("ready", "running", "awaiting") and os.path.isdir(s.get("repo", "")):
            best_tok, best = tok, s
    return _session_view(best_tok, best) if best else {}


def get_session(token: str) -> dict:
    """Full view of one session (for resuming a specific session from the list)."""
    _load_registry()
    s = _SESSIONS.get(token)
    return _session_view(token, s) if s else {}


def list_sessions() -> list:
    """Compact summaries of all pair sessions, newest first, for the UI's session list."""
    _load_registry()
    out = []
    for tok, s in _SESSIONS.items():
        out.append({
            "token": s.get("token", tok), "repo": s["repo"],
            "goal": s.get("goal", ""), "status": s.get("status", "ready"),
            "round": s.get("round", 0), "exists": os.path.isdir(s.get("repo", "")),
        })
    out.reverse()   # most recent first
    return out


def forget_session(token: str) -> bool:
    """Remove a session from the registry (does not touch the repo)."""
    _load_registry()
    if token in _SESSIONS:
        del _SESSIONS[token]
        _persist()
        return True
    return False


def recap(token: str) -> str:
    """Ask Claude (via its remembered session) to summarize what's been done so far —
    so a resumed session shows where it left off even if the transcript wasn't saved."""
    _load_registry()
    s = _SESSIONS.get(token)
    if not s:
        return "Unknown session."
    if not s.get("claude_session") or not os.path.isdir(s.get("repo", "")):
        return "Nothing to recap (no Claude session / repo missing)."
    res = _run_claude(
        s["repo"],
        "Concisely recap what we have accomplished in THIS coding session so far, and the "
        "current state of the work — so we know where to continue. Do not change anything.",
        s["claude_session"], resume=True, timeout=300)
    txt = res.get("result", "").strip() or "(no recap returned)"
    s.setdefault("history", []).append({"role": "Claude", "text": txt})
    _persist()
    return txt


# ── Tool (mid-chat / voice): run an autonomous session to completion ────────────

TOOL_DECLARATIONS = [
    {
        "name": "code_with_claude",
        "description": (
            "Start a pair-programming session where Miko directs Claude Code (a full "
            "coder) to build/modify a real project. Miko instructs, Claude implements, "
            "they iterate until both agree it's done. Use after researching/planning, when "
            "the user wants actual code written or a repo changed. Every round is "
            "git-checkpointed so changes can be reverted. Returns the dev transcript + "
            "outcome. (For watching it live with approve/revert controls, the Chat UI's "
            "pair-programming panel is better.)"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "project_dir": {"type": "STRING", "description": "Absolute path to the project/repo directory."},
                "goal": {"type": "STRING", "description": "What to build or change (be specific)."},
                "research": {"type": "STRING", "description": "Optional findings/context to hand Claude."},
                "max_rounds": {"type": "INTEGER", "description": "Max back-and-forth rounds (default 5)."},
            },
            "required": ["project_dir", "goal"],
        },
    }
]


def code_with_claude(project_dir: str, goal: str, research: str = "", max_rounds: int = 5) -> str:
    from config import CONFIG
    started = start_session(
        project_dir, goal, mode="autonomous", research=research,
        provider="gemini", model="gemini-3.5-flash",
        api_key=getattr(CONFIG, "gemini_api_key", ""), max_rounds=max_rounds)
    if started.get("error"):
        return started["error"]
    token = started["token"]
    lines = [f"Pair-programming session in {started['repo']} (revert token: {token})"]
    for ev in run(token):
        t = ev.get("type")
        if t == "miko":
            lines.append(f"\n[Miko → Claude, round {ev['round']}]\n{ev['text']}")
        elif t == "claude":
            lines.append(f"\n[Claude → Miko, round {ev['round']}]\n{ev['text']}"
                         + (f"\n(files: {', '.join(ev['files'])})" if ev.get("files") else ""))
        elif t == "done":
            lines.append(f"\n✅ Done after {ev['rounds']} round(s): {ev.get('summary','')}")
        elif t == "error":
            lines.append(f"\n⚠ {ev['error']}")
    lines.append(f"\nTo undo everything, revert the session ({token}).")
    return "\n".join(lines)[:6000]

