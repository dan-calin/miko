"""
modules/speech.py — speech-to-text for Miko (multilingual dictation).

Used by the chat UI's dictation mic and the voice engine (core/voice_chat.py).

Two engines, selected by env:
  • Cloud (default): Gemini's multimodal model — auto-detects language, no local
    model download. Browser audio (webm/opus, mp4) is transcoded to WAV via ffmpeg
    first; Gemini accepts ogg/wav/mp3/flac/aac/aiff directly, so those skip it.
  • Local: a parakeet-rs / NVIDIA Nemotron ASR HTTP sidecar on this machine
    (MIKO_STT_URL) — keeps audio on-device and is low-latency once warm. Falls back
    to cloud on failure unless MIKO_STT_STRICT is set. See _local_stt for the contract.

Env knobs:
  MIKO_STT_URL       local ASR sidecar endpoint (enables the local engine)
  MIKO_STT_PROVIDER  'local'/'parakeet'/'nemotron' also force the local engine
  MIKO_STT_LANG      target language for the local model ('auto' or e.g. 'ro-RO')
  MIKO_STT_STRICT    truthy → local-only (never fall back to the cloud)
  MIKO_STT_TIMEOUT   seconds to wait on the sidecar (default 30)
"""

import logging
import os
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


def _local_stt(audio_bytes: bytes, mime_type: str, language: str) -> str:
    """Transcribe via a LOCAL ASR HTTP sidecar (e.g. a parakeet-rs / NVIDIA Nemotron
    server running on this machine) instead of the cloud. Keeps audio on-device and
    cuts latency once the model is warm.

    Contract — the sidecar accepts `POST <MIKO_STT_URL>` with a 16 kHz mono WAV body
    (Content-Type: audio/wav) and an optional `X-Language` header ('auto' or a locale
    like 'en-US'/'ro-RO'), and replies with either the raw transcript text or JSON
    `{"text": "..."}`. Returns '' on any failure so the caller can fall back to cloud.
    """
    url = os.getenv("MIKO_STT_URL", "").strip()
    if not url:
        return ""
    data, mime = audio_bytes, (mime_type or "").split(";")[0].strip().lower()
    if mime not in ("audio/wav", "audio/x-wav"):
        try:
            data = _ffmpeg_to_wav(audio_bytes)
        except Exception as e:
            logger.error(f"local STT transcode failed: {e}")
            return ""
    lang = (language or os.getenv("MIKO_STT_LANG", "").strip() or "auto")
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "audio/wav", "X-Language": lang})
        timeout = float(os.getenv("MIKO_STT_TIMEOUT", "30") or "30")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace").strip()
        if body.startswith("{"):
            return (json.loads(body).get("text") or "").strip()
        return body
    except Exception as e:
        logger.warning(f"local STT ({url}) failed: {e}")
        return ""


def transcribe(audio_bytes: bytes, mime_type: str = "", api_key: str = "",
               model: str = "", language: str = "") -> str:
    """Transcribe audio to text. Auto-detects language unless `language` is given.
    Returns '' on any failure (caller surfaces a friendly message).

    Engine: a local ASR sidecar when MIKO_STT_URL (or MIKO_STT_PROVIDER=local) is set,
    otherwise cloud Gemini. The local path falls back to Gemini on failure unless
    MIKO_STT_STRICT is truthy (then local-only)."""
    if not audio_bytes:
        return ""

    provider = os.getenv("MIKO_STT_PROVIDER", "").strip().lower()
    if os.getenv("MIKO_STT_URL", "").strip() or provider in ("local", "parakeet", "nemotron"):
        text = _local_stt(audio_bytes, mime_type, language)
        if text:
            return text
        if os.getenv("MIKO_STT_STRICT", "").strip().lower() in ("1", "true", "yes"):
            return ""   # local-only: don't leak audio to the cloud on failure
        # else fall through to the cloud path below

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
