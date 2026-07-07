"""
chat_backend.py — Model-agnostic chat with Miko's tools.

Powers the web Chat UI. Lets the user talk to Miko by text using the provider of
their choice (Gemini, MiniMax/Anthropic, OpenAI, OpenRouter, NVIDIA, DeepSeek,
Kimi, or a custom
OpenAI-compatible endpoint). Miko's full tool set is exposed to the model, so it
can control the PC, Discord, calendars, etc. — the same tools the voice agent uses.

Three wire protocols cover every provider:
  - "gemini"    → google-genai
  - "anthropic" → anthropic SDK (Messages API)         [MiniMax /anthropic]
  - "openai"    → openai SDK (Chat Completions)         [OpenAI, OpenRouter, NVIDIA,
                                                         DeepSeek, Kimi, custom]

Tool execution goes through CommandRouter. Read-only tools always run; sensitive
tools (delete, send message, shutdown, …) require the per-session "allow actions"
flag, since a text UI has no voice-confirmation step.
"""

import base64
import json
import logging
import os

logger = logging.getLogger("miko.chat")

_MAX_ROUNDS = 6
_MAX_HISTORY = 30  # neutral messages handed to the model per turn

# Conversations are persisted to disk — see conversation_store.py.


# ── Provider presets ──────────────────────────────────────────────────────────
# env_key: the .env variable holding the API key (used if the UI doesn't supply one).
# Model lists current as of June 2026. Newest first; older models kept for
# compatibility. You still need a key with access to whichever model you pick.
PROVIDERS = {
    "gemini": {
        "label": "Google Gemini",
        "protocol": "gemini",
        "base_url": "",
        "env_key": "LLM_API_KEY",
        "models": ["gemini-3.5-flash", "gemini-3.1-pro", "gemini-3.1-flash-lite",
                   "gemini-2.5-flash", "gemini-2.5-pro"],
    },
    "openai": {
        "label": "OpenAI",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "models": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.2", "gpt-4.1", "gpt-4o"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "protocol": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "models": ["openrouter/free"],
    },
    "nvidia": {
        "label": "NVIDIA NIM",
        "protocol": "openai",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "env_key": "NVIDIA_API_KEY",
        "models": ["z-ai/glm-5.2", "google/diffusiongemma-26b-a4b-it"],
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "protocol": "anthropic",
        "base_url": "",   # SDK default → api.anthropic.com (real Claude)
        "env_key": "ANTHROPIC_API_KEY",
        "models": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"],
    },
    "minimax": {
        "label": "MiniMax",
        "protocol": "anthropic",
        "base_url": "https://api.minimax.io/anthropic",
        "env_key": "MINIMAX_API_KEY",
        "models": ["MiniMax-M3", "MiniMax-M3-highspeed", "MiniMax-M2.7"],
    },
    "deepseek": {
        "label": "DeepSeek",
        "protocol": "openai",
        "base_url": "https://api.deepseek.com",
        "env_key": "DEEPSEEK_API_KEY",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
    },
    "kimi": {
        "label": "Kimi (Moonshot)",
        "protocol": "openai",
        "base_url": "https://api.moonshot.ai/v1",
        "env_key": "MOONSHOT_API_KEY",
        "models": ["kimi-k2.6", "kimi-k2-0905-preview", "moonshot-v1-32k"],
    },
    "grok": {
        "label": "xAI Grok",
        "protocol": "openai",
        "base_url": "https://api.x.ai/v1",
        "env_key": "XAI_API_KEY",
        "models": ["grok-4.3", "grok-4.20", "grok-4-fast"],
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "protocol": "openai",
        "base_url": "",
        "env_key": "",
        "models": [],
    },
}


def list_models() -> dict:
    """Return provider presets + whether each has a key configured in .env."""
    out = {}
    for pid, p in PROVIDERS.items():
        out[pid] = {
            "label": p["label"],
            "protocol": p["protocol"],
            "base_url": p["base_url"],
            "models": p["models"],
            "env_key": p["env_key"],
            "has_env_key": bool(p["env_key"] and os.getenv(p["env_key"], "")),
            "needs_base_url": pid == "custom",
        }
    return out


def complete_text(provider: str, model: str = "", api_key: str = "", base_url: str = "",
                  system: str = "", user: str = "", max_tokens: int = 2048) -> str:
    """One-shot text completion (no tools) for any provider — used by pipelines such
    as deep research that need the model to plan or synthesize. Raises on no key."""
    preset = PROVIDERS.get(provider) or PROVIDERS["gemini"]
    key = (api_key or "").strip() or os.getenv(preset["env_key"], "")
    if not key:
        raise RuntimeError(f"No API key for {preset['label']}.")
    base = (base_url or "").strip() or preset["base_url"]
    model = model or (preset["models"] or [""])[0]
    proto = preset["protocol"]

    if proto == "gemini":
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        cfg = types.GenerateContentConfig(system_instruction=system) if system else None
        resp = client.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text=user)])],
            config=cfg,
        )
        cand = resp.candidates[0] if resp.candidates else None
        if cand and cand.content and cand.content.parts:
            return " ".join(p.text for p in cand.content.parts if p.text).strip()
        return ""

    if proto == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=key, base_url=base or None)
        kwargs = {"model": model, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": user}]}
        if system:
            kwargs["system"] = system
        resp = client.messages.create(**kwargs)
        return " ".join(b.text for b in resp.content if hasattr(b, "text")).strip()

    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base or None)
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": user})
    resp = client.chat.completions.create(model=model, messages=msgs)
    msg = _openai_first_message(resp, provider, model)
    return _openai_msg_content(msg).strip()


# ── .env read/write (settings panel) ──────────────────────────────────────────
# The Chat UI can read and persist API keys straight into the project's .env so
# they survive restarts. This is a self-hosted, single-user tool — keys live in
# plain text in .env (already git-ignored), same as if you edited the file by hand.

from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent / ".env"

