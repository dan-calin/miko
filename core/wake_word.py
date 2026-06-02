"""
core/wake_word.py — Wake word detection on transcription text.
Since Gemini Live already transcribes speech, we operate on the text layer,
not the raw audio stream — zero extra compute overhead.
"""

import re
import unicodedata

# All accepted forms of the wake word (STT often mishears accents/names)
# NOTE: "micro" removed — too many false positives (microphone, Microsoft, etc.)
# "mika" added — common STT mishear of "miko"
_WAKE_PATTERNS = re.compile(
    r"\b(miko|mico|myko|mika|"
    r"alo\s+(miko|mika)|hey\s+(miko|mika)|"
    r"hei\s+(miko|mika)|salut\s+miko|buna\s+miko)\b",
    re.IGNORECASE,
)

# Romanian mode-change phrases
_MODE_STANDBY = re.compile(
    r"\b(taci|standby|stand[\s\-]?by|pauza|pauza|intr[aă]\s+[îi]n\s+stand[\s\-]?by|"
    r"shut\s+up|be\s+quiet)\b",
    re.IGNORECASE,
)
_MODE_ACTIVE = re.compile(
    r"\b(trezeste[\s\-]?te|treze[șs]te[\s\-]?te|ie[șs]i\s+din\s+stand[\s\-]?by|"
    r"activeaz[aă]|intr[aă]\s+[îi]n\s+mod\s+activ|wake\s+up)\b",
    re.IGNORECASE,
)
_MODE_AUTO = re.compile(
    r"\b(intr[aă]\s+[îi]n\s+mod\s+(auto|conversa[tț]ie)|"
    r"activeaz[aă]\s+modul\s+conversa[tț]ie|ie[șs]i\s+din\s+stand[\s\-]?by)\b",
    re.IGNORECASE,
)
_MODE_EXIT_AUTO = re.compile(
    r"\b(opre[șs]te\s+modul\s+conversa[tț]ie|ie[șs]i\s+din\s+mod\s+auto|"
    r"exit\s+auto)\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Strip diacritics for accent-insensitive matching."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def contains_wake_word(text: str) -> bool:
    """Returns True if the transcription contains a wake-word variant."""
    return bool(_WAKE_PATTERNS.search(_normalize(text)))


def strip_wake_word(text: str) -> str:
    """Remove the wake-word prefix so the remainder is routed as a clean command."""
    return _WAKE_PATTERNS.sub("", _normalize(text)).strip(" ,!?.")


def detect_standby(text: str) -> bool:
    return bool(_MODE_STANDBY.search(_normalize(text)))


def detect_active(text: str) -> bool:
    return bool(_MODE_ACTIVE.search(_normalize(text)))


def detect_auto(text: str) -> bool:
    return bool(_MODE_AUTO.search(_normalize(text)))


def detect_exit_auto(text: str) -> bool:
    return bool(_MODE_EXIT_AUTO.search(_normalize(text)))


def is_confirmation(text: str) -> bool | None:
    """
    Returns True for 'da'/'yes', False for 'nu'/'no', None for neither.
    Used to resolve ConfirmationPending.
    """
    norm = _normalize(text).lower().strip(" .,!?")
    if norm in ("da", "da sefu", "da boss", "yes", "confirm", "confirma", "sigur"):
        return True
    if norm in ("nu", "nu sefu", "nu boss", "no", "anuleaza", "anulat", "cancel"):
        return False
    # Partial match
    if re.search(r"\bda\b", norm) and not re.search(r"\bnu\b", norm):
        return True
    if re.search(r"\bnu\b", norm) and not re.search(r"\bda\b", norm):
        return False
    return None
