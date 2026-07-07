"""
modules/browser.py — real browser automation for Miko (Playwright).

Lets Miko DRIVE a browser — open pages, click, type, extract text, screenshot —
not just fetch HTML. A single persistent headless Chromium is owned by one
dedicated worker thread (Playwright's sync API is thread-bound); tool calls are
queued to it and serialized, so calls from the chat threadpool / voice loop are
safe. Degrades gracefully if Playwright isn't installed.

Setup: pip install playwright  &&  python -m playwright install chromium
"""

import logging
import queue
import threading

logger = logging.getLogger("miko.browser")

TOOL_DECLARATIONS = [
    {
        "name": "browser_open",
        "description": (
            "Open a URL in Miko's hidden automation browser and return the page title + visible "
            "text. Use to start driving a site (logging in, filling forms, multi-step flows) "
            "when Miko needs to inspect or interact with it herself. The user cannot see this "
            "browser. If the user asks to show/open something on their screen, use open_url "
            "or show_email instead."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {"url": {"type": "STRING", "description": "The URL to open."}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_click",
        "description": "Click an element on the current page by its visible text or a CSS selector.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"target": {"type": "STRING", "description": "Visible text or CSS selector."}},
            "required": ["target"],
        },
    },
    {
        "name": "browser_type",
        "description": "Type text into an input on the current page (by label text or CSS selector).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "field": {"type": "STRING", "description": "Field label or CSS selector."},
                "text": {"type": "STRING", "description": "Text to type."},
                "submit": {"type": "BOOLEAN", "description": "Press Enter after typing (default false)."},
            },
            "required": ["field", "text"],
        },
    },
    {
        "name": "browser_extract",
        "description": "Return the readable text of the current page (for reading/scraping).",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "browser_screenshot",
        "description": "Screenshot the current page and save it; returns the file path.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
]


class _Worker:
    """Owns the Playwright browser in one thread; runs queued callables on the page."""

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._err = None
        self._pw = self._browser = self._page = None
        threading.Thread(target=self._run, daemon=True, name="Browser").start()

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page()
        except Exception as e:
            self._err = e
            self._ready.set()
            logger.warning(f"browser unavailable: {e}")
            return
        self._ready.set()
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, box, done = item
            try:
                box["result"] = fn(self._page)
            except Exception as e:
                box["error"] = str(e)
            done.set()

    def call(self, fn, timeout: int = 60):
        self._ready.wait(40)
        if self._err:
            return ("Browser automation needs Playwright: "
                    "`pip install playwright` then `python -m playwright install chromium`.")
        box, done = {}, threading.Event()
        self._q.put((fn, box, done))
        if not done.wait(timeout):
            return "Browser action timed out."
        if "error" in box:
            return f"Browser error: {box['error']}"
        return box.get("result")


_worker = None
_lock = threading.Lock()


def _w() -> _Worker:
    global _worker
    with _lock:
        if _worker is None:
            _worker = _Worker()
    return _worker


# ── Tools ─────────────────────────────────────────────────────────────────────

def browser_open(url: str) -> str:
    def fn(page):
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        title = page.title()
        text = (page.inner_text("body") or "")[:1500]
        return f"Opened: {title or url}\nURL: {page.url}\n\n{text}"
    return _w().call(fn)


def browser_click(target: str) -> str:
    def fn(page):
        try:
            page.get_by_text(target, exact=False).first.click(timeout=8000)
        except Exception:
            page.click(target, timeout=8000)
        page.wait_for_timeout(600)
        return f"Clicked '{target}'. Now at: {page.url}"
    return _w().call(fn)


def browser_type(field: str, text: str, submit: bool = False) -> str:
    def fn(page):
        try:
            loc = page.get_by_label(field).first
            loc.fill(text, timeout=8000)
        except Exception:
            loc = page.locator(field)
            loc.fill(text, timeout=8000)
        if submit:
            loc.press("Enter")
            page.wait_for_timeout(800)
        return f"Typed into '{field}'" + (" and submitted." if submit else ".")
    return _w().call(fn)


def browser_extract() -> str:
    def fn(page):
        return (page.inner_text("body") or "")[:4000]
    return _w().call(fn)


def browser_screenshot() -> str:
    def fn(page):
        from config import CONFIG
        from datetime import datetime
        CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
        path = CONFIG.data_dir / f"browser_{datetime.now():%Y%m%d_%H%M%S}.png"
        page.screenshot(path=str(path))
        return str(path)
    return _w().call(fn)