# Categorised settings the UI exposes, so users configure keys/credentials in a
# Settings panel instead of hand-editing .env. Each field maps to one env var.
SETTINGS_GROUPS = [
    {"category": "AI Models", "fields": [
        {"key": "LLM_API_KEY", "label": "Google Gemini API key", "secret": True,
         "help": "Core model + dictation fallback. aistudio.google.com/apikey"},
        {"key": "OPENAI_API_KEY", "label": "OpenAI API key", "secret": True},
        {"key": "OPENROUTER_API_KEY", "label": "OpenRouter API key", "secret": True},
        {"key": "NVIDIA_API_KEY", "label": "NVIDIA NIM API key", "secret": True},
        {"key": "ANTHROPIC_API_KEY", "label": "Anthropic (Claude) API key", "secret": True},
        {"key": "MINIMAX_API_KEY", "label": "MiniMax API key", "secret": True},
        {"key": "MINIMAX_BASE_URL", "label": "MiniMax base URL", "placeholder": "https://api.minimax.io/anthropic"},
        {"key": "MINIMAX_MODEL", "label": "MiniMax model", "placeholder": "MiniMax-M2.7"},
        {"key": "DEEPSEEK_API_KEY", "label": "DeepSeek API key", "secret": True},
        {"key": "MOONSHOT_API_KEY", "label": "Kimi / Moonshot API key", "secret": True},
        {"key": "XAI_API_KEY", "label": "xAI (Grok) API key", "secret": True},
    ]},
    {"category": "Sub-agents", "fields": [
        {"key": "MIKO_SUBAGENT_MODEL_MODE", "label": "Model mode", "placeholder": "main",
         "options": ["main", "custom"],
         "help": "'main' inherits the current chat/voice model; 'custom' uses the fields below."},
        {"key": "MIKO_SUBAGENT_PROVIDER", "label": "Custom provider", "placeholder": "openrouter"},
        {"key": "MIKO_SUBAGENT_MODEL", "label": "Custom model", "placeholder": "openrouter/free"},
        {"key": "MIKO_SUBAGENT_BASE_URL", "label": "Custom base URL", "placeholder": "https://host/v1"},
        {"key": "MIKO_SUBAGENT_API_KEY", "label": "Custom API key override", "secret": True,
         "help": "Optional. If blank, Miko uses the provider key from the AI Models section."},
    ]},
    {"category": "Discord", "fields": [
        {"key": "DISCORD_TOKEN", "label": "Bot token", "secret": True},
        {"key": "DISCORD_GUILD_ID", "label": "Server (guild) ID"},
        {"key": "DISCORD_OWNER", "label": "Your Discord account (exact)",
         "help": "Your exact Discord username or display name. 'send me' / 'join my call' resolve to this — prevents Miko picking a similar name."},
        {"key": "OWNER_ALIASES", "label": "Your other names",
         "help": "Comma-separated names you go by (e.g. Dan,Roxan) that also mean you."},
        {"key": "TRUSTED_VOICE_USERS", "label": "Trusted users (Discord names)",
         "help": "Comma-separated display names allowed to command Miko via DM. Your owner name is always trusted."},
        {"key": "DISCORD_RPC_CLIENT_ID", "label": "Personal-account RPC client ID"},
        {"key": "DISCORD_RPC_CLIENT_SECRET", "label": "RPC client secret", "secret": True},
        {"key": "DISCORD_RPC_REDIRECT", "label": "RPC redirect", "placeholder": "http://localhost"},
    ]},
    {"category": "Email", "fields": [
        {"key": "EMAIL_USER", "label": "Email address"},
        {"key": "EMAIL_PASS", "label": "Password / App Password", "secret": True,
         "help": "Gmail: enable 2FA and use an App Password."},
        {"key": "EMAIL_IMAP_HOST", "label": "IMAP host", "placeholder": "imap.gmail.com"},
        {"key": "EMAIL_IMAP_PORT", "label": "IMAP port", "placeholder": "993"},
        {"key": "EMAIL_SMTP_HOST", "label": "SMTP host", "placeholder": "smtp.gmail.com"},
        {"key": "EMAIL_SMTP_PORT", "label": "SMTP port", "placeholder": "587"},
        {"key": "EMAIL_FROM", "label": "From (optional)"},
    ]},
    {"category": "Calendar", "fields": [
        {"key": "ICLOUD_EMAIL", "label": "iCloud email"},
        {"key": "ICLOUD_APP_PASSWORD", "label": "iCloud app password", "secret": True},
        {"key": "AZURE_CLIENT_ID", "label": "Azure client ID (Outlook/Teams)"},
        {"key": "AZURE_TENANT_ID", "label": "Azure tenant ID", "placeholder": "common"},
    ]},
    {"category": "Voice & Language", "fields": [
        {"key": "MIKO_LANGUAGE", "label": "Language (en / ro)", "placeholder": "en"},
        {"key": "MIKO_DICTATION_LANG", "label": "Dictation language (BCP-47)", "placeholder": "ro-RO"},
        {"key": "MIKO_DICTATION_MODEL", "label": "Dictation fallback model", "placeholder": "gemini-2.5-flash"},
        {"key": "MIKO_VOICE", "label": "Live voice name", "placeholder": "Aoede"},
        {"key": "MIKO_VOICE_PROVIDER", "label": "Voice chat provider", "placeholder": "custom"},
        {"key": "MIKO_VOICE_MODEL", "label": "Voice chat model", "placeholder": "qwen3-8b"},
        {"key": "MIKO_VOICE_BASE_URL", "label": "Voice local base URL", "placeholder": "http://127.0.0.1:1234/v1"},
        {"key": "MIKO_VOICE_API_KEY", "label": "Voice local API key", "placeholder": "lm-studio", "secret": True},
        {"key": "MIKO_VOICE_COMPACT_TOOLS", "label": "Voice compact tools", "placeholder": "true"},
    ]},
    {"category": "General", "fields": [
        {"key": "OWNER_NAME", "label": "Your name"},
        {"key": "MIKO_NOTES_DIR", "label": "Notes vault folder"},
        {"key": "HOME_POSTCODE", "label": "Home postcode (weather/journey)"},
        {"key": "MIKO_EMAIL_WATCH_INTERVAL", "label": "Inbox-watch poll (seconds)", "placeholder": "120"},
        {"key": "TOOL_SERVER_KEY", "label": "Tool-server bearer key (optional)", "secret": True},
    ]},
]

EDITABLE_ENV_KEYS = [f["key"] for g in SETTINGS_GROUPS for f in g["fields"]]
_SECRET_KEYS = {f["key"] for g in SETTINGS_GROUPS for f in g["fields"] if f.get("secret")}

# The chat sidebar key editor reads these back in full (to show "saved in .env").
# read_env_keys is deliberately limited to them so GET /chat/env never returns the
# other secrets (Discord token, email password, …) in plaintext — those go through
# settings_schema(), which masks secret values.
CHAT_ENV_KEYS = [
    "LLM_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY",
    "ANTHROPIC_API_KEY", "MINIMAX_API_KEY",
    "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "XAI_API_KEY",
]


def settings_schema() -> dict:
    """The grouped settings schema + current state. Secret values are never sent back
    (only a `set` flag); non-secret values are returned so the UI can show/edit them."""
    groups = []
    for g in SETTINGS_GROUPS:
        fields = []
        for f in g["fields"]:
            v = os.getenv(f["key"], "")
            fields.append({
                "key": f["key"], "label": f["label"], "secret": bool(f.get("secret")),
                "placeholder": f.get("placeholder", ""), "help": f.get("help", ""),
                "options": f.get("options", []),
                "set": bool(v), "value": ("" if f.get("secret") else v),
            })
        groups.append({"category": g["category"], "fields": fields})
    return {"groups": groups}


def read_env_keys() -> dict:
    """Return current values for the CHAT provider keys only (the sidebar key editor
    compares against these). Other secrets are never returned in full — see
    settings_schema()."""
    return {k: os.getenv(k, "") for k in CHAT_ENV_KEYS}


