"""Separate git worktrees for parallel agents, so two agents editing the same project never overwrite
each other. Each agent gets its own checkout of the current commit on its own branch
(forge/agent-N-<time>) under .forge/worktrees/; its changes are committed there and the main agent
(or you) merges the branches it wants. Turned on with FORGE_WORKTREES=1 or the run_parallel `isolate`
option; needs git and a project that is a git repository with at least one commit."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import time


def _git(args, cwd, timeout=60):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def wanted(isolate: bool | None = None) -> bool:
    if isolate is not None:
        return bool(isolate)
    return os.environ.get("FORGE_WORKTREES", "").strip().lower() in ("1", "on", "true", "yes")


def repo_root(cwd) -> pathlib.Path | None:
    if not shutil.which("git"):
        return None
    try:
        r = _git(["rev-parse", "--show-toplevel"], cwd, 10)
        if r.returncode != 0:
            return None
        if _git(["rev-parse", "--verify", "HEAD"], cwd, 10).returncode != 0:
            return None                         # no commit yet: nothing to check out
        return pathlib.Path(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return None


def create(root: pathlib.Path, n: int, stamp: str | None = None) -> tuple[pathlib.Path, str] | None:
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    branch = f"forge/agent-{n}-{stamp}"
    path = root / ".forge" / "worktrees" / f"agent-{n}-{stamp}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _exclude(root)
    r = _git(["worktree", "add", "-b", branch, str(path), "HEAD"], root)
    return (path, branch) if r.returncode == 0 else None


def _exclude(root: pathlib.Path) -> None:
    """Keep .forge/worktrees out of `git status` without touching the project's .gitignore."""
    try:
        info = pathlib.Path(_git(["rev-parse", "--git-path", "info/exclude"], root, 10).stdout.strip())
        info = info if info.is_absolute() else root / info
        text = info.read_text(encoding="utf-8") if info.exists() else ""
        if ".forge/worktrees/" not in text:
            info.parent.mkdir(parents=True, exist_ok=True)
            info.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".forge/worktrees/\n",
                            encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired):
        pass


def finish(root: pathlib.Path, path: pathlib.Path, branch: str, task: str) -> str:
    """Commit the agent's work on its branch; remove the checkout (the branch stays). One-line summary."""
    try:
        _git(["add", "-A"], path)
        changed = _git(["diff", "--cached", "--stat"], path).stdout.strip()
        if changed:
            _git(["-c", "user.name=Forge agent", "-c", "user.email=forge@localhost", "commit", "-q",
                  "-m", f"forge agent: {task[:70]}"], path)
        _git(["worktree", "remove", "--force", str(path)], root)
        if not changed:
            _git(["branch", "-D", branch], root)
            return "no file changes (branch removed)"
        last = changed.splitlines()[-1].strip()
        return f"committed on branch `{branch}` ({last}); merge with `git merge {branch}`"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"worktree left at {path} on branch `{branch}` ({e})"
