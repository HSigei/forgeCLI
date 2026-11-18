"""The actual tools. Each is small and narrow on purpose.

This file combines Nova's original app/system/screen/memory tools with Luhen's file-and-shell
tools. It also includes Claude-compatible tool aliases (bash_tool, view, create_file, str_replace)
so Nova can execute Anthropic-style system prompts.
"""
from __future__ import annotations

import difflib
import fnmatch
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from forgecli.nova.timeparse import describe, human_duration, parse_when, split_reminder_ex
from forgecli.nova.tools import apps
from forgecli.nova.tools import windows
from forgecli.nova.tools import search as drive_search
from forgecli.nova.tools.luhen_skills import SkillStore, discover_skills
from forgecli.nova.tools.base import NeedsAccess, NeedsInfo, NeedsSearch, Registry, Tool, ToolEnv, ToolError

# ─── constants ──────────────────────────────────────────────────────────────────────────────
SKIP_DIRS = {
    "node_modules", "appdata", "$recycle.bin", "__pycache__", "site-packages",
    ".git", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build", ".next", ".idea", ".vscode", "target",
    "Local", "LocalLow", "Roaming", "Temp", "tmp", "cache", "caches", "Cache", "Caches",
    "Windows", "Program Files", "Program Files (x86)", "ProgramData", "System Volume Information",
    "Recovery", "Boot", "PerfLogs", "Intel", "NVIDIA", "AMD", "Microsoft",
    ".gradle", ".m2", ".npm", ".cache", ".config", ".local", ".rustup", ".cargo", ".go", ".docker",
    "bower_components", "vendor", "packages", "bin", "obj", "Debug", "Release", "__MACOSX",
}

TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".log", ".rtf", ".csv", ".tsv",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".vue", ".svelte", ".astro",
    ".py", ".pyi", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".java", ".kt", ".swift", ".go", ".rs", ".rb", ".php", ".lua", ".pl",
    ".sql", ".xml", ".graphql", ".proto", ".dockerfile", ".gitignore", ".gitattributes",
}

SAFE_OPEN_EXTENSIONS = TEXT_EXTENSIONS | {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".mp3", ".wav", ".flac", ".mp4", ".mkv", ".avi", ".mov",
    ".docx", ".xlsx", ".pptx",
}

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_RISKY = [(re.compile(p, re.I), why) for p, why in [
    (r"\b(rm|rmdir|del|erase|rd)\b", "deletes files"),
    (r"\bremove-item\b", "deletes files"),
    (r"\bclear-content\b|\bset-content\b.*-force", "overwrites files"),
    (r"\bmove-item\b|\bmv\b|\bren(ame)?\b|\brename-item\b", "moves or renames files"),
    (r"\bgit\s+(push|reset\s+--hard|clean|checkout\s+--|restore|rebase|branch\s+-D|stash\s+drop)", "changes git history or discards work"),
    (r"\bformat\b|\bdiskpart\b|\bmkfs\b|\bdd\s+if=", "touches disks"),
    (r"\b(shutdown|reboot|restart-computer|stop-computer|logoff|poweroff)\b", "shuts the machine down"),
    (r"\breg(\.exe)?\s+(add|delete|import)|\bset-itemproperty\b|\bnew-itemproperty\b|hklm:|hkcu:", "edits the registry"),
    (r"\b(stop-process|taskkill|kill|pkill|killall)\b", "kills processes"),
    (r"\bset-executionpolicy\b|\bnetsh\b|\bsc(\.exe)?\s+(delete|config|stop)|\bchmod\s+-R\b|\bchown\b", "changes system settings"),
    (r"\b(npm|pnpm|yarn)\s+(i|install|add)\s+(-g|--global)\b", "installs globally"),
    (r"\bchoco\s+install|\bwinget\s+(install|uninstall)|\bscoop\s+install|\bapt(-get)?\s+(install|remove)|\bbrew\s+(install|uninstall)", "installs software"),
    (r"\bpip\s+uninstall|\bnpm\s+uninstall", "uninstalls software"),
    (r"(curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)[^|]*\|\s*(sh|bash|iex|invoke-expression|python|powershell)", "runs a downloaded script"),
    (r"\biex\b|\binvoke-expression\b|\beval\b", "runs generated code"),
    (r"\bsudo\b|\brunas\b", "asks for admin rights"),
    (r"\bc:\\windows\b|\bc:\\program files|>\s*/etc/|>\s*/usr/", "writes into system folders"),
    (r"\b(docker|kubectl)\s+(rm|rmi|delete|prune|system\s+prune)", "deletes containers or resources"),
    (r"\bdrop\s+(table|database)\b|\btruncate\s+table\b", "deletes database data"),
]]

_FORBIDDEN = [re.compile(p, re.I) for p in (
    r"\brm\s+(-[a-z]*\s+)*(/|~|\$home|\*)(\s|$)",
    r"\bformat\s+[a-z]:",
    r"\bmkfs\b.*\s/dev/",
    r"\bdd\s+if=.*\sof=/dev/(sd|nvme|disk)",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\bremove-item\b.*\s-recurse.*\s[a-z]:\\\s*$",
    r"\brd\s+/s\s+/q\s+[a-z]:\\\s*$",
)]


def shell_risk(command: str) -> str:
    for rx, why in _RISKY:
        if rx.search(command):
            return why
    return ""


def inside(path, root) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


