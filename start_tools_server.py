"""
start_tools_server.py — Lightweight Miko tool server (no audio, no Gemini Live).

Use this when you want Hermes (or any MCP client) to access Miko's Windows tools
without starting the full voice assistant.

Starts:
  - Tool HTTP server  on port 7832  (Hermes MCP bridge)
  - Discord bot       (optional — only if DISCORD_TOKEN is set, needed for Discord tools)
  - File indexer      (optional — runs in background, enables find_file)

Does NOT start:
  - Gemini Live audio session
  - Microphone capture
  - Text-to-speech output

Usage:
  cd "C:\\Users\\Roxan\\Desktop\\Jarvis V2"
  python start_tools_server.py
"""

import sys
import time
import logging
import threading

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import CONFIG
from utils.logger import setup_logger

logger = setup_logger(CONFIG.logs_dir / "tools_server.log")


def _start_discord(command_router) -> None:
    if not CONFIG.discord_token:
        print("[tools-server] Discord token not set — Discord tools disabled.")
        return
    try:
        from modules.discord_bot import (
            start as discord_start, get_pending_messages, send_dm_direct,
        )
        discord_start()
        print("[tools-server] Discord bot started.")
    except Exception as e:
        logger.warning(f"Discord bot failed to start: {e}")
        print(f"[tools-server] Discord bot failed: {e}")
        return

    # Process INCOMING Discord DMs even without the voice app running (parity
    # with main.py). Without this loop the bot connects and can SEND, but nobody
    # drains the inbound queue — so messages people send Miko go unseen.
    # Trusted senders (owner + TRUSTED_VOICE_USERS) get a real reply via the
    # phone commander; untrusted senders are logged but not acted on (so a random
    # stranger can't drive Miko). Add trusted names via TRUSTED_VOICE_USERS / Settings.
    try:
        from modules.phone_commander import init_commander
        commander = init_commander(CONFIG, command_router)
    except Exception as e:
        logger.warning(f"Phone commander init failed: {e}")
        print(f"[tools-server] Discord DM processing unavailable: {e}")
        return

    owner_l = CONFIG.owner_name.lower()

    def _poll() -> None:
        while True:
            time.sleep(2.0)
            try:
                for sender, content, is_dm, audio_data in get_pending_messages():
                    if not is_dm:
                        logger.info(f"[discord] mention from {sender}: {content[:60]}")
                        continue
                    trusted = (sender.lower() == owner_l
                               or sender.lower() in CONFIG.trusted_voice_users)
                    if not trusted:
                        logger.info(f"[discord] DM from untrusted user {sender!r} ignored: {content[:60]}")
                        continue

                    def _handle(s=sender, c=content, ad=audio_data):
                        try:
                            if ad:
                                audio_bytes, mime_type = ad
                                reply = commander.process_voice(audio_bytes, mime_type, sender=s)
                            else:
                                reply = commander.process_text(c, sender=s)
                            send_dm_direct(recipient_name=s, message=reply)
                            logger.info(f"[discord] replied to {s}")
                        except Exception as e:
                            logger.warning(f"[discord] DM handling error for {s}: {e}")

                    logger.info(f"[discord] DM from {sender}: {content[:60]}")
                    threading.Thread(target=_handle, daemon=True, name="DiscordDM").start()
            except Exception as e:
                logger.warning(f"[discord] poll error: {e}")

    threading.Thread(target=_poll, daemon=True, name="DiscordPoll").start()
    print("[tools-server] Discord DM processing active (replies to owner + trusted users).")


def _start_file_indexer() -> None:
    try:
        from modules.file_indexer import _indexer
        _indexer().start()
        print("[tools-server] File indexer started.")
    except Exception as e:
        logger.warning(f"File indexer failed to start: {e}")
        print(f"[tools-server] File indexer failed (find_file won't work): {e}")


def main() -> None:
    print()
    print("=" * 50)
    print("   Miko Tool Server (headless mode)")
    print(f"   Port : {__import__('os').getenv('TOOL_SERVER_PORT', '7832')}")
    print("   Audio: DISABLED")
    print(f"   Discord: {'enabled' if CONFIG.discord_token else 'disabled'}")
    print("=" * 50)
    print()

    from core.command_router import CommandRouter
    command_router = CommandRouter(CONFIG, speak_callback=None)

    # Wire os_control speak callback to a no-op so reminder timers don't crash
    try:
        from modules.os_control import set_speak_callback as _set_os_speak
        _set_os_speak(lambda text: logger.info(f"[reminder] {text}"))
    except Exception:
        pass

    _start_file_indexer()
    _start_discord(command_router)

    try:
        from modules.calendar_reminders import start as _start_reminders
        _start_reminders(CONFIG.owner_name)
        print(f"[tools-server] Calendar reminders active (pinging {CONFIG.owner_name} on Discord).")
    except Exception as e:
        print(f"[tools-server] Calendar reminders failed to start: {e}")

    # Scheduled tasks + inbox watches persist on disk; their daemons must run every
    # session, not only when a task/watch is created this run.
    try:
        from modules.scheduled_tasks import start as _start_tasks
        _start_tasks()
        from modules.email_watch import start as _start_watch
        _start_watch()
        print("[tools-server] Scheduled tasks + inbox watches active.")
    except Exception as e:
        print(f"[tools-server] Task/email-watch daemons failed to start: {e}")

    from tool_server import start as _start_tool_server
    _start_tool_server(command_router)

    port = __import__("os").getenv("TOOL_SERVER_PORT", "7832")
    print(f"[tools-server] Ready. Listening on http://0.0.0.0:{port}")
    print("[tools-server] Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n[tools-server] Stopped.")


if __name__ == "__main__":
    main()
