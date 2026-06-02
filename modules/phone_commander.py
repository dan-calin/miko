"""
modules/phone_commander.py — Processes text and voice commands from Discord DMs.

Backend auto-selection (based on .env):
  MINIMAX_API_KEY + base_url contains "anthropic"  → Anthropic SDK (MiniMax /anthropic)
  MINIMAX_API_KEY + other base_url                 → OpenAI-compat SDK
  fallback                                         → Gemini generate_content
"""

import io
import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger("miko.phone")

_MAX_ROUNDS = 5
_MAX_HISTORY = 20  # max messages kept per user

def _phone_model() -> str:
    return os.getenv("PHONE_MODEL", "gemini-2.5-flash")

_commander: Optional["PhoneCommander"] = None


def init_commander(config, command_router) -> "PhoneCommander":
    global _commander
    _commander = PhoneCommander(config, command_router)
    return _commander


def get_commander() -> Optional["PhoneCommander"]:
    return _commander


_SYSTEM_PROMPT_TEMPLATE = (
    "You are Miko, {owner_name}'s personal AI assistant responding via Discord DM from their phone.\n"
    "Be concise — phone screens are small. Execute commands directly.\n"
    "For journey/maps results, always include the Google Maps link.\n"
    "When the user replies with a number (e.g. '1', '2', '3'), treat it as selecting that item from the previous list.\n"
    "Respond in Romanian unless the user writes in English."
)


