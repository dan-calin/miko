"""
modules/schedule_briefs.py — scheduled calendar awareness.

A daemon (same pattern as calendar_reminders) that:
  - refreshes a cached copy of today's events every ~15 min, and
  - sends a Discord DM brief at morning / midday / night.

The cache (`data/schedule_today.json`) is read cheaply by the voice + chat
prompts so Miko always knows the day's schedule without hitting the calendar
backend on every turn.
"""

import json
import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger("miko.briefs")

# (slot name, local hour, DM header)
_SLOTS = [
    ("morning", 8,  "☀ Good morning — here's your day"),
    ("midday",  12, "🌤 Midday check-in"),
    ("night",   20, "🌙 Tonight / tomorrow heads-up"),
]
_POLL = 900            # refresh + slot check every 15 min
_started = False
_sent: set = set()     # (date_iso, slot) so each brief fires once/day
_lock = threading.Lock()

# Phrases that mean "nothing scheduled" (don't DM an empty brief).
_EMPTY_HINTS = ("nu ai", "niciun eveniment", "no event", "nothing")


def _cache_path() -> Path:
    from config import CONFIG
    return CONFIG.data_dir / "schedule_today.json"


def _refresh() -> str:
    """Fetch today's events and cache them. Returns the schedule text ('' on failure)."""
    try:
        from modules.calendar import get_today_events
        text = (get_today_events() or "").strip()
    except Exception as e:
        logger.debug(f"schedule refresh failed: {e}")
        return ""
    try:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"date": date.today().isoformat(), "text": text}),
                     encoding="utf-8")
    except Exception:
        pass
    return text


def get_today_brief(max_chars: int = 500) -> str:
    """Cheap, cached read of today's schedule for prompt injection ('' if none/stale)."""
    try:
        p = _cache_path()
        if not p.exists():
            return ""
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("date") != date.today().isoformat():
            return ""
        text = (d.get("text") or "").strip()
        if not text or any(h in text.lower() for h in _EMPTY_HINTS):
            return ""
        return text[:max_chars]
    except Exception:
        return ""


def _loop(owner_name: str) -> None:
    logger.info("Schedule-briefs daemon started (morning/midday/night)")
    while True:
        try:
            text = _refresh()
            now = datetime.now()
            today = now.date().isoformat()
            for name, hour, header in _SLOTS:
                if now.hour != hour:
                    continue
                key = (today, name)
                with _lock:
                    if key in _sent:
                        continue
                    _sent.add(key)
                if text and not any(h in text.lower() for h in _EMPTY_HINTS):
                    try:
                        from modules.discord_bot import send_dm_direct
                        send_dm_direct(recipient_name=owner_name, message=f"{header}:\n{text}")
                        logger.info(f"Sent {name} brief to {owner_name}")
                    except Exception as e:
                        logger.debug(f"brief DM failed: {e}")
            # keep the dedup set from growing across days
            if len(_sent) > 30:
                with _lock:
                    _sent.intersection_update({(today, n) for n, _, _ in _SLOTS})
        except Exception as e:
            logger.warning(f"brief loop error: {e}")
        time.sleep(_POLL)


def start(owner_name: str) -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, args=(owner_name,), daemon=True,
                     name="ScheduleBriefs").start()
