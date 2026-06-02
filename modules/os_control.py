"""
modules/os_control.py — OS-level operations for Windows 11.
Covers: app launching, file operations, CMD execution, reminders,
system info, screenshots, screen lock, shutdown/restart.

All file write/delete operations double-validate paths against blocked dirs.
"""

import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("miko.os")

# Wired from main.py so reminders can speak aloud via Miko's voice
_speak_callback = None


def set_speak_callback(cb) -> None:
    global _speak_callback
    _speak_callback = cb

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Deschide o aplicație sau un fișier. Poate lansa apps instalate, "
            "fișiere, sau foldere. Exemple: 'deschide Chrome', 'pornește Spotify', 'deschide Documents'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Numele aplicației sau calea fișierului/folderului.",
                }
            },
            "required": ["app_name"],
        },
    },
    {
        "name": "file_op",
        "description": (
            "Operații pe fișiere și foldere: listare, creare, ștergere (cu confirmare), "
            "mutare, copiere, redenumire, citire, scriere, info disc."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "Acțiunea: list | create_file | create_folder | delete | "
                        "move | copy | rename | read | write | info | disk_usage"
                    ),
                },
                "path": {"type": "STRING", "description": "Calea fișierului/folderului."},
                "destination": {"type": "STRING", "description": "Calea destinație (pentru move/copy/rename)."},
                "content": {"type": "STRING", "description": "Conținut (pentru write)."},
            },
            "required": ["action", "path"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Execută o comandă în terminal sau descrie o sarcină în limbaj natural "
            "și Miko o transformă în comandă CMD. Exemple: 'golește Recycle Bin', 'listează fișierele'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {
                    "type": "STRING",
                    "description": "Sarcina în limbaj natural sau comanda exactă de executat.",
                },
                "visible": {
                    "type": "BOOLEAN",
                    "description": "Dacă True, deschide CMD vizibil. Default: False (fundal).",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "set_reminder",
        "description": "Setează un reminder/alarmă. Poate fi în secunde, minute, sau la o dată/oră exactă.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "message": {"type": "STRING", "description": "Mesajul reminderului."},
                "seconds": {"type": "INTEGER", "description": "Peste câte SECUNDE să sune. Folosește pentru '30 de secunde', '10 secunde', etc."},
                "minutes": {"type": "INTEGER", "description": "Peste câte MINUTE să sune. Folosește pentru '5 minute', 'un sfert de oră', etc."},
                "date": {"type": "STRING", "description": "Data exactă (YYYY-MM-DD). Folosește împreună cu time_str."},
                "time_str": {"type": "STRING", "description": "Ora exactă (HH:MM). Folosește împreună cu date."},
            },
            "required": ["message"],
        },
    },
    {
        "name": "system_info",
        "description": "Returnează informații despre sistem: CPU, RAM, disc, baterie, OS.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Ce informație vrei: all | cpu | ram | disk | battery | os",
                }
            },
        },
    },
    {
        "name": "take_screenshot",
        "description": "Face un screenshot al ecranului și îl salvează pe Desktop.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "lock_workstation",
        "description": "Blochează stația de lucru (Windows Lock Screen).",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "shutdown_computer",
        "description": "Oprește calculatorul. Necesită confirmare.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "restart_computer",
        "description": "Repornește calculatorul. Necesită confirmare.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "clipboard",
        "description": "Citește sau scrie în clipboard-ul Windows.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "read — citește clipboard | write — scrie în clipboard",
                },
                "text": {
                    "type": "STRING",
                    "description": "Textul de scris (doar pentru action='write').",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "window_control",
        "description": (
            "Controlează ferestrele deschise. "
            "Poate lista, minimiza, maximiza, restaura, închide sau aduce în față o fereastră după titlu."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "list | minimize | maximize | restore | close | focus",
                },
                "title": {
                    "type": "STRING",
                    "description": "Titlul ferestrei (sau parte din el). Nu e necesar pentru action='list'.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "process_manager",
        "description": "Listează procesele active sau oprește un proces după nume.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "list — arată procesele active | kill — oprește un proces",
                },
                "name": {
                    "type": "STRING",
                    "description": "Numele procesului de oprit (ex: 'chrome', 'discord.exe'). Necesar pentru kill.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "calculate",
        "description": (
            "Calculează o expresie matematică. "
            "Suportă: +, -, *, /, **, %, sqrt, sin, cos, tan, log, abs, round, pi, e. "
            "Exemple: '15% din 240', 'sqrt(144)', '2^10', '(17+3)*5'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "expression": {
                    "type": "STRING",
                    "description": "Expresia matematică de calculat.",
                }
            },
            "required": ["expression"],
        },
    },
    {
        "name": "type_text",
        "description": (
            "Tastează text în fereastra activă curentă. "
            "Folosește pentru dictare, completare formulare, scriere rapidă. "
            "Suportă caractere române și speciale."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {
                    "type": "STRING",
                    "description": "Textul de tastat în fereastra activă.",
                },
                "press_enter": {
                    "type": "BOOLEAN",
                    "description": "Dacă True, apasă Enter după tastare. Default: False.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "send_shortcut",
        "description": (
            "Trimite un shortcut de tastatură. "
            "Exemple: 'ctrl+c', 'alt+f4', 'win+d', 'ctrl+shift+esc', 'ctrl+z'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "keys": {
                    "type": "STRING",
                    "description": "Shortcut-ul de trimis, cu '+' între taste (ex: 'ctrl+c', 'alt+tab').",
                }
            },
            "required": ["keys"],
        },
    },
    {
        "name": "wifi_control",
        "description": "Activează, dezactivează sau verifică statusul Wi-Fi-ului.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "on — activează | off — dezactivează | status — verifică starea",
                }
            },
            "required": ["action"],
        },
    },
]