def write_env_keys(updates: dict) -> dict:
    """
    Persist the given {KEY: value} pairs to .env (creating it if needed) and update
    the live process env so the change takes effect immediately. Only keys in
    EDITABLE_ENV_KEYS are honoured. Returns the updated values.
    """
    # Blank secret fields mean "leave unchanged" (the UI never echoes secrets back),
    # so a masked empty field can't wipe an existing key. Non-secrets may be cleared.
    clean = {k: str(v) for k, v in (updates or {}).items()
             if k in EDITABLE_ENV_KEYS and not (k in _SECRET_KEYS and not str(v).strip())}
    if not clean:
        return read_env_keys()

    # Read existing lines (preserve comments / ordering / unrelated keys).
    lines = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()

    seen = set()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name in clean:
            lines[i] = f"{name}={clean[name]}"
            seen.add(name)

    # Append any keys that weren't already present.
    missing = [k for k in clean if k not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        for k in missing:
            lines.append(f"{k}={clean[k]}")

    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Reflect immediately in the running process.
    for k, v in clean.items():
        os.environ[k] = v

    logger.info(f"[chat] wrote {len(clean)} key(s) to .env: {list(clean.keys())}")
    return read_env_keys()


# ── System prompt ─────────────────────────────────────────────────────────────

def _system_prompt(owner_name: str, language: str, workspace: str = "") -> str:
    if language == "ro":
        base = (
            f"Ești Miko, asistentul personal al lui {owner_name}, accesibil printr-un chat text. "
            "Ai acces la unelte care controlează PC-ul Windows al userului, Discord, calendarele, "
            "căutarea web, notițe și fișiere. Folosește uneltele când e nevoie; nu inventa rezultate. "
            "Răspunde scurt și la obiect. Răspunde în română dacă userul scrie în română. "
            "MUNCA CONTINUĂ / ÎN FUNDAL cere o unealtă reală, nu o promisiune. Dacă userul îți cere "
            "să urmărești / să fii atentă la emailuri care vin ('spune-mi când îmi scrie X', "
            "'anunță-mă dacă primesc mail de la Y'), TREBUIE să apelezi watch_email. Dacă cere ceva "
            "programat sau repetat ('în fiecare dimineață', 'amintește-mi la 6', 'verifică la 2 ore', "
            "'pe 8 să faci…'), TREBUIE să apelezi schedule_task. Nu spune niciodată că 'urmărești', "
            "că 'o să te anunț când vine' sau că ceva e 'programat' decât dacă unealta chiar a rulat "
            "și a returnat succes ACUM. Dacă unealta eșuează sau lipsește, spune asta direct. "
            "CITIREA FIȘIERELOR: ca să citești conținutul unui fișier folosește file_op cu "
            "action='read' (sau read_note pentru notițe, read_email pentru email). NU rula comenzi "
            "shell ca să citești un fișier — fără run_command cu 'type', 'cat', 'more', "
            "'Get-Content'; e unealta greșită și cere o aprobare inutilă. Folosește run_command doar "
            "pentru ce nu are o unealtă dedicată. "
            "VISIBLE SCREEN: browser_open is Miko's private hidden browser; the user cannot see it. "
            "When the user asks to show/open something on their screen, use open_url for web pages "
            "or show_email for email messages. Do not claim the user can see a private browser page. "
            "Dacă userul cere postarea/mesajul/butonul/linkul dintr-un email ('arată-mi postarea', "
            "'view message', 'deschide postarea Nextdoor'), folosește open_email_link, nu show_email. "
            "STIL EMAIL: fii scurtă cu emailurile. Pentru căutări/listări, dă doar cel mai bun rezultat "
            "sau maxim trei rezultate scurte dacă userul nu cere mai mult. Pentru show_email, spune doar "
            "că e deschis pe ecran; nu rezuma din nou corpul emailului. "
            "ORICE acțiune necesită APELAREA uneltei ei ÎN tura CURENTĂ — trimitere mesaj Discord, "
            "vorbit pe voice, rulat o comandă etc. Să răspunzi 'Gata', 'Zis', 'Trimis' sau 'propus' "
            "FĂRĂ un apel de unealtă în aceeași tură e o halucinație — interzis. Inclusiv REPETĂRILE: "
            "când userul zice 'fă din nou', 'iar', 'la fel', 'încă o dată', TREBUIE să apelezi din "
            "nou unealta; nu poți refolosi acțiunea dintr-o tură anterioară. Răspunsurile marcate "
            "'[Done by calling: …]' au rulat o unealtă reală — fă la fel de fiecare dată. "
            "SCRIEREA DE COD: când userul îți cere să construiești/creezi/modifici cod sau o aplicație, "
            "scrii TU fișierele cu file_op — asta e varianta implicită. NU apela code_with_claude decât "
            "dacă userul cere EXPLICIT Claude Code / pair coder ('folosește Claude Code', "
            "'fă pair-programming'); rulează un CLI extern pe abonamentul Claude al userului și eșuează "
            "dacă nu e activ, deci nu-l folosi niciodată neinvitat. Când userul CERE Claude Code și nu a "
            "spus cât să ruleze, ÎNTREABĂ-L O DATĂ — în text — câte runde să pună limită, sau dacă să "
            "ruleze până decizi TU că proiectul e gata; apoi apelează code_with_claude cu acel număr ca "
            "max_rounds (pune 0 pentru 'până decizi tu'). Asta e singura întrebare pe care o poți pune "
            "înainte de a coda — orice altceva cere un apel real de unealtă, nu o întrebare."
        )
        if workspace:
            base += (
                f" Lucrezi acum în folderul ales de user (workspace-ul curent): {workspace}. "
                "Acesta E directorul tău curent de lucru: comenzile din shell (inclusiv git) rulează "
                "deja AICI, iar căile relative se rezolvă tot aici — nu prefixa C:\\ și nu da 'cd' "
                "în altă parte. Când userul pomenește un fișier doar pe nume (ex. 'demo.cast'), "
                "presupune că e în workspace și caută-l ACOLO întâi (file_op list pe workspace sau "
                "calea directă), NU porni o căutare prin tot sistemul (Desktop, Documents, find_file) "
                "decât dacă chiar lipsește din workspace."
            )
            from modules.wsl_util import is_wsl_path
            if is_wsl_path(workspace):
                base += (
                    " Workspace-ul ăsta e în WSL (Linux), deci run_command rulează comanda în bash "
                    "în distro, deja cu cd aici. Scrie comenzi Linux/bash (ls, cat, git, rm — nu "
                    "dir/copy/PowerShell) și căi POSIX; git merge nativ, deci doar "
                    "'git add … && git commit -m … && git push'."
                )
        return base
    base = (
        f"You are Miko, {owner_name}'s personal assistant, reachable through a text chat. "
        "You have tools that control the user's Windows PC, Discord, calendars, web search, "
        "notes, and files. Use the tools when needed; never make up results. Be concise and "
        "direct. IMPORTANT: always write your reply in the SAME language as the user's latest "
        "message (the user is writing in English → reply in English). Some tool descriptions "
        "are in Romanian; ignore that — it must not change your reply language. "
        "NEVER claim a file was saved or an action was done unless the tool result confirms "
        "it (it returns the real absolute path). If a tool result says an action is PROPOSED "
        "or awaiting approval, tell the user it is pending their approval — do not say it's done. "
        "Conversely, if a tool result says ACTION COMPLETED, it really happened — report it as "
        "DONE, never as pending. Judge each action ONLY by its own latest tool result, not by "
        "what an earlier turn said. "
        "CONFIRMATIONS ARE AUTOMATIC: this chat has an Approve toggle. When it's ON your "
        "sensitive actions are held as proposals and the user clicks Approve; when it's OFF they "
        "run immediately. EITHER WAY you must NEVER ask the user to confirm in text — no 'should "
        "I send this?', no 'reply yes', no 'confirm the recipient', no asking who to send to. "
        "Just CALL the tool; the system gates it. Some tool descriptions mention voice "
        "confirmation — that's only for the voice assistant; ignore it here. When the user says "
        f"'send me ...' / 'ping me' / 'DM me', the recipient is {owner_name} — never ask who. "
        "ONGOING / BACKGROUND work needs a real tool, never just a promise. If the user asks you "
        "to watch / monitor / keep an eye on / look out for incoming email ('tell me when X emails "
        "me', 'ping me if I get a mail from Y', 'let me know what they reply'), you MUST call "
        "watch_email. If they ask for something on a schedule or repeatedly ('every morning…', "
        "'remind me at 6', 'check X every 2 hours', 'on the 8th do…'), you MUST call schedule_task. "
        "NEVER say you're 'watching', 'keeping an eye out', 'will ping you when it arrives', or that "
        "something is 'scheduled/set up' unless that tool actually ran and returned success THIS "
        "turn. If the tool fails or isn't available, say so plainly — don't pretend it's running. "
        "READING FILES: to read a file's contents use file_op with action='read' (or read_note for "
        "vault notes, read_email for email). NEVER run a shell command to read a file — no "
        "run_command with 'type', 'cat', 'more', 'Get-Content' — that's the wrong tool and forces a "
        "needless approval. Reserve run_command for things with no dedicated tool. "
        "VISIBLE SCREEN: browser_open is Miko's private hidden browser; the user cannot see it. "
        "When the user asks to show/open something on their screen, use open_url for web pages "
        "or show_email for email messages. Do not claim the user can see a private browser page. "
        "If the user asks for the post/message/button/link inside an email notification "
        "('show me the post', 'view message', 'open the Nextdoor post'), use open_email_link, "
        "not show_email. "
        "EMAIL RESPONSE STYLE: Be terse with email. For searches/lists, give only the best match "
        "or at most three short matches unless the user asks for more. For show_email, say it is "
        "open on screen and stop; do not re-summarize the email body. "
        "EVERY action requires CALLING its tool in the CURRENT turn — sending a Discord message, "
        "speaking on voice, running a command, etc. Replying 'Done', 'Sent', 'Zis', or 'proposed' "
        "WITHOUT a matching tool call in the same turn is a hallucination — forbidden. This includes "
        "REPEATS: when the user says 'do it again', 'again', 'same', 'one more', you MUST call the "
        "tool again; you cannot reuse an earlier turn's action. Earlier replies tagged "
        "'[Done by calling: …]' each ran a real tool — match that every time, never just narrate. "
        "WRITING CODE: when the user asks you to build, create, or change code or an app, write the "
        "files YOURSELF with file_op — that is the default. Do NOT call code_with_claude unless the "
        "user EXPLICITLY asks to use Claude Code / the pair coder (e.g. 'use Claude Code', "
        "'pair-program this'); it runs an external CLI under the user's own Claude subscription and "
        "fails when that isn't active, so never reach for it uninvited. When the user DOES ask for "
        "Claude Code and hasn't said how long to run it, ASK them ONCE — in text — how many rounds "
        "to cap it at, or whether to run until you judge the project done; then call code_with_claude "
        "with that number as max_rounds (pass 0 for 'until you decide it's done'). This rounds "
        "question is the one clarification you may ask before coding — everything else still requires "
        "a real tool call, not a question."
    )
    if workspace:
        base += (
            f" You are currently working in the user's selected workspace folder: {workspace}. "
            "This IS your current working directory: shell commands (including git) already run "
            "HERE, and relative paths resolve here too — don't prefix C:\\ or 'cd' somewhere else. "
            "When the user mentions a file by bare name (e.g. 'demo.cast'), assume it lives in this "
            "workspace and look THERE first (file_op list on the workspace, or the direct path) — "
            "do NOT launch a system-wide hunt (Desktop, Documents, find_file) unless it's genuinely "
            "absent from the workspace."
        )
        from modules.wsl_util import is_wsl_path
        if is_wsl_path(workspace):
            base += (
                " This workspace lives inside WSL (Linux), so run_command executes your command "
                "in bash inside the distro, already cd'd here. Write Linux/bash commands (ls, cat, "
                "git, rm — not dir/copy/PowerShell) and use POSIX paths; git works natively, so just "
                "'git add … && git commit -m … && git push'."
            )
    return base


# ── Long-term memory + semantic recall (the "second brain") ───────────────────

def _memory_context(message: str) -> str:
    """Build the memory addendum for the system prompt: the user's known facts,
    plus any vault notes semantically relevant to this message. Best-effort."""
    from config import CONFIG
    from memory.memory_manager import load_memory, format_memory_for_prompt

    parts = []
    facts = format_memory_for_prompt(load_memory(CONFIG.memory_file))
    if facts:
        parts.append(facts.strip())

    # Ranked recall across notes/episodes/insights, capped to a tight token budget
    # (~350 tokens ≈ 1400 chars) so per-turn cost stays small regardless of vault size.
    try:
        from memory import knowledge_store as KS
        hits = KS.search(message, k=6, kinds=["note", "episode", "insight"])
        budget, used, lines = 1400, 0, []
        for h in hits:
            snippet = h["text"][:220].strip()
            src = ""
            if h["kind"] == "note":
                src = " (" + os.path.basename(h["ref"].split("#")[0]) + ")"
            line = f"- {snippet}{src}"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line)
        if lines:
            parts.append("[RELEVANT MEMORY]\n" + "\n".join(lines))
    except Exception as e:
        logger.warning(f"recall failed: {e}")

    try:   # today's schedule (cached by the briefs daemon — no live calendar call)
        from modules import schedule_briefs
        brief = schedule_briefs.get_today_brief()
        if brief:
            parts.append("[TODAY'S SCHEDULE]\n" + brief)
    except Exception:
        pass

    try:   # what the user is building (details are recall-able vault notes)
        import modules.projects as PR
        pl = PR.get_active_projects_line()
        if pl:
            parts.append("[" + pl + "]")
    except Exception:
        pass

    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def _learn_async(user_msg: str, assistant_msg: str, session_id: str = "") -> None:
    """Extract durable facts + an episodic summary from a chat turn (throttled, in a
    daemon thread) — the same learner the voice agent uses, now wired into chat too."""
    try:
        from config import CONFIG
        from memory.memory_manager import update_from_conversation_async
        update_from_conversation_async(
            CONFIG.memory_file, CONFIG.gemini_api_key, user_msg, assistant_msg,
            minimax_api_key=getattr(CONFIG, "minimax_api_key", ""),
            minimax_base_url=getattr(CONFIG, "minimax_base_url", ""),
            minimax_model=getattr(CONFIG, "minimax_model", ""),
            session_id=session_id,
        )
    except Exception as e:
        logger.warning(f"chat learn failed: {e}")


