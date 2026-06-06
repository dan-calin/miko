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
            return HTMLResponse(html_path.read_text(encoding="utf-8"))
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

        def factory(should_cancel):
            import deep_research
            return deep_research.run(topic, provider, model, api_key, base_url,
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

    @app.get("/chat/agent-skills")
    def chat_agent_skills(_=Depends(_auth)):
        import agent_skills
        return {"agents": agent_skills.list_agents(), "skills": agent_skills.list_skills()}

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
