"""Tool definitions, the file-safety sandbox, and the registry."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from forgecli.nova.core.config import ROOT


class ToolError(Exception):
    """A problem the user (and the model) should be told about plainly."""


class NeedsInfo(ToolError):
    """The tool needs one more answer from the user (for example, when a reminder should happen).
    `followup` says how to finish the job once they reply."""

    def __init__(self, message: str, followup: dict):
        super().__init__(message)
        self.followup = followup


class NeedsAccess(ToolError):
    """The path is outside the folders Nova normally uses. The runner asks the user whether to allow it
    (for this session), then runs the tool again."""

    def __init__(self, path: Path):
        self.path = Path(path)
        super().__init__(f"{self.path} is outside the folders I'm allowed to use.")

    def prompt(self) -> str:
        return f"That's outside my usual folders. Allow me to use {self.path} for this session?"


class NeedsSearch(ToolError):
    """Something wasn't found in the usual folders. The runner offers to search the user's drives."""

    def __init__(self, query: str, kind: str = "any", arg: str | None = None, rest: tuple = (), drive: str = ""):
        self.query = str(query).strip()
        self.kind = kind if kind in ("folder", "file", "any") else "any"
        self.arg = arg            # which argument of the original tool to replace with the found path
        self.rest = tuple(rest)   # sub-folders that were asked for inside the thing being searched for
        self.drive = drive
        noun = {"folder": "folder", "file": "file"}.get(self.kind, "file or folder")
        super().__init__(f"I couldn't find a {noun} called '{self.query}' in my usual folders.")

    def prompt(self) -> str:
        noun = {"folder": "folder", "file": "file"}.get(self.kind, "file or folder")
        where = f"drive {self.drive.upper()}:" if self.drive else "all your drives"
        return (f"I can't find the {noun} '{self.query}' in my usual folders. "
                f"Want me to search {where} for it? I only look at names, not contents.")


class NeedsChoice(ToolError):
    """The tool wants yes/no answers in the middle of its work (for example "Save your changes before closing?").

    `steps` is a list of (question, changes): the runner asks the questions in order; the first "yes" runs the tool
    again with that step's changes added to its arguments. If every answer is "no", the tool runs again with
    `decline` added (or, when there is none, the action simply stops). A silence or timeout counts as "no".
    """

    def __init__(self, message: str, steps: list, decline: dict | None = None):
        super().__init__(message)
        self.steps = [(str(q), dict(c)) for q, c in steps]
        self.decline = dict(decline) if decline is not None else None


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    required: list
    handler: Callable            # (args: dict, env: ToolEnv) -> str
    group: str                   # "apps" | "files" | "system"
    readonly: bool = False       # never changes anything
    untrusted: bool = False      # output may contain text written by someone else
    policy: str = "allow"        # default policy: allow | confirm | deny
    summarize: Callable | None = None   # (args, env) -> what to show in a confirmation
    escalate: Callable | None = None    # (args, env) -> True to force a confirmation

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": self.properties, "required": self.required},
            },
        }


class ToolEnv:
    """Where tools may operate. Everything file-related goes through resolve_path()."""

    def __init__(self, cfg: dict):
        self.cfg = cfg["tools"]
        home = Path.home()
        workspace = Path(self.cfg["workspace"])
        if not workspace.is_absolute():
            workspace = ROOT / workspace
        workspace.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace.resolve()

        self.aliases: dict[str, Path] = {}
        roots: list[Path] = []
        for name in ("Desktop", "Documents", "Downloads"):
            for cand in (home / name, home / "OneDrive" / name):
                if cand.exists():
                    resolved = cand.resolve()
                    self.aliases.setdefault(name.lower(), resolved)
                    roots.append(resolved)
        self.aliases["workspace"] = self.workspace
        roots.append(self.workspace)
        for extra in self.cfg.get("allowed_folders") or []:
            p = Path(os.path.expandvars(str(extra))).expanduser()
            if p.exists():
                roots.append(p.resolve())
        self.roots = list(dict.fromkeys(roots))
        self.granted: list[Path] = []      # folders the user approved this session (see grant())
        self.last_matches: list[Path] = []
        self._protected = ROOT.resolve()
        # owner_mode: when on, Nova may edit files inside its own install folder.
        # owner_roots: folders where the session prompt ("allow me to use ...?") is skipped.
        self.owner_mode = bool(self.cfg.get("owner_mode", False))
        self.owner_roots = [
            Path(os.path.expandvars(str(x))).expanduser().resolve()
            for x in (self.cfg.get("owner_roots") or [])
        ]
        self.screen = None  # set by the server when screen features are on
        self.memory = None  # the MemoryStore, set by the server
        self.scheduler = None  # the ReminderStore, set by the server

    def in_roots(self, p: Path) -> bool:
        if any(p == r or r in p.parents for r in self.owner_roots):
            return True
        return any(p == r or r in p.parents for r in (*self.roots, *self.granted))

    def grant(self, p: Path) -> None:
        """Allow this folder (or file) for the rest of the session. System folders can never be granted."""
        p = Path(p).resolve()
        owner = any(p == r or r in p.parents for r in self.owner_roots)
        if not owner and (self.is_system(p) or (p == self._protected or self._protected in p.parents)):
            return
        if p not in self.granted:
            self.granted.append(p)

    @staticmethod
    def is_system(p: Path) -> bool:
        """Windows system locations that are never searched or opened up to Nova."""
        if os.name != "nt":
            return False
        names = {"windows", "program files", "program files (x86)", "programdata", "system volume information",
                 "$recycle.bin", "recovery", "$windows.~bt"}
        parts = [x.lower() for x in p.parts[1:]]
        return bool(parts) and parts[0] in names

    def resolve_path(self, raw) -> Path:
        raw = str(raw or "").strip().strip('"').strip("'")
        if not raw:
            raise ToolError("I need a file or folder path for that.")
        p = Path(os.path.expandvars(raw)).expanduser()
        if not p.is_absolute():
            parts = p.parts
            alias = self.aliases.get(parts[0].lower()) if parts else None
            p = alias.joinpath(*parts[1:]) if alias else self.workspace / p
        p = p.resolve()
        # never let tools touch the companion's own code, settings or secret token,
        # unless the owner has turned on owner_mode (config.json: tools.owner_mode)
        if not self.owner_mode and (p == self._protected or self._protected in p.parents) and not (
            p == self.workspace or self.workspace in p.parents
        ):
            raise ToolError("That's part of my own program files, so I won't touch it.")
        if not self.in_roots(p):
            if self.is_system(p):
                raise ToolError("That's a Windows system folder, so I won't go in there.")
            raise NeedsAccess(p)
        return p


class Registry:
    def __init__(self, tools: list[Tool]):
        self.tools = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return list(self.tools)

    def schemas_for(self, text: str) -> list[dict]:
        """Offer only the tool groups that match the request: fewer tokens for a small model."""
        t = text.lower()
        groups = set()
        if re.search(r"\b(file|files|folder|folders|document|documents|download|downloads|desktop|find|search|drive|drives|disk|disks|"
                     r"locate|create|make|write|save|delete|remove|trash|move|rename|read|note|list|"
                     r"image|images|picture|photo|png|jpg|pdf|docx|excel|xlsx|word)\b", t):
            groups.add("files")
        if re.search(r"\b(open|launch|start|run|app|apps|website|browser|url|youtube|google|site|link|close|quit|exit|"
                     r"kill|terminate|shut|funga|windows|window)\b", t):
            groups.add("apps")
        if re.search(r"\b(time|date|day|volume|mute|unmute|louder|quieter|music|pause|resume|play|skip|next|"
                     r"previous|cpu|ram|memory|slow|process|processes|disk|powershell|command|install)\b", t):
            groups.add("system")
        if re.search(r"\b(screen|window|screenshot|camera|webcam|wearing|looking|see|app am i|read my|terminal|console|error|errors)\b", t):
            groups.add("screen")
        if re.search(r"\b(remember|forget|memory|memories|recall|know about me|learned about me)\b", t):
            groups.add("memory")
        if re.search(r"\b(remind|reminder|reminders|timer|timers|alarm|schedule|every day|every weekday)\b", t):
            groups.add("time")
        if re.search(r"\b(click|tap|type|press|scroll|drag|button|menu|field|form|fill|select|navigate|settings|"
                     r"instagram|facebook|twitter|tiktok|whatsapp|spotify|app|window|screen|desktop|icon|login|log in|sign in)\b", t):
            groups.add("computer")
        if re.search(r"\b(browser|website|web ?page|page|site|url|http|www|\.com|youtube|instagram|facebook|twitter|"
                     r"tiktok|linkedin|gmail|google|amazon|scrape|scraping|crawl|extract|login|log in|sign in|search for|post|"
                     r"comment|like|follow|subscribe|watch)\b", t):
            groups.add("browser")
        if re.search(r"\b(skill|skills|procedure|how do i usually|like last time|again|same way|learn|improve)\b", t):
            groups.add("skills")
        chosen = [x for x in self.tools.values() if x.group not in ("mcp", "agent") and (not groups or x.group in groups)]
        chosen += [x for x in self.tools.values() if x.group == "agent"]
        # MCP tools only when the request names their server (or says "mcp"): a server can bring dozens
        chosen += [x for x in list(self.tools.values()) if x.group == "mcp"
                   and ("mcp" in t or x.name.split("__")[1].replace("_", " ") in t.replace("_", " ").replace("-", " ")
                        or x.name.split("__")[1] in t)]
        return [x.schema() for x in chosen]

    def all_schemas(self) -> list[dict]:
        # MCP servers can bring hundreds of tools (REA: 200+); they are reached via mcp_search / mcp_call instead
        return [x.schema() for x in self.tools.values() if x.group != "mcp"]