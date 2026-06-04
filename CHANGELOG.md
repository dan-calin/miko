# Changelog

All notable changes to Miko are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Approval mode — review file/command changes before they apply** (Claude-Code-style
  permissions). A sidebar **Approve Changes** toggle: when on, mutating tools (file
  writes/moves/deletes, `run_command`, and the destructive/send tools) are **not executed**
  — instead Miko proposes them and the chat shows an **approval card** with the command or
  a **unified diff** of the file change, plus **Approve / Deny**. Approving runs the action
  (and links any files it produced); denying discards it. Read-only tools still run freely.
  New endpoint `POST /chat/approve`; `/chat/message` accepts an `approval` flag.

- **Per-model settings + effort dial, and an expanded provider list.** The Chat UI's
  Agent/Skills popover remembers persona + skills **per provider+model** and adds an
  **Effort** control (Quick/Standard/Deep). Effort drives three things: deep-research
  depth, the chat **tool-call budget** (3/6/8 rounds), and — where the model supports
  it — a **native reasoning parameter** sent to the API (OpenAI `reasoning_effort`,
  real-Claude `output_config.effort`, Gemini 2.5/3.x thinking budget), gated per model
  with a safe retry-without fallback so unsupported models (e.g. MiniMax) keep working.
  Provider/model lists refreshed to current IDs (Gemini 3.5/3.1/3, GPT-5.5/5.4/5.2,
  MiniMax-M3, DeepSeek-V4, Kimi K2.6) plus two new providers: **Anthropic Claude**
  (real Claude — unlocks the native effort/thinking passthrough) and **xAI Grok**.
- **Resource monitor** in the Chat UI sidebar — session tokens, last-turn tokens,
  session time, and message count (token usage captured from each provider response).

- **Memory v2 — token-efficient "lean cognitive" memory** (research-driven upgrade
  of the semantic memory; informed by Mem0, A-MEM, Zep, and 2025–26 surveys).
  Same local-only substrate (fastembed + SQLite), but a much smarter intelligence
  layer, with all heavy work async on a cheap model (`MIKO_MEMORY_MODEL`, default
  flash-lite) and a tiny, ranked per-turn injection:
  - **Self-reconciling facts:** the learner now makes one memory-aware
    extract→reconcile call that emits canonical `set`/`delete` ops, so corrections
    overwrite the right fact instead of piling up junk keys ("I'm Dan, not Roxan"
    just works). Replaces the naive YES/NO+JSON extractor on both voice and chat.
  - **Scored hybrid recall:** retrieval now ranks by relevance (semantic + keyword)
    + recency (exp decay) + importance, with a `last_used` freshness bump. Per-turn
    injection is capped to a ~350-token budget (leaner than before).
  - **Episodic memory + reflection:** a one-line episode summary is folded into the
    same reconcile call (no extra LLM call); periodically an async reflection pass
    distills recent episodes + facts into durable `insight`s and prunes the episode
    log — consolidation that compresses many memories into few. Powers "what have I
    been working on?".
  - **Note-brain (Obsidian/PARA):** the vault gains a light PARA structure
    (`Inbox/Projects/Areas/Resources/Archives/Daily` + a README), captures route to
    `Inbox/`, research reports to `Resources/`, and notes auto-link to related notes
    with `[[wikilinks]]` (vector neighbours) — open the graph view in Obsidian. New
    `vault.py`; note tools now search recursively and index new notes immediately.
  - New store columns + helpers (`importance/created/last_used/source`, `recent`,
    `prune`), auto-migrating older DBs.

- **Deep Research pipeline that feeds the second brain** (`deep_research.py`,
  `chat_backend.complete_text`, `modules/research.search_results`/`fetch_text`).
  Toggling the **Deep Research** skill now runs a real, orchestrated pipeline
  instead of a one-shot search: it asks the model for a research plan
  (sub-questions), web-searches each, **reads the top sources in full** (real page
  fetching via `beautifulsoup4`), synthesizes a **cited report**, then **saves it as
  a Markdown note in your vault and indexes it** — so research becomes permanent,
  recall-able knowledge. The Chat UI streams **live progress** (plan → searching →
  reading → synthesizing) in a progress card, then renders the report with a
  clickable link to the saved note. New streaming endpoint `POST /chat/research`
  (NDJSON); the turn is persisted like any other (with a `deep_research` chip and
  the note as a file link).