# ── History helpers (persistent — see conversation_store.py) ──────────────────

def _get_history(session_id: str) -> list:
    import conversation_store as convo
    return convo.history_for_model(session_id, _MAX_HISTORY)


def reset_session(session_id: str) -> None:
    import conversation_store as convo
    convo.clear(session_id)


# ── Tool execution ────────────────────────────────────────────────────────────

# Matches an absolute Windows path ending in a file extension, e.g. C:\Users\me\a.py
import re as _re
_PATH_RE = _re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]+\.[A-Za-z0-9]{1,8}")
# Tool-argument keys that commonly hold a file path.
_PATH_ARG_KEYS = ("path", "destination", "filepath", "file", "filename", "dest")


def _collect_files(name: str, args: dict, result: str, files: list) -> None:
    """Record real on-disk files this tool touched, so the UI can link/open them.

    Looks at both the tool's arguments (the reliable source — the model passed the
    path) and any absolute path echoed in the result string. Only paths that exist
    as files are kept; results are de-duplicated, order-preserving.
    """
    candidates: list[str] = []
    for k in _PATH_ARG_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())
    if isinstance(result, str):
        candidates.extend(_PATH_RE.findall(result))

    seen = {f["path"].lower() for f in files}
    for c in candidates:
        try:
            if not os.path.isfile(c):
                continue
            full = os.path.abspath(c)
        except (OSError, ValueError):
            continue
        if full.lower() in seen:
            continue
        seen.add(full.lower())
        files.append({"path": full, "name": os.path.basename(full)})


# ── Approval gate (the user approves file/command changes before they apply) ──

_READONLY_FILE_OPS = {"list", "read", "exists", "info", "search", "open"}


def _needs_approval(name: str, args: dict) -> bool:
    """True if this tool mutates the system and should require explicit approval."""
    from core.command_router import REQUIRES_CONFIRMATION
    if name in REQUIRES_CONFIRMATION:
        return True
    if name == "run_command":
        return True
    if name == "file_op":
        return str(args.get("action", "")).lower().strip() not in _READONLY_FILE_OPS
    return False


def _file_diff(path: str, new: str) -> str:
    """A short unified diff of a proposed file write (or a preview for a new file)."""
    import difflib
    old = ""
    try:
        if path and os.path.isfile(path):
            old = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        old = ""
    new = str(new or "")
    if not old:
        return "(new file)\n" + new[:2000]
    diff = "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="",
        fromfile="current", tofile="proposed", n=2))
    return diff[:6000] if diff else "(no change)"


def _action_preview(name: str, args: dict) -> dict:
    """Human-facing summary of a proposed action for the approval card."""
    if name == "run_command":
        return {"kind": "command", "summary": "Run a shell command", "command": args.get("task", "")}
    if name == "file_op":
        action = str(args.get("action", "")).lower().strip()
        path = args.get("path", "") or args.get("destination", "")
        if action == "write":
            return {"kind": "file", "summary": f"Write {path}", "path": path,
                    "diff": _file_diff(path, args.get("content", ""))}
        if action in ("delete",):
            return {"kind": "delete", "summary": f"Delete {path}", "path": path}
        dest = args.get("destination", "")
        return {"kind": "file", "summary": f"{action} {path}" + (f" → {dest}" if dest else ""),
                "path": path}
    if name == "delete_file":
        return {"kind": "delete", "summary": f"Delete {args.get('path', '')}", "path": args.get("path", "")}
    return {"kind": "action", "summary": name.replace("_", " ")}


def _emit(emit, event: dict) -> None:
    """Best-effort progress callback (used by the streaming chat path)."""
    if emit:
        try:
            emit(event)
        except Exception:
            pass


def _run_tool(router, name: str, args: dict, allow_actions: bool,
              used: list, files: list, approval: bool = False, pending: list = None,
              emit=None) -> str:
    from core.command_router import REQUIRES_CONFIRMATION

    used.append(name)
    _emit(emit, {"type": "tool_start", "name": name, "args": _args_summary(args),
                 "note": _tool_activity(name, args)})

    def done(result: str, status: str = "ok") -> str:
        _emit(emit, {"type": "tool_end", "name": name, "status": status,
                     "summary": _result_summary(result)})
        return result

    safe, reason = router._safety_check(name, args)
    if not safe:
        return done(f"[blocked for security: {reason}]", "blocked")

    # Approval mode: queue mutating/destructive actions for the user to approve,
    # instead of running them. Read-only tools still run normally.
    if approval and pending is not None and _needs_approval(name, args):
        import uuid
        action = {"id": "act-" + uuid.uuid4().hex[:8], "tool": name, "args": args}
        action.update(_action_preview(name, args))
        pending.append(action)
        return done(
            f"[NOT EXECUTED — this action ({name}) is only PROPOSED and is waiting for the "
            f"user to click Approve in the UI (id {action['id']}). Nothing has changed on "
            "disk or anywhere yet. You MUST tell the user it is pending their approval. Do "
            "NOT say it is saved/done/created — that would be a lie. The user saying 'yes' "
            "in chat does NOT approve it; only the Approve button does.]", "proposed")

    if name in REQUIRES_CONFIRMATION and not allow_actions:
        return done(
            f"[blocked] '{name}' is a sensitive action. The user must enable "
            "'Allow actions' in the chat UI before this can run.", "blocked")
    try:
        result = str(router._dispatch_module(name, args))
        _collect_files(name, args, result, files)
        # A sensitive action that actually RAN (approval off / allow_actions on) must
        # not be reported as pending — weak models otherwise echo an earlier turn's
        # "awaiting approval" framing. Mark it unambiguously as completed.
        if name in REQUIRES_CONFIRMATION and not str(result).startswith(("[error", "[blocked")):
            return done("[ACTION COMPLETED — this really executed; it is NOT pending and needs "
                        "no approval. Report it as DONE.] " + result)
        return done(result)
    except Exception as e:
        logger.error(f"chat tool error {name}: {e}", exc_info=True)
        return done(f"[error running {name}: {e}]", "error")


def _args_summary(args: dict) -> str:
    """A compact, safe one-line preview of tool args for the activity view."""
    try:
        parts = []
        for k, v in (args or {}).items():
            s = str(v).replace("\n", " ")
            parts.append(f"{k}={s[:60]}" + ("…" if len(s) > 60 else ""))
        return ", ".join(parts)[:200]
    except Exception:
        return ""


def _result_summary(result: str) -> str:
    return (str(result).replace("\n", " ")[:200]).strip()


