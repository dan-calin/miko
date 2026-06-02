"""
memory/memory_manager.py — Persistent long-term memory for Miko.
Stores user facts (identity, preferences, relationships, notes) across sessions.
Auto-extracts facts from conversation transcripts via Gemini.
"""

import json
import re
import threading
import logging
from pathlib import Path

logger = logging.getLogger("miko.memory")

_lock = threading.Lock()
MAX_VALUE_LENGTH = 300
_MEMORY_EVERY_N = 5
_turn_counter = 0
_last_user_text = ""


def _empty_memory() -> dict:
    return {
        "identity": {},
        "preferences": {},
        "relationships": {},
        "notes": {},
    }


def load_memory(memory_file: Path) -> dict:
    if not memory_file.exists():
        return _empty_memory()
    with _lock:
        try:
            data = json.loads(memory_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else _empty_memory()
        except Exception as e:
            logger.warning(f"Memory load error: {e}")
            return _empty_memory()


def save_memory(memory_file: Path, memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        memory_file.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def _truncate(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            entry = (
                {"value": _truncate(str(value["value"]))}
                if isinstance(value, dict) and "value" in value
                else {"value": _truncate(str(value))}
            )
            if key not in target or target[key] != entry:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_file: Path, memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory(memory_file)
    memory = load_memory(memory_file)
    if _recursive_update(memory, memory_update):
        save_memory(memory_file, memory)
        logger.info(f"Memory updated: {list(memory_update.keys())}")
    return memory


def format_memory_for_prompt(memory: dict | None) -> str:
    if not memory:
        return ""
    lines = []
    identity = memory.get("identity", {})
    for key in ("name", "age", "birthday", "city", "job"):
        val = identity.get(key, {}).get("value")
        if val:
            lines.append(f"{key.title()}: {val}")
    for i, (key, entry) in enumerate(memory.get("preferences", {}).items()):
        if i >= 5:
            break
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            lines.append(f"{key.replace('_', ' ').title()}: {val}")
    for i, (key, entry) in enumerate(memory.get("relationships", {}).items()):
        if i >= 5:
            break
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            lines.append(f"{key.title()}: {val}")
    for i, (key, entry) in enumerate(memory.get("notes", {}).items()):
        if i >= 3:
            break
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            lines.append(f"{key}: {val}")
    if not lines:
        return ""
    result = "[USER MEMORY]\n" + "\n".join(f"- {l}" for l in lines)
    if len(result) > 800:
        result = result[:797] + "…"
    return result + "\n"


def update_from_conversation_async(
    memory_file: Path, gemini_api_key: str, user_text: str, ai_text: str,
    minimax_api_key: str = "", minimax_base_url: str = "", minimax_model: str = "",
) -> None:
    """
    Called every N turns. Runs in a daemon thread.
    Extracts personal facts from the user's speech via MiniMax (preferred) or Gemini.
    """
    global _turn_counter, _last_user_text
    _turn_counter += 1
    if _turn_counter % _MEMORY_EVERY_N != 0:
        return
    text = user_text.strip()
    if len(text) < 10 or text == _last_user_text:
        return
    _last_user_text = text

    def _complete(prompt: str) -> str:
        if minimax_api_key:
            if "anthropic" in minimax_base_url.lower():
                import anthropic
                client = anthropic.Anthropic(api_key=minimax_api_key, base_url=minimax_base_url)
                resp = client.messages.create(
                    model=minimax_model,
                    max_tokens=512,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp.content[0].text.strip() if resp.content else ""
            else:
                from openai import OpenAI
                client = OpenAI(api_key=minimax_api_key, base_url=minimax_base_url)
                resp = client.chat.completions.create(
                    model=minimax_model,
                    messages=[{"role": "user", "content": prompt}],
                )
                return (resp.choices[0].message.content or "").strip()
        else:
            from google import genai
            client = genai.Client(api_key=gemini_api_key)
            return client.models.generate_content(
                model="gemini-2.5-flash", contents=prompt
            ).text.strip()

    def _run():
        try:
            check = _complete(
                "Does this message contain personal facts about the user "
                "(name, age, city, job, hobby, relationship, birthday, preference)? "
                f"Reply only YES or NO.\n\nMessage: {text[:300]}"
            )
            if "YES" not in check.upper():
                return

            raw = _complete(
                "Extract personal facts from this message. Any language.\n"
                "Return ONLY valid JSON or {} if nothing found.\n"
                "Extract: name, age, birthday, city, job, hobbies.\n"
                'Format: {"identity":{"name":{"value":"..."}}}\n\n'
                f"Message: {text[:500]}\n\nJSON:"
            )

            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            if raw and raw != "{}":
                data = json.loads(raw)
                if data:
                    update_memory(memory_file, data)
        except Exception as e:
            if "429" not in str(e):
                logger.warning(f"Memory extraction error: {e}")

    threading.Thread(target=_run, daemon=True, name="MemoryExtractor").start()