# ── Safety guard ──────────────────────────────────────────────────────────────

_BLOCKED_PATHS = (
    "c:\\windows",
    "c:\\windows\\system32",
    "c:\\windows\\syswow64",
)

_BLOCKED_CMD_PATTERNS = re.compile(
    r"(format\s+[a-z]:|\bdel\s+/[fqs]|\brd\s+/s|\brm\s+-rf|"
    r"reg\s+(add|delete|import)|bcdedit|diskpart|"
    r"net\s+(user|localgroup)\s+.*\/add|"
    r"powershell.*(bypass|hidden)|cmd.*\/c.*del)",
    re.IGNORECASE,
)


def _validate_path(path: str) -> tuple[bool, str]:
    path_lower = path.lower()
    for blocked in _BLOCKED_PATHS:
        if path_lower.startswith(blocked):
            return False, f"Calea '{blocked}' este protejată, sefu."
    return True, "OK"


# ── App launching ─────────────────────────────────────────────────────────────

# Known app aliases → Windows URI or process name
_APP_ALIASES: dict[str, str] = {
    "chrome":     "chrome",
    "firefox":    "firefox",
    "edge":       "msedge",
    "notepad":    "notepad",
    "explorer":   "explorer",
    "spotify":    "spotify",
    "discord":    "discord",
    "obs":        "obs64",
    "code":       "code",
    "vscode":     "code",
    "task manager":    "taskmgr",
    "taskmgr":         "taskmgr",
    "calculator":      "calc",
    "paint":           "mspaint",
    "word":            "WINWORD",
    "excel":           "EXCEL",
    "powerpoint":      "POWERPNT",
    "teams":           "teams",
    "steam":           "steam",
    "vlc":             "vlc",
    "terminal":        "wt",
    "cmd":             "cmd",
    "powershell":      "powershell",
    "settings":        "ms-settings:",
    "store":           "ms-windows-store:",
    "camera":          "microsoft.windows.camera:",
    "photos":          "ms-photos:",
    "maps":            "bingmaps:",
}

