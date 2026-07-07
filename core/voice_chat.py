"""
core/voice_chat.py — Voice mode built on the chat brain (STT → chat() → TTS).

Replaces the Gemini Live pipeline as the default voice engine. Voice becomes a
thin ears-and-mouth layer over chat_backend.chat(), so it inherits everything
the text path already does well: tool routing, anti-hallucination rules,
recipient resolution, scheduling, memory. One brain, two interfaces.

Pipeline (three worker threads + the asyncio keep-alive):
  mic (sounddevice, 16 kHz mono) → energy VAD segments an utterance →
  WAV → modules.speech.transcribe (cloud Gemini, or a LOCAL parakeet-rs/Nemotron
        ASR sidecar when MIKO_STT_URL is set — fully on-device, low latency) →
  ModeManager gate (wake word / standby / mode commands) →
  chat_backend.chat(session "voice", allow_actions on) →
  edge-tts synth → ffmpeg decode → sounddevice playback.

Design choices:
  - Half-duplex by default, with optional speech barge-in to stop TTS.
  - No mic? Keep running. TTS, Discord notifications, and daemons still work;
    the mic is retried every 30 s. (The old engine died in a reconnect loop.)
  - The provider/model comes from MIKO_VOICE_PROVIDER / MIKO_VOICE_MODEL
    (default: minimax if a key is set, else gemini) — voice is no longer
    chained to Gemini Live.

Env knobs:
  MIKO_VOICE_ENGINE      chat | live   (read by main.py; default chat)
  MIKO_VOICE_PROVIDER    chat_backend provider id (minimax/gemini/openai/…)
  MIKO_VOICE_MODEL       model override ('' = provider default)
  MIKO_VOICE_FALLBACK_PROVIDER provider to retry after voice brain rate limits (default gemini)
  MIKO_VOICE_FALLBACK_MODEL optional fallback model override
  MIKO_TTS_VOICE         edge-tts voice (default en-US-JennyNeural / ro-RO-AlinaNeural)
  MIKO_VAD_THRESHOLD     RMS floor for speech (default 350)
  MIKO_VOICE_INTERRUPT   true/false (default true)
  MIKO_VOICE_INTERRUPT_MODE  speech | phrase (default speech)
  MIKO_VOICE_INTERRUPT_SENSITIVITY  low | medium | high (default medium)
  MIKO_VOICE_ALLOW_ACTIONS  true/false (default true — voice is the owner)
  MIKO_VOICE_MAX_UTTER_SECS  max single STT clip before forced chunking (default 90)
  MIKO_VOICE_TURN_GRACE_SECS wait for follow-on chunks before sending to Miko (default 0.8)
  MIKO_VOICE_INCOMPLETE_GRACE_SECS extra wait after unfinished phrases (default 3.0)
  MIKO_VAD_END_SILENCE_MS silence before an audio chunk ends (default 900)
"""

import asyncio
import io
import logging
import os
import queue
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from core.mode_manager import ModeManager
    from core.command_router import CommandRouter
    from config import MikoConfig

logger = logging.getLogger("miko.voicechat")

_SAMPLE_RATE = 16000
_FRAME_MS = 30
_FRAME_SAMPLES = _SAMPLE_RATE * _FRAME_MS // 1000     # 480
_PREROLL_FRAMES = 10        # ~300 ms kept from before speech starts
_START_FRAMES = 3           # consecutive voiced frames to open an utterance
_DEFAULT_END_SILENCE_MS = 900
_DEFAULT_MAX_UTTER_SECS = 90
_DEFAULT_TURN_GRACE_SECS = 0.8
_DEFAULT_INCOMPLETE_GRACE_SECS = 3.0
_MIN_VOICED_FRAMES = 12     # ~360 ms of actual speech or we discard (coughs, taps)
_INTERRUPT_PRESETS = {
    "high": (120, 1.10),
    "medium": (240, 1.35),
    "low": (420, 1.75),
}

