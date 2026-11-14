"""Running shell commands without ever hanging the chat.

Output goes to a log file instead of pipes. (On Windows, a pipe stays open while any
grandchild holds it, so a server started through powershell made subprocess.run wait
forever even after its timeout.) A command that turns out to be a server (it prints a
local URL and keeps running) is moved to the background automatically, so the agent can
carry on and open it in the browser. Background processes are stopped when Forge exits."""
from __future__ import annotations

import atexit
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

LOG_DIR = Path(tempfile.gettempdir()) / "forge-cli-logs"
URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|[\w.-]+):\d{2,5}[^\s'\"<>]*", re.I)
SERVER_HINT = re.compile(r"running on|listening|serving|server (?:is )?(?:running|started)|"
                         r"press ctrl\+c|ready in|local:|started server|development server", re.I)
WIN = os.name == "nt"


@dataclass
class Proc:
    id: int
    command: str
    cwd: str
    popen: subprocess.Popen
    log: Path
    started: float = field(default_factory=time.time)

    def alive(self) -> bool:
        return self.popen.poll() is None

    def output(self, tail: int = 8000) -> str:
        try:
            data = self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return data[-tail:]

    def urls(self) -> list[str]:
        seen = []
        for u in URL_RE.findall(self.output(200_000)):
            u = u.rstrip(".,);]").replace("0.0.0.0", "127.0.0.1")
            if u not in seen:
                seen.append(u)
        return seen

    def state(self) -> str:
        return "running" if self.alive() else f"exited ({self.popen.returncode})"


_PROCS: dict[int, Proc] = {}
_LOCK = threading.Lock()
_NEXT = [1]


def argv_for(shell: str, command: str) -> list[str]:
    if shell == "powershell":
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    if shell == "pwsh":
        return ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command]
    if shell == "cmd":
        return ["cmd", "/c", command]
    return [shell, "-lc", command]


def start(shell: str, command: str, cwd: str, argv: list[str] | None = None) -> Proc:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        pid_n = _NEXT[0]
        _NEXT[0] += 1
    log = LOG_DIR / f"{os.getpid()}-{pid_n}.log"
    fh = open(log, "wb")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WIN else {"start_new_session": True}
    try:
        popen = subprocess.Popen(argv or argv_for(shell, command), cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=env, **kw)
    finally:
        fh.close()   # the child has its own handle
    p = Proc(pid_n, command, cwd, popen, log)
    with _LOCK:
        _PROCS[pid_n] = p
    return p


def kill(p: Proc) -> None:
    """Stop the process and everything it started."""
    if not p.alive():
        return
    try:
        if WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.popen.pid)],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(p.popen.pid, signal.SIGTERM)
    except Exception:
        pass
    try:
        p.popen.wait(timeout=5)
    except Exception:
        try:
            p.popen.kill()
        except Exception:
            pass


def forget(p: Proc) -> None:
    with _LOCK:
        _PROCS.pop(p.id, None)
    try:
        p.log.unlink()
    except OSError:
        pass


def get(n) -> Proc | None:
    try:
        return _PROCS.get(int(n))
    except (TypeError, ValueError):
        return None


def all_procs() -> list[Proc]:
    return list(_PROCS.values())


def looks_like_server(p: Proc) -> bool:
    out = p.output(20_000)
    return bool(URL_RE.search(out)) or bool(SERVER_HINT.search(out))


@atexit.register
def _cleanup() -> None:
    for p in all_procs():
        kill(p)
        forget(p)
