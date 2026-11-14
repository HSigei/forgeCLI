"""Small, explicit, project-scoped persistent notes."""
from __future__ import annotations

import hashlib
import os
import pathlib

from .config import HOME_DIR

MAX_CHARS = 50_000
MAX_NOTE_CHARS = 4_000


def _path(scope: str, cwd: str) -> pathlib.Path:
    if scope == "user":
        return HOME_DIR / "memory" / "user.md"
    if scope != "project":
        raise ValueError("scope must be 'user' or 'project'")
    root = os.path.normcase(str(pathlib.Path(cwd).resolve()))
    key = hashlib.sha256(root.encode("utf-8")).hexdigest()[:20]
    return HOME_DIR / "memory" / "projects" / key / "memory.md"


def read(scope: str, cwd: str) -> str:
    path = _path(scope, cwd)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")[-MAX_CHARS:]


def remember(scope: str, cwd: str, note: str) -> str:
    note = note.strip()
    if not note:
        raise ValueError("note must not be empty")
    if len(note) > MAX_NOTE_CHARS:
        raise ValueError(f"note must be at most {MAX_NOTE_CHARS} characters")
    path = _path(scope, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read(scope, cwd)
    combined = (existing.rstrip() + "\n- " + note + "\n") if existing else ("- " + note + "\n")
    path.write_text(combined[-MAX_CHARS:], encoding="utf-8")
    return str(path)
