"""
modules/research.py — Web search and summarization.
Primary: DuckDuckGo DDGS. Secondary: opens browser for deep research.
"""

import logging
import webbrowser
import urllib.parse

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
