"""
modules/research.py — Web search and summarization.
Primary: DuckDuckGo DDGS. Secondary: opens browser for deep research.
"""

import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser

logger = logging.getLogger("miko.research")

# Reader fallback for bot-blocked pages: r.jina.ai renders JS + bypasses most 403s
# and returns clean text. Free, no key. Sends the URL to a third-party service, so it's
# toggleable (MIKO_RESEARCH_READER=0 disables). Only used when a direct fetch is blocked.
_READER_ENABLED = os.getenv("MIKO_RESEARCH_READER", "1").strip() not in ("0", "false", "no")

# The parallel fan-out can produce a burst of blocked links all hitting Jina at once,
# which itself trips Jina's free-tier rate limit. So GLOBALLY pace the reader: every
# failed link still goes through it, but as a steady queue — one new request per
# interval (default ~3s ≈ 20/min). MIKO_JINA_INTERVAL tunes it.
try:
    _JINA_MIN_INTERVAL = max(0.0, float(os.getenv("MIKO_JINA_INTERVAL", "3.0")))
except ValueError:
    _JINA_MIN_INTERVAL = 3.0
_JINA_LOCK = threading.Lock()
_jina_next_at = [0.0]


def _jina_throttle() -> None:
    """Reserve the next evenly-spaced Jina slot (paced across all threads), then wait
    for it OUTSIDE the lock so reserved requests still run concurrently."""
    with _JINA_LOCK:
        now = time.time()
        wait = max(0.0, _jina_next_at[0] - now)
        _jina_next_at[0] = now + wait + _JINA_MIN_INTERVAL
    if wait > 0:
        time.sleep(wait)


# DuckDuckGo search pacing — the exhaustive fan-out fires many searches at once and DDG
# rate-limits a burst (returning "No results found"), starving branches of sources. One
# search per interval keeps us under the limit. MIKO_SEARCH_INTERVAL tunes it.
try:
    _SEARCH_MIN_INTERVAL = max(0.0, float(os.getenv("MIKO_SEARCH_INTERVAL", "1.5")))
except ValueError:
    _SEARCH_MIN_INTERVAL = 1.5
_SEARCH_LOCK = threading.Lock()
_search_next_at = [0.0]


def _search_throttle() -> None:
    """Same evenly-spaced pacer as the reader, for web searches."""
    with _SEARCH_LOCK:
        now = time.time()
        wait = max(0.0, _search_next_at[0] - now)
        _search_next_at[0] = now + wait + _SEARCH_MIN_INTERVAL
    if wait > 0:
        time.sleep(wait)


TOOL_DECLARATIONS = [
    {
        "name": "web_search",
        "description": (
            "Caută informații pe web și returnează un rezumat. "
            "Folosește pentru 'caută', 'ce știi despre', 'informații despre', 'cine este', etc."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Interogarea de căutat pe web.",
                },
                "open_browser": {
                    "type": "BOOLEAN",
                    "description": "Dacă True, deschide și rezultatele în browser pentru cercetare aprofundată.",
                },
                "max_results": {
                    "type": "INTEGER",
                    "description": "Numărul maxim de rezultate de returnat (implicit 5).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "deep_research",
        "description": (
            "Run a thorough, multi-round research pipeline on a topic: it distills the "
            "subject, plans sub-questions, searches the web and reads sources in parallel "
            "across several rounds (finding and filling gaps), writes a CITED report, and "
            "SAVES it as a note in the vault. Use for 'research', 'deep dive', "
            "'investigate', 'find out everything about'. Returns a short summary to say "
            "back (the full cited report is in the vault)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "topic": {"type": "STRING", "description": "The research topic or question."},
                "effort": {
                    "type": "STRING",
                    "description": "Depth: 'quick', 'standard' (default), or 'deep' (more rounds/sources).",
                },
            },
            "required": ["topic"],
        },
    },
    {
        "name": "open_url",
        "description": (
            "Deschide un URL sau o adresă locală în browserul implicit al userului. "
            "Folosește ASTA ori de câte ori userul zice 'deschide <site/pagină/interfața ta web>' "
            "— inclusiv adrese localhost (ex: http://localhost:7832/chat). "
            "Doar lansează browserul — NU descarcă pagina și NU verifică dacă s-a încărcat, "
            "deci nu inventa erori de conexiune. Spune EXACT ce returnează unealta, nimic în plus."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "url": {"type": "STRING", "description": "URL-ul de deschis."}
            },
            "required": ["url"],
        },
    },
    {
        "name": "weather",
        "description": (
            "Afișează vremea curentă și prognoza pentru un oraș. "
            "Exemple: 'ce vreme e în București', 'temperatură Cluj', 'vreme afară'. "
            "Nu necesită API key."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {
                    "type": "STRING",
                    "description": "Numele orașului (ex: București, Cluj-Napoca, London). Opțional — fără oraș returnează vremea locală.",
                }
            },
        },
    },
    {
        "name": "get_network_info",
        "description": "Returnează informații despre rețea: IP local, IP public, hostname.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
]


