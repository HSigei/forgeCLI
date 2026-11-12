"""Skills: reusable instructions stored as Markdown files.

A skill is either  <dir>/<name>/SKILL.md  or  <dir>/<name>.md , with optional
front matter:

    ---
    name: review
    description: Review uncommitted changes for bugs before committing
    ---
    Instructions for the model...

Searched (later wins on name clashes):
  0. claude     every skill and plugin command Claude Code has installed on this machine
                (~/.claude/skills, installed plugins, synced plugins) - read in place, never copied
  1. bundled    <forge-cli>/skills/library - engineering skills that ship with Forge
                (agent-skills and wondelai, MIT), so they work with no Claude Code and no sync
  2. built-in   <forge-cli>/skills
  3. user       ~/.forge/skills
  4. project    <cwd>/.forge/skills

There can be a couple of hundred skills, so only Forge's own core ones are listed to the model;
the rest are found on demand with the find_skill tool (keeps every request small).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import re
import shutil
from dataclasses import dataclass

from .config import APP_DIR, HOME_DIR

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,80}$")
CLAUDE_DIR = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude")
MAX_BODY = 12_000   # characters of a skill sent at once (fits small free-tier limits); rest via read_file

# Claude Code tool names that skills mention, and what to use in Forge instead.
TOOL_MAP = ("Tool names in this skill are Claude Code's. In Forge use: Bash/PowerShell → run_shell, "
            "Read → read_file, Write → write_file, Edit/MultiEdit → edit_file, Grep → grep, "
            "Glob → glob, WebFetch → web_fetch, WebSearch → web_search, TodoWrite/TaskCreate → todo, "
            "AskUserQuestion → ask_user (only when Forge's ask rule allows it; otherwise take the recommended option), Skill → use_skill (find_skill to look one up), "
            "Agent/Task → delegate or run_parallel when enabled (otherwise do the steps yourself). "
            "For MCP connectors, call mcp_search and then use only the exact loaded mcp__… tool schema. "
            "For APIs, use http_request or import an OpenAPI spec; do not invent endpoints or parameters.")


@dataclass
class Skill:
    name: str
    description: str
    path: pathlib.Path
    source: str          # claude | built-in | user | project
    origin: str = ""     # e.g. the Claude plugin it came from
    needs: str = ""      # connector the skill relies on that Forge doesn't have

    def body(self) -> str:
        text = _split(self.path.read_text(encoding="utf-8", errors="replace"))[1].strip()
        base = str(self.path.parent)
        root = str(_plugin_root(self.path) or self.path.parent)
        for k, v in (("${CLAUDE_SKILL_DIR}", base), ("${CLAUDE_PLUGIN_ROOT}", root),
                     ("{baseDir}", base), ("$SKILL_DIR", base)):
            text = text.replace(k, v)
        if len(text) > MAX_BODY:
            text = (text[:MAX_BODY] + f"\n\n… [skill continues: read_file {self.path} "
                    f"with offset to read the rest]")
        return text

    def full(self) -> str:
        """What use_skill returns: where the skill lives, how to map tools, then its text."""
        head = [f"Skill '{self.name}'" + (f" (from Claude Code: {self.origin})" if self.origin else ""),
                f"Its folder: {self.path.parent} — files it mentions (references/, scripts/, "
                f"templates) are there; read them with read_file, run scripts with run_shell."]
        if self.source in ("claude", "bundled"):
            head.append(TOOL_MAP)
        if self.needs:
            head.append(f"Note: this skill comes from the {self.needs} plugin, which may rely on online "
                        "service connectors. Search configured MCP servers with mcp_search; if no matching "
                        "tool is available, do the parts that work and report what needs that service.")
        return "\n".join(head) + "\n\n" + self.body()


def _split(text: str) -> tuple[dict, str]:
    """Front matter → dict. Handles one-line values and YAML block values (> or |)."""
    meta: dict[str, str] = {}
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith("---"):
        return meta, text
    end = text.find("\n---", 3)
    if end == -1:
        return meta, text
    key = None
    for line in text[3:end].splitlines():
        if key and (line.startswith((" ", "\t")) or not line.strip()):
            meta[key] = (meta[key] + " " + line.strip()).strip()
            continue
        key = None
        if ":" in line and not line.startswith((" ", "\t", "-")):
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if v in (">", "|", ">-", "|-", ""):
                meta[k], key = "", k
            else:
                meta[k] = v.strip('"').strip("'")
    return meta, text[end + 4:]


def _plugin_root(path: pathlib.Path) -> pathlib.Path | None:
    for parent in path.parents:
        if (parent / ".claude-plugin").is_dir():
            return parent
        if parent in (CLAUDE_DIR, LIBRARY):
            break
    return None


# Forge's own copy of Claude Code's skills, so they keep working without Claude Code.
LIBRARY = HOME_DIR / "library"
MANIFEST = "manifest.json"
_SKIP = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".venv", "venv", "*.pyc")


def synced_info() -> dict:
    try:
        return json.loads((LIBRARY / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def sync() -> dict:
    """Copy every skill Claude Code has (with its scripts and reference files) into ~/.forge/library.
    Built in a temp folder and swapped in, so a failed copy never leaves a half library."""
    sources = _claude_sources()
    if not sources:
        raise RuntimeError(f"no Claude Code skills found in {CLAUDE_DIR}")
    tmp = LIBRARY.with_name("library.new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    plugins: dict[str, pathlib.Path] = {}
    for d, plugin in sources:
        if plugin:
            plugins.setdefault(plugin, _plugin_root(d) or d.parent)
    if (CLAUDE_DIR / "skills").is_dir():
        shutil.copytree(CLAUDE_DIR / "skills", tmp / "skills", ignore=_SKIP)
    for name, root in plugins.items():
        shutil.copytree(root, tmp / "plugins" / name, ignore=_SKIP, dirs_exist_ok=True)
    info = {"synced": dt.datetime.now().isoformat(timespec="seconds"), "from": str(CLAUDE_DIR),
            "plugins": sorted(plugins)}
    (tmp / MANIFEST).write_text(json.dumps(info, indent=1), encoding="utf-8")
    old = LIBRARY.with_name("library.old")
    shutil.rmtree(old, ignore_errors=True)
    if LIBRARY.exists():
        LIBRARY.rename(old)
    tmp.rename(LIBRARY)
    shutil.rmtree(old, ignore_errors=True)
    return info


def _library_sources() -> list[tuple[pathlib.Path, str]]:
    """Same shape as _claude_sources(), but from Forge's own copy."""
    out: list[tuple[pathlib.Path, str]] = []
    skills = LIBRARY / "skills"
    if skills.is_dir():
        out.append((skills, ""))
        if (skills / "synced").is_dir():
            out += [(d, "") for d in (skills / "synced").iterdir() if d.is_dir()]
    plugins = LIBRARY / "plugins"
    if plugins.is_dir():
        for p in sorted(plugins.iterdir()):
            for sub in ("skills", "commands"):
                if (p / sub).is_dir():
                    out.append((p / sub, p.name))
    return out