class Checkpoints:
    """File snapshots grouped by user request: undo puts back every file the last request changed."""

    def __init__(self):
        self.turns: list[dict] = []          # {"label", "time", "files": [(Path, bytes | None)]}

    def begin(self, label: str = ""):
        """A new user request starts; empty requests are dropped so undo skips them."""
        self.turns = [t for t in self.turns if t["files"]][-30:]
        self.turns.append({"label": label[:120], "time": time.time(), "files": []})

    def new_turn(self):                      # older callers: only opens a group when none exists
        if not self.turns:
            self.begin()

    def capture(self, path):
        p = Path(path)
        if not self.turns:
            self.begin()
        files = self.turns[-1]["files"]
        if any(q == p for q, _ in files):
            return                           # keep the state from before this request's first change
        try:
            old = p.read_bytes() if p.is_file() else None
        except OSError:
            old = None
        files.append((p, old))

    def history(self) -> list[dict]:
        return [{"index": i, "label": t["label"], "time": t["time"], "files": [str(f) for f, _ in t["files"]]}
                for i, t in enumerate(self.turns) if t["files"]]

    def undo(self, steps: int = 1):
        done = []
        for _ in range(max(1, steps)):
            while self.turns and not self.turns[-1]["files"]:
                self.turns.pop()
            if not self.turns:
                break
            for p, old in reversed(self.turns.pop()["files"]):
                try:
                    if old is None:
                        if p.exists():
                            p.unlink()
                            done.append(f"removed {p}")
                    else:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(old)
                        done.append(f"restored {p}")
                except OSError as exc:
                    done.append(f"could not undo {p}: {exc}")
        return done


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _require_windows(what: str) -> None:
    if os.name != "nt":
        raise ToolError(f"{what} only works on Windows.")


def _clip(text: str, limit: int = 20_000) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit * 2 // 3] + f"\n... [{len(text) - limit} characters cut] ...\n" + text[-limit // 3:]


def _binary(p) -> bool:
    try:
        with open(p, "rb") as fh:
            return b"\0" in fh.read(2048)
    except OSError:
        return False


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "yes", "1")


def app_exists(name: str) -> bool:
    name = str(name or "").strip()
    return bool(name) and apps.app_exists(name)

def knows_exactly(name: str) -> bool:
    return apps.knows_exactly(name)


# ─── app + URL tools ────────────────────────────────────────────────────────────────────────
_SPLIT_APPS = re.compile(r"\s*(?:,|&|\band\b|\bthen\b|\bna\b)\s*", re.I)

def _open_one(name: str, env: ToolEnv) -> str:
    cleaned = apps.norm(name)
    folder = _shell_folder(cleaned, env)
    if folder is not None:
        os.startfile(str(folder))
        return f"Opening your {folder.name} folder."
    return apps.open_app(name, env.cfg)


def _t_open_app(args: dict, env: ToolEnv) -> str:
    _require_windows("Opening apps")
    name = str(args.get("name", "")).strip()
    if not apps.norm(name):
        raise ToolError("Which app should I open?")
    if re.search(r"[\\/]|^[a-z]:|\.(?:exe|bat|cmd|ps1|msi|lnk)$", name, re.I):
        raise ToolError("I open apps by their name, like 'chrome' or 'notepad', not by a file path.")
    parts = [x for x in _SPLIT_APPS.split(name) if x.strip()]
    if len(parts) > 1 and not apps.knows_exactly(name, env.cfg):
        done, problems = [], []
        for part in parts:
            try:
                done.append(_open_one(part, env).rstrip("."))
            except ToolError as exc:
                problems.append(str(exc))
        if not done:
            raise ToolError(" ".join(problems))
        return ". ".join(done) + "." + ((" But: " + " ".join(problems)) if problems else "")
    return _open_one(name, env)


def _t_list_apps(args: dict, env: ToolEnv) -> str:
    query = str(args.get("query", "")).strip()
    cat = apps.get_catalog(env.cfg)
    if query:
        names = cat.search(query, limit=20)
        if names and len(names) > 1:
            return f"Installed apps that fit '{query}': " + ", ".join(names) + "."
        return cat.describe(query)
    names = cat.search("", limit=25)
    if not names:
        return "I couldn't read the list of installed apps."
    return "Some of the apps installed on this PC: " + ", ".join(names) + "."


def _closer(env: ToolEnv) -> "windows.Closer":
    closer = getattr(env, "closer", None)
    if closer is None:
        closer = env.closer = windows.Closer(windows.RealBackend(), cfg=env.cfg)
    return closer


def _t_close_app(args: dict, env: ToolEnv) -> str:
    _require_windows("Closing apps")
    name = str(args.get("name", "")).strip()
    if re.search(r"[\\/]|^[a-z]:", name, re.I):
        raise ToolError("I close apps by their name, like 'chrome' or 'notepad', not by a file path.")
    return _closer(env).close_app(name, args)


def _t_close_window(args: dict, env: ToolEnv) -> str:
    _require_windows("Closing windows")
    return _closer(env).close_window(str(args.get("title", "")).strip(), args)


def _t_list_windows(args: dict, env: ToolEnv) -> str:
    _require_windows("Listing open windows")
    return _closer(env).list_open()


def _t_open_url(args: dict, env: ToolEnv) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        raise ToolError("Which web address?")
    if "://" not in url:
        url = "https://" + url
    if not re.match(r"^https?://[^\s]+$", url, re.I):
        raise ToolError("I can only open normal web addresses (http or https).")
    webbrowser.open(url)
    return f"Opening {url}."


SHELL_FOLDERS = ("downloads", "documents", "desktop", "pictures", "music", "videos")

def _shell_folder(name: str, env: ToolEnv) -> Path | None:
    key = re.sub(r"^(?:my|the)\s+", "", name.strip().lower())
    if key in env.aliases and key != "workspace":
        return env.aliases[key]
    if key in SHELL_FOLDERS:
        p = Path.home() / key.capitalize()
        return p if p.is_dir() else None
    return None


