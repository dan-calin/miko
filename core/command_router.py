"""
core/command_router.py — Tool call dispatcher with safety guards and confirmation flow.

All module imports are lazy (inside _dispatch_module) so a missing dependency
in one module never prevents other tools from working.
"""

import logging
import time
import threading
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("miko.router")

# Tools that require explicit voice confirmation before executing
REQUIRES_CONFIRMATION: frozenset = frozenset({
    "delete_file",
    "delete_note",
    "send_discord_dm",
    "send_discord_channel",
    "send_interactive_hub",
    "send_email",
    "shutdown_computer",
    "restart_computer",
    "format_drive",          # blocked anyway, but belt+suspenders
})

# How long to wait for voice confirmation before auto-cancelling (seconds)
CONFIRMATION_TIMEOUT = 30


@dataclass
class ConfirmationPending:
    tool_name: str
    args: dict
    prompt: str       # Romanian text Miko should speak to ask for confirmation
    created_at: float = 0.0

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > CONFIRMATION_TIMEOUT


class CommandRouter:
    def __init__(self, config, speak_callback: Optional[Callable[[str], None]] = None):
        self._config = config
        self.speak_callback = speak_callback
        self._pending: Optional[ConfirmationPending] = None
        self._lock = threading.Lock()

    def _t(self, en: str, ro: str) -> str:
        """Pick the string for the configured language (default English)."""
        return en if getattr(self._config, "language", "en") == "en" else ro

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def has_pending_confirmation(self) -> bool:
        with self._lock:
            if self._pending and self._pending.is_expired():
                logger.info("Confirmation timed out — auto-cancelling")
                self._pending = None
                if self.speak_callback:
                    self.speak_callback(self._t(
                        "The confirmation timed out. I cancelled the operation, boss.",
                        "Confirmarea a expirat. Am anulat operațiunea, sefu.",
                    ))
            return self._pending is not None

    def check_and_resolve_confirmation(self, text: str) -> Optional[str]:
        """
        Called with every transcription when has_pending_confirmation is True.
        Returns a result string if confirmation was resolved, else None.
        """
        from core.wake_word import is_confirmation
        decision = is_confirmation(text)
        if decision is None:
            return None  # Not a confirmation response — ignore
        return self.resolve_confirmation(decision)

    def resolve_confirmation(self, confirmed: bool) -> str:
        with self._lock:
            pending = self._pending
            self._pending = None

        if pending is None:
            return self._t("There's no pending operation.", "Nu există nicio operațiune în așteptare.")

        if not confirmed:
            return self._t("Cancelled, boss.", "Am anulat operațiunea, sefu.")

        # Execute the previously-held tool call
        return self._dispatch_module(pending.tool_name, pending.args)

    def dispatch(self, tool_name: str, args: dict) -> "str | ConfirmationPending":
        """
        Main entry point from AudioHandler._execute_tool.
        1. Safety check
        2. Confirmation gate for destructive tools
        3. Module dispatch
        """
        # Layer 1 — safety guard
        safe, reason = self._safety_check(tool_name, args)
        if not safe:
            logger.warning(f"Safety block: {tool_name} — {reason}")
            return self._t(
                f"The operation was blocked for security reasons: {reason}",
                f"Operațiunea a fost blocată din motive de securitate: {reason}",
            )

        # Layer 2 — confirmation gate
        if tool_name in REQUIRES_CONFIRMATION:
            with self._lock:
                # Reuse existing pending if it's the same tool and hasn't expired.
                # Prevents Gemini re-calling the tool from spawning a second confirmation loop.
                if (
                    self._pending
                    and self._pending.tool_name == tool_name
                    and not self._pending.is_expired()
                ):
                    logger.info(f"Reusing existing confirmation for: {tool_name}")
                    return self._pending
                prompt = self._build_confirmation_prompt(tool_name, args)
                pending = ConfirmationPending(tool_name=tool_name, args=args, prompt=prompt)
                self._pending = pending
            logger.info(f"Confirmation required for: {tool_name}")
            return pending

        return self._dispatch_module(tool_name, args)

    # ── Safety ────────────────────────────────────────────────────────────────

    def _safety_check(self, tool_name: str, args: dict) -> tuple[bool, str]:
        """Validates all string arguments against blocked paths and registry keys."""
        blocked_paths = self._config.SAFETY_BLOCKED_PATHS
        blocked_reg   = self._config.SAFETY_BLOCKED_REGISTRY

        for val in args.values():
            if not isinstance(val, str):
                continue
            val_lower = val.lower()
            for blocked in blocked_paths:
                if val_lower.startswith(blocked):
                    return False, f"Calea '{blocked}' este protejată."
            for reg in blocked_reg:
                if val_lower.startswith(reg):
                    return False, "Operațiunile pe registrul Windows sunt blocate."

        # Explicit tool-level blocks
        if tool_name in ("format_drive", "edit_registry", "disable_antivirus"):
            return False, f"Tool-ul '{tool_name}' este blocat permanent."

        return True, "OK"

    # ── Confirmation prompt builder ───────────────────────────────────────────

    def _build_confirmation_prompt(self, tool_name: str, args: dict) -> str:
        if tool_name == "delete_file":
            path = args.get("path", args.get("name", "the file"))
            return self._t(
                f"Are you sure you want to delete '{path}'? Say 'yes' to confirm or 'no' to cancel.",
                f"Ești sigur că vrei să ștergi '{path}'? Spune 'da' pentru a confirma sau 'nu' pentru a anula.",
            )
        if tool_name in ("send_discord_dm", "send_discord_channel"):
            recipient = args.get("recipient_name", args.get("channel_name", "recipient"))
            message   = args.get("message", "")
            return self._t(
                f"Do you want me to send {recipient} the message: \"{message}\"? "
                "Say 'yes' to confirm or 'no' to cancel.",
                f"Vrei să trimit mesajul lui {recipient}: \"{message}\"? "
                "Spune 'da' pentru a confirma sau 'nu' pentru a anula.",
            )
        if tool_name == "shutdown_computer":
            return self._t(
                "Are you sure you want to shut down the computer? Say 'yes' or 'no'.",
                "Ești sigur că vrei să oprești calculatorul? Spune 'da' sau 'nu'.",
            )
        if tool_name == "restart_computer":
            return self._t(
                "Are you sure you want to restart the computer? Say 'yes' or 'no'.",
                "Ești sigur că vrei să repornești calculatorul? Spune 'da' sau 'nu'.",
            )
        return self._t(
            f"Confirm running '{tool_name}'? Say 'yes' to confirm or 'no' to cancel.",
            f"Confirmi executarea '{tool_name}'? Spune 'da' pentru a confirma sau 'nu' pentru a anula.",
        )

    # ── Module dispatch ───────────────────────────────────────────────────────

    def _dispatch_module(self, tool_name: str, args: dict) -> str:
        """
        Lazy import + call. Each branch imports only what it needs.
        A missing dependency returns a Romanian error, never raises.
        """
        logger.info(f"Dispatch: {tool_name}({list(args.keys())})")
        try:
            # ── MCP (external servers — dynamic tool names mcp_<server>_<tool>) ─
            if tool_name.startswith("mcp_"):
                from modules.mcp_client import call_tool
                return call_tool(tool_name, args)

            if tool_name == "spawn_agents":
                from modules.subagents import spawn_agents
                return spawn_agents(**args)

            if tool_name == "code_with_claude":
                from modules.claude_code import code_with_claude
                return code_with_claude(**args)

            # ── Media control ────────────────────────────────────────────────
            if tool_name == "set_volume":
                from modules.media_control import set_volume
                return set_volume(**args)

            if tool_name == "media_control":
                from modules.media_control import media_control
                return media_control(**args)

            if tool_name == "get_volume":
                from modules.media_control import get_volume
                return f"Volumul curent este {get_volume()}%."

            # ── Discord ──────────────────────────────────────────────────────
            if tool_name == "send_discord_dm":
                from modules.discord_bot import send_dm
                return send_dm(**args)

            if tool_name == "send_discord_channel":
                from modules.discord_bot import send_channel
                return send_channel(**args)

            if tool_name == "call_discord":
                from modules.discord_bot import call_discord
                return call_discord(**args)

            if tool_name == "join_voice":
                from modules.discord_bot import join_voice
                return join_voice(**args)

            if tool_name == "leave_voice":
                from modules.discord_bot import leave_voice
                return leave_voice()

            if tool_name == "reconnect_discord":
                from modules.discord_bot import reconnect_discord
                return reconnect_discord()

            if tool_name == "stop_audio":
                from modules.discord_bot import stop_audio
                return stop_audio()

            if tool_name == "play_media_discord":
                from modules.discord_bot import stream_youtube_on_voice
                return stream_youtube_on_voice(**args)

            if tool_name == "get_music_queue":
                from modules.discord_bot import get_music_queue
                return get_music_queue()

            if tool_name == "speak_on_discord":
                from modules.discord_bot import speak_on_discord
                return speak_on_discord(**args)

            if tool_name == "get_dm_history":
                from modules.discord_bot import get_dm_history
                return get_dm_history(**args)

            if tool_name == "send_interactive_hub":
                from modules.discord_bot import send_interactive_message_hub
                owner = args.get("owner_name", self._config.owner_name)
                return send_interactive_message_hub(owner)

            # ── YouTube / browser ────────────────────────────────────────────
            if tool_name == "play_youtube":
                from modules.youtube_player import play_in_browser
                return play_in_browser(**args)

            if tool_name == "get_video_info":
                from modules.youtube_player import get_video_info
                return get_video_info(**args)

            if tool_name == "get_trending":
                from modules.youtube_player import get_trending
                return get_trending(**args)

            # ── Email ────────────────────────────────────────────────────────
            if tool_name == "list_emails":
                from modules.email_box import list_emails
                return list_emails(**args)

            if tool_name == "read_email":
                from modules.email_box import read_email
                return read_email(**args)

            if tool_name == "search_emails":
                from modules.email_box import search_emails
                return search_emails(**args)

            if tool_name == "send_email":
                from modules.email_box import send_email
                return send_email(**args)

            # ── Scheduled tasks ──────────────────────────────────────────────
            if tool_name == "schedule_task":
                from modules.scheduled_tasks import schedule_task
                return schedule_task(**args)

            if tool_name == "list_scheduled_tasks":
                from modules.scheduled_tasks import list_scheduled_tasks
                return list_scheduled_tasks()

            if tool_name == "cancel_scheduled_task":
                from modules.scheduled_tasks import cancel_scheduled_task
                return cancel_scheduled_task(**args)

            # ── Browser automation ───────────────────────────────────────────
            if tool_name == "browser_open":
                from modules.browser import browser_open
                return browser_open(**args)

            if tool_name == "browser_click":
                from modules.browser import browser_click
                return browser_click(**args)

            if tool_name == "browser_type":
                from modules.browser import browser_type
                return browser_type(**args)

            if tool_name == "browser_extract":
                from modules.browser import browser_extract
                return browser_extract()

            if tool_name == "browser_screenshot":
                from modules.browser import browser_screenshot
                return browser_screenshot()

            # ── Research ─────────────────────────────────────────────────────
            if tool_name == "web_search":
                from modules.research import web_search
                return web_search(**args)

            if tool_name == "deep_research":
                from modules.research import deep_research
                return deep_research(**args)

            # ── Knowledge (learn / recall) ───────────────────────────────────
            if tool_name == "remember":
                from modules.knowledge import remember
                return remember(**args)

            if tool_name == "recall":
                from modules.knowledge import recall
                return recall(**args)

            if tool_name == "forget":
                from modules.knowledge import forget
                return forget(**args)

            # ── Projects (user-mapped) ───────────────────────────────────────
            if tool_name == "add_project":
                from modules.projects import add_project
                return add_project(**args)

            if tool_name == "list_projects":
                from modules.projects import list_projects
                return list_projects()

            if tool_name == "forget_project":
                from modules.projects import forget_project
                return forget_project(**args)

            # ── Notes ────────────────────────────────────────────────────────
            if tool_name == "create_note":
                from modules.notes import _notes_manager
                return _notes_manager().create_note(**args)

            if tool_name == "read_note":
                from modules.notes import _notes_manager
                return _notes_manager().read_note(**args)

            if tool_name == "list_notes":
                from modules.notes import _notes_manager
                return _notes_manager().list_notes()

            if tool_name == "search_notes":
                from modules.notes import _notes_manager
                return _notes_manager().search_notes(**args)

            if tool_name == "delete_note":
                from modules.notes import _notes_manager
                return _notes_manager().delete_note(**args)

            # ── OS control ───────────────────────────────────────────────────
            if tool_name == "open_app":
                from modules.os_control import open_app
                return open_app(**args)

            if tool_name == "file_op":
                from modules.os_control import file_op
                return file_op(**args)

            if tool_name == "delete_file":
                from modules.os_control import file_op
                path = args.get("path", args.get("name", ""))
                return file_op(action="delete", path=path)

            if tool_name == "run_command":
                from modules.os_control import run_command
                return run_command(**args)

            if tool_name == "set_reminder":
                from modules.os_control import set_reminder
                return set_reminder(**args)

            if tool_name == "system_info":
                from modules.os_control import system_info
                return system_info(**args)

            if tool_name == "take_screenshot":
                from modules.os_control import take_screenshot
                return take_screenshot()

            if tool_name == "lock_workstation":
                from modules.os_control import lock_workstation
                return lock_workstation()

            if tool_name == "open_url":
                from modules.research import open_url
                return open_url(**args)

            if tool_name == "shutdown_computer":
                from modules.os_control import shutdown_computer
                return shutdown_computer()

            if tool_name == "restart_computer":
                from modules.os_control import restart_computer
                return restart_computer()

            if tool_name == "clipboard":
                from modules.os_control import clipboard
                return clipboard(**args)

            if tool_name == "window_control":
                from modules.os_control import window_control
                return window_control(**args)

            if tool_name == "process_manager":
                from modules.os_control import process_manager
                return process_manager(**args)

            if tool_name == "calculate":
                from modules.os_control import calculate
                return calculate(**args)

            if tool_name == "type_text":
                from modules.os_control import type_text
                return type_text(**args)

            if tool_name == "send_shortcut":
                from modules.os_control import send_shortcut
                return send_shortcut(**args)

            if tool_name == "wifi_control":
                from modules.os_control import wifi_control
                return wifi_control(**args)

            if tool_name == "weather":
                from modules.research import weather
                return weather(**args)

            if tool_name == "get_network_info":
                from modules.research import get_network_info
                return get_network_info()

            # ── Journey planning ─────────────────────────────────────────────
            if tool_name == "search_nearby_places":
                from modules.journey import search_nearby_places
                return search_nearby_places(**args)

            if tool_name == "calculate_route":
                from modules.journey import calculate_route
                return calculate_route(**args)

            if tool_name == "plan_journey":
                from modules.journey import plan_journey
                return plan_journey(**args)

            # Legacy redirects (in case old session still calls these)
            if tool_name == "find_nearby":
                from modules.journey import search_nearby_places
                place = args.get("place_type", "restaurant")
                return search_nearby_places(query=place)

            if tool_name == "joy_ride":
                from modules.journey import search_nearby_places
                return search_nearby_places(query="tourist attraction or scenic park")

            # ── File indexer ─────────────────────────────────────────────────
            if tool_name == "find_file":
                from modules.file_indexer import _indexer
                return _indexer().find_file(**args)

            if tool_name == "rebuild_file_index":
                from modules.file_indexer import _indexer
                return _indexer().rebuild_index()

            # ── Calendar ─────────────────────────────────────────────────────
            if tool_name == "list_events":
                from modules.calendar import list_events
                return list_events(**args)

            if tool_name == "get_today_events":
                from modules.calendar import get_today_events
                return get_today_events()

            if tool_name == "create_event":
                from modules.calendar import create_event
                return create_event(**args)

            if tool_name == "delete_event":
                from modules.calendar import delete_event
                return delete_event(**args)

            # ── Personal Discord account (RPC) ───────────────────────────────
            if tool_name == "join_voice_as_me":
                from modules.discord_rpc import join_voice_as_me
                return join_voice_as_me(**args)

            if tool_name == "leave_voice_as_me":
                from modules.discord_rpc import leave_voice_as_me
                return leave_voice_as_me()

            if tool_name == "set_my_voice":
                from modules.discord_rpc import set_my_voice
                return set_my_voice(**args)

            if tool_name == "discord_rpc_login":
                from modules.discord_rpc import discord_rpc_login
                return discord_rpc_login()

            # ── Unknown ──────────────────────────────────────────────────────
            logger.warning(f"Unknown tool: {tool_name}")
            return f"Nu cunosc tool-ul '{tool_name}', sefu."

        except ImportError as e:
            logger.error(f"Import error for {tool_name}: {e}")
            return f"Modulul pentru '{tool_name}' nu este disponibil: {e}"
        except TypeError as e:
            logger.error(f"Argument error for {tool_name}: {e}")
            return f"Argumente incorecte pentru '{tool_name}': {e}"
        except Exception as e:
            logger.error(f"Error in {tool_name}: {e}", exc_info=True)
            return f"A apărut o eroare la '{tool_name}', sefu: {e}"
