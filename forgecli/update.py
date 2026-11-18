"""Update check and `/update` / `forge --update`.

- Once a day, in the background, Forge asks PyPI for the newest `forgecommandline` version
  (one GET to pypi.org, nothing about you or your project is sent). The answer is cached in
  ~/.forge/update.json, so the next start can say "update available" without waiting.
- Never in pipes, scripts, CI or `forge -p`. FORGE_UPDATE_CHECK=0 turns the check off.
- Auto-update (on unless FORGE_AUTO_UPDATE=0): when a newer release is known, a small hidden helper
  waits for this Forge to close and then upgrades it with pip/pipx, so the files are never replaced
  while they're in use (Windows locks forge.exe). The next start says "Updated to vX". If the
  upgrade fails it's retried next time; after two failures Forge asks with the update card instead.
  Never for the Windows .exe build, a source checkout, pipes, scripts or CI.
- /update (or `forge --update`) installs straight away.
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
    st = _load()
    st.update(latest=latest, checked=time.time())
    _save(st)
    return latest


def cached() -> str | None:
    """Newest version seen by the last check (no network)."""
    latest = _load().get("latest")
    return latest if isinstance(latest, str) and newer(latest) else None


def check_in_background() -> None:
    """At most once a day; never blocks start-up."""
    if not enabled() or time.time() - float(_load().get("checked") or 0) < EVERY:
        return
    threading.Thread(target=_check_and_arrange, daemon=True).start()


def _check_and_arrange() -> None:
    latest = fetch()
    if latest and newer(latest):
        auto_update(latest)


# auto-update -----------------------------------------------------------------------------------
MAX_FAILS = 2
_scheduled: set[str] = set()

# Runs in its own hidden process with only the standard library (never imports forgecli, whose
# files it is about to replace): wait for Forge to exit, upgrade, record how it went.
_HELPER = r"""
import json, os, subprocess, sys, time
pid, version, state, cmd = int(sys.argv[1]), sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
def alive(pid):
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)        # SYNCHRONIZE
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
start = time.time()
while alive(pid) and time.time() - start < 7 * 24 * 3600:
    time.sleep(2)
time.sleep(1)
flags = 0x08000000 if os.name == "nt" else 0                 # CREATE_NO_WINDOW
try:
    ok = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, creationflags=flags, timeout=900).returncode == 0
except Exception:
    ok = False
try:
    st = json.load(open(state, encoding="utf-8"))
except Exception:
    st = {}
auto = st.get("auto") or {}
auto.pop("pid", None)
if ok:
    st["installed"] = version
    auto["fails"] = 0
else:
    auto["fails"] = int(auto.get("fails") or 0) + 1
    auto["failed"] = version
st["auto"] = auto
os.makedirs(os.path.dirname(state), exist_ok=True)
json.dump(st, open(state, "w", encoding="utf-8"))
"""


def auto_enabled() -> bool:
    return enabled() and os.environ.get("FORGE_AUTO_UPDATE", "1").strip().lower() not in (
        "0", "off", "false", "no")


def auto_failing(latest: str) -> bool:
    """The quiet upgrade to this version keeps failing: time to ask instead."""
    auto = _load().get("auto") or {}
    return auto.get("failed") == latest and int(auto.get("fails") or 0) >= MAX_FAILS


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == 0x102
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def auto_update(latest: str) -> bool:
    """Arrange for `latest` to install once this Forge closes. True when it's arranged (or already
    was). Never asks and never interrupts; any problem just means it's tried again next start."""
    if latest in _scheduled:
        return True
    if not (auto_enabled() and newer(latest)) or auto_failing(latest):
        return False
    kind, cmd = how_installed()
    if kind not in ("pip", "pipx") or not cmd:
        return False
    st = _load()
    auto = st.get("auto") or {}
    other = auto.get("pid")
    if other and auto.get("version") == latest and _pid_alive(int(other)):
        _scheduled.add(latest)                 # another open Forge already has it lined up
        return True
    try:
        kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000   # detached, own group, no window
        else:
            kw["start_new_session"] = True
        subprocess.Popen([sys.executable, "-I", "-c", _HELPER, str(os.getpid()), latest,
                          str(_path()), json.dumps(cmd)], **kw)
    except Exception:
        return False
    auto.update(pid=os.getpid(), version=latest, at=time.time())
    st["auto"] = auto
    _save(st)
    _scheduled.add(latest)
    return True


def just_updated() -> str | None:
    """The version a quiet update installed, the first time it starts (then never again)."""
    st = _load()
    v = st.get("installed")
    if not v or v != __version__ or st.get("announced") == v:
        return None
    st["announced"] = v
    _save(st)
    return v


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
