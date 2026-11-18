"""Developer tools ported from the Luhen CLI: git, patches, HTTP, tests, code navigation, background
processes, a task list, downloads, archives and the clipboard. Everything file-related goes through
env.resolve_path(), so the same folder rules apply as for the built-in file tools."""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from forgecli.nova.tools.base import Tool, ToolEnv, ToolError

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".mypy_cache",
             ".pytest_cache", "target", ".idea", ".vscode"}
CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".cs", ".cpp", ".c", ".h", ".rb", ".php"}


def _clip(text: str, limit: int = 14_000) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return f"{text[:head]}\n[... {len(text) - limit} characters cut ...]\n{text[-(limit - head):]}"


def _run(argv: list[str], cwd: Path, timeout: int = 120, stdin: str | None = None) -> tuple[int, str]:
    from forgecli.nova.tools.builtin import run_capture
    try:
        proc = run_capture(argv, cwd, timeout, stdin)
    except FileNotFoundError:
        raise ToolError(f"{argv[0]} isn't installed or isn't on PATH.") from None
    except subprocess.TimeoutExpired:
        raise ToolError(f"{argv[0]} took longer than {timeout}s and I stopped it.") from None
    out = (proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")).strip()
    return proc.returncode, out


def _dir(args: dict, env: ToolEnv, key: str = "cwd") -> Path:
    p = env.resolve_path(args.get(key) or ".")
    if not p.is_dir():
        raise ToolError(f"Folder not found: {p}")
    return p


def _walk(base: Path, limit: int = 20_000):
    n = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(files):
            n += 1
            if n > limit:
                return
            yield Path(root) / f


# ─── list_dir ───────────────────────────────────────────────────────────────────────────────
def _t_list_dir(args: dict, env: ToolEnv) -> str:
    base = _dir(args, env, "path")
    try:
        depth = max(1, min(int(args.get("depth", 2)), 5))
    except (TypeError, ValueError):
        depth = 2
    lines: list[str] = [f"{base}"]

    def walk(p: Path, level: int):
        if len(lines) > 400:
            return
        try:
            entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        for e in entries:
            if e.name in SKIP_DIRS:
                if e.is_dir():
                    lines.append("  " * level + f"{e.name}/ (skipped)")
                continue
            if e.is_dir():
                lines.append("  " * level + f"{e.name}/")
                if level < depth:
                    walk(e, level + 1)
            else:
                try:
                    size = e.stat().st_size
                except OSError:
                    size = 0
                lines.append("  " * level + f"{e.name}  ({size:,} B)")
            if len(lines) > 400:
                lines.append("  ... (more entries not shown)")
                return

    walk(base, 1)
    return "\n".join(lines)


# ─── git ────────────────────────────────────────────────────────────────────────────────────
GIT_READ = {"status", "diff", "log", "show", "blame", "branch_list", "remote", "stash_list"}
GIT_WRITE = {"add", "commit", "checkout", "switch", "branch", "stash", "pull", "push", "fetch", "init", "restore",
             "reset", "merge", "tag"}


def _git_argv(args: dict) -> list[str]:
    action = str(args.get("action", "")).strip().lower()
    extra = args.get("args") or []
    if isinstance(extra, str):
        extra = extra.split()
    extra = [str(x) for x in extra]
    if action == "status":
        return ["status", "--short", "--branch", *extra]
    if action == "diff":
        return ["diff", "--stat", "--patch", *extra]
    if action == "log":
        return ["log", "--oneline", "--decorate", "-n", "30", *extra]
    if action == "branch_list":
        return ["branch", "-a", "-vv"]
    if action == "stash_list":
        return ["stash", "list"]
    if action == "remote":
        return ["remote", "-v"]
    if action == "commit":
        message = str(args.get("message") or (extra[0] if extra else "")).strip()
        if not message:
            raise ToolError("A commit needs a message.")
        rest = extra[1:] if not args.get("message") else extra
        return ["commit", "-m", message, *rest]
    if action in GIT_READ | GIT_WRITE:
        return [action, *extra]
    raise ToolError(f"Unknown git action '{action}'. Use one of: {', '.join(sorted(GIT_READ | GIT_WRITE))}.")


def _t_git(args: dict, env: ToolEnv) -> str:
    cwd = _dir(args, env)
    argv = _git_argv(args)
    code, out = _run(["git", "-c", "color.ui=never", "--no-pager", *argv], cwd, timeout=180)
    if code != 0:
        raise ToolError(f"git {argv[0]} failed (exit {code}).\n{_clip(out)}")
    return _clip(out) or f"git {argv[0]}: done (no output)."


def _git_escalate(args: dict, env: ToolEnv) -> bool:
    action = str(args.get("action", "")).lower()
    dangerous = action in {"push", "reset", "restore", "checkout", "merge", "pull"} or (
        action == "stash" and "drop" in " ".join(map(str, args.get("args") or [])))
    return dangerous


def _sum_git(args: dict, env: ToolEnv) -> str:
    return "Run git " + " ".join(_git_argv(args))


# ─── apply_patch ────────────────────────────────────────────────────────────────────────────
# A tolerant unified-diff applier: hunks are located by their content (line numbers are only a hint),
# line endings and trailing whitespace don't matter, and every file is checked before any is written.
def _parse_patch(patch: str) -> list[dict]:
    files: list[dict] = []
    cur = hunk = None
    for line in patch.replace("\r\n", "\n").split("\n"):
        if line.startswith("--- "):
            cur = {"old": line[4:].split("\t")[0].strip(), "new": None, "hunks": []}
            files.append(cur)
            hunk = None
        elif line.startswith("+++ ") and cur is not None and cur["new"] is None:
            cur["new"] = line[4:].split("\t")[0].strip()
        elif line.startswith("@@") and cur is not None:
            m = re.match(r"@@ -(\d+)", line)
            hunk = {"start": int(m.group(1)) if m else 1, "lines": []}
            cur["hunks"].append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk["lines"].append(line)
        elif hunk is not None and line == "":
            hunk["lines"].append(" ")
        elif line.startswith("\\"):
            continue
    for f in files:
        while f["hunks"] and f["hunks"][-1]["lines"] and f["hunks"][-1]["lines"][-1] == " ":
            f["hunks"][-1]["lines"].pop()   # blank line at the end of the patch, not real context
    return [f for f in files if f["hunks"] or f["old"] == "/dev/null"]


def _strip_prefix(name: str) -> str:
    return name[2:] if name[:2] in ("a/", "b/") else name


def _apply_hunks(lines: list[str], hunks: list[dict], name: str) -> list[str]:
    out = list(lines)
    shift = 0
    for n, h in enumerate(hunks, 1):
        old = [l[1:] for l in h["lines"] if l[0] in " -"]
        new = [l[1:] for l in h["lines"] if l[0] in " +"]
        norm = [x.rstrip() for x in old]
        hint = max(0, h["start"] - 1 + shift)
        spots = [i for i in range(len(out) - len(old) + 1) if [x.rstrip() for x in out[i:i + len(old)]] == norm]
        if not spots and old:   # second chance: ignore all whitespace differences
            squash = [re.sub(r"\s+", "", x) for x in old]
            spots = [i for i in range(len(out) - len(old) + 1)
                     if [re.sub(r"\s+", "", x) for x in out[i:i + len(old)]] == squash]
        if not old:
            spots = [min(hint, len(out))]
        if not spots:
            first = next((x for x in old if x.strip()), "")
            raise ToolError(f"Hunk {n} for {name} doesn't match the file (looking for lines starting "
                            f"'{first.strip()[:80]}'). Re-read the file and make the patch against its current content.")
        at = min(spots, key=lambda i: abs(i - hint))
        out[at:at + len(old)] = new
        shift += len(new) - len(old)
    return out


def _t_apply_patch(args: dict, env: ToolEnv) -> str:
    cwd = _dir(args, env)
    patch = str(args.get("patch", ""))
    files = _parse_patch(patch)
    if not files:
        raise ToolError("That isn't a unified diff: I need '--- a/file', '+++ b/file' and '@@' hunk lines.")
    plan: list[tuple[str, Path, str | None]] = []
    for f in files:
        old, new = _strip_prefix(f["old"]), _strip_prefix(f["new"] or f["old"])
        if new == "/dev/null":
            target = env.resolve_path(str(cwd / old))
            if not target.is_file():
                raise ToolError(f"Can't delete {old}: it doesn't exist.")
            plan.append(("delete", target, None))
            continue
        target = env.resolve_path(str(cwd / new))
        if old == "/dev/null":
            if target.exists():
                raise ToolError(f"{new} already exists, so the patch can't create it.")
            body = "\n".join(l[1:] for h in f["hunks"] for l in h["lines"] if l[0] in " +")
            plan.append(("create", target, body + "\n"))
            continue
        source = env.resolve_path(str(cwd / old))
        if not source.is_file():
            raise ToolError(f"{old} doesn't exist. Check the path (it's relative to {cwd}).")
        raw = source.read_bytes().decode("utf-8", "replace")
        eol = "\r\n" if "\r\n" in raw else "\n"
        ends = raw.endswith(("\n", "\r"))
        lines = raw.replace("\r\n", "\n").split("\n")
        if ends:
            lines.pop()
        result = _apply_hunks(lines, f["hunks"], new)
        plan.append(("edit", target, eol.join(result) + (eol if ends else "")))
        if source != target:
            plan.append(("delete", source, None))
    if args.get("check"):
        return f"The patch applies cleanly to {len(plan)} file(s) (check only, nothing changed)."
    from forgecli.nova.tools.builtin import _checkpoints
    cp = _checkpoints(env)
    done = []
    for kind, path, body in plan:
        cp.capture(path)
        if kind == "delete":
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode("utf-8"))
        done.append(f"{kind} {path.relative_to(cwd) if cwd in path.parents else path}")
    return "Patch applied: " + ", ".join(done)