_VOICE_FRAME = (
    "[VOICE CONVERSATION]\n"
    "The user is SPEAKING to you and your reply is read aloud by TTS. Keep replies "
    "short and natural — 1-3 spoken sentences unless asked for detail. No markdown, "
    "no bullet lists, no code blocks, no emoji, no URLs (say the site name instead). "
    "Numbers and times in words where natural. You still have ALL your tools — call "
    "them exactly as in chat; never just narrate an action.\n"
    "[EMAIL VOICE STYLE]\n"
    "For email searches, say only the best match or at most three short matches: subject, "
    "sender, and date if useful. Do not read snippets, bodies, links, or explanations unless "
    "the user asks for details. When you open/show an email, just confirm it is open on screen; "
    "do not summarize the email afterward. If the user asks for the post/message/button/link "
    "inside an email notification, call open_email_link instead of show_email."
)


def _strip_for_speech(text: str) -> str:
    """Make a chat reply speakable: drop markdown furniture and cap the length."""
    import re
    t = text or ""
    t = re.sub(r"```.*?```", " (code omitted) ", t, flags=re.DOTALL)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)             # images
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)          # links → label
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.MULTILINE)    # headers
    t = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", t)     # bold/italic
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.MULTILINE)   # bullets
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 900:
        cut = t[:900]
        # end on a sentence boundary if one is reasonably close
        for stop in (". ", "! ", "? "):
            i = cut.rfind(stop)
            if i > 500:
                cut = cut[: i + 1]
                break
        t = cut
    return t


def _voice_email_reply(reply: str, tools_used: list, lang: str = "en") -> str:
    """Keep spoken email handling terse; visual details are already on screen."""
    names = set()
    for t in tools_used or []:
        if isinstance(t, str):
            names.add(t)
        elif isinstance(t, dict):
            names.add(t.get("name") or t.get("tool") or "")
    if "open_email_link" in names:
        low = (reply or "").lower()
        if any(x in low for x in ("couldn't", "could not", "no email", "couldn't find", "nu am", "n-am")):
            return reply
        return "L-am deschis pe ecran." if lang == "ro" else "Opened it on your screen."
    if "show_email" in names:
        low = (reply or "").lower()
        if any(x in low for x in ("couldn't", "could not", "no email", "nu am", "n-am")):
            return reply
        return "L-am deschis pe ecran." if lang == "ro" else "Opened it on your screen."
    if names.intersection({"list_emails", "search_emails", "triage_inbox"}) and len(reply or "") > 360:
        import re
        sentences = re.split(r"(?<=[.!?])\s+", (reply or "").strip())
        short = " ".join(s for s in sentences[:2] if s).strip()
        return short or reply
    return reply


