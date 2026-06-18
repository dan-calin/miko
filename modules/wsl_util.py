"""Helpers for working inside a WSL workspace.

When the user's active workspace lives in a WSL distro (a
``\\\\wsl.localhost\\<distro>\\...`` UNC path), Windows-native tools operate on
that share unreliably — git in particular trips "dubious ownership" and file
locking, and shell built-ins are the wrong dialect. Routing those commands
through ``wsl.exe`` runs them as real Linux inside the distro, which is what the
user expects when they point Miko at a WSL folder.

File I/O over the ``\\\\wsl.localhost`` share is fine from Windows, so only the
shell and git need routing — see ``os_control.run_command`` and
``claude_code._git``.
"""
from __future__ import annotations

import re
import shlex

# Matches \\wsl.localhost\<distro>\... and the older \\wsl$\<distro>\... form.
_WSL_RE = re.compile(r"^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\?(.*)$", re.IGNORECASE)


def wsl_parts(path: str):
    """Return ``(distro, linux_path)`` for a WSL UNC path, else ``(None, None)``.

    Accepts either slash style; ``linux_path`` is always POSIX and absolute.
    """
    if not path:
        return None, None
    p = path.strip().replace("/", "\\")
    m = _WSL_RE.match(p)
    if not m:
        return None, None
    distro = m.group(1)
    rest = m.group(2).replace("\\", "/").strip("/")
    return distro, ("/" + rest if rest else "/")


def is_wsl_path(path: str) -> bool:
    """True when ``path`` points inside a WSL distro."""
    return wsl_parts(path)[0] is not None


def wsl_bash(distro: str, linux_path: str, command: str) -> list:
    """argv that runs ``command`` in bash, inside ``distro``, cd'd to ``linux_path``."""
    cd = f"cd {shlex.quote(linux_path)} && " if linux_path and linux_path != "/" else ""
    return ["wsl.exe", "-d", distro, "--", "bash", "-lc", cd + command]


def wsl_git(distro: str, linux_path: str, args) -> list:
    """argv for ``git -C <linux_path> <args>`` run inside the distro."""
    return ["wsl.exe", "-d", distro, "git", "-C", linux_path, *args]
