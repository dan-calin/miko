"""
tool_server.py — HTTP bridge exposing all Miko tools to external agents (e.g. Hermes on WSL2).

Architecture:
  Hermes (WSL2)  →  POST http://<windows-ip>:7832/tools/{name}  →  Miko modules (Windows)

Endpoints:
  GET  /              — health check
  GET  /tools         — tool schemas (?format=openai|anthropic|gemini)
  POST /tools/{name}  — execute tool, body = JSON args dict

Auth: set TOOL_SERVER_KEY in .env to require  Authorization: Bearer <key>
Port: TOOL_SERVER_PORT (default 7832)
"""

import logging
import os
import threading

logger = logging.getLogger("miko.toolserver")

_router = None


def _ndjson_stream(request, gen_factory, on_event=None):
    """Stream a sync generator's events as NDJSON, with client-disconnect → cancel.

    gen_factory(should_cancel) must return a sync generator of JSON-able event dicts;
    `should_cancel` is a zero-arg callable the generator polls to abort cooperatively.
    on_event(ev) runs per event (for side effects like persisting the final result).
    """
    import asyncio
    import json as _json
    import threading
    from fastapi.responses import StreamingResponse

    cancel = threading.Event()

    async def agen():
        loop = asyncio.get_event_loop()
        gen = gen_factory(cancel.is_set)
        _SENT = object()

        def _nxt():
            try:
                return next(gen)
            except StopIteration:
                return _SENT

        try:
            while True:
                if await request.is_disconnected():
                    cancel.set()
                item = await loop.run_in_executor(None, _nxt)
                if item is _SENT:
                    break
                if on_event:
                    try:
                        on_event(item)
                    except Exception:
                        pass
                yield _json.dumps(item) + "\n"
        finally:
            cancel.set()

    return StreamingResponse(
        agen(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def start(command_router) -> None:
    """Start the tool HTTP server in a daemon thread. Called from main.py."""
    global _router
    _router = command_router

    port = int(os.getenv("TOOL_SERVER_PORT", "7832"))
    host = os.getenv("TOOL_SERVER_HOST", "0.0.0.0")

    threading.Thread(
        target=_run,
        args=(host, port),
        daemon=True,
        name="ToolServer",
    ).start()
    _start_knowledge_index()
    try:
        from config import CONFIG
        from modules.schedule_briefs import start as _start_briefs
        _start_briefs(CONFIG.owner_name)
    except Exception as e:
        logger.warning(f"schedule briefs not started: {e}")
    try:
        from modules.scheduled_tasks import start as _start_tasks
        _start_tasks()
    except Exception as e:
        logger.warning(f"scheduled tasks not started: {e}")
    try:
        from modules.email_watch import start as _start_email_watch, _load as _ew_load
        if any(r.get("active") for r in _ew_load().values()):
            _start_email_watch()   # only spin the IMAP poller if there are live watches
    except Exception as e:
        logger.warning(f"email watch not started: {e}")
    try:
        from modules.mcp_client import start as _start_mcp
        _start_mcp()
    except Exception as e:
        logger.warning(f"MCP client not started: {e}")
    logger.info(f"Tool server listening on {host}:{port}")


def _start_knowledge_index() -> None:
    """Build/refresh the semantic knowledge index (facts + notes vault) in the
    background so it's ready for recall without blocking server startup."""
    def _build():
        try:
            import vault
            from config import CONFIG
            from memory import knowledge_store as KS
            from memory.memory_manager import load_memory
            vault.ensure_structure(CONFIG.notes_dir)   # PARA folders in the vault
            KS.index_facts(load_memory(CONFIG.memory_file))
            KS.reindex_notes(CONFIG.notes_dir)
        except Exception as e:
            logger.warning(f"knowledge index build failed: {e}")

    threading.Thread(target=_build, daemon=True, name="KnowledgeIndex").start()


def _run(host: str, port: int) -> None:
    try:
        import uvicorn
        uvicorn.run(_build_app(), host=host, port=port, log_level="warning")
    except ImportError:
        logger.error("Tool server needs 'fastapi' and 'uvicorn' — run: pip install fastapi uvicorn")
    except Exception as e:
        logger.error(f"Tool server crashed: {e}")


def _build_app():
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Miko Tool Server", version="1.0", docs_url=None)
    _secret = os.getenv("TOOL_SERVER_KEY", "")

    # ── Auth guard ─────────────────────────────────────────────────────────────
    def _auth(request: Request):
        if not _secret:
            return
        auth = request.headers.get("Authorization", "")
        if not auth or auth != f"Bearer {_secret}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    # ── Health ─────────────────────────────────────────────────────────────────
    @app.get("/")
    def health():
        from tools import ALL_TOOL_DECLARATIONS
        return {
            "status": "ok",
            "tool_count": len(ALL_TOOL_DECLARATIONS),
            "auth_required": bool(_secret),
        }

    # ── Tool schema discovery ──────────────────────────────────────────────────
    @app.get("/tools")
    def list_tools(format: str = "openai", _=Depends(_auth)):
        """
        Returns tool schemas for Hermes to register.
        ?format=anthropic  — use this if Hermes is on the MiniMax /anthropic endpoint
        ?format=openai     — OpenAI/compat format (default)
        ?format=gemini     — raw Gemini FunctionDeclaration dicts
        """
        if format == "anthropic":
            from tools import ALL_TOOL_DECLARATIONS_ANTHROPIC
            return ALL_TOOL_DECLARATIONS_ANTHROPIC
        if format == "gemini":
            from tools import ALL_TOOL_DECLARATIONS
            return ALL_TOOL_DECLARATIONS
        from tools import ALL_TOOL_DECLARATIONS_OPENAI
        return ALL_TOOL_DECLARATIONS_OPENAI

    # ── Chat UI ─────────────────────────────────────────────────────────────────
    @app.get("/chat")
    def chat_page():
        from fastapi.responses import HTMLResponse, PlainTextResponse
        from pathlib import Path
        html_path = Path(__file__).resolve().parent / "webui" / "chat.html"
        try:
            # no-store so a server restart always serves the latest UI (no stale cache)
            return HTMLResponse(html_path.read_text(encoding="utf-8"),
                                headers={"Cache-Control": "no-store, max-age=0"})
        except Exception as e:
            return PlainTextResponse(f"Chat UI not found: {e}", status_code=500)

    @app.get("/chat/models")
    def chat_models(_=Depends(_auth)):
        from chat_backend import list_models
        return list_models()

    @app.post("/chat/message")
    async def chat_message(request: Request, _=Depends(_auth)):
        from chat_backend import chat
        from config import CONFIG

        if _router is None:
            raise HTTPException(status_code=503, detail="Router not initialised — Miko not fully started yet")

        try:
            body = await request.json()
        except Exception:
            body = {}

        message = (body.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "Empty message."}, status_code=400)

        import file_browser
        workspace = (body.get("workspace") or "").strip() or file_browser.get_workspace()

        skills = body.get("skills") or []
        if not isinstance(skills, list):
            skills = []

        result = chat(
            router=_router,
            session_id=body.get("session_id", "default"),
            message=message,
            provider=body.get("provider", "gemini"),
            model=body.get("model", ""),
            api_key=body.get("api_key", ""),
            base_url=body.get("base_url", ""),
            allow_actions=bool(body.get("allow_actions", False)),
            owner_name=CONFIG.owner_name,
            language=getattr(CONFIG, "language", "en"),
            workspace=workspace,
            agent=(body.get("agent") or "").strip(),
            skills=skills,
            effort=(body.get("effort") or "standard").strip(),
            approval=bool(body.get("approval", False)),
            thinking=bool(body.get("thinking", False)),
            attachments=body.get("attachments") or [],
        )
        return JSONResponse(result)

    @app.post("/chat/message/stream")
    async def chat_message_stream(request: Request, _=Depends(_auth)):
        """Same as /chat/message but streams live progress (round/tool_start/tool_end)
        as NDJSON, ending with a {"type":"reply"} event. Cancelable by disconnecting."""
        from chat_backend import chat_stream
        from config import CONFIG
        if _router is None:
            raise HTTPException(status_code=503, detail="Router not initialised")
        try:
            body = await request.json()
        except Exception:
            body = {}
        message = (body.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "Empty message."}, status_code=400)

        import file_browser
        workspace = (body.get("workspace") or "").strip() or file_browser.get_workspace()
        skills = body.get("skills") or []
        if not isinstance(skills, list):
            skills = []

        def factory(should_cancel):
            return chat_stream(
                _router,
                body.get("session_id", "default"),
                message,
                body.get("provider", "gemini"),
                body.get("model", ""),
                body.get("api_key", ""),
                body.get("base_url", ""),
                bool(body.get("allow_actions", False)),
                CONFIG.owner_name,
                getattr(CONFIG, "language", "en"),
                workspace,
                (body.get("agent") or "").strip(),
                skills,
                (body.get("effort") or "standard").strip(),
                bool(body.get("approval", False)),
                bool(body.get("thinking", False)),
                body.get("attachments") or [],
                should_cancel=should_cancel,
            )

        return _ndjson_stream(request, factory)

    @app.post("/chat/approve")
    async def chat_approve(request: Request, _=Depends(_auth)):
        """Execute an action the user approved in the Chat UI (file write/command/…)."""
        from chat_backend import _needs_approval, _collect_files
        if _router is None:
            raise HTTPException(status_code=503, detail="Router not initialised")
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = (body.get("tool") or "").strip()
        args = body.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if not name or not _needs_approval(name, args):
            return JSONResponse({"error": "Not an approvable action."}, status_code=400)
        try:
            result = str(_router._dispatch_module(name, args))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        files: list = []
        try:
            _collect_files(name, args, result, files)
        except Exception:
            pass
        return {"ok": True, "result": result, "files": files}

    @app.post("/chat/research")
    async def chat_research(request: Request, _=Depends(_auth)):
        """Run the deep-research pipeline, streaming NDJSON progress events (cancelable
        by disconnecting), then persist the final report + its vault note."""
        from config import CONFIG

        try:
            body = await request.json()
        except Exception:
            body = {}
        topic = (body.get("message") or "").strip()
        if not topic:
            return JSONResponse({"error": "Empty message."}, status_code=400)

        session_id = body.get("session_id", "default")
        provider = body.get("provider", "gemini")
        model = body.get("model", "")
        api_key = body.get("api_key", "")
        base_url = body.get("base_url", "")
        effort = (body.get("effort") or "standard").strip()
        agent = (body.get("agent") or "").strip()
        skills = body.get("skills") or []
        if not isinstance(skills, list):
            skills = []

        # Research engine: defaults to a Gemini text model (free tier, reliable for the
        # JSON planning/synthesis) decoupled from the chat model. "chat" = use chat model.
        rm = body.get("research_model", "gemini-3.5-flash")
        rm = (rm or "").strip()
        if rm == "minimax":
            # High rate limit → deep research runs wider (see _EFFORT_HIGH).
            r_provider = "minimax"
            r_model = getattr(CONFIG, "minimax_model", "") or "MiniMax-Text-01"
            r_key = getattr(CONFIG, "minimax_api_key", "")
            r_base = getattr(CONFIG, "minimax_base_url", "")
        elif rm and rm != "chat":
            r_provider, r_model, r_key, r_base = "gemini", rm, "", ""   # uses LLM_API_KEY via env
        else:
            r_provider, r_model, r_key, r_base = provider, model, api_key, base_url

        def factory(should_cancel):
            import deep_research
            return deep_research.run(topic, r_provider, r_model, r_key, r_base,
                                     language=getattr(CONFIG, "language", "en"),
                                     effort=effort, agent=agent, skills=skills,
                                     should_cancel=should_cancel)

        state = {"report": "", "note": ""}

        def on_event(ev):
            if ev.get("type") == "report":
                state["report"] = ev.get("reply", "")
                state["note"] = ev.get("note", "")
                if state["report"]:
                    import conversation_store as convo
                    files = ([{"path": state["note"], "name": os.path.basename(state["note"])}]
                             if state["note"] else [])
                    try:
                        convo.append_turn(session_id, topic, state["report"],
                                          ["deep_research"], files)
                    except Exception:
                        pass

        return _ndjson_stream(request, factory, on_event)

    @app.post("/chat/code")
    async def chat_code(request: Request, _=Depends(_auth)):
        """Start a Miko↔Claude-Code pair-programming session and stream the live
        debate (checkpoint/miko/claude/awaiting/done) as NDJSON, cancelable."""
        from config import CONFIG
        import modules.claude_code as CC
        try:
            body = await request.json()
        except Exception:
            body = {}
        goal = (body.get("message") or "").strip()
        project_dir = (body.get("project_dir") or "").strip()
        if not goal or not project_dir:
            return JSONResponse({"error": "Need a project_dir and a goal."}, status_code=400)
        mode = "controlled" if body.get("mode") == "controlled" else "autonomous"
        rm = (body.get("research_model") or "gemini-3.5-flash").strip()
        base = ""
        if rm == "minimax":
            provider = "minimax"
            model = getattr(CONFIG, "minimax_model", "") or "MiniMax-Text-01"
            key = getattr(CONFIG, "minimax_api_key", "")
            base = getattr(CONFIG, "minimax_base_url", "")
        elif rm == "chat":
            provider, model, key = body.get("provider", "gemini"), body.get("model", ""), body.get("api_key", "")
            base = body.get("base_url", "")
        else:
            provider, model, key = "gemini", rm, ""
        max_rounds = int(body.get("max_rounds") or 6)
        research = (body.get("research") or "").strip()

        # Research→build bridge: the active persona/skills (e.g. a skill distilled from a
        # deep-research run) become Miko-the-instructor's domain context for the build.
        try:
            import agent_skills
            overlay = agent_skills.build_overlay((body.get("agent") or "").strip(),
                                                 body.get("skills") or [])
            if overlay:
                research = (overlay + ("\n\n" + research if research else "")).strip()
        except Exception:
            pass

        started = CC.start_session(project_dir, goal, mode=mode, research=research,
                                   provider=provider, model=model, api_key=key,
                                   base_url=base, max_rounds=max_rounds,
                                   coder=(body.get("coder") or "claude"),
                                   coder_model=(body.get("coder_model") or "").strip(),
                                   coder_effort=(body.get("coder_effort") or "").strip())
        if started.get("error"):
            return JSONResponse({"error": started["error"]}, status_code=400)
        token = started["token"]

        def factory(should_cancel):
            def gen():
                yield {"type": "session", "token": token, "repo": started["repo"], "mode": mode}
                for ev in CC.run(token, should_cancel):
                    yield ev
            return gen()

        return _ndjson_stream(request, factory)

    @app.post("/chat/code/continue")
    async def chat_code_continue(request: Request, _=Depends(_auth)):
        """Resume a controlled session for the next round (after the user approves)."""
        import modules.claude_code as CC
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = (body.get("token") or "").strip()
        if not token:
            return JSONResponse({"error": "Missing token."}, status_code=400)
        guidance = (body.get("guidance") or "").strip()

        def factory(should_cancel):
            return CC.run(token, should_cancel, guidance=guidance)

        return _ndjson_stream(request, factory)

    @app.post("/chat/code/queue")
    async def chat_code_queue(request: Request, _=Depends(_auth)):
        """Queue a message for Miko mid-run — picked up at the next round without
        interrupting the round in flight (lets the user steer an autonomous session)."""
        import modules.claude_code as CC
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = (body.get("token") or "").strip()
        message = (body.get("message") or "").strip()
        if not token:
            return JSONResponse({"error": "Missing token."}, status_code=400)
        res = CC.queue_message(token, message)
        if res.get("error"):
            return JSONResponse(res, status_code=400)
        return res

    # ── Dictation (speech-to-text for the chat composer) ───────────────────────
    @app.post("/chat/transcribe")
    async def chat_transcribe(request: Request, _=Depends(_auth)):
        """Transcribe a raw audio body (browser MediaRecorder blob) to text.
        Multilingual + auto-detecting via Gemini; optional ?language= hint."""
        from modules.speech import transcribe
        data = await request.body()
        if not data:
            return JSONResponse({"error": "Empty audio."}, status_code=400)
        mime = (request.headers.get("content-type") or "audio/webm")
        lang = request.query_params.get("language", "")
        text = transcribe(data, mime, language=lang)
        if not text:
            return JSONResponse(
                {"error": "Could not transcribe (check that the Gemini key is set and you spoke clearly)."},
                status_code=502)
        return {"text": text}

    # ── Sub-agents (user-launchable + observable) ──────────────────────────────
    @app.post("/chat/agents/launch")
    async def chat_agents_launch(request: Request, _=Depends(_auth)):
        """Launch a batch of parallel sub-agents (non-blocking). Returns the batch view;
        the UI then streams /chat/agents/stream to watch them work."""
        from modules import agent_jobs
        try:
            body = await request.json()
        except Exception:
            body = {}
        tasks = body.get("tasks") or []
        if isinstance(tasks, str):
            tasks = [tasks]
        tasks = [str(t).strip() for t in tasks if str(t).strip()]
        if not tasks:
            return JSONResponse({"error": "Provide at least one task."}, status_code=400)
        view = agent_jobs.launch(
            tasks, (body.get("context") or "").strip(),
            (body.get("provider") or "gemini").strip(),
            (body.get("model") or "").strip(),
            (body.get("api_key") or "").strip(),
            (body.get("base_url") or "").strip(),
            (body.get("session_id") or "").strip(),
        )
        if view.get("error"):
            return JSONResponse(view, status_code=400)
        return view

    @app.post("/chat/agents/stream")
    async def chat_agents_stream(request: Request, _=Depends(_auth)):
        """NDJSON stream of a batch's live state until every agent is terminal."""
        from modules import agent_jobs
        try:
            body = await request.json()
        except Exception:
            body = {}
        batch_id = (body.get("batch_id") or "").strip()
        if not batch_id:
            return JSONResponse({"error": "Missing batch_id."}, status_code=400)

        def factory(should_cancel):
            return agent_jobs.stream_batch(batch_id)
        return _ndjson_stream(request, factory)

    @app.get("/chat/agents/list")
    def chat_agents_list(limit: int = 20, session_id: str = "", _=Depends(_auth)):
        from modules import agent_jobs
        return {"batches": agent_jobs.list_batches(limit, session_id)}

    @app.get("/chat/agents/batch")
    def chat_agents_batch(batch_id: str, _=Depends(_auth)):
        from modules import agent_jobs
        return agent_jobs.get_batch(batch_id) or {}

    @app.post("/chat/agents/cancel")
    async def chat_agents_cancel(request: Request, _=Depends(_auth)):
        from modules import agent_jobs
        try:
            body = await request.json()
        except Exception:
            body = {}
        if body.get("agent_id"):
            return agent_jobs.cancel_agent(body["agent_id"].strip())
        if body.get("batch_id"):
            return agent_jobs.cancel_batch(body["batch_id"].strip())
        return JSONResponse({"error": "Provide batch_id or agent_id."}, status_code=400)

    @app.post("/chat/agents/delete")
    async def chat_agents_delete(request: Request, _=Depends(_auth)):
        """Remove a sub-agent batch from the panel (and disk)."""
        from modules import agent_jobs
        try:
            body = await request.json()
        except Exception:
            body = {}
        bid = (body.get("batch_id") or "").strip()
        if not bid:
            return JSONResponse({"error": "Missing batch_id."}, status_code=400)
        return agent_jobs.delete_batch(bid)

    @app.post("/chat/code/revert")
    async def chat_code_revert(request: Request, _=Depends(_auth)):
        """Revert the repo to a checkpoint (UI Revert button)."""
        import modules.claude_code as CC
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = (body.get("token") or "").strip()
        snap = (body.get("snap") or "").strip()
        if not token:
            return JSONResponse({"error": "Missing token."}, status_code=400)
        return {"message": CC.revert_round(token, snap)}

    @app.get("/chat/code/active")
    def chat_code_active(_=Depends(_auth)):
        """The most recent resumable pair session (so the UI can restore it after a
        refresh and continue it). Returns {} if there's none."""
        import modules.claude_code as CC
        return CC.get_active_session() or {}

    @app.get("/chat/code/sessions")
    def chat_code_sessions(_=Depends(_auth)):
        """All pair sessions (compact) for the UI's session list."""
        import modules.claude_code as CC
        return {"sessions": CC.list_sessions()}

    @app.get("/chat/code/session")
    def chat_code_session(token: str, _=Depends(_auth)):
        """Full view of one session, to resume it from the list."""
        import modules.claude_code as CC
        return CC.get_session(token) or {}

    @app.post("/chat/code/forget")
    async def chat_code_forget(request: Request, _=Depends(_auth)):
        """Remove a pair session from the registry (does not touch the repo)."""
        import modules.claude_code as CC
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = (body.get("token") or "").strip()
        return {"ok": CC.forget_session(token)}

    @app.post("/chat/code/recap")
    async def chat_code_recap(request: Request, _=Depends(_auth)):
        """Ask Claude to summarize what a session has done so far (where it left off)."""
        import modules.claude_code as CC
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = (body.get("token") or "").strip()
        if not token:
            return JSONResponse({"error": "Missing token."}, status_code=400)
        return {"recap": CC.recap(token)}

    @app.get("/chat/agent-skills")
    def chat_agent_skills(_=Depends(_auth)):
        import agent_skills
        return {"agents": agent_skills.list_agents(), "skills": agent_skills.list_skills()}

    @app.get("/chat/catalog")
    def chat_catalog(_=Depends(_auth)):
        """Unified marketplace catalog: agents, skills (with pairs_with links) + MCP
        capabilities. Phase 2 of the skills marketplace."""
        import agent_skills
        return agent_skills.catalog()

    @app.get("/chat/settings")
    def chat_settings(_=Depends(_auth)):
        """Front-end settings sourced from the server config. dictation_lang is the
        BCP-47 tag for the Web Speech dictation engine: MIKO_DICTATION_LANG if set,
        else derived from MIKO_LANGUAGE (en→en-US, ro→ro-RO)."""
        from config import CONFIG
        lang = os.getenv("MIKO_DICTATION_LANG", "").strip()
        if not lang:
            lang = {"ro": "ro-RO", "en": "en-US"}.get(getattr(CONFIG, "language", "en"), "en-US")
        return {"dictation_lang": lang}

    @app.post("/chat/skill/install")
    async def chat_skill_install(request: Request, _=Depends(_auth)):
        """Install an agent/skill from a markdown definition (text only — never runs
        code). Body: {md: str, overwrite?: bool}."""
        import agent_skills
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok, msg = agent_skills.install_skill(body.get("md", ""), bool(body.get("overwrite")))
        return JSONResponse({"ok": ok, "message": msg}, status_code=(200 if ok else 400))

    @app.post("/chat/skill/from-research")
    async def chat_skill_from_research(request: Request, _=Depends(_auth)):
        """Distill a deep-research result into a reusable skill (text only). Body:
        {topic?: str, report?: str}. Uses the supplied report if present (the UI already
        has it), else finds the matching vault research note by topic."""
        import agent_skills
        try:
            body = await request.json()
        except Exception:
            body = {}
        topic = (body.get("topic") or "").strip()
        report = (body.get("report") or "").strip()
        if report:
            ok, msg, sid = agent_skills.skill_from_research(report, topic=topic, overwrite=True)
            return JSONResponse({"ok": ok, "message": msg, "skill_id": sid},
                                status_code=(200 if ok else 400))
        msg = agent_skills.create_skill_from_research(topic)
        ok = not msg.lower().startswith(("n-am", "spune-mi"))
        return JSONResponse({"ok": ok, "message": msg}, status_code=200)

    # ── Knowledge / second brain ─────────────────────────────────────────────────
    @app.get("/knowledge/stats")
    def knowledge_stats(_=Depends(_auth)):
        from memory import knowledge_store as KS
        return KS.stats()

    @app.post("/knowledge/reindex")
    def knowledge_reindex(_=Depends(_auth)):
        from config import CONFIG
        from memory import knowledge_store as KS
        from memory.memory_manager import load_memory
        facts = KS.index_facts(load_memory(CONFIG.memory_file))
        notes = KS.reindex_notes(CONFIG.notes_dir)
        return {"ok": True, "facts_indexed": facts, "notes": notes, "stats": KS.stats()}

    @app.post("/knowledge/recall")
    async def knowledge_recall(request: Request, _=Depends(_auth)):
        from memory import knowledge_store as KS
        try:
            body = await request.json()
        except Exception:
            body = {}
        query = (body.get("query") or "").strip()
        if not query:
            return JSONResponse({"error": "Empty query."}, status_code=400)
        return {"results": KS.search(query, k=int(body.get("k", 6)))}

    @app.get("/chat/env")
    def chat_env_get(_=Depends(_auth)):
        from chat_backend import read_env_keys
        return read_env_keys()

    @app.get("/chat/settings/schema")
    def chat_settings_schema(_=Depends(_auth)):
        """Categorised settings + current state for the Settings panel (secrets masked)."""
        from chat_backend import settings_schema
        return settings_schema()

    # ── Scheduler (recurring tasks) ─────────────────────────────────────────────
    @app.get("/chat/tasks")
    def chat_tasks_list(_=Depends(_auth)):
        from modules.scheduled_tasks import list_tasks
        return {"tasks": list_tasks()}

    @app.post("/chat/tasks/parse")
    async def chat_tasks_parse(request: Request, _=Depends(_auth)):
        """Turn a natural-language request into a structured task draft (no persist)."""
        from modules.scheduled_tasks import parse_task_nl
        try:
            body = await request.json()
        except Exception:
            body = {}
        res = parse_task_nl((body.get("text") or "").strip())
        return JSONResponse(res, status_code=(400 if res.get("error") and not res.get("draft") else 200))

    @app.post("/chat/tasks")
    async def chat_tasks_create(request: Request, _=Depends(_auth)):
        """Create a task from structured fields (schedule, time, prompt, …)."""
        from modules.scheduled_tasks import create_task
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return create_task(**body)
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/chat/tasks/action")
    async def chat_tasks_action(request: Request, _=Depends(_auth)):
        """Pause / resume / delete / run a task by id. Body: {id, action}."""
        from modules.scheduled_tasks import set_status, delete_task, run_task_now
        try:
            body = await request.json()
        except Exception:
            body = {}
        tid = (body.get("id") or "").strip()
        action = (body.get("action") or "").strip()
        if not tid:
            return JSONResponse({"error": "Missing task id."}, status_code=400)
        if action == "pause":
            res = set_status(tid, "paused")
        elif action == "resume":
            res = set_status(tid, "active")
        elif action == "delete":
            res = delete_task(tid)
        elif action == "run":
            res = run_task_now(tid)
        else:
            return JSONResponse({"error": "Unknown action."}, status_code=400)
        return JSONResponse(res, status_code=(400 if res.get("error") else 200))

    @app.get("/chat/inbox")
    def chat_inbox(limit: int = 25, unread_only: bool = False, _=Depends(_auth)):
        """Structured inbox listing for the UI (the same mailbox Miko reads)."""
        from modules.email_box import inbox_view
        res = inbox_view(limit=limit, unread_only=unread_only)
        return JSONResponse(res, status_code=(400 if res.get("error") else 200))

    @app.get("/chat/inbox/message")
    def chat_inbox_message(uid: str, _=Depends(_auth)):
        """Full body of one inbox message by uid (read-only, won't mark it seen)."""
        from modules.email_box import message_view
        res = message_view(uid)
        return JSONResponse(res, status_code=(400 if res.get("error") else 200))

    @app.post("/chat/env")
    async def chat_env_set(request: Request, _=Depends(_auth)):
        from chat_backend import write_env_keys
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "Expected a JSON object of KEY: value."}, status_code=400)
        updated = write_env_keys(body)
        return {"ok": True, "env": updated}

    # ── Workspace (file explorer + editor) ──────────────────────────────────────
    @app.get("/files/roots")
    def files_roots(_=Depends(_auth)):
        import file_browser
        return {"roots": file_browser.roots()}

    @app.get("/workspace")
    def workspace_get(_=Depends(_auth)):
        import file_browser
        return {"workspace": file_browser.get_workspace()}

    @app.post("/workspace")
    async def workspace_set(request: Request, _=Depends(_auth)):
        import file_browser
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return file_browser.set_workspace(body.get("workspace", ""))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/files/list")
    def files_list(path: str = "", _=Depends(_auth)):
        import file_browser
        try:
            return file_browser.list_dir(path)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/files/pick")
    async def files_pick(request: Request, _=Depends(_auth)):
        import file_browser
        try:
            body = await request.json()
        except Exception:
            body = {}
        path = file_browser.pick_directory(body.get("initial", ""))
        if not path:
            return {"cancelled": True}
        return {"path": path}

    @app.get("/files/read")
    def files_read(path: str, _=Depends(_auth)):
        import file_browser
        try:
            return file_browser.read_file(path)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/files/write")
    async def files_write(request: Request, _=Depends(_auth)):
        import file_browser
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return file_browser.write_file(body.get("path", ""), body.get("content", ""))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/files/create")
    async def files_create(request: Request, _=Depends(_auth)):
        import file_browser
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return file_browser.create_entry(
                body.get("path", ""), body.get("name", ""), bool(body.get("is_dir", False))
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/files/rename")
    async def files_rename(request: Request, _=Depends(_auth)):
        import file_browser
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return file_browser.rename_entry(body.get("path", ""), body.get("name", ""))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/files/delete")
    async def files_delete(request: Request, _=Depends(_auth)):
        import file_browser
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return file_browser.delete_entry(body.get("path", ""))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/files/paste")
    async def files_paste(request: Request, _=Depends(_auth)):
        import file_browser
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return file_browser.paste_entry(
                body.get("src", ""), body.get("dest", ""), bool(body.get("move", False))
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    # ── Conversations (persistent history) ──────────────────────────────────────
    @app.get("/chat/conversations")
    def chat_conversations(_=Depends(_auth)):
        import conversation_store as convo
        return {"conversations": convo.list_conversations()}

    @app.get("/chat/conversation")
    def chat_conversation(id: str, _=Depends(_auth)):
        import conversation_store as convo
        conv = convo.get_conversation(id)
        if conv is None:
            return JSONResponse({"error": "Conversation not found."}, status_code=404)
        return conv

    @app.post("/chat/conversation/rename")
    async def chat_conversation_rename(request: Request, _=Depends(_auth)):
        import conversation_store as convo
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok = convo.rename(body.get("id", ""), body.get("title", ""))
        return {"ok": ok}

    @app.post("/chat/conversation/delete")
    async def chat_conversation_delete(request: Request, _=Depends(_auth)):
        import conversation_store as convo
        try:
            body = await request.json()
        except Exception:
            body = {}
        convo.delete(body.get("id", ""))
        return {"ok": True}

    @app.post("/chat/reset")
    async def chat_reset(request: Request, _=Depends(_auth)):
        from chat_backend import reset_session
        try:
            body = await request.json()
        except Exception:
            body = {}
        reset_session(body.get("session_id", "default"))
        return {"ok": True}

    # ── Tool execution ─────────────────────────────────────────────────────────
    @app.post("/tools/{tool_name}")
    async def call_tool(tool_name: str, request: Request, _=Depends(_auth)):
        """
        Execute a Miko tool. Body = JSON dict of args (can be empty {}).
        Returns {"result": "..."} on success.
        Returns 403 {"error": "confirmation_required"} for destructive tools
        unless the caller sends X-Bypass-Confirmation: true (trusted local agents).
        """
        from core.command_router import ConfirmationPending

        if _router is None:
            raise HTTPException(status_code=503, detail="Router not initialised — Miko not fully started yet")

        bypass = request.headers.get("X-Bypass-Confirmation", "").lower() == "true"

        try:
            args = await request.json()
        except Exception:
            args = {}

        if not isinstance(args, dict):
            args = {}

        logger.info(f"[ToolServer] {tool_name}({list(args.keys())}) bypass={bypass}")

        if bypass:
            # Skip confirmation gate — dispatch directly to the module
            try:
                result = _router._dispatch_module(tool_name, args)
            except Exception as e:
                logger.error(f"[ToolServer] dispatch error for {tool_name}: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)
        else:
            try:
                result = _router.dispatch(tool_name, args)
            except Exception as e:
                logger.error(f"[ToolServer] dispatch error for {tool_name}: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)

            if isinstance(result, ConfirmationPending):
                return JSONResponse(
                    {
                        "error": "confirmation_required",
                        "message": (
                            f"'{tool_name}' requires voice confirmation from Roxan and cannot "
                            "be executed remotely without X-Bypass-Confirmation: true."
                        ),
                    },
                    status_code=403,
                )

        return JSONResponse({"result": result})

    return app
