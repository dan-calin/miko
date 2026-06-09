"""
modules/discord_bot.py — Discord bot integration for Miko.
Handles DM monitoring, message sending, voice channel joining, and YouTube music streaming.

Runs the Discord bot on its own asyncio event loop in a daemon thread.
All public functions are synchronous and thread-safe.
Adapted from jarvis/discord_client.py with cleaner module separation.
"""

import asyncio
import collections
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

import discord
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("miko.discord")

TOKEN    = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or "0")
TRUSTED_VOICE_USERS = [
    x.strip().lower()
    for x in os.getenv("TRUSTED_VOICE_USERS", "").split(",")
    if x.strip()
]

TOOL_DECLARATIONS = [
    {
        "name": "play_media_discord",
        "description": (
            "Caută o melodie sau playlist pe YouTube și o redă în voice channel-ul Discord "
            "în care se află utilizatorul. Adaugă la coadă dacă deja rulează ceva."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "song_name": {
                    "type": "STRING",
                    "description": "Numele melodiei, artistului, sau URL-ul YouTube.",
                },
                "owner_name": {
                    "type": "STRING",
                    "description": "Numele utilizatorului pe Discord (default: din config).",
                },
            },
            "required": ["song_name"],
        },
    },
    {
        "name": "get_music_queue",
        "description": "Arată melodiile din coada de așteptare Discord.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "stop_audio",
        "description": "Oprește muzica pe Discord și golește coada de așteptare.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "send_discord_dm",
        "description": (
            "ACESTA este tool-ul de folosit ca să TRIMIȚI conținut/text/rezultate cuiva pe "
            "Discord, ca DM. Inclusiv ție însuți (owner-ul) când user-ul spune 'trimite-mi "
            "X pe discord', 'send me the results on discord', 'dă-mi pe discord lista' — "
            "recipient_name = numele owner-ului. Pune rezultatul efectiv în 'message'. "
            "Acțiune sensibilă: confirmarea e gestionată automat de sistem — NU cere tu "
            "confirmare în text, doar apeleaz-o."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "recipient_name": {
                    "type": "STRING",
                    "description": "Numele de afișare al destinatarului pe Discord.",
                },
                "message": {"type": "STRING", "description": "Mesajul de trimis."},
            },
            "required": ["recipient_name", "message"],
        },
    },
    {
        "name": "send_discord_channel",
        "description": (
            "Trimite un mesaj într-un canal text Discord. Acțiune sensibilă: confirmarea e "
            "gestionată automat de sistem — NU cere tu confirmare în text, doar apeleaz-o."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "channel_name": {
                    "type": "STRING",
                    "description": "Numele canalului Discord (#general, #chat etc.).",
                },
                "message": {"type": "STRING", "description": "Mesajul de trimis."},
            },
            "required": ["channel_name", "message"],
        },
    },
    {
        "name": "call_discord",
        "description": "Invită un utilizator Discord în voice channel și se alătură pentru apel.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "person_name": {
                    "type": "STRING",
                    "description": "Numele utilizatorului de chemat.",
                },
                "owner_name": {
                    "type": "STRING",
                    "description": "Numele tău pe Discord (default: din config).",
                },
            },
            "required": ["person_name"],
        },
    },
    {
        "name": "join_voice",
        "description": "Se alătură voice channel-ului curent al utilizatorului.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "owner_name": {"type": "STRING", "description": "Numele tău pe Discord."}
            },
        },
    },
    {
        "name": "leave_voice",
        "description": "Deconectează botul din voice channel.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "reconnect_discord",
        "description": (
            "Reconectează botul la Discord de la zero (sesiune nouă) FĂRĂ a reporni aplicația. "
            "Folosește când Discord pare 'blocat' — de ex. spui că ești pe voice dar botul nu te vede, "
            "sau nu mai primește mesaje. Repară starea învechită fără restart."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "speak_on_discord",
        "description": "Dictează un text prin TTS în voice channel-ul Discord.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {"type": "STRING", "description": "Textul de dictat."},
                "owner_name": {"type": "STRING"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "get_dm_history",
        "description": (
            "Citește ultimele mesaje dintr-o conversație DM cu o persoană SPECIFICĂ (știi deja numele). "
            "Folosește DOAR dacă user-ul menționează explicit un nume: 'ce mi-a scris Andrei', 'ultimele mesaje de la SNX'. "
            "Dacă user-ul vrea să vadă toate mesajele fără să specifice o persoană, folosește send_interactive_hub."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "user_name": {
                    "type": "STRING",
                    "description": "Numele exact al utilizatorului Discord.",
                },
                "limit": {
                    "type": "INTEGER",
                    "description": "Numărul de mesaje de citit (default: 5).",
                },
            },
            "required": ["user_name"],
        },
    },
    {
        "name": "send_interactive_hub",
        "description": (
            "Deschide un meniu (dropdown) pentru a CITI mesajele Discord PRIMITE de la "
            "contacte — alegi de la cine vrei să vezi conversația. NU trimite niciun "
            "conținut și NU se folosește pentru a-i trimite user-ului rezultate/text (pentru "
            "asta folosește send_discord_dm). Folosește DOAR pentru citirea mesajelor primite: "
            "'ce mesaje am primit', 'cine mi-a scris', 'arată-mi mesajele primite', "
            "'show me my messages', 'who wrote to me'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "owner_name": {"type": "STRING", "description": "Numele tău pe Discord (din config dacă nu e specificat)."}
            },
        },
    },
]

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents                 = discord.Intents.default()
intents.members         = True
intents.message_content = True
intents.presences       = True
intents.voice_states    = True   # explicit: needed to see who's sitting in voice channels