# ─── http_request ───────────────────────────────────────────────────────────────────────────
def _t_http_request(args: dict, env: ToolEnv) -> str:
    url = str(args.get("url", "")).strip()
    if not re.match(r"https?://", url):
        raise ToolError("The URL must start with http:// or https://.")
    method = str(args.get("method", "GET")).upper()
    headers = {str(k): str(v) for k, v in (args.get("headers") or {}).items()} if isinstance(
        args.get("headers"), dict) else {}
    data = None
    if args.get("json") is not None:
        data = json.dumps(args["json"]).encode()
        headers.setdefault("Content-Type", "application/json")
    elif args.get("body") is not None:
        data = str(args["body"]).encode()
    headers.setdefault("User-Agent", "Nova/2")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=int(args.get("timeout") or 30)) as resp:
            status, rh, body = resp.status, dict(resp.headers), resp.read(2_000_000)
    except urllib.error.HTTPError as exc:
        status, rh, body = exc.code, dict(exc.headers or {}), exc.read(200_000)
    except Exception as exc:
        raise ToolError(f"Request failed: {type(exc).__name__}: {exc}") from None
    ms = int((time.monotonic() - t0) * 1000)
    text = body.decode("utf-8", "replace")
    if "json" in rh.get("Content-Type", ""):
        try:
            text = json.dumps(json.loads(text), indent=2)
        except ValueError:
            pass
    keep = ("Content-Type", "Content-Length", "Location", "Retry-After", "Set-Cookie", "Server")
    head = "\n".join(f"{k}: {v}" for k, v in rh.items() if k in keep)
    return _clip(f"HTTP {status} in {ms} ms\n{head}\n\n{text}")


