# Changelog

All notable changes to Miko are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Web Chat UI — "Miko as a tool inside a chat app"** (`chat_backend.py`,
  `webui/chat.html`, routes in `tool_server.py`). A ChatGPT-style page served at
  `GET /chat` where you type to Miko with her full tool set available to the model.
  - Model-agnostic backend with three wire protocols (gemini / anthropic / openai)
    covering **Google Gemini, MiniMax, OpenAI, DeepSeek, Kimi (Moonshot)**, and a
    **custom** OpenAI-compatible endpoint (LM Studio, Ollama, …).
  - Per-session history, provider/model/key picked in the UI (keys persisted to the
    browser's localStorage) or read from `.env`.
  - Safety: read-only tools always run; sensitive tools require an in-UI
    "Allow actions" toggle (no spoken confirmation in a text chat).
  - New endpoints: `GET /chat`, `GET /chat/models`, `POST /chat/message`,
    `POST /chat/reset`. New optional env keys: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`,
    `MOONSHOT_API_KEY`.
  - **In-UI `.env` editor**: enter a provider API key in the sidebar and click
    "save to .env" to persist it to the project's `.env` (survives restarts), instead
    of only living in the browser. New endpoints `GET /chat/env` / `POST /chat/env`
    (only the chat-provider keys are editable; writes preserve comments and unrelated
    keys, and update the live process env immediately). Suited to a self-hosted,
    single-user tool — keys are stored in plain text in the already-git-ignored `.env`.
  - **Redesigned UI** (dark "operator console" theme): IBM Plex Sans/Mono, a fixed
    left rail (wordmark, model picker grouped by provider, key + status, Allow-Actions
    toggle, reset) and a centered conversation column with a Miko glyph, tool chips,
    thinking dots, and an auto-growing composer. Ported from a Figma Make design to a
    single self-contained `webui/chat.html`; the JS request/response contract is
    unchanged.
  - **Clickable file links**: when Miko creates or edits a file, the chat response now
    lists it as a chip under her message (`chat()` returns a `files` array; paths are
    captured from the actual tool-call arguments and any path echoed in the result, then
    verified to exist on disk). Clicking a chip opens it in the workspace.
  - **Built-in Workspace** (file explorer + code editor, `file_browser.py`): a VS
    Code-style overlay to browse folders, open files, and save edits without leaving the
    page. CodeMirror gives syntax highlighting (Python, JS, HTML/CSS, Markdown, shell,
    YAML, C-like…), with Ctrl/⌘-S to save and a dirty/saved indicator; it degrades to a
    plain textarea if the CDN is unreachable. New endpoints: `GET /files/roots`,
    `GET /files/list`, `GET /files/read`, `POST /files/write`. Browsing is fenced to the
    home directory and the project folder, and refuses the same Windows/system paths the
    voice tools refuse; binary and >1 MB files open read-only.

## [0.2.0] — 2026-06-03

### Added

- **Bilingual support with a language toggle** (`MIKO_LANGUAGE=en|ro`, default `en`).
  Adds an English system prompt (`core/prompt_en.txt`) selected at runtime, and English
  translations of the spoken confirmation prompts, safety/cancel messages, mode-change
  announcements (`core/mode_manager.py`), the phone-commander language rule, and the
  startup/reconnect console lines. `config.py` gains a `language` field; `ModeManager`
  and `CommandRouter` are language-aware.

### Other additions

- **MiniMax / Anthropic / OpenAI backends for the phone commander.**
  `modules/phone_commander.py` now auto-selects its LLM backend: Anthropic-compatible
  endpoints (e.g. MiniMax `…/anthropic`), OpenAI-compatible endpoints, or Gemini as
  the fallback. Added per-user conversation history (capped, thread-safe) so Discord
  DM commands keep context across messages.
- **MiniMax config** (`config.py`): `MINIMAX_API_KEY`, `MINIMAX_BASE_URL`,
  `MINIMAX_MODEL` environment variables.
- **Tool-schema converters** (`tools.py`): `ALL_TOOL_DECLARATIONS_OPENAI` and
  `ALL_TOOL_DECLARATIONS_ANTHROPIC` derived from the Gemini declarations, with
  recursive `type` normalisation.
- **HTTP tool server** (`tool_server.py`): FastAPI bridge exposing all Miko tools to
  external agents (e.g. Hermes on WSL2). Endpoints: `GET /` (health),
  `GET /tools?format=openai|anthropic|gemini` (schema discovery),
  `POST /tools/{name}` (execution). Optional `TOOL_SERVER_KEY` bearer auth.
- **Headless launcher** (`start_tools_server.py`): runs only the tool server +
  Discord bot + file indexer, without the Gemini Live audio session — so external
  agents can use Miko's tools without the voice assistant listening.
- **`X-Bypass-Confirmation` header** (`tool_server.py`): trusted local agents can skip
  the voice-confirmation gate on destructive tools, dispatching directly.
- **Calendar integration** (`modules/calendar.py`): iCloud via CalDAV and Microsoft
  Teams/Outlook via Microsoft Graph (device-code OAuth, cached + auto-refreshed token).
  Tools: `list_events`, `get_today_events`, `create_event`, `delete_event`.
- **Calendar reminder daemon** (`modules/calendar_reminders.py`): polls calendars and
  sends Discord DM reminders 30 min and 5 min before each event, with dedup.
- **Personal Discord account control via local RPC** (`modules/discord_rpc.py`):
  drives the user's own Discord desktop client (not the bot account) to join voice
  channels, move between them, and mute/deafen. Tools: `join_voice_as_me`,
  `leave_voice_as_me`, `set_my_voice`, `discord_rpc_login`. Added
  `resolve_voice_channel` / `list_voice_channels` helpers to `modules/discord_bot.py`.
  (Sending messages from the personal account was prototyped via UI automation but
  removed — the bot account handles message sending.)

### Changed

- `main.py`: starts the HTTP tool server and the calendar reminder daemon alongside
  the existing services.
- `memory/memory_manager.py`: `update_from_conversation_async()` now supports MiniMax /
  Anthropic / OpenAI backends for fact extraction, with Gemini fallback.
- `core/audio_handler.py`: passes MiniMax config through to the memory extractor.
- `core/audio_handler.py`: tool dispatch now runs on a dedicated `ThreadPoolExecutor`
  so slow tool calls (Discord RPC handshakes, OAuth HTTP, UI automation) never starve
  the audio-I/O executor threads. Session reconnects after a dropped Gemini Live
  connection now give visible + audible feedback (the reconnect was previously silent).

### Fixed

- Phone commander lost conversation context between Discord DMs (e.g. replying "1" to a
  list). Per-user history now preserves context.
- iCloud CalDAV parsing under `caldav` 2.0 (which dropped the `vobject` dependency):
  switched to `icalendar_instance` / `.walk("VEVENT")`.
- Microsoft Graph scope error on AAD tenants: use the full
  `https://graph.microsoft.com/Calendars.ReadWrite` scope and let MSAL add
  `offline_access` itself.
- Asking about iCloud events no longer triggers a Microsoft login prompt: the Teams
  device-login requirement is now a footnote (or only blocks when Teams is specifically
  requested), instead of masking iCloud results.
- iCloud `create_event` `403 Forbidden`: iCloud returns read-only calendars (subscribed,
  holidays, birthdays) too. Now ranks/falls through to a writable calendar, and honors
  `ICLOUD_CALENDAR_NAME` to pin a specific one.
- Calendar events created at the wrong time (off by the UTC offset, e.g. BST +1h): the
  spoken time is now interpreted in the system's local timezone before converting to UTC.
- Confirmation replies are now understood naturally (`core/wake_word.py`): besides "da",
  Miko accepts "yes", "sure", "trimite-l", "hai, fă-o", "go ahead", and cancels on "nu",
  "lasă", "stop", "cancel", etc. "nu trimite" correctly cancels (first intent wins).

### Dependencies

- Added: `fastapi`, `uvicorn`, `openai`, `anthropic` (tool server + backends);
  `caldav`, `icalendar`, `msal` (calendar); `pypresence` (personal Discord RPC).

### Notes for self-hosters

- The Hermes ↔ Miko MCP bridge script (`miko_mcp.py`) lives outside this repo on the
  host (`C:\Users\<you>\miko_mcp.py`) and is referenced from the WSL2 Hermes config.
- Teams calendar access on a managed (work/school) tenant may require admin approval;
  registering the Azure app under a personal Microsoft account with the `common`
  authority avoids this.
- Personal Discord RPC requires the Discord **desktop** client running and logged in;
  the restricted `rpc` scopes work without Discord whitelist approval because you are
  the app owner.

## [0.1.0] — Initial commit

- Miko voice AI agent: Gemini Live audio pipeline, ACTIVE/STANDBY/AUTO modes,
  Discord bot, SQLite file indexer, Markdown notes, journey planning, OS control,
  media control, web research, long-term memory, and safety guards.
