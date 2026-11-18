"""Searching every drive for a file or folder by name (never by content).

Breadth-first, so things near the top of a drive (like D:\\Henry) are found in a second or two. It stops on a
time budget, skips system and junk folders, and never follows shortcuts/links.
"""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
import string
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

SKIP_NAMES = {"node_modules", "appdata", "$recycle.bin", "__pycache__", "site-packages", ".git", ".venv", "venv",
              "windows", "program files", "program files (x86)", "programdata", "system volume information",
              "recovery", "$windows.~bt", "winsxs", "proc", "sys", "dev", "snap"}
MAX_DEPTH = 12
FOLDER_WORDS = re.compile(r"\b(?:folder|directory|dir)\b", re.I)
FILE_WORDS = re.compile(r"\bfiles?\b", re.I)


@dataclass
class Match:
    path: Path
    is_dir: bool
    score: int


def clean_query(raw: str) -> tuple[str, str]:
    """('the Henry folder' style text) -> (name to look for, kind). Kind is folder/file/any."""
    q = str(raw or "").strip().strip('"').strip("'")
    kind = "any"
    if FOLDER_WORDS.search(q):
        kind = "folder"
    elif FILE_WORDS.search(q):
        kind = "file"
    q = FOLDER_WORDS.sub(" ", q)
    q = FILE_WORDS.sub(" ", q)
    q = re.sub(r"\b(?:called|named|the|my|a|an)\b", " ", q, flags=re.I)
    return re.sub(r"\s+", " ", q).strip(" ."), kind


def parse_drive(raw: str) -> str:
    """'d', 'D:', 'drive d', 'D:\\' -> 'D' (or '' if it isn't a drive letter)."""
    m = re.fullmatch(r"\s*(?:(?:the\s+)?drive\s+)?([a-zA-Z])\s*(?::\\?|\s+drive)?\s*", str(raw or ""))
    return m.group(1).upper() if m else ""


def drive_roots(only: str = "", extra: list | None = None) -> list[Path]:
    """Roots to search. On Windows: every drive letter that exists. Elsewhere (testing): home and mounts."""
    if extra:
        roots = [Path(os.path.expandvars(str(e))).expanduser() for e in extra]
        return [r for r in roots if r.exists()]
    if os.name == "nt":
        letters = [only] if only else list(string.ascii_uppercase)
        out = []
        for c in letters:
            p = Path(f"{c}:\\")
            try:
                if p.exists():
                    out.append(p)
            except OSError:
                continue
        return out
    return [p for p in (Path.home(), Path("/mnt"), Path("/media")) if p.exists()]


def _norm(name: str) -> str:
    return re.sub(r"[\s_\-.]+", " ", name.lower()).strip()


def _score(name: str, query: str, q_norm: str, glob: bool) -> int:
    low = name.lower()
    if glob:
        return 90 if fnmatch.fnmatch(low, query.lower()) else 0
    n = _norm(name)
    if low == query.lower() or n == q_norm:
        return 100
    if n.startswith(q_norm):
        return 80
    if q_norm in n:
        return 60
    if len(q_norm) >= 4:
        ratio = difflib.SequenceMatcher(None, n, q_norm).ratio()
        if ratio >= 0.82:
            return int(40 + ratio * 10)
    return 0


def search(query: str, kind: str = "any", roots: list[Path] | None = None, seconds: float = 30.0,
           limit: int = 12, is_blocked=None) -> tuple[list[Match], dict]:
    """Returns (best matches, info). info: {'timed_out': bool, 'visited': int, 'roots': [..]}"""
    query = str(query).strip()
    glob = any(c in query for c in "*?[")
    q_norm = _norm(query)
    roots = roots if roots is not None else drive_roots()
    deadline = time.monotonic() + max(1.0, float(seconds))
    found: list[Match] = []
    visited = 0
    timed_out = False
    queue: deque = deque((r, 0) for r in roots)
    exact = 0
    while queue:
        if time.monotonic() > deadline:
            timed_out = True
            break
        directory, depth = queue.popleft()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except (OSError, PermissionError):
            continue
        for e in entries:
            visited += 1
            try:
                if e.is_symlink():
                    continue
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            name = e.name
            low = name.lower()
            if is_dir and (low in SKIP_NAMES or (name.startswith(".") and not glob)):
                continue
            path = Path(e.path)
            if is_blocked is not None and is_blocked(path):
                continue
            if (kind == "folder" and is_dir) or (kind == "file" and not is_dir) or kind == "any":
                sc = _score(name, query, q_norm, glob)
                if sc:
                    found.append(Match(path, is_dir, sc))
                    if sc == 100:
                        exact += 1
            if is_dir and depth + 1 <= MAX_DEPTH:
                queue.append((path, depth + 1))
        if exact >= 3 or len(found) >= 200:
            break
    # best score first, then shallower paths
    found.sort(key=lambda m: (-m.score, len(m.path.parts), str(m.path).lower()))
    return found[:limit], {"timed_out": timed_out, "visited": visited, "roots": [str(r) for r in roots]}