def _http_escalate(args: dict, env: ToolEnv) -> bool:
    return str(args.get("method", "GET")).upper() not in ("GET", "HEAD", "OPTIONS")


# ─── run_tests ──────────────────────────────────────────────────────────────────────────────
def _detect_tests(cwd: Path) -> tuple[str, list[str]]:
    if (cwd / "pyproject.toml").exists() or (cwd / "pytest.ini").exists() or (cwd / "setup.cfg").exists() \
            or any(cwd.glob("test*.py")) or (cwd / "tests").is_dir():
        return "pytest", ["python", "-m", "pytest", "-q", "--tb=short", "--no-header", "-p", "no:cacheprovider"]
    if (cwd / "package.json").exists():
        try:
            scripts = json.loads((cwd / "package.json").read_text(encoding="utf-8")).get("scripts", {})
        except ValueError:
            scripts = {}
        if "test" in scripts:
            return "npm", ["npm", "test", "--silent", "--"]
    if (cwd / "Cargo.toml").exists():
        return "cargo", ["cargo", "test", "--quiet"]
    if (cwd / "go.mod").exists():
        return "go", ["go", "test", "./..."]
    raise ToolError("I couldn't tell how this project runs its tests. Use run_shell with the test command.")


def _t_run_tests(args: dict, env: ToolEnv) -> str:
    cwd = _dir(args, env)
    kind, argv = _detect_tests(cwd)
    if kind == "npm" and os.name == "nt":
        argv[0] = "npm.cmd"
    target = str(args.get("target") or "").strip()
    if target:
        argv += ["-k", target] if kind == "pytest" else ["--run", target] if kind == "go" else [target]
    try:
        timeout = max(10, min(int(args.get("timeout", 600)), 1800))
    except (TypeError, ValueError):
        timeout = 600
    t0 = time.monotonic()
    code, out = _run(argv, cwd, timeout=timeout)
    took = time.monotonic() - t0
    summary = ""
    if kind == "pytest":
        m = re.findall(r"^=*\s*(.*\b(passed|failed|error|no tests ran)\b.*?)\s*=*$", out, re.M)
        summary = m[-1][0] if m else ""
    head = f"{kind}: {'PASSED' if code == 0 else 'FAILED'} (exit {code}, {took:.1f}s) {summary}".strip()
    if code == 0:
        return head + "\n" + _clip(out, 3000)
    failures = re.findall(r"^(FAILED .*|ERROR .*)$", out, re.M)
    raise ToolError(head + ("\n" + "\n".join(failures[:30]) if failures else "") + "\n\n" + _clip(out, 12_000))