def _tool_activity(name: str, args: dict) -> str:
    """A short, present-tense note on what Miko is doing right now, for the activity
    card header (e.g. 'Creating app.py', 'Modifying styles.css', 'Reading config')."""
    args = args or {}

    def base(*keys):
        for k in keys:
            v = str(args.get(k, "") or "").strip()
            if v:
                return os.path.basename(v.rstrip("/\\")) or v
        return ""

    if name == "file_op":
        action = str(args.get("action", "")).lower().strip()
        f = base("path", "destination")
        if action == "write":
            path = args.get("path", "") or args.get("destination", "")
            verb = "Modifying" if (path and os.path.isfile(path)) else "Creating"
            return f"{verb} {f}" if f else verb
        # actions whose label is already a full phrase (no filename to append)
        whole = {"list": "Listing files", "search": "Searching files"}
        if action in whole:
            return whole[action]
        labels = {"read": "Reading", "open": "Reading", "delete": "Deleting",
                  "move": "Moving", "rename": "Renaming", "copy": "Copying",
                  "append": "Updating", "mkdir": "Creating folder",
                  "exists": "Checking", "info": "Inspecting"}
        verb = labels.get(action, action.capitalize() or "Working on")
        return f"{verb} {f}".strip()
    if name == "run_command":
        task = str(args.get("task", "") or "").replace("\n", " ").strip()
        return ("Running " + task[:48] + ("…" if len(task) > 48 else "")) if task else "Running a command"
    if name in ("create_note", "write_note"):
        return f"Writing note {base('title', 'name', 'path')}".strip()
    if name == "read_note":
        return f"Reading note {base('title', 'name', 'path')}".strip()
    if name in ("deep_research", "research", "research_topic"):
        topic = str(args.get("topic", "") or args.get("query", "")).strip()
        return ("Researching " + topic[:48]) if topic else "Researching"
    if name in ("search_web", "web_search", "browse"):
        q = str(args.get("query", "") or args.get("url", "")).strip()
        return ("Searching " + q[:48]) if q else "Searching the web"
    if name == "code_with_claude":
        return "Coding with Claude"
    if name == "spawn_agents":
        return "Launching sub-agents"
    if name == "add_project":
        return f"Mapping {base('name', 'path')}".strip()
    if name == "import_memories":
        return "Importing memory"
    if name == "recall":
        return "Recalling memory"
    return name.replace("_", " ").capitalize()


# ── Public entry point ────────────────────────────────────────────────────────

# "exhaustive" is a research tier; in plain chat it behaves like "deep".
_EFFORT_ROUNDS = {"quick": 3, "standard": 6, "deep": 8, "exhaustive": 8}

# Native reasoning-effort mapping, per provider (only for models that support it).
_EFFORT_LEVEL = {"quick": "low", "standard": "medium", "deep": "high", "exhaustive": "high"}
_EFFORT_GEMINI_BUDGET = {"quick": 0, "standard": -1, "deep": 12000, "exhaustive": 12000}   # -1 = dynamic


def _reasoning_kwargs(protocol: str, model: str, effort: str) -> dict:
    """Extra API kwargs that pass the chosen effort as the provider's NATIVE reasoning
    parameter — only for models known to support it. Empty dict otherwise."""
    m = (model or "").lower()
    if protocol == "openai":
        if m.startswith(("o1", "o3", "o4")) or m.startswith("gpt-5"):
            return {"reasoning_effort": _EFFORT_LEVEL.get(effort, "medium")}
    elif protocol == "anthropic":
        if "claude" in m:   # real Claude (not MiniMax via /anthropic)
            return {"output_config": {"effort": _EFFORT_LEVEL.get(effort, "medium")}}
    return {}


def _thinking_kwargs(protocol: str, model: str, thinking: bool) -> dict:
    """Native reasoning/thinking toggle. On the Anthropic wire both real Claude AND
    MiniMax M-series accept a `thinking` block — MiniMax M3 ships with thinking OFF by
    default, which makes it sloppy at tool routing, so enabling adaptive thinking is the
    fix. Returns {} when the toggle is off (provider default) or unsupported."""
    if protocol != "anthropic":
        return {}   # gemini handled via thinking_budget; openai via reasoning_effort
    if thinking:
        return {"thinking": {"type": "adaptive"}}
    m = (model or "").lower()
    if m.startswith("minimax") or m.startswith("abab"):
        return {"thinking": {"type": "disabled"}}   # be explicit for MiniMax when off
    return {}


def _create_safe(fn, base_kwargs: dict, extra: dict):
    """Call an SDK create() with reasoning kwargs; if the provider rejects them
    (unsupported model/endpoint), retry once without — so nothing ever breaks."""
    if not extra:
        return fn(**base_kwargs)
    try:
        return fn(**base_kwargs, **extra)
    except Exception as e:
        logger.warning(f"reasoning param rejected ({e}); retrying without it")
        return fn(**base_kwargs)


def _is_tool_support_error(exc: Exception) -> bool:
    """Provider says this model/route cannot accept tool declarations."""
    msg = str(exc).lower()
    return (
        ("tool" in msg and "support" in msg)
        or "no endpoints found that support tool use" in msg
        or "tools are not supported" in msg
        or "tool use is not supported" in msg
    )


def _openai_response_detail(resp) -> str:
    """Best-effort detail from OpenAI-compatible responses that lack choices."""
    if resp is None:
        return "empty response"
    data = {}
    try:
        if isinstance(resp, dict):
            data = resp
        elif hasattr(resp, "model_dump"):
            data = resp.model_dump(exclude_none=True)
        elif hasattr(resp, "__dict__"):
            data = dict(resp.__dict__)
    except Exception:
        data = {}

    err = data.get("error") if isinstance(data, dict) else None
    if err:
        if isinstance(err, dict):
            return str(err.get("message") or err.get("detail") or err)
        return str(err)
    for key in ("message", "detail", "warning"):
        val = data.get(key) if isinstance(data, dict) else None
        if val:
            return str(val)
    return f"response type {type(resp).__name__}"


def _openai_first_message(resp, provider: str, model: str):
    choices = getattr(resp, "choices", None)
    if choices is None and isinstance(resp, dict):
        choices = resp.get("choices")
    if not choices:
        detail = _openai_response_detail(resp)
        raise RuntimeError(
            f"{provider or 'OpenAI-compatible provider'}/{model or '(default)'} "
            f"returned no choices: {detail}"
        )
    msg = getattr(choices[0], "message", None)
    if msg is None and isinstance(choices[0], dict):
        msg = choices[0].get("message")
    if msg is None:
        raise RuntimeError(
            f"{provider or 'OpenAI-compatible provider'}/{model or '(default)'} "
            "returned a choice without a message."
        )
    return msg


def _openai_msg_content(msg) -> str:
    if isinstance(msg, dict):
        return msg.get("content") or ""
    return getattr(msg, "content", "") or ""


def _openai_msg_tool_calls(msg) -> list:
    if isinstance(msg, dict):
        return msg.get("tool_calls") or []
    return getattr(msg, "tool_calls", None) or []


def _is_rate_limit_error(exc: Exception) -> bool:
    """Provider quota/rate-limit failure that can be safely retried elsewhere."""
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    return (
        status == 429
        or code == 429
        or "rate limit" in msg
        or "ratelimit" in msg
        or "quota" in msg
        or "free-models-per-day" in msg
    )


def _mcp_tools(protocol: str) -> list:
    """Tool declarations for any connected MCP servers, in the given protocol's format.
    Returns [] if MCP isn't configured/available — so it just adds to the base tool set."""
    try:
        import modules.mcp_client as MC
        base = MC.get_tool_declarations()   # Gemini-format
        if not base:
            return []
        if protocol == "openai":
            from tools import _to_openai_tool
            return [_to_openai_tool(d) for d in base]
        if protocol == "anthropic":
            from tools import _to_anthropic_tool
            return [_to_anthropic_tool(d) for d in base]
        return base
    except Exception:
        return []


# ── Attachments (images / files the user sends with a message) ────────────────

_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".py", ".js",
    ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".xml", ".yaml", ".yml",
    ".ini", ".cfg", ".toml", ".sh", ".bat", ".ps1", ".java", ".c", ".cpp", ".h",
    ".hpp", ".go", ".rs", ".rb", ".php", ".sql", ".env", ".gitignore",
}


def _supports_vision(provider: str, model: str, base_url: str = "") -> bool:
    """Whether the selected model can natively take image input. Cautious: only
    say yes where we're confident (Gemini, real Claude, OpenAI's 4o/o-series).
    Everything else (MiniMax, generic OpenAI-compatible) gets the describe fallback."""
    m = (model or "").lower()
    b = (base_url or "").lower()
    if "minimax" in b or m.startswith(("minimax", "abab")):
        return False
    if provider == "gemini":
        return True
    if provider == "anthropic":
        return m.startswith("claude")          # claude-3+ are all vision-capable
    # openai-compatible
    if not b or "api.openai.com" in b:
        return any(t in m for t in ("4o", "4.1", "4-turbo", "o1", "o3", "o4", "gpt-5"))
    return False


def _strip_xml(xml: str) -> str:
    import html as _html
    text = _re.sub(r"<[^>]+>", "", xml)
    text = _html.unescape(text)
    text = _re.sub(r"[ \t]+", " ", text)
    text = _re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text)
    return text.strip()