_loop: Optional[asyncio.AbstractEventLoop] = None
_ready = threading.Event()

import datetime as _dt
_start_time: Optional[_dt.datetime] = None

_message_queue:     collections.deque = collections.deque()
_voice_notifications: collections.deque = collections.deque()
_voice_client: Optional[discord.VoiceClient]  = None
_music_queue:  collections.deque              = collections.deque()
_pending_call_target: Optional[str]           = None


# ── Contacts cache ─────────────────────────────────────────────────────────────

def _contacts_file() -> Path:
    from config import CONFIG
    return CONFIG.contacts_file

_cached_contacts: set = set()


def _load_contacts() -> None:
    global _cached_contacts
    cf = _contacts_file()
    if cf.exists():
        try:
            _cached_contacts = set(json.loads(cf.read_text(encoding="utf-8")))
        except Exception:
            _cached_contacts = set()


def _save_contact(name: str) -> None:
    global _cached_contacts
    if not _cached_contacts:
        _load_contacts()
    if name not in _cached_contacts:
        _cached_contacts.add(name)
        try:
            cf = _contacts_file()
            cf.parent.mkdir(parents=True, exist_ok=True)
            cf.write_text(json.dumps(list(_cached_contacts)), encoding="utf-8")
        except Exception:
            pass


_load_contacts()


# ── Bot events ────────────────────────────────────────────────────────────────

def _build_bot() -> discord.Client:
    """
    Create a fresh client with its event handlers registered. Re-callable so
    `reconnect_discord()` can stand up a brand-new gateway session (a full
    IDENTIFY → fresh GUILD_CREATE → current voice states) without restarting
    the whole app. Handlers close over `client`, not the module global, so they
    keep working on the new instance even before `bot` is reassigned.
    """
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        global _start_time
        _start_time = _dt.datetime.now(_dt.timezone.utc)
        logger.info(f"Discord bot online as '{client.user}' | Guild: {GUILD_ID}")
        _ready.set()

    @client.event
    async def on_message(message: discord.Message):
        if message.author == client.user:
            return
        if _start_time and message.created_at < _start_time:
            return

        if isinstance(message.channel, discord.DMChannel):
            sender = message.author.display_name
            audio_data = None
            content = message.content

            for attachment in message.attachments:
                is_voice = False
                if attachment.content_type and attachment.content_type.startswith("audio/"):
                    is_voice = True
                else:
                    try:
                        if attachment.flags.value & 8192:
                            is_voice = True
                    except Exception:
                        pass

                if is_voice:
                    try:
                        audio_bytes = await attachment.read()
                        mime = attachment.content_type or "audio/ogg"
                        audio_data = (audio_bytes, mime)
                        content = "[Voice message]"
                        logger.debug(f"Voice DM from {sender}: {mime}, {len(audio_bytes)} bytes")
                    except Exception as e:
                        logger.warning(f"Failed to read voice attachment from {sender}: {e}")
                    break

            _message_queue.append((sender, content, True, audio_data))
            _save_contact(sender)
            if audio_data is None:
                logger.debug(f"DM from {sender}: {content[:60]}")

        elif client.user in message.mentions:
            sender  = message.author.display_name
            _message_queue.append((sender, message.content, False, None))
            _save_contact(sender)
            logger.debug(f"Mention from {sender}")

    @client.event
    async def on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        global _pending_call_target
        if member == client.user:
            return
        if (
            after.channel is not None
            and (before.channel is None or before.channel != after.channel)
            and _pending_call_target
        ):
            tgt = _pending_call_target.lower()
            if member.display_name.lower() == tgt or member.name.lower() == tgt:
                _voice_notifications.append(f"{member.display_name} a intrat pe voice, sefu!")
                logger.info(f"{member.display_name} joined voice — call answered")
                _pending_call_target = None

    return client


