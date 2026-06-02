# Miko — Voice AI Agent

Asistent vocal personal pentru Windows 11, alimentat de Google Gemini Live. Vorbești în română, Miko răspunde instant și execută comenzi pe PC, Discord, web și fișiere.

> **English version** at the bottom of this file.

---

## Cuprins

1. [Cerințe preliminare](#cerinte)
2. [Instalare](#instalare)
3. [Configurare Discord Bot](#discord-setup)
4. [Prima pornire](#prima-pornire)
5. [Comenzi vocale](#comenzi-vocale)
6. [Depanare](#depanare)
7. [English Setup Guide](#english-guide)

---

## Cerințe preliminare {#cerinte}

| Cerință | Versiune minimă | Notă |
|---------|-----------------|------|
| Python | 3.11+ | [python.org](https://python.org) |
| FFmpeg | orice versiune stabilă | Adaugă în PATH! |
| Google Gemini API Key | — | [aistudio.google.com](https://aistudio.google.com/apikey) |
| Windows 11 | — | Doar Windows (pycaw, winsound) |
| Microfon + Boxe | — | Necesare pentru voice I/O |

### Instalare FFmpeg

1. Descarcă de la [ffmpeg.org/download.html](https://ffmpeg.org/download.html) (Windows builds)
2. Extrage arhiva (ex: `C:\ffmpeg\`)
3. Adaugă `C:\ffmpeg\bin` în variabila de sistem `PATH`
4. Testează: `ffmpeg -version` în CMD

---

## Instalare {#instalare}

```bash
# 1. Clonează/descarcă proiectul în folderul dorit
cd "C:\Users\TuNume\Desktop\Jarvis V2"

# 2. Creează un virtual environment (recomandat)
python -m venv venv
venv\Scripts\activate

# 3. Instalează dependențele
pip install -r requirements.txt

# 4. Configurează variabilele de mediu
copy .env.example .env
notepad .env
```

Editează `.env` și completează cel puțin `LLM_API_KEY`.

---

## Configurare Discord Bot {#discord-setup}

Dacă vrei să folosești funcțiile Discord (muzică în voice, DM-uri, notificări):

1. Mergi la [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** → dă un nume (ex: "Miko")
3. Du-te la **Bot** → **Add Bot** → confirmă
4. Sub **Token**, click **Reset Token** și copiază token-ul în `.env` la `DISCORD_TOKEN`
5. La **Privileged Gateway Intents**, activează:
   - ✅ Server Members Intent
   - ✅ Message Content Intent
   - ✅ Presence Intent
6. Du-te la **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Read Message History`, `Connect`, `Speak`, `Use Voice Activity`
7. Copiază URL-ul generat, deschide-l în browser, și adaugă botul pe serverul tău
8. Copiează ID-ul serverului tău în `.env` la `DISCORD_GUILD_ID`
   (Activează Developer Mode în Discord → click dreapta pe server → Copy Server ID)

---

## Prima pornire {#prima-pornire}

```bash
# Activează virtualenv dacă nu e activ
venv\Scripts\activate

# Pornește Miko
python main.py
```

La prima pornire:
- Se aude un **bip dublu** când conexiunea la Gemini Live este stabilită
- Miko spune **"Conectat! Vorbește..."**
- Indexarea fișierelor rulează în fundal (poate dura 2-5 minute, în funcție de disc)
- Miko este în modul **ACTIV** — fiecare propoziție este procesată

---

## Comenzi vocale {#comenzi-vocale}

### Moduri de funcționare

| Comandă vocală | Efect |
|----------------|-------|
| `Miko, intră în stand-by` | Trece în STANDBY — răspunde doar la "Miko" |
| `Miko, ieși din stand-by` | Revine în modul ACTIV |
| `Miko, intră în modul conversație` | Modul AUTO — răspunde natural |
| `Miko, oprește modul conversație` | Iese din AUTO |

### Control sistem

| Comandă vocală | Efect |
|----------------|-------|
| `Miko, deschide Chrome` | Pornește Chrome |
| `Miko, deschide Documents` | Deschide folderul Documents |
| `Miko, fă un screenshot` | Screenshot salvat pe Desktop |
| `Miko, blochează ecranul` | Lock screen |
| `Miko, ce informații ai despre sistem?` | CPU, RAM, baterie, disc |
| `Miko, setează un reminder în 10 minute — apel cu Ion` | Reminder cu notificare |

### Volum și media

| Comandă vocală | Efect |
|----------------|-------|
| `Miko, dă volumul la 50` | Setează volum la 50% |
| `Miko, dă mai tare` | Volume up x3 |
| `Miko, dă mai încet` | Volume down x3 |
| `Miko, pune pe mut` | Mute |
| `Miko, play / pauză / next` | Control media sistem |

### Muzică pe Discord (voice channel)

| Comandă vocală | Efect |
|----------------|-------|
| `Miko, pune Linkin Park pe Discord` | Caută și redă pe voice |
| `Miko, adaugă la coadă Eminem` | Adaugă în playlist |
| `Miko, skip` / `Miko, next` | Sare melodia curentă |
| `Miko, pauză` / `Miko, resume` | Pauză / reluare |
| `Miko, oprește muzica` | Stop + golește coada |
| `Miko, ce melodii urmează?` | Afișează coada |
| `Miko, intră pe voice` | Se alătură canalului tău |
| `Miko, ieși de pe voice` | Deconectare |

### Cercetare web

| Comandă vocală | Efect |
|----------------|-------|
| `Miko, caută cele mai bune restaurante din Cluj` | Căutare DuckDuckGo + rezumat |
| `Miko, ce știi despre inteligența artificială?` | Căutare + rezumat |
| `Miko, deschide Google în browser` | Deschide URL |

### Notițe

| Comandă vocală | Efect |
|----------------|-------|
| `Miko, notează că am o întâlnire mâine la 14:00` | Creează notiță |
| `Miko, citește notița de azi` | Citește ultima notiță din zi |
| `Miko, arată-mi notițele recente` | Lista notițelor |
| `Miko, caută în notițe: proiect` | Căutare în notițe |

### Fișiere

| Comandă vocală | Efect |
|----------------|-------|
| `Miko, găsește fișierul budget.xlsx` | Caută în index SQLite |
| `Miko, unde e documentul cu CV-ul meu?` | Caută PDF-uri / docx |
| `Miko, listează fișierele din Documents` | Conținut folder |
| `Miko, reconstruiește indexul de fișiere` | Re-indexare completă |

### Discord messaging

| Comandă vocală | Efect |
|----------------|-------|
| `Miko, trimite-i un mesaj lui Ion: "Vin la 8"` | DM cu confirmare |
| `Miko, citește mesajele de pe Discord` | Ultimele DM-uri |
| `Miko, cheamă-l pe Ion pe voice` | Invitație + join |

---

## Depanare {#depanare}

### Miko nu răspunde la voce
- Verifică microfonul în Settings → Sound → Input
- Asigură-te că `sounddevice` are acces la microfon
- Verifică că `LLM_API_KEY` este corect în `.env`

### Eroare `No module named 'pycaw'`
```bash
pip install pycaw comtypes
```

### Discord bot nu se conectează
- Verifică `DISCORD_TOKEN` în `.env`
- Asigură-te că botul are Privileged Intents activate în Developer Portal
- Botul trebuie să fie pe serverul specificat în `DISCORD_GUILD_ID`

### Muzica pe Discord nu funcționează
- Verifică că FFmpeg este instalat și în PATH: `ffmpeg -version`
- Asigură-te că ești conectat la un voice channel înainte de comandă
- Verifică că `PyNaCl` este instalat: `pip install PyNaCl`

### `UnicodeEncodeError` în consolă
- Rulează în Windows Terminal (nu CMD clasic)
- Sau setează `PYTHONIOENCODING=utf-8` în variabilele de sistem

### Indexarea fișierelor este lentă
- Normal la prima pornire — poate dura 3-10 minute pe disc-uri mari
- Ulterior se face incremental (la 30 min) și este rapid

---

---

## English Setup Guide {#english-guide}

### Prerequisites

- Python 3.11+
- FFmpeg (add to PATH)
- Google Gemini API key from [aistudio.google.com](https://aistudio.google.com/apikey)
- Windows 11

### Quick Start

```bash
cd "C:\Users\YourName\Desktop\Jarvis V2"
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Edit .env with your keys
python main.py
```

### Discord Bot Setup

1. Create app at [discord.com/developers/applications](https://discord.com/developers/applications)
2. Add a Bot, copy the Token → paste in `.env` as `DISCORD_TOKEN`
3. Enable **Server Members Intent**, **Message Content Intent**, **Presence Intent**
4. Invite bot using OAuth2 URL Generator (scopes: `bot`, permissions: Send Messages, Connect, Speak)
5. Copy your server ID → paste in `.env` as `DISCORD_GUILD_ID`

### Architecture Overview

```
main.py
├── AudioHandler (Gemini Live WebSocket)
│   ├── Microphone capture (sounddevice, 16kHz)
│   ├── Audio playback (sounddevice, 24kHz)
│   ├── ModeManager (ACTIVE/STANDBY/AUTO filtering)
│   └── CommandRouter (tool dispatch + safety)
├── DiscordBot thread (own asyncio loop)
├── DiscordPoll thread (checks DMs every 2s)
├── FileIndexer thread (SQLite, full + incremental)
└── MemoryExtractor thread (every 5 turns, facts from speech)
```

### Voice Commands (English)

Miko understands both Romanian and English. If you speak English, it will respond in English.

- `Miko, open Chrome` — Launch application
- `Miko, set volume to 60` — Volume control  
- `Miko, play Coldplay on Discord` — Music in voice channel
- `Miko, search for Python tutorials` — Web search
- `Miko, take a screenshot` — Screenshot to Desktop
- `Miko, go to standby` — Enter STANDBY mode
- `Miko, wake up` — Exit STANDBY mode