def _extract_document_text(raw: bytes, name: str, mime: str) -> str:
    """Pull readable text out of Office/OpenDocument files (all ZIP+XML, so no extra
    deps) — .docx/.pptx/.xlsx/.odt/.ods/.odp — and .rtf. Returns '' if unsupported."""
    import io
    import zipfile
    ext = os.path.splitext(name or "")[1].lower()
    try:
        if ext == ".rtf" or mime == "application/rtf":
            txt = raw.decode("latin-1", "replace")
            txt = _re.sub(r"\\'[0-9a-fA-F]{2}", "", txt)
            txt = _re.sub(r"\\[a-zA-Z]+-?\d* ?", "", txt)
            txt = txt.replace("{", "").replace("}", "")
            return _re.sub(r"\n\s*\n\s*\n+", "\n\n", txt).strip()
        if not zipfile.is_zipfile(io.BytesIO(raw)):
            return ""
        zf = zipfile.ZipFile(io.BytesIO(raw))
        names = zf.namelist()
        parts = []
        if "word/document.xml" in names:                       # .docx
            xml = zf.read("word/document.xml").decode("utf-8", "replace").replace("</w:p>", "\n")
            parts.append(_strip_xml(xml))
        elif any(n.startswith("ppt/slides/slide") for n in names):   # .pptx
            for n in sorted(n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")):
                xml = zf.read(n).decode("utf-8", "replace").replace("</a:p>", "\n")
                parts.append(_strip_xml(xml))
        elif "xl/sharedStrings.xml" in names:                  # .xlsx (cell strings)
            xml = zf.read("xl/sharedStrings.xml").decode("utf-8", "replace").replace("</si>", "\n")
            parts.append(_strip_xml(xml))
        elif "content.xml" in names:                           # ODF (.odt/.ods/.odp)
            xml = zf.read("content.xml").decode("utf-8", "replace")
            xml = _re.sub(r"</text:(p|h)>", "\n", xml)
            parts.append(_strip_xml(xml))
        return "\n".join(p for p in parts if p).strip()
    except Exception as e:
        logger.warning(f"document extract failed for {name}: {e}")
        return ""


def _extract_pdf_text(raw: bytes) -> str:
    """Best-effort PDF text via an optional lib; '' if none is installed."""
    try:
        import io
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages).strip()
    except Exception as e:
        logger.debug(f"pdf extract failed: {e}")
        return ""


def _is_textual(mime: str, name: str) -> bool:
    if (mime or "").startswith("text/"):
        return True
    if mime in ("application/json", "application/xml", "application/javascript"):
        return True
    return os.path.splitext(name or "")[1].lower() in _TEXT_EXT


def _describe_image(raw: bytes, mime: str) -> str:
    """Fallback for models that can't see: caption an image via Gemini so a
    text-only model still gets its content. Returns '' if no Gemini key."""
    try:
        from config import CONFIG
        gkey = getattr(CONFIG, "gemini_api_key", "") or os.getenv("LLM_API_KEY", "")
    except Exception:
        gkey = os.getenv("LLM_API_KEY", "")
    if not gkey:
        return ""
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=gkey)
        resp = client.models.generate_content(
            model=os.getenv("MIKO_DICTATION_MODEL", "").strip() or "gemini-2.5-flash",
            contents=[types.Part.from_bytes(data=raw, mime_type=mime),
                      types.Part(text="Describe this image thoroughly for someone who can't "
                                      "see it. Transcribe any visible text verbatim.")],
        )
        return (resp.text or "").strip()
    except Exception as e:
        logger.warning(f"image describe fallback failed: {e}")
        return ""


_ATT_OVERVIEW = 8000   # chars of extracted text kept per attachment for display + history


def _prepare_attachments(attachments, provider, model, base_url, message):
    """Split user attachments into native `media` (for vision models) and text the
    model can read. Returns (media, augmented_message, meta). `meta` is a per-file
    list of {name, mime, kind, overview} for the UI (chip + click-to-expand) and for
    re-folding context into history — so the visible bubble stays a clean chip rather
    than the whole extracted document. Images on a non-vision model are captioned via
    Gemini; text files are inlined; unreadable types get a note."""
    media, extra, meta = [], [], []
    can_see = _supports_vision(provider, model, base_url)

    def record(name, mime, kind, overview=""):
        meta.append({"name": name, "mime": mime, "kind": kind,
                     "overview": (overview or "")[:_ATT_OVERVIEW]})

    for att in (attachments or []):
        name = att.get("name") or "file"
        mime = (att.get("mime") or "").lower()
        try:
            raw = base64.b64decode(att.get("data") or "")
        except Exception:
            continue
        if not raw:
            continue
        if mime.startswith("image/"):
            if can_see:
                media.append({"mime": mime, "data": raw})
                record(name, mime, "image", "")   # the model sees the image directly
            else:
                desc = _describe_image(raw, mime)
                extra.append(
                    f"[Attached image '{name}' — the current model can't view images, so here "
                    f"is a description:\n{desc}]" if desc else
                    f"[User attached image '{name}', but this model can't view images "
                    f"and no image reader is configured.]")
                record(name, mime, "image", desc)
        elif mime == "application/pdf":
            if provider == "gemini" and can_see:
                media.append({"mime": mime, "data": raw})     # Gemini reads PDFs natively
                record(name, mime, "pdf", "")
            else:
                txt = _extract_pdf_text(raw)
                extra.append(f"[Attached PDF '{name}']\n```\n{txt[:20000]}\n```" if txt else
                             f"[User attached PDF '{name}', but no PDF reader is available "
                             f"(pip install pypdf) and this model can't read PDFs.]")
                record(name, mime, "pdf", txt)
        elif _is_textual(mime, name):
            txt = raw.decode("utf-8", errors="replace")
            full = txt
            if len(txt) > 20000:
                txt = txt[:20000] + "\n… (truncated)"
            extra.append(f"[Attached file '{name}']\n```\n{txt}\n```")
            record(name, mime, "text", full)
        else:
            doc = _extract_document_text(raw, name, mime)
            if doc:
                full = doc
                if len(doc) > 20000:
                    doc = doc[:20000] + "\n… (truncated)"
                extra.append(f"[Attached document '{name}' — extracted text]\n```\n{doc}\n```")
                record(name, mime, "document", full)
            else:
                extra.append(f"[User attached '{name}' ({mime or 'unknown type'}); this model "
                             f"can't read this file type.]")
                record(name, mime, "unreadable", "")
    if extra:
        message = (message + "\n\n" + "\n\n".join(extra)).strip()
    return media, message, meta


