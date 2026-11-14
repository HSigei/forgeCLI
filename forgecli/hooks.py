"""Opt-in local hooks from the user's trusted Forge configuration."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

from .config import HOME_DIR

HOOK_CONFIG = HOME_DIR / "hooks.json"
_ENV_KEYS = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE")


class Hooks:
    def __init__(self, cwd: pathlib.Path, ui):
        self.cwd = cwd
        self.ui = ui

    def run(self, event: str, tool: str, args: dict) -> str:
        if event not in ("pre_tool", "post_tool"):
            return f"Error: unsupported hook event {event!r}."
        if not HOOK_CONFIG.is_file():
            return ""
        try:
            config = json.loads(HOOK_CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"Error: cannot read {HOOK_CONFIG}: {exc}"
        commands = config.get(event, [])
        if not isinstance(commands, list):
            return f"Error: {event} in {HOOK_CONFIG} must be a list of argument arrays."
        env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
        payload = json.dumps({"event": event, "tool": tool, "arguments": args,
                              "cwd": str(self.cwd)}, ensure_ascii=False)
        for command in commands:
            if (not isinstance(command, list) or not command or
                    any(not isinstance(part, str) or not part for part in command)):
                return f"Error: invalid {event} hook in {HOOK_CONFIG}; expected a non-empty argument array."
            try:
                result = subprocess.run(command, input=payload, text=True, capture_output=True,
                                        timeout=5, cwd=self.cwd, env=env, shell=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"Error: {event} hook {command[0]!r} failed: {exc}"
            output = (result.stdout + result.stderr).strip()[:2_000]
            if result.returncode:
                message = f"{event} hook {command[0]!r} exited {result.returncode}"
                if output:
                    message += f": {output}"
                return message
        return ""
