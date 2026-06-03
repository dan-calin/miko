"""
core/mode_manager.py — ACTIVE / STANDBY / AUTO mode state machine.

Mode filtering happens at the transcription layer (post-transcription,
pre-command-routing), which means Gemini Live keeps running regardless of mode.
The system prompt addendum reinforces what the code already enforces.
"""

import logging
import threading
import time
from enum import Enum
from typing import Callable, Optional

from core.wake_word import (
    contains_wake_word,
    detect_standby,
    detect_active,
    detect_auto,
    detect_exit_auto,
)

logger = logging.getLogger("miko.mode")

_STANDBY_WINDOW_SECS = 30  # seconds of follow-up commands allowed after a wake word


class Mode(Enum):
    ACTIVE  = "active"   # All transcriptions processed
    STANDBY = "standby"  # Only wake-word transcriptions processed
    AUTO    = "auto"     # Context-aware, no wake word needed


_MODE_TRANSITIONS = {
    # (current_mode, trigger_fn) -> new_mode
}

# Verbal acknowledgements for each transition (per language)
_TRANSITION_ACK_EN = {
    (Mode.ACTIVE,  Mode.STANDBY): "Going to standby. Call my name when you need me.",
    (Mode.STANDBY, Mode.ACTIVE):  "Out of standby. Listening to everything.",
    (Mode.STANDBY, Mode.AUTO):    "Entering conversation mode. Talk naturally.",
    (Mode.ACTIVE,  Mode.AUTO):    "Entering conversation mode. Talk naturally.",
    (Mode.AUTO,    Mode.ACTIVE):  "Left conversation mode. Listening to everything.",
    (Mode.AUTO,    Mode.STANDBY): "Going to standby. Call my name when you need me.",
}

_TRANSITION_ACK_RO = {
    (Mode.ACTIVE,  Mode.STANDBY): "Intru în stand-by. Strig-mă pe nume când ai nevoie.",
    (Mode.STANDBY, Mode.ACTIVE):  "Am ieșit din stand-by. Ascult tot.",
    (Mode.STANDBY, Mode.AUTO):    "Intru în modul conversație. Vorbește natural.",
    (Mode.ACTIVE,  Mode.AUTO):    "Intru în modul conversație. Vorbește natural.",
    (Mode.AUTO,    Mode.ACTIVE):  "Am ieșit din modul conversație. Ascult tot.",
    (Mode.AUTO,    Mode.STANDBY): "Intru în stand-by. Strig-mă pe nume când ai nevoie.",
}


class ModeManager:
    def __init__(self, speak_callback: Optional[Callable[[str], None]] = None,
                 language: str = "en"):
        self._mode = Mode.ACTIVE
        self._lock = threading.Lock()
        self._speak = speak_callback  # Set after AudioHandler is created
        self._language = language
        self._ack = _TRANSITION_ACK_EN if language == "en" else _TRANSITION_ACK_RO
        self._standby_window_until: float = 0.0  # epoch; >now means follow-up commands allowed

    def set_speak_callback(self, cb: Callable[[str], None]) -> None:
        self._speak = cb

    @property
    def mode(self) -> Mode:
        with self._lock:
            return self._mode

    def set_mode(self, new_mode: Mode) -> None:
        with self._lock:
            old = self._mode
            if old == new_mode:
                return
            self._mode = new_mode
            if new_mode == Mode.STANDBY:
                self._standby_window_until = 0.0  # close any open window on STANDBY entry

        ack = self._ack.get((old, new_mode))
        logger.info(f"Mode: {old.value} → {new_mode.value}")
        if ack and self._speak:
            self._speak(ack)

    def should_process_transcription(self, text: str) -> bool:
        """
        Core gate: decides whether a transcription should reach the command router.
        ACTIVE  → always True
        STANDBY → True if wake word present, OR if inside the 30s follow-up window
        AUTO    → True (Gemini's context handles relevance in auto mode)
        """
        mode = self.mode
        if mode == Mode.ACTIVE:
            return True
        if mode == Mode.STANDBY:
            now = time.time()
            # Within the conversation window — allow follow-ups without repeating "Miko"
            if now < self._standby_window_until:
                with self._lock:
                    self._standby_window_until = now + _STANDBY_WINDOW_SECS  # slide window
                logger.debug("STANDBY: follow-up allowed (window active)")
                return True
            # Wake word detected — open a fresh window
            if contains_wake_word(text):
                with self._lock:
                    self._standby_window_until = now + _STANDBY_WINDOW_SECS
                logger.debug("STANDBY: wake word detected, window opened")
                return True
            return False
        if mode == Mode.AUTO:
            return True
        return False

    def detect_and_apply_mode_change(self, text: str) -> bool:
        """
        Scans transcription for mode-change commands.
        Returns True if a mode change was applied (caller should skip normal routing).
        Must be called BEFORE should_process_transcription.
        """
        mode = self.mode

        if detect_standby(text):
            if mode != Mode.STANDBY:
                self.set_mode(Mode.STANDBY)
            return True

        if detect_exit_auto(text) and mode == Mode.AUTO:
            self.set_mode(Mode.ACTIVE)
            return True

        if detect_auto(text):
            if mode != Mode.AUTO:
                self.set_mode(Mode.AUTO)
            return True

        if detect_active(text):
            if mode != Mode.ACTIVE:
                self.set_mode(Mode.ACTIVE)
            return True

        return False

    def get_mode_prompt_addendum(self) -> str:
        """Dynamic text appended to the system prompt on each reconnect."""
        mode = self.mode
        is_en = self._language == "en"
        if mode == Mode.STANDBY:
            if is_en:
                return (
                    "\n\n[CURRENT MODE: STANDBY]\n"
                    "You are in STANDBY. Do NOT respond to ANYTHING you hear in the background. "
                    "Respond ONLY when you directly hear 'Miko' or 'Hey Miko'."
                )
            return (
                "\n\n[MODUL CURENT: STANDBY]\n"
                "Ești în STANDBY. NU răspunzi la NIMIC din ce auzi ambiental. "
                "Răspunzi EXCLUSIV când auzi direct 'Miko' sau 'Hey Miko'."
            )
        if mode == Mode.AUTO:
            if is_en:
                return (
                    "\n\n[CURRENT MODE: AUTO / CONVERSATION]\n"
                    "You are in conversation mode. Respond to direct commands and questions. "
                    "Ignore background conversations not addressed to you."
                )
            return (
                "\n\n[MODUL CURENT: AUTO / CONVERSAȚIE]\n"
                "Ești în modul conversație. Răspunzi la comenzi directe și întrebări. "
                "Ignoră conversațiile ambientale care nu ți se adresează."
            )
        return ""  # ACTIVE — no addendum needed