# ─── find ──────────────────────────────────────────────────────────────────────────────────
def _search_roots(env: ToolEnv) -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        import ctypes
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            try:
                if not drive.exists(): continue
                if not (ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") & 0x3): continue
                if (drive / "Windows").exists():
                    users = drive / "Users"
                    if users.exists(): roots.append(users)
                else:
                    roots.append(drive)
            except Exception:
                continue
    else:
        roots.append(Path("/"))
    for extra in (Path.home(), env.workspace):
        if extra not in roots: roots.append(extra)
    return roots


def _t_find(args: dict, env: ToolEnv) -> str:
    name = str(args.get("name") or args.get("query") or "").strip()
    if not name:
        raise ToolError("Give a name (or part of a name) to look for.")
    kind = args.get("kind") if args.get("kind") in ("file", "folder", "any") else "any"
    try: limit = max(1, min(int(args.get("limit") or 50), 500))
    except (TypeError, ValueError): limit = 50
    needle = name.lower()
    hits: list[Path] = []
    scanned = 0
    for root in _search_roots(env):
        try:
            for dirpath, dirs, files in os.walk(root, onerror=lambda e: None):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                scanned += 1
                p = Path(dirpath)
                if kind in ("folder", "any") and needle in p.name.lower(): hits.append(p)
                if kind in ("file", "any"):
                    for f in files:
                        if needle in f.lower(): hits.append(p / f)
                if len(hits) >= limit: break
        except OSError: continue
        if len(hits) >= limit: break
    if not hits:
        return f"(no {kind} matching {name!r} found - I searched {scanned:,} folders, skipping system folders.)"
    shown = "\n".join(str(h) for h in hits[:limit])
    more = f"\n... and {len(hits) - limit} more (stopped at {limit})" if len(hits) > limit else ""
    return shown + more


# ─── read/write/edit ────────────────────────────────────────────────────────────────────────
def _verify_write(path: Path, expected: str, action: str) -> None:
    try: written = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc: raise ToolError(f"{action} but couldn't read it back: {exc}") from None
    if written != expected:
        raise ToolError(f"{action} but the file's contents don't match what I sent. (Read {len(written)} chars, expected {len(expected)}.)")


def _checkpoints(env: ToolEnv) -> Checkpoints:
    cp = getattr(env, "_luhen_checkpoints", None)
    if cp is None: cp = env._luhen_checkpoints = Checkpoints()
    return cp


def _syntax_note(p: Path) -> str:
    try:
        if p.suffix == ".py": compile(p.read_text(encoding="utf-8"), str(p), "exec")
        elif p.suffix == ".json": json.loads(p.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError) as exc: return f" WARNING: the file now has a syntax error: {exc}"
    return ""


def _t_read_file(args: dict, env: ToolEnv) -> str:
    raw = args.get("path")
    p = env.resolve_path(raw)
    if not p.is_file(): raise ToolError(f"I can't find a file called {p.name}.")
    if p.suffix.lower() not in TEXT_EXTENSIONS:
        from forgecli.nova.tools.power import IMAGE_EXTENSIONS, DOC_EXTENSIONS, _t_read_document
        if p.suffix.lower() in IMAGE_EXTENSIONS | DOC_EXTENSIONS: return _t_read_document(args, env)
        if p.stat().st_size > 2_000_000 or b"\0" in p.read_bytes()[:4096]:   # unknown extension: fine if it is plain text
            raise ToolError(f"{p.name} is a binary file ({p.suffix}); I can't read it as text.")
    try:
        offset = max(1, int(args.get("offset", 1)))
        limit = max(1, min(int(args.get("limit", 400)), 2000))
    except (TypeError, ValueError): offset, limit = 1, 400
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    chunk = lines[offset - 1: offset - 1 + limit]
    if not chunk: return f"(empty, or offset past the end; the file has {len(lines)} lines)"
    body = "\n".join(f"{i:>6}\t{ln}" for i, ln in enumerate(chunk, offset))
    end = offset + len(chunk) - 1
    tail = f"\n(showing lines {offset}-{end} of {len(lines)})" if end < len(lines) or offset > 1 else ""
    return body + tail


def _t_view(args: dict, env: ToolEnv) -> str:
    if env.resolve_path(args.get("path")).is_dir():
        from forgecli.nova.tools.dev import _t_list_dir
        return _t_list_dir(args, env)
    return _t_read_file(args, env)


def _t_write_file(args: dict, env: ToolEnv) -> str:
    p = env.resolve_path(args.get("path"))
    content = str(args.get("content") or args.get("file_text") or "")
    if len(content) > 2_000_000: raise ToolError("That's too much text for one write (2 MB limit).")
    exists = p.exists()
    if exists and p.is_dir(): raise ToolError(f"{p} is a folder.")
    old = p.read_text(encoding="utf-8", errors="replace") if exists else ""
    if exists and old == content: raise ToolError(f"{p} already has exactly this content; nothing changed.")
    if exists and not _truthy(args.get("overwrite")): raise ToolError(f"{p.name} already exists. Tell me if I should replace it.")
    _checkpoints(env).capture(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    try: p.write_text(content, encoding="utf-8")
    except OSError as exc: raise ToolError(f"I couldn't write {p}: {exc}") from None
    _verify_write(p, content, f"Saved {p.name}")
    return f"Wrote {len(content)} characters to {p}." + _syntax_note(p)


def _t_edit_file(args: dict, env: ToolEnv) -> str:
    p = env.resolve_path(args.get("path"))
    old_string = str(args.get("old_string") or args.get("old_str") or "")
    new_string = str(args.get("new_string") or args.get("new_str") or "")
    replace_all = _truthy(args.get("replace_all"))
    if not p.is_file(): raise ToolError(f"I can't find a file called {p.name}.")
    if old_string == new_string: raise ToolError("old_string and new_string are the same.")
    if not old_string: raise ToolError("old_string is empty. Copy the exact text to replace.")
    text = p.read_text(encoding="utf-8")
    n = text.count(old_string)
    if n == 0: raise ToolError("old_string not found. Read the file again and copy the text exactly.")
    if n > 1 and not replace_all: raise ToolError(f"old_string occurs {n} times. Add context or set replace_all.")
    new = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    _checkpoints(env).capture(p)
    try: p.write_text(new, encoding="utf-8")
    except OSError as exc: raise ToolError(f"I couldn't edit {p}: {exc}") from None
    _verify_write(p, new, f"Edited {p.name}")
    return f"Edited {p} ({n if replace_all else 1} replacement(s))." + _syntax_note(p)


def _t_undo(args: dict, env: ToolEnv) -> str:
    try: steps = int(args.get("steps") or 1)
    except (TypeError, ValueError): steps = 1
    done = _checkpoints(env).undo(steps)
    if not done: return "Nothing to undo."
    return "\n".join(done)


# ─── glob / grep ────────────────────────────────────────────────────────────────────────────
def _walk(base: Path):
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(files): yield Path(dirpath) / f


def _t_glob(args: dict, env: ToolEnv) -> str:
    base = env.resolve_path(args.get("path") or ".")
    pattern = str(args.get("pattern", "")).strip()
    if not pattern: raise ToolError("Which pattern, like '**/*.py'?")
    if not base.is_dir(): raise ToolError(f"Not a folder: {base}")
    hits = []
    for f in _walk(base):
        rel = f.relative_to(base).as_posix()
        if (fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern) or fnmatch.fnmatch(rel, pattern.replace("**/", ""))):
            hits.append(rel)
            if len(hits) >= 300: break
    return "\n".join(hits) if hits else "(no matches)"


def _t_grep(args: dict, env: ToolEnv) -> str:
    base = env.resolve_path(args.get("path") or ".")
    pattern = str(args.get("pattern", "")).strip()
    glob = args.get("glob")
    ignore_case = _truthy(args.get("ignore_case"))
    if not pattern: raise ToolError("Which regular expression?")
    try: rx = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as exc: raise ToolError(f"Bad regular expression: {exc}") from None
    files = [base] if base.is_file() else list(_walk(base))
    hits = []
    scanned = 0
    for f in files:
        if glob and not fnmatch.fnmatch(f.name, glob): continue
        try:
            if f.stat().st_size > 2_000_000 or _binary(f): continue
            scanned += 1
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 150: return "\n".join(hits) + "\n(stopped at 150 matches)"
        except OSError: continue
    return "\n".join(hits) if hits else f"(no matches in {scanned} files)"


# ─── open_path ──────────────────────────────────────────────────────────────────────────────
def _t_open_path(args: dict, env: ToolEnv) -> str:
    _require_windows("Opening paths")
    target = str(args.get("target") or args.get("path") or "").strip()
    if not target: raise ToolError("What should I open (a file, a folder, or a web address)?")
    if re.match(r"^https?://", target, re.I):
        webbrowser.open(target)
        return f"Opened {target} in the browser."
    p = env.resolve_path(target)
    if not p.exists(): raise ToolError(f"Not found: {p}")
    if p.is_file() and p.suffix.lower() not in SAFE_OPEN_EXTENSIONS: raise ToolError(f"I won't open {p.suffix or 'that kind of'} files - they could run programs.")
    os.startfile(str(p))
    return f"Opened {p}."


# ─── run_shell ──────────────────────────────────────────────────────────────────────────────
def _fresh_env() -> dict:
    env = os.environ.copy()
    if os.name != "nt": return env
    try:
        import winreg
        extra = []
        for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"), (winreg.HKEY_CURRENT_USER, "Environment")):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    value, _ = winreg.QueryValueEx(key, "Path")
                    extra += [x for x in os.path.expandvars(str(value)).split(os.pathsep) if x.strip()]
            except OSError: pass
        have = [x for x in env.get("PATH", "").split(os.pathsep) if x.strip()]
        seen = {os.path.normcase(x.rstrip("\\/")) for x in have}
        for x in extra:
            if os.path.normcase(x.rstrip("\\/")) not in seen:
                have.append(x); seen.add(os.path.normcase(x.rstrip("\\/")))
        env["PATH"] = os.pathsep.join(have)
    except Exception: pass
    return env


_LIVE: set = set()
_LIVE_LOCK = threading.Lock()


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None: return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15, creationflags=NO_WINDOW)
    else:
        try: os.killpg(proc.pid, 9)
        except OSError: proc.kill()


