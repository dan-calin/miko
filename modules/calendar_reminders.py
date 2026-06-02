"""
modules/calendar_reminders.py — Background calendar reminder daemon.

Polls iCloud + Teams every 10 minutes. Sends a Discord DM:
  - 30 minutes before each event
  -  5 minutes before each event

Deduplicates via (event_id, lead_minutes) so reminders fire exactly once.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("miko.calendar.reminders")

# Minutes before an event to send a reminder (add/remove as needed)
LEAD_TIMES = [30, 5]

# How often to poll calendars (seconds). Keep >= 300 to avoid iCloud rate limits.
POLL_INTERVAL = 600

_sent: set = set()
_lock = threading.Lock()
_started = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_events() -> list[dict]:
    """Pull upcoming events from all available sources. Silently skips failures."""
    events = []

    try:
        from modules.calendar import _icloud_list_events
        events += _icloud_list_events(days_ahead=1)
    except Exception as e:
        logger.debug(f"iCloud reminder fetch skip: {e}")

    try:
        from modules.calendar import _ms_list_events
        events += _ms_list_events(days_ahead=1)
    except Exception:
        pass  # Teams not authed yet — silent

    return events


def _parse_dt(start_str: str) -> datetime | None:
    """Parse 'YYYY-MM-DD HH:MM' string → UTC datetime."""
    try:
        return datetime.strptime(start_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _dedup_key(event: dict, lead: int) -> tuple:
    eid = event.get("id") or f"{event.get('title', '')}|{event.get('start', '')}"
    return (eid, lead)


def _build_message(event: dict, lead: int, start_dt: datetime) -> str:
    source = "📅 iCloud" if event.get("source") == "icloud" else "📅 Teams"
    time_str = start_dt.strftime("%H:%M UTC")
    title = event.get("title", "Unnamed event")

    if lead >= 30:
        msg = f"⏰ **{title}** starts in 30 minutes ({time_str}). {source}"
    else:
        msg = f"🔔 **{title}** starts in {lead} minutes! ({time_str}) {source}"

    desc = event.get("description", "").strip()
    if desc:
        msg += f"\n_{desc[:120]}_"

    return msg


# ── Core check ────────────────────────────────────────────────────────────────

def _check_and_notify(owner_name: str) -> None:
    events = _fetch_events()
    if not events:
        return

    now = datetime.now(timezone.utc)
    poll_window = POLL_INTERVAL / 60  # minutes

    for event in events:
        start_dt = _parse_dt(event.get("start", ""))
        if start_dt is None:
            continue

        minutes_until = (start_dt - now).total_seconds() / 60

        for lead in LEAD_TIMES:
            # Fire if we're within (lead - poll_window, lead + 2] minutes of the event.
            # The +2 gives a small buffer; dedup ensures it fires only once per lead.
            if lead - poll_window < minutes_until <= lead + 2:
                key = _dedup_key(event, lead)
                with _lock:
                    if key in _sent:
                        continue
                    _sent.add(key)

                msg = _build_message(event, lead, start_dt)
                try:
                    from modules.discord_bot import send_dm_direct
                    send_dm_direct(recipient_name=owner_name, message=msg)
                    logger.info(f"Reminder sent [{lead}min]: {event.get('title')}")
                except Exception as e:
                    logger.warning(f"Reminder DM failed: {e}")


def _trim_sent() -> None:
    """Keep the dedup set from growing forever."""
    with _lock:
        if len(_sent) > 1000:
            _sent.clear()


# ── Daemon loop ───────────────────────────────────────────────────────────────

def _loop(owner_name: str) -> None:
    logger.info(f"Calendar reminder daemon started (poll every {POLL_INTERVAL//60}min, leads: {LEAD_TIMES}min)")
    while True:
        try:
            _check_and_notify(owner_name)
            _trim_sent()
        except Exception as e:
            logger.warning(f"Reminder loop error: {e}")
        time.sleep(POLL_INTERVAL)


def start(owner_name: str) -> None:
    """Start the reminder daemon. Safe to call multiple times — only starts once."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(
        target=_loop,
        args=(owner_name,),
        daemon=True,
        name="CalendarReminder",
    ).start()
    logger.info("Calendar reminder daemon launched")