def _pcm_to_wav(pcm: bytes, rate: int = _SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class VoiceChat:
    """Drop-in alternative to AudioHandler: same `speak_text()` + `run()` surface."""

    def __init__(self, config: "MikoConfig", mode_manager: "ModeManager",
                 command_router: "CommandRouter", memory_file: Path = None):
        self._config = config
        self._modes = mode_manager
        self._router = command_router
        self._say_q: "queue.Queue[str]" = queue.Queue()
        self._utt_q: "queue.Queue[tuple[bytes, bool, bool, bool]]" = queue.Queue(maxsize=16)
        self._speaking = threading.Event()   # half-duplex: mic gated while set
        self._recording = threading.Event()  # true while VAD is inside a user utterance
        self._interrupting = threading.Event()
        self._stop = threading.Event()
        self._mic_ok = False

        lang = getattr(config, "language", "en")
        self._tts_voice = os.getenv("MIKO_TTS_VOICE", "") or (
            "ro-RO-AlinaNeural" if lang == "ro" else "en-US-JennyNeural")
        self._provider = os.getenv("MIKO_VOICE_PROVIDER", "").strip().lower() or (
            "minimax" if getattr(config, "minimax_api_key", "") else "gemini")
        self._model = os.getenv("MIKO_VOICE_MODEL", "").strip()
        self._fallback_provider = os.getenv("MIKO_VOICE_FALLBACK_PROVIDER", "gemini").strip().lower()
        if self._fallback_provider in ("0", "false", "none", "off", "disabled"):
            self._fallback_provider = ""
        self._fallback_model = os.getenv("MIKO_VOICE_FALLBACK_MODEL", "").strip()
        self._fallback_api_key = os.getenv("MIKO_VOICE_FALLBACK_API_KEY", "").strip()
        self._fallback_base_url = os.getenv("MIKO_VOICE_FALLBACK_BASE_URL", "").strip()
        self._api_key = os.getenv("MIKO_VOICE_API_KEY", "").strip()
        self._base_url = os.getenv("MIKO_VOICE_BASE_URL", "").strip()
        self._compact_tools = (os.getenv("MIKO_VOICE_COMPACT_TOOLS", "true")
                               .strip().lower() not in ("0", "false", "no"))
        self._barge_in = (os.getenv("MIKO_VOICE_INTERRUPT", "true")
                          .strip().lower() not in ("0", "false", "no"))
        self._interrupt_mode = os.getenv("MIKO_VOICE_INTERRUPT_MODE", "speech").strip().lower()
        if self._interrupt_mode not in ("speech", "phrase"):
            self._interrupt_mode = "speech"
        self._interrupt_sensitivity = (
            os.getenv("MIKO_VOICE_INTERRUPT_SENSITIVITY", "medium").strip().lower() or "medium"
        )
        if self._interrupt_sensitivity not in _INTERRUPT_PRESETS:
            self._interrupt_sensitivity = "medium"
        interrupt_min_ms, interrupt_threshold_mult = _INTERRUPT_PRESETS[self._interrupt_sensitivity]
        try:
            interrupt_min_ms = max(60, int(os.getenv("MIKO_VOICE_INTERRUPT_MIN_MS", interrupt_min_ms)))
        except ValueError:
            pass
        try:
            interrupt_threshold_mult = max(
                1.0,
                float(os.getenv("MIKO_VOICE_INTERRUPT_THRESHOLD_MULTIPLIER", interrupt_threshold_mult)),
            )
        except ValueError:
            pass
        self._interrupt_start_frames = max(1, int(interrupt_min_ms / _FRAME_MS))
        self._interrupt_threshold_mult = interrupt_threshold_mult
        self._allow_actions = (os.getenv("MIKO_VOICE_ALLOW_ACTIONS", "true")
                               .strip().lower() not in ("0", "false", "no"))
        try:
            self._vad_floor = max(100, int(os.getenv("MIKO_VAD_THRESHOLD", "350")))
        except ValueError:
            self._vad_floor = 350
        try:
            self._max_utter_secs = max(
                15.0, float(os.getenv("MIKO_VOICE_MAX_UTTER_SECS", str(_DEFAULT_MAX_UTTER_SECS)))
            )
        except ValueError:
            self._max_utter_secs = float(_DEFAULT_MAX_UTTER_SECS)
        try:
            self._turn_grace_secs = max(
                0.0, float(os.getenv("MIKO_VOICE_TURN_GRACE_SECS", str(_DEFAULT_TURN_GRACE_SECS)))
            )
        except ValueError:
            self._turn_grace_secs = _DEFAULT_TURN_GRACE_SECS
        try:
            end_silence_ms = max(
                600, int(os.getenv("MIKO_VAD_END_SILENCE_MS", str(_DEFAULT_END_SILENCE_MS)))
            )
        except ValueError:
            end_silence_ms = _DEFAULT_END_SILENCE_MS
        self._end_silence_frames = max(1, int(end_silence_ms / _FRAME_MS))
        try:
            self._incomplete_grace_secs = max(
                self._turn_grace_secs,
                float(os.getenv("MIKO_VOICE_INCOMPLETE_GRACE_SECS", str(_DEFAULT_INCOMPLETE_GRACE_SECS))),
            )
        except ValueError:
            self._incomplete_grace_secs = _DEFAULT_INCOMPLETE_GRACE_SECS

    def _chat_creds(self) -> tuple[str, str]:
        """Return (api_key, base_url) for the configured chat provider.

        MIKO_VOICE_API_KEY / MIKO_VOICE_BASE_URL are voice-specific overrides,
        useful for local OpenAI-compatible servers such as LM Studio.

        The WebUI can keep provider keys in browser-local storage and sends them
        to the backend per chat request. Voice has no browser, so when .env is
        blank we borrow the latest in-memory WebUI credentials if they match the
        voice provider. The key is not written to disk here.
        """
        if self._api_key or self._base_url:
            return self._api_key, self._base_url

        try:
            import chat_backend
            preset = chat_backend.PROVIDERS.get(self._provider) or {}
            env_key = preset.get("env_key") or ""
            key = os.getenv(env_key, "").strip() if env_key else ""
            base = preset.get("base_url") or ""
            if key:
                return key, ""
        except Exception:
            base = ""

        try:
            from modules import runtime_model
            rm = runtime_model.get()
            rp = (rm.get("provider") or "").strip().lower()
            rb = (rm.get("base_url") or "").strip()
            same_provider = rp == self._provider
            openrouter_custom = (
                self._provider == "openrouter"
                and (rp == "openrouter" or "openrouter.ai" in rb.lower())
            )
            if same_provider or openrouter_custom:
                key = (rm.get("api_key") or "").strip()
                if key:
                    return key, rb if openrouter_custom and rb else ""
        except Exception:
            pass
        return "", ""

    # ── Public thread-safe API (same surface as AudioHandler) ────────────────

    def _is_interrupt_phrase(self, text: str) -> bool:
        import re
        t = re.sub(r"[^a-z0-9ăâîșşțţ\s-]", " ", (text or "").lower())
        t = re.sub(r"\s+", " ", t).strip()
        if not t:
            return False
        phrases = (
            "miko stop", "miko stop talking", "miko stop speaking",
            "miko pause", "miko enough", "miko be quiet", "miko quiet",
            "miko shut up", "stop miko", "stop talking", "stop speaking",
            "be quiet", "enough miko", "that's enough", "that s enough",
            "that is enough",
            "miko taci", "miko gata", "miko opreste", "miko opreste-te",
            "miko oprește", "miko oprește-te", "taci miko", "gata miko",
        )
        return any(p in t for p in phrases)

    def _stop_speech(self, clear_queue: bool = True) -> None:
        """Stop current TTS playback and optionally drop queued speech."""
        self._interrupting.set()
        if clear_queue:
            try:
                while True:
                    self._say_q.get_nowait()
            except queue.Empty:
                pass
        try:
            import sounddevice as sd
            sd.stop()
        except Exception as e:
            logger.debug(f"sd.stop during interrupt failed: {e}")

    def speak_text(self, text: str) -> None:
        """Queue text to be spoken aloud. Thread-safe; used by Discord poll,
        reminders, and mode-change acks."""
        text = (text or "").strip()
        if text:
            self._say_q.put(text)

    # ── Mic capture + VAD segmentation ────────────────────────────────────────

    def _mic_loop(self) -> None:
        """Open the mic and hand voiced utterances to the brain queue. If no mic is
        available, keep the rest of Miko alive and retry every 30 s."""
        import sounddevice as sd
        frames: "queue.Queue[tuple[bytes, bool]]" = queue.Queue(maxsize=200)

        def _cb(indata, _frames, _time, status):
            if status:
                logger.debug(f"mic status: {status}")
            was_speaking = self._speaking.is_set()
            if was_speaking and not self._barge_in:
                return                          # half-duplex: ignore own voice
            if not frames.full():
                frames.put_nowait((indata[:, 0].astype(np.int16).tobytes(), was_speaking))

        while not self._stop.is_set():
            try:
                stream = sd.InputStream(
                    samplerate=_SAMPLE_RATE, channels=1, dtype="int16",
                    blocksize=_FRAME_SAMPLES, callback=_cb)
                stream.start()
            except Exception as e:
                if not self._mic_ok:
                    logger.warning(f"No microphone available ({e}) — voice input off, "
                                   f"retrying every 30s. Speech output still works.")
                self._mic_ok = False
                if self._stop.wait(30):
                    return
                continue

            self._mic_ok = True
            logger.info(f"Mic open @ {_SAMPLE_RATE} Hz — listening")
            print("[Miko] Listening... (speak normally)")
            try:
                self._vad_consume(frames)
            except Exception as e:
                logger.warning(f"mic loop error: {e} — reopening")
            finally:
                self._recording.clear()
                try:
                    stream.stop(); stream.close()
                except Exception:
                    pass

    def _vad_consume(self, frames: "queue.Queue[tuple[bytes, bool]]") -> None:
        """Energy-gate the frame stream into utterances. Adaptive noise floor:
        speech = RMS above max(static floor, 3× ambient)."""
        preroll: list[tuple[bytes, bool]] = []
        utter: list[tuple[bytes, bool]] = []
        voiced_run = 0
        silence_run = 0
        voiced_total = 0
        in_utt = False
        barge_interrupted = False
        interrupt_voiced_run = 0
        ambient = 200.0                          # EMA of non-speech RMS

        while not self._stop.is_set():
            try:
                frame, was_speaking = frames.get(timeout=1.0)
            except queue.Empty:
                continue
            if self._speaking.is_set() and not self._barge_in:
                preroll, utter = [], []
                in_utt = False
                self._recording.clear()
                voiced_run = silence_run = voiced_total = 0
                barge_interrupted = False
                interrupt_voiced_run = 0
                continue

            rms = float(np.sqrt(np.mean(
                np.frombuffer(frame, dtype=np.int16).astype(np.float64) ** 2)) or 0)
            threshold = max(self._vad_floor, ambient * 3.0)
            voiced = rms > threshold
            interrupt_voiced = rms > (threshold * self._interrupt_threshold_mult)
            if (was_speaking and self._barge_in
                    and self._interrupt_mode == "speech"
                    and not self._interrupting.is_set()):
                interrupt_voiced_run = interrupt_voiced_run + 1 if interrupt_voiced else 0
                if interrupt_voiced_run >= self._interrupt_start_frames:
                    logger.info(
                        "voice barge-in detected; stopping TTS "
                        f"(sensitivity={self._interrupt_sensitivity})"
                    )
                    print("[Miko] Interrupted.")
                    self._stop_speech()
                    barge_interrupted = True
            else:
                interrupt_voiced_run = 0
            if not voiced:
                ambient = ambient * 0.95 + rms * 0.05

            if not in_utt:
                preroll.append((frame, was_speaking))
                if len(preroll) > _PREROLL_FRAMES:
                    preroll.pop(0)
                voiced_run = voiced_run + 1 if voiced else 0
                if voiced_run >= _START_FRAMES:
                    in_utt = True
                    self._recording.set()
                    utter = preroll[:]
                    preroll = []
                    silence_run = 0
                    voiced_total = voiced_run
                continue

            utter.append((frame, was_speaking))
            if voiced:
                voiced_total += 1
                silence_run = 0
            else:
                silence_run += 1

            too_long = len(utter) * _FRAME_MS / 1000 >= self._max_utter_secs
            if silence_run >= self._end_silence_frames or too_long:
                in_utt = False
                self._recording.clear()
                voiced_run = 0
                pcm = b"".join(f for f, _s in utter)
                utter_from_speaking = any(_s for _f, _s in utter)
                utter = []
                if voiced_total >= _MIN_VOICED_FRAMES:
                    try:
                        self._utt_q.put((pcm, utter_from_speaking, barge_interrupted, too_long), timeout=0.25)
                    except queue.Full:
                        logger.warning("voice utterance queue full; dropped transcript chunk")
                voiced_total = 0
                barge_interrupted = False
                interrupt_voiced_run = 0

    # ── Brain: STT → mode gate → chat() ──────────────────────────────────────

    def _looks_incomplete_turn(self, text: str) -> bool:
        """Return true when the transcript strongly sounds like the user paused mid-thought."""
        import re

        t = re.sub(r"[^\wăâîșşțţ\s-]+$", "", (text or "").lower()).strip()
        if not t:
            return False
        endings = (
            "about", "regarding", "related to", "relating to", "in relation to",
            "for", "with", "from", "by", "that", "which", "who", "where",
            "show me", "open the", "can you show me", "i need to show me",
            "most recent", "the most recent", "recent emails", "emails about",
            "emails regarding", "emails related to",
            "despre", "legate de", "legat de", "în legătură cu", "in legatura cu",
            "pentru", "cu", "de la", "care", "unde", "arată-mi", "arata-mi",
            "cele mai recente", "email uri legate de", "emailuri legate de",
        )
        return any(t.endswith(e) for e in endings)

    def _transcribe_turn(
        self,
        first: "tuple[bytes, bool, bool, bool]",
        speech,
        lang: str,
    ) -> tuple[str, bool, bool]:
        """Transcribe and merge adjacent VAD chunks into one user turn.

        Long spoken prompts can naturally split on pauses or the max clip length.
        Holding briefly for follow-on chunks keeps Miko from answering a half-prompt.
        """
        texts: list[str] = []
        from_speaking_any = False
        barge_any = False
        item = first

        while not self._stop.is_set():
            pcm, from_speaking, was_barge_interrupt, forced_split = item
            from_speaking_any = from_speaking_any or from_speaking
            barge_any = barge_any or was_barge_interrupt
            try:
                text = speech.transcribe(_pcm_to_wav(pcm), "audio/wav", language=lang)
            except Exception as e:
                logger.warning(f"voice STT failed: {e}")
                text = ""
            text = (text or "").strip()
            if text:
                texts.append(text)
                logger.info(f"voice transcript chunk ({len(text)} chars): {text[:100]}")

            while not self._stop.is_set():
                joined = " ".join(texts).strip()
                wait = 0.25 if self._recording.is_set() else self._turn_grace_secs
                if not self._recording.is_set() and self._looks_incomplete_turn(joined):
                    wait = max(wait, self._incomplete_grace_secs)
                if forced_split:
                    wait = max(wait, 0.5)
                try:
                    item = self._utt_q.get(timeout=wait)
                    forced_split = item[3]
                    break
                except queue.Empty:
                    if self._recording.is_set():
                        continue
                    if self._looks_incomplete_turn(" ".join(texts).strip()):
                        logger.info("voice turn ended on an unfinished phrase; sending after extended grace")
                    return " ".join(texts).strip(), from_speaking_any, barge_any

        return " ".join(texts).strip(), from_speaking_any, barge_any

    def _brain_loop(self) -> None:
        import chat_backend
        from modules import speech

        lang = getattr(self._config, "language", "en")
        while not self._stop.is_set():
            try:
                first = self._utt_q.get(timeout=1.0)
            except queue.Empty:
                continue
            text, from_speaking, was_barge_interrupt = self._transcribe_turn(first, speech, lang)
            if not text or len(text) < 2:
                continue
            print(f"[Tu]   {text}")
            logger.info(f"voice heard ({len(text)} chars): {text[:100]}")
            if (from_speaking or self._speaking.is_set()) and self._is_interrupt_phrase(text):
                logger.info(f"voice interrupt: {text[:80]}")
                print("[Miko] Stopped.")
                self._stop_speech()
                continue
            if from_speaking and not was_barge_interrupt:
                logger.debug("discarded barge-in audio that was not an interrupt phrase")
                continue

            # Mode commands ("standby", "wake up", …) short-circuit the brain;
            # the ModeManager speaks its own acknowledgement via speak_text.
            if self._modes.detect_and_apply_mode_change(text):
                continue
            if not self._modes.should_process_transcription(text):
                logger.debug("gated by mode (no wake word)")
                continue

            try:
                api_key, base_url = self._chat_creds()
                out = chat_backend.chat(
                    router=self._router, session_id="voice", message=text,
                    provider=self._provider, model=self._model,
                    api_key=api_key, base_url=base_url,
                    allow_actions=self._allow_actions,
                    owner_name=self._config.owner_name, language=lang,
                    system_extra=_VOICE_FRAME,
                    compact_tools=self._compact_tools,
                    fallback_provider=self._fallback_provider,
                    fallback_model=self._fallback_model,
                    fallback_api_key=self._fallback_api_key,
                    fallback_base_url=self._fallback_base_url,
                )
            except Exception as e:
                logger.error(f"voice chat() error: {e}")
                self.speak_text("Sorry, I hit an error handling that."
                                if lang != "ro" else "Scuze, am dat de o eroare.")
                continue
            if out.get("error"):
                logger.error(f"voice chat() error: {out['error']}")
                self.speak_text("Sorry, that failed." if lang != "ro" else "Scuze, n-a mers.")
                continue
            reply = _voice_email_reply(out.get("reply") or "", out.get("tools_used") or [], lang)
            reply = _strip_for_speech(reply)
            if reply:
                print(f"[Miko] {reply}")
                self.speak_text(reply)
            self._modes.refresh_window()    # keep the standby follow-up window open

    # ── Mouth: edge-tts → ffmpeg decode → sounddevice playback ───────────────

    def _synth(self, text: str) -> Optional[bytes]:
        """Text → 24 kHz mono PCM via edge-tts + ffmpeg. None on failure."""
        try:
            import edge_tts

            async def _gen() -> bytes:
                chunks = []
                async for part in edge_tts.Communicate(text, self._tts_voice).stream():
                    if part["type"] == "audio":
                        chunks.append(part["data"])
                return b"".join(chunks)

            mp3 = asyncio.run(_gen())
            if not mp3:
                return None
            from modules.speech import _ffmpeg_exe
            proc = subprocess.run(
                [_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                 "-ac", "1", "-ar", "24000", "-f", "s16le", "pipe:1"],
                input=mp3, capture_output=True, timeout=60)
            return proc.stdout if proc.returncode == 0 and proc.stdout else None
        except Exception as e:
            logger.warning(f"TTS synth failed: {e}")
            return None

    def _tts_loop(self) -> None:
        import sounddevice as sd
        while not self._stop.is_set():
            try:
                text = self._say_q.get(timeout=1.0)
            except queue.Empty:
                continue
            pcm = self._synth(text)
            if not pcm:
                continue
            self._interrupting.clear()
            self._speaking.set()
            try:
                arr = np.frombuffer(pcm, dtype=np.int16)
                sd.play(arr, samplerate=24000, blocking=True)
            except Exception as e:
                logger.warning(f"playback failed: {e}")
            finally:
                time.sleep(0.25)        # echo tail before re-opening the mic
                self._speaking.clear()
                self._interrupting.clear()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the worker threads and keep the event loop alive (matches the
        AudioHandler.run() interface main.py awaits)."""
        logger.info(f"Voice-chat engine: provider={self._provider} "
                    f"model={self._model or '(default)'} tts={self._tts_voice}")
        print(f"[Miko] Voice mode (chat brain) — provider: {self._provider}"
              f"{' / ' + self._model if self._model else ''}, voice: {self._tts_voice}")
        threads = [
            threading.Thread(target=self._mic_loop, daemon=True, name="VoiceMic"),
            threading.Thread(target=self._brain_loop, daemon=True, name="VoiceBrain"),
            threading.Thread(target=self._tts_loop, daemon=True, name="VoiceTTS"),
        ]
        for t in threads:
            t.start()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            self._stop.set()