# Common Windows user folders — resolved at runtime so Path.home() is correct
def _get_folder_aliases() -> dict[str, str]:
    home = Path.home()
    return {
        # English names
        "desktop":      str(home / "Desktop"),
        "documents":    str(home / "Documents"),
        "downloads":    str(home / "Downloads"),
        "pictures":     str(home / "Pictures"),
        "videos":       str(home / "Videos"),
        "music":        str(home / "Music"),
        "home":         str(home),
        "user":         str(home),
        # Romanian names
        "descarcări":   str(home / "Downloads"),
        "descarcari":   str(home / "Downloads"),
        "imagini":      str(home / "Pictures"),
        "documente":    str(home / "Documents"),
        "videoclipuri": str(home / "Videos"),
        "muzică":       str(home / "Music"),
        "muzica":       str(home / "Music"),
        # Shorthand
        "dl":           str(home / "Downloads"),
        "docs":         str(home / "Documents"),
        "pics":         str(home / "Pictures"),
    }


def open_app(app_name: str) -> str:
    name_lower = app_name.lower().strip()

    # 1. Check if it's a common Windows folder name
    folder_aliases = _get_folder_aliases()
    folder_path = folder_aliases.get(name_lower)
    if folder_path and os.path.isdir(folder_path):
        try:
            subprocess.Popen(["explorer", folder_path])
            return f"Am deschis folderul {app_name}, sefu."
        except Exception as e:
            return f"N-am putut deschide folderul {app_name}: {e}"

    # 2. Check app alias map
    alias = _APP_ALIASES.get(name_lower)

    # 3. If it's a URI (ms-settings: etc.), use os.startfile
    if alias and alias.endswith(":"):
        try:
            os.startfile(alias)
            return f"Am deschis {app_name}, sefu."
        except Exception as e:
            return f"N-am putut deschide {app_name}: {e}"

    # 4. If it looks like an existing path, open directly
    if os.path.exists(app_name):
        try:
            os.startfile(app_name)
            return f"Am deschis '{app_name}', sefu."
        except Exception as e:
            return f"N-am putut deschide '{app_name}': {e}"

    # 5. Try subprocess start (works for PATH executables)
    exe = alias or name_lower
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", exe],
            shell=False,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        return f"Am pornit {app_name}, sefu."
    except Exception:
        pass

    # 6. Search Start Menu .lnk shortcuts
    lnk = _find_start_menu_shortcut(name_lower)
    if lnk:
        try:
            os.startfile(str(lnk))
            return f"Am pornit {app_name} din Start Menu, sefu."
        except Exception as e:
            return f"Am găsit {app_name} dar n-am putut porni: {e}"

    return f"N-am găsit aplicația sau folderul '{app_name}', sefu. Verifică dacă e instalat/ă."


def _find_start_menu_shortcut(name: str) -> Optional[Path]:
    """Search Windows Start Menu folders for a .lnk shortcut matching name."""
    search_dirs = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu",
        Path(r"C:\ProgramData\Microsoft\Windows\Start Menu"),
    ]
    for base in search_dirs:
        if not base.exists():
            continue
        for lnk in base.rglob("*.lnk"):
            if name in lnk.stem.lower():
                return lnk
    return None


# ── File operations ───────────────────────────────────────────────────────────