def _sources() -> list[tuple[pathlib.Path, str]]:
    """Forge's copy when there is one (made automatically the first time Claude Code's skills are seen)."""
    if not (LIBRARY / MANIFEST).is_file() and CLAUDE_DIR.is_dir():
        try:
            sync()
        except Exception:   # can't copy? read them in place this time
            return _claude_sources()
    return _library_sources() if (LIBRARY / MANIFEST).is_file() else _claude_sources()


def _needs(root: pathlib.Path | None, plugin: str) -> str:
    if root and ((root / ".mcp.json").is_file() or (root / "mcp.json").is_file()):
        return plugin or root.name.split("~")[0]
    return ""


def _claude_sources() -> list[tuple[pathlib.Path, str]]:
    """(folder holding skills or commands, plugin name or '') for everything Claude Code has installed."""
    out: list[tuple[pathlib.Path, str]] = []
    if not CLAUDE_DIR.is_dir():
        return out
    skills = CLAUDE_DIR / "skills"
    if skills.is_dir():
        out.append((skills, ""))
        synced = skills / "synced"
        if synced.is_dir():
            out += [(d, "") for d in synced.iterdir() if d.is_dir()]
    plugins: list[pathlib.Path] = []
    try:
        idx = json.loads((CLAUDE_DIR / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        for entries in (idx.get("plugins") or {}).values():
            for e in entries if isinstance(entries, list) else [entries]:
                p = pathlib.Path(e.get("installPath", ""))
                if p.is_dir():
                    plugins.append(p)
    except (OSError, ValueError, AttributeError):
        pass
    synced = CLAUDE_DIR / "plugins" / "synced"
    if synced.is_dir():
        for bucket in synced.iterdir():
            if bucket.is_dir() and not bucket.name.startswith("."):
                plugins += [p for p in bucket.iterdir() if p.is_dir() and not p.name.startswith(".")]
    for p in plugins:
        name = p.name if not re.match(r"^\d+\.\d+", p.name) else p.parent.name
        try:
            name = json.loads((p / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        name = str(name).split("~")[0]
        for sub in ("skills", "commands"):
            if (p / sub).is_dir():
                out.append((p / sub, name))
    return out


def _entries(d: pathlib.Path):
    """Skill files directly in d: <name>/SKILL.md or <name>.md."""
    try:
        items = sorted(d.iterdir())
    except OSError:
        return
    for entry in items:
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            yield entry / "SKILL.md", entry.name
        elif entry.is_file() and entry.suffix.lower() == ".md" and entry.stem.upper() != "README":
            yield entry, entry.stem


def _load(path: pathlib.Path, default: str) -> tuple[str, str] | None:
    try:
        meta, body = _split(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    name = (meta.get("name") or default).strip().lower().replace(" ", "-")
    if not NAME_RE.match(name):
        return None
    desc = meta.get("description") or next(
        (ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
    return name, " ".join(desc.split())


def discover() -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    # Claude Code's skills first, so Forge's own (and yours) win on a name clash.
    for d, plugin in _sources():
        root = _plugin_root(d)
        needs = _needs(root, plugin)
        for path, default in _entries(d):
            got = _load(path, default)
            if not got:
                continue
            name, desc = got
            sk = Skill(name, desc, path, "claude", plugin or "skills", needs)
            if plugin:
                found[f"{plugin}:{name}"] = Skill(f"{plugin}:{name}", desc, path, "claude", plugin, needs)
            # The short name goes to the first skill that has it, preferring ones that need no connector.
            if name not in found or (found[name].needs and not needs):
                found[name] = sk
    for source, d in skill_dirs():
        if not d.is_dir():
            continue
        for path, default in _entries(d):
            got = _load(path, default)
            if got:
                found[got[0]] = Skill(got[0], got[1], path, source)
    return found


def skill_dirs() -> list[tuple[str, pathlib.Path]]:
    installed = pathlib.Path(sys.prefix) / "skills"
    builtin = installed if installed.is_dir() else APP_DIR / "skills"
    return [("bundled", builtin / "library"),
            ("built-in", builtin),
            ("user", HOME_DIR / "skills"),
            ("project", pathlib.Path.cwd() / ".forge" / "skills")]


def listed(skills: dict[str, Skill]) -> list[Skill]:
    """Skills named in the system prompt: Forge's own plus yours. The rest are found with find_skill."""
    return [s for s in skills.values() if s.source not in ("claude", "bundled")]


def find(skills: dict[str, Skill], query: str, limit: int = 8) -> list[Skill]:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    if not words:
        return []
    seen_paths, scored = set(), []
    for s in skills.values():
        if s.path in seen_paths:
            continue
        name, desc = s.name.lower(), s.description.lower()
        score = sum(3 * (w in name) + desc.count(w) for w in words)
        if score:
            scored.append((score - (2 if s.needs else 0), s))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, s in scored:
        if s.path not in seen_paths:
            seen_paths.add(s.path)
            out.append(s)
        if len(out) >= limit:
            break
    return out


# --- automatic skill detection --------------------------------------------------------
# A local BM25 index over skill names + descriptions: picks the skill a request needs in about a
# millisecond, with no model call, so the right instructions ride along with the very first request.

_STOP = set("""a an the and or but if then so to of in on at by for with from into onto about as is are was
were be been being it its this that these those i me my we our you your he she they them their what which
who whom how why when where can could should would will shall may might must do does did done make made
get got have has had please just also some any all more most very really want need like use using used
help me let lets let's one two new now up out over not no yes thing things stuff something way forge forgecli""".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ies", "es", "ed", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_PATH = re.compile(r"(?:\b[a-z]:[\\/]|~[\\/]|(?<!\w)\.{0,2}/)[^\s\"']*|\b[\w.-]+\.(?:png|jpe?g|gif|webp|svg|"
                   r"pdf|docx?|xlsx?|pptx?|csv|txt|md|html?|css|js|ts|py|json|zip)\b", re.I)


def _keep_ext(m: re.Match) -> str:
    ext = re.search(r"\.([a-z0-9]{2,5})$", m.group(0), re.I)
    return f" {ext.group(1)} " if ext else " "


def request_words(text: str) -> str:
    """What the user asked, without links and file paths: their words ("Users", "Downloads", "co")
    say nothing about the task and used to pull in unrelated skills. A file's type (pdf, xlsx)
    does, so it stays."""
    return _PATH.sub(_keep_ext, _URL.sub(" ", text or ""))


def _terms(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in _STOP]


class Matcher:
    """Built once per skill set. match(text) → (skill, confidence) or (None, 0)."""
    K1, B = 1.2, 0.6
    MIN_SCORE = 6.0      # below this nothing is confidently relevant
    MIN_LEAD = 1.25      # the winner must beat the runner-up by this factor

    def __init__(self, skills: dict[str, "Skill"]):
        import math
        uniq, seen = [], set()
        for s in skills.values():
            if s.path not in seen:
                seen.add(s.path)
                uniq.append(s)
        self.docs = []
        df: dict[str, int] = {}
        for s in uniq:
            name_terms = _terms(s.name.replace("-", " ").replace(":", " "))
            terms = name_terms * 3 + _terms(s.description)    # the name counts triple
            tf: dict[str, int] = {}
            for t in terms:
                tf[t] = tf.get(t, 0) + 1
            for t in tf:
                df[t] = df.get(t, 0) + 1
            self.docs.append((s, tf, len(terms)))
        n = max(1, len(self.docs))
        self.avg = sum(d[2] for d in self.docs) / n if self.docs else 1
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        # IDF (and so scores) shrink with fewer skills; scale the bar to match (tuned on ~200 skills)
        self.min_score = self.MIN_SCORE * min(1.0, math.log(1 + n) / math.log(201))

    def scores(self, text: str) -> list[tuple[float, "Skill"]]:
        q = set(_terms(request_words(text)))
        out = []
        for s, tf, length in self.docs:
            sc = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    sc += self.idf[t] * f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * length / self.avg))
            if sc:
                if s.needs:            # needs a connector Forge doesn't have
                    sc *= 0.6
                if s.source != "claude":
                    sc *= 1.15         # Forge's own skills fit Forge's tools best
                out.append((sc, s))
        out.sort(key=lambda x: -x[0])
        return out

    def match(self, text: str) -> tuple["Skill | None", float]:
        if len(request_words(text).strip()) < 12 or text.lstrip().startswith("/"):
            return None, 0.0
        ranked = self.scores(text)
        if not ranked:
            return None, 0.0
        best, s = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0
        if best >= self.min_score and best >= second * self.MIN_LEAD:
            return s, best
        return None, best


def create(name: str, project: bool = False) -> pathlib.Path:
    name = name.strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,40}$", name):
        raise ValueError("use lowercase letters, digits, - or _ (max 41 chars)")
    base = (pathlib.Path.cwd() / ".forge" / "skills") if project else (HOME_DIR / "skills")
    path = base / name / "SKILL.md"
    if path.exists():
        raise ValueError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""---
name: {name}
description: One line saying what this skill does and when to use it
---
# {name}

Write the instructions the agent should follow when this skill is used.
For example: which files to look at, which commands to run, what the
result should look like, and what to avoid.
""", encoding="utf-8")
    return path


def use_skill_schema(skills: dict[str, Skill]) -> dict:
    return {"type": "function", "function": {
        "name": "use_skill",
        "description": "Load the full instructions of a skill (by exact name, from the list or from "
                       "find_skill). Call it before starting a task that matches, then follow them.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}}


FIND_SCHEMA = {"type": "function", "function": {
    "name": "find_skill",
    "description": "Search the library of expert skills (engineering, web development, design, testing, "
                   "debugging, security, data, documents like pdf/docx/xlsx/pptx, research, marketing, "
                   "sales, video, audio…) by keywords. Use it at the start of any non-trivial task.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "a few keywords, e.g. 'react frontend ui'"}},
        "required": ["query"]}}}
