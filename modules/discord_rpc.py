"""
modules/discord_rpc.py — Control the user's PERSONAL Discord account via local RPC.

Unlike discord_bot.py (which acts as a separate bot account), this drives YOUR
own Discord desktop client over its local IPC socket. Lets Miko join voice
channels as you, move you between channels, and mute/deafen yourself.

Requirements:
  - Discord DESKTOP client running + logged into your personal account on this PC
  - A Discord application (Developer Portal) — you are the owner, so the
    restricted `rpc` scopes work WITHOUT Discord's whitelist approval
  - .env: DISCORD_RPC_CLIENT_ID, DISCORD_RPC_CLIENT_SECRET, DISCORD_RPC_REDIRECT

First use triggers a one-time "<app> wants to connect" popup in your Discord
client. After you approve once, the token is cached and it runs headless.
"""

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("miko.discord_rpc")

CLIENT_ID     = os.getenv("DISCORD_RPC_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_RPC_CLIENT_SECRET", "")
REDIRECT_URI  = os.getenv("DISCORD_RPC_REDIRECT", "http://localhost")
RPC_SCOPES    = ["rpc", "rpc.voice.read", "rpc.voice.write"]

TOOL_DECLARATIONS = [
    {
        "name": "join_voice_as_me",
        "description": (
            "Connect the user's OWN personal Discord account to a voice channel by name "
            "(controls the desktop client via local RPC — not the bot). "
            "Use this when the user says 'join X channel for me' or 'connect me to voice'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel_name": {
                    "type": "string",
                    "description": "Name of the voice channel to join (partial match allowed).",
                },
            },
            "required": ["channel_name"],
        },
    },
    {
        "name": "leave_voice_as_me",
        "description": "Disconnect the user's OWN personal Discord account from voice (via local RPC).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "set_my_voice",
        "description": (
            "Mute or deafen the user's OWN personal Discord account (via local RPC). "
            "Use when the user says 'mute myself' or 'deafen me'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mute":  {"type": "boolean", "description": "True to mute your mic, False to unmute."},
                "deafen": {"type": "boolean", "description": "True to deafen, False to undeafen."},
            },
        },
    },
    {
        "name": "discord_rpc_login",
        "description": (
            "Trigger the one-time Discord RPC authorization popup to link Miko to your "
            "personal account. Run this once; approve the popup in your Discord client."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
]


# ── Token storage ─────────────────────────────────────────────────────────────

def _token_path() -> Path:
    from config import CONFIG
    return CONFIG.data_dir / "discord_rpc_token.json"


def _load_token() -> dict:
    p = _token_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_token(tok: dict) -> None:
    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tok))


