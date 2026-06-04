"""
modules/research.py — Web search and summarization.
Primary: DuckDuckGo DDGS. Secondary: opens browser for deep research.
"""

import logging
import re
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser

logger = logging.getLogger("miko.research")

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
        "name": "open_url",
        "description": "Deschide un URL în browserul implicit.",
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


def open_url(url: str) -> str:
    try:
        webbrowser.open(url)
        return f"Am deschis {url} în browser, sefu."
    except Exception as e:
        return f"N-am putut deschide URL-ul: {e}"


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
    """Return structured DuckDuckGo results: [{title, body, url}] (empty on error)."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    out = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                out.append({
                    "title": (r.get("title") or "").strip(),
                    "body": (r.get("body") or "").strip(),
                    "url": (r.get("href") or r.get("url") or "").strip(),
                })
    except Exception as e:
        logger.warning(f"search_results error: {e}")
    return out


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


def fetch_text(url: str, max_chars: int = 4000, timeout: int = 12) -> str:
    """Download a page and return readable plain text (best-effort, length-capped)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; MikoResearch/1.0)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "").lower()
            if ctype and "html" not in ctype and "text" not in ctype:
                return ""   # skip PDFs, images, etc.
            raw = resp.read(3_000_000)
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        html = raw.decode(charset, errors="replace")
    except Exception as e:
        logger.warning(f"fetch_text {url}: {e}")
        return ""

    text = _html_to_text(html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()
    return text[:max_chars]
