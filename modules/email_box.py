"""
modules/email_box.py — IMAP/SMTP email for Miko (read, triage, draft, send).

Gives the Chief-of-Staff / email-ops skills a real mailbox. Read-only IMAP for
listing/reading/searching; SMTP for sending (a sensitive action — gated behind
confirmation/approval like other sends). Configure in .env:

  EMAIL_USER, EMAIL_PASS            (account + app password)
  EMAIL_IMAP_HOST, EMAIL_IMAP_PORT  (default port 993, SSL)
  EMAIL_SMTP_HOST, EMAIL_SMTP_PORT  (default port 587, STARTTLS; 465 = SSL)
  EMAIL_FROM                        (optional display From, defaults to EMAIL_USER)
"""

import base64
import logging
import os
import re
from email.header import decode_header, make_header
from email.utils import parseaddr

logger = logging.getLogger("miko.email")

TOOL_DECLARATIONS = [
    {
        "name": "list_emails",
        "description": (
            "List recent emails from the inbox (newest first): sender, subject, date. "
            "Use for 'check my email', 'what's in my inbox', 'any new mail'. Set "
            "unread_only=true for just unread."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "limit": {"type": "INTEGER", "description": "How many to list (default 10)."},
                "unread_only": {"type": "BOOLEAN", "description": "Only unread (default false)."},
            },
        },
    },
    {
        "name": "read_email",
        "description": "Read the full body of an email matched by a subject keyword or sender.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "A keyword from the subject or the sender."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_emails",
        "description": "Search the mailbox for emails matching keywords (subject/body/sender).",
        "parameters": {
            "type": "OBJECT",
            "properties": {"query": {"type": "STRING", "description": "Search terms."}},
            "required": ["query"],
        },
    },
    {
        "name": "triage_inbox",
        "description": (
            "Answer a natural-language question about recent emails by reading the last "
            "few days of inbox and filtering by meaning (not just keywords). Use for "
            "'have I received any job-related emails in the past 2 days?', 'any "
            "invoices this week?', 'did anyone reply about the apartment?'. Returns the "
            "matching emails with a one-line reason each."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "criteria": {"type": "STRING", "description": "What to look for, in plain language (e.g. 'job related', 'invoices')."},
                "days": {"type": "INTEGER", "description": "How many days back to scan (default 2)."},
            },
            "required": ["criteria"],
        },
    },
    {
        "name": "send_email",
        "description": (
            "Send an email. Sensitive action — approval is handled automatically by the "
            "system (the chat Approve toggle / voice confirmation); do NOT ask the user to "
            "confirm in text, just call it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "to": {"type": "STRING", "description": "Recipient address."},
                "subject": {"type": "STRING", "description": "Subject line."},
                "body": {"type": "STRING", "description": "Plain-text body."},
            },
            "required": ["to", "subject", "body"],
        },
    },
]


class _Cfg:
    def __init__(self):
        self.user = os.getenv("EMAIL_USER", "").strip()
        self.password = os.getenv("EMAIL_PASS", "").strip()
        self.imap_host = os.getenv("EMAIL_IMAP_HOST", "").strip()
        self.imap_port = int(os.getenv("EMAIL_IMAP_PORT", "993") or 993)
        self.smtp_host = os.getenv("EMAIL_SMTP_HOST", "").strip()
        self.smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587") or 587)
        self.from_addr = os.getenv("EMAIL_FROM", "").strip() or self.user

    def imap_ready(self):
        return bool(self.user and self.password and self.imap_host)

    def smtp_ready(self):
        return bool(self.user and self.password and self.smtp_host)


