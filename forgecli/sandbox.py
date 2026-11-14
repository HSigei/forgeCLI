"""Optional OS-level sandbox for shell commands. Forge's approval prompts decide *whether* a command
runs; a sandbox limits *what it can reach* once it does. With FORGE_SANDBOX=docker every run_shell
command runs in a throwaway Docker container that can see only the project folder (mounted at
/work), not your home folder, keys or other projects.

  FORGE_SANDBOX=docker               turn it on (needs Docker Desktop running)
  FORGE_SANDBOX_IMAGE=node:22-bookworm  image to use (this default has Node, npm, Python 3 and git)
  FORGE_SANDBOX_NET=none             cut network access too (installs and web requests then fail)
  FORGE_SANDBOX_PORTS=5173,3000,8000,8080  ports published to 127.0.0.1 so dev servers stay reachable

Commands use Linux shell syntax inside the container. File tools (read/write/edit) still run in
Forge itself and are limited by the approval rules, not by the container."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

DEFAULT_IMAGE = "node:22-bookworm"
DEFAULT_PORTS = "5173,3000,8000,8080"


def mode() -> str:
    return os.environ.get("FORGE_SANDBOX", "").strip().lower()


def active() -> bool:
    return mode() == "docker"


def available() -> str | None:
    """None when Docker is ready, otherwise why not (in plain words)."""
    if not shutil.which("docker"):
        return "Docker isn't installed (install Docker Desktop, or unset FORGE_SANDBOX)."
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                           text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return "Docker didn't answer (start Docker Desktop, or unset FORGE_SANDBOX)."
    return None if r.returncode == 0 else "Docker isn't running (start Docker Desktop, or unset FORGE_SANDBOX)."


def _ports() -> list[str]:
    out = []
    for part in os.environ.get("FORGE_SANDBOX_PORTS", DEFAULT_PORTS).split(","):
        part = part.strip()
        if part.isdigit() and 0 < int(part) < 65536:
            out += ["-p", f"127.0.0.1:{part}:{part}"]
    return out


def argv(root: pathlib.Path, where: pathlib.Path, command: str) -> list[str] | str:
    """The docker command line for `command` run in `where`, or an error string if `where` is outside
    the project folder (the container can only see the project)."""
    root, where = pathlib.Path(root).resolve(), pathlib.Path(where).resolve()
    try:
        rel = where.relative_to(root)
    except ValueError:
        return (f"Error: the sandbox can only run commands inside the project folder {root}; "
                f"{where} is outside it.")
    workdir = "/work" + ("/" + rel.as_posix() if str(rel) != "." else "")
    net = os.environ.get("FORGE_SANDBOX_NET", "").strip().lower()
    cmd = ["docker", "run", "--rm", "-i", "--init",
           "-v", f"{root}:/work", "-w", workdir,
           "--memory", os.environ.get("FORGE_SANDBOX_MEMORY", "4g"),
           "--pids-limit", "512",
           "--security-opt", "no-new-privileges",
           "-e", "PYTHONUNBUFFERED=1", "-e", "CI=1"]
    if net == "none":
        cmd += ["--network", "none"]
    else:
        cmd += _ports()
    cmd += [os.environ.get("FORGE_SANDBOX_IMAGE", DEFAULT_IMAGE) or DEFAULT_IMAGE, "sh", "-lc", command]
    return cmd


def describe() -> str:
    if not active():
        return "off — commands run directly on this computer (approval rules still apply)"
    net = "no network" if os.environ.get("FORGE_SANDBOX_NET", "").lower() == "none" else "network on"
    return (f"docker · {os.environ.get('FORGE_SANDBOX_IMAGE', DEFAULT_IMAGE)} · only the project folder "
            f"is visible · {net}")