def web_search(query: str, open_browser: bool = False, max_results: int = 5) -> str:
    """
    Search DuckDuckGo and return a formatted summary in Romanian.
    Falls back to a DuckDuckGo browser search if the library is unavailable.
    """
    if not query.strip():
        return "Nu mi-ai dat nicio interogare de căutat, sefu."

    if open_browser:
        _open_search_in_browser(query)

    try:
        return _ddg_search(query, max_results)
    except ImportError:
        logger.warning("duckduckgo-search not installed, opening browser only")
        if not open_browser:
            _open_search_in_browser(query)
        return f"Am deschis rezultatele pentru '{query}' în browser, sefu."
    except Exception as e:
        logger.error(f"Search error: {e}")
        if not open_browser:
            _open_search_in_browser(query)
        return f"Căutarea a eșuat ({e}), dar am deschis browserul, sefu."


def _ddg_search(query: str, max_results: int = 5) -> str:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append(r)

    if not results:
        return f"Nu am găsit nimic relevant pentru '{query}', sefu."

    lines = [f"Iată ce am găsit despre '{query}':\n"]
    for i, r in enumerate(results[:max_results], 1):
        title = r.get("title", "Fără titlu")
        body  = r.get("body", "")
        url   = r.get("href", "")
        snippet = body[:200].strip()
        if snippet and not snippet.endswith("."):
            snippet += "…"
        lines.append(f"{i}. **{title}**")
        if snippet:
            lines.append(f"   {snippet}")
        if url:
            lines.append(f"   {url}")

    return "\n".join(lines)


def _open_search_in_browser(query: str) -> None:
    encoded = urllib.parse.quote_plus(query)
    webbrowser.open(f"https://duckduckgo.com/?q={encoded}")


def deep_research(topic: str, effort: str = "standard") -> str:
    """Run the orchestrated deep-research pipeline and return a short spoken summary.
    The full cited report is saved to the vault by the pipeline. Synchronous."""
    topic = (topic or "").strip()
    if not topic:
        return "What should I research, sefu?"
    try:
        from config import CONFIG
        import deep_research as _dr
    except Exception as e:
        return f"Research is unavailable: {e}"

    report, note = "", ""
    try:
        for ev in _dr.run(
            topic, provider="gemini", model="gemini-2.5-flash",
            api_key=getattr(CONFIG, "gemini_api_key", ""),
            language=getattr(CONFIG, "language", "en"),
            effort=(effort or "standard"),
        ):
            t = ev.get("type")
            if t == "report":
                report, note = ev.get("reply", ""), ev.get("note", "")
            elif t == "error":
                return f"Research failed: {ev.get('error')}"
    except Exception as e:
        logger.error(f"deep_research tool error: {e}")
        return f"Research failed: {e}"

    if not report:
        return "I couldn't find enough to report on that, sefu."
    summary = report.strip().split("\n\n", 1)[0][:500]
    tail = " I saved the full report to your vault." if note else ""
    return summary + tail