bot = _build_bot()


# ── Bot startup ───────────────────────────────────────────────────────────────

def start() -> None:
    if not TOKEN:
        logger.warning("DISCORD_TOKEN not set — Discord integration disabled.")
        return

    def _run():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        try:
            _loop.run_until_complete(bot.start(TOKEN))
        except Exception as e:
            logger.error(f"Discord bot error: {e}")

    threading.Thread(target=_run, daemon=True, name="DiscordBot").start()
    logger.info("Discord bot thread started — connecting...")


def is_ready() -> bool:
    return _ready.is_set()


def reconnect_discord() -> str:
    """
    Force a fresh gateway session without restarting the app. Closes the current
    client and starts a brand-new one, which re-IDENTIFIES and pulls a fresh
    GUILD_CREATE — the only thing that recovers voice states the old session
    missed (the 'I don't see you in voice until you restart me' case).
    """
    global bot, _voice_client
    if not TOKEN:
        return "Discord nu e configurat, sefu."

    old_bot, old_loop = bot, _loop
    _ready.clear()

    # Tear down the old session on its own loop; that ends its run thread cleanly.
    if old_bot is not None and old_loop is not None and not old_loop.is_closed():
        try:
            fut = asyncio.run_coroutine_threadsafe(old_bot.close(), old_loop)
            fut.result(timeout=10)
        except Exception as e:
            logger.debug(f"old client close during reconnect: {e}")
    _voice_client = None

    # Fresh client + fresh thread/loop.
    bot = _build_bot()
    start()

    if _ready.wait(timeout=20):
        return "M-am reconectat la Discord, sefu — văd din nou cine e pe voice."
    return "Am repornit conexiunea Discord, dar încă se sincronizează, sefu. Mai încearcă în câteva secunde."


# ── Cross-thread coroutine runner ─────────────────────────────────────────────

def _run_coro(coro, timeout: int = 15):
    if _loop is None or not _ready.is_set():
        return "Botul Discord nu este conectat încă, sefu."
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return future.result(timeout=timeout)
    except Exception as e:
        return f"Eroare Discord: {e}"


# ── Member / channel lookup ───────────────────────────────────────────────────

def _find_member(name: str) -> Optional[discord.Member]:
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return None
    name_l = name.lower().strip()
    for m in guild.members:
        if m.bot:
            continue
        if m.display_name.lower() == name_l or m.name.lower() == name_l:
            return m
    for m in guild.members:
        if m.bot:
            continue
        if name_l in m.display_name.lower() or name_l in m.name.lower():
            return m
    return None


def _find_channel(name: str) -> Optional[discord.TextChannel]:
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return None
    name_l = name.lower().strip().replace(" ", "-")
    for ch in guild.text_channels:
        if ch.name.lower() == name_l or name_l in ch.name.lower():
            return ch
    return None


def _find_owner_voice_channel(owner_name: str) -> Optional[discord.VoiceChannel]:
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return None
    owner_l = owner_name.lower()
    for m in guild.members:
        if m.display_name.lower() == owner_l or m.name.lower() == owner_l:
            if m.voice and m.voice.channel:
                return m.voice.channel
    return None


