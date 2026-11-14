"""Saved conversations in ~/.forge/sessions/<id>.json."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import secrets
from dataclasses import dataclass, field

from . import textsafe
from .config import HOME_DIR

SESSION_DIR = HOME_DIR / "sessions"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


@dataclass
class Session:
    id: str = field(default_factory=lambda: dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2))
    title: str = ""
    cwd: str = field(default_factory=lambda: os.getcwd())
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)
    messages: list[dict] = field(default_factory=list)  # everything after the system prompt
    usage_in: int = 0
    usage_out: int = 0

    @property
    def path(self) -> pathlib.Path:
        return SESSION_DIR / f"{self.id}.json"

    def save(self) -> None:
        if not any(m.get("role") == "user" for m in self.messages):
            return  # don't litter the list with empty sessions
        if not self.title:
            first = next(m["content"] for m in self.messages if m.get("role") == "user")
            self.title = " ".join(first.split())[:60]
        self.updated = _now()
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # surrogate halves (from a Windows paste or a tool's output) cannot be written as UTF-8
        tmp.write_text(textsafe.clean(json.dumps(self.__dict__, ensure_ascii=False, indent=1)),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, sid: str) -> "Session":
        data = json.loads((SESSION_DIR / f"{sid}.json").read_text(encoding="utf-8"))
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def list_sessions() -> list[dict]:
    """Newest first. Each item: id, title, cwd, updated, count."""
    out = []
    if not SESSION_DIR.is_dir():
        return out
    for f in SESSION_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"id": d.get("id", f.stem), "title": d.get("title", ""), "cwd": d.get("cwd", ""),
                    "updated": d.get("updated", ""),
                    "count": sum(1 for m in d.get("messages", []) if m.get("role") == "user")})
    return sorted(out, key=lambda s: s["updated"], reverse=True)


def find(ref: str) -> str | None:
    """Resolve '3' (row in /sessions), a full id, or an id prefix."""
    items = list_sessions()
    if ref.isdigit() and 1 <= int(ref) <= len(items):
        return items[int(ref) - 1]["id"]
    hits = [s["id"] for s in items if s["id"].startswith(ref)]
    return hits[0] if len(hits) == 1 else None


def latest_for(cwd: str) -> str | None:
    for s in list_sessions():
        if os.path.normcase(s["cwd"]) == os.path.normcase(cwd):
            return s["id"]
    return None


def delete(sid: str) -> None:
    (SESSION_DIR / f"{sid}.json").unlink(missing_ok=True)


def ago(stamp: str) -> str:
    try:
        secs = (dt.datetime.now() - dt.datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return stamp
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{int(secs // n)}{unit} ago"
    return "just now"