def open_url(url: str) -> str:
    import os
    u = (url or "").strip().strip("<>\"'")
    if not u:
        return "Nu mi-ai dat niciun URL de deschis, sefu."
    # Add a scheme so the OS opens it as a URL (default browser), not a file path.
    if "://" not in u:
        u = "http://" + u
    try:
        if os.name == "nt":
            # Hands the URL to the default browser via the shell and RAISES if the
            # association/launch fails — so we never claim success falsely.
            os.startfile(u)
            return f"Am deschis {u} în browser, sefu."
        # POSIX: webbrowser.open returns False when no browser could be launched.
        if webbrowser.open(u):
            return f"Am deschis {u} în browser, sefu."
        return f"N-am putut deschide {u} — n-am găsit un browser disponibil, sefu."
    except Exception as e:
        return f"N-am putut deschide URL-ul {u}: {e}"


def weather(city: str = "") -> str:
    """Current weather via wttr.in — no API key, supports Romanian city names."""
    import urllib.request
    try:
        target = urllib.parse.quote_plus(city.strip()) if city.strip() else ""
        url = f"https://wttr.in/{target}?format=4&lang=ro"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip()
        if not data or "Unknown location" in data:
            # Retry without language param
            url2 = f"https://wttr.in/{target}?format=4"
            req2 = urllib.request.Request(url2, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req2, timeout=8) as resp2:
                data = resp2.read().decode("utf-8", errors="replace").strip()
        return data if data else f"Nu am găsit informații meteo pentru '{city}', sefu."
    except Exception as e:
        logger.error(f"Weather error: {e}")
        return f"N-am putut obține vremea: {e}"