def _exchange_code(code: str) -> dict:
    import requests
    r = requests.post(
        "https://discord.com/api/oauth2/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    data["_expires_at"] = time.time() + data.get("expires_in", 0) - 60
    return data


def _refresh_token(refresh: str) -> dict:
    import requests
    r = requests.post(
        "https://discord.com/api/oauth2/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    data["_expires_at"] = time.time() + data.get("expires_in", 0) - 60
    return data


# ── RPC client + auth ─────────────────────────────────────────────────────────

def _check_config() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        return (
            "Discord RPC nu este configurat. Adaugă DISCORD_RPC_CLIENT_ID și "
            "DISCORD_RPC_CLIENT_SECRET în .env (din Discord Developer Portal)."
        )
    return ""


def _get_authed_client(do_login: bool = False):
    """
    Returns a started + authenticated pypresence Client, or raises RuntimeError
    with a user-facing message.
    """
    from pypresence import Client

    client = Client(CLIENT_ID)
    try:
        client.start()
    except Exception as e:
        raise RuntimeError(
            f"Nu m-am putut conecta la clientul Discord. Asigură-te că aplicația "
            f"Discord desktop rulează și ești logat. ({e})"
        )

    tok = _load_token()

    # Refresh if we have a refresh_token but it's expired
    if tok.get("refresh_token") and tok.get("_expires_at", 0) <= time.time():
        try:
            tok = _refresh_token(tok["refresh_token"])
            _save_token(tok)
        except Exception as e:
            logger.warning(f"RPC token refresh failed: {e}")
            tok = {}

    # No valid token → need the one-time authorize popup
    if not tok.get("access_token"):
        if not do_login:
            client.close()
            raise RuntimeError(
                "Contul tău Discord nu este încă autorizat. Rulează comanda de login "
                "Discord RPC o singură dată și aprobă popup-ul din clientul Discord."
            )
        # Trigger the AUTHORIZE popup
        try:
            auth = client.authorize(CLIENT_ID, RPC_SCOPES)
            code = auth["data"]["code"]
            tok = _exchange_code(code)
            _save_token(tok)
        except Exception as e:
            client.close()
            raise RuntimeError(f"Autorizarea a eșuat: {e}")

    # Authenticate the RPC session with the access token
    try:
        client.authenticate(tok["access_token"])
    except Exception as e:
        client.close()
        raise RuntimeError(f"Autentificarea RPC a eșuat: {e}")

    return client


# ── Public tools ──────────────────────────────────────────────────────────────

def discord_rpc_login() -> str:
    err = _check_config()
    if err:
        return err
    try:
        client = _get_authed_client(do_login=True)
        client.close()
        return "Contul tău Discord a fost autorizat cu succes! Acum pot să te conectez la voice."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error(f"discord_rpc_login error: {e}", exc_info=True)
        return f"Eroare la autorizare: {e}"


def join_voice_as_me(channel_name: str) -> str:
    err = _check_config()
    if err:
        return err

    try:
        from modules.discord_bot import resolve_voice_channel, list_voice_channels
    except Exception:
        return "Botul Discord nu este disponibil ca să găsesc canalul."

    resolved = resolve_voice_channel(channel_name)
    if not resolved:
        available = ", ".join(list_voice_channels()[:15]) or "niciun canal"
        return f"Nu am găsit canalul de voice '{channel_name}'. Canale disponibile: {available}."

    _guild_id, channel_id, real_name = resolved

    try:
        client = _get_authed_client()
        try:
            client.select_voice_channel(str(channel_id), force=True)
        except TypeError:
            # Older pypresence has no `force` kwarg
            client.select_voice_channel(str(channel_id))
        client.close()
        return f"Te-am conectat pe canalul de voice '{real_name}', sefu."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error(f"join_voice_as_me error: {e}", exc_info=True)
        return f"Eroare la conectarea pe voice: {e}"


def leave_voice_as_me() -> str:
    err = _check_config()
    if err:
        return err
    client = None
    try:
        client = _get_authed_client()
        # force=True is required to leave when already in a channel — without it Discord
        # RPC rejects select_voice_channel(None) with 5003 "User is already joined".
        try:
            client.select_voice_channel(None, force=True)
        except TypeError:
            client.select_voice_channel(None)   # older pypresence: no `force` kwarg
        return "Te-am deconectat de pe voice, sefu."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error(f"leave_voice_as_me error: {e}", exc_info=True)
        return f"N-am putut să te deconectez de pe voice: {e}"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def set_my_voice(mute: bool = None, deafen: bool = None) -> str:
    err = _check_config()
    if err:
        return err
    if mute is None and deafen is None:
        return "Spune-mi ce vrei: să te mut-ez sau să te deafen-ez."

    kwargs = {}
    if mute is not None:
        kwargs["mute"] = bool(mute)
    if deafen is not None:
        kwargs["deaf"] = bool(deafen)

    try:
        client = _get_authed_client()
        client.set_voice_settings(**kwargs)
        client.close()
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error(f"set_my_voice error: {e}", exc_info=True)
        return f"Eroare la setările de voice: {e}"

    parts = []
    if mute is not None:
        parts.append("mut-at" if mute else "demut-at")
    if deafen is not None:
        parts.append("deafen-at" if deafen else "undeafen-at")
    return f"Te-am {' și '.join(parts)}, sefu."