# ─── code navigation ────────────────────────────────────────────────────────────────────────
_DEF_RX = {
    "js": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?\s+(\w+)|class\s+(\w+)|"
                     r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>|interface\s+(\w+)|type\s+(\w+)\s*=)"),
    "go": re.compile(r"^\s*(?:func\s+(?:\([^)]*\)\s*)?(\w+)|type\s+(\w+))"),
    "rs": re.compile(r"^\s*(?:pub\s+)?(?:fn\s+(\w+)|struct\s+(\w+)|enum\s+(\w+)|trait\s+(\w+)|impl(?:<[^>]*>)?\s+(\w+))"),
    "other": re.compile(r"^\s*(?:public|private|protected|static|\s)*(?:class|interface|def|function|fun|func)\s+(\w+)"),
}


def _outline(p: Path) -> list[tuple[int, str]]:
    text = p.read_text(encoding="utf-8", errors="replace")
    out: list[tuple[int, str]] = []
    if p.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            return [(exc.lineno or 0, f"SYNTAX ERROR: {exc.msg}")]

        def visit(node, prefix=""):
            for n in ast.iter_child_nodes(node):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    a = ", ".join(x.arg for x in n.args.args)
                    out.append((n.lineno, f"{prefix}def {n.name}({a})"))
                    visit(n, prefix + "  ")
                elif isinstance(n, ast.ClassDef):
                    out.append((n.lineno, f"{prefix}class {n.name}"))
                    visit(n, prefix + "  ")
        visit(tree)
        return out
    rx = _DEF_RX["js"] if p.suffix in (".js", ".jsx", ".ts", ".tsx") else _DEF_RX.get(p.suffix[1:], _DEF_RX["other"])
    for i, line in enumerate(text.splitlines(), 1):
        m = rx.match(line)
        if m:
            name = next(g for g in m.groups() if g)
            out.append((i, line.strip()[:120] if len(line) < 160 else name))
    return out