class PhoneCommander:
    def __init__(self, config, command_router):
        self._config = config
        self._router = command_router
        # Per-sender history — shared dict (OpenAI and Anthropic both use list-of-dicts)
        self._mm_history: dict[str, list] = {}
        self._gemini_history: dict[str, list] = {}
        self._lock = threading.Lock()

    # ── Backend detection ──────────────────────────────────────────────────────

    def _use_minimax(self) -> bool:
        return bool(getattr(self._config, "minimax_api_key", ""))

    def _use_anthropic(self) -> bool:
        return "anthropic" in getattr(self._config, "minimax_base_url", "").lower()

    def _system_prompt(self) -> str:
        return _SYSTEM_PROMPT_TEMPLATE.format(owner_name=self._config.owner_name)

    # ── History helpers ────────────────────────────────────────────────────────

    def _get_mm_history(self, sender: str) -> list:
        with self._lock:
            return list(self._mm_history.get(sender, []))

    def _save_mm_history(self, sender: str, new_messages: list) -> None:
        with self._lock:
            existing = self._mm_history.get(sender, [])
            combined = existing + new_messages
            self._mm_history[sender] = combined[-_MAX_HISTORY:]

    def _get_gemini_history(self, sender: str) -> list:
        with self._lock:
            return list(self._gemini_history.get(sender, []))

    def _save_gemini_history(self, sender: str, new_contents: list) -> None:
        with self._lock:
            existing = self._gemini_history.get(sender, [])
            combined = existing + new_contents
            self._gemini_history[sender] = combined[-_MAX_HISTORY:]

    def clear_history(self, sender: str) -> None:
        with self._lock:
            self._mm_history.pop(sender, None)
            self._gemini_history.pop(sender, None)

    # ── Public entry points ────────────────────────────────────────────────────

    def process_text(self, text: str, sender: str = "") -> str:
        if self._use_minimax():
            if self._use_anthropic():
                return self._process_anthropic(text, sender)
            return self._process_openai(text, sender)
        return self._process_gemini(text, sender)

    def process_voice(self, audio_bytes: bytes, mime_type: str = "audio/ogg", sender: str = "") -> str:
        transcript = self._transcribe(audio_bytes, mime_type)
        if not transcript:
            return "Nu am putut transcrie mesajul vocal."
        logger.info(f"Voice transcript from {sender!r}: {transcript!r}")
        text_response = self.process_text(transcript, sender=sender)
        return f"[🎤 {transcript}]\n{text_response}"

    # ── Anthropic Messages API backend (MiniMax /anthropic endpoint) ───────────

    def _process_anthropic(self, text: str, sender: str) -> str:
        import anthropic
        from tools import ALL_TOOL_DECLARATIONS_ANTHROPIC
        from core.command_router import ConfirmationPending

        try:
            client = anthropic.Anthropic(
                api_key=self._config.minimax_api_key,
                base_url=self._config.minimax_base_url,
            )
        except Exception as e:
            logger.error(f"Anthropic client init failed: {e}")
            return f"Eroare la inițializarea clientului: {e}"

        system_prompt = self._system_prompt()
        history = self._get_mm_history(sender) if sender else []
        messages = history + [{"role": "user", "content": text}]
        history_len = len(history)

        tools = ALL_TOOL_DECLARATIONS_ANTHROPIC or None

        for round_num in range(_MAX_ROUNDS):
            try:
                kwargs = {
                    "model": self._config.minimax_model,
                    "max_tokens": 4096,
                    "system": system_prompt,
                    "messages": messages,
                }
                if tools:
                    kwargs["tools"] = tools
                response = client.messages.create(**kwargs)
            except Exception as e:
                logger.error(f"Anthropic generate failed (round {round_num}): {e}")
                return f"Eroare la generarea răspunsului: {e}"

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            if not tool_use_blocks:
                text_blocks = [b.text for b in response.content if hasattr(b, "text")]
                result_text = " ".join(text_blocks).strip() or "Niciun răspuns primit."
                if sender:
                    new_msgs = messages[history_len:]
                    new_msgs.append({"role": "assistant", "content": result_text})
                    self._save_mm_history(sender, new_msgs)
                return result_text

            # Serialize assistant content (tool_use blocks) for history
            assistant_content = []
            for b in response.content:
                if b.type == "text":
                    assistant_content.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": b.id,
                        "name": b.name,
                        "input": dict(b.input) if b.input else {},
                    })
            messages.append({"role": "assistant", "content": assistant_content})

            # Execute tools and collect results
            tool_results = []
            for block in tool_use_blocks:
                tool_name = block.name
                args = dict(block.input) if block.input else {}
                logger.info(f"Anthropic tool call: {tool_name}({list(args.keys())})")
                result = self._router.dispatch(tool_name, args)
                if isinstance(result, ConfirmationPending):
                    result = f"Această comandă necesită confirmare vocală și nu poate fi executată de pe telefon: {tool_name}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                })
            messages.append({"role": "user", "content": tool_results})

        # Fell through — plain summary without tools
        try:
            final = client.messages.create(
                model=self._config.minimax_model,
                max_tokens=1024,
                system=system_prompt,
                messages=messages,
            )
            text_blocks = [b.text for b in final.content if hasattr(b, "text")]
            result_text = " ".join(text_blocks).strip() or "Comanda a fost executată."
            if sender:
                new_msgs = messages[history_len:]
                new_msgs.append({"role": "assistant", "content": result_text})
                self._save_mm_history(sender, new_msgs)
            return result_text
        except Exception as e:
            logger.error(f"Anthropic final summary failed: {e}")
            return "Comanda a fost executată."

    # ── OpenAI-compat backend ──────────────────────────────────────────────────

    def _process_openai(self, text: str, sender: str) -> str:
        from openai import OpenAI
        from tools import ALL_TOOL_DECLARATIONS_OPENAI
        from core.command_router import ConfirmationPending

        try:
            client = OpenAI(
                api_key=self._config.minimax_api_key,
                base_url=self._config.minimax_base_url,
            )
        except Exception as e:
            logger.error(f"OpenAI client init failed: {e}")
            return f"Eroare la inițializarea clientului: {e}"

        system_prompt = self._system_prompt()
        history = self._get_mm_history(sender) if sender else []
        messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": text}]
        history_start = len(history)
        tools = ALL_TOOL_DECLARATIONS_OPENAI or None

        for round_num in range(_MAX_ROUNDS):
            try:
                kwargs = {"model": self._config.minimax_model, "messages": messages}
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                response = client.chat.completions.create(**kwargs)
            except Exception as e:
                logger.error(f"OpenAI generate failed (round {round_num}): {e}")
                return f"Eroare la generarea răspunsului: {e}"

            msg = response.choices[0].message
            tool_calls = msg.tool_calls or []

            if not tool_calls:
                result_text = (msg.content or "").strip() or "Niciun răspuns primit."
                if sender:
                    new_msgs = messages[1 + history_start:]
                    new_msgs.append({"role": "assistant", "content": result_text})
                    self._save_mm_history(sender, new_msgs)
                return result_text

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                tool_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                logger.info(f"OpenAI tool call: {tool_name}({list(args.keys())})")
                result = self._router.dispatch(tool_name, args)
                if isinstance(result, ConfirmationPending):
                    result = f"Această comandă necesită confirmare vocală și nu poate fi executată de pe telefon: {tool_name}"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})

        try:
            final = client.chat.completions.create(
                model=self._config.minimax_model, messages=messages)
            result_text = (final.choices[0].message.content or "").strip() or "Comanda a fost executată."
            if sender:
                new_msgs = messages[1 + history_start:]
                new_msgs.append({"role": "assistant", "content": result_text})
                self._save_mm_history(sender, new_msgs)
            return result_text
        except Exception as e:
            logger.error(f"OpenAI final summary failed: {e}")
            return "Comanda a fost executată."

    # ── Gemini fallback backend ────────────────────────────────────────────────

    def _process_gemini(self, text: str, sender: str) -> str:
        from google import genai
        from google.genai import types
        from tools import ALL_TOOL_DECLARATIONS
        from core.command_router import ConfirmationPending

        try:
            client = genai.Client(api_key=self._config.gemini_api_key)
        except Exception as e:
            logger.error(f"Gemini client init failed: {e}")
            return f"Eroare la inițializarea clientului Gemini: {e}"

        system_prompt = self._system_prompt()
        history = self._get_gemini_history(sender) if sender else []
        new_user_msg = types.Content(role="user", parts=[types.Part(text=text)])
        contents = history + [new_user_msg]
        history_len = len(history)

        gen_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=ALL_TOOL_DECLARATIONS)] if ALL_TOOL_DECLARATIONS else [],
        )

        for round_num in range(_MAX_ROUNDS):
            try:
                response = client.models.generate_content(
                    model=_phone_model(), contents=contents, config=gen_config)
            except Exception as e:
                logger.error(f"Gemini generate failed (round {round_num}): {e}")
                return f"Eroare la generarea răspunsului: {e}"

            candidate = response.candidates[0] if response.candidates else None
            if not candidate or not candidate.content or not candidate.content.parts:
                break

            parts = candidate.content.parts
            function_call_parts = [p for p in parts if p.function_call is not None]

            if not function_call_parts:
                text_parts = [p.text for p in parts if p.text]
                result_text = " ".join(text_parts).strip() or "Niciun răspuns primit."
                if sender:
                    new_contents = contents[history_len:]
                    final_content = types.Content(role="model", parts=[types.Part(text=result_text)])
                    self._save_gemini_history(sender, new_contents + [final_content])
                return result_text

            contents.append(candidate.content)
            response_parts = []
            for part in function_call_parts:
                fc = part.function_call
                tool_name = fc.name
                args = dict(fc.args) if fc.args else {}
                logger.info(f"Gemini tool call: {tool_name}({list(args.keys())})")
                result = self._router.dispatch(tool_name, args)
                if isinstance(result, ConfirmationPending):
                    result = f"Această comandă necesită confirmare vocală și nu poate fi executată de pe telefon: {tool_name}"
                response_parts.append(
                    types.Part(function_response=types.FunctionResponse(
                        name=tool_name, response={"result": result}))
                )
            contents.append(types.Content(role="user", parts=response_parts))

        try:
            final = client.models.generate_content(
                model=_phone_model(), contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt))
            result_text = (final.text or "").strip() or "Comanda a fost executată."
            if sender:
                new_contents = contents[history_len:]
                final_content = types.Content(role="model", parts=[types.Part(text=result_text)])
                self._save_gemini_history(sender, new_contents + [final_content])
            return result_text
        except Exception as e:
            logger.error(f"Gemini final summary failed: {e}")
            return "Comanda a fost executată."

    # ── Transcription (Gemini Files API — backend-agnostic) ───────────────────

    def _transcribe(self, audio_bytes: bytes, mime_type: str) -> str:
        from google import genai
        from google.genai import types
        try:
            client = genai.Client(api_key=self._config.gemini_api_key)
        except Exception as e:
            logger.error(f"Gemini transcription client failed: {e}")
            return ""

        uploaded = None
        try:
            uploaded = client.files.upload(
                file=io.BytesIO(audio_bytes),
                config={"mime_type": mime_type, "display_name": "voice_cmd"},
            )
            response = client.models.generate_content(
                model=_phone_model(),
                contents=[
                    types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime_type),
                    "Transcribe this audio exactly as spoken. Output only the transcription.",
                ],
            )
            return response.text.strip() if response.text else ""
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return ""
        finally:
            if uploaded is not None:
                try:
                    client.files.delete(name=uploaded.name)
                except Exception:
                    pass