def chat(router, session_id: str, message: str, provider: str, model: str,
         api_key: str = "", base_url: str = "", allow_actions: bool = False,
         owner_name: str = "Roxan", language: str = "en", workspace: str = "",
         agent: str = "", skills=None, effort: str = "standard", approval: bool = False,
         thinking: bool = False, attachments=None, system_extra: str = "",
         emit=None, should_cancel=None, compact_tools: bool = False,
         fallback_provider: str = "", fallback_model: str = "",
         fallback_api_key: str = "", fallback_base_url: str = "") -> dict:
    """Run one chat turn. Returns {"reply": str, "tools_used": [...], "error": str|None}.

    emit: optional callback(event_dict) for live progress (tool_start/tool_end/round).
    should_cancel: optional zero-arg callable; the tool-loop stops when it returns True.
    """
    preset = PROVIDERS.get(provider)
    if not preset:
        return {"reply": "", "tools_used": [], "error": f"Unknown provider '{provider}'."}

    key = (api_key or "").strip() or os.getenv(preset["env_key"], "")
    if not key:
        return {"reply": "", "tools_used": [],
                "error": f"No API key for {preset['label']}. Enter one in the UI or set "
                         f"{preset['env_key']} in .env."}

    base = (base_url or "").strip() or preset["base_url"]
    if not model:
        model = (preset["models"] or [""])[0]
    if not model:
        return {"reply": "", "tools_used": [], "error": "No model specified."}

    # Tie any sub-agents Miko spawns this turn to the conversation that launched them,
    # so they show up under the right chat in the sub-agent panel. (Sub-agent sessions
    # themselves can't spawn, so they never reach launch().)
    if not session_id.startswith("subagent-"):
        try:
            from modules import agent_jobs
            agent_jobs.set_current_session(session_id)
            agent_jobs.set_current_model(provider, model, key, base)
        except Exception:
            pass

    # "Whose assistant am I" follows the remembered identity name, so the base
    # prompt agrees with memory after the user corrects their name.
    try:
        from config import CONFIG as _C
        from memory.memory_manager import load_memory as _lm
        _nm = _lm(_C.memory_file).get("identity", {}).get("name", {}).get("value")
        if _nm:
            owner_name = _nm
    except Exception:
        pass

    system = _system_prompt(owner_name, language, workspace)
    # Give chat the current date/time (voice already has it) so trivial "what time is
    # it" questions are answered directly instead of reaching for run_command.
    from datetime import datetime as _dt
    system = f"[CURRENT DATE & TIME]\nIt is now {_dt.now():%A, %d %B %Y, %H:%M} (local time).\n\n" + system
    if agent or skills:
        try:
            import agent_skills
            overlay = agent_skills.build_overlay(agent, skills)
            if overlay:
                system += overlay
        except Exception as e:
            logger.error(f"agent/skills overlay failed: {e}")

    # Inject long-term memory + semantically relevant vault notes.
    try:
        system += _memory_context(message)
    except Exception as e:
        logger.warning(f"memory context failed: {e}")

    # Caller-specific framing (e.g. the voice pipeline appends spoken-style rules).
    if system_extra:
        system += "\n\n" + system_extra.strip()

    history = _get_history(session_id)
    used: list = []
    files: list = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    rounds = _EFFORT_ROUNDS.get(effort, _MAX_ROUNDS)   # effort → tool-call budget
    pending: list = []                                 # actions awaiting approval

    # Fold user attachments into native image input + readable text (capability-aware).
    # Keep the user's ORIGINAL message for the visible transcript; the extracted text
    # only goes to the model + is stored as a per-file overview (shown on click), so a
    # long CV doesn't fill the conversation bubble.
    orig_message = message
    media, att_meta = [], []
    if attachments:
        try:
            media, message, att_meta = _prepare_attachments(attachments, provider, model, base, message)
        except Exception as e:
            logger.warning(f"attachment processing failed: {e}")

    # Persist the user's message NOW (history was already captured above, so the model
    # doesn't see a duplicate). This makes the conversation appear + survive immediately,
    # even when the turn runs long (a pair session) or errors before finishing.
    ephemeral = session_id.startswith("subagent-")
    if not ephemeral:
        try:
            import conversation_store as _convo
            _convo.append_user(session_id, orig_message, att_meta)
        except Exception as e:
            logger.warning(f"persist user turn failed: {e}")

    def _run_selected(run_provider: str, run_model: str, run_key: str, run_base: str) -> str:
        run_preset = PROVIDERS.get(run_provider)
        if not run_preset:
            raise RuntimeError(f"Unknown fallback provider '{run_provider}'.")
        run_key = (run_key or "").strip() or os.getenv(run_preset["env_key"], "")
        if not run_key:
            raise RuntimeError(f"No API key for {run_preset['label']} fallback.")
        run_base = (run_base or "").strip() or run_preset["base_url"]
        run_model = run_model or (run_preset["models"] or [""])[0]
        if not run_model:
            raise RuntimeError(f"No fallback model specified for {run_preset['label']}.")

        if run_preset["protocol"] == "gemini":
            return _run_gemini(router, run_key, run_model, system, history, message, allow_actions, used, files, usage, rounds, effort, approval, pending, emit, should_cancel, thinking, media=media)
        if run_preset["protocol"] == "anthropic":
            return _run_anthropic(router, run_key, run_base, run_model, system, history, message, allow_actions, used, files, usage, rounds, effort, approval, pending, emit, should_cancel, thinking, media=media)
        return _run_openai(router, run_key, run_base, run_model, system, history, message, allow_actions, used, files, usage, rounds, effort, approval, pending, emit, should_cancel, media=media, compact_tools=compact_tools)

    def _failed(err: Exception) -> dict:
        logger.error(f"chat() error ({provider}/{model}): {err}", exc_info=True)
        # The user turn is already saved; record the failure so the conversation isn't
        # left as a lone user message.
        if not ephemeral:
            try:
                import conversation_store as _convo
                _convo.append_assistant(session_id, f"⚠ {err}", used, files)
            except Exception:
                pass
        return {"reply": "", "tools_used": used, "files": files, "usage": usage,
                "pending": pending, "error": str(err)}

    try:
        if preset["protocol"] == "gemini":
            reply = _run_gemini(router, key, model, system, history, message, allow_actions, used, files, usage, rounds, effort, approval, pending, emit, should_cancel, thinking, media=media)
        elif preset["protocol"] == "anthropic":
            reply = _run_anthropic(router, key, base, model, system, history, message, allow_actions, used, files, usage, rounds, effort, approval, pending, emit, should_cancel, thinking, media=media)
        else:
            reply = _run_openai(router, key, base, model, system, history, message, allow_actions, used, files, usage, rounds, effort, approval, pending, emit, should_cancel, media=media, compact_tools=compact_tools)
    except Exception as e:
        fp = (fallback_provider or "").strip().lower()
        if not (fp and fp != provider and _is_rate_limit_error(e)):
            return _failed(e)
        try:
            logger.warning(
                f"{provider}/{model} rate-limited; retrying with "
                f"{fp}/{fallback_model or '(default)'}"
            )
            reply = _run_selected(fp, fallback_model, fallback_api_key, fallback_base_url)
            provider, model = fp, fallback_model or (PROVIDERS[fp]["models"] or [model])[0]
            preset = PROVIDERS[fp]
        except Exception as fe:
            logger.error(f"fallback chat() error ({fp}/{fallback_model}): {fe}", exc_info=True)
            return _failed(fe)

    cancelled = _cancelled(should_cancel) or reply == "(cancelled)"
    # The user message was persisted at the start of the turn; now attach the reply.
    # Sub-agent sessions are internal — never persisted or learned from.
    if not ephemeral:
        try:
            import conversation_store as _convo
            _convo.append_assistant(session_id, reply or "(stopped)", used, files)
        except Exception as e:
            logger.warning(f"persist assistant turn failed: {e}")
        if not cancelled:
            _learn_async(message, reply, session_id)   # learn facts + episode (throttled)
    return {"reply": reply, "tools_used": used, "files": files, "usage": usage,
            "pending": pending, "attachments": att_meta, "cancelled": cancelled, "error": None}


def _cancelled(should_cancel) -> bool:
    try:
        return bool(should_cancel and should_cancel())
    except Exception:
        return False


def chat_stream(*args, should_cancel=None, **kwargs):
    """Generator wrapper around chat() that yields live progress events.

    Yields {"type": "tool_start"|"tool_end"|"round", ...} as the turn runs, then a
    final {"type": "reply", ...} (or {"type": "cancelled"} / {"type": "error"}).
    Runs chat() in a worker thread and bridges its emit() callback through a queue,
    so the runners stay simple synchronous functions.
    """
    import queue as _queue
    import threading

    q: _queue.Queue = _queue.Queue()
    _SENTINEL = object()
    result_box = {}

    def _emit_cb(ev):
        q.put(ev)

    def _worker():
        try:
            result_box["result"] = chat(*args, emit=_emit_cb,
                                        should_cancel=should_cancel, **kwargs)
        except Exception as e:
            result_box["error"] = str(e)
        finally:
            q.put(_SENTINEL)

    t = threading.Thread(target=_worker, daemon=True, name="ChatStream")
    t.start()

    while True:
        ev = q.get()
        if ev is _SENTINEL:
            break
        yield ev

    if "error" in result_box:
        yield {"type": "error", "error": result_box["error"]}
        return
    res = result_box.get("result", {}) or {}
    if res.get("cancelled"):
        yield {"type": "cancelled", **{k: res.get(k) for k in
               ("tools_used", "files", "usage", "pending")}}
        return
    yield {"type": "reply", "reply": res.get("reply", ""),
           "tools_used": res.get("tools_used", []), "files": res.get("files", []),
           "usage": res.get("usage", {}), "pending": res.get("pending", []),
           "attachments": res.get("attachments", []), "error": res.get("error")}


def _accum_usage(usage: dict, resp, proto: str) -> None:
    """Add a provider response's token counts into the running per-turn total."""
    try:
        if proto == "openai":
            u = getattr(resp, "usage", None)
            if u:
                usage["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                usage["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0
        elif proto == "anthropic":
            u = getattr(resp, "usage", None)
            if u:
                usage["prompt_tokens"] += getattr(u, "input_tokens", 0) or 0
                usage["completion_tokens"] += getattr(u, "output_tokens", 0) or 0
        elif proto == "gemini":
            u = getattr(resp, "usage_metadata", None)
            if u:
                usage["prompt_tokens"] += getattr(u, "prompt_token_count", 0) or 0
                usage["completion_tokens"] += getattr(u, "candidates_token_count", 0) or 0
    except Exception:
        pass
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]


# ── OpenAI-compatible (OpenAI, OpenRouter, NVIDIA, DeepSeek, Kimi, custom) ─────

_VOICE_TOOL_NAMES = {
    "set_volume", "media_control", "get_volume", "open_app", "set_reminder",
    "system_info", "take_screenshot", "clipboard", "window_control",
    "process_manager", "calculate", "world_time", "type_text", "send_shortcut",
    "remember", "recall", "create_note", "read_note", "list_notes", "search_notes",
    "send_discord_dm", "send_discord_channel", "call_discord", "join_voice",
    "leave_voice", "speak_on_discord", "get_dm_history", "join_voice_as_me",
    "leave_voice_as_me", "set_my_voice", "get_today_events", "list_events",
    "create_event", "schedule_task", "list_scheduled_tasks", "cancel_scheduled_task",
    "list_emails", "read_email", "show_email", "open_email_link", "search_emails", "triage_inbox",
    "watch_email", "list_email_watches", "cancel_email_watch",
    "weather", "web_search", "open_url",
}


def _compact_openai_tools(tools: list) -> list:
    return [
        t for t in (tools or [])
        if ((t.get("function") or {}).get("name") in _VOICE_TOOL_NAMES)
    ]


def _openai_extra_body(base: str, model: str) -> dict:
    model_l = (model or "").lower()
    base_l = (base or "").lower()
    if "integrate.api.nvidia.com" in base_l and model_l == "google/diffusiongemma-26b-a4b-it":
        return {"chat_template_kwargs": {"enable_thinking": True}}
    return {}


