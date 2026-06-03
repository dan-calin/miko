"""
modules/calendar.py — Calendar integration: iCloud (CalDAV) + Microsoft Teams/Outlook (Graph API)
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("miko.calendar")

TOOL_DECLARATIONS = [
    {
        "name": "list_events",
        "description": "List upcoming calendar events from iCloud, Microsoft Teams/Outlook, or both.",
        "parameters": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days ahead to look (default 7).",
                },
                "source": {
                    "type": "string",
                    "enum": ["all", "icloud", "teams"],
                    "description": "Which calendar to query: 'all', 'icloud', or 'teams'.",
                },
            },
        },
    },
    {
        "name": "get_today_events",
        "description": "Get all calendar events for today from both iCloud and Teams.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "create_event",
        "description": "Create a new calendar event in iCloud or Teams/Outlook.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title."},
                "date":  {"type": "string", "description": "Date in YYYY-MM-DD format."},
                "time":  {"type": "string", "description": "Start time in HH:MM (24h, UTC). Default 09:00."},
                "duration_minutes": {"type": "integer", "description": "Duration in minutes (default 60)."},
                "description": {"type": "string", "description": "Optional event description/notes."},
                "source": {
                    "type": "string",
                    "enum": ["teams", "icloud"],
                    "description": "Which calendar to create in (default: teams).",
                },
            },
            "required": ["title", "date"],
        },
    },
    {
        "name": "delete_event",
        "description": "Delete a calendar event by its ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The event ID returned by list_events."},
                "source": {
                    "type": "string",
                    "enum": ["teams", "icloud"],
                    "description": "Which calendar the event is in (default: teams).",
                },
            },
            "required": ["event_id"],
        },
    },
]

# ── iCloud CalDAV ─────────────────────────────────────────────────────────────

def _icloud_client():
    import caldav
    email    = os.getenv("ICLOUD_EMAIL", "")
    password = os.getenv("ICLOUD_APP_PASSWORD", "")
    if not email or not password:
        raise RuntimeError("ICLOUD_EMAIL and ICLOUD_APP_PASSWORD not set in .env")
    return caldav.DAVClient(url="https://caldav.icloud.com", username=email, password=password)


def _parse_ical_component(component) -> dict | None:
    try:
        from datetime import date as date_type
        summary = str(component.get("SUMMARY", "No title"))
        dtstart = component.get("DTSTART").dt
        dtend_prop = component.get("DTEND") or component.get("DTSTART")
        dtend = dtend_prop.dt
        uid  = str(component.get("UID", ""))
        desc = str(component.get("DESCRIPTION", ""))

        def _fmt(dt):
            if isinstance(dt, datetime):
                return dt.strftime("%Y-%m-%d %H:%M")
            if isinstance(dt, date_type):
                return dt.strftime("%Y-%m-%d 00:00")
            return str(dt)

        return {"id": uid, "title": summary, "start": _fmt(dtstart),
                "end": _fmt(dtend), "description": desc, "source": "icloud"}
    except Exception as e:
        logger.debug(f"ical component parse skip: {e}")
        return None


def _icloud_list_events(days_ahead: int) -> list[dict]:
    client    = _icloud_client()
    principal = client.principal()
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    events = []
    for cal in principal.calendars():
        try:
            for ev in cal.date_search(start=now, end=end, expand=True):
                try:
                    for component in ev.icalendar_instance.walk("VEVENT"):
                        parsed = _parse_ical_component(component)
                        if parsed:
                            events.append(parsed)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"iCloud calendar read error: {e}")
    return events


def _icloud_writable_calendars(principal) -> list:
    """
    Order iCloud calendars so the most likely writable one is tried first.
    iCloud also returns read-only calendars (subscribed, holidays, birthdays)
    which reject writes with 403 Forbidden — push those to the back.
    Honors ICLOUD_CALENDAR_NAME if set (exact or partial, case-insensitive).
    """
    calendars = principal.calendars()
    if not calendars:
        return []

    preferred = os.getenv("ICLOUD_CALENDAR_NAME", "").lower().strip()
    read_only_hint = ("holiday", "sărbători", "sarbatori", "birthday",
                      "zile de naștere", "subscribed", "abonat", "us ", "uk ")

    def _rank(cal) -> int:
        try:
            name = (cal.name or "").lower()
        except Exception:
            name = ""
        if preferred and preferred in name:
            return 0
        # Prefer calendars that explicitly support events
        try:
            comps = [c.upper() for c in (cal.get_supported_components() or [])]
            if comps and "VEVENT" not in comps:
                return 9  # can't hold events at all
        except Exception:
            pass
        if any(h in name for h in read_only_hint):
            return 8
        return 5

    return sorted(calendars, key=_rank)


def _icloud_create_event(title: str, start_dt: datetime, end_dt: datetime, description: str) -> str:
    import uuid
    client    = _icloud_client()
    principal = client.principal()
    calendars = _icloud_writable_calendars(principal)
    if not calendars:
        raise RuntimeError("No iCloud calendars found.")

    uid       = str(uuid.uuid4())
    now_s     = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start_s   = start_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end_s     = end_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    ical = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Miko//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:{now_s}\r\nDTSTART:{start_s}\r\nDTEND:{end_s}\r\n"
        f"SUMMARY:{title}\r\nDESCRIPTION:{description}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    # Try writable calendars in order; skip ones that reject the write (403).
    last_err = None
    for cal in calendars:
        try:
            cal.save_event(ical)
            return uid
        except Exception as e:
            last_err = e
            if "forbidden" in str(e).lower() or "authorization" in str(e).lower():
                continue  # read-only calendar — try the next one
            raise
    raise RuntimeError(
        f"Niciun calendar iCloud nu acceptă scrierea. "
        f"Setează ICLOUD_CALENDAR_NAME în .env cu numele unui calendar editabil. "
        f"(ultima eroare: {last_err})"
    )


def _icloud_delete_event(uid: str) -> bool:
    client    = _icloud_client()
    principal = client.principal()
    for cal in principal.calendars():
        try:
            for ev in cal.events():
                try:
                    for component in ev.icalendar_instance.walk("VEVENT"):
                        if str(component.get("UID", "")) == uid:
                            ev.delete()
                            return True
                except Exception:
                    pass
        except Exception:
            pass
    return False


# ── Microsoft Graph ────────────────────────────────────────────────────────────

_MS_SCOPES = ["https://graph.microsoft.com/Calendars.ReadWrite"]


def _token_cache_path() -> Path:
    from config import CONFIG
    return CONFIG.data_dir / "ms_token.json"


def _get_ms_token() -> str:
    import msal
    client_id = os.getenv("AZURE_CLIENT_ID", "")
    tenant_id = os.getenv("AZURE_TENANT_ID", "")
    if not client_id or not tenant_id:
        raise RuntimeError("AZURE_CLIENT_ID and AZURE_TENANT_ID not set in .env")

    cache      = msal.SerializableTokenCache()
    cache_file = _token_cache_path()
    if cache_file.exists():
        cache.deserialize(cache_file.read_text())

    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(_MS_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            if cache.has_state_changed:
                cache_file.write_text(cache.serialize())
            return result["access_token"]

    # First-time / expired: device code flow
    flow = app.initiate_device_flow(scopes=_MS_SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow error: {flow.get('error_description', 'unknown')}")

    # Return the login instructions as a RuntimeError so the tool surfaces them
    raise _DeviceCodeRequired(
        verification_uri=flow["verification_uri"],
        user_code=flow["user_code"],
        app=app,
        flow=flow,
        cache=cache,
        cache_file=cache_file,
    )


class _DeviceCodeRequired(Exception):
    """Raised when a Microsoft login is needed. Callers should present the code to the user."""
    def __init__(self, verification_uri, user_code, app, flow, cache, cache_file):
        self.verification_uri = verification_uri
        self.user_code        = user_code
        self._app             = app
        self._flow            = flow
        self._cache           = cache
        self._cache_file      = cache_file
        super().__init__(
            f"Microsoft login required. Go to {verification_uri} and enter code: {user_code}"
        )

    def complete_auth(self) -> str:
        result = self._app.acquire_token_by_device_flow(self._flow)
        if "access_token" not in result:
            raise RuntimeError(f"Auth failed: {result.get('error_description', result.get('error'))}")
        if self._cache.has_state_changed:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(self._cache.serialize())
        return result["access_token"]


def _ms_get(path: str) -> dict:
    import requests
    r = requests.get(
        f"https://graph.microsoft.com/v1.0{path}",
        headers={"Authorization": f"Bearer {_get_ms_token()}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _ms_post(path: str, body: dict) -> dict:
    import requests
    r = requests.post(
        f"https://graph.microsoft.com/v1.0{path}",
        headers={"Authorization": f"Bearer {_get_ms_token()}", "Content-Type": "application/json"},
        json=body,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _ms_delete(path: str) -> None:
    import requests
    r = requests.delete(
        f"https://graph.microsoft.com/v1.0{path}",
        headers={"Authorization": f"Bearer {_get_ms_token()}"},
        timeout=15,
    )
    r.raise_for_status()


def _ms_list_events(days_ahead: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    start_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_s   = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    data = _ms_get(
        f"/me/calendarview?startDateTime={start_s}&endDateTime={end_s}"
        f"&$orderby=start/dateTime&$top=50"
        f"&$select=id,subject,start,end,bodyPreview"
    )
    events = []
    for item in data.get("value", []):
        start_raw = item.get("start", {}).get("dateTime", "")
        end_raw   = item.get("end",   {}).get("dateTime", "")
        events.append({
            "id":          item.get("id", ""),
            "title":       item.get("subject", "No title"),
            "start":       start_raw[:16].replace("T", " "),
            "end":         end_raw[:16].replace("T", " "),
            "description": item.get("bodyPreview", ""),
            "source":      "teams",
        })
    return events


def _ms_create_event(title: str, start_dt: datetime, end_dt: datetime, description: str) -> str:
    body = {
        "subject": title,
        "body": {"contentType": "text", "content": description},
        "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
        "end":   {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),   "timeZone": "UTC"},
    }
    result = _ms_post("/me/events", body)
    return result.get("id", "")


# ── Public tool functions ─────────────────────────────────────────────────────

def list_events(days_ahead: int = 7, source: str = "all") -> str:
    events = []
    errors = []
    ms_login = None  # set if Teams needs the one-time Microsoft device login

    if source in ("all", "icloud"):
        try:
            events += _icloud_list_events(days_ahead)
        except Exception as e:
            errors.append(f"iCloud: {e}")

    if source in ("all", "teams"):
        try:
            events += _ms_list_events(days_ahead)
        except _DeviceCodeRequired as e:
            ms_login = (
                f"Pentru calendarul Teams, autentifică-te o singură dată: "
                f"mergi la {e.verification_uri} și introdu codul {e.user_code}."
            )
            # Only block the whole response if Teams was specifically requested.
            if source == "teams":
                return (
                    f"Autentificare Microsoft necesară pentru Teams.\n"
                    f"1. Mergi la: {e.verification_uri}\n"
                    f"2. Introdu codul: {e.user_code}\n"
                    f"Apoi încearcă din nou comanda."
                )
        except Exception as e:
            errors.append(f"Teams: {e}")

    if not events:
        if errors:
            msg = "Eroare la accesarea calendarelor: " + "; ".join(errors)
        else:
            msg = f"Nu ai niciun eveniment în următoarele {days_ahead} zile."
        if ms_login:
            msg += f"\n{ms_login}"
        return msg

    events.sort(key=lambda x: x["start"])
    lines = [f"Evenimente — următoarele {days_ahead} zile ({len(events)}):"]
    for ev in events:
        tag = "[iCloud]" if ev["source"] == "icloud" else "[Teams] "
        lines.append(f"  {tag} {ev['start']} — {ev['title']}")
        if ev.get("description"):
            lines.append(f"          {ev['description'][:80]}")

    if errors:
        lines.append(f"\nAvertisment: {'; '.join(errors)}")
    if ms_login:
        lines.append(f"\n{ms_login}")
    return "\n".join(lines)


def get_today_events() -> str:
    return list_events(days_ahead=1, source="all")


def create_event(
    title: str,
    date: str,
    time: str = "09:00",
    duration_minutes: int = 60,
    description: str = "",
    source: str = "teams",
) -> str:
    try:
        naive = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return "Format invalid. Folosește data YYYY-MM-DD și ora HH:MM."
    # The spoken time is the user's LOCAL time. Interpret it in the system's
    # local timezone, then convert to UTC for storage (fixes DST/BST offsets).
    start_dt = naive.astimezone(timezone.utc)

    end_dt = start_dt + timedelta(minutes=duration_minutes)

    try:
        if source == "icloud":
            uid = _icloud_create_event(title, start_dt, end_dt, description)
            return f"Eveniment creat în iCloud: '{title}' pe {date} la {time}."
        else:
            _ms_create_event(title, start_dt, end_dt, description)
            return f"Eveniment creat în Teams/Outlook: '{title}' pe {date} la {time}."
    except _DeviceCodeRequired as e:
        return (
            f"Autentificare Microsoft necesară.\n"
            f"1. Mergi la: {e.verification_uri}\n"
            f"2. Introdu codul: {e.user_code}\n"
            f"Apoi încearcă din nou."
        )
    except Exception as e:
        logger.error(f"create_event error: {e}", exc_info=True)
        return f"Eroare la crearea evenimentului: {e}"


def delete_event(event_id: str, source: str = "teams") -> str:
    try:
        if source == "icloud":
            found = _icloud_delete_event(event_id)
            return "Evenimentul a fost șters din iCloud." if found else "Evenimentul nu a fost găsit în iCloud."
        else:
            _ms_delete(f"/me/events/{event_id}")
            return "Evenimentul a fost șters din Teams/Outlook."
    except _DeviceCodeRequired as e:
        return (
            f"Autentificare Microsoft necesară.\n"
            f"1. Mergi la: {e.verification_uri}\n"
            f"2. Introdu codul: {e.user_code}"
        )
    except Exception as e:
        logger.error(f"delete_event error: {e}", exc_info=True)
        return f"Eroare la ștergerea evenimentului: {e}"
