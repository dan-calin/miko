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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import CONFIG
from utils.logger import setup_logger

logger = setup_logger(CONFIG.logs_dir / "tools_server.log")


def _start_discord() -> None:
    if not CONFIG.discord_token:
        print("[tools-server] Discord token not set — Discord tools disabled.")
        return
    try:
        from modules.discord_bot import start as discord_start
        discord_start()
        print("[tools-server] Discord bot started.")
    except Exception as e:
        logger.warning(f"Discord bot failed to start: {e}")
        print(f"[tools-server] Discord bot failed: {e}")


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
    _start_discord()

    try:
        from modules.calendar_reminders import start as _start_reminders
        _start_reminders(CONFIG.owner_name)
        print(f"[tools-server] Calendar reminders active (pinging {CONFIG.owner_name} on Discord).")
    except Exception as e:
        print(f"[tools-server] Calendar reminders failed to start: {e}")

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