def _t_code_outline(args: dict, env: ToolEnv) -> str:
    p = env.resolve_path(args.get("path"))
    if p.is_file():
        rows = _outline(p)
        return f"{p} ({len(rows)} symbols)\n" + "\n".join(f"{n:>5}  {s}" for n, s in rows) if rows else f"{p}: no symbols found."
    if not p.is_dir():
        raise ToolError(f"Not found: {p}")
    parts = []
    for f in _walk(p, 3000):
        if f.suffix in CODE_EXT:
            rows = [s for _, s in _outline(f) if not s.startswith("  ")]
            if rows:
                parts.append(f"{f.relative_to(p)}: " + "; ".join(r.split("(")[0] for r in rows[:25]))
        if len(parts) > 300:
            break
    return _clip("\n".join(parts) or "No code files found.")


def _t_find_symbol(args: dict, env: ToolEnv) -> str:
    name = str(args.get("name", "")).strip()
    if not re.fullmatch(r"[\w$.]+", name or "-"):
        raise ToolError("Give a plain symbol name, e.g. build_registry or UserService.save.")
    base = _dir(args, env, "path")
    short = name.split(".")[-1]
    word = re.compile(rf"(?<![\w$]){re.escape(short)}(?![\w$])")
    defs, refs = [], []
    def_rx = re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:pub\s+)?"
                        rf"(?:def|class|function|func|fn|struct|interface|type|enum|const|let|var)\s+{re.escape(short)}\b")
    for f in _walk(base):
        if f.suffix not in CODE_EXT:
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if word.search(line):
                row = f"{f.relative_to(base)}:{i}: {line.strip()[:160]}"
                (defs if def_rx.match(line) else refs).append(row)
        if len(refs) > 400:
            break
    if not defs and not refs:
        return f"No mention of '{short}' under {base}."
    return _clip(f"Definitions ({len(defs)}):\n" + ("\n".join(defs) or "  none found") +
                 f"\n\nReferences ({len(refs)}):\n" + "\n".join(refs[:200]))


# ─── todo ───────────────────────────────────────────────────────────────────────────────────
def _t_todo(args: dict, env: ToolEnv) -> str:
    items = args.get("items")
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            items = [{"text": x.strip(), "status": "pending"} for x in items.splitlines() if x.strip()]
    if items is not None:
        clean = []
        for it in items if isinstance(items, list) else []:
            if isinstance(it, str):
                it = {"text": it}
            if isinstance(it, dict) and str(it.get("text") or it.get("content") or "").strip():
                st = str(it.get("status", "pending")).lower()
                clean.append({"text": str(it.get("text") or it.get("content")).strip(),
                              "status": st if st in ("pending", "in_progress", "done") else "pending"})
        env.todo = clean
    todo = getattr(env, "todo", None) or []
    if not todo:
        return "The task list is empty."
    mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
    done = sum(t["status"] == "done" for t in todo)
    return f"Task list ({done}/{len(todo)} done):\n" + "\n".join(f"{mark[t['status']]} {t['text']}" for t in todo)


# ─── background processes ───────────────────────────────────────────────────────────────────
class _Proc:
    def __init__(self, pid: int, command: str, cwd: Path, popen: subprocess.Popen):
        self.id, self.command, self.cwd, self.popen = pid, command, cwd, popen
        self.output: list[str] = []
        self.started = time.time()
        self.lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for raw in iter(self.popen.stdout.readline, b""):
            with self.lock:
                self.output.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
                if len(self.output) > 5000:
                    del self.output[:1000]

    def tail(self, n: int = 80) -> str:
        with self.lock:
            return "\n".join(self.output[-n:])

    def state(self) -> str:
        code = self.popen.poll()
        return "running" if code is None else f"exited ({code})"


_PROCS: dict[int, _Proc] = {}
_NEXT = [1]