- **Semantic long-term memory / "second brain"** (`memory/embeddings.py`,
  `memory/knowledge_store.py`, `modules/knowledge.py`). Miko now learns from you and
  recalls it by meaning, across both the voice agent and the web chat:
  - **Embeddings** are pluggable and **local-first**: uses `fastembed` (offline ONNX,
    no API cost/limits) when installed, otherwise falls back to the Gemini/OpenAI
    embedding API using your existing key, otherwise keyword search. Nothing hard-fails.
  - **Vector store** is a dependency-light SQLite table with a NumPy cosine search
    (`data/knowledge.db`, git-ignored) over two kinds of content: structured personal
    **facts** (from `long_term.json`) and **chunks of your notes vault**.
  - **The vault is your existing `Desktop/Miko Notes` folder** — already Markdown +
    YAML, so it doubles as an **Obsidian vault** (open it in Obsidian for graph view,
    backlinks, and search with zero migration). Notes are (re)indexed on server start
    and incrementally (unchanged files skip).
  - **New shared tools** `remember` and `recall`, registered for every provider, so
    both voice and chat can save durable facts and semantically search memory + notes.
  - **The web chat now learns**: it injects your known facts + the notes most relevant
    to each message into the system prompt, and extracts durable facts from chat turns
    (the same learner the voice agent already used — previously voice-only).
  - New endpoints: `GET /knowledge/stats`, `POST /knowledge/reindex`,
    `POST /knowledge/recall`. New dependency: `fastembed` (optional but recommended).

- **Selectable ECC agents & skills in the Chat UI** (`agent_skills.py`, vendored
  docs under `vendor/ecc/`). A **⚙ Agent / Skills** button by the composer opens a
  picker: choose one **persona** (Chief of Staff, Planner, Code Explorer, Code
  Reviewer, Security Reviewer) and toggle any **skills** (Deep Research, Article
  Writing, Brand Voice, Git Workflow, GitHub Ops, Email Ops, Knowledge Ops,
  Codebase Onboarding). The active selection shows as chips by the send box and
  persists in the browser. Adapted from the [ECC project](https://github.com/affaan-m/ecc)
  (MIT, © Affaan Mustafa) — the original Markdown is vendored verbatim for
  provenance, and a **trimmed, Miko-adapted** instruction block for each is
  appended to the chat **system prompt** when selected. Because it rides in the
  system prompt, it works on **every provider** (Gemini, MiniMax, OpenAI,
  DeepSeek, Kimi), not just Claude; Claude-Code-only references (MCP tools, the
  Task subagent tool, hooks, `gh`/`gog` CLIs, `~/.claude/` paths) are stripped and
  re-pointed at Miko's own tools. Only the selected blocks cost tokens
  (~150–300 each); nothing is sent when nothing is selected. New endpoint:
  `GET /chat/agent-skills`; `/chat/message` accepts optional `agent` + `skills`.

- **Persistent, resumable chat conversations** (`conversation_store.py`). Chats are now
  saved to disk (one JSON per conversation under `data/conversations/`, git-ignored)
  instead of an in-memory dict, so they survive restarts. The Chat UI gains a
  **Conversations** list in the sidebar: **+ New** to start a fresh thread, click to
  switch and continue any past conversation (full transcript restored, including tool
  chips and file links), double-click to rename, × to delete. The last conversation is
  restored on load. New endpoints: `GET /chat/conversations`, `GET /chat/conversation`,
  `POST /chat/conversation/rename`, `POST /chat/conversation/delete`; the model receives
  the persisted history for context continuity.

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
  - **Selectable active workspace**: a "Workspace" picker in the sidebar lets you choose
    the folder Miko works in right now — any folder you like. The chosen folder (a) is
    where the explorer opens, (b) is injected into the chat system prompt (so "what
    workspace are you in?" and "create a file here" follow your choice), and (c) is
    exported as `MIKO_WORKSPACE` so `run_command` executes there. Set it from the sidebar
    or with "set this folder" while browsing; it persists across restarts
    (`data/workspace.json`). New endpoints: `GET /workspace`, `POST /workspace`;
    `/chat/message` accepts an optional `workspace`.
  - **Built-in Workspace** (file explorer + code editor, `file_browser.py`): a VS
    Code-style overlay to browse folders, open files, and save edits without leaving the
    page. Free navigation — a **Browse…** button that opens the native Windows
    "Select Folder" dialog (the server runs locally, so the picker shows on your screen
    via `POST /files/pick`), an editable address bar to type/paste any path, and real
    drive-letter quick-jumps. CodeMirror gives syntax highlighting (Python, JS, HTML/CSS,
    Markdown, shell, YAML, C-like…), with Ctrl/⌘-S to save and a dirty/saved indicator; it
    degrades to a plain textarea if the CDN is unreachable. New endpoints:
    `GET /files/roots`, `GET /files/list`, `GET /files/read`, `POST /files/write`.
    Browsing and reading are unrestricted (it's your machine); the only hard rule is that
    *writes* into the Windows system folders are refused, and binary / >1 MB files open
    read-only.
  - **Right-click context menu** in the explorer: New File, New Folder, Rename, Delete
    (to the Recycle Bin via `send2trash`), Copy, Cut, Paste, and Refresh. Right-click an
    item to act on it, or empty space to create/paste in the current folder. New names
    are validated, paste auto-suffixes collisions (`name (copy).ext`), and pasting a
    folder into itself is refused. New endpoints: `POST /files/create`,
    `POST /files/rename`, `POST /files/delete`, `POST /files/paste`.

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