def file_op(action: str, path: str, destination: str = "", content: str = "") -> str:
    action = action.lower().strip()

    # Safety check for write/delete operations
    if action in ("delete", "write", "move", "rename"):
        ok, reason = _validate_path(path)
        if not ok:
            return reason
        if destination:
            ok2, reason2 = _validate_path(destination)
            if not ok2:
                return reason2

    p = Path(path)

    if action == "list":
        if not p.exists():
            return f"Calea '{path}' nu există, sefu."
        if p.is_file():
            return f"'{path}' este un fișier, nu un folder."
        try:
            items = list(p.iterdir())
            if not items:
                return f"Folderul '{path}' este gol, sefu."
            lines = [f"Conținut '{path}' ({len(items)} elemente):"]
            for item in sorted(items)[:30]:
                lines.append(f"  {'📁' if item.is_dir() else '📄'} {item.name}")
            if len(items) > 30:
                lines.append(f"  … și încă {len(items) - 30} elemente.")
            return "\n".join(lines)
        except PermissionError:
            return f"Nu am acces la '{path}', sefu."

    if action == "create_file":
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"Am creat fișierul '{path}', sefu."
        except Exception as e:
            return f"N-am putut crea fișierul: {e}"

    if action == "create_folder":
        try:
            p.mkdir(parents=True, exist_ok=True)
            return f"Am creat folderul '{path}', sefu."
        except Exception as e:
            return f"N-am putut crea folderul: {e}"

    if action == "delete":
        if not p.exists():
            return f"'{path}' nu există, sefu."
        try:
            import send2trash
            send2trash.send2trash(str(p))
            return f"Am trimis '{p.name}' la Coșul de Gunoi, sefu."
        except ImportError:
            # Fallback to direct delete only if send2trash unavailable
            if p.is_dir():
                import shutil
                shutil.rmtree(p)
            else:
                p.unlink()
            return f"Am șters '{p.name}', sefu."
        except Exception as e:
            return f"N-am putut șterge '{path}': {e}"

    if action == "move":
        if not destination:
            return "Spune-mi și destinația pentru mutare, sefu."
        try:
            import shutil
            shutil.move(str(p), destination)
            return f"Am mutat '{p.name}' la '{destination}', sefu."
        except Exception as e:
            return f"N-am putut muta fișierul: {e}"

    if action == "copy":
        if not destination:
            return "Spune-mi și destinația pentru copiere, sefu."
        try:
            import shutil
            shutil.copy2(str(p), destination)
            return f"Am copiat '{p.name}' la '{destination}', sefu."
        except Exception as e:
            return f"N-am putut copia fișierul: {e}"

    if action == "rename":
        if not destination:
            return "Spune-mi și noul nume, sefu."
        try:
            dest = p.parent / destination
            p.rename(dest)
            return f"Am redenumit '{p.name}' în '{destination}', sefu."
        except Exception as e:
            return f"N-am putut redenumi: {e}"

    if action == "read":
        if not p.exists():
            return f"'{path}' nu există, sefu."
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            if len(text) > 2000:
                text = text[:2000] + "\n… (fișier mai lung, am citit primele 2000 caractere)"
            return f"Conținut '{p.name}':\n\n{text}"
        except Exception as e:
            return f"N-am putut citi fișierul: {e}"

    if action == "write":
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"Am scris în fișierul '{p.name}', sefu."
        except Exception as e:
            return f"N-am putut scrie în fișier: {e}"

    if action == "info":
        if not p.exists():
            return f"'{path}' nu există, sefu."
        stat = p.stat()
        size = _fmt_size(stat.st_size)
        modified = datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M")
        kind = "Folder" if p.is_dir() else "Fișier"
        return f"{kind}: {p.name}\nDimensiune: {size}\nModificat: {modified}\nCalea completă: {p}"

    if action == "disk_usage":
        try:
            import shutil
            total, used, free = shutil.disk_usage(path or "C:\\")
            return (
                f"Disc '{path or 'C:\\'}': "
                f"Total {_fmt_size(total)}, "
                f"Folosit {_fmt_size(used)}, "
                f"Liber {_fmt_size(free)}."
            )
        except Exception as e:
            return f"N-am putut obține info disc: {e}"

    if action == "open":
        if not p.exists():
            return f"'{path}' nu există, sefu."
        try:
            os.startfile(str(p))
            return f"Am deschis '{p.name}', sefu."
        except Exception as e:
            return f"N-am putut deschide '{path}': {e}"

    return f"Nu înțeleg acțiunea '{action}'. Încearcă: list, create_file, delete, move, copy, rename, read, write, info."


# ── CMD execution ─────────────────────────────────────────────────────────────