def _t_process(args: dict, env: ToolEnv) -> str:
    action = str(args.get("action", "list")).lower()
    if action == "start":
        from forgecli.nova.tools.builtin import _FORBIDDEN, _fresh_env, _pick_shell
        if not env.cfg.get("enable_shell"):
            raise ToolError("Running commands is switched off in config.json (tools.enable_shell).")
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolError("What command should I start?")
        if any(rx.search(command) for rx in _FORBIDDEN):
            raise ToolError("That command is refused because it could wipe the computer.")
        cwd = _dir(args, env)
        shell = _pick_shell()
        argv = (["cmd", "/c", command] if shell == "cmd" else
                [shell, "-NoProfile", "-NonInteractive", "-Command", command] if shell in ("pwsh", "powershell")
                else [shell, "-c", command])
        popen = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, env=_fresh_env())
        pid = _NEXT[0]
        _NEXT[0] += 1
        _PROCS[pid] = p = _Proc(pid, command, cwd, popen)
        time.sleep(float(args.get("wait", 3) or 0))
        return f"Started process {pid}: {command}\nState: {p.state()}\nFirst output:\n{p.tail(40) or '(none yet)'}"
    if action == "list":
        if not _PROCS:
            return "No background processes."
        return "\n".join(f"{p.id}: [{p.state()}] {p.command}  (in {p.cwd}, {int(time.time() - p.started)}s)"
                         for p in _PROCS.values())
    try:
        p = _PROCS[int(args.get("id"))]
    except (TypeError, ValueError, KeyError):
        raise ToolError("No background process with that id. Use action=list.") from None
    if action == "output":
        return f"{p.id} [{p.state()}] {p.command}\n{_clip(p.tail(int(args.get('lines') or 120)))}"
    if action == "stop":
        if p.popen.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.popen.pid)], capture_output=True,
                               creationflags=NO_WINDOW)
            else:
                p.popen.terminate()
            try:
                p.popen.wait(5)
            except subprocess.TimeoutExpired:
                p.popen.kill()
        _PROCS.pop(p.id, None)
        return f"Stopped process {p.id}. Last output:\n{p.tail(30)}"
    raise ToolError("action must be start, list, output or stop.")


def _process_escalate(args: dict, env: ToolEnv) -> bool:
    return str(args.get("action", "list")).lower() == "start"


# ─── download / archive / clipboard ─────────────────────────────────────────────────────────
def _t_download_file(args: dict, env: ToolEnv) -> str:
    url = str(args.get("url", "")).strip()
    if not re.match(r"https?://", url):
        raise ToolError("The URL must start with http:// or https://.")
    name = Path(url.split("?")[0].rstrip("/")).name or "download"
    dest = env.resolve_path(args.get("path") or name)
    if dest.is_dir():
        dest = dest / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Nova/2"})
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:
            while chunk := resp.read(1 << 16):
                total += len(chunk)
                if total > 2_000_000_000:
                    raise ToolError("The download is over 2 GB, so I stopped it.")
                fh.write(chunk)
    except ToolError:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise ToolError(f"Download failed: {type(exc).__name__}: {exc}") from None
    return f"Downloaded {total:,} bytes to {dest}"


def _t_archive(args: dict, env: ToolEnv) -> str:
    action = str(args.get("action", "")).lower()
    src = env.resolve_path(args.get("path"))
    if action == "list":
        with zipfile.ZipFile(src) as z:
            names = z.namelist()
        return f"{src.name}: {len(names)} entries\n" + "\n".join(names[:300])
    if action in ("extract", "unzip"):
        dest = env.resolve_path(args.get("destination") or str(src.with_suffix("")))
        dest.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src) as z:
                for m in z.namelist():
                    target = (dest / m).resolve()
                    if dest not in target.parents and target != dest:
                        raise ToolError(f"Refusing to extract {m}: it points outside the destination.")
                z.extractall(dest)
        else:
            shutil.unpack_archive(str(src), str(dest), filter="data") if hasattr(shutil, "_unpack_tarfile") else \
                shutil.unpack_archive(str(src), str(dest))
        return f"Extracted {src.name} to {dest}"
    if action in ("create", "zip"):
        dest = env.resolve_path(args.get("destination") or str(src) + ".zip")
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            if src.is_file():
                z.write(src, src.name)
            else:
                for f in _walk(src, 100_000):
                    z.write(f, f.relative_to(src.parent))
        return f"Created {dest} ({dest.stat().st_size:,} bytes)"
    raise ToolError("action must be list, extract or create.")


def _t_clipboard(args: dict, env: ToolEnv) -> str:
    action = str(args.get("action", "get")).lower()
    if os.name != "nt":
        raise ToolError("The clipboard tool works on Windows only.")
    if action == "set":
        text = str(args.get("text", ""))
        _run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"], Path.home(), 15, stdin=text)
        return f"Copied {len(text)} characters to the clipboard."
    code, out = _run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], Path.home(), 15)
    return _clip(out) if out else "The clipboard is empty (or holds no text)."


