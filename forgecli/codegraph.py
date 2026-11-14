"""Built-in code graph, powered by graphify (github.com/Graphify-Labs/graphify, Apache-2.0).

graphify parses a project with tree-sitter into a graph of files, classes, functions and the calls
and imports between them. No model and no network: the code-only build takes seconds. Forge uses it
for the questions plain search answers badly - what calls this, what breaks if I change it, how does
a request get from A to B - so the model reads the two files that matter instead of all of them.

The graph lives in <project>/graphify-out/ (graphify's own layout, so the graphify skill and CLI can
use it too) and is rebuilt automatically when source files are newer than it.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".scala",
          ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".lua", ".zig", ".ps1",
          ".ex", ".exs", ".m", ".jl", ".sh", ".vue", ".svelte", ".groovy", ".dart"}
SKIP = {".git", "node_modules", "graphify-out", "__pycache__", ".venv", "venv", "env", "dist", "build",
        ".next", "target", ".pytest_cache", ".mypy_cache", ".tox", "vendor", ".idea", ".vscode"}
MAX_FILES = 6000          # beyond this, a first build is slow; tell the model instead of hanging
ACTIONS = ("overview", "query", "explain", "path", "affected")


def command() -> list[str] | None:
    """How to run graphify: the copy installed with Forge first, then one on PATH."""
    if os.environ.get("FORGE_CODE_GRAPH", "").lower() in ("0", "off", "false", "no"):
        return None
    if importlib.util.find_spec("graphify") is not None:
        return [sys.executable, "-m", "graphify"]
    exe = shutil.which("graphify")
    return [exe] if exe else None


def _sources(root: Path):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() in SOURCE:
                yield os.path.join(dirpath, f)


def stale(root: Path) -> tuple[bool, int]:
    """(needs a rebuild, number of source files)."""
    graph = root / "graphify-out" / "graph.json"
    built = graph.stat().st_mtime if graph.exists() else 0
    n, newer = 0, not graph.exists()
    for f in _sources(root):
        n += 1
        if not newer:
            try:
                newer = os.path.getmtime(f) > built
            except OSError:
                pass
        if n > MAX_FILES:
            break
    return newer, n


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", NO_COLOR="1")
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as e:
        return 127, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _ignore(root: Path) -> None:
    """Keep graphify-out/ out of the user's commits."""
    gi = root / ".gitignore"
    if not (root / ".git").exists():
        return
    try:
        text = gi.read_text(encoding="utf-8") if gi.exists() else ""
        if "graphify-out" not in text:
            gi.write_text(text + ("" if not text or text.endswith("\n") else "\n") + "graphify-out/\n",
                          encoding="utf-8")
    except OSError:
        pass


def ensure(root: Path) -> str:
    """Build or refresh the graph. Returns '' when ready, else why not."""
    cmd = command()
    if cmd is None:
        return "graphify is not installed (pip install graphifyy) or FORGE_CODE_GRAPH=off"
    need, n = stale(root)
    if n == 0:
        return "no source files to map in " + str(root)
    if n > MAX_FILES:
        return (f"over {MAX_FILES} source files; build it once with `graphify update {root}` "
                "(later updates are incremental), or use grep / repo_map on a subfolder")
    if not need:
        return ""
    code, out = _run(cmd + ["update", "."], root, timeout=300)
    if code != 0 or not (root / "graphify-out" / "graph.json").exists():
        return "graphify failed: " + out.strip()[-600:]
    _ignore(root)
    return ""


def _trim(text: str, limit: int = 9000) -> str:
    text = "\n".join(l for l in text.splitlines()
                     if not l.startswith(("Tip:", "[graphify watch]", "  AST cache")))
    return text if len(text) <= limit else text[:limit] + "\n... (cut; ask a narrower question)"


def answer(root: Path, action: str, target: str = "", target2: str = "", budget: int = 1500) -> str:
    action = (action or "overview").strip().lower()
    if action not in ACTIONS:
        return f"Error: action must be one of {', '.join(ACTIONS)}."
    if action != "overview" and not target.strip():
        return f"Error: {action} needs a target (a question, or a function/class/file name)."
    if action == "path" and not target2.strip():
        return "Error: path needs target and target2 (the two things to connect)."
    why = ensure(root)
    if why:
        return "Error: code graph unavailable: " + why + ". Use repo_map / grep instead."
    cmd = command()
    graph = ["--graph", str(root / "graphify-out" / "graph.json")]
    if action == "overview":
        report = root / "graphify-out" / "GRAPH_REPORT.md"
        code, hubs = _run(cmd + ["god-nodes", "--top", "15"] + graph, root, 60)
        head = report.read_text(encoding="utf-8", errors="replace")[:5000] if report.exists() else ""
        return _trim(head + "\n\nMost connected (the hubs most code depends on):\n" + hubs)
    budget = max(300, min(int(budget or 1500), 4000))
    if action == "query":
        args = ["query", target, "--budget", str(budget)]
    elif action == "explain":
        args = ["explain", target]
    elif action == "path":
        args = ["path", target, target2]
    else:
        args = ["affected", target]
    code, out = _run(cmd + args + graph, root, 120)
    if code == 4:
        return f"No node matches {target!r}. Try a function, class or file name, or action=query."
    if code not in (0, 3):
        return "Error: graphify " + action + " failed: " + out.strip()[-600:]
    return _trim(out) + ("\n(Several nodes match: repeat with one of the names listed.)" if code == 3 else "")
