"""
modules/youtube_player.py — Browser-side YouTube playback and video info.
For in-Discord-voice streaming, see modules/discord_bot.py → stream_youtube_on_voice().
"""

import logging
import webbrowser
import urllib.parse

logger = logging.getLogger("miko.youtube")

TOOL_DECLARATIONS = [
    {
        "name": "play_youtube",
        "description": (
            "Deschide YouTube în browser și redă un videoclip sau o melodie. "
            "Folosește pentru 'deschide pe YouTube', 'pune videoclipul X', 'ascultă Y pe YouTube'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Numele melodiei, artistului, sau URL-ul YouTube.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_video_info",
        "description": "Obține informații despre un videoclip YouTube: titlu, durată, canal, vizualizări.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Numele videoclipului sau URL-ul YouTube.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_trending",
        "description": "Returnează top videoclipuri trending pe YouTube pentru o regiune.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "region": {
                    "type": "STRING",
                    "description": "Codul de țară (ex: RO, US, GB). Default: RO.",
                }
            },
        },
    },
]


def play_in_browser(query: str) -> str:
    """Search YouTube and open the top result in the default browser."""
    if not query.strip():
        return "Spune-mi ce melodie sau videoclip să caut, sefu."

    # If it's already a URL, open directly
    if query.startswith(("http://", "https://", "www.")):
        webbrowser.open(query)
        return f"Am deschis YouTube: {query}"

    # Use yt-dlp to get the top result URL
    try:
        url, title = _extract_top_url(query)
        webbrowser.open(url)
        return f"Am deschis '{title}' pe YouTube în browser, sefu."
    except ImportError:
        # Fallback: open a YouTube search
        encoded = urllib.parse.quote_plus(query)
        url     = f"https://www.youtube.com/results?search_query={encoded}"
        webbrowser.open(url)
        return f"Am deschis căutarea YouTube pentru '{query}', sefu."
    except Exception as e:
        logger.error(f"play_in_browser error: {e}")
        encoded = urllib.parse.quote_plus(query)
        webbrowser.open(f"https://www.youtube.com/results?search_query={encoded}")
        return f"Am deschis căutarea YouTube pentru '{query}', sefu."


def get_video_info(query: str) -> str:
    """Return metadata for a YouTube video or search result."""
    try:
        import yt_dlp

        opts = {
            "quiet": True,
            "noplaylist": True,
            "default_search": "ytsearch1",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            q = query if query.startswith("http") else f"ytsearch1:{query}"
            info = ydl.extract_info(q, download=False)

            if "entries" in info:
                info = info["entries"][0]

            title    = info.get("title", "Necunoscut")
            channel  = info.get("channel", info.get("uploader", "Necunoscut"))
            duration = info.get("duration")
            views    = info.get("view_count")
            url      = info.get("webpage_url", "")

            dur_str   = _fmt_duration(duration) if duration else "N/A"
            views_str = f"{views:,}".replace(",", ".") if views else "N/A"

            return (
                f"Videoclip: {title}\n"
                f"Canal: {channel}\n"
                f"Durată: {dur_str}\n"
                f"Vizualizări: {views_str}\n"
                f"URL: {url}"
            )
    except ImportError:
        return "yt-dlp nu este instalat. Rulează: pip install yt-dlp"
    except Exception as e:
        return f"N-am putut obține informații: {e}"


def get_trending(region: str = "RO") -> str:
    """Return top trending YouTube videos for a region."""
    try:
        import yt_dlp

        opts = {
            "quiet": True,
            "extract_flat": True,
            "playlistend": 10,
        }
        url = f"https://www.youtube.com/feed/trending?gl={region.upper()}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        entries = info.get("entries", [])
        if not entries:
            return f"Nu am putut obține trending pentru {region}, sefu."

        lines = [f"Top trending YouTube ({region.upper()}):"]
        for i, entry in enumerate(entries[:10], 1):
            title   = entry.get("title", "Fără titlu")
            channel = entry.get("channel", entry.get("uploader", ""))
            lines.append(f"{i}. {title}" + (f" — {channel}" if channel else ""))

        return "\n".join(lines)
    except ImportError:
        return "yt-dlp nu este instalat. Rulează: pip install yt-dlp"
    except Exception as e:
        return f"N-am putut obține trending: {e}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_top_url(query: str) -> tuple[str, str]:
    import yt_dlp

    opts = {
        "quiet": True,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "default_search": "ytsearch1",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        q    = f"ytsearch1:{query}"
        info = ydl.extract_info(q, download=False)

        if "entries" in info and info["entries"]:
            e     = info["entries"][0]
            vid   = e.get("url") or e.get("id", "")
            title = e.get("title", query)
            if not vid.startswith("http"):
                vid = f"https://www.youtube.com/watch?v={vid}"
            return vid, title

        url   = info.get("webpage_url", "")
        title = info.get("title", query)
        return url, title


def _fmt_duration(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"
