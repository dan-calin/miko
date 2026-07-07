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
            "unread_only=true for just unread. Keep the user-facing reply compact: "
            "usually one best match or up to three short matches unless asked for more."
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
        "description": (
            "Read the full body of an email matched by a subject keyword or sender. "
            "Returns text to Miko; if the user asks to show/open it on their screen, "
            "use show_email instead."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "A keyword from the subject or the sender."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "show_email",
        "description": (
            "Find an email by subject/sender/body keyword and open it in the user's "
            "visible desktop browser. For Gmail mailboxes this opens the matching "
            "Gmail conversation link when possible, and also renders a local HTML "
            "fallback viewer. Use this when the user says 'show me that email', "
            "'open the email on my screen', or wants to see email details themselves. "
            "This does NOT use Miko's hidden automation browser. After calling this, "
            "only confirm it opened; do not summarize the email body unless asked."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "A keyword from the subject, sender, or body."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "open_email_link",
        "description": (
            "Find an email by subject/sender/body keyword, inspect its original HTML, "
            "and open a matching button/link from inside it in the user's visible browser. "
            "Use this when the user says 'show me the post', 'open the View Message button', "
            "'open the link from that email', 'view the Nextdoor post', or wants the website "
            "behind an email notification rather than the email itself. For link_text use "
            "phrases like 'View Message', 'View Post', 'See more', 'Apply', or 'Reply'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Keyword to identify the email: subject, sender, or body text."},
                "link_text": {"type": "STRING", "description": "Optional visible link/button text to prefer, e.g. View Post."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_emails",
        "description": (
            "Search the mailbox for emails matching keywords (subject/body/sender). "
            "Keep the user-facing reply compact: one best match or up to three short matches."
        ),
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
    # A timeout so a stale connection can't hang the watch/poll daemon forever
    # (which would silently stop all polling until the next restart).
    try:
        M = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=30)
    except TypeError:               # Python < 3.9 has no timeout kwarg
        M = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    M.login(cfg.user, cfg.password)
    return M


def list_emails(limit: int = 10, unread_only: bool = False, folder: str = "INBOX") -> str:
    cfg = _Cfg()
    if not cfg.imap_ready():
        return "Email isn't configured. Set EMAIL_USER / EMAIL_PASS / EMAIL_IMAP_HOST in .env."
    import email as _email
    M = None
    try:
        M = _imap()
        M.select(folder, readonly=True)
        typ, data = M.search(None, "UNSEEN" if unread_only else "ALL")
        ids = data[0].split()[-int(limit or 10):][::-1]
        if not ids:
            return "No emails found." if not unread_only else "No unread emails."
        lines = [f"{'Unread' if unread_only else 'Recent'} emails:"]
        for n, i in enumerate(ids, 1):
            typ, md = M.fetch(i, "(X-GM-THRID X-GM-MSGID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            meta, hdr_bytes = _split_fetch_response(md)
            hdr = _email.message_from_bytes(hdr_bytes)
            frm = parseaddr(_dec(hdr.get("From", "")))[1] or _dec(hdr.get("From", ""))
            subj = _dec(hdr.get("Subject", "(no subject)"))
            gmail_url = _gmail_message_url(hdr, _parse_gmail_fetch_attrs(meta))
            date = _dec(hdr.get("Date", ""))[:25]
            if gmail_url:
                lines.append(f"{n}. {subj} - {frm} ({date})")
                lines.append(f"   Gmail: {gmail_url}")
                continue
            lines.append(f"{n}. {subj} — {frm} ({date})")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"list_emails error: {e}")
        return f"Couldn't reach the mailbox: {e}"
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


def read_email(query: str) -> str:
    cfg = _Cfg()
    if not cfg.imap_ready():
        return "Email isn't configured."
    import email as _email
    M = None
    try:
        M = _imap()
        M.select("INBOX", readonly=True)
        typ, data = M.search(None, "TEXT", f'"{query}"')
        ids = data[0].split()
        if not ids:
            typ, data = M.search(None, "FROM", f'"{query}"')
            ids = data[0].split()
        if not ids:
            return f"No email matching '{query}'."
        typ, md = M.fetch(ids[-1], "(X-GM-THRID X-GM-MSGID RFC822)")
        meta, msg_bytes = _split_fetch_response(md)
        msg = _email.message_from_bytes(msg_bytes)
        gmail_ids = _parse_gmail_fetch_attrs(meta)
        frm = _dec(msg.get("From", ""))
        subj = _dec(msg.get("Subject", "(no subject)"))
        body = _body_text(msg).strip()
        gmail_url = _gmail_message_url(msg, gmail_ids)
        if gmail_url:
            body = f"Gmail: {gmail_url}\n\n{body}"
        if len(body) > 2000:
            body = body[:2000] + "\n… (truncated)"
        return f"From: {frm}\nSubject: {subj}\n\n{body}"
    except Exception as e:
        logger.error(f"read_email error: {e}")
        return f"Couldn't read that email: {e}"
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


def _find_email_message(query: str):
    """Return (message, sequence_id, gmail_ids) for the newest email matching query."""
    import email as _email

    q = (query or "").strip()
    if not q:
        return None, None, {}

    M = _imap()
    try:
        M.select("INBOX", readonly=True)
        ids = []
        for field in ("TEXT", "FROM", "SUBJECT"):
            try:
                typ, data = M.search(None, field, f'"{q}"')
                ids = data[0].split() if data and data[0] else []
            except Exception:
                ids = []
            if ids:
                break
        if not ids:
            return None, None, {}
        seq_id = ids[-1]
        typ, md = M.fetch(seq_id, "(X-GM-THRID X-GM-MSGID BODY.PEEK[])")
        if not md or not md[0]:
            return None, None, {}
        meta, msg_bytes = _split_fetch_response(md)
        return _email.message_from_bytes(msg_bytes), seq_id, _parse_gmail_fetch_attrs(meta)
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _split_fetch_response(md) -> tuple[bytes, bytes]:
    """Return (metadata, message/header bytes) from imaplib's mixed FETCH response."""
    meta = b""
    payload = b""
    for part in md or []:
        if isinstance(part, tuple):
            meta += part[0] or b""
            payload = part[1] or payload
        elif isinstance(part, (bytes, bytearray)):
            meta += bytes(part)
    return meta, payload


def _parse_gmail_fetch_attrs(meta: bytes) -> dict:
    """Parse Gmail IMAP extension attrs from a FETCH metadata blob."""
    text = (meta or b"").decode("ascii", "ignore")
    out = {}
    for key in ("X-GM-THRID", "X-GM-MSGID"):
        m = re.search(rf"\b{key}\s+(\d+)", text, flags=re.IGNORECASE)
        if m:
            out[key.lower().replace("x-gm-", "")] = m.group(1)
    return out


def _gmail_search_url(msg) -> str:
    import urllib.parse

    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip().strip("<>")
    if message_id:
        query = f"rfc822msgid:{message_id}"
    else:
        subject = _dec(msg.get("Subject", ""))
        _, addr = parseaddr(_dec(msg.get("From", "")))
        query = " ".join(x for x in (f'from:{addr}' if addr else "", subject) if x)
    return "https://mail.google.com/mail/u/0/#search/" + urllib.parse.quote(query, safe="")


def _gmail_thread_url(gmail_ids: dict, label: str = "inbox") -> str:
    try:
        thrid = int((gmail_ids or {}).get("thrid") or 0)
    except (TypeError, ValueError):
        thrid = 0
    if not thrid:
        return ""
    return f"https://mail.google.com/mail/u/0/#{label}/{thrid:x}"


def _gmail_message_url(msg, gmail_ids: dict | None = None) -> str:
    return _gmail_thread_url(gmail_ids or {}) or _gmail_search_url(msg)


def _norm_link_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _extract_email_links(msg) -> list[dict]:
    """Extract visible links/buttons from the original HTML part of an email."""
    from html.parser import HTMLParser
    from urllib.parse import urljoin

    _text, html, _inline, _atts = _collect(msg)
    links: list[dict] = []

    class LinkParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.current = None

        def handle_starttag(self, tag, attrs):
            attr = {k.lower(): (v or "") for k, v in attrs}
            if tag.lower() == "a" and attr.get("href"):
                self.current = {"url": attr["href"].strip(), "parts": []}
            elif tag.lower() == "img" and self.current and attr.get("alt"):
                self.current["parts"].append(attr["alt"])

        def handle_data(self, data):
            if self.current and data:
                self.current["parts"].append(data)

        def handle_endtag(self, tag):
            if tag.lower() == "a" and self.current:
                url = (self.current.get("url") or "").strip()
                label = _norm_link_text(" ".join(self.current.get("parts") or []))
                if url and not url.lower().startswith(("mailto:", "tel:", "cid:")):
                    links.append({"label": label or url, "url": urljoin("https://mail.google.com/", url)})
                self.current = None

    if html:
        try:
            p = LinkParser()
            p.feed(html)
        except Exception:
            pass

    # Some transactional email HTML is malformed enough that HTMLParser misses hrefs.
    seen = {x["url"] for x in links}
    for href in re.findall(r'href\s*=\s*["\']([^"\']+)["\']', html or "", flags=re.IGNORECASE):
        href = href.strip()
        if href and href not in seen and not href.lower().startswith(("mailto:", "tel:", "cid:")):
            links.append({"label": href, "url": href})
            seen.add(href)
    return links


def _score_email_link(link: dict, wanted: str = "") -> int:
    label = _norm_link_text(link.get("label", ""))
    url = (link.get("url") or "").lower()
    hay = f"{label} {url}"
    if not url.startswith(("http://", "https://")):
        return -1000
    bad = (
        "unsubscribe", "manage preferences", "email preferences", "notification settings",
        "privacy", "terms", "help center", "view in browser", "view online",
    )
    if any(b in hay for b in bad):
        return -200

    score = 0
    wanted = _norm_link_text(wanted)
    aliases = {
        "post": ("view post", "post", "see more", "read more", "open post"),
        "message": ("view message", "message", "reply", "respond"),
        "reply": ("reply", "respond", "view message"),
        "apply": ("apply", "view job", "job", "application"),
    }
    wanted_terms = aliases.get(wanted, (wanted,)) if wanted else ()
    if wanted_terms:
        for term in wanted_terms:
            if term and term in hay:
                score += 100
    else:
        for term in ("view message", "view post", "see more", "read more", "reply", "apply", "view job", "open"):
            if term in label:
                score += 50

    if label in ("view message", "view post", "see more", "read more", "reply", "apply"):
        score += 40
    if "button" in hay or any(x in label for x in ("view", "post", "message", "reply", "apply")):
        score += 10
    return score


def _pick_email_link(links: list[dict], link_text: str = "") -> dict | None:
    if not links:
        return None
    ranked = sorted(links, key=lambda x: _score_email_link(x, link_text), reverse=True)
    return ranked[0] if _score_email_link(ranked[0], link_text) > -100 else None


def _write_visible_email(msg, query: str, gmail_ids: dict | None = None) -> str:
    import html as _html
    from datetime import datetime
    from pathlib import Path
    from config import CONFIG

    CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
    out_dir = CONFIG.data_dir / "email_views"
    out_dir.mkdir(parents=True, exist_ok=True)

    name, addr = parseaddr(_dec(msg.get("From", "")))
    subject = _dec(msg.get("Subject", "(no subject)"))
    date = _dec(msg.get("Date", ""))
    to = _dec(msg.get("To", ""))
    body = _body_text(msg).strip() or "(no text content)"
    if len(body) > 50000:
        body = body[:50000] + "\n... (truncated)"

    attachments = []
    try:
        for part in msg.walk():
            filename = part.get_filename()
            if filename:
                attachments.append(_dec(filename))
    except Exception:
        attachments = []

    gmail_url = _gmail_thread_url(gmail_ids or {})
    gmail_search_url = _gmail_search_url(msg)
    safe_subject = re.sub(r"[^A-Za-z0-9._-]+", "_", subject).strip("_")[:60] or "email"
    path = out_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{safe_subject}.html"
    attachment_html = ""
    if attachments:
        items = "\n".join(f"<li>{_html.escape(a)}</li>" for a in attachments)
        attachment_html = f"<section><h2>Attachments</h2><ul>{items}</ul></section>"

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(subject)}</title>
<style>
  :root {{ color-scheme: light; font-family: Segoe UI, Arial, sans-serif; }}
  body {{ margin: 0; background: #f5f7fb; color: #111827; }}
  main {{ max-width: 980px; margin: 32px auto; background: #fff; border: 1px solid #d9dee8; border-radius: 8px; padding: 28px; }}
  h1 {{ font-size: 24px; line-height: 1.25; margin: 0 0 18px; }}
  h2 {{ font-size: 16px; margin: 24px 0 8px; }}
  .meta {{ display: grid; gap: 6px; color: #374151; font-size: 14px; border-bottom: 1px solid #e5e7eb; padding-bottom: 18px; }}
  .meta b {{ color: #111827; }}
  .body {{ white-space: pre-wrap; font-size: 16px; line-height: 1.55; margin-top: 22px; }}
  a {{ color: #0f62fe; }}
</style>
</head>
<body>
<main>
  <h1>{_html.escape(subject)}</h1>
  <div class="meta">
    <div><b>From:</b> {_html.escape(name or addr or _dec(msg.get("From", "")))} {_html.escape(f"<{addr}>" if addr else "")}</div>
    <div><b>To:</b> {_html.escape(to)}</div>
    <div><b>Date:</b> {_html.escape(date)}</div>
    <div><b>Matched query:</b> {_html.escape(query)}</div>
    {f'<div><b>Gmail:</b> <a href="{_html.escape(gmail_url)}">{_html.escape(gmail_url)}</a></div>' if gmail_url else ''}
    <div><b>Gmail search:</b> <a href="{_html.escape(gmail_search_url)}">{_html.escape(gmail_search_url)}</a></div>
  </div>
  <section class="body">{_html.escape(body)}</section>
  {attachment_html}
</main>
</body>
</html>
"""
    path.write_text(html_doc, encoding="utf-8")
    return str(path)


def show_email(query: str) -> str:
    cfg = _Cfg()
    if not cfg.imap_ready():
        return "Email isn't configured. Set EMAIL_USER / EMAIL_PASS / EMAIL_IMAP_HOST in .env."
    query = (query or "").strip()
    if not query:
        return "Which email should I show?"
    try:
        msg, _seq_id, gmail_ids = _find_email_message(query)
        if msg is None:
            return f"No email matching '{query}'."
        path = _write_visible_email(msg, query, gmail_ids)
        gmail_url = _gmail_message_url(msg, gmail_ids)
        opened = gmail_url or path
        try:
            if os.name == "nt":
                os.startfile(opened)
            else:
                import webbrowser
                from pathlib import Path
                webbrowser.open(opened if gmail_url else Path(path).as_uri())
        except Exception as e:
            return f"Rendered the email to {path}, but couldn't open it on screen: {e}"
        subj = _dec(msg.get("Subject", "(no subject)"))
        frm = _dec(msg.get("From", ""))
        if gmail_url:
            return (
                f"Opened on your screen: {subj} - {frm}\n"
                f"Gmail: {gmail_url}\nLocal viewer: {path}\n"
                "Reply guidance: only say that it is open on screen unless the user asks for details. "
                "If the user asks for a button/link/post/message from this email, call open_email_link "
                f"with query={subj!r}."
            )
        return (
            f"Opened on your screen: {subj} — {frm}\nLocal viewer: {path}\n"
            "Reply guidance: only say that it is open on screen unless the user asks for details. "
            "If the user asks for a button/link/post/message from this email, call open_email_link "
            f"with query={subj!r}."
        )
    except Exception as e:
        logger.error(f"show_email error: {e}")
        return f"Couldn't show that email: {e}"


def open_email_link(query: str, link_text: str = "") -> str:
    cfg = _Cfg()
    if not cfg.imap_ready():
        return "Email isn't configured. Set EMAIL_USER / EMAIL_PASS / EMAIL_IMAP_HOST in .env."
    query = (query or "").strip()
    link_text = (link_text or "").strip()
    if not query:
        return "Which email should I inspect for a link?"
    try:
        msg, _seq_id, _gmail_ids = _find_email_message(query)
        if msg is None:
            return f"No email matching '{query}'."
        links = _extract_email_links(msg)
        picked = _pick_email_link(links, link_text)
        if not picked:
            shown = []
            for link in links[:8]:
                label = (link.get("label") or link.get("url") or "").strip()
                if label:
                    shown.append(f"- {label[:80]}")
            suffix = "\nLinks I found:\n" + "\n".join(shown) if shown else ""
            return f"I couldn't find a matching link/button in that email.{suffix}"

        url = picked["url"]
        try:
            if os.name == "nt":
                os.startfile(url)
            else:
                import webbrowser
                webbrowser.open(url)
        except Exception as e:
            return f"Found the link, but couldn't open it: {e}\n{url}"

        subj = _dec(msg.get("Subject", "(no subject)"))
        label = picked.get("label") or link_text or "link"
        return (
            f"Opened email link on your screen: {label}\n"
            f"From email: {subj}\nURL: {url}\n"
            "Reply guidance: only say that it is open on screen unless the user asks for details."
        )
    except Exception as e:
        logger.error(f"open_email_link error: {e}")
        return f"Couldn't open a link from that email: {e}"


def search_emails(query: str) -> str:
    cfg = _Cfg()
    if not cfg.imap_ready():
        return "Email isn't configured."
    import email as _email
    M = None
    try:
        M = _imap()
        M.select("INBOX", readonly=True)
        typ, data = M.search(None, "TEXT", f'"{query}"')
        ids = data[0].split()[-10:][::-1]
        if not ids:
            return f"No emails matching '{query}'."
        lines = [f"Emails matching '{query}':"]
        for i in ids:
            typ, md = M.fetch(i, "(X-GM-THRID X-GM-MSGID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            meta, hdr_bytes = _split_fetch_response(md)
            hdr = _email.message_from_bytes(hdr_bytes)
            frm = parseaddr(_dec(hdr.get("From", "")))[1]
            lines.append(f"- {_dec(hdr.get('Subject', '(no subject)'))} — {frm}")
            gmail_url = _gmail_message_url(hdr, _parse_gmail_fetch_attrs(meta))
            if gmail_url:
                lines.append(f"  Gmail: {gmail_url}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"search_emails error: {e}")
        return f"Search failed: {e}"
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


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
            typ, md = M.uid("FETCH", uid, "(FLAGS X-GM-THRID X-GM-MSGID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            if not md:
                continue
            meta, hdr_bytes = _split_fetch_response(md)
            unread = "\\Seen" not in meta.decode("utf-8", "replace")
            hdr = _email.message_from_bytes(hdr_bytes)
            name, addr = parseaddr(_dec(hdr.get("From", "")))
            raw_date = _dec(hdr.get("Date", ""))
            gmail_ids = _parse_gmail_fetch_attrs(meta)
            msgs.append({
                "uid": uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid),
                "from_name": name or addr or _dec(hdr.get("From", "")),
                "from_addr": addr,
                "subject": _dec(hdr.get("Subject", "(no subject)")),
                "date": raw_date[:31],
                "ts": _parse_ts(hdr.get("Date", "")),
                "unread": unread,
                "gmail_url": _gmail_message_url(hdr, gmail_ids),
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
        typ, md = M.uid("FETCH", uid, "(X-GM-THRID X-GM-MSGID BODY.PEEK[])")
        if not md or not md[0]:
            return {"error": "Message not found."}
        meta, msg_bytes = _split_fetch_response(md)
        msg = _email.message_from_bytes(msg_bytes)
        gmail_ids = _parse_gmail_fetch_attrs(meta)
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
            "gmail_url": _gmail_message_url(msg, gmail_ids),
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
