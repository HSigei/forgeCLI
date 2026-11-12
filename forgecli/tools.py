"""File and shell tools the model can call."""
from __future__ import annotations

import difflib
import fnmatch
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.parse

from . import browsercheck
from . import guards, procs, safety
from . import memory as memorymod
from .checkpoints import Checkpoints
from .hooks import Hooks
from .httpapi import HTTP_SCHEMA, HttpAPI, OPENAPI_IMPORT_SCHEMA, OPENAPI_SEARCH_SCHEMA
from .mcp import MCPManager, MCP_SEARCH_SCHEMA, UsageStore
from .openapi import OpenAPIs
from .ui import UI

MAX_OUT = 16_000   # characters of tool output sent back to the model
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".next", "dist", "build"}


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


SCHEMAS = [
    _fn("read_file", "Read a text file. Returns numbered lines (500 by default; use offset/limit for more). "
        "Don't re-read a file you already read unless it changed.",
        {"path": {"type": "string"}, "offset": {"type": "integer", "description": "1-based start line"},
         "limit": {"type": "integer", "description": "max lines (default 500)"}}, ["path"]),
    _fn("write_file", "Create or overwrite a file with the given full content.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("edit_file", "Replace an exact string in a file. old_string must match exactly (including "
        "whitespace) and be unique unless replace_all is true. Read the file first.",
        {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
         "replace_all": {"type": "boolean"}}, ["path", "old_string", "new_string"]),
    _fn("apply_patch", "Apply a multi-file patch. Use *** Begin Patch, *** Update File: path, @@ hunks "
        "with context lines prefixed by space, removals by -, additions by +, and *** End Patch. "
        "Use *** End of File to anchor a hunk at the file end. Context matching tolerates whitespace "
        "drift. Read files first.",
        {"patch": {"type": "string"}}, ["patch"]),
    _fn("list_dir", "List a directory (dirs end with /).", {"path": {"type": "string"}}, []),
    _fn("glob", "Find files by glob pattern, e.g. '**/*.py'.",
        {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
    _fn("grep", "Search file contents with a regular expression. Returns path:line: text.",
        {"pattern": {"type": "string"}, "path": {"type": "string"},
         "glob": {"type": "string", "description": "filename filter, e.g. '*.js'"},
         "ignore_case": {"type": "boolean"}}, ["pattern"]),
    _fn("repo_map", "List definitions and source locations in a project folder. Use for codebase structure.",
        {"path": {"type": "string"}}, []),
    _fn("run_shell", "Run a shell command and return its output and exit code. For anything that keeps "
        "running (web servers, dev servers, watchers, GUI apps) set background=true: it starts, you get the "
        "first output and any local URL, and you can carry on. A command that turns out to be a server is "
        "moved to the background automatically.",
        {"command": {"type": "string"}, "cwd": {"type": "string"},
         "timeout": {"type": "integer", "description": "seconds, default 120, max 600 (foreground only)"},
         "background": {"type": "boolean", "description": "start it and return right away"}}, ["command"]),
    _fn("process", "Manage background processes started by run_shell: list them, read one's latest "
        "output, wait for one to finish (use this for a long install or build instead of polling "
        "output again and again), or stop one.",
        {"action": {"type": "string", "enum": ["list", "output", "wait", "stop"]},
         "id": {"type": "integer", "description": "process id from run_shell (for output/wait/stop)"},
         "timeout": {"type": "integer", "description": "seconds to wait, default 300, max 600 (wait only)"}},
        ["action"]),
    _fn("open_url", "Open a web address (e.g. a local server's URL) in the user's default browser.",
        {"url": {"type": "string"}}, ["url"]),
    _fn("browser_check", "Check a locally running web page in an isolated headless browser: HTTP status, "
        "title, console errors, uncaught page errors (with Playwright) and a screenshot (Playwright, or "
        "Edge/Chrome when Playwright isn't installed). Vision models are shown the screenshot so they can "
        "judge how the page actually looks.",
        {"url": {"type": "string", "description": "local http(s) URL only"}}, ["url"]),
    _fn("web_fetch", "Download a web page or file over http(s) and return it as readable text "
        "(HTML is converted to text). Use for docs, APIs, checking a local server's page.",
        {"url": {"type": "string"},
         "max_chars": {"type": "integer", "description": "default 12000"}}, ["url"]),
    HTTP_SCHEMA,
    _fn("github_request", "Call the GitHub REST API with the connected GitHub account. Use only verified routes.",
        {"method": {"type": "string"}, "path": {"type": "string", "description": "API path beginning with /"},
         "query": {"type": "object"}, "json_body": {},
         "timeout": {"type": "integer"}}, ["method", "path"]),
    _fn("web_search", "Search the web. Returns titles, links and snippets; web_fetch a link to read it.",
        {"query": {"type": "string"}, "count": {"type": "integer", "description": "default 6"}}, ["query"]),
    OPENAPI_IMPORT_SCHEMA,
    OPENAPI_SEARCH_SCHEMA,
    _fn("todo", "Keep a visible checklist for multi-step work. Send the whole list each time, "
        "marking items todo / doing / done.",
        {"items": {"type": "array", "items": {"type": "object", "properties": {
            "task": {"type": "string"}, "status": {"type": "string", "enum": ["todo", "doing", "done"]}},
            "required": ["task", "status"]}}}, ["items"]),
    _fn("ask_user", "Ask the user a question - rarely. Only when a wrong guess is costly or can't be "
        "undone, or you need a fact only they know. Taste, wording, layout and tech choices are not "
        "worth asking: decide and list the assumption. Offer 2-4 short options, best one first.",
        {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
         "default": {"type": "string", "description": "What you will do if the user doesn't answer."},
         "stakes": {"type": "string", "enum": ["irreversible", "costly_rework", "personal_fact", "preference"],
                    "description": "Why asking beats guessing. 'preference' questions are answered with your default."}},
        ["question", "default", "stakes"]),
    _fn("memory", "Read or save an explicit persistent note. Use project scope for this codebase and user "
        "scope for cross-project preferences. Save only useful facts the user wants remembered.",
        {"action": {"type": "string", "enum": ["read", "remember"]},
         "scope": {"type": "string", "enum": ["project", "user"]},
         "note": {"type": "string"}}, ["action", "scope"]),
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


_PS_PIPES = [
    (re.compile(r"\|\s*head\s+-n\s*(\d+)\b"), r"| Select-Object -First \1"),
    (re.compile(r"\|\s*head\s+-(\d+)\b"), r"| Select-Object -First \1"),
    (re.compile(r"\|\s*head\s*(?=$|[;|&])"), "| Select-Object -First 10"),
    (re.compile(r"\|\s*tail\s+-n\s*(\d+)\b"), r"| Select-Object -Last \1"),
    (re.compile(r"\|\s*tail\s+-(\d+)\b"), r"| Select-Object -Last \1"),
    (re.compile(r"\|\s*wc\s+-l\b"), "| Measure-Object -Line"),
    (re.compile(r"\|\s*grep\s+-i\s+-([AC])\s*(\d+)\s+"), r"| Select-String -Context \2 "),
    (re.compile(r"\|\s*grep\s+-([AC])\s*(\d+)\s+(?:-i\s+)?"), r"| Select-String -Context \2 "),
    (re.compile(r"\|\s*grep\s+(?:-i\s+)?-B\s*(\d+)\s+(?:-i\s+)?"), r"| Select-String -Context \1,0 "),
    (re.compile(r"\|\s*grep\s+-i\s+"), "| Select-String "),
    (re.compile(r"\|\s*grep\s+(?!-)"), "| Select-String "),
    (re.compile(r"^\s*ls\s+-R\b"), "Get-ChildItem -Recurse -Name"),
    (re.compile(r"^\s*ls\s+-(?:la|al|l|a)\b"), "Get-ChildItem -Force"),
]


def _closest(text: str, old: str) -> str:
    """The part of the file most like the text the model tried to replace, so the retry can copy
    it exactly instead of guessing from a stale memory of the file (the kvlite edit loop)."""
    lines, want = text.splitlines(), old.strip("\n").splitlines() or [old]
    k = len(want)
    if not lines:
        return " The file is empty."
    best, at = 0.0, 0
    first = want[0].strip()
    for i in range(max(1, len(lines) - k + 1)):
        if first and difflib.SequenceMatcher(None, first, lines[i].strip()).quick_ratio() < 0.5:
            continue
        r = difflib.SequenceMatcher(None, "\n".join(want), "\n".join(lines[i:i + k])).ratio()
        if r > best:
            best, at = r, i
    if best < 0.4:
        return " Nothing in the file looks like it; read the file again before editing."
    lo, hi = max(0, at - 2), min(len(lines), at + k + 2)
    snippet = "\n".join(f"{j + 1:>5}  {lines[j]}" for j in range(lo, hi))
    return (f" The file has changed since you read it. Closest match (lines {at + 1}-{at + k}), copy "
            f"old_string from this exactly, without the line numbers:\n{snippet}")


def bashism(cmd: str) -> str:
    """Bash syntax PowerShell can't run. Each one used to fail after an approval prompt
    (often a wrong "deletes files" warning, from words inside the inline script)."""
    if re.search(r"<<-?\s*['\"]?\w+", cmd):
        return "heredocs (`<< EOF`) don't exist in it."
    if re.search(r"\{[^{}\s]+,[^{}\s]+\}", cmd) and re.search(r"\b(mkdir|touch|cp|mv)\b", cmd):
        return "brace expansion (`{a,b}`) doesn't exist in it; create each folder separately."
    if "\n" in cmd.strip() and re.search(r"\b(python|py|node)(\.exe)?\s+-(c|e)\s+[\"']", cmd):
        return "a multi-line inline script breaks its quoting."
    return ""


def unixy_to_powershell(cmd: str) -> str:
    """Models often write Linux pipes; rewrite the common ones so they work in PowerShell."""
    for pat, rep in _PS_PIPES:
        cmd = pat.sub(rep, cmd)
    # Windows PowerShell 5.1 has no `&&` / `||`; models write them constantly and the command
    # fails with "The token '&&' is not a valid statement separator", wasting a whole step.
    if "&&" in cmd or "||" in cmd:
        parts = _split_outside_quotes(cmd)
        if len(parts) > 1:
            out = parts[0]
            for op, nxt in zip(parts[1::2], parts[2::2]):
                out = f"{out}; if ({'$?' if op == '&&' else '-not $?'}) {{ {nxt} }}"
            cmd = out
    return windows_python(cmd)


def _binary(p: pathlib.Path) -> bool:
    """True for files that aren't text (a NUL byte in the first 4 KB)."""
    try:
        with open(p, "rb") as f:
            return b"\0" in f.read(4096)
    except OSError:
        return False


def _split_outside_quotes(cmd: str) -> list[str]:
    """Split on && / || that sit outside quotes: `cd "C:\\My App" && npm i` used to be left alone
    (any quote disabled the rewrite) and failed with "'&&' is not a valid statement separator"."""
    parts, buf, quote, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            if ch == quote:
                quote = None
            buf.append(ch)
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif cmd[i:i + 2] in ("&&", "||"):
            parts += ["".join(buf).strip(), cmd[i:i + 2]]
            buf = []
            i += 2
            continue
        else:
            buf.append(ch)
        i += 1
    if quote:                       # unbalanced quotes: don't guess
        return [cmd]
    parts.append("".join(buf).strip())
    return parts if all(parts[::2]) else [cmd]


_PY3 = re.compile(r"(^|[;&|{(]\s*)python3(?:\.exe)?(?=\s|$)", re.M)
_PIP = re.compile(r"(^|[;&|{(]\s*)pip3?(?=\s+(?:install|uninstall|show|list|freeze)\b)", re.M)


def windows_python(cmd: str) -> str:
    """On Windows `python3` is usually the Microsoft Store stub or a different install than
    `python`, so packages installed with `pip` were "missing" (ModuleNotFoundError: docx) and the
    model looped installing them. Use one interpreter: `python`, and `python -m pip`."""
    if os.name != "nt":
        return cmd
    cmd = _PY3.sub(r"\1python", cmd)
    return _PIP.sub(r"\1python -m pip", cmd)


def local_only_server(cmd: str) -> str:
    """`python -m http.server` listens on every network interface by default, which shares the
    served folder (often the whole home directory) with anyone on the same Wi-Fi. Bind it to
    this machine unless the command already chose an address."""
    if re.search(r"http\.server", cmd) and not re.search(r"(--bind|-b)\s", cmd):
        cmd = re.sub(r"(-m\s+http\.server)", lambda m: m.group(1) + " --bind 127.0.0.1", cmd, count=1)
    return cmd


def html_to_text(html: str) -> str:
    """Rough but dependency-free HTML → text: drops scripts/styles/nav, keeps links and headings."""
    import html as htmlmod
    html = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer)\b.*?</\1>", " ", html)
    html = re.sub(r"(?is)<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>",
                  lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(1).startswith("http") else m.group(2), html)
    html = re.sub(r"(?i)<h([1-6])\b[^>]*>", lambda m: "\n\n" + "#" * int(m.group(1)) + " ", html)
    html = re.sub(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6]|/section|/article)\b[^>]*>", "\n", html)
    html = re.sub(r"(?i)<li\b[^>]*>", "\n- ", html)
    text = htmlmod.unescape(re.sub(r"(?s)<[^>]+>", " ", html))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text).strip()


_LONG_COMMAND = re.compile(
    r"\b(?:(?:npm|pnpm|yarn|bun)\s+(?:install|i|add|ci|update|upgrade)\b|(?:pnpm|yarn|bun)\s*$|"
    r"(?:pip3?|uv\s+pip|poetry|pipenv|conda|mamba)\s+(?:install|sync|lock)\b|uv\s+sync\b|"
    r"cargo\s+(?:build|install)\b|go\s+(?:build|mod\s+download|install)\b|git\s+clone\b|"
    r"docker\s+(?:build|pull|compose\s+(?:build|pull|up))\b|winget\s+install\b|choco\s+install\b|"
    r"(?:mvn|gradle|gradlew|dotnet)\s+(?:install|build|restore)\b|npx\s+create-|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build\b)", re.I | re.M)


def _still_progressing(p, within: float = 30) -> bool:
    """True when a running command wrote output in the last `within` seconds."""
    try:
        return time.time() - p.log.stat().st_mtime < within
    except (OSError, AttributeError):
        return False


def http_get(url: str, timeout: float = 20) -> tuple[int, str, str]:
    """(status, content-type, text). Never raises for HTTP errors."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status, ctype = r.read(3_000_000), r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw, status, ctype = e.read(200_000), e.code, e.headers.get("Content-Type", "")
    except urllib.error.URLError as e:
        if "CERTIFICATE" not in str(e) or not shutil.which("curl"):
            raise
        # Python lacks an intermediate certificate Windows has; curl uses the Windows store.
        r = subprocess.run(["curl", "-sSL", "--max-time", str(int(timeout)), "-A", UA,
                            "-w", "\n%{http_code} %{content_type}", url], capture_output=True)
        raw, _, tail = r.stdout.rpartition(b"\n")
        code, _, ctype = tail.decode("latin-1").partition(" ")
        status = int(code) if code.isdigit() else 0
        if r.returncode:
            return 0, "", f"Error: {r.stderr.decode('utf-8', 'replace').strip()}"
    charset = (re.search(r"charset=([\w-]+)", ctype) or [None, "utf-8"])[1]
    return status, ctype, raw.decode(charset, "replace")

NEEDS_APPROVAL = {"write_file", "edit_file", "run_shell"}


def _clip(text: str, limit: int = MAX_OUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n\n… [{len(text) - limit} characters truncated] …\n\n" + text[-half:]


def _broad_search_root(path: pathlib.Path) -> bool:
    try:
        return path == pathlib.Path.home() or path.parent == path
    except OSError:
        return False


class Tools:
    def __init__(self, ui: UI, shell: str, auto_approve: bool = False, approval: str = "smart"):
        self.ui = ui
        self.shell = shell
        self.mode = "auto" if auto_approve else (approval if approval in safety.MODES else "smart")
        self.root = pathlib.Path.cwd()
        self.pending_images: list[str] = []   # screenshots to show the model after this step
        self.hooks = Hooks(self.root, ui)
        self.always: set[str] = set()
        self.seen: dict[tuple, tuple] = {}   # (path, offset, limit) -> (mtime, call number)
        self.calls = 0                       # tool calls made, to tell recent reads from old ones
        self.recent_only = False             # set once history is being trimmed (see Agent._compact)
        # What this turn produced that the user would want to look at (Agent opens it when the turn ends).
        self.pages: list[pathlib.Path] = []   # .html files written or edited
        self.urls: list[str] = []             # local URLs of servers started
        self.opened: set[str] = set()         # everything opened this session (never opened twice)
        self._stop_warned: set[int] = set()   # servers the model was told not to stop
        self.opened_now = False               # open_url was called this turn
        self.checkpoints = Checkpoints()
        self.ledger = guards.Ledger()         # what tools really did this turn (checked against the answer)
        self.github_api_base = "https://api.github.com"
        self.github_account: str | None = None
        self.usage = UsageStore(warn=ui.warn)
        self.http = HttpAPI(self)
        self.openapi = OpenAPIs(self.http, self.root)
        self.mcp = MCPManager(self, self.root, self.usage)

    @property
    def auto_approve(self) -> bool:
        return self.mode == "auto"

    @auto_approve.setter
    def auto_approve(self, on: bool) -> None:
        self.mode = "auto" if on else ("smart" if self.mode == "auto" else self.mode)

    def _path(self, p: str | None) -> pathlib.Path:
        q = pathlib.Path(os.path.expandvars(os.path.expanduser(p or ".")))
        if not q.is_absolute():
            q = self.root / q           # each agent's own folder (a worktree for parallel agents)
        return q.resolve()

    def _approve(self, name: str, title: str, detail: str, path: pathlib.Path | None = None,
                 command: str = "", why: str = "") -> str | None:
        """None if approved, otherwise the denial message for the model."""
        reason = safety.needs_ok(self.mode, name, self.root, path, command)
        if self.mode == "smart" and why:
            reason = why
        if not reason or name in self.always:
            return None
        choice, response = self.ui.confirm(title, detail, reason)
        if choice == "a":
            self.always.add(name)
        if choice in ("y", "a"):
            return None
        return "The user denied this action." + (f" They said: {response}" if response else "")

    def _confirm_destructive(self, title: str, detail: str) -> str | None:
        choice, response = self.ui.confirm(
            title, detail, "destructive GitHub action; always requires approval")
        if choice == "y":
            return None
        return "The user denied this destructive action." + (f" They said: {response}" if response else "")

    def github_token(self) -> str:
        """Resolve GitHub credentials without sending them to the UI or model."""
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            return token if not any(ch.isspace() for ch in token) else ""
        from .keyvault import KeyStore
        token = next(iter(KeyStore().load().get("github", [])), "").strip()
        if token:
            return token if not any(ch.isspace() for ch in token) else ""
        gh = shutil.which("gh")
        if not gh:
            return ""
        try:
            result = subprocess.run([gh, "auth", "token", "--hostname", "github.com"],
                                    capture_output=True, text=True, timeout=8, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode:
            return ""
        token = result.stdout.strip()
        return token if token and not any(ch.isspace() for ch in token) else ""

    # dispatch ---------------------------------------------------------------
    def execute(self, name: str, raw_args: str) -> str:
        self.calls += 1
        try:
            args = json.loads(raw_args or "{}")
        except ValueError:
            return "Error: arguments were not valid JSON."
        if not isinstance(args, dict):
            return "Error: arguments must be a JSON object."
        denial = safety.policy_denial(name, str(args.get("command", "")))
        if denial:
            result = "Error: " + denial
        else:
            pre_error = self.hooks.run("pre_tool", name, args)
            if pre_error:
                result = "Error: " + pre_error
            else:
                result = self._dispatch(name, args)
                post_error = self.hooks.run("post_tool", name, args)
                if post_error:
                    self.ui.warn(post_error)
        self.ledger.add(name, args, result)
        return result

    def _dispatch(self, name: str, args: dict) -> str:
        if not isinstance(args, dict):
            return "Error: arguments must be a JSON object."
        if name.startswith("mcp__"):
            return _clip(self.mcp.execute(name, args))
        if name.startswith("openapi__"):
            return _clip(self.openapi.execute(name, args))
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return f"Error: unknown tool '{name}'."
        try:
            return _clip(fn(**args))
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

    # tools ------------------------------------------------------------------
    def t_read_file(self, path, offset=1, limit=500):
        p = self._path(path)
        self.ui.peek("read", str(p))
        sig = (str(p), int(offset or 1), int(limit or 500))
        mtime = p.stat().st_mtime
        prev = self.seen.get(sig)
        # Only point back to a copy the model can still see: once history is trimmed, that is just
        # the last few calls (Agent._compact always keeps them).
        if prev and prev[0] == mtime and (not self.recent_only or self.calls - prev[1] <= 6):
            return ("(Unchanged since you last read it in this conversation; use that copy instead of "
                    "reading it again.)")
        self.seen[sig] = (mtime, self.calls)
        from . import attach
        what = attach.kind(p)
        if what in ("document", "spreadsheet") or p.suffix.lower() == ".pdf":
            # Word/PowerPoint/PDF are zip or binary files: reading them as text gave the model
            # pages of garbage, so it fell back to guessing python3 + python-docx one-liners.
            text = attach.extract(p)
            if text:
                lines = text.splitlines()
            else:
                return (f"{p.name} is a {what} file whose text Forge couldn't extract here. "
                        "Ask the user to paste the parts you need, or try a converter with run_shell.")
        elif what in ("image", "video", "audio", "font", "archive") or _binary(p):
            return (f"{p.name} is a binary {what} file ({attach._size(p)}), so it has no text to read. "
                    "Use it by path: copy it into the project (for example assets/) and reference it "
                    "from the code" + ("; images the user attaches are shown to vision models directly."
                                       if what == "image" else "."))
        else:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(offset or 1))
        chunk = lines[start - 1:start - 1 + int(limit or 500)]
        if not chunk:
            return f"(empty or offset past end; file has {len(lines)} lines)"
        body = "\n".join(f"{i:>6}\t{ln}" for i, ln in enumerate(chunk, start))
        end = start + len(chunk) - 1
        return body + (f"\n(showing lines {start}-{end} of {len(lines)})" if end < len(lines) or start > 1 else "")

    def t_write_file(self, path, content):
        p = self._path(path)
        exists = p.exists()
        old = p.read_text(encoding="utf-8", errors="replace") if exists and p.is_file() else ""
        if exists and old == content:
            return f"Error: {p} already has exactly this content; nothing was changed."
        lazy = self._lazy(p, content, old)
        if lazy:
            return lazy
        diff = "\n".join(difflib.unified_diff(old.splitlines(), content.splitlines(), lineterm="", n=1)) \
            if exists else "\n".join("+" + ln for ln in content.splitlines()[:40])
        denied = self._approve("write_file", f"{'Overwrite' if exists else 'Create'} {p}", diff, p)
        if denied:
            return denied
        p.parent.mkdir(parents=True, exist_ok=True)
        checkpoint = self.checkpoints.capture([p])
        self.checkpoints.add(checkpoint)
        p.write_text(content, encoding="utf-8")
        if not self._landed(p, content):
            return f"Error: wrote {p} but reading it back gave different content; check the file."
        self.ui.wrote(str(p), content, old if exists else None)
        self._produced(p)
        return f"Wrote {len(content)} characters to {p}." + self._syntax_note(p)

    def t_edit_file(self, path, old_string, new_string, replace_all=False):
        p = self._path(path)
        if old_string == new_string:
            return "Error: old_string and new_string are the same, so this edit would change nothing."
        if not old_string:
            return "Error: old_string is empty. Copy the exact text to replace from the file."
        text = p.read_text(encoding="utf-8")
        lazy = self._lazy(p, new_string, old_string)
        if lazy:
            return lazy
        n = text.count(old_string)
        if n == 0:
            return "Error: old_string not found." + _closest(text, old_string)
        if n > 1 and not replace_all:
            return f"Error: old_string occurs {n} times. Add surrounding context or set replace_all."
        new = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        diff = "\n".join(difflib.unified_diff(text.splitlines(), new.splitlines(), lineterm="", n=2))
        denied = self._approve("edit_file", f"Edit {p}", diff, p)
        if denied:
            return denied
        checkpoint = self.checkpoints.capture([p])
        self.checkpoints.add(checkpoint)
        p.write_text(new, encoding="utf-8")
        if not self._landed(p, new):
            return f"Error: edited {p} but reading it back gave different content; check the file."
        self.ui.edited(str(p), text, new)
        self._produced(p)
        return (f"Edited {p} ({n if replace_all else 1} replacement{'s' if replace_all and n > 1 else ''})."
                + self._syntax_note(p))

    def t_apply_patch(self, patch: str) -> str:
        try:
            operations = self._parse_patch(patch)
            changes = self._prepare_patch(operations)
        except (OSError, UnicodeError, ValueError) as e:
            return f"Error: patch not applied: {e}"
        if not changes:
            return "Error: patch contains no file changes."
        for path, old, new, kind in changes:
            label = {"add": "Create", "delete": "Delete"}.get(kind, "Edit")
            diff = "\n".join(difflib.unified_diff(
                old.splitlines(), new.splitlines(), fromfile=str(path), tofile=str(path), lineterm=""))
            denied = self._approve("edit_file", f"{label} {path}", diff, path)
            if denied:
                return denied
        checkpoint = self.checkpoints.capture([path for path, _, _, _ in changes])
        self.checkpoints.add(checkpoint)
        try:
            for path, _, new, kind in changes:
                if kind == "delete":
                    path.unlink()
                elif kind == "add":
                    self._atomic_write(path, new.encode("utf-8"))
                else:
                    self._atomic_write(path, new.encode("utf-8"))
            for path, old, new, kind in changes:
                if kind == "add":
                    self.ui.wrote(str(path), new, None)
                    self._produced(path)
                elif kind == "delete":
                    self.ui.edited(str(path), old, new)
                else:
                    self.ui.edited(str(path), old, new)
                    self._produced(path)
        except OSError as e:
            try:
                self.checkpoints.restore(checkpoint)
                self.checkpoints.entries.remove(checkpoint)
            except OSError as restore_error:
                return (f"Error: patch partially applied ({e}); rollback failed ({restore_error}). "
                        "Use /undo to restore the captured files.")
            return f"Error: patch not applied; changes rolled back: {e}"
        notes = "".join(self._syntax_note(path) for path, _, _, kind in changes if kind != "delete")
        return f"Patched {len(changes)} file(s)." + notes

    _SCRIPT_TAG = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)

    def _syntax_note(self, p) -> str:
        """Syntax-check JavaScript that was just written (a .js/.mjs/.cjs file or the inline scripts of
        an .html page) with `node --check`. A page whose script doesn't parse shows nothing but its
        placeholder, and an HTTP 200 won't reveal that, so the error goes straight back to the model.
        Empty when it parses, when Node isn't installed, or for any other kind of file."""
        p = pathlib.Path(p)
        ext = p.suffix.lower()
        if ext in (".py", ".json"):
            # Parse only - nothing from the project runs, so this is safe after every edit.
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
                if ext == ".py":
                    compile(src, str(p), "exec", dont_inherit=True)
                else:
                    json.loads(src)
            except SyntaxError as e:
                return (f"\nSyntax check FAILED: {p.name} line {e.lineno}: {e.msg}. "
                        "The file is saved but won't run; fix it now.")
            except ValueError as e:
                return f"\nJSON check FAILED: {p.name}: {e}. The file is saved but isn't valid JSON; fix it now."
            except OSError:
                pass
            return ""
        if ext not in (".html", ".htm", ".js", ".mjs", ".cjs") or not shutil.which("node"):
            return ""
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        if ext in (".js", ".mjs", ".cjs"):
            scripts = [(1, text, ext)]
        else:
            scripts = []
            for m in self._SCRIPT_TAG.finditer(text):
                attrs, body = m.group(1), m.group(2)
                if re.search(r"\bsrc\s*=", attrs, re.I) or not body.strip():
                    continue
                kind = re.search(r"\btype\s*=\s*[\"']?([^\"'\s>]+)", attrs, re.I)
                kind = (kind.group(1).lower() if kind else "text/javascript")
                if kind == "module":
                    suffix = ".mjs"
                elif kind in ("text/javascript", "application/javascript"):
                    suffix = ".js"
                else:
                    continue            # importmap, JSON, text/babel (Babel compiles it in the page)
                scripts.append((text.count("\n", 0, m.start(2)) + 1, body, suffix))
        problems = []
        for first_line, body, suffix in scripts[:8]:
            fd, tmp = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(body)
                r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=15)
            except (OSError, subprocess.SubprocessError):
                continue
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            if r.returncode == 0:
                continue
            err = r.stderr or r.stdout
            where = re.search(r":(\d+)\s*$", err.splitlines()[0] if err else "")
            line = first_line + int(where.group(1)) - 1 if where else first_line
            msg = next((ln.strip() for ln in err.splitlines() if "Error" in ln), "syntax error")
            snippet = next((ln.rstrip() for ln in err.splitlines()[1:] if ln.strip()), "")
            note = f"line {line}: {msg}" + (f"  ->  {snippet.strip()}" if snippet else "")
            if "Unexpected token '<'" in msg and ext in (".html", ".htm"):
                note += ("\n  That is JSX. A browser can't run JSX without a build step: write the "
                         "components with htm (html`<${Comp} prop=${x}>...</${Comp}>`) or "
                         "React.createElement, or set the project up with a bundler (Vite).")
            problems.append(note)
        if not problems:
            return ""
        return (f"\n[Forge] {p.name} has JavaScript that does not parse, so it will not run "
                "(a page shows only its placeholder). Fix this before testing or saying it works:\n  "
                + "\n  ".join(problems))

    @staticmethod
    def _parse_patch(patch: str) -> list[
            tuple[str, str, list[tuple[int | None, list[tuple[str, str]], bool]] | str]]:
        lines = patch.splitlines()
        if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
            raise ValueError("expected *** Begin Patch and *** End Patch markers")
        operations = []
        index = 1
        while index < len(lines) - 1:
            line = lines[index]
            index += 1
            if line.startswith("*** Update File: "):
                path = line[len("*** Update File: "):].strip()
                hunks = []
                while index < len(lines) - 1 and not lines[index].startswith("*** "):
                    header = lines[index]
                    if not header.startswith("@@"):
                        raise ValueError(f"expected a hunk header, got {header!r}")
                    match = re.match(r"^@@ -(\d+)(?:,\d+)?(?: \+\d+(?:,\d+)?)? @@", header)
                    start = int(match.group(1)) - 1 if match else None
                    index += 1
                    body = []
                    end_of_file = False
                    while index < len(lines) - 1 and not lines[index].startswith("@@"):
                        item = lines[index]
                        if item == "*** End of File":
                            end_of_file = True
                            index += 1
                            break
                        if item.startswith(("*** Update File:", "*** Add File:", "*** Delete File:", "*** End Patch")):
                            break
                        if not item or item[0] not in " +-":
                            raise ValueError(f"invalid patch line {item!r}")
                        body.append((item[0], item[1:]))
                        index += 1
                    hunks.append((start, body, end_of_file))
                if not hunks:
                    raise ValueError(f"no hunks supplied for {path}")
                operations.append(("update", path, hunks))
            elif line.startswith("*** Add File: "):
                path = line[len("*** Add File: "):].strip()
                content = []
                while index < len(lines) - 1 and not lines[index].startswith("*** "):
                    item = lines[index]
                    if not item.startswith("+"):
                        raise ValueError(f"added file lines must start with +, got {item!r}")
                    content.append(item[1:])
                    index += 1
                operations.append(("add", path, "\n".join(content) + ("\n" if content else "")))
            elif line.startswith("*** Delete File: "):
                path = line[len("*** Delete File: "):].strip()
                operations.append(("delete", path, ""))
            else:
                raise ValueError(f"unknown patch directive {line!r}")
            if not path:
                raise ValueError("patch file path is empty")
        return operations

    def _prepare_patch(self, operations):
        changes = []
        seen = set()
        for kind, raw_path, payload in operations:
            path = self._path(raw_path)
            if path in seen:
                raise ValueError(f"file appears more than once in patch: {path}")
            seen.add(path)
            exists = path.is_file()
            if kind == "add":
                if path.exists():
                    raise ValueError(f"cannot add existing file: {path}")
                new = payload
                old = ""
            elif not exists:
                raise ValueError(f"file does not exist: {path}")
            elif kind == "delete":
                old = path.read_bytes().decode("utf-8")
                new = ""
            else:
                old = path.read_bytes().decode("utf-8")
                new = self._apply_hunks(old, payload)
            if new == old or guards.placeholders(new, old):
                raise ValueError(f"patch for {path} changes nothing or adds placeholder text")
            changes.append((path, old, new, kind))
        return changes

    @staticmethod
    def _apply_hunks(text: str, hunks) -> str:
        lines = text.splitlines()
        final_newline = text.endswith(("\n", "\r"))
        newline = "\r\n" if "\r\n" in text else "\n"
        line_offset = 0
        for start, body, end_of_file in hunks:
            old = [value for prefix, value in body if prefix in " -"]
            new = [value for prefix, value in body if prefix in " +"]
            if not old:
                position = len(lines) if start is None else min(max(0, start + line_offset), len(lines))
                if end_of_file and position != len(lines):
                    raise ValueError("end-of-file hunk does not point to the file end")
                lines[position:position] = new
                line_offset += len(new)
                continue
            expected = None if start is None else max(0, start + line_offset)
            match_at = Tools._locate_hunk(lines, old, expected)
            if end_of_file and match_at + len(old) != len(lines):
                raise ValueError("end-of-file hunk does not match the file end")
            replacement = []
            old_index = 0
            for prefix, value in body:
                if prefix == " ":
                    replacement.append(lines[match_at + old_index])
                    old_index += 1
                elif prefix == "-":
                    old_index += 1
                else:
                    replacement.append(value)
            lines[match_at:match_at + len(old)] = replacement
            line_offset += len(new) - len(old)
        result = newline.join(lines)
        if final_newline and lines:
            result += newline
        elif not final_newline:
            result = result.rstrip("\r\n")
        return result

    @staticmethod
    def _locate_hunk(lines: list[str], old: list[str], expected: int | None) -> int:
        limit = len(lines) - len(old)
        exact = [i for i in range(limit + 1) if lines[i:i + len(old)] == old]
        if expected in exact:
            return expected
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValueError("hunk context is ambiguous")

        def normalize(value):
            return re.sub(r"\s+", " ", value).strip()

        wanted = [normalize(line) for line in old]
        relaxed = [i for i in range(limit + 1)
                   if [normalize(line) for line in lines[i:i + len(old)]] == wanted]
        if expected in relaxed:
            return expected
        if len(relaxed) == 1:
            return relaxed[0]
        if len(relaxed) > 1:
            raise ValueError("whitespace-tolerant hunk context is ambiguous")

        scored = []
        for i in range(limit + 1):
            candidate = [normalize(line) for line in lines[i:i + len(old)]]
            score = difflib.SequenceMatcher(a=wanted, b=candidate, autojunk=False).ratio()
            scored.append((score, i))
        scored.sort(reverse=True)
        if not scored or scored[0][0] < 0.88 or len(scored) > 1 and scored[0][0] - scored[1][0] < 0.08:
            raise ValueError("no matching context for patch hunk")
        return scored[0][1]

    @staticmethod
    def _atomic_write(path: pathlib.Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.forge-patch-",
                                        dir=path.parent)
        tmp = pathlib.Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            if path.exists():
                os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _lazy(self, p: pathlib.Path, content: str, old: str) -> str | None:
        """Refuse placeholder content on every attempt; a retry must provide real content."""
        hits = guards.placeholders(content, old)
        if not hits:
            return None
        return ("Error: not written — this looks like placeholder text instead of real content: "
                + " | ".join(hits[:3]) + ". Write the complete code (the tool replaces text literally, so "
                "a placeholder would delete what it stands for). Replace the placeholder with complete content.")

    @staticmethod
    def _landed(p: pathlib.Path, expected: str) -> bool:
        try:
            return p.read_text(encoding="utf-8").replace("\r\n", "\n") == expected.replace("\r\n", "\n")
        except OSError:
            return False

    def _produced(self, p: pathlib.Path) -> None:
        if p.suffix.lower() in (".html", ".htm") and p not in self.pages:
            self.pages.append(p)

    def new_turn(self) -> None:
        self.pages, self.urls, self.opened_now = [], [], False
        self.todo_items = []
        self.ledger.clear()
        self.openapi.active.clear()
        self.mcp.exposed.clear()

    def show_result(self) -> str | None:
        """After a turn: open what was built, if the model didn't already. Returns what was opened."""
        if self.opened_now:
            return None
        target = None
        if self.urls:
            target = self.urls[-1]
        elif self.pages:
            # The page the user lands on: index.html if one was made, else the first page written.
            page = next((p for p in self.pages if p.name.lower() in ("index.html", "index.htm")), self.pages[0])
            target = page.resolve().as_uri()
        if not target:
            return None
        if target in self.opened:
            return "refresh"   # already open in a tab; a new tab per edit would pile up
        import webbrowser
        if webbrowser.open(target):
            self.opened.add(target)
            return target
        return None

    def t_list_dir(self, path="."):
        p = self._path(path)
        self.ui.peek("list", str(p))
        rows = []
        for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:500]:
            rows.append(e.name + "/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} B)")
        out = "\n".join(rows) or "(empty directory)"
        skill_docs = [d for d in [p / "SKILL.md", *sorted(p.glob("skills/*/SKILL.md"))[:3]] if d.is_file()]
        if skill_docs:
            out += ("\n\n[Forge: this folder is an agent skill. Its instructions are "
                    + ", ".join(str(d) for d in skill_docs)
                    + ": read that first and follow its steps and checks (run any scripts it says to). "
                    "Its examples and demo pages are references, not the thing to copy.]")
        return out

    def t_glob(self, pattern, path="."):
        base = self._path(path)
        self.ui.peek("search", f"{pattern} in {base}")
        if "**" in pattern and _broad_search_root(base):
            return ("Error: refusing a recursive search from a home or drive root. "
                    "Search a specific project or subdirectory instead.")
        hits = []
        for m in base.glob(pattern):
            if SKIP_DIRS.intersection(m.relative_to(base).parts):
                continue
            hits.append(str(m))
            if len(hits) >= 500:
                hits.append("… (stopped at 500 results)")
                break
        return "\n".join(hits) or "No matches."

    def t_grep(self, pattern, path=".", glob=None, ignore_case=False):
        base = self._path(path)
        self.ui.peek("search", f"/{pattern}/ in {base}")
        if base.is_dir() and _broad_search_root(base):
            return ("Error: refusing a recursive search from a home or drive root. "
                    "Search a specific project or subdirectory instead.")
        rx = re.compile(pattern, re.I if ignore_case else 0)
        files = [base] if base.is_file() else []
        if base.is_dir():
            for root, dirs, names in os.walk(base):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                files += [pathlib.Path(root, n) for n in names if not glob or fnmatch.fnmatch(n, glob)]
        out = []
        for f in files:
            try:
                if f.stat().st_size > 2_000_000:
                    continue
                data = f.read_bytes()
                if b"\0" in data[:8000]:
                    continue
                for i, ln in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                    if rx.search(ln):
                        out.append(f"{f}:{i}: {ln.strip()[:300]}")
                        if len(out) >= 200:
                            return "\n".join(out) + "\n… (stopped at 200 matches)"
            except OSError:
                continue
        return "\n".join(out) or "No matches."

    def t_repo_map(self, path="."):
        from . import repomap

        base = self._path(path)
        if _broad_search_root(base):
            return ("Error: refusing to map a home or drive root. "
                    "Choose a specific project folder instead.")
        self.ui.peek("search", f"repository map in {base}")
        return repomap.build(base)

    def t_run_shell(self, command, cwd=None, timeout=120, background=False):
        where = self._path(cwd)
        from . import sandbox
        boxed = sandbox.active()
        if boxed:
            problem = sandbox.available()
            if problem:
                return "Error: FORGE_SANDBOX=docker is on but " + problem
        elif "powershell" in self.shell.lower() or "pwsh" in self.shell.lower():
            bad = bashism(command)
            if bad:
                return ("Error: not run. This shell is Windows PowerShell, and " + bad + " To create or "
                        "change a file use write_file / edit_file, then run it (e.g. `python script.py`).")
            command = unixy_to_powershell(command)
        command = local_only_server(command)
        from . import github
        risk = github.git_risk(command)
        if risk == github.DESTRUCTIVE:
            denied = self._confirm_destructive("Destructive GitHub/Git action", "$ " + command)
        else:
            denied = self._approve("run_shell", f"Run in {where}" + (" (docker sandbox)" if boxed else ""),
                                   "$ " + command, command=command,
                                   why="GitHub write action" if risk == github.WRITE else "")
        if denied:
            return denied
        self.ui.tool("run_shell", command + ("   (background)" if background else ""))
        if not where.is_dir():
            return f"Error: folder not found: {where}"
        try:
            timeout = max(1, min(int(timeout or 120), 600))
        except (TypeError, ValueError):
            timeout = 120
        if _LONG_COMMAND.search(command):
            # Installs, clones and builds routinely pass two minutes on a slow connection; killing
            # them at 120s made the model start over again and again.
            timeout = max(timeout, 600)
        box_argv = None
        if boxed:
            box_argv = sandbox.argv(self.root, where, command)
            if isinstance(box_argv, str):
                return box_argv
        p = procs.start(self.shell, command, str(where), argv=box_argv)
        if background:
            return self._detached(p, wait=6)
        self.ui.start_spinner(f"running {command[:50]}")
        deadline = time.time() + timeout
        try:
            while p.alive():
                time.sleep(0.25)
                ran = time.time() - p.started
                # Servers never exit; once one reports it's up, let it run and hand back control.
                if ran >= 3 and procs.looks_like_server(p):
                    self.ui.stop_spinner()
                    return self._detached(p, wait=0, auto=True)
                if time.time() > deadline:
                    self.ui.stop_spinner()
                    if _still_progressing(p):
                        # Still printing progress: it's slow, not stuck. Keep it running instead
                        # of throwing the work away.
                        return ("It is still working (output is still coming), so it was left "
                                "running. " + self._detached(p, wait=0) +
                                "\nUse process with action=wait to wait for it to finish.")
                    procs.kill(p)
                    out = p.output().strip()
                    procs.forget(p)
                    self.ui.tool_result(out or "(no output)")
                    return (f"Command was stopped after {timeout}s (still running). If it is meant to keep "
                            f"running, use background=true.\nOutput so far:\n{out or '(no output)'}")
        except KeyboardInterrupt:
            self.ui.stop_spinner()
            procs.kill(p)
            procs.forget(p)
            raise
        self.ui.stop_spinner()
        out = p.output(MAX_OUT * 2).strip()
        code = p.popen.returncode
        procs.forget(p)
        self.ui.tool_result(out or "(no output)")
        return f"exit code {code}\n{out or '(no output)'}"

    def _detached(self, p, wait: float, auto: bool = False) -> str:
        end = time.time() + wait
        while wait and time.time() < end and p.alive() and not p.urls():
            time.sleep(0.25)
        out = p.output(4000).strip()
        if not p.alive():
            code = p.popen.returncode
            procs.forget(p)
            self.ui.tool_result(out or "(no output)")
            return f"The command exited right away (exit code {code}).\n{out or '(no output)'}"
        urls = p.urls()
        if urls:
            self.urls.append(urls[0])
        self.ui.tool_result((f"running in background as process {p.id}" +
                             (f" · {urls[0]}" if urls else "")))
        head = ("It kept running like a server, so it was moved to the background. " if auto else "") + \
            f"Running in the background as process {p.id}."
        if urls:
            head += f" Local URL: {urls[0]} (open it with open_url)."
        return (f"{head} Use the process tool to read its output or stop it; it is stopped when Forge "
                f"exits.\nOutput so far:\n{out or '(none yet)'}")

    def t_process(self, action="list", id=None, timeout=300):
        if action == "list":
            items = procs.all_procs()
            if not items:
                return "No background processes."
            return "\n".join(f"{p.id}: {p.state()} · {p.command} · in {p.cwd}" +
                             (f" · {p.urls()[0]}" if p.urls() else "") for p in items)
        p = procs.get(id)
        if not p:
            return f"Error: no background process {id}. Use action=list."
        if action == "output":
            self.ui.tool("process", f"output of {p.id}")
            return f"{p.state()}\n{p.output() or '(no output)'}"
        if action == "wait":
            try:
                timeout = max(1, min(int(timeout or 300), 600))
            except (TypeError, ValueError):
                timeout = 300
            self.ui.tool("process", f"wait for {p.id}: {p.command}")
            self.ui.start_spinner(f"waiting for {p.command[:50]}")
            end = time.time() + timeout
            try:
                while p.alive() and time.time() < end:
                    time.sleep(0.5)
            finally:
                self.ui.stop_spinner()
            if p.alive():
                return (f"Still running after waiting {timeout}s more (process {p.id}). Wait again or "
                        f"stop it.\nLatest output:\n{p.output(4000) or '(none yet)'}")
            code = p.popen.returncode
            out = p.output(MAX_OUT).strip()
            procs.forget(p)
            self.ui.tool_result(out or "(no output)")
            return f"Finished, exit code {code}\n{out or '(no output)'}"
        if action == "stop":
            ports = {urllib.parse.urlparse(u).port for u in p.urls()} - {None}
            shown = [u for u in self.opened if urllib.parse.urlparse(u).port in ports]
            if shown and p.id not in self._stop_warned:
                self._stop_warned.add(p.id)
                return (f"Not stopped: process {p.id} serves {shown[0]}, which you opened for the user. "
                        "Stopping it leaves them a dead page, so leave it running (Forge stops it when "
                        "it exits). Call stop again only if the user asked or it must restart.")
            self.ui.tool("process", f"stop {p.id}: {p.command}")
            procs.kill(p)
            out = p.output(2000)
            procs.forget(p)
            return f"Stopped process {p.id}.\nLast output:\n{out}"
        return "Error: action must be list, output or stop."

    def t_web_fetch(self, url, max_chars=12000):
        if not re.match(r"^https?://", url or "", re.I):
            return "Error: web_fetch needs an http:// or https:// address."
        self.peek("fetch", url)
        status, ctype, text = http_get(url)
        if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            text = html_to_text(text)
        max_chars = max(500, min(int(max_chars or 12000), MAX_OUT))
        more = f"\n\n… [{len(text) - max_chars} more characters]" if len(text) > max_chars else ""
        if "html" in ctype and len(text.strip()) < 200:
            more += ("\n\n[Forge] Almost no text: this page is built by JavaScript in the browser, so "
                     "web_fetch cannot show whether it renders. HTTP 200 only proves the file is "
                     "served. Check the script and imports it loads, or use browser_check.")
        return f"HTTP {status} · {ctype or 'unknown type'}\n\n{text[:max_chars]}{more}"

    def t_http_request(self, **args):
        return self.http.request(**args)

    def t_github_request(self, method, path, query=None, json_body=None, timeout=30):
        path = str(path or "")
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
            return "Error: GitHub API path must be an absolute API path without parent traversal."
        return self.http.request(method, self.github_api_base.rstrip("/") + path,
                                 query=query, json_body=json_body, timeout=timeout)

    def t_openapi_import(self, source, name=""):
        started = time.monotonic()
        result = self.openapi.import_spec(source, name)
        self.usage.record("openapi", str(name or "imported API"),
                          not result.startswith("Error:"), time.monotonic() - started)
        return result

    def t_openapi_search(self, query):
        hits = self.openapi.search(str(query))
        if not hits:
            return "No imported OpenAPI operation matches that query."
        return "Matching operations (their exact schemas are now available):\n" + "\n".join(
            f"- {o['name']}: {o['method']} {o['path']} — {o['summary']}" for o in hits)

    def t_mcp_search(self, query):
        return self.mcp.search(str(query))

    def extra_schemas(self, query: str) -> list[dict]:
        schemas = [OPENAPI_IMPORT_SCHEMA, OPENAPI_SEARCH_SCHEMA, *self.openapi.schemas(query)]
        if self.mcp.configs:
            schemas.extend(self.mcp.schemas(query))
        return schemas

    def doctor_report(self) -> str:
        reports = [self.mcp.report(), self.openapi.report()]
        return "\n".join(report for report in reports if report)

    def t_web_search(self, query, count=6):
        import urllib.parse
        self.peek("search", f"web: {query}")
        status, _, page = http_get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
        if status != 200:
            return f"Error: search failed (HTTP {status}). Try web_fetch on a known site instead."
        out = []
        for m in re.finditer(r'(?s)class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
                             r'class="result__snippet"[^>]*>(.*?)</a>', page):
            link = urllib.parse.unquote(re.sub(r"^.*?uddg=([^&]+).*$", r"\1", m.group(1)))
            if "duckduckgo.com/y.js" in link:
                continue   # ad
            title, snip = (html_to_text(m.group(i)) for i in (2, 3))
            out.append(f"{len(out) + 1}. {title}\n   {link}\n   {snip}")
            if len(out) >= max(1, min(int(count or 6), 10)):
                break
        return "\n".join(out) if out else "No results."

    def t_todo(self, items):
        items = [i for i in (items or []) if isinstance(i, dict)]
        self.ui.todos(items)
        self.todo_items = items
        left = sum(i.get("status") != "done" for i in items)
        return f"Checklist updated ({len(items) - left}/{len(items)} done)."

    def t_memory(self, action, scope, note=""):
        if action == "read":
            content = memorymod.read(scope, str(self.root))
            return content or f"No {scope} memory saved yet."
        if action == "remember":
            path = memorymod.remember(scope, str(self.root), note)
            return f"Saved {scope} memory to {path}."
        return "Error: action must be 'read' or 'remember'."

    def t_ask_user(self, question, options=None):
        ans = self.ui.ask(str(question), [str(o) for o in (options or [])][:6])
        if not ans:
            return "No answer (non-interactive). Make the most sensible choice and say which you picked."
        # People answer questions with files too ("use C:\pics\me.jpg"): treat those exactly like
        # attachments to a message, extracted text and all.
        from . import attach
        paths = attach.find(ans, self.root)
        return f"The user answered: {ans}" + (attach.note(paths, self.root) if paths else "")

    def peek(self, kind, target):
        self.ui.peek(kind, target)

    def t_open_url(self, url):
        if not re.match(r"^(https?://|file:///)", url or "", re.I):
            return "Error: only http(s):// or file:/// addresses can be opened."
        self.ui.tool("open_url", url)
        self.opened.add(url)
        self.opened_now = True
        import webbrowser
        return "Opened in the browser." if webbrowser.open(url) else "Error: could not open a browser."

    def t_browser_check(self, url):
        parsed = urllib.parse.urlparse(url or "")
        host = (parsed.hostname or "").lower()
        if (parsed.scheme not in ("http", "https") or host not in ("localhost", "127.0.0.1", "::1") or
                parsed.username is not None or parsed.password is not None):
            return "Error: browser_check only accepts localhost URLs."
        shot = browsercheck.shot_path(self.root)
        info = browsercheck.playwright_check(url, shot)
        if info is None:                       # no Playwright: Edge/Chrome can still take the picture
            info = browsercheck.cli_screenshot(url, shot)
        if shot.exists() and shot.stat().st_size > 0:
            self.pending_images.append(str(shot))
            info += f"\nscreenshot: {shot} (shown to you next if your model can see images)"
        return info