def run_command(task: str, visible: bool = False) -> str:
    if not task.strip():
        return "Spune-mi ce comandă să execut, sefu."

    # Safety check
    if _BLOCKED_CMD_PATTERNS.search(task):
        return "Comanda pare periculoasă și a fost blocată din motive de securitate, sefu."

    # If it looks like a direct command, run it. Otherwise treat as NL.
    try:
        if visible:
            subprocess.Popen(["cmd", "/k", task], creationflags=subprocess.CREATE_NEW_CONSOLE)
            return f"Am deschis un terminal nou cu comanda: {task}"
        else:
            result = subprocess.run(
                task,
                shell=True,
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            if result.returncode == 0:
                return out or f"Comandă executată cu succes (exit 0)."
            return f"Eroare (exit {result.returncode}): {err or out}"
    except subprocess.TimeoutExpired:
        return "Comanda a depășit limita de timp (15s), sefu."
    except Exception as e:
        return f"N-am putut executa comanda: {e}"


# ── Reminders / timers ────────────────────────────────────────────────────────

def set_reminder(
    message: str,
    seconds: Optional[int] = None,
    minutes: Optional[int] = None,
    date: Optional[str] = None,
    time_str: Optional[str] = None,
) -> str:
    if seconds:
        fire_at = datetime.now() + timedelta(seconds=seconds)
    elif minutes:
        fire_at = datetime.now() + timedelta(minutes=minutes)
    elif date and time_str:
        try:
            fire_at = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            return f"Format dată/oră invalid. Folosește YYYY-MM-DD și HH:MM, sefu."
    else:
        return "Spune-mi peste câte secunde/minute sau la ce dată/oră vrei reminderul, sefu."

    delay = (fire_at - datetime.now()).total_seconds()
    if delay <= 0:
        return "Data/ora specificată a trecut deja, sefu."

    def _fire():
        time.sleep(delay)
        try:
            import winsound
            for _ in range(3):
                winsound.Beep(1000, 300)
                time.sleep(0.1)
        except Exception:
            pass
        print(f"\n[REMINDER] ⏰ {message}")
        try:
            from win10toast import ToastNotifier
            ToastNotifier().show_toast("Miko — Reminder", message, duration=10, threaded=True)
        except Exception:
            pass
        if _speak_callback:
            try:
                _speak_callback(f"Atenție, sefu! Reminder: {message}")
            except Exception:
                pass

    threading.Thread(target=_fire, daemon=True, name="Reminder").start()

    # Build a human-friendly delay description
    if delay < 60:
        delay_display = f"în {int(delay)} secunde"
    elif delay < 3600:
        delay_display = f"în {int(delay // 60)} minute"
    else:
        delay_display = f"la {fire_at.strftime('%d.%m.%Y %H:%M')}"
    return f"Reminder setat {delay_display}: '{message}'. Te anunț eu, sefu."


# ── System info ───────────────────────────────────────────────────────────────

def system_info(query: str = "all") -> str:
    try:
        import psutil
    except ImportError:
        return "psutil nu este instalat. Rulează: pip install psutil"

    query = (query or "all").lower()
    parts = []

    if query in ("all", "cpu"):
        cpu = psutil.cpu_percent(interval=0.5)
        parts.append(f"CPU: {cpu}%")

    if query in ("all", "ram", "memory"):
        ram = psutil.virtual_memory()
        parts.append(
            f"RAM: {_fmt_size(ram.used)} folosit din {_fmt_size(ram.total)} "
            f"({ram.percent}%)"
        )

    if query in ("all", "disk", "disc"):
        try:
            import shutil
            total, used, free = shutil.disk_usage("C:\\")
            parts.append(f"Disc C: {_fmt_size(free)} liber din {_fmt_size(total)}")
        except Exception:
            pass

    if query in ("all", "battery", "baterie"):
        batt = psutil.sensors_battery()
        if batt:
            plug = "conectat la curent" if batt.power_plugged else "pe baterie"
            parts.append(f"Baterie: {batt.percent:.0f}% ({plug})")

    if query in ("all", "os"):
        parts.append(f"OS: {platform.system()} {platform.release()} {platform.version()[:20]}")
        parts.append(f"Hostname: {platform.node()}")

    if not parts:
        return f"Nu știu ce înseamnă '{query}'. Încearcă: all, cpu, ram, disk, battery, os."

    return "\n".join(parts)


# ── Screenshot ────────────────────────────────────────────────────────────────

def take_screenshot() -> str:
    try:
        import pyautogui
        from PIL import Image  # noqa: F401

        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        desktop    = Path.home() / "Desktop"
        filepath   = desktop / f"Miko_Screenshot_{timestamp}.png"
        screenshot = pyautogui.screenshot()
        screenshot.save(str(filepath))
        logger.info(f"Screenshot saved: {filepath}")
        return f"Screenshot salvat pe Desktop: {filepath.name}"
    except Exception as e:
        return f"N-am putut face screenshot: {e}"


# ── Lock / Shutdown / Restart ─────────────────────────────────────────────────

def lock_workstation() -> str:
    try:
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return "Am blocat ecranul, sefu."
    except Exception as e:
        return f"N-am putut bloca ecranul: {e}"


def shutdown_computer() -> str:
    try:
        subprocess.run(["shutdown", "/s", "/t", "10"], check=True)
        return "Calculatorul se va opri în 10 secunde. La revedere, sefu!"
    except Exception as e:
        return f"N-am putut opri calculatorul: {e}"


def restart_computer() -> str:
    try:
        subprocess.run(["shutdown", "/r", "/t", "10"], check=True)
        return "Calculatorul se va reporni în 10 secunde, sefu."
    except Exception as e:
        return f"N-am putut reporni calculatorul: {e}"


# ── Calculator ───────────────────────────────────────────────────────────────

def calculate(expression: str) -> str:
    if not expression.strip():
        return "Dă-mi o expresie de calculat, sefu."
    import ast
    import math as _math
    import operator as _op

    _SAFE_OPS = {
        ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul,
        ast.Div: _op.truediv, ast.Mod: _op.mod, ast.Pow: _op.pow,
        ast.FloorDiv: _op.floordiv,
    }
    _SAFE_NAMES = {
        "abs": abs, "round": round, "min": min, "max": max,
        "sqrt": _math.sqrt, "sin": _math.sin, "cos": _math.cos, "tan": _math.tan,
        "log": _math.log, "log10": _math.log10, "ceil": _math.ceil,
        "floor": _math.floor, "pi": _math.pi, "e": _math.e,
    }

    def _eval(node):
        if isinstance(node, ast.Expression): return _eval(node.body)
        if isinstance(node, ast.Constant):   return node.value
        if isinstance(node, ast.BinOp):
            fn = _SAFE_OPS.get(type(node.op))
            if fn is None: raise ValueError("Operator nesuportat")
            return fn(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub): return -_eval(node.operand)
            if isinstance(node.op, ast.UAdd): return +_eval(node.operand)
            raise ValueError("Operator unar nesuportat")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name): raise ValueError("Apel complex nesuportat")
            fn = _SAFE_NAMES.get(node.func.id)
            if fn is None: raise ValueError(f"Funcție nesuportată: {node.func.id}")
            return fn(*[_eval(a) for a in node.args])
        if isinstance(node, ast.Name):
            val = _SAFE_NAMES.get(node.id)
            if val is None: raise ValueError(f"Variabilă necunoscută: {node.id}")
            return val
        raise ValueError(f"Expresie invalidă: {type(node).__name__}")

    try:
        expr = expression.replace("×", "*").replace("÷", "/").replace("^", "**").replace(",", ".")
        # Handle percentage shorthand: "15% din 240" → "0.15 * 240"
        expr = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*(?:din|of|out of)\s*", r"(\1/100)*", expr)
        tree = ast.parse(expr.strip(), mode="eval")
        result = _eval(tree.body)
        if isinstance(result, float) and result == int(result):
            result = int(result)
        return f"{expression} = {result}"
    except ZeroDivisionError:
        return "Împărțire la zero nu e posibilă nici pentru mine, sefu."
    except Exception as e:
        return f"N-am putut calcula '{expression}': {e}"


