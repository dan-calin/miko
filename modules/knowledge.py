"""
modules/knowledge.py — Miko's learn/recall tools over the semantic knowledge store.

Two tools, shared by the voice agent and the web chat (registered once in tools.py):
  - remember(content, category): durably save a fact/preference/instruction. Writes
    to long_term.json (so it's injected into every system prompt) AND indexes it in
    the vector store (so it's semantically recall-able).
  - recall(query, scope): semantic search across the user's facts + the Obsidian/Miko
    Notes vault, returning the most relevant snippets with their source.

The heavy lifting lives in memory/knowledge_store.py (vectors) and
memory/memory_manager.py (structured facts). This module is just the tool surface.
"""

import logging
import os
import re

logger = logging.getLogger("miko.knowledge")

_CATEGORIES = ("identity", "preferences", "relationships", "notes")

TOOL_DECLARATIONS = [
    {
        "name": "remember",
        "description": (
            "Save a durable fact, preference, or instruction to long-term memory. "
            "Use whenever the user shares something worth keeping across conversations: "
            "a personal detail, a preference, a relationship, or 'remember that ...'. "
            "Write a self-contained sentence so it makes sense later on its own."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "content": {
                    "type": "STRING",
                    "description": "The fact to remember, as one self-contained sentence.",
                },
                "category": {
                    "type": "STRING",
                    "description": "identity, preferences, relationships, or notes (default notes).",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "recall",
        "description": (
            "Search Miko's long-term memory and notes vault (second brain) for what it "
            "already knows about a topic. Use BEFORE answering questions about the user, "
            "their past conversations, or earlier research, so answers use stored knowledge."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "What to look up."},
                "scope": {
                    "type": "STRING",
                    "description": "all, facts, or notes (default all).",
                },
            },
            "required": ["query"],
        },
    },
]


def _slug(text: str, words: int = 6) -> str:
    toks = re.findall(r"[A-Za-z0-9]+", text.lower())[:words]
    return "_".join(toks)[:48] or "note"


def remember(content: str, category: str = "notes") -> str:
    content = (content or "").strip()
    if not content:
        return "There's nothing to remember — give me a fact."
    cat = category if category in _CATEGORIES else "notes"
    key = _slug(content)

    from config import CONFIG
    from memory.memory_manager import update_memory
    update_memory(CONFIG.memory_file, {cat: {key: {"value": content}}})

    # Index immediately so recall can find it semantically.
    try:
        from memory import knowledge_store as KS
        label = key.replace("_", " ")
        KS.upsert("fact", f"{cat}/{key}", label, f"{label}: {content}")
    except Exception as e:
        logger.warning(f"remember: index failed: {e}")

    return "Got it — I'll remember that."


def recall(query: str, scope: str = "all") -> str:
    query = (query or "").strip()
    if not query:
        return "What would you like me to recall?"

    kinds = None
    if scope == "facts":
        kinds = ["fact"]
    elif scope in ("notes", "vault"):
        kinds = ["note"]

    try:
        from memory import knowledge_store as KS
        hits = KS.search(query, k=5, kinds=kinds)
    except Exception as e:
        logger.warning(f"recall failed: {e}")
        return "I couldn't reach my memory store just now."

    if not hits:
        return f"I don't have anything stored about '{query}' yet."

    lines = [f"Here's what I know about '{query}':"]
    for h in hits:
        snippet = h["text"][:220].strip()
        if h["kind"] == "note":
            src = os.path.basename(h["ref"].split("#")[0])
            lines.append(f"• {snippet}  — note: {src}")
        else:
            lines.append(f"• {snippet}")
    return "\n".join(lines)
