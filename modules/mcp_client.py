"""
modules/mcp_client.py — connect Miko to external MCP servers and use their tools.

Miko already exposes its own tools to others (the tool server); this makes it an
MCP *consumer*. Configure servers in data/mcp_servers.json:

  {"servers": [
     {"name": "filesystem", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/path"]},
     {"name": "remote", "type": "sse", "url": "https://host/sse"}
  ]}

On startup it connects to each server (in a dedicated asyncio thread), lists their
tools, and registers them as `mcp_<server>_<tool>` so the model can call them. The
async sessions are owned by one event loop; tool calls bridge over via
run_coroutine_threadsafe. Degrades to nothing if not configured / SDK missing.
"""

import asyncio
import json
import logging
import re
import threading

logger = logging.getLogger("miko.mcp")

_loop = None
_thread = None
_started = False
_ready = threading.Event()
_sessions: dict = {}   # server name -> ClientSession
_tools: dict = {}      # full_name -> {server, tool, schema, description}


def _config_path():
    from config import CONFIG
    return CONFIG.data_dir / "mcp_servers.json"


def _load_servers() -> list:
    p = _config_path()
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d.get("servers", [])
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _san(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(s)).strip("_") or "x"


async def _connect_all(stack):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    for srv in _load_servers():
        name = srv.get("name") or "server"
        try:
            if srv.get("type") == "sse" or srv.get("url"):
                from mcp.client.sse import sse_client
                read, write = await stack.enter_async_context(sse_client(srv["url"]))
            else:
                params = StdioServerParameters(
                    command=srv["command"], args=srv.get("args", []), env=srv.get("env"))
                read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            _sessions[name] = session
            listed = await session.list_tools()
            for t in listed.tools:
                full = f"mcp_{_san(name)}_{_san(t.name)}"
                _tools[full] = {
                    "server": name, "tool": t.name,
                    "schema": t.inputSchema or {"type": "object", "properties": {}},
                    "description": t.description or "",
                }
            logger.info(f"MCP '{name}': {len(listed.tools)} tool(s)")
        except Exception as e:
            logger.warning(f"MCP server '{name}' failed: {e}")


async def _serve():
    from contextlib import AsyncExitStack
    async with AsyncExitStack() as stack:
        await _connect_all(stack)
        _ready.set()
        await asyncio.Event().wait()   # keep the connections alive


def start() -> None:
    global _loop, _thread, _started
    if _started:
        return
    _started = True
    if not _load_servers():
        _ready.set()
        return   # nothing configured

    def _run():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        try:
            _loop.run_until_complete(_serve())
        except Exception as e:
            logger.warning(f"MCP loop ended: {e}")

    _thread = threading.Thread(target=_run, daemon=True, name="MCPClient")
    _thread.start()


def get_tool_declarations() -> list:
    """Gemini-format declarations for the connected MCP tools (chat_backend merges these)."""
    return [
        {"name": full, "description": (info["description"] or full)[:1024],
         "parameters": info["schema"]}
        for full, info in _tools.items()
    ]


def call_tool(full_name: str, args: dict) -> str:
    info = _tools.get(full_name)
    if not info or _loop is None:
        return f"[MCP tool '{full_name}' is not available]"
    session = _sessions.get(info["server"])
    if session is None:
        return f"[MCP server for '{full_name}' is not connected]"

    async def _call():
        res = await session.call_tool(info["tool"], arguments=args or {})
        parts = [getattr(c, "text", "") for c in (res.content or []) if getattr(c, "text", "")]
        return "\n".join(parts) or "(no output)"

    try:
        return asyncio.run_coroutine_threadsafe(_call(), _loop).result(timeout=60)
    except Exception as e:
        return f"[MCP call error: {e}]"