# ── Type text / keyboard ──────────────────────────────────────────────────────

def type_text(text: str, press_enter: bool = False) -> str:
    if not text:
        return "N-ai specificat textul de tastat, sefu."
    try:
        import time as _time
        # Use clipboard + Ctrl+V so Romanian/Unicode chars work correctly
        clipboard("write", text)
        _time.sleep(0.15)
        import pyautogui
        pyautogui.hotkey("ctrl", "v")
        if press_enter:
            _time.sleep(0.05)
            pyautogui.press("enter")
        return "Am tastat textul, sefu."
    except Exception as e:
        return f"N-am putut tasta textul: {e}"


_KEY_MAP = {
    "win": "winleft", "windows": "winleft", "super": "winleft",
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "space": "space", "spatiu": "space",
    "enter": "enter", "return": "return",
    "esc": "esc", "escape": "escape", "iesi": "esc",
    "tab": "tab",
    "delete": "delete", "del": "delete", "sterge": "delete",
    "backspace": "backspace",
    "home": "home", "end": "end",
    "up": "up", "jos": "down", "sus": "up",
    "down": "down", "left": "left", "right": "right",
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}


def send_shortcut(keys: str) -> str:
    if not keys.strip():
        return "Spune-mi ce shortcut să trimit, sefu."
    try:
        import pyautogui
        parts = [k.strip().lower() for k in re.split(r"[+\s]+", keys.strip()) if k.strip()]
        mapped = [_KEY_MAP.get(p, p) for p in parts]
        pyautogui.hotkey(*mapped)
        return f"Am apăsat {keys}, sefu."
    except Exception as e:
        return f"N-am putut trimite shortcut-ul '{keys}': {e}"


