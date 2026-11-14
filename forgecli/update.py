"""Update check and `/update` / `forge --update`.

- Once a day, in the background, Forge asks PyPI for the newest `forgecommandline` version
  (one GET to pypi.org, nothing about you or your project is sent). The answer is cached in
  ~/.forge/update.json, so the next start can say "update available" without waiting.
- Never in pipes, scripts, CI or `forge -p`. FORGE_UPDATE_CHECK=0 turns the check off.
- Nothing installs by itself: the user runs /update (or `forge --update`).
- On Windows a running program can't replace its own files, so /update starts the upgrade in a
  new window right after Forge closes. Elsewhere it upgrades in place and asks for a restart.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

from . import __version__

PACKAGE = "forgecommandline"
PYPI = f"https://pypi.org/pypi/{PACKAGE}/json"
EVERY = 24 * 3600
TIMEOUT = 2.5


def _home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("FORGE_HOME") or pathlib.Path.home() / ".forge")


def _path() -> pathlib.Path:
    return _home() / "update.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st: dict) -> None:
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(st), encoding="utf-8")
    except Exception:
        pass


def parse(v: str) -> tuple:
    """'0.10.2' -> (0, 10, 2). Pre-releases ('0.9.0rc1') sort before the release."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)(.*)$", v or "")
    if not m:
        return ()
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums += (0,) * (3 - len(nums))
    return nums + ((0,) if m.group(2).strip() else (1,))


def newer(latest: str, current: str = __version__) -> bool:
    a, b = parse(latest), parse(current)
    return bool(a and b and a > b)


def enabled() -> bool:
    if os.environ.get("FORGE_UPDATE_CHECK", "1") == "0" or os.environ.get("CI"):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def fetch() -> str | None:
    """Newest version on PyPI, or None when offline/slow. Caches the answer."""
    try:
        req = urllib.request.Request(PYPI, headers={"User-Agent": f"forge-cli/{__version__}",
                                                    "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            latest = json.loads(r.read(2_000_000).decode("utf-8", "replace"))["info"]["version"]
    except Exception:
        return None
    _save({"latest": latest, "checked": time.time()})
    return latest


def cached() -> str | None:
    """Newest version seen by the last check (no network)."""
    latest = _load().get("latest")
    return latest if isinstance(latest, str) and newer(latest) else None


def check_in_background() -> None:
    """At most once a day; never blocks start-up."""
    if not enabled() or time.time() - float(_load().get("checked") or 0) < EVERY:
        return
    threading.Thread(target=fetch, daemon=True).start()


def how_installed() -> tuple[str, list[str] | None]:
    """(kind, command). kind is 'pipx', 'pip', 'exe' or 'source' (no command for the last two)."""
    if getattr(sys, "frozen", False):
        return "exe", None                 # the Windows forge.exe: new zip from GitHub Releases
    here = pathlib.Path(__file__).resolve().parent
    if (here.parent / ".git").exists() or (here.parent / "pyproject.toml").exists() and \
            "site-packages" not in str(here):
        return "source", None
    if "pipx" in sys.prefix.lower().replace("\\", "/"):
        pipx = shutil.which("pipx")
        if pipx:
            return "pipx", [pipx, "upgrade", PACKAGE]
        return "pipx", [sys.executable, "-m", "pipx", "upgrade", PACKAGE]
    return "pip", [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]


def run_after_exit(cmd: list[str]) -> bool:
    """Windows: open a new console that waits for this Forge to close, then upgrades."""
    if os.name != "nt":
        return False
    pid = os.getpid()
    ps = ("$ErrorActionPreference='SilentlyContinue'; "
          f"Wait-Process -Id {pid} -Timeout 60; "
          "Write-Host 'Updating Forge...'; "
          f"& {' '.join(_ps_quote(c) for c in cmd)}; "
          "Write-Host ''; Write-Host 'Done. Type forge to start the new version.'; "
          "Read-Host 'Press Enter to close'")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10), close_fds=True)
        return True
    except Exception:
        return False


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
