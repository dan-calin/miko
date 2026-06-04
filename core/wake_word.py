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
# Hardest quiet mode — "leave me fully alone" (distinct from plain standby).
_MODE_MUTE = re.compile(
    r"\b(mute\s+yourself|mute\s+mode|go\s+(completely\s+)?silent|"
    r"complete\s+silence|total\s+silence|leave\s+me\s+alone|"
    r"taci\s+de\s+tot|t[aă]cere\s+complet[aă]|las[aă][\s\-]?m[aă]\s+[îi]n\s+pace|"
    r"mut\s+complet)\b",
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


def detect_mute(text: str) -> bool:
    return bool(_MODE_MUTE.search(_normalize(text)))


# Natural affirmations / negations (diacritics already stripped before matching).
# Matched on word boundaries so "trimite-l", "fa-o", "okay", "yeah" all count.
_AFFIRM = re.compile(
    r"\b("
    r"da+|asa|exact|corect|"                       # da, daa, așa, exact, corect
    r"sigur|sigur\s*ca\s*da|bineinteles|evident|normal|firesc|"
    r"confirm|confirma|aprob\w*|"
    r"ok|okay|okey|oki|bine|bun|perfect|super|misto|"
    r"trimite\w*|trimitel|trimiteo|"               # trimite / trimite-l / trimite-o
    r"fa[\s\-]*o|fai|fa|"                           # fă, fă-o, fai
    r"hai|haide|haida|da[\s\-]*i\s*drumul|mergi|merge|continua|"
    r"yes|yeah|yep|yup|sure|go|go\s*ahead|do\s*it|send\s*it|send|proceed"
    r")\b",
    re.IGNORECASE,
)
_NEGATE = re.compile(
    r"\b("
    r"nu+|no|nope|nah|"
    r"stop|stai|asteapta|"                          # stop, stai, așteaptă
    r"cancel|anuleaz\w*|anulat|renunt\w*|"
    r"las\w*|opreste\w*|abort|"                      # lasă, las-o, oprește
    r"ba\s*nu|ba"
    r")\b",
    re.IGNORECASE,
)


def is_confirmation(text: str) -> bool | None:
    """
    Interprets a free-form reply to a yes/no confirmation prompt.
    Returns True for an affirmation, False for a negation, None if ambiguous
    (e.g. both present, or neither) so the caller keeps waiting.

    Accepts natural phrasing in Romanian and English — "da", "trimite-l",
    "hai, fă-o", "sure, go ahead", "yeah", as well as "nu", "lasă", "stop", etc.
    """
    norm = _normalize(text).lower().strip(" .,!?")
    if not norm:
        return None

    aff = _AFFIRM.search(norm)
    neg = _NEGATE.search(norm)

    if aff and not neg:
        return True
    if neg and not aff:
        return False
    if aff and neg:
        # Both present ("nu trimite", "da de ce nu") — the one that comes
        # FIRST wins, since that's the leading intent of the reply.
        return aff.start() < neg.start()
    return None