# ── Wi-Fi control ─────────────────────────────────────────────────────────────

_WIFI_IF_NAMES = ["Wi-Fi", "WiFi", "Wireless Network Connection", "WLAN"]


def wifi_control(action: str) -> str:
    action = action.lower().strip()

    if action in ("status", "stare", "info", "verifica", "verifică"):
        try:
            r = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True, timeout=8,
                encoding=sys.stdout.encoding or "utf-8", errors="replace",
            )
            out = r.stdout.strip()
            if not out:
                return "Wi-Fi-ul pare dezactivat sau nu există adaptor wireless, sefu."
            useful = [l.strip() for l in out.splitlines()
                      if any(k in l for k in ("SSID", "State", "Signal", "Band", "Radio"))]
            return "\n".join(useful[:8]) if useful else out[:300]
        except Exception as e:
            return f"N-am putut verifica Wi-Fi-ul: {e}"

    if action in ("on", "enable", "porneste", "pornit", "activeaza", "activează"):
        netsh_action, msg = "enabled", "Am activat Wi-Fi-ul, sefu."
    elif action in ("off", "disable", "opreste", "oprit", "dezactiveaza", "dezactivează"):
        netsh_action, msg = "disabled", "Am dezactivat Wi-Fi-ul, sefu."
    else:
        return f"Acțiune necunoscută: '{action}'. Folosește 'on', 'off', sau 'status'."

    for name in _WIFI_IF_NAMES:
        try:
            r = subprocess.run(
                ["netsh", "interface", "set", "interface", name, netsh_action],
                capture_output=True, timeout=8,
                encoding="utf-8", errors="replace",
            )
            if r.returncode == 0:
                return msg
        except Exception:
            continue

    verb = "activa" if netsh_action == "enabled" else "dezactiva"
    return f"N-am putut {verb} Wi-Fi-ul. Poate necesită drepturi de administrator, sefu."


# ── Clipboard ────────────────────────────────────────────────────────────────

def clipboard(action: str, text: str = "") -> str:
    action = action.lower().strip()
    import ctypes
    import ctypes.wintypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE  = 0x0002
    user32         = ctypes.windll.user32
    kernel32       = ctypes.windll.kernel32

    if action == "read":
        try:
            if not user32.OpenClipboard(0):
                return "N-am putut deschide clipboard-ul, sefu."
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return "Clipboard-ul este gol sau nu conține text, sefu."
                ptr = kernel32.GlobalLock(h)
                if not ptr:
                    return "N-am putut citi clipboard-ul, sefu."
                content = ctypes.wstring_at(ptr)
                kernel32.GlobalUnlock(h)
                return f"Clipboard: {content}" if content else "Clipboard-ul este gol, sefu."
            finally:
                user32.CloseClipboard()
        except Exception as e:
            return f"Eroare la citirea clipboard-ului: {e}"

    if action == "write":
        if not text:
            return "N-ai specificat textul de copiat, sefu."
        try:
            encoded = (text + "\0").encode("utf-16-le")
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            ptr = kernel32.GlobalLock(h)
            ctypes.memmove(ptr, encoded, len(encoded))
            kernel32.GlobalUnlock(h)
            if not user32.OpenClipboard(0):
                return "N-am putut deschide clipboard-ul, sefu."
            try:
                user32.EmptyClipboard()
                user32.SetClipboardData(CF_UNICODETEXT, h)
            finally:
                user32.CloseClipboard()
            preview = text[:60] + ("…" if len(text) > 60 else "")
            return f"Am copiat în clipboard: \"{preview}\", sefu."
        except Exception as e:
            return f"Eroare la scrierea în clipboard: {e}"

    return f"Acțiune necunoscută: '{action}'. Folosește 'read' sau 'write'."