def kill_running() -> int:
    """Kill every command a tool is still waiting on (the Stop button). Returns how many."""
    with _LIVE_LOCK: procs = list(_LIVE)
    for p in procs:
        try: _kill_tree(p)
        except Exception: pass
    return len(procs)


def run_capture(argv: list[str], cwd, timeout: float, stdin: str | None = None) -> subprocess.CompletedProcess:
    """subprocess.run that can't hang: stdin is closed (or fed), and on timeout or Stop the
    whole process tree dies. Plain run(timeout=) only kills the shell; a grandchild keeps the
    pipes open and the read blocks forever."""
    proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_fresh_env(),
                            creationflags=NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                            start_new_session=os.name != "nt")
    with _LIVE_LOCK: _LIVE.add(proc)
    try:
        try:
            out, err = proc.communicate(stdin.encode() if stdin is not None else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try: proc.communicate(timeout=5)
            except subprocess.TimeoutExpired: pass
            raise
    finally:
        with _LIVE_LOCK: _LIVE.discard(proc)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _pick_shell() -> str:
    if os.name == "nt":
        for exe in ("pwsh", "powershell"):
            if shutil.which(exe): return exe
        return "cmd"
    return "bash" if shutil.which("bash") else "sh"


def _sum_shell(args: dict, env: ToolEnv) -> str:
    command = str(args.get("command", ""))
    risky = shell_risk(command)
    return (f"Warning: this command {risky}.\n" if risky else "") + f"Run this command:\n{command}"


def _t_run_shell(args: dict, env: ToolEnv) -> str:
    _require_windows("Running commands")
    if not env.cfg.get("enable_shell"): raise ToolError("Running commands is switched off in config.json (tools.enable_shell).")
    command = str(args.get("command", "")).strip()
    if not command: raise ToolError("What command should I run?")
    for rx in _FORBIDDEN:
        if rx.search(command): raise ToolError("That command is refused because it could wipe the computer.")
    try: timeout = max(1, min(int(args.get("timeout", 120)), 900))
    except (TypeError, ValueError): timeout = 120
    cwd = env.resolve_path(args.get("cwd") or ".")
    if not cwd.is_dir(): raise ToolError(f"Folder not found: {cwd}")
    shell = _pick_shell()
    if shell == "cmd": argv = ["cmd", "/c", command]
    elif shell in ("pwsh", "powershell"): argv = [shell, "-NoProfile", "-NonInteractive", "-Command", command]
    else: argv = [shell, "-c", command]
    try:
        proc = run_capture(argv, cwd, timeout)
    except subprocess.TimeoutExpired: raise ToolError(f"The command took longer than {timeout}s and I stopped it. If it waits for input or runs forever (a server, a watcher), use the process tool instead.") from None
    out = (proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")).strip()
    if proc.returncode != 0:
        raise ToolError(f"The command failed (exit {proc.returncode}).\n{_clip(out, 14_000)}" if out else f"The command failed (exit {proc.returncode}).")
    return "The command finished (exit 0)." + (f"\n{_clip(out, 14_000)}" if out else " (no output)")


# ─── web tools ──────────────────────────────────────────────────────────────────────────────
def _t_web_search(args: dict, env: ToolEnv) -> str:
    query = urllib.parse.quote(str(args.get("query", "")))
    if not query: raise ToolError("What should I search for?")
    req = urllib.request.Request(f"https://html.duckduckgo.com/html/?q={query}", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            import re
            results = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>.*?<a class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
            out = []
            for link, title, snippet in results[:5]:
                clean_title = re.sub('<[^>]+>', '', title).strip()
                clean_snippet = re.sub('<[^>]+>', '', snippet).strip()
                out.append(f"{clean_title}\n{link}\n{clean_snippet}\n")
            return "\n".join(out) if out else "No results found."
    except Exception as e: raise ToolError(f"Search failed: {e}")


def _t_web_fetch(args: dict, env: ToolEnv) -> str:
    url = str(args.get("url", "")).strip()
    if not url.startswith(("http://", "https://")): raise ToolError("URL must start with http:// or https://")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            import re
            html = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", html, flags=re.DOTALL|re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:8000]
    except Exception as e: raise ToolError(f"Fetch failed: {e}")


# ─── system + media ─────────────────────────────────────────────────────────────────────────
def _t_system_info(args: dict, env: ToolEnv) -> str:
    try: import psutil
    except ImportError: raise ToolError("I need the 'psutil' package: pip install psutil") from None
    procs = [p for p in psutil.process_iter(["name"]) if p.pid != 0 and (p.info.get("name") or "") != "System Idle Process"]
    cores = psutil.cpu_count() or 1
    for p in procs:
        try: p.cpu_percent(None)
        except Exception: pass
    cpu = psutil.cpu_percent(interval=0.6)
    mem = psutil.virtual_memory()
    rows = []
    for p in procs:
        try: rows.append((p.cpu_percent(None) / cores, p.memory_info().rss, p.info.get("name") or "?"))
        except Exception: continue
    top_cpu = sorted(rows, key=lambda r: r[0], reverse=True)[:4]
    top_mem = sorted(rows, key=lambda r: r[1], reverse=True)[:4]
    lines = [f"CPU is at {cpu:.0f}% and memory is {mem.percent:.0f}% used ({_size(mem.used)} of {_size(mem.total)})."]
    lines.append("Busiest by CPU: " + ", ".join(f"{n} {c:.0f}%" for c, _m, n in top_cpu))
    lines.append("Biggest by memory: " + ", ".join(f"{n} {_size(m)}" for _c, m, n in top_mem))
    return "\n".join(lines)


def _t_get_datetime(args: dict, env: ToolEnv) -> str:
    now = datetime.now()
    return f"It's {now:%A, %d %B %Y}, {now:%H:%M}."


MEDIA_KEYS = {"volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD, "next": 0xB0, "previous": 0xB1, "play_pause": 0xB3}

def _t_media_control(args: dict, env: ToolEnv) -> str:
    _require_windows("Media keys")
    import ctypes
    action = str(args.get("action", "")).strip().lower()
    if action not in MEDIA_KEYS: raise ToolError("Choose one of: " + ", ".join(MEDIA_KEYS))
    try: steps = max(1, min(int(args.get("steps", 5 if action.startswith("volume") else 1)), 25))
    except (TypeError, ValueError): steps = 1
    vk = MEDIA_KEYS[action]
    user32 = ctypes.windll.user32
    for _ in range(steps):
        user32.keybd_event(vk, 0, 0, 0); user32.keybd_event(vk, 0, 2, 0); time.sleep(0.02)
    return {"volume_up": "Turned the volume up.", "volume_down": "Turned the volume down.", "mute": "Toggled mute.", "next": "Skipped to the next track.", "previous": "Went back a track.", "play_pause": "Toggled play and pause."}[action]


# ─── skills ─────────────────────────────────────────────────────────────────────────────────
def _t_find_skill(args: dict, env: ToolEnv) -> str:
    store: SkillStore = env.skills
    query = str(args.get("query", ""))
    hits = store.find(query)
    archived = [p.stem for p in (store.root / ".archive").glob("*.md")
                if any(w in p.stem for w in re.findall(r"\w{4,}", query.lower()))] if (store.root / ".archive").exists() else []
    tail = (f"\nArchived skills that may fit: {', '.join(archived)} (skill_manage action=restore name=...)" if archived else "")
    if not hits: return "No skill matches. Carry on without one; if you work out a repeatable procedure, save it with skill_create." + tail
    return "\n".join(f"- {s['name']}: {s['description'][:160]}" for s in hits) + "\nCall use_skill with the exact name of the best match." + tail

def _t_use_skill(args: dict, env: ToolEnv) -> str:
    store: SkillStore = env.skills
    skill = store.get(str(args.get("name", "")).strip().lower())
    if skill is None:
        near = store.find(str(args.get("name", "")), 5)
        msg = f"No skill named '{args.get('name')}'."
        if near: msg += " Did you mean: " + ", ".join(x["name"] for x in near)
        raise ToolError(msg)
    from forgecli.nova.tools.learning import lessons_for, record_use
    try: record_use(skill["name"])
    except Exception: pass
    lessons = lessons_for(skill["name"])
    extra = ("\n\n## Lessons learned since this was written (follow these too)\n" + "\n".join(f"- {l}" for l in lessons)) if lessons else ""
    return skill["full"] + extra + ("\n\n(If this skill turns out wrong or incomplete, record the fix with skill_update lesson=...)")


# ─── screen & camera ────────────────────────────────────────────────────────────────────────
def _screen(env: ToolEnv):
    if env.screen is None: raise ToolError("Screen features are switched off (awareness.enabled in config.json).")
    return env.screen

def _t_active_window(args: dict, env: ToolEnv) -> str:
    s = _screen(env)
    info = s.foreground_info()
    if not info: raise ToolError("I couldn't tell which window is in front.")
    return f"You're using {s.label(info)}."

def _t_read_screen(args: dict, env: ToolEnv) -> str:
    s = _screen(env)
    info = s.foreground_info()
    window_only = _truthy(args.get("window_only"))
    png = s.capture_png(info["rect"] if (window_only and info) else None)
    text = s.ocr_png(png).strip()
    head = f"Text on screen (active window: {s.label(info)})."
    if len(text) < 40 and s.cfg.get("vision_fallback", True) and not window_only:
        try:
            seen = s.ask_vision(png, "Describe what is on this computer screen in two or three sentences.")
            return (head + "\n(There is hardly any readable text, so this is a description of the picture.)\n" + seen + (f"\nText that was readable: {text}" if text else ""))
        except ToolError: pass
    if not text: return head + "\n(I couldn't read any text.)"
    return head + "\n" + text[:3000]

def _t_describe_screen(args: dict, env: ToolEnv) -> str:
    s = _screen(env)
    question = str(args.get("question") or "Describe what is on this computer screen in two or three sentences.")
    return s.ask_vision(s.capture_png(), question[:300])

def _t_save_screenshot(args: dict, env: ToolEnv) -> str:
    s = _screen(env)
    folder = env.workspace / "screenshots"
    folder.mkdir(exist_ok=True)
    path = folder / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
    path.write_bytes(s.capture_png())
    return f"Saved a screenshot to {path}."

def _t_look_with_camera(args: dict, env: ToolEnv) -> str:
    s = _screen(env)
    question = str(args.get("question") or "Describe what you see in this picture in two or three sentences.")
    return s.ask_vision(s.camera_jpeg(), question[:300])


# ─── memory ─────────────────────────────────────────────────────────────────────────────────
def _mem(env: ToolEnv):
    if env.memory is None: raise ToolError("My memory is switched off (memory.enabled in config.json).")
    return env.memory

def _t_remember(args: dict, env: ToolEnv) -> str:
    store = _mem(env)
    text = str(args.get("text", "")).strip()
    pinned = bool(args.get("pin")) or bool(re.search(r"\b(?:name is|call (?:me|him|her))\b", text, re.I))
    kind = str(args.get("kind") or "").lower() or None
    res = store.add(text, kind, "you", pinned=pinned)
    if res["status"] == "refused": raise ToolError(res["reason"])
    if res["status"] != "added": return "I already knew that."
    return "Saved as a standing rule - I'll check it before every related action." if res.get("kind") == "rule" else "Okay, I'll remember that."

def _fmt_memories(rows: list[dict]) -> list[str]:
    def tag(m):
        bits = [m.get("kind", "fact")] + ([m["tier"]] if m.get("tier", "active") != "active" else [])
        return f" [#{m['id']} {', '.join(bits)}]"
    return [("* " if m["pinned"] else "- ") + m["text"] + tag(m) for m in rows]

def _t_recall(args: dict, env: ToolEnv) -> str:
    store = _mem(env)
    query = str(args.get("query", "")).strip()
    rows = store.search(query, 8) if query else store.list()[:15]
    if not rows: return "I don't have anything saved about that yet." if query else "I don't remember anything about you yet."
    head = f"I remember {len(rows)} thing{'s' if len(rows) != 1 else ''}" + (f" about '{query}'." if query else ".")
    return head + "\n" + "\n".join(_fmt_memories(rows))

def _t_memory_manage(args: dict, env: ToolEnv) -> str:
    store = _mem(env)
    action = str(args.get("action") or "status").lower()
    if action == "status":
        layers = store.layers()
        rules = store.rules()
        return (f"Memory layers: core {layers['core']} (pinned + rules), active {layers['active']}, stale {layers['stale']}, "
                f"archived {layers['archived']}.\nStanding rules:\n" + ("\n".join(_fmt_memories(rules)) or "- none"))
    if action == "curate":
        res = store.curate()
        return f"Layers updated: {res['moved']}. Snapshot: {res['snapshot']}"
    if action == "archived":
        rows = [m for m in store.list() if m["tier"] == "archived"]
        return "\n".join(_fmt_memories(rows[:40])) or "Nothing is archived."
    try: mid = int(str(args.get("id") or "").lstrip("#"))
    except ValueError: raise ToolError("Give the memory's id (the #number recall shows).") from None
    if action in ("pin", "unpin"):
        ok = store.update(mid, pinned=action == "pin")
    elif action == "rule":
        ok = store.update(mid, kind="rule")
    elif action in ("restore", "archive"):
        ok = store.set_tier(mid, "active" if action == "restore" else "archived")
    else:
        raise ToolError("action must be status, curate, archived, pin, unpin, rule, restore or archive.")
    if not ok: raise ToolError(f"No memory #{mid}.")
    return f"Done: memory #{mid} {action}."

def _forget_matches(args: dict, env: ToolEnv) -> list[dict]:
    store = _mem(env)
    query = str(args.get("query", "")).strip()
    if query.lower() in ("__all__", "everything", "all"): return store.list()
    rows = store.search(query, 5)
    if not rows: raise ToolError(f"I don't have a memory matching '{query}'.")
    return rows

def _t_forget(args: dict, env: ToolEnv) -> str:
    store = _mem(env)
    rows = _forget_matches(args, env)
    for m in rows: store.delete(m["id"])
    return f"Forgot {len(rows)} thing{'s' if len(rows) != 1 else ''}."

def _sum_forget(args: dict, env: ToolEnv) -> str:
    rows = _forget_matches(args, env)
    if str(args.get("query", "")).strip().lower() in ("__all__", "everything", "all"):
        return f"Forget EVERYTHING I remember about you ({len(rows)} memories)."
    return "Forget " + ("this:" if len(rows) == 1 else "these:") + "\n" + "\n".join(_fmt_memories(rows))


# ─── reminders ──────────────────────────────────────────────────────────────────────────────
def _sched(env: ToolEnv):
    if env.scheduler is None: raise ToolError("Reminders are switched off (schedule.enabled in config.json).")
    return env.scheduler

def _t_set_reminder(args: dict, env: ToolEnv) -> str:
    store = _sched(env)
    text = str(args.get("text", "")).strip()
    when = str(args.get("when", "")).strip()
    lead = str(args.get("lead", "")).strip().lower()
    kind = "timer" if str(args.get("kind", "")).lower() == "timer" else "reminder"
    if not when:
        sp = split_reminder_ex(text)
        if sp: text, when, lead = sp
    base = {"tool": "set_reminder", "args": {"text": text, "lead": lead, "kind": kind}}
    if not when: raise NeedsInfo("When should I remind you?", {**base, "field": "when"})
    parsed = parse_when(when)
    if parsed is None: raise NeedsInfo("I couldn't tell when you meant. When should I remind you?", {**base, "field": "when"})
    if kind == "reminder" and text.lower() in ("", "reminder"): raise NeedsInfo("What should I remind you about?", {"tool": "set_reminder", "field": "text", "args": {"when": when, "kind": kind}})
    try: store.add(text or "Reminder", parsed["due"], parsed["recur"], kind, lead)
    except ValueError as exc: raise ToolError(str(exc)) from None
    now = datetime.now()
    if kind == "timer": return f"Timer set for {human_duration((parsed['due'] - now).total_seconds())}."
    label = describe(parsed["due"], parsed["recur"], now)
    if lead in ("to", "that", "about", "of"): return f"Okay, I'll remind you {lead} {text} {label}."
    return f"Okay, I'll remind you {label}: {text}."

def _t_list_reminders(args: dict, env: ToolEnv) -> str:
    items = _sched(env).list()
    if not items: return "You don't have any reminders or timers."
    lines = [f"You have {len(items)} reminder{'s' if len(items) != 1 else ''} or timer{'s' if len(items) != 1 else ''}."]
    for i in items[:15]: lines.append(f"- {i['text']} ({describe(datetime.fromtimestamp(i['due']), i['recur'])})")
    return "\n".join(lines)

def _t_cancel_reminder(args: dict, env: ToolEnv) -> str:
    found = _sched(env).cancel(str(args.get("query", "")).strip() or "__all__")
    if not found: raise ToolError("I couldn't find a matching reminder.")
    return f"Cancelled {len(found)} reminder{'s' if len(found) != 1 else ''}."


def _t_delete_file(args: dict, env: ToolEnv) -> str:
    _require_windows("The Recycle Bin")
    import subprocess
    p = env.resolve_path(args.get("path"))
    if not p.exists():
        raise ToolError(f"I can't find {p.name}.")
    if not p.is_file():
        raise ToolError("I can only delete single files, not folders.")
    script = (
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
        f"'{str(p).replace(chr(39), chr(39) * 2)}', 'OnlyErrorDialogs', 'SendToRecycleBin')"
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=30, creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        raise ToolError("Windows wouldn't let me delete that file.")
    if p.exists():
        raise ToolError(f"The command succeeded but {p.name} is still there.")
    return f"Moved {p.name} to the Recycle Bin."

def _sum_delete(args, env):
    return f"Move this file to the Recycle Bin:\n{env.resolve_path(args.get('path'))}"

def _t_move_file(args: dict, env: ToolEnv) -> str:
    import shutil
    src = env.resolve_path(args.get("source"))
    dst = env.resolve_path(args.get("destination"))
    if not src.exists():
        raise ToolError(f"I can't find {src.name}.")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise ToolError(f"{dst.name} already exists there.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
    except OSError as exc:
        raise ToolError(f"I couldn't move {src.name}: {exc}") from None
    if src.exists() or not dst.exists():
        raise ToolError(f"The move didn't fully complete. Check {src} and {dst}.")
    return f"Moved {src.name} to {dst.parent}."

def _sum_move(args, env):
    return f"Move\n{env.resolve_path(args.get('source'))}\nto\n{env.resolve_path(args.get('destination'))}"


# ─── registry ───────────────────────────────────────────────────────────────────────────────
def _sum_delete(args, env): return f"Move this file to the Recycle Bin:\n{env.resolve_path(args.get('path'))}"
def _sum_move(args, env): return f"Move\n{env.resolve_path(args.get('source'))}\nto\n{env.resolve_path(args.get('destination'))}"
def _sum_write(args, env): return f"Replace the existing file:\n{env.resolve_path(args.get('path'))}"
def _esc_write(args, env): return _truthy(args.get("overwrite")) and env.resolve_path(args.get("path")).exists()


# ─── registry ───
def build_registry(cfg: dict) -> Registry:
    s = {"type": "string"}
    if os.name == "nt": apps.warm_up(cfg["tools"])

    tools = [
        Tool("delete_file", "Delete a file (to the Recycle Bin).", 
             {"path": s}, ["path"], _t_delete_file, "files", 
             policy="confirm", summarize=_sum_delete),
        Tool("move_file", "Move or rename a file or folder.", 
             {"source": s, "destination": s}, ["source", "destination"], 
             _t_move_file, "files", policy="confirm", summarize=_sum_move),
        Tool("open_app", "Open any app by name.", {"name": s}, ["name"], _t_open_app, "apps"),
        Tool("list_apps", "List installed apps, or answer 'do I have <name>?'.", {"query": s}, [], _t_list_apps, "apps", readonly=True),
        Tool("close_app", "Close an app politely (asks before force-closing).", {"name": s}, ["name"], _t_close_app, "apps", summarize=lambda a, e: f"Close {a.get('name', 'that app')}?"),
        Tool("close_window", "Close one window by title (or the front window).", {"title": s}, [], _t_close_window, "apps", summarize=lambda a, e: f"Close the window {a.get('title') or 'in front'}?"),
        Tool("list_windows", "List open app windows.", {}, [], _t_list_windows, "apps", readonly=True),
        Tool("open_url", "Open a web address in the browser.", {"url": s}, ["url"], _t_open_url, "apps"),
        
        Tool("find", "Find a file or folder by name ANYWHERE on the computer (all fixed drives).", {"name": s, "kind": {"type": "string", "enum": ["file", "folder", "any"]}, "limit": {"type": "integer"}}, ["name"], _t_find, "files", readonly=True, untrusted=True),
        Tool("read_file", "Read a text or code file, with line numbers.", {"path": s, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"], _t_read_file, "files", readonly=True, untrusted=True),
        Tool("write_file", "Create a file or fully replace one. The write is verified.", {"path": s, "content": s, "overwrite": {"type": "boolean"}}, ["path", "content"], _t_write_file, "files"),
        Tool("edit_file", "Replace exact text in a file. old_string must match exactly once unless replace_all is true.", {"path": s, "old_string": s, "new_string": s, "replace_all": {"type": "boolean"}}, ["path", "old_string", "new_string"], _t_edit_file, "files"),
        Tool("undo", "Put back every file changed by the last request (write_file, edit_file, apply_patch). steps = how many requests to go back. Shell commands can't be undone.", {"steps": {"type": "integer"}}, [], _t_undo, "files"),
        Tool("glob", "Find files by pattern, e.g. '**/*.py' or '*.pdf'.", {"pattern": s, "path": s}, ["pattern"], _t_glob, "files", readonly=True),
        Tool("grep", "Search file contents with a regular expression.", {"pattern": s, "path": s, "glob": s, "ignore_case": {"type": "boolean"}}, ["pattern"], _t_grep, "files", readonly=True, untrusted=True),
        Tool("open_path", "Open a file, a folder, or a web address in its normal app.", {"target": s}, ["target"], _t_open_path, "files"),

        Tool("run_shell", "Run a PowerShell / bash command and get its full output and exit code. Refuses catastrophic commands; risky ones (deleting, killing, installing...) always ask first. For servers or watchers use the process tool.", {"command": s, "cwd": s, "timeout": {"type": "integer"}}, ["command"], _t_run_shell, "system", untrusted=True, policy="confirm", summarize=_sum_shell, escalate=lambda a, e: bool(shell_risk(str(a.get("command", ""))))),
        Tool("web_search", "Search the web for current information.", {"query": s}, ["query"], _t_web_search, "web", readonly=True),
        Tool("web_fetch", "Fetch the text of a specific URL.", {"url": s}, ["url"], _t_web_fetch, "web", readonly=True),

        Tool("system_info", "Show CPU and memory use and the busiest programs.", {}, [], _t_system_info, "system", readonly=True),
        Tool("get_datetime", "Get the current date and time.", {}, [], _t_get_datetime, "system", readonly=True),
        Tool("media_control", "Press a media key (volume_up, volume_down, mute, play_pause, next, previous).", {"action": {"type": "string", "enum": list(MEDIA_KEYS)}, "steps": {"type": "integer"}}, ["action"], _t_media_control, "system"),

        Tool("find_skill", "Search the skills library by keywords.", {"query": s}, ["query"], _t_find_skill, "skills", readonly=True),
        Tool("use_skill", "Load a skill's full instructions by exact name.", {"name": s}, ["name"], _t_use_skill, "skills", readonly=True),
    ]

    if cfg["schedule"].get("enabled"):
        tools += [
            Tool("set_reminder", "Set a reminder or timer.", {"text": s, "when": s, "kind": s}, ["text", "when"], _t_set_reminder, "time"),
            Tool("list_reminders", "List reminders and timers.", {}, [], _t_list_reminders, "time", readonly=True),
            Tool("cancel_reminder", "Cancel reminders matching the query.", {"query": s}, ["query"], _t_cancel_reminder, "time"),
        ]
    if cfg["memory"].get("enabled"):
        tools += [
            Tool("remember", "Remember something lasting about the user. kind=rule for standing instructions "
                 "(\"always cc Ann on invoices\", \"never post before 9am\") - rules are checked before every related "
                 "action forever. pin=true keeps it in every prompt.", {"text": s, "kind": s, "pin": {"type": "boolean"}},
                 ["text"], _t_remember, "memory"),
            Tool("memory_manage", "Memory layers: action=status|curate|archived, or pin|unpin|rule|restore|archive with id.",
                 {"action": s, "id": s}, ["action"], _t_memory_manage, "memory"),
            Tool("recall", "Recall memories (or all if empty).", {"query": s}, [], _t_recall, "memory", readonly=True),
            Tool("forget", "Forget memories matching the query.", {"query": s}, ["query"], _t_forget, "memory", policy="confirm", summarize=_sum_forget),
        ]
    if cfg["awareness"].get("enabled"):
        tools += [
            Tool("active_window", "Say which app and window the user is using.", {}, [], _t_active_window, "screen", readonly=True, untrusted=True),
            Tool("read_screen", "Read the text currently visible on the user's screen.", {"window_only": {"type": "boolean"}}, [], _t_read_screen, "screen", readonly=True, untrusted=True),
            Tool("describe_screen", "Look at the screen with a vision model and answer a question.", {"question": s}, [], _t_describe_screen, "screen", readonly=True, untrusted=True),
            Tool("save_screenshot", "Save a screenshot to the workspace.", {}, [], _t_save_screenshot, "screen"),
            Tool("look_with_camera", "Take one photo and answer a question about it.", {"question": s}, [], _t_look_with_camera, "screen", readonly=True, untrusted=True, policy="confirm", summarize=lambda a, e: "Take one photo with your camera to look at what's in front of it."),
        ]

    # ─── Claude Aliases ─────────────────────────────────────────────────────────────────────
    tools.append(Tool("bash_tool", "Run a shell command (alias for run_shell).", {"command": s, "description": s}, ["command"], _t_run_shell, "system", untrusted=True, policy="confirm", summarize=_sum_shell, escalate=lambda a, e: bool(shell_risk(str(a.get("command", ""))))))
    tools.append(Tool("view", "Read a file or list a directory (alias for read_file / list_dir).", {"path": s}, ["path"], _t_view, "files", readonly=True, untrusted=True))
    tools.append(Tool("create_file", "Create a new file (alias for write_file).", {"path": s, "file_text": s}, ["path", "file_text"], lambda a, e: _t_write_file({"path": a.get("path"), "content": a.get("file_text", a.get("content", ""))}, e), "files"))
    def _t_str_replace(args, env):
        return _t_edit_file({"path": args.get("path"), "old_string": args.get("old_str"), "new_string": args.get("new_str")}, env)
    tools.append(Tool("str_replace", "Edit a file by replacing text (alias for edit_file).", {"path": s, "old_str": s, "new_str": s}, ["path", "old_str"], _t_str_replace, "files"))

    from forgecli.nova.tools.dev import dev_tools
    from forgecli.nova.tools.power import power_tools
    from forgecli.nova.tools.computer import computer_tools
    from forgecli.nova.tools.browser import browser_tools
    from forgecli.nova.tools.learning import learning_tools
    tools += dev_tools(cfg)
    tools += power_tools(cfg)
    tools += computer_tools(cfg)
    tools += browser_tools(cfg)
    tools += learning_tools(cfg)
    return Registry(tools)