async def _resolve_owner_vc_fresh(owner_name: str) -> Optional[discord.VoiceChannel]:
    """
    Failure-path self-heal for a stale member/voice cache: force a member chunk on
    the bot's own loop, then re-scan. Catches the common 'members weren't chunked
    yet' case that otherwise needs a full restart. Runs ON the bot loop, so awaiting
    guild.chunk() is safe. (Note: this can't recover voice states the gateway never
    delivered — only a reconnect fixes that.)
    """
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return None
    try:
        if not guild.chunked:
            await guild.chunk()
    except Exception as e:
        logger.debug(f"chunk() during voice resolve failed: {e}")
    owner_l = owner_name.lower()
    for m in guild.members:
        if (m.display_name.lower() == owner_l or m.name.lower() == owner_l) \
                and m.voice and m.voice.channel:
            return m.voice.channel
    return None


def _resolve_owner_vc(owner_name: str) -> Optional[discord.VoiceChannel]:
    """Cache lookup first; on a miss, chunk-and-retry on the bot loop once."""
    vc = _find_owner_voice_channel(owner_name)
    if vc:
        return vc
    fresh = _run_coro(_resolve_owner_vc_fresh(owner_name), timeout=20)
    return fresh if isinstance(fresh, discord.VoiceChannel) else None


def resolve_voice_channel(name: str) -> Optional[tuple]:
    """
    Resolve a voice-channel name to (guild_id, channel_id, channel_name).
    Used by the personal-account RPC controller. Returns None if not found.
    """
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return None
    name_l = (name or "").lower().strip()
    if not name_l:
        return None
    # Exact match first
    for ch in guild.voice_channels:
        if ch.name.lower() == name_l:
            return (guild.id, ch.id, ch.name)
    # Partial match fallback
    for ch in guild.voice_channels:
        if name_l in ch.name.lower():
            return (guild.id, ch.id, ch.name)
    return None