def get_network_info() -> str:
    """Return local hostname, local IP, and public IP."""
    import socket
    import urllib.request
    lines = []
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        lines.append(f"Hostname: {hostname}")
        lines.append(f"IP local: {local_ip}")
    except Exception:
        lines.append("IP local: necunoscut")
    try:
        req = urllib.request.Request(
            "https://api.ipify.org", headers={"User-Agent": "Miko/2.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            lines.append(f"IP public: {resp.read().decode().strip()}")
    except Exception:
        lines.append("IP public: necunoscut (fără internet?)")
    return "\n".join(lines)


# ── Structured search + page fetching (used by the deep-research pipeline) ─────

def search_results(query: str, max_results: int = 6) -> list[dict]:
    """Return structured DuckDuckGo results: [{title, body, url}] (empty on error).
    Globally paced (see _search_throttle) so the fan-out's burst of searches doesn't
    trip DDG's rate limit, with one backoff-retry when a result comes back empty
    (almost always a transient rate-limit, not a genuinely empty query)."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    last_err = None
    for attempt in range(2):
        _search_throttle()
        try:
            out = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    out.append({
                        "title": (r.get("title") or "").strip(),
                        "body": (r.get("body") or "").strip(),
                        "url": (r.get("href") or r.get("url") or "").strip(),
                    })
            if out:
                return out
            last_err = "no results"
        except Exception as e:
            last_err = e
        if attempt == 0:
            time.sleep(4.0)   # DDG throttled the burst — back off, then retry once

    if last_err and str(last_err) != "no results":
        logger.warning(f"search_results error: {last_err}")
    return []


class _TextExtractor(HTMLParser):
    """Stdlib fallback HTML→text extractor (used when BeautifulSoup is missing)."""
    _SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            t = data.strip()
            if t:
                self.parts.append(t)


def _html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "head",
                         "nav", "footer", "header", "form", "aside"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.body or soup
        return main.get_text("\n")
    except Exception:
        p = _TextExtractor()
        try:
            p.feed(html)
        except Exception:
            pass
        return "\n".join(p.parts)


# A real, current browser fingerprint. The old "MikoResearch/1.0" UA was an obvious
# bot string that Cloudflare and most CMSs reject with 403 — which starved research of
# actual page text (it fell back to search snippets). A realistic UA + headers recovers
# a large share of those reads.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",   # we don't decompress gzip ourselves
    "Connection": "close",
}


def _pdf_to_text(data: bytes, max_chars: int) -> str:
    """Extract text from a PDF (books/papers found via filetype:pdf). Capped by pages
    and chars so a 600-page book doesn't blow up the run. No-op if pypdf is absent."""
    try:
        import io
        import pypdf
        logging.getLogger("pypdf").setLevel(logging.ERROR)   # silence object-pointer noise
    except Exception:
        logger.warning("pypdf not installed — skipping PDF")
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        out, total = [], 0
        for page in reader.pages[:40]:        # cap pages — books can be enormous
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t:
                out.append(t)
                total += len(t)
                if total >= max_chars * 2:
                    break
        text = re.sub(r"[ \t]+", " ", "\n".join(out))
        text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()
        return text[:max_chars]
    except Exception as e:
        logger.warning(f"pdf parse failed: {e}")
        return ""


def _direct_fetch(url: str, max_chars: int, timeout: int):
    """Direct urllib fetch. Returns (text, recoverable): recoverable=True means a reader
    fallback may still help (403 block / SSL / timeout / JS-rendered empty); False means
    it's a dead end (404/410/DNS or a successfully-read page)."""
    import time
    is_pdf_url = url.lower().split("?")[0].split("#")[0].endswith(".pdf")
    data, ctype = None, ""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ctype = resp.headers.get("Content-Type", "").lower()
                is_pdf = is_pdf_url or "application/pdf" in ctype
                if not is_pdf and ctype and "html" not in ctype and "text" not in ctype:
                    return "", False   # images/video — a reader won't help
                data = resp.read(12_000_000 if is_pdf else 3_000_000)
            break
        except Exception as e:
            if getattr(e, "code", None) == 429 and attempt == 0:
                time.sleep(2.0)   # rate-limited — brief backoff, one retry
                continue
            code = getattr(e, "code", None)
            msg = str(e).lower()
            dead = code in (404, 410) or "getaddrinfo failed" in msg or "name or service" in msg
            logger.warning(f"fetch_text {url}: {e}")
            return "", (not dead)
    if data is None:
        return "", True

    if is_pdf_url or "application/pdf" in ctype:
        return _pdf_to_text(data, max_chars), False   # PDFs go through pypdf, not the reader

    charset = "utf-8"
    if "charset=" in ctype:
        charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    text = _html_to_text(data.decode(charset, errors="replace"))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()[:max_chars]
    return text, (len(text) < 200)        # thin/empty (likely JS-rendered) → reader may help


def _jina_fetch(url: str, max_chars: int, timeout: int) -> str:
    """Fallback reader (r.jina.ai): renders JS + bypasses most bot blocks, returns clean
    text. Free, no key. Globally paced (see _jina_throttle) so the parallel fan-out's
    blocked links queue instead of bursting. NOTE: sends the URL to a third party."""
    for attempt in range(2):
        _jina_throttle()
        try:
            req = urllib.request.Request(
                "https://r.jina.ai/" + url,
                headers={"User-Agent": _BROWSER_HEADERS["User-Agent"],
                         "Accept": "text/plain", "X-Return-Format": "text"})
            with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
                raw = resp.read(3_000_000)
            text = re.sub(r"[ \t]+", " ", raw.decode("utf-8", errors="replace"))
            text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()
            return text[:max_chars]
        except Exception as e:
            if getattr(e, "code", None) == 429 and attempt == 0:
                time.sleep(10.0)   # paced but still overshot — back off harder, retry once
                continue
            logger.warning(f"jina reader {url}: {e}")
            return ""
    return ""


def fetch_text(url: str, max_chars: int = 4000, timeout: int = 12) -> str:
    """Readable plain text for a URL (best-effort, length-capped). Tries a direct fetch
    (browser UA, PDF-aware); if that's blocked/thin and a reader fallback is enabled,
    retries through r.jina.ai. Dead links (404/DNS) skip the fallback."""
    text, recoverable = _direct_fetch(url, max_chars, timeout)
    if text and len(text) >= 200:
        return text
    if recoverable and _READER_ENABLED:
        jt = _jina_fetch(url, max_chars, timeout)
        if jt and len(jt) >= 200:
            return jt
    return text