# ── Window control ────────────────────────────────────────────────────────────

def window_control(action: str, title: str = "") -> str:
    action = action.lower().strip()
    import ctypes
    import ctypes.wintypes

    user32       = ctypes.windll.user32
    SW_MINIMIZE  = 6
    SW_MAXIMIZE  = 3
    SW_RESTORE   = 9
    WM_CLOSE     = 0x0010

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )
    windows: list[tuple[int, str]] = []

    def _enum_cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            t = buf.value.strip()
            if t:
                windows.append((hwnd, t))
        return True

    user32.EnumWindows(EnumWindowsProc(_enum_cb), 0)

    if action == "list":
        if not windows:
            return "Nu am găsit ferestre deschise, sefu."
        lines = ["Ferestre deschise:"] + [f"  - {t}" for _, t in windows[:25]]
        if len(windows) > 25:
            lines.append(f"  … și încă {len(windows) - 25} ferestre.")
        return "\n".join(lines)

    if not title:
        return "Spune-mi titlul ferestrei (sau parte din el), sefu."

    title_l = title.lower()
    matches = [(h, t) for h, t in windows if title_l in t.lower()]
    if not matches:
        return f"Nu am găsit nicio fereastră cu '{title}', sefu."

    hwnd, win_title = matches[0]

    if action == "minimize":
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        return f"Am minimizat '{win_title}', sefu."
    if action == "maximize":
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        return f"Am maximizat '{win_title}', sefu."
    if action == "restore":
        user32.ShowWindow(hwnd, SW_RESTORE)
        return f"Am restaurat '{win_title}', sefu."
    if action == "close":
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return f"Am trimis comanda de închidere la '{win_title}', sefu."
    if action in ("focus", "activate", "bring"):
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return f"Am adus în față '{win_title}', sefu."

    return f"Acțiune necunoscută: '{action}'. Încearcă: list, minimize, maximize, restore, close, focus."


# ── Process manager ───────────────────────────────────────────────────────────

def process_manager(action: str, name: str = "") -> str:
    action = action.lower().strip()
    try:
        import psutil
    except ImportError:
        return "psutil nu este instalat. Rulează: pip install psutil"

    if action == "list":
        procs = []
        for p in psutil.process_iter(["pid", "name", "memory_percent"]):
            try:
                if p.info["memory_percent"] and p.info["memory_percent"] > 0.05:
                    procs.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        procs.sort(key=lambda x: x.get("memory_percent", 0), reverse=True)
        lines = ["Procese active (top 15 după memorie):"]
        for p in procs[:15]:
            lines.append(f"  {p['name']} (PID {p['pid']}) — {p.get('memory_percent', 0):.1f}% RAM")
        return "\n".join(lines)

    if action == "kill":
        if not name:
            return "Spune-mi numele procesului de oprit, sefu."
        name_l = name.lower().replace(".exe", "")
        killed, denied = [], []
        found = False
        for p in psutil.process_iter(["pid", "name"]):
            try:
                pname = p.info["name"] or ""
                if name_l in pname.lower().replace(".exe", ""):
                    found = True
                    p.kill()
                    killed.append(f"{pname} (PID {p.info['pid']})")
            except psutil.AccessDenied:
                denied.append(p.info.get("name", "?"))
            except psutil.NoSuchProcess:
                pass
        if not found:
            return f"Nu am găsit niciun proces cu numele '{name}', sefu."
        if not killed and denied:
            return f"Acces refuzat pentru '{name}' — probabil un proces de sistem, sefu."
        return f"Am oprit: {', '.join(killed)}, sefu."

    return f"Acțiune necunoscută: '{action}'. Folosește 'list' sau 'kill'."


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"