def list_voice_channels() -> list:
    """Return a list of voice-channel names in the guild (for diagnostics)."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return []
    return [ch.name for ch in guild.voice_channels]


# ── Public blocking API ───────────────────────────────────────────────────────

def get_pending_messages() -> list[tuple[str, str, bool, any]]:
    msgs = []
    while _message_queue:
        msgs.append(_message_queue.popleft())
    return msgs


def get_voice_notifications() -> list[str]:
    msgs = []
    while _voice_notifications:
        msgs.append(_voice_notifications.popleft())
    return msgs


def send_dm(recipient_name: str, message: str) -> str:
    member = _find_member(recipient_name)
    if not member:
        return f"Nu am găsit utilizatorul '{recipient_name}' pe server, sefu."

    async def _send():
        try:
            await member.send(message)
            _save_contact(member.display_name)
            logger.info(f"DM sent to {member.display_name}")
            return f"Mesaj trimis lui {member.display_name}, sefu."
        except discord.Forbidden:
            return f"{member.display_name} nu acceptă mesaje directe, sefu."
        except Exception as e:
            return f"Eroare la trimitere: {e}"

    return _run_coro(_send())


def send_dm_direct(recipient_name: str, message: str) -> str:
    """Send a DM directly without going through the confirmation flow.
    Used for replying to phone commands — user already confirmed intent by sending the command."""
    member = _find_member(recipient_name)
    if not member:
        return f"Nu am găsit utilizatorul '{recipient_name}' pe server, sefu."

    async def _send():
        try:
            if len(message) <= 2000:
                chunks = [message]
            else:
                chunks = [message[i:i + 1900] for i in range(0, len(message), 1900)]
            for chunk in chunks:
                await member.send(chunk)
            _save_contact(member.display_name)
            logger.info(f"Direct DM sent to {member.display_name} ({len(chunks)} chunk(s))")
            return "Răspuns trimis, sefu."
        except discord.Forbidden:
            return f"{member.display_name} nu acceptă mesaje directe, sefu."
        except Exception as e:
            return f"Eroare la trimitere: {e}"

    return _run_coro(_send())


def send_channel(channel_name: str, message: str) -> str:
    channel = _find_channel(channel_name)
    if not channel:
        return f"Nu am găsit canalul '{channel_name}' pe server, sefu."

    async def _send():
        try:
            await channel.send(message)
            logger.info(f"Message sent to #{channel.name}")
            return f"Mesaj trimis în #{channel.name}, sefu."
        except discord.Forbidden:
            return f"Nu am permisiuni să scriu în #{channel.name}, sefu."
        except Exception as e:
            return f"Eroare la trimitere: {e}"

    return _run_coro(_send())


def get_dm_history(user_name: str, limit: int = 5) -> str:
    member = _find_member(user_name)
    if not member:
        return f"Nu am găsit utilizatorul '{user_name}' pe server, sefu."

    async def _fetch():
        try:
            dm = await member.create_dm()
            messages = [m async for m in dm.history(limit=limit)]
            messages.reverse()
            if not messages:
                return f"Nu există mesaje cu {member.display_name}, sefu."
            lines = []
            for m in messages:
                who = "Tu" if m.author == bot.user else m.author.display_name
                lines.append(f"{who}: {m.content}")
            return " | ".join(lines)
        except Exception as e:
            return f"N-am putut citi conversația: {e}"

    return _run_coro(_fetch())


def join_voice(owner_name: str = "Roxan") -> str:
    from config import CONFIG
    owner = owner_name or CONFIG.owner_name
    vc    = _resolve_owner_vc(owner)
    if not vc:
        return "Nu ești pe niciun canal de voice, sefu."

    async def _join():
        global _voice_client
        try:
            if _voice_client and _voice_client.is_connected():
                if _voice_client.channel != vc:
                    await _voice_client.move_to(vc)
            else:
                _voice_client = await vc.connect(self_deaf=True, timeout=20.0, reconnect=False)
            logger.info(f"Joined voice: {vc.name}")
            return f"M-am conectat pe {vc.name}, sefu."
        except Exception as e:
            return f"Eroare la conectare: {e}"

    return _run_coro(_join(), timeout=25)


def leave_voice() -> str:
    global _voice_client

    if not _voice_client or not _voice_client.is_connected():
        return "Nu sunt conectat la niciun voice channel, sefu."

    async def _leave():
        global _voice_client
        await _voice_client.disconnect()
        _voice_client = None
        return "Am ieșit de pe voice, sefu."

    return _run_coro(_leave())


def call_discord(person_name: str, owner_name: str = "Roxan") -> str:
    from config import CONFIG
    owner = owner_name or CONFIG.owner_name
    vc    = _resolve_owner_vc(owner)
    if not vc:
        return "Sefu, nu ești conectat la niciun voice channel. Intră undeva și apoi cheamă-l!"
    member = _find_member(person_name)
    if not member:
        return f"Nu am găsit utilizatorul '{person_name}' pe server, sefu."

    async def _do_call():
        global _voice_client, _pending_call_target
        try:
            await member.send(f"🎧 Vino pe **{vc.name}**, te cheamă {owner}!")
            _pending_call_target = member.display_name
        except discord.Forbidden:
            return f"{member.display_name} nu acceptă mesaje directe, sefu."
        except Exception as e:
            return f"Nu am putut trimite invitația: {e}"
        try:
            if _voice_client and _voice_client.is_connected():
                await _voice_client.disconnect(force=True)
            _voice_client = await vc.connect(self_deaf=True, timeout=20.0, reconnect=False)
        except Exception as e:
            return f"Nu am putut intra pe voice: {e}"

        async def _auto_disconnect():
            await asyncio.sleep(300)
            if _voice_client and _voice_client.is_connected():
                await _voice_client.disconnect()
                logger.info("Auto-disconnected from voice (5min timeout)")

        asyncio.create_task(_auto_disconnect())
        return f"I-am trimis invitație lui {member.display_name} și am intrat pe {vc.name}, sefu!"

    return _run_coro(_do_call(), timeout=30)


def stream_youtube_on_voice(song_name: str, owner_name: str = "Roxan") -> str:
    from config import CONFIG
    owner = owner_name or CONFIG.owner_name
    vc    = _resolve_owner_vc(owner)
    if not vc:
        return "Sefu, nu ești conectat la niciun voice channel. Intră undeva și apoi dă comanda!"

    def _extract_flat():
        import yt_dlp
        opts = {
            "extract_flat": "in_playlist",
            "noplaylist": False,
            "quiet": True,
            "default_search": "ytsearch1",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            query = song_name
            if not query.startswith("http"):
                q = song_name if "official" in song_name.lower() else f"{song_name} official audio"
                query = f"ytsearch1:{q}"
            info    = ydl.extract_info(query, download=False)
            results = []
            if "entries" in info:
                for e in info["entries"]:
                    url = e.get("url") or e.get("id", "")
                    if url and not url.startswith("http"):
                        url = f"https://www.youtube.com/watch?v={url}"
                    if url:
                        results.append((url, e.get("title", "Unknown Track")))
            elif "url" in info or "webpage_url" in info:
                url   = info.get("webpage_url") or info.get("url", "")
                title = info.get("title", "Unknown Track")
                results.append((url, title))
            return results

    def _extract_direct(video_url: str):
        import yt_dlp
        with yt_dlp.YoutubeDL({"format": "bestaudio/best", "quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(video_url, download=False)
            return info.get("url"), info.get("title")

    async def _play_next():
        global _voice_client
        if not _music_queue or not _voice_client or not _voice_client.is_connected():
            return
        video_url, title = _music_queue.popleft()
        try:
            stream_url, real_title = await _loop.run_in_executor(None, _extract_direct, video_url)
            if not stream_url:
                raise ValueError("No stream URL")
            try:
                import imageio_ffmpeg
                exe = imageio_ffmpeg.get_ffmpeg_exe()
            except ImportError:
                exe = "ffmpeg"
            ffmpeg_opts = {
                "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                "options": "-vn",
            }
            source = discord.FFmpegPCMAudio(stream_url, executable=exe, **ffmpeg_opts)

            def _after(error):
                if error:
                    logger.error(f"Playback error: {error}")
                if _music_queue:
                    asyncio.run_coroutine_threadsafe(_play_next(), _loop)

            _voice_client.play(source, after=_after)
            logger.info(f"Now playing: {real_title or title}")
        except Exception as e:
            logger.error(f"Play error for {title}: {e}")
            if _music_queue:
                asyncio.run_coroutine_threadsafe(_play_next(), _loop)

    async def _queue_and_play():
        global _voice_client
        tracks = await _loop.run_in_executor(None, _extract_flat)
        if not tracks:
            return "Nu am găsit nicio melodie sau playlist, sefu."

        _music_queue.extend(tracks)

        try:
            if not _voice_client or not _voice_client.is_connected():
                _voice_client = await vc.connect(self_deaf=True, timeout=20.0, reconnect=False)
            elif _voice_client.channel != vc:
                await _voice_client.move_to(vc)

            if not _voice_client.is_playing():
                asyncio.create_task(_play_next())
                if len(tracks) > 1:
                    return f"Am pornit playlistul cu {len(tracks)} piese. Pun prima melodie, sefu."
                return f"Pun '{tracks[0][1]}' pe Discord, sefu."
            else:
                if len(tracks) > 1:
                    return f"Am adăugat {len(tracks)} melodii la coadă, sefu."
                return f"Am adăugat '{tracks[0][1]}' la coadă, sefu."
        except Exception as e:
            return f"Nu am putut intra pe voice sau seta melodia: {e}"

    return _run_coro(_queue_and_play(), timeout=45)


def stop_audio() -> str:
    global _voice_client, _music_queue
    _music_queue.clear()
    if _voice_client and _voice_client.is_playing():
        _voice_client.stop()
        return "Am oprit muzica și am șters coada, sefu."
    return "Nu rulează nicio muzică în acest moment, sefu."


def get_music_queue() -> str:
    if not _music_queue:
        return "Nu există nicio melodie în așteptare, sefu."
    lines = ["Melodii în coadă:"]
    for i, (url, title) in enumerate(_music_queue):
        if i >= 10:
            lines.append(f"… și încă {len(_music_queue) - 10} piese.")
            break
        lines.append(f"{i + 1}. {title}")
    return "\n".join(lines)


def speak_on_discord(text: str, owner_name: str = "Roxan") -> str:
    from config import CONFIG
    if not text:
        return "Nu am primit text pentru a vorbi, sefu."
    owner = owner_name or CONFIG.owner_name
    vc    = _resolve_owner_vc(owner)
    if not vc:
        return "Sefu, nu ești conectat la niciun voice channel."

    async def _do_speak():
        global _voice_client
        try:
            if not _voice_client or not _voice_client.is_connected():
                _voice_client = await vc.connect(self_deaf=True, timeout=20.0, reconnect=False)
            elif _voice_client.channel != vc:
                await _voice_client.move_to(vc)

            import edge_tts
            tmp = os.path.join(tempfile.gettempdir(), "miko_discord_tts.mp3")
            communicate = edge_tts.Communicate(text, "ro-RO-AlinaNeural")
            await communicate.save(tmp)

            if _voice_client.is_playing():
                _voice_client.stop()

            try:
                import imageio_ffmpeg
                exe = imageio_ffmpeg.get_ffmpeg_exe()
            except ImportError:
                exe = "ffmpeg"

            source = discord.FFmpegPCMAudio(tmp, executable=exe)
            _voice_client.play(source)
            return "Am dictat textul pe Discord, sefu."
        except Exception as e:
            return f"Nu am putut vorbi pe Discord: {e}"

    return _run_coro(_do_speak(), timeout=30)


# ── Interactive Hub UI ────────────────────────────────────────────────────────

class _UserSelect(discord.ui.Select):
    def __init__(self, names: list):
        options = [discord.SelectOption(label=n, value=n) for n in names[:25]] or [
            discord.SelectOption(label="Niciun contact.", value="none")
        ]
        super().__init__(
            placeholder="Selectează o persoană…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        if name == "none":
            await interaction.response.send_message("Momentan nu ai contacte.", ephemeral=True)
            return
        member = _find_member(name)
        if not member:
            await interaction.response.edit_message(content=f"'{name}' nu a fost găsit.")
            return
        try:
            dm     = await member.create_dm()
            msgs   = [m async for m in dm.history(limit=6)]
            msgs.reverse()
            if not msgs:
                history = f"Nu există mesaje cu {member.display_name}."
            else:
                lines = []
                for m in msgs:
                    who = "Miko" if m.author == bot.user else m.author.display_name
                    if len(m.content) < 100:
                        lines.append(f"**{who}**: {m.content}")
                history = "\n".join(lines[-5:]) or "Niciun mesaj relevant."
            embed = discord.Embed(
                title=f"Conversație cu {member.display_name}",
                description=history,
                color=discord.Color.blue(),
            )
            await interaction.response.edit_message(content=None, embed=embed, view=self.view)
        except Exception as e:
            await interaction.response.edit_message(content=f"Eroare: {e}")


class _HubView(discord.ui.View):
    def __init__(self, names: list):
        super().__init__(timeout=None)
        self.add_item(_UserSelect(names))


def send_interactive_message_hub(owner_name: str = "Roxan") -> str:
    from config import CONFIG
    owner = owner_name or CONFIG.owner_name
    member = _find_member(owner)
    if not member:
        return f"Nu l-am găsit pe '{owner}' pe server."
    _load_contacts()

    async def _send():
        try:
            view = _HubView(list(_cached_contacts))
            await member.send(
                "Centrul de mesaje interactiv, sefu. Alege de la cine vrei să citești:",
                view=view,
            )
            logger.info(f"Interactive hub sent to {member.display_name} (id={member.id})")
            return (
                f"Ți-am trimis meniul interactiv lui {member.display_name} pe Discord, sefu. "
                "Caută mesajul în Mesaje Directe — dacă e prima dată, verifică și 'Message Requests'."
            )
        except discord.Forbidden:
            return f"{member.display_name} nu acceptă mesaje directe sau are DM-urile dezactivate, sefu."
        except Exception as e:
            logger.error(f"Hub send error: {e}")
            return f"Eroare la trimiterea hub-ului: {e}"

    return _run_coro(_send())
