"""
modules/media_control.py — Windows system media and volume control.
Uses pycaw for precise volume setting and pyautogui for media key simulation.
"""

import logging
import math

logger = logging.getLogger("miko.media")

TOOL_DECLARATIONS = [
    {
        "name": "set_volume",
        "description": (
            "Setează volumul sistemului la o valoare procentuală. "
            "Folosește pentru 'dă mai tare', 'pune pe 50%', 'mărește/micșorează volumul'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "level": {
                    "type": "INTEGER",
                    "description": "Volumul dorit între 0 și 100. 0=mut, 100=maxim.",
                }
            },
            "required": ["level"],
        },
    },
    {
        "name": "media_control",
        "description": (
            "Controlează redarea media sistemului. "
            "Acțiuni: play, pause, next, previous, stop, mute, unmute, volume_up, volume_down."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "play | pause | next | previous | stop | mute | unmute | volume_up | volume_down",
                }
            },
            "required": ["action"],
        },
    },
    {
        "name": "get_volume",
        "description": "Returnează volumul curent al sistemului ca procent (0-100).",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
]

# ── Volume via pycaw / comtypes ───────────────────────────────────────────────
#
# AudioUtilities.GetSpeakers() returns different types across pycaw versions:
#   - Older pycaw (≤20220427): raw COM IMMDevice → has .Activate()
#   - Newer pycaw (>20220427): Python AudioDevice wrapper → no .Activate()
#
# We bypass GetSpeakers() entirely and use IMMDeviceEnumerator directly,
# which is stable across all Windows versions and all pycaw versions.

def _get_endpoint_volume():
    """
    Returns IAudioEndpointVolume for the default audio output device.
    Uses IMMDeviceEnumerator → GetDefaultAudioEndpoint → Activate,
    bypassing AudioUtilities.GetSpeakers() to avoid version-specific wrappers.
    """
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL, GUID, CoCreateInstance
    from pycaw.pycaw import IAudioEndpointVolume, IMMDeviceEnumerator

    CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

    # Get enumerator → default render endpoint → activate volume interface
    enumerator = CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
    device     = enumerator.GetDefaultAudioEndpoint(0, 1)   # eRender=0, eConsole=1
    interface  = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def set_volume(level: int) -> str:
    """Set system volume 0-100%. Uses scalar (0.0-1.0) for maximum compatibility."""
    level = max(0, min(100, int(level)))
    try:
        vol = _get_endpoint_volume()
        vol.SetMasterVolumeLevelScalar(level / 100.0, None)
        logger.info(f"Volume → {level}%")
        return f"Am setat volumul la {level}%, sefu."
    except ImportError:
        return _volume_by_keys(level)
    except Exception as e:
        logger.error(f"Volume set error: {e}")
        # Graceful degradation — try key presses
        try:
            return _volume_by_keys(level)
        except Exception:
            return f"N-am putut seta volumul ({e}). Încearcă manual, sefu."


def get_volume() -> int:
    """Returns current master volume as 0-100 integer."""
    try:
        vol    = _get_endpoint_volume()
        scalar = vol.GetMasterVolumeLevelScalar()
        return round(scalar * 100)
    except Exception as e:
        logger.warning(f"get_volume error: {e}")
        return -1


def _volume_by_keys(target: int) -> str:
    """Fallback volume control via repeated key presses (±2% per press)."""
    try:
        import pyautogui
        current = get_volume()
        if current < 0:
            current = 50  # Assume 50% if unknown
        delta = target - current
        key   = "volumeup" if delta > 0 else "volumedown"
        steps = min(abs(delta) // 2, 50)  # Cap at 50 presses
        for _ in range(steps):
            pyautogui.press(key)
        return f"Am ajustat volumul spre {target}%, sefu."
    except Exception as e:
        return f"N-am putut ajusta volumul: {e}"


# ── Media key simulation via pyautogui ───────────────────────────────────────

_MEDIA_KEY_MAP = {
    "play":        "playpause",
    "pause":       "playpause",
    "playpause":   "playpause",
    "next":        "nexttrack",
    "skip":        "nexttrack",
    "previous":    "prevtrack",
    "prev":        "prevtrack",
    "back":        "prevtrack",
    "stop":        "stop",
    "mute":        "volumemute",
    "unmute":      "volumemute",
    "volume_up":   "volumeup",
    "volume_down": "volumedown",
}

_ACTION_RESPONSES = {
    "play":        "Am dat play, sefu.",
    "pause":       "Am pus pe pauză, sefu.",
    "playpause":   "Am apăsat play/pauză, sefu.",
    "next":        "Am trecut la melodia următoare, sefu.",
    "skip":        "Am sărit melodia, sefu.",
    "previous":    "Am revenit la melodia anterioară, sefu.",
    "prev":        "Am revenit la melodia anterioară, sefu.",
    "back":        "Am revenit la melodia anterioară, sefu.",
    "stop":        "Am oprit redarea, sefu.",
    "mute":        "Am dat mute, sefu.",
    "unmute":      "Am scos mute-ul, sefu.",
    "volume_up":   "Am mărit volumul, sefu.",
    "volume_down": "Am micșorat volumul, sefu.",
}


def media_control(action: str) -> str:
    action = action.lower().strip()
    key    = _MEDIA_KEY_MAP.get(action)
    if not key:
        return f"Nu știu acțiunea media '{action}'. Încearcă: play, pause, next, previous, mute."
    try:
        import pyautogui
        if action in ("volume_up", "volume_down"):
            for _ in range(3):
                pyautogui.press(key)
        else:
            pyautogui.press(key)
        return _ACTION_RESPONSES.get(action, f"Am executat '{action}', sefu.")
    except Exception as e:
        logger.error(f"media_control error: {e}")
        return f"N-am putut executa '{action}': {e}"
