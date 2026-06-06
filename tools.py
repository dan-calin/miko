"""
tools.py — Aggregates all tool declarations from every module.
Each module owns its TOOL_DECLARATIONS list. This file assembles them into:
  ALL_TOOL_DECLARATIONS        — Gemini FunctionDeclaration dicts
  ALL_TOOL_DECLARATIONS_OPENAI — OpenAI/OpenAI-compat format
  ALL_TOOL_DECLARATIONS_ANTHROPIC — Anthropic Messages API format (MiniMax /anthropic endpoint)
"""

# Lazy imports — if one module is broken/missing, the others still load.

def _safe_import(module_path: str, attr: str = "TOOL_DECLARATIONS") -> list:
    try:
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, attr, [])
    except Exception as e:
        import logging
        logging.getLogger("miko.tools").warning(f"Could not load {module_path}: {e}")
        return []


def _fix_schema_types(schema: dict) -> dict:
    """Recursively lowercase all 'type' values (Gemini uses UPPERCASE, OpenAI uses lowercase)."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.lower()
        elif isinstance(v, dict):
            out[k] = _fix_schema_types(v)
        elif isinstance(v, list):
            out[k] = [_fix_schema_types(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


def _to_openai_tool(decl: dict) -> dict:
    """Wrap a Gemini FunctionDeclaration dict in the OpenAI tool format."""
    return {
        "type": "function",
        "function": {
            "name": decl["name"],
            "description": decl.get("description", ""),
            "parameters": _fix_schema_types(
                decl.get("parameters", {"type": "object", "properties": {}})
            ),
        },
    }


ALL_TOOL_DECLARATIONS: list = (
    _safe_import("modules.media_control")
    + _safe_import("modules.discord_bot")
    + _safe_import("modules.youtube_player")
    + _safe_import("modules.research")
    + _safe_import("modules.notes")
    + _safe_import("modules.knowledge")
    + _safe_import("modules.projects")
    + _safe_import("modules.email_box")
    + _safe_import("modules.scheduled_tasks")
    + _safe_import("modules.browser")
    + _safe_import("modules.subagents")
    + _safe_import("modules.os_control")
    + _safe_import("modules.file_indexer")
    + _safe_import("modules.journey")
    + _safe_import("modules.calendar")
    + _safe_import("modules.discord_rpc")
)

def _to_anthropic_tool(decl: dict) -> dict:
    """Convert to Anthropic Messages API tool format (input_schema instead of parameters)."""
    return {
        "name": decl["name"],
        "description": decl.get("description", ""),
        "input_schema": _fix_schema_types(
            decl.get("parameters", {"type": "object", "properties": {}})
        ),
    }


# OpenAI / OpenAI-compat format
ALL_TOOL_DECLARATIONS_OPENAI: list = [_to_openai_tool(d) for d in ALL_TOOL_DECLARATIONS]

# Anthropic Messages API format (used by MiniMax /anthropic endpoint)
ALL_TOOL_DECLARATIONS_ANTHROPIC: list = [_to_anthropic_tool(d) for d in ALL_TOOL_DECLARATIONS]
