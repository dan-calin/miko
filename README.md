# Miko — Voice AI Agent

A personal voice assistant for Windows 11, powered by **Google Gemini Live**.
Speak naturally and Miko responds instantly and executes commands across your
PC, Discord, the web, and your files. Miko understands both **English and
Romanian** — if you speak English, it replies in English.

> A modern take on JARVIS: loyal, direct, fast, with a bit of humor.

---

## Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Discord bot setup](#discord-bot-setup)
4. [First run](#first-run)
5. [Voice commands](#voice-commands)
6. [Architecture](#architecture)
7. [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Requirement | Minimum version | Note |
|-------------|-----------------|------|
| Python | 3.11+ | [python.org](https://python.org) |
| FFmpeg | any stable build | Must be on your `PATH` |
| Google Gemini API key | — | [aistudio.google.com](https://aistudio.google.com/apikey) |
| Windows 11 | — | Windows-only (uses `pycaw`, `winsound`) |
| Microphone + speakers | — | Required for voice I/O |

### Installing FFmpeg

1. Download from [ffmpeg.org/download.html](https://ffmpeg.org/download.html) (Windows builds).
2. Extract the archive (e.g. `C:\ffmpeg\`).
3. Add `C:\ffmpeg\bin` to your system `PATH`.
4. Verify with `ffmpeg -version` in a terminal.

---

## Installation

```bash
# 1. Clone or download the project
cd "C:\Users\YourName\Desktop\Jarvis V2"

# 2. Create a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
copy .env.example .env
notepad .env
```

Edit `.env` and fill in at least `LLM_API_KEY`. See `.env.example` for every
supported setting.

---

## Discord bot setup

Only needed if you want the Discord features (music in a voice channel, DMs,
notifications):

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications).
2. Click **New Application** and give it a name (e.g. "Miko").
3. Open **Bot → Add Bot** and confirm.
4. Under **Token**, click **Reset Token** and copy it into `.env` as `DISCORD_TOKEN`.
5. Under **Privileged Gateway Intents**, enable:
   - ✅ Server Members Intent
   - ✅ Message Content Intent
   - ✅ Presence Intent
6. Under **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot permissions: `Send Messages`, `Read Message History`, `Connect`, `Speak`, `Use Voice Activity`
7. Open the generated URL in a browser and add the bot to your server.
8. Copy your server ID into `.env` as `DISCORD_GUILD_ID`.
   (Enable Developer Mode in Discord → right-click the server → Copy Server ID.)

---

## First run

```bash
# Activate the virtualenv if it isn't already
venv\Scripts\activate

# Start Miko
python main.py
```

On first launch:

- A **double beep** plays once the connection to Gemini Live is established.
- Miko announces that it's **connected and listening**.
- File indexing runs in the background (2–5 minutes depending on your disk).
- Miko starts in **ACTIVE** mode — every sentence you speak is processed.

---

## Voice commands

> The examples below are in English. The equivalent Romanian phrasing works too.

### Modes

| Voice command | Effect |
|---------------|--------|
| `Miko, go to standby` | Enter STANDBY — only responds to the wake word "Miko" |
| `Miko, wake up` | Return to ACTIVE mode |
| `Miko, enter conversation mode` | AUTO mode — responds naturally to everything |
| `Miko, stop conversation mode` | Leave AUTO mode |

### System control

| Voice command | Effect |
|---------------|--------|
| `Miko, open Chrome` | Launch an application |
| `Miko, open Documents` | Open the Documents folder |
| `Miko, take a screenshot` | Save a screenshot to the Desktop |
| `Miko, lock the screen` | Lock Windows |
| `Miko, what's my system status?` | CPU, RAM, battery, disk |
| `Miko, set a reminder in 10 minutes — call John` | Reminder with a notification |

### Volume & media

| Voice command | Effect |
|---------------|--------|
| `Miko, set volume to 50` | Set volume to 50% |
| `Miko, turn it up` | Volume up ×3 |
| `Miko, turn it down` | Volume down ×3 |
| `Miko, mute` | Mute |
| `Miko, play / pause / next` | System media control |

### Music on Discord (voice channel)

| Voice command | Effect |
|---------------|--------|
| `Miko, play Linkin Park on Discord` | Search and play in the voice channel |
| `Miko, queue up Eminem` | Add to the playlist |
| `Miko, skip` / `Miko, next` | Skip the current track |
| `Miko, pause` / `Miko, resume` | Pause / resume |
| `Miko, stop the music` | Stop and clear the queue |
| `Miko, what's up next?` | Show the queue |
| `Miko, join voice` | Join your voice channel |
| `Miko, leave voice` | Disconnect |

### Web research

| Voice command | Effect |
|---------------|--------|
| `Miko, find the best restaurants in town` | DuckDuckGo search + summary |
| `Miko, what do you know about artificial intelligence?` | Search + summary |
| `Miko, open Google in the browser` | Open a URL |

### Notes

| Voice command | Effect |
|---------------|--------|
| `Miko, note that I have a meeting tomorrow at 2 PM` | Create a note |
| `Miko, read today's note` | Read the latest note from today |
| `Miko, show me recent notes` | List notes |
| `Miko, search notes for: project` | Search within notes |

### Files

| Voice command | Effect |
|---------------|--------|
| `Miko, find the file budget.xlsx` | Search the SQLite index |
| `Miko, where's my CV document?` | Search PDFs / docx |
| `Miko, list the files in Documents` | Folder contents |
| `Miko, rebuild the file index` | Full re-index |

### Discord messaging

| Voice command | Effect |
|---------------|--------|
| `Miko, send John a message: "I'll be there at 8"` | DM with confirmation |
| `Miko, read my Discord messages` | Latest DMs |
| `Miko, invite John to voice` | Invitation + join |

---

## Architecture

```
main.py
├── AudioHandler (Gemini Live WebSocket)
│   ├── Microphone capture (sounddevice, 16 kHz)
│   ├── Audio playback (sounddevice, 24 kHz)
│   ├── ModeManager (ACTIVE / STANDBY / AUTO filtering)
│   └── CommandRouter (tool dispatch + safety)
├── DiscordBot thread (own asyncio loop)
├── DiscordPoll thread (checks DMs every 2s)
├── FileIndexer thread (SQLite, full + incremental)
└── MemoryExtractor thread (every 5 turns, extracts facts from speech)
```

Miko also ships a **FastAPI tool server** (`tool_server.py`) that exposes all of
Miko's tools over HTTP so external agents can call them. It serves tool schemas
in OpenAI / Anthropic / Gemini formats and guards execution with an optional
bearer token (`TOOL_SERVER_KEY`) and a confirmation gate for destructive actions.

---

## Troubleshooting

### Miko doesn't respond to voice
- Check the microphone under Settings → Sound → Input.
- Make sure `sounddevice` has microphone access.
- Confirm `LLM_API_KEY` is correct in `.env`.

### `No module named 'pycaw'`
```bash
pip install pycaw comtypes
```

### Discord bot won't connect
- Check `DISCORD_TOKEN` in `.env`.
- Make sure the Privileged Intents are enabled in the Developer Portal.
- The bot must be a member of the server set in `DISCORD_GUILD_ID`.

### Discord music doesn't work
- Verify FFmpeg is installed and on PATH: `ffmpeg -version`.
- Make sure you're in a voice channel before issuing the command.
- Confirm `PyNaCl` is installed: `pip install PyNaCl`.

### `UnicodeEncodeError` in the console
- Run in Windows Terminal (not the classic CMD).
- Or set `PYTHONIOENCODING=utf-8` in your system variables.

### File indexing is slow
- Normal on first run — it can take 3–10 minutes on large disks.
- After that it runs incrementally (every 30 min) and is fast.
