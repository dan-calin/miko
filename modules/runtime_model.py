"""In-RAM record of the model the user is driving from the chat UI.

The Discord/phone path (``phone_commander``) normally runs on the MiniMax creds
from ``.env``. When that backend fails — e.g. the MiniMax plan runs out of
credits and returns HTTP 429 — a DM from a trusted contact would otherwise get a
raw error instead of an answer. So we fall back to whatever model the user is
actively using in the chat UI.

The chat UI sends its ``provider/model/api_key/base_url`` with every request; we
keep the latest here so the bot can borrow it. **The API key is held in memory
only — never written to disk** — matching Miko's "don't persist secrets" rule, so
it survives only for the life of the process.
"""

import threading

_lock = threading.Lock()
_model = {"provider": "", "model": "", "api_key": "", "base_url": ""}


def remember(provider: str = "", model: str = "",
             api_key: str = "", base_url: str = "") -> None:
    """Record the UI model. No-ops on a credential-less call so an unrelated
    request can never wipe a good fallback."""
    provider = (provider or "").strip()
    api_key = (api_key or "").strip()
    if not provider and not api_key:
        return
    with _lock:
        if provider:
            _model["provider"] = provider
        if model:
            _model["model"] = model.strip()
        if api_key:
            _model["api_key"] = api_key
        _model["base_url"] = (base_url or "").strip()


def get() -> dict:
    """A copy of the last UI model creds."""
    with _lock:
        return dict(_model)


def available() -> bool:
    """True when we have enough to actually call the UI model as a fallback."""
    with _lock:
        # Gemini may authenticate from the env LLM_API_KEY, so it's usable keyless.
        return bool(_model["provider"]) and bool(
            _model["api_key"] or _model["provider"] == "gemini")
