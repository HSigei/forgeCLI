"""Which actions need the user's OK.

Approval modes:
  smart (default)  reading, and writing/editing files inside the project folder, just happen.
                   Shell commands run without asking unless they look risky: deleting, force-pushing,
                   installing system-wide, touching the registry or disks, shutting down, piping
                   downloads into a shell, or anything aimed outside the project folder.
  ask              every write, edit and shell command asks (the old behaviour).
  auto             nothing asks. Use with care.
"""
from __future__ import annotations

import pathlib
import re
import fnmatch
import os

MODES = ("smart", "ask", "auto")

# Patterns that always ask in smart mode. Kept deliberately broad: a false alarm costs one keypress.
RISKY = [
    (r"\b(rm|rmdir|del|erase|rd)\b", "deletes files"),
    (r"\bremove-item\b|\bri\b\s", "deletes files"),
    (r"\bclear-content\b|\bset-content\b.*-force", "overwrites files"),
    (r"\bmove-item\b|\bmv\b|\bmove\b|\bren(ame)?\b|\brename-item\b", "moves or renames files"),
    (r"\bgit\s+(push|reset\s+--hard|clean|checkout\s+--|restore|rebase|branch\s+-D|stash\s+drop)", "changes git history or discards work"),
    (r"\bformat\b|\bdiskpart\b|\bmkfs\b|\bdd\s+if=", "touches disks"),
    (r"\b(shutdown|restart-computer|stop-computer|logoff)\b", "shuts the machine down"),
    (r"\breg(\.exe)?\s+(add|delete|import)|\bset-itemproperty\b|\bnew-itemproperty\b|hklm:|hkcu:", "edits the registry"),
    (r"\b(stop-process|taskkill|kill|pkill|killall)\b", "kills processes"),
    (r"\bset-executionpolicy\b|\bnetsh\b|\bsc(\.exe)?\s+(delete|config|stop)", "changes system settings"),
    (r"\b(npm|pnpm|yarn)\s+(i|install|add)\s+(-g|--global)\b", "installs globally"),
    (r"\bchoco\s+install|\bwinget\s+(install|uninstall)|\bscoop\s+install", "installs software"),
    (r"\bpip\s+uninstall|\bnpm\s+uninstall|\bwinget\s+uninstall", "uninstalls software"),
    (r"(curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)[^|]*\|\s*(sh|bash|iex|invoke-expression|python|powershell)", "runs a downloaded script"),
    (r"\biex\b|\binvoke-expression\b", "runs generated code"),
    (r"\bsudo\b|\brunas\b|-verb\s+runas", "asks for admin rights"),
    (r">\s*[a-z]:\\(windows|program files)|\bc:\\windows\b", "writes into system folders"),
    (r"\b(docker|kubectl)\s+(rm|rmi|delete|prune|system\s+prune)", "deletes containers or resources"),
    (r"\bdrop\s+(table|database)\b|\btruncate\s+table\b", "deletes database data"),
]
_RISKY = [(re.compile(p, re.I), why) for p, why in RISKY]


def policy_denial(tool: str, command: str = "") -> str:
    """Return a hard deny from the user's explicit environment policy."""
    blocked_tools = [item.strip().lower() for item in os.environ.get("FORGE_DENY_TOOLS", "").split(",")
                     if item.strip()]
    if any(fnmatch.fnmatchcase(tool.lower(), pattern) for pattern in blocked_tools):
        return f"blocked by FORGE_DENY_TOOLS policy ({tool})"
    for pattern in (item.strip() for item in os.environ.get("FORGE_DENY_COMMANDS", "").split(";")
                    if item.strip()):
        if not command and tool != "run_shell":
            continue
        try:
            if re.search(pattern, command, re.I):
                return f"blocked by FORGE_DENY_COMMANDS policy ({pattern})"
        except re.error as exc:
            if tool == "run_shell":
                return f"invalid FORGE_DENY_COMMANDS pattern {pattern!r}: {exc}; shell commands are blocked"
    return ""


def shell_risk(command: str) -> str:
    """'' if the command looks safe, otherwise a short reason it needs approval."""
    for rx, why in _RISKY:
        if rx.search(command):
            return why
    return ""


def inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def needs_ok(mode: str, tool: str, root: pathlib.Path, path: pathlib.Path | None = None,
             command: str = "") -> str:
    """'' when the action can go ahead, otherwise why it needs the user's OK."""
    if mode == "auto":
        return ""
    if mode == "ask":
        return "asks before every change (approval mode: ask)"
    if tool in ("write_file", "edit_file"):
        if path is not None and not inside(path, root):
            return "outside the project folder"
        return ""
    if tool == "run_shell":
        return shell_risk(command)
    return ""