def dev_tools(cfg: dict) -> list[Tool]:
    s = {"type": "string"}
    i = {"type": "integer"}
    b = {"type": "boolean"}
    return [
        Tool("list_dir", "Show a folder as a tree (skips node_modules, .git, venvs). Use this to get oriented in a project.",
             {"path": s, "depth": i}, [], _t_list_dir, "files", readonly=True),
        Tool("code_outline", "List the classes and functions (with line numbers) in a code file, or every file in a folder.",
             {"path": s}, ["path"], _t_code_outline, "code", readonly=True, untrusted=True),
        Tool("find_symbol", "Find where a function/class/variable is defined and every place it is used.",
             {"name": s, "path": s}, ["name"], _t_find_symbol, "code", readonly=True, untrusted=True),
        Tool("git", "Run git. action: status, diff, log, show, blame, branch_list, remote, stash_list (read-only) or "
                    "add, commit (with message), checkout, switch, branch, stash, pull, push, fetch, init, restore, reset, merge, tag. "
                    "Extra command-line arguments go in args.",
             {"action": s, "args": {"type": "array", "items": s}, "message": s, "cwd": s}, ["action"], _t_git, "code",
             untrusted=True, escalate=_git_escalate, summarize=_sum_git),
        Tool("apply_patch", "Apply a unified diff (git format) to files. Good for many edits across files at once. "
                            "check=true only tests it.",
             {"patch": s, "cwd": s, "check": b}, ["patch"], _t_apply_patch, "code",
             summarize=lambda a, e: "Apply this patch:\n" + str(a.get("patch", ""))[:1500]),
        Tool("run_tests", "Run the project's tests (pytest, npm test, cargo test, go test - detected automatically). "
                          "target narrows to matching tests. Failures are summarised.",
             {"cwd": s, "target": s, "timeout": i}, [], _t_run_tests, "code", untrusted=True, policy="confirm",
             summarize=lambda a, e: f"Run the tests in {a.get('cwd') or 'the workspace'}" +
                                    (f" matching '{a['target']}'" if a.get("target") else "")),
        Tool("process", "Background processes (dev servers, watchers, long builds). action=start (command, cwd, wait seconds), "
                        "list, output (id, lines), stop (id).",
             {"action": {"type": "string", "enum": ["start", "list", "output", "stop"]}, "command": s, "cwd": s,
              "id": i, "lines": i, "wait": i}, ["action"], _t_process, "system", untrusted=True,
             escalate=_process_escalate, summarize=lambda a, e: f"Start in the background:\n{a.get('command', '')}"),
        Tool("todo", "Keep a visible task list for multi-step work. Pass the whole list each time as items "
                     "[{text, status: pending|in_progress|done}]; call with no items to see it.",
             {"items": {"type": "array", "items": {"type": "object", "properties": {"text": s, "status": s}}}},
             [], _t_todo, "code"),
        Tool("http_request", "Call an HTTP API (any method, headers, JSON body) and see status, headers and body.",
             {"url": s, "method": s, "headers": {"type": "object"}, "json": {"type": "object"}, "body": s, "timeout": i},
             ["url"], _t_http_request, "web", untrusted=True, escalate=_http_escalate,
             summarize=lambda a, e: f"Send {str(a.get('method', 'GET')).upper()} {a.get('url', '')}"),
        Tool("download_file", "Download a URL to a file (path may be a folder).",
             {"url": s, "path": s}, ["url"], _t_download_file, "web"),
        Tool("archive", "Zip archives: action=list, extract (path, destination) or create (path, destination).",
             {"action": {"type": "string", "enum": ["list", "extract", "create"]}, "path": s, "destination": s},
             ["action", "path"], _t_archive, "files"),
        Tool("clipboard", "Read (action=get) or set (action=set, text) the clipboard text.",
             {"action": {"type": "string", "enum": ["get", "set"]}, "text": s}, [], _t_clipboard, "system",
             untrusted=True),
    ]
