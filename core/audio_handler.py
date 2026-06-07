"""
core/audio_handler.py — Gemini Live audio pipeline.

Manages the entire WebSocket session: mic capture, audio playback,
transcription filtering through ModeManager, and tool dispatch.
Adapted from jarvis/main.py MikoLive class with proper class separation.
"""

import asyncio
import logging
import threading
import traceback
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types

if TYPE_CHECKING:
    from core.mode_manager import ModeManager
    from core.command_router import CommandRouter, ConfirmationPending
    from config import MikoConfig

logger = logging.getLogger("miko.audio")


class AudioHandler:
    def __init__(
        self,
        config: "MikoConfig",
        mode_manager: "ModeManager",
        command_router: "CommandRouter",
        memory_file: Path,
    ):
        self._config        = config
        self._mode_manager  = mode_manager
        self._router        = command_router
        self._memory_file   = memory_file

        self.session         = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.audio_in_queue: asyncio.Queue | None    = None
        self.out_queue: asyncio.Queue | None         = None

        # Dedicated pool for (possibly slow) tool dispatch — RPC handshakes, HTTP
        # token refreshes, UI automation — so they never starve audio-I/O threads.
        from concurrent.futures import ThreadPoolExecutor
        self._tool_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ToolExec")

    # ── Public thread-safe API ────────────────────────────────────────────────

    def speak_text(self, text: str) -> None:
        """
        Inject text into the live session so Miko speaks it aloud.
        Thread-safe — can be called from any daemon thread.

        Uses send_realtime_input(text=) instead of send_client_content to avoid
        interleaving ordered and realtime channels on the same session, which
        gemini-3.1-flash-live-preview rejects with 'invalid argument'.
        """
        if not self._loop or not self.session:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.session.send_realtime_input(text=text),
                self._loop,
            )
        except Exception as e:
            logger.warning(f"speak_text error: {e}")

    # ── Session config ────────────────────────────────────────────────────────

    def _build_live_config(self) -> types.LiveConnectConfig:
        """Rebuilt on each reconnect so mode prompt addendum stays current."""
        from memory.memory_manager import load_memory, format_memory_for_prompt
        from tools import ALL_TOOL_DECLARATIONS

        memory  = load_memory(self._memory_file)
        mem_str = format_memory_for_prompt(memory)
        now_str = datetime.now().strftime("%A, %d %B %Y — %H:%M")

        is_en = getattr(self._config, "language", "en") == "en"
        prompt_file = "prompt_en.txt" if is_en else "prompt.txt"
        prompt_path = self._config.base_dir / "core" / prompt_file
        try:
            base = prompt_path.read_text(encoding="utf-8")
            base = base.replace("{owner_name}", self._config.owner_name)
        except Exception:
            if is_en:
                base = (
                    f"You are Miko, {self._config.owner_name}'s personal assistant. "
                    "You speak English, you're direct and friendly."
                )
            else:
                base = (
                    f"Ești Miko, asistentul personal al lui {self._config.owner_name}. "
                    "Vorbești în română, ești direct și prietenos."
                )

        if is_en:
            sys_prompt = f"[CURRENT DATE AND TIME]\nIt is now: {now_str}\n\n"
        else:
            sys_prompt = f"[DATA ȘI ORA CURENTA]\nAcum este: {now_str}\n\n"
        if mem_str:
            sys_prompt += mem_str + "\n"
        sys_prompt += base
        sys_prompt += self._mode_manager.get_mode_prompt_addendum()

        # Voice parity with the chat "brain": layer in the configured ECC agent/skills
        # (MIKO_VOICE_AGENT / MIKO_VOICE_SKILLS) and a little recalled context (the
        # latest reflection insights). The `recall` tool gives query-specific depth.
        try:
            import os as _os
            import agent_skills
            overlay = agent_skills.build_overlay(
                _os.getenv("MIKO_VOICE_AGENT", ""),
                [s.strip() for s in _os.getenv("MIKO_VOICE_SKILLS", "").split(",") if s.strip()],
            )
            if overlay:
                sys_prompt += overlay
        except Exception:
            pass
        try:
            from memory import knowledge_store as KS
            insights = KS.recent("insight", 3)
            if insights:
                label = "WHAT'S BEEN GOING ON" if is_en else "CE S-A ÎNTÂMPLAT RECENT"
                sys_prompt += f"\n\n[{label}]\n" + "\n".join(f"- {i}" for i in insights)
        except Exception:
            pass
        # Voice can't re-query memory per utterance the way chat does, so make recall a
        # reflex: before answering anything that depends on the user's history, notes,
        # projects, or past conversations, call the `recall` tool first.
        if is_en:
            sys_prompt += ("\n\n[MEMORY]\nBefore answering questions about the user's past, "
                           "their notes, projects, or anything 'do you remember…', call the "
                           "`recall` tool first, then answer from what it returns.")
        else:
            sys_prompt += ("\n\n[MEMORIE]\nÎnainte să răspunzi la întrebări despre trecutul "
                           "utilizatorului, notițele, proiectele lui sau 'îți amintești…', "
                           "folosește întâi unealta `recall`, apoi răspunde din ce găsești.")
        try:
            import schedule_briefs
            brief = schedule_briefs.get_today_brief()
            if brief:
                label = "TODAY'S SCHEDULE" if is_en else "PROGRAMUL DE AZI"
                sys_prompt += f"\n\n[{label}]\n{brief}"
        except Exception:
            pass
        try:
            import modules.projects as PR
            pl = PR.get_active_projects_line()
            if pl:
                sys_prompt += f"\n\n[{pl}]"
        except Exception:
            pass

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=sys_prompt,
            tools=[{"function_declarations": ALL_TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._config.voice_name
                    )
                )
            ),
        )

    # ── Tool execution ────────────────────────────────────────────────────────

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})
        loop = asyncio.get_event_loop()

        result = await loop.run_in_executor(
            self._tool_executor, lambda: self._router.dispatch(name, args)
        )

        # Handle ConfirmationPending sentinel
        from core.command_router import ConfirmationPending
        if isinstance(result, ConfirmationPending):
            logger.info(f"Confirmation pending for: {name}")
            # Tell Gemini to ask the user for confirmation
            return types.FunctionResponse(
                id=fc.id,
                name=name,
                response={"result": result.prompt},
            )

        result_str = str(result or "Gata, sefu.")
        logger.info(f"Tool {name} → {result_str[:80]}")

        return types.FunctionResponse(
            id=fc.id,
            name=name,
            response={"result": result_str},
        )

    # ── Audio I/O coroutines ──────────────────────────────────────────────────

    async def _capture_mic(self) -> None:
        """Reads microphone via sounddevice, pushes PCM chunks to out_queue."""
        logger.info(f"Opening mic @ {self._config.send_sample_rate}Hz")
        loop = asyncio.get_event_loop()

        def _callback(indata, frames, _time, status):
            if status:
                logger.debug(f"Mic status: {status}")
            raw = indata.flatten().astype(np.int16).tobytes()
            # Non-blocking put — drop old chunks to prevent buffering lag
            if not self.out_queue.full():
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": raw, "mime_type": "audio/pcm;rate=16000"},
                )

        with sd.InputStream(
            samplerate=self._config.send_sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self._config.chunk_size,
            callback=_callback,
        ):
            logger.info("Mic open — listening...")
            while True:
                await asyncio.sleep(0.1)

    async def _mic_to_session(self) -> None:
        """Forwards mic queue chunks to the Gemini Live WebSocket."""
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(audio=msg)

    async def _play_audio(self) -> None:
        """Plays audio received from Gemini in real time."""
        logger.info(f"Output audio @ {self._config.receive_sample_rate}Hz")
        loop     = asyncio.get_event_loop()
        stream   = sd.OutputStream(
            samplerate=self._config.receive_sample_rate,
            channels=1,
            dtype="int16",
        )
        stream.start()
        try:
            while True:
                chunk = await self.audio_in_queue.get()
                if chunk:
                    arr = np.frombuffer(chunk, dtype=np.int16)
                    await loop.run_in_executor(None, stream.write, arr)
        except Exception as e:
            logger.error(f"Playback error: {e}")
        finally:
            stream.stop()
            stream.close()

    async def _receive_responses(self) -> None:
        """
        Core receive loop. Handles:
        - Audio chunks → audio_in_queue → playback
        - Transcriptions → mode filter → confirmation check → logging
        - Tool calls → command_router.dispatch() → FunctionResponse
        - turn_complete → memory extraction trigger

        STANDBY audio gate: audio chunks are buffered per-turn and only released
        to the playback queue when a wake word is detected in the input transcription.
        If the turn ends with no wake word, the buffered audio is silently discarded.
        """
        in_buf:  list[str] = []
        out_buf: list[str] = []

        try:
            while True:
                turn = self.session.receive()

                # Per-turn quiet state — snapshot at turn start (STANDBY or MUTE):
                # playback is buffered and only released on a wake word.
                _standby = self._mode_manager.is_quiet()
                _wake    = False          # True once wake word is confirmed this turn
                _buf: list[bytes] = []    # Holds audio chunks until wake word or turn end

                async for response in turn:

                    # ── Audio chunk ──────────────────────────────────────────
                    if response.data:
                        if _standby and not _wake:
                            _buf.append(response.data)
                        else:
                            self.audio_in_queue.put_nowait(response.data)

                    # ── Server content ───────────────────────────────────────
                    if response.server_content:
                        sc = response.server_content

                        if sc.input_transcription and sc.input_transcription.text:
                            t = sc.input_transcription.text.strip()
                            if t:
                                in_buf.append(t)
                                # Real-time wake word check while still in the turn —
                                # flush buffered audio immediately if wake word found
                                if _standby and not _wake and \
                                        self._mode_manager.should_process_transcription(t):
                                    _wake = True
                                    for c in _buf:
                                        self.audio_in_queue.put_nowait(c)
                                    _buf = []

                        if sc.output_transcription and sc.output_transcription.text:
                            t = sc.output_transcription.text.strip()
                            if t:
                                out_buf.append(t)

                        if sc.turn_complete:
                            _buf = []  # Discard — wake word was never detected this turn

                            full_in  = " ".join(in_buf).strip()
                            full_out = " ".join(out_buf).strip()
                            in_buf   = []
                            out_buf  = []

                            if full_in:
                                print(f"[Tu]   {full_in}")
                                self._handle_transcription(full_in)
                            if full_out and (not _standby or _wake):
                                print(f"[Miko] {full_out}")

                            # Trigger memory extraction every N turns — but NOT for
                            # ambient speech in STANDBY/MUTE (no wake word), so passing
                            # conversation doesn't get ingested as if you talked to Miko.
                            if full_in and len(full_in) > 5 and (not _standby or _wake):
                                from memory.memory_manager import update_from_conversation_async
                                update_from_conversation_async(
                                    self._memory_file,
                                    self._config.gemini_api_key,
                                    full_in,
                                    full_out,
                                    minimax_api_key=getattr(self._config, "minimax_api_key", ""),
                                    minimax_base_url=getattr(self._config, "minimax_base_url", ""),
                                    minimax_model=getattr(self._config, "minimax_model", ""),
                                    session_id="voice",
                                )

                    # ── Tool calls ───────────────────────────────────────────
                    if response.tool_call:
                        # STANDBY/MUTE guard: the model hears everything (we need the
                        # audio to detect the wake word), but it must NOT *act* on what
                        # it hears unless this turn was woken. Otherwise ambient speech
                        # like "send a message…" gets executed silently by accident.
                        if _standby and not _wake:
                            blocked = []
                            for fc in response.tool_call.function_calls:
                                logger.info(f"STANDBY blocked tool (no wake word): {fc.name}")
                                blocked.append(types.FunctionResponse(
                                    id=fc.id,
                                    name=fc.name,
                                    response={"result": "Ignored: Miko is in standby and "
                                              "was not addressed (no wake word). Take no action."},
                                ))
                            await self.session.send_tool_response(function_responses=blocked)
                        else:
                            responses = []
                            for fc in response.tool_call.function_calls:
                                logger.info(f"Tool call: {fc.name}")
                                fr = await self._execute_tool(fc)
                                responses.append(fr)
                            await self.session.send_tool_response(function_responses=responses)

        except Exception as e:
            logger.error(f"Receive loop error: {e}")
            traceback.print_exc()
            raise

    def _handle_transcription(self, text: str) -> None:
        """
        Processes a completed user transcription.
        Order: mode-change detection → confirmation check → mode gate.
        If passed, send text back to Gemini as context (Gemini decides routing).
        """
        # 1. Mode-change commands take priority and bypass normal routing
        if self._mode_manager.detect_and_apply_mode_change(text):
            return

        # 2. If there's a pending confirmation, try to resolve it
        if self._router.has_pending_confirmation:
            result = self._router.check_and_resolve_confirmation(text)
            if result is not None:
                logger.info(f"Confirmation resolved → {result[:80]}")
                self.speak_text(result)
                return

        # 3. Mode gate — STANDBY drops non-wake-word speech
        if not self._mode_manager.should_process_transcription(text):
            logger.debug(f"STANDBY filtered: {text[:40]}")
            return

        # In ACTIVE/AUTO mode, Gemini already heard the audio and responds.
        # We don't need to re-send; the transcription tracking is for logging/memory.

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop — connects, starts TaskGroup, reconnects on any error."""
        import winsound

        client = genai.Client(
            api_key=self._config.gemini_api_key,
            http_options={"api_version": "v1beta"},
        )

        while True:
            try:
                logger.info("Connecting to Gemini Live...")
                config = self._build_live_config()

                async with (
                    client.aio.live.connect(
                        model=self._config.live_model, config=config
                    ) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session        = session
                    self._loop          = asyncio.get_event_loop()
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue      = asyncio.Queue(maxsize=20)

                    print("[Miko] Connected! Speak..." if self._config.language == "en"
                          else "[Miko] Conectat! Vorbește...")
                    try:
                        winsound.Beep(880, 80)
                        time.sleep(0.05)
                        winsound.Beep(1100, 80)
                    except Exception:
                        pass

                    tg.create_task(self._mic_to_session())
                    tg.create_task(self._capture_mic())
                    tg.create_task(self._receive_responses())
                    tg.create_task(self._play_audio())

            except KeyboardInterrupt:
                print("\n[Miko] Goodbye!" if self._config.language == "en"
                      else "\n[Miko] La revedere!")
                return
            except Exception as e:
                logger.error(f"Session error: {e}")
                traceback.print_exc()

            # Visible + audible feedback so the user knows it's auto-recovering,
            # not dead. (Gemini Live preview drops connections periodically.)
            print("[Miko] Connection dropped — reconnecting in 3 seconds..." if self._config.language == "en"
                  else "[Miko] Conexiune întreruptă — mă reconectez în 3 secunde...")
            try:
                winsound.Beep(440, 200)
            except Exception:
                pass
            logger.info("Reconnecting in 3 seconds...")
            await asyncio.sleep(3)
