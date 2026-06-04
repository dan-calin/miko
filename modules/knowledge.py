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
            "Save OR UPDATE a durable fact in long-term memory. Use whenever the user "
            "shares something worth keeping across conversations, or corrects an earlier "
            "fact. ALWAYS pass a short canonical 'key' (1-2 words, e.g. name, job, age, "
            "city, birthday, favorite_food) plus the 'value'. To correct or update a fact, "
            "REUSE THE SAME key so it overwrites — never invent a new key for the same "
            "attribute. Example: user says 'my name is Dan' → "
            "remember(category='identity', key='name', value='Dan')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": "One of: identity, preferences, relationships, notes.",
                },
                "key": {
                    "type": "STRING",
                    "description": "Short canonical identifier, e.g. name, job, age, city, "
                                   "favorite_food. Reuse the same key to update a fact.",
                },
                "value": {
                    "type": "STRING",
                    "description": "The fact value, e.g. 'Dan' or 'IT Technician'.",
                },
            },
            "required": ["category", "key", "value"],
        },
    },
    {
        "name": "forget",
        "description": (
            "Remove a stored fact from long-term memory by its category and key. Use when "
            "the user asks you to forget something or says a stored fact is wrong/outdated."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {"type": "STRING", "description": "identity, preferences, relationships, or notes."},
                "key": {"type": "STRING", "description": "The key of the fact to remove (e.g. name, job)."},
            },
            "required": ["category", "key"],
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


def _clean_key(key: str) -> str:
    """Normalise a model-supplied key to a short snake_case identifier."""
    toks = re.findall(r"[A-Za-z0-9]+", (key or "").lower())[:4]
    return "_".join(toks)[:40]


def remember(category: str = "notes", key: str = "", value: str = "", content: str = "") -> str:
    value = (value or content or "").strip()   # `content` kept as a fallback alias
    if not value:
        return "There's nothing to remember — give me a fact."
    cat = category if category in _CATEGORIES else "notes"
    k = _clean_key(key) or _slug(value)

    from config import CONFIG
    from memory.memory_manager import update_memory
    update_memory(CONFIG.memory_file, {cat: {k: {"value": value}}})   # same key overwrites

    # Index immediately so recall can find it semantically.
    try:
        from memory import knowledge_store as KS
        label = k.replace("_", " ")
        KS.upsert("fact", f"{cat}/{k}", label, f"{label}: {value}")
    except Exception as e:
        logger.warning(f"remember: index failed: {e}")

    return f"Got it — remembered ({cat}/{k})."


def forget(category: str = "", key: str = "") -> str:
    cat = (category or "").strip()
    k = _clean_key(key)
    if not cat or not k:
        return "Tell me which fact to forget (a category and key)."

    from config import CONFIG
    from memory.memory_manager import load_memory, save_memory
    mem = load_memory(CONFIG.memory_file)
    bucket = mem.get(cat)
    removed = bucket.pop(k, None) if isinstance(bucket, dict) else None
    if removed is None:
        return f"I had nothing stored under {cat}/{k}."
    save_memory(CONFIG.memory_file, mem)
    try:
        from memory import knowledge_store as KS
        KS.delete_ref("fact", f"{cat}/{k}")
    except Exception as e:
        logger.warning(f"forget: index cleanup failed: {e}")
    return f"Forgotten: {cat}/{k}."


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