def _run_openai(router, key, base, model, system, history, message, allow_actions, used, files, usage=None, rounds=_MAX_ROUNDS, effort="standard", approval=False, pending=None, emit=None, should_cancel=None, media=None, compact_tools=False) -> str:
    from openai import OpenAI
    from tools import ALL_TOOL_DECLARATIONS_OPENAI

    client = OpenAI(api_key=key, base_url=base or None)
    messages = [{"role": "system", "content": system}]
    messages += [{"role": m["role"], "content": m["content"]} for m in history]
    if media:
        content = [{"type": "text", "text": message}]
        for mm in media:
            b64 = base64.b64encode(mm["data"]).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{mm['mime']};base64,{b64}"}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": message})
    tools = ALL_TOOL_DECLARATIONS_OPENAI + ([] if compact_tools else _mcp_tools("openai"))
    if compact_tools:
        tools = _compact_openai_tools(tools)
        logger.info(f"compact OpenAI tool set enabled ({len(tools)} tools)")
    tools = tools or None
    rk = _reasoning_kwargs("openai", model, effort)   # native reasoning_effort (if supported)
    extra_body = _openai_extra_body(base, model)

    def _run_plain_no_tools() -> str:
        messages[0]["content"] += (
            "\n\n[NO TOOLS AVAILABLE]\n"
            "The selected model endpoint does not support tool/function calls. "
            "Answer conversationally only. Do not claim to run actions, change "
            "settings, send messages, read files, or use external tools."
        )
        plain = _create_safe(
            client.chat.completions.create,
            {"model": model, "messages": messages, **({"extra_body": extra_body} if extra_body else {})},
            rk,
        )
        plain_msg = _openai_first_message(plain, base or "openai", model)
        if usage is not None:
            _accum_usage(usage, plain, "openai")
        return _openai_msg_content(plain_msg).strip() or "(no response)"

    for _r in range(rounds):
        if _cancelled(should_cancel):
            return "(cancelled)"
        _emit(emit, {"type": "round", "n": _r + 1})
        kwargs = {"model": model, "messages": messages}
        if extra_body:
            kwargs["extra_body"] = extra_body
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            resp = _create_safe(client.chat.completions.create, kwargs, rk)
        except Exception as e:
            if tools and _is_tool_support_error(e):
                logger.warning(
                    "model rejected tool declarations; retrying this turn without tools "
                    f"({model})"
                )
                return _run_plain_no_tools()
            raise
        try:
            msg = _openai_first_message(resp, base or "openai", model)
        except RuntimeError as e:
            if tools and _is_tool_support_error(e):
                logger.warning(
                    "model returned a tool-support error without choices; retrying "
                    f"this turn without tools ({model})"
                )
                return _run_plain_no_tools()
            raise
        if usage is not None:
            _accum_usage(usage, resp, "openai")
        tool_calls = _openai_msg_tool_calls(msg)

        if not tool_calls:
            return _openai_msg_content(msg).strip() or "(no response)"

        messages.append({
            "role": "assistant",
            "content": _openai_msg_content(msg),
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            result = _run_tool(router, tc.function.name, args, allow_actions, used, files, approval, pending, emit)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    final = _create_safe(client.chat.completions.create, {"model": model, "messages": messages}, rk)
    final_msg = _openai_first_message(final, base or "openai", model)
    if usage is not None:
        _accum_usage(usage, final, "openai")
    return _openai_msg_content(final_msg).strip() or "(done)"


# ── Anthropic-compatible (MiniMax /anthropic) ────────────────────────────────

def _run_anthropic(router, key, base, model, system, history, message, allow_actions, used, files, usage=None, rounds=_MAX_ROUNDS, effort="standard", approval=False, pending=None, emit=None, should_cancel=None, thinking=False, media=None) -> str:
    import anthropic
    from tools import ALL_TOOL_DECLARATIONS_ANTHROPIC

    client = anthropic.Anthropic(api_key=key, base_url=base or None)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    if media:
        content = [{"type": "text", "text": message}]
        for mm in media:
            b64 = base64.b64encode(mm["data"]).decode()
            if mm["mime"] == "application/pdf":
                content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}})
            else:
                content.append({"type": "image", "source": {"type": "base64", "media_type": mm["mime"], "data": b64}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": message})
    tools = (ALL_TOOL_DECLARATIONS_ANTHROPIC + _mcp_tools("anthropic")) or None
    rk = {**_reasoning_kwargs("anthropic", model, effort),       # real-Claude effort (if supported)
          **_thinking_kwargs("anthropic", model, thinking)}      # thinking toggle (MiniMax M3 + Claude)

    for _r in range(rounds):
        if _cancelled(should_cancel):
            return "(cancelled)"
        _emit(emit, {"type": "round", "n": _r + 1})
        kwargs = {"model": model, "max_tokens": 4096, "system": system, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        resp = _create_safe(client.messages.create, kwargs, rk)
        if usage is not None:
            _accum_usage(usage, resp, "anthropic")

        tool_use = [b for b in resp.content if b.type == "tool_use"]
        if not tool_use:
            texts = [b.text for b in resp.content if hasattr(b, "text")]
            return " ".join(texts).strip() or "(no response)"

        assistant_content = []
        for b in resp.content:
            if b.type == "text":
                assistant_content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use", "id": b.id, "name": b.name,
                    "input": dict(b.input) if b.input else {},
                })
        messages.append({"role": "assistant", "content": assistant_content})

        results = []
        for block in tool_use:
            args = dict(block.input) if block.input else {}
            result = _run_tool(router, block.name, args, allow_actions, used, files, approval, pending, emit)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": results})

    final = client.messages.create(model=model, max_tokens=1024, system=system, messages=messages)
    if usage is not None:
        _accum_usage(usage, final, "anthropic")
    texts = [b.text for b in final.content if hasattr(b, "text")]
    return " ".join(texts).strip() or "(done)"


# ── Gemini ────────────────────────────────────────────────────────────────────

def _run_gemini(router, key, model, system, history, message, allow_actions, used, files, usage=None, rounds=_MAX_ROUNDS, effort="standard", approval=False, pending=None, emit=None, should_cancel=None, thinking=False, media=None) -> str:
    from google import genai
    from google.genai import types
    from tools import ALL_TOOL_DECLARATIONS

    client = genai.Client(api_key=key)
    contents = [
        types.Content(
            role=("model" if m["role"] == "assistant" else "user"),
            parts=[types.Part(text=m["content"])],
        )
        for m in history
    ]
    user_parts = [types.Part(text=message)]
    for mm in (media or []):
        try:
            user_parts.append(types.Part.from_bytes(data=mm["data"], mime_type=mm["mime"]))
        except Exception as e:
            logger.warning(f"gemini media part skipped: {e}")
    contents.append(types.Content(role="user", parts=user_parts))

    _decls = ALL_TOOL_DECLARATIONS + _mcp_tools("gemini")
    cfg_kwargs = {
        "system_instruction": system,
        "tools": [types.Tool(function_declarations=_decls)] if _decls else [],
    }
    # Native thinking budget for 2.5+ models (quick=off, standard=dynamic, deep=high).
    # The Thinking toggle forces it on (dynamic) even at quick effort.
    budget = _EFFORT_GEMINI_BUDGET.get(effort)
    if thinking and (budget is None or budget == 0):
        budget = -1   # dynamic
    use_thinking = budget is not None and any(t in model.lower() for t in ("2.5", "2-5", "3."))
    if use_thinking:
        try:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
        except Exception:
            use_thinking = False
    gen_config = types.GenerateContentConfig(**cfg_kwargs)

    for _r in range(rounds):
        if _cancelled(should_cancel):
            return "(cancelled)"
        _emit(emit, {"type": "round", "n": _r + 1})
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=gen_config)
        except Exception as e:
            if use_thinking:   # model/endpoint rejected the thinking budget → drop it
                logger.warning(f"gemini thinking budget rejected ({e}); retrying without")
                cfg_kwargs.pop("thinking_config", None)
                gen_config = types.GenerateContentConfig(**cfg_kwargs)
                use_thinking = False
                resp = client.models.generate_content(model=model, contents=contents, config=gen_config)
            else:
                raise
        if usage is not None:
            _accum_usage(usage, resp, "gemini")
        candidate = resp.candidates[0] if resp.candidates else None
        if not candidate or not candidate.content or not candidate.content.parts:
            break

        parts = candidate.content.parts
        fc_parts = [p for p in parts if p.function_call is not None]
        if not fc_parts:
            texts = [p.text for p in parts if p.text]
            return " ".join(texts).strip() or "(no response)"

        contents.append(candidate.content)
        response_parts = []
        for part in fc_parts:
            fc = part.function_call
            args = dict(fc.args) if fc.args else {}
            result = _run_tool(router, fc.name, args, allow_actions, used, files, approval, pending, emit)
            response_parts.append(
                types.Part(function_response=types.FunctionResponse(
                    name=fc.name, response={"result": result}))
            )
        contents.append(types.Content(role="user", parts=response_parts))

    return "(done)"