def _dec(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg) -> str:
    """Extract a readable plain-text body from an email.message.Message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
        # fall back to stripped HTML
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    from modules.research import _html_to_text
                    raw = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                    return _html_to_text(raw)
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return str(msg.get_payload())


def _imap():
    import imaplib
    cfg = _Cfg()
    M = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    M.login(cfg.user, cfg.password)
    return M


def list_emails(limit: int = 10, unread_only: bool = False, folder: str = "INBOX") -> str:
    cfg = _Cfg()
    if not cfg.imap_ready():
        return "Email isn't configured. Set EMAIL_USER / EMAIL_PASS / EMAIL_IMAP_HOST in .env."
    import email as _email
    try:
        M = _imap()
        M.select(folder, readonly=True)
        typ, data = M.search(None, "UNSEEN" if unread_only else "ALL")
        ids = data[0].split()[-int(limit or 10):][::-1]
        if not ids:
            M.logout()
            return "No emails found." if not unread_only else "No unread emails."
        lines = [f"{'Unread' if unread_only else 'Recent'} emails:"]
        for n, i in enumerate(ids, 1):
            typ, md = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            hdr = _email.message_from_bytes(md[0][1])
            frm = parseaddr(_dec(hdr.get("From", "")))[1] or _dec(hdr.get("From", ""))
            subj = _dec(hdr.get("Subject", "(no subject)"))
            date = _dec(hdr.get("Date", ""))[:25]
            lines.append(f"{n}. {subj} — {frm} ({date})")
        M.logout()
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"list_emails error: {e}")
        return f"Couldn't reach the mailbox: {e}"


def read_email(query: str) -> str:
    cfg = _Cfg()
    if not cfg.imap_ready():
        return "Email isn't configured."
    import email as _email
    try:
        M = _imap()
        M.select("INBOX", readonly=True)
        typ, data = M.search(None, "TEXT", f'"{query}"')
        ids = data[0].split()
        if not ids:
            typ, data = M.search(None, "FROM", f'"{query}"')
            ids = data[0].split()
        if not ids:
            M.logout()
            return f"No email matching '{query}'."
        typ, md = M.fetch(ids[-1], "(RFC822)")
        msg = _email.message_from_bytes(md[0][1])
        M.logout()
        frm = _dec(msg.get("From", ""))
        subj = _dec(msg.get("Subject", "(no subject)"))
        body = _body_text(msg).strip()
        if len(body) > 2000:
            body = body[:2000] + "\n… (truncated)"
        return f"From: {frm}\nSubject: {subj}\n\n{body}"
    except Exception as e:
        logger.error(f"read_email error: {e}")
        return f"Couldn't read that email: {e}"


def search_emails(query: str) -> str:
    cfg = _Cfg()
    if not cfg.imap_ready():
        return "Email isn't configured."
    import email as _email
    try:
        M = _imap()
        M.select("INBOX", readonly=True)
        typ, data = M.search(None, "TEXT", f'"{query}"')
        ids = data[0].split()[-10:][::-1]
        if not ids:
            M.logout()
            return f"No emails matching '{query}'."
        lines = [f"Emails matching '{query}':"]
        for i in ids:
            typ, md = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            hdr = _email.message_from_bytes(md[0][1])
            frm = parseaddr(_dec(hdr.get("From", "")))[1]
            lines.append(f"- {_dec(hdr.get('Subject', '(no subject)'))} — {frm}")
        M.logout()
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"search_emails error: {e}")
        return f"Search failed: {e}"


def _recent_messages(days: int, cap: int = 30) -> list:
    """Fetch (from, subject, date, snippet) for inbox mail from the last `days` days."""
    from datetime import datetime, timedelta
    import email as _email
    from email.utils import parseaddr
    since = (datetime.now() - timedelta(days=max(1, days))).strftime("%d-%b-%Y")
    out = []
    M = None
    try:
        M = _imap()
        M.select("INBOX", readonly=True)
        typ, data = M.uid("SEARCH", None, f"(SINCE {since})")
        uids = (data[0].split() if data and data[0] else [])[-cap:][::-1]
        for uid in uids:
            typ, md = M.uid("FETCH", uid, "(RFC822)")
            if not md or not md[0]:
                continue
            msg = _email.message_from_bytes(md[0][1])
            frm = _dec(msg.get("From", ""))
            subj = _dec(msg.get("Subject", "(no subject)"))
            date = _dec(msg.get("Date", ""))[:25]
            snippet = " ".join(_body_text(msg).split())[:280]
            out.append({"from": frm, "subject": subj, "date": date, "snippet": snippet})
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass
    return out


def triage_inbox(criteria: str, days: int = 2) -> str:
    cfg = _Cfg()
    if not cfg.imap_ready():
        return "Email isn't configured. Set EMAIL_USER / EMAIL_PASS / EMAIL_IMAP_HOST in .env."
    criteria = (criteria or "").strip()
    if not criteria:
        return "What should I look for? (e.g. 'job related', 'invoices')"
    try:
        days = int(days or 2)
    except (TypeError, ValueError):
        days = 2
    try:
        msgs = _recent_messages(days)
    except Exception as e:
        logger.error(f"triage_inbox fetch error: {e}")
        return f"Couldn't reach the mailbox: {e}"
    if not msgs:
        return f"No emails in the last {days} day(s)."

    catalog = "\n".join(
        f"[{i}] From: {m['from']} | Subject: {m['subject']} | {m['date']}\n    {m['snippet']}"
        for i, m in enumerate(msgs, 1)
    )
    sys_prompt = (
        "You triage a user's inbox. Given a list of recent emails and a plain-language "
        "criterion, return ONLY the emails that genuinely match the criterion's meaning. "
        "For each match, one line: 'From — Subject — short reason'. If none match, reply "
        "exactly 'No matching emails.'. Be precise; do not invent emails."
    )
    user = f"Criterion: {criteria}\n\nRecent emails (last {days} day(s)):\n{catalog}"
    try:
        from chat_backend import complete_text
        from config import CONFIG
        ans = complete_text("gemini", "gemini-2.5-flash",
                            api_key=getattr(CONFIG, "gemini_api_key", ""),
                            system=sys_prompt, user=user, max_tokens=600).strip()
    except Exception as e:
        logger.warning(f"triage_inbox LLM failed: {e}")
        return (f"I pulled {len(msgs)} email(s) from the last {days} day(s) but couldn't "
                f"analyze them: {e}")
    header = f"Scanned {len(msgs)} email(s) from the last {days} day(s) for “{criteria}”:\n"
    return header + (ans or "No matching emails.")


def _parse_ts(raw: str):
    """Email Date header → epoch float, or None."""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        return dt.timestamp() if dt else None
    except Exception:
        return None


def inbox_view(limit: int = 25, unread_only: bool = False, folder: str = "INBOX") -> dict:
    """Structured inbox listing for the UI: newest first, with unread flags + uids.
    Read-only (BODY.PEEK) so nothing gets marked seen just by listing."""
    cfg = _Cfg()
    if not cfg.imap_ready():
        return {"error": "Email isn't configured. Add EMAIL_USER / EMAIL_PASS / EMAIL_IMAP_HOST in Settings → Email."}
    import email as _email
    M = None
    try:
        limit = max(1, min(int(limit or 25), 100))
        M = _imap()
        M.select(folder, readonly=True)
        typ, data = M.uid("SEARCH", None, "UNSEEN" if unread_only else "ALL")
        uids = (data[0].split() if data and data[0] else [])[-limit:][::-1]
        msgs = []
        for uid in uids:
            typ, md = M.uid("FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if not md:
                continue
            meta, hdr_bytes = b"", b""
            for part in md:
                if isinstance(part, tuple):
                    meta += part[0] or b""
                    hdr_bytes = part[1] or hdr_bytes
                elif isinstance(part, (bytes, bytearray)):
                    meta += part
            unread = "\\Seen" not in meta.decode("utf-8", "replace")
            hdr = _email.message_from_bytes(hdr_bytes)
            name, addr = parseaddr(_dec(hdr.get("From", "")))
            raw_date = _dec(hdr.get("Date", ""))
            msgs.append({
                "uid": uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid),
                "from_name": name or addr or _dec(hdr.get("From", "")),
                "from_addr": addr,
                "subject": _dec(hdr.get("Subject", "(no subject)")),
                "date": raw_date[:31],
                "ts": _parse_ts(hdr.get("Date", "")),
                "unread": unread,
            })
        return {"messages": msgs, "account": cfg.user, "folder": folder}
    except Exception as e:
        logger.error(f"inbox_view error: {e}")
        return {"error": f"Couldn't reach the mailbox: {e}"}
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


_ATT_MAX = 8_000_000        # per-attachment embed cap (raw bytes)
_ATT_TOTAL = 16_000_000     # total embedded-attachment cap per message


def _decode_part(part) -> str:
    try:
        return (part.get_payload(decode=True) or b"").decode(
            part.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return ""


def _inline_cids(html: str, inline: dict) -> str:
    """Rewrite <img src="cid:..."> to the embedded data: URIs."""
    if not html or not inline:
        return html

    def repl(m):
        cid = m.group(1).strip().strip("<>")
        uri = inline.get(cid)
        return f'src="{uri}"' if uri else m.group(0)

    return re.sub(r'src\s*=\s*["\']?\s*cid:([^"\'>\s]+)["\']?', repl, html, flags=re.IGNORECASE)


def _collect(msg):
    """Walk a message → (plain_text, html, inline_cid_map, attachments).
    Inline images and attachments are embedded as data: URIs (size-capped) so the
    UI can render them without a second authenticated request."""
    text, html, inline, atts, total = "", "", {}, [], 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition", "")).lower()
        cid = part.get("Content-ID")
        filename = part.get_filename()
        if filename:
            filename = _dec(filename)
        is_attach = bool(filename) or "attachment" in disp
        if ctype == "text/plain" and not is_attach and not text:
            text = _decode_part(part); continue
        if ctype == "text/html" and not is_attach and not html:
            html = _decode_part(part); continue
        if cid and ctype.startswith("image/"):
            payload = part.get_payload(decode=True) or b""
            if 0 < len(payload) <= _ATT_MAX:
                inline[cid.strip().strip("<>")] = "data:%s;base64,%s" % (
                    ctype, base64.b64encode(payload).decode())
        if is_attach:
            payload = part.get_payload(decode=True) or b""
            size = len(payload)
            entry = {
                "filename": filename or ("image" if ctype.startswith("image/") else "attachment"),
                "content_type": ctype, "size": size,
                "is_image": ctype.startswith("image/"),
            }
            if 0 < size <= _ATT_MAX and total + size <= _ATT_TOTAL:
                entry["data_uri"] = "data:%s;base64,%s" % (ctype, base64.b64encode(payload).decode())
                total += size
            else:
                entry["too_large"] = True
            atts.append(entry)
    return text, html, inline, atts


def message_view(uid: str, folder: str = "INBOX") -> dict:
    """Full message by uid, with rich HTML + inline images + attachments.
    Read-only (BODY.PEEK) — won't mark it seen."""
    cfg = _Cfg()
    if not cfg.imap_ready():
        return {"error": "Email isn't configured."}
    uid = (str(uid) or "").strip()
    if not uid.isdigit():
        return {"error": "Bad message id."}
    import email as _email
    M = None
    try:
        M = _imap()
        M.select(folder, readonly=True)
        typ, md = M.uid("FETCH", uid, "(BODY.PEEK[])")
        if not md or not md[0]:
            return {"error": "Message not found."}
        msg = _email.message_from_bytes(md[0][1])
        name, addr = parseaddr(_dec(msg.get("From", "")))
        text, html, inline, atts = _collect(msg)
        html = _inline_cids(html, inline)
        body = (text or "").strip()
        if not body and html:
            try:
                from modules.research import _html_to_text
                body = _html_to_text(html).strip()
            except Exception:
                body = ""
        if len(body) > 20000:
            body = body[:20000] + "\n… (truncated)"
        if html and len(html) > 600000:
            html = html[:600000]
        return {
            "uid": uid,
            "from_name": name or addr or _dec(msg.get("From", "")),
            "from_addr": addr,
            "to": _dec(msg.get("To", "")),
            "subject": _dec(msg.get("Subject", "(no subject)")),
            "date": _dec(msg.get("Date", ""))[:31],
            "body": body or "(no text content)",
            "html": html,
            "has_html": bool(html),
            "attachments": atts,
        }
    except Exception as e:
        logger.error(f"message_view error: {e}")
        return {"error": f"Couldn't read that email: {e}"}
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


def send_email(to: str, subject: str, body: str) -> str:
    cfg = _Cfg()
    if not cfg.smtp_ready():
        return "Email sending isn't configured. Set EMAIL_SMTP_HOST / EMAIL_USER / EMAIL_PASS in .env."
    import smtplib
    from email.message import EmailMessage
    try:
        msg = EmailMessage()
        msg["From"] = cfg.from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body or "")
        if cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
            server.starttls()
        server.login(cfg.user, cfg.password)
        server.send_message(msg)
        server.quit()
        return f"Sent to {to}: \"{subject}\"."
    except Exception as e:
        logger.error(f"send_email error: {e}")
        return f"Couldn't send: {e}"
