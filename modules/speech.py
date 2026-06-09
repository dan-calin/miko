"""
modules/speech.py — speech-to-text for Miko (multilingual dictation).

Used by the chat UI's dictation mic and reusable elsewhere. Transcription goes
through Gemini's multimodal model (the same proven path as Discord voice notes):
it auto-detects the spoken language and handles English + many others well, with
no local model download. Browser audio (webm/opus, mp4) is transcoded to WAV via
ffmpeg first — Gemini accepts ogg/wav/mp3/flac/aac/aiff directly, so those skip it.
"""

import logging
import subprocess

logger = logging.getLogger("miko.speech")

# Audio MIME types Gemini accepts directly; anything else we transcode to WAV.
_GEMINI_OK = {
    "audio/wav", "audio/x-wav", "audio/mp3", "audio/mpeg", "audio/aiff",
    "audio/aac", "audio/ogg", "audio/flac",
}


def _ffmpeg_exe() -> str:
    """Resolve an ffmpeg executable. Prefers the bundled imageio-ffmpeg binary
    (same as the Discord audio path) so it works even when ffmpeg isn't on PATH;
    honours MIKO_FFMPEG/FFMPEG_PATH overrides; falls back to PATH."""
    import os
    override = os.getenv("MIKO_FFMPEG") or os.getenv("FFMPEG_PATH")
    if override:
        return override
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffmpeg_to_wav(audio_bytes: bytes) -> bytes:
    """Transcode arbitrary audio to 16 kHz mono WAV via ffmpeg."""
    proc = subprocess.run(
        [_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
        input=audio_bytes, capture_output=True, timeout=60,
    )
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"").decode("utf-8", "replace")[:300]
        raise RuntimeError(err or "ffmpeg produced no output")
    return proc.stdout


def transcribe(audio_bytes: bytes, mime_type: str = "", api_key: str = "",
               model: str = "", language: str = "") -> str:
    """Transcribe audio to text. Auto-detects language unless `language` is given.
    Returns '' on any failure (caller surfaces a friendly message)."""
    if not audio_bytes:
        return ""
    mime = (mime_type or "").split(";")[0].strip().lower() or "audio/webm"

    data, send_mime = audio_bytes, mime
    if mime not in _GEMINI_OK:
        try:
            data = _ffmpeg_to_wav(audio_bytes)
            send_mime = "audio/wav"
        except FileNotFoundError:
            logger.error("ffmpeg not found on PATH — cannot transcode dictation audio")
            return ""
        except Exception as e:
            logger.error(f"audio transcode failed: {e}")
            return ""

    import os
    from config import CONFIG
    key = (api_key or "").strip() or getattr(CONFIG, "gemini_api_key", "")
    if not key:
        logger.warning("transcribe: no Gemini key configured")
        return ""
    # Default to flash; override to gemini-2.5-flash-lite for faster dictation.
    # (`or`-chain so a blank MIKO_DICTATION_MODEL= line in .env doesn't yield "".)
    model = model or os.getenv("MIKO_DICTATION_MODEL", "").strip() or "gemini-2.5-flash"

    instr = ("Transcribe this audio exactly as spoken. Output only the transcription "
             "text, with no commentary, labels, or quotation marks.")
    if language:
        instr += f" The spoken language is {language}."

    from google import genai
    from google.genai import types
    try:
        client = genai.Client(api_key=key)
    except Exception as e:
        logger.error(f"gemini client init failed: {e}")
        return ""

    # Inline the audio in the request (no Files API upload/delete round-trips) —
    # dictation clips are tiny and well under the ~20MB inline limit, so this is
    # noticeably faster than uploading each clip.
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[types.Part.from_bytes(data=data, mime_type=send_mime), instr],
        )
        return (resp.text or "").strip()
    except Exception as e:
        logger.error(f"transcription failed: {e}")
        return ""
