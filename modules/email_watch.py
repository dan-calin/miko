"""
modules/email_watch.py — let Miko watch the inbox for specific mail and ping on Discord.

The feature the user asked for: "look out for an email from netcom training and ping
me on Discord when I receive one." A rule matches on sender and/or subject; a daemon
polls IMAP (reusing modules.email_box) every couple minutes and, on a NEW match, DMs
the owner via Discord. Rules are one-shot by default (fire once, then deactivate) — the
natural "tell me when it arrives" behaviour — but can be standing (recurring) too.

Stored in data/email_watch.json. Dedupe is by message Message-ID so the same email is
never announced twice, even across restarts.
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("miko.email_watch")

_started = False
_SEEN_CAP = 200   # keep the dedupe list bounded per rule

TOOL_DECLARATIONS = [
    {
        "name": "watch_email",
        "description": (
            "Watch the inbox for matching email and ping the user on Discord when it "
            "arrives. Use for 'look out for emails from X and let me know', 'tell me when "
            "HR emails me', 'ping me about anything from netcom training'. Give at least "
            "one of sender/subject. By DEFAULT it keeps watching and alerts on EVERY new "
            "matching email (recurring) — this is what 'look out for emails from X' means. "
            "Only set recurring=false when the user wants a single one-off heads-up for one "
            "specific awaited message ('tell me the moment THIS reply lands, then stop')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "sender": {"type": "STRING", "description": "Sender name or email substring to match (e.g. 'netcom')."},
                "subject": {"type": "STRING", "description": "Subject keyword to match (optional)."},
                "recurring": {"type": "BOOLEAN", "description": "Keep alerting on every match. Default TRUE (standing watch). false = fire once then stop."},
            },
        },
    },
    {
        "name": "list_email_watches",
        "description": "List the inbox watches Miko has set (who/what she's looking out for).",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "cancel_email_watch",
        "description": "Stop an inbox watch by its id or a keyword from its sender/subject.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"which": {"type": "STRING", "description": "Watch id or keyword."}},
            "required": ["which"],
        },
    },
]


def _path() -> Path:
    from config import CONFIG
    return CONFIG.data_dir / "email_watch.json"


def _load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(reg: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")


def _label(rule: dict) -> str:
    bits = []
    if rule.get("sender"):
        bits.append(f"from '{rule['sender']}'")
    if rule.get("subject"):
        bits.append(f"subject ~ '{rule['subject']}'")
    return " · ".join(bits) or "(any mail)"


# ── Tools ─────────────────────────────────────────────────────────────────────

def watch_email(sender: str = "", subject: str = "", recurring: bool = True) -> str:
    sender = (sender or "").strip()
    subject = (subject or "").strip()
    if not sender and not subject:
        return "Tell me who or what to watch for — a sender and/or a subject keyword."
    from modules.email_box import _Cfg
    if not _Cfg().imap_ready():
        return ("Email isn't configured, so I can't watch the inbox. Set EMAIL_USER / "
                "EMAIL_PASS / EMAIL_IMAP_HOST in .env (for Gmail use an App Password).")
    reg = _load()
    mode = "every time" if recurring else "once, when it first arrives"
    # Don't stack duplicate watches for the same target — reuse an existing active
    # one (and deactivate any other duplicates so a single email pings only once).
    sl, jl = sender.lower(), subject.lower()
    existing = [r for r in reg.values()
                if r.get("active") and r.get("sender", "").strip().lower() == sl
                and r.get("subject", "").strip().lower() == jl]
    if existing:
        keep = existing[0]
        keep["recurring"] = bool(recurring)
        for dup in existing[1:]:
            dup["active"] = False
        _save(reg)
        start()
        return f"Already watching for mail {_label(keep)} — I'll ping you on Discord {mode}."
    rid = "watch-" + uuid.uuid4().hex[:6]
    reg[rid] = {
        "sender": sender, "subject": subject, "recurring": bool(recurring),
        "active": True, "created": datetime.now().isoformat(), "seen": [],
    }
    _save(reg)
    start()  # make sure the daemon is running
    return f"Watching for mail {_label(reg[rid])} — I'll ping you on Discord {mode}. (id {rid})"


def list_email_watches() -> str:
    reg = _load()
    active = {k: v for k, v in reg.items() if v.get("active")}
    if not active:
        return "I'm not watching the inbox for anything right now."
    lines = ["Inbox watches:"]
    for rid, r in active.items():
        kind = "recurring" if r.get("recurring") else "once"
        lines.append(f"- [{rid}] {_label(r)} ({kind})")
    return "\n".join(lines)


def cancel_email_watch(which: str) -> str:
    reg = _load()
    which = (which or "").strip()
    key = which if which in reg else next(
        (k for k, v in reg.items()
         if which.lower() in (v.get("sender", "") + " " + v.get("subject", "")).lower()), None)
    if not key:
        return f"No inbox watch matching '{which}'."
    reg.pop(key, None)
    _save(reg)
    return f"Stopped watching ({key})."


# ── Matching + poll ─────────────────────────────────────────────────────────────

def _msg_ts(raw: str):
    """Email Date header → epoch float (tz-aware), or None."""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        return dt.timestamp() if dt else None
    except Exception:
        return None


def _strip_gmail_dots(text: str) -> str:
    """Gmail ignores dots in the local part, so 'a.b@gmail.com' == 'ab@gmail.com'.
    Normalize that inside any gmail/googlemail address in the string."""
    return re.sub(
        r'([\w.+-]+)@(gmail|googlemail)\.com',
        lambda m: m.group(1).replace('.', '') + '@gmail.com',
        (text or "").lower(),
    )


def _matches(rule: dict, frm: str, subj: str) -> bool:
    s = (rule.get("sender") or "").strip().lower()
    j = (rule.get("subject") or "").strip().lower()
    f = (frm or "").lower()
    if s and s not in f and _strip_gmail_dots(s) not in _strip_gmail_dots(f):
        return False
    if j and j not in (subj or "").lower():
        return False
    return bool(s or j)


def _fetch_snippet(M, uid, limit: int = 1500) -> str:
    """Pull a readable text preview of the matched message body."""
    try:
        import email as _email
        from modules.email_box import _body_text
        typ, md = M.uid("FETCH", uid, "(BODY.PEEK[])")
        if not md or not md[0]:
            return ""
        msg = _email.message_from_bytes(md[0][1])
        body = " ".join((_body_text(msg) or "").split())
        return body[:limit] + ("…" if len(body) > limit else "")
    except Exception as e:
        logger.debug(f"email-watch snippet fetch failed: {e}")
        return ""


def _notify(rule: dict, frm: str, subj: str, date: str, body: str = "") -> None:
    try:
        from config import CONFIG
        from modules.discord_bot import send_dm_direct
        msg = (f"📬 Email you were waiting for arrived\n"
               f"From: {frm}\nSubject: {subj}" + (f"\nDate: {date}" if date else ""))
        if body:
            msg += f"\n\n{body}"
        send_dm_direct(recipient_name=CONFIG.owner_name, message=msg)
        logger.info(f"email-watch ping sent for {_label(rule)}")
    except Exception as e:
        logger.warning(f"email-watch notify failed: {e}")


_poll_count = 0


def _poll_once() -> None:
    global _poll_count
    _poll_count += 1
    reg = _load()
    active = {k: v for k, v in reg.items() if v.get("active")}
    if not active:
        logger.debug(f"email-watch poll #{_poll_count}: no active watches")
        return
    from modules.email_box import _Cfg, _imap, _dec
    if not _Cfg().imap_ready():
        logger.warning("email-watch poll: email isn't configured (EMAIL_USER/PASS/IMAP)")
        return
    import email as _email
    from email.utils import parseaddr

    # Look back to the oldest still-active rule's creation (cap at 7 days) so a watch
    # set moments before the mail lands still catches it, without scanning the whole box.
    oldest = min((r.get("created", "") for r in active.values()), default="")
    try:
        since_dt = datetime.fromisoformat(oldest) if oldest else datetime.now()
    except Exception:
        since_dt = datetime.now()
    since_dt = max(since_dt - timedelta(minutes=5), datetime.now() - timedelta(days=7))
    since = since_dt.strftime("%d-%b-%Y")

    # Per-rule creation epoch — a watch must only fire for mail that arrives AFTER it
    # was set, never the backlog already in the inbox (a 2-min grace covers clock skew).
    GRACE = 120
    created_ts = {}
    for rid, rule in active.items():
        try:
            created_ts[rid] = datetime.fromisoformat(rule.get("created", "")).timestamp() - GRACE
        except Exception:
            created_ts[rid] = 0

    M = None
    fired = 0
    try:
        M = _imap()
        M.select("INBOX", readonly=True)
        typ, data = M.uid("SEARCH", None, f"(SINCE {since})")
        uids = (data[0].split() if data and data[0] else [])[-50:]
        if not uids:
            logger.debug(f"email-watch poll #{_poll_count}: {len(active)} watch(es), 0 msgs since {since}")
            return
        changed = False
        notified = set()   # message-ids already pinged this poll (dedupe across overlapping watches)
        for uid in uids:
            typ, md = M.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            if not md or not md[0]:
                continue
            hdr = _email.message_from_bytes(md[0][1])
            frm_raw = _dec(hdr.get("From", ""))
            frm = parseaddr(frm_raw)[1] or frm_raw
            subj = _dec(hdr.get("Subject", ""))
            mid = (hdr.get("Message-ID") or hdr.get("Message-Id") or uid.decode(errors="replace")).strip()
            date = _dec(hdr.get("Date", ""))[:30]
            msg_ts = _msg_ts(hdr.get("Date", ""))
            for rid, rule in active.items():
                if mid in rule.get("seen", []):
                    continue
                if _matches(rule, frm_raw + " " + frm, subj):
                    # Ignore (but remember) mail that predates the watch — don't fire on
                    # the backlog, and don't let it burn a fire-once watch.
                    if msg_ts and created_ts.get(rid) and msg_ts < created_ts[rid]:
                        rule.setdefault("seen", []).append(mid)
                        rule["seen"] = rule["seen"][-_SEEN_CAP:]
                        changed = True
                        continue
                    # Another overlapping watch already pinged for this exact email this
                    # poll — record it as seen here too, but don't send a duplicate DM.
                    if mid not in notified:
                        snippet = _fetch_snippet(M, uid)
                        _notify(rule, frm or frm_raw, subj or "(no subject)", date, snippet)
                        notified.add(mid)
                        fired += 1
                    rule.setdefault("seen", []).append(mid)
                    rule["seen"] = rule["seen"][-_SEEN_CAP:]
                    if not rule.get("recurring"):
                        rule["active"] = False
                    changed = True
        if changed:
            _save(reg)   # reg holds the same dict objects as `active` → updates persist
        log = logger.info if fired else logger.debug
        log(f"email-watch poll #{_poll_count}: {len(active)} watch(es), scanned "
            f"{len(uids)} msg(s) since {since}, fired {fired}")
    except Exception as e:
        logger.warning(f"email-watch poll #{_poll_count} ERROR: {e}")
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


def _interval() -> int:
    # 30s by default so a new email pings within about half a minute, not 2 minutes.
    try:
        return max(15, int(os.getenv("MIKO_EMAIL_WATCH_INTERVAL", "30")))
    except ValueError:
        return 30


def _loop() -> None:
    logger.info(f"Email-watch daemon started (polling every {_interval()}s)")
    while True:
        try:
            _poll_once()
        except BaseException as e:                 # never let the poll loop die
            logger.warning(f"email-watch loop error: {e}")
        try:
            time.sleep(_interval())
        except BaseException:
            time.sleep(120)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="EmailWatch").start()
