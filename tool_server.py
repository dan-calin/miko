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
