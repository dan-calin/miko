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
    logger.info(f"Tool server listening on {host}:{port}")


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
        )
        return JSONResponse(result)

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

    @app.get("/files/list")
    def files_list(path: str = "", _=Depends(_auth)):
        import file_browser
        try:
            return file_browser.list_dir(path)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

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
