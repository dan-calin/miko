"""
core/voice_chat.py — Voice mode built on the chat brain (STT → chat() → TTS).

Replaces the Gemini Live pipeline as the default voice engine. Voice becomes a
thin ears-and-mouth layer over chat_backend.chat(), so it inherits everything
the text path already does well: tool routing, anti-hallucination rules,
recipient resolution, scheduling, memory. One brain, two interfaces.

Pipeline (three worker threads + the asyncio keep-alive):
  mic (sounddevice, 16 kHz mono) → energy VAD segments an utterance →
  WAV → modules.speech.transcribe (Gemini, auto language) →
  ModeManager gate (wake word / standby / mode commands) →
  chat_backend.chat(session "voice", allow_actions on) →
  edge-tts synth → ffmpeg decode → sounddevice playback.

Design choices:
  - Half-duplex: the mic is ignored while Miko is speaking (echo prevention).
  - No mic? Keep running. TTS, Discord notifications, and daemons still work;
    the mic is retried every 30 s. (The old engine died in a reconnect loop.)
  - The provider/model comes from MIKO_VOICE_PROVIDER / MIKO_VOICE_MODEL
    (default: minimax if a key is set, else gemini) — voice is no longer
    chained to Gemini Live.

Env knobs:
  MIKO_VOICE_ENGINE      chat | live   (read by main.py; default chat)
  MIKO_VOICE_PROVIDER    chat_backend provider id (minimax/gemini/openai/…)
  MIKO_VOICE_MODEL       model override ('' = provider default)
  MIKO_TTS_VOICE         edge-tts voice (default en-US-JennyNeural / ro-RO-AlinaNeural)
  MIKO_VAD_THRESHOLD     RMS floor for speech (default 350)
  MIKO_VOICE_ALLOW_ACTIONS  true/false (default true — voice is the owner)
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
_END_SILENCE_FRAMES = 27    # ~810 ms of silence ends the utterance
_MAX_UTTER_SECS = 30
_MIN_VOICED_FRAMES = 12     # ~360 ms of actual speech or we discard (coughs, taps)

_VOICE_FRAME = (
    "[VOICE CONVERSATION]\n"
    "The user is SPEAKING to you and your reply is read aloud by TTS. Keep replies "
    "short and natural — 1-3 spoken sentences unless asked for detail. No markdown, "
    "no bullet lists, no code blocks, no emoji, no URLs (say the site name instead). "
    "Numbers and times in words where natural. You still have ALL your tools — call "
    "them exactly as in chat; never just narrate an action."
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
        self._utt_q: "queue.Queue[bytes]" = queue.Queue(maxsize=4)
        self._speaking = threading.Event()   # half-duplex: mic gated while set
        self._stop = threading.Event()
        self._mic_ok = False

        lang = getattr(config, "language", "en")
        self._tts_voice = os.getenv("MIKO_TTS_VOICE", "") or (
            "ro-RO-AlinaNeural" if lang == "ro" else "en-US-JennyNeural")
        self._provider = os.getenv("MIKO_VOICE_PROVIDER", "").strip().lower() or (
            "minimax" if getattr(config, "minimax_api_key", "") else "gemini")
        self._model = os.getenv("MIKO_VOICE_MODEL", "").strip()
        self._allow_actions = (os.getenv("MIKO_VOICE_ALLOW_ACTIONS", "true")
                               .strip().lower() not in ("0", "false", "no"))
        try:
            self._vad_floor = max(100, int(os.getenv("MIKO_VAD_THRESHOLD", "350")))
        except ValueError:
            self._vad_floor = 350

    # ── Public thread-safe API (same surface as AudioHandler) ────────────────

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
        frames: "queue.Queue[bytes]" = queue.Queue(maxsize=200)

        def _cb(indata, _frames, _time, status):
            if status:
                logger.debug(f"mic status: {status}")
            if self._speaking.is_set():
                return                          # half-duplex: ignore own voice
            if not frames.full():
                frames.put_nowait(indata[:, 0].astype(np.int16).tobytes())

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
                try:
                    stream.stop(); stream.close()
                except Exception:
                    pass

    def _vad_consume(self, frames: "queue.Queue[bytes]") -> None:
        """Energy-gate the frame stream into utterances. Adaptive noise floor:
        speech = RMS above max(static floor, 3× ambient)."""
        preroll: list[bytes] = []
        utter: list[bytes] = []
        voiced_run = 0
        silence_run = 0
        voiced_total = 0
        in_utt = False
        ambient = 200.0                          # EMA of non-speech RMS

        while not self._stop.is_set():
            try:
                frame = frames.get(timeout=1.0)
            except queue.Empty:
                continue
            if self._speaking.is_set():          # flush state while Miko talks
                preroll, utter = [], []
                in_utt = False
                voiced_run = silence_run = voiced_total = 0
                continue

            rms = float(np.sqrt(np.mean(
                np.frombuffer(frame, dtype=np.int16).astype(np.float64) ** 2)) or 0)
            threshold = max(self._vad_floor, ambient * 3.0)
            voiced = rms > threshold
            if not voiced:
                ambient = ambient * 0.95 + rms * 0.05

            if not in_utt:
                preroll.append(frame)
                if len(preroll) > _PREROLL_FRAMES:
                    preroll.pop(0)
                voiced_run = voiced_run + 1 if voiced else 0
                if voiced_run >= _START_FRAMES:
                    in_utt = True
                    utter = preroll[:]
                    preroll = []
                    silence_run = 0
                    voiced_total = voiced_run
                continue

            utter.append(frame)
            if voiced:
                voiced_total += 1
                silence_run = 0
            else:
                silence_run += 1

            too_long = len(utter) * _FRAME_MS / 1000 >= _MAX_UTTER_SECS
            if silence_run >= _END_SILENCE_FRAMES or too_long:
                in_utt = False
                voiced_run = 0
                pcm = b"".join(utter)
                utter = []
                if voiced_total >= _MIN_VOICED_FRAMES and not self._utt_q.full():
                    self._utt_q.put(pcm)
                voiced_total = 0

    # ── Brain: STT → mode gate → chat() ──────────────────────────────────────

    def _brain_loop(self) -> None:
        import chat_backend
        from modules import speech

        lang = getattr(self._config, "language", "en")
        while not self._stop.is_set():
            try:
                pcm = self._utt_q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                text = speech.transcribe(_pcm_to_wav(pcm), "audio/wav")
            except Exception as e:
                logger.warning(f"voice STT failed: {e}")
                continue
            text = (text or "").strip()
            if not text or len(text) < 2:
                continue
            print(f"[Tu]   {text}")
            logger.info(f"voice heard: {text[:100]}")

            # Mode commands ("standby", "wake up", …) short-circuit the brain;
            # the ModeManager speaks its own acknowledgement via speak_text.
            if self._modes.detect_and_apply_mode_change(text):
                continue
            if not self._modes.should_process_transcription(text):
                logger.debug("gated by mode (no wake word)")
                continue

            try:
                out = chat_backend.chat(
                    router=self._router, session_id="voice", message=text,
                    provider=self._provider, model=self._model,
                    allow_actions=self._allow_actions,
                    owner_name=self._config.owner_name, language=lang,
                    system_extra=_VOICE_FRAME,
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
            reply = _strip_for_speech(out.get("reply") or "")
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
            self._speaking.set()
            try:
                arr = np.frombuffer(pcm, dtype=np.int16)
                sd.play(arr, samplerate=24000, blocking=True)
            except Exception as e:
                logger.warning(f"playback failed: {e}")
            finally:
                time.sleep(0.25)        # echo tail before re-opening the mic
                self._speaking.clear()

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
