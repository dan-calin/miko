"""
config.py — Miko Configuration
Loads .env once at import time, exposes a typed, immutable config singleton.
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


@dataclass(frozen=True)
class MikoConfig:
    # ── API keys ──────────────────────────────────────────────────────────────
    gemini_api_key: str
    discord_token: str
    discord_guild_id: int
    trusted_voice_users: tuple  # tuple[str] — frozenset semantics

    # ── MiniMax (optional — phone commander uses this when set) ───────────────
    minimax_api_key: str
    minimax_base_url: str
    minimax_model: str

    # ── Model ─────────────────────────────────────────────────────────────────
    live_model: str
    voice_name: str
    owner_name: str
    language: str  # "en" or "ro" — default user-facing language

    # ── Audio ─────────────────────────────────────────────────────────────────
    send_sample_rate: int
    receive_sample_rate: int
    chunk_size: int

    # ── Paths ─────────────────────────────────────────────────────────────────
    base_dir: Path
    data_dir: Path
    logs_dir: Path
    memory_file: Path
    notes_dir: Path
    db_path: Path
    contacts_file: Path
    queue_state_file: Path

    # ── Safety — hardcoded, NOT overridable via .env ──────────────────────────
    SAFETY_BLOCKED_PATHS: tuple = field(default_factory=lambda: (
        "c:\\windows",
        "c:\\windows\\system32",
        "c:\\windows\\syswow64",
    ))
    SAFETY_BLOCKED_REGISTRY: tuple = field(default_factory=lambda: (
        "hkey_",
        "hklm",
        "hkcu",
        "hkcr",
        "hku",
        "hkcc",
    ))


def load_config() -> MikoConfig:
    base = _base_dir()
    data = base / "data"
    logs = base / "logs"

    guild_raw = os.getenv("DISCORD_GUILD_ID", "0")
    try:
        guild_id = int(guild_raw)
    except ValueError:
        guild_id = 0

    trusted_raw = os.getenv("TRUSTED_VOICE_USERS", "")
    trusted = tuple(x.strip().lower() for x in trusted_raw.split(",") if x.strip())

    notes_dir_env = os.getenv("MIKO_NOTES_DIR", "")
    if notes_dir_env:
        notes_dir = Path(notes_dir_env)
    else:
        notes_dir = Path.home() / "Desktop" / "Miko Notes"

    return MikoConfig(
        gemini_api_key=os.getenv("LLM_API_KEY", ""),
        discord_token=os.getenv("DISCORD_TOKEN", ""),
        discord_guild_id=guild_id,
        trusted_voice_users=trusted,
        minimax_api_key=os.getenv("MINIMAX_API_KEY", ""),
        # MiniMax speaks the Anthropic wire protocol — the anthropic SDK appends
        # /v1/messages to this base, so it must be the bare /anthropic endpoint.
        minimax_base_url=os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/anthropic"),
        minimax_model=os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
        live_model=os.getenv(
            "LIVE_MODEL",
            "models/gemini-3.1-flash-live-preview"
        ),
        voice_name=os.getenv("MIKO_VOICE", "Aoede"),
        owner_name=os.getenv("OWNER_NAME", "Roxan"),
        language=(os.getenv("MIKO_LANGUAGE", "en").strip().lower() or "en"),
        send_sample_rate=16000,
        receive_sample_rate=24000,
        chunk_size=1600,
        base_dir=base,
        data_dir=data,
        logs_dir=logs,
        memory_file=base / "memory" / "long_term.json",
        notes_dir=notes_dir,
        db_path=data / "miko_files.db",
        contacts_file=data / "discord_contacts.json",
        queue_state_file=data / "queue_state.json",
    )


# Module-level singleton — import this everywhere
CONFIG: MikoConfig = load_config()
