"""Self-improving skills: Nova writes down procedures that worked, fixes them when they don't, and a curator
keeps the library tidy without ever losing anything.

    skills/learned/<name>.md     skills Nova wrote itself (the only ones the curator may move)
    skills/.archive/<name>.md    archived skills (dot folders are not loaded) - one command brings them back
    data/skills-ledger.json      uses, last use, pins, state, lessons per skill
    data/skill-snapshots/*.tar.gz  the whole skills folder + ledger, taken before every curator pass and rollback

Lifecycle (agent-authored, unpinned skills only):
    active --30 days unused--> stale --90 days unused--> archived <--restore--> active
Pinned skills stay active forever; lessons can still be added to them. Bundled skills are never moved.
Curator runs when 7 days have passed since the last pass AND Nova has been idle 2 hours:
    phase 1  deterministic state changes (no model)
    phase 2  model review of up to 8 skills that have collected lessons: folds the lessons into the steps
Nothing the curator does is irreversible: `skill_curate action=rollback` restores the last snapshot, and the
rollback itself takes a snapshot first.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tarfile
import threading
import time
from pathlib import Path

from forgecli.nova.core.config import DATA_DIR
from forgecli.nova.tools.base import Tool, ToolEnv, ToolError

log = logging.getLogger("brain.learning")

STALE_DAYS, ARCHIVE_DAYS = 30, 90
CURATE_EVERY, IDLE_NEEDED = 7 * 86400, 2 * 3600
REVIEW_MAX = 8
LEDGER = DATA_DIR / "skills-ledger.json"
SNAPSHOTS = DATA_DIR / "skill-snapshots"
_LOCK = threading.RLock()


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:60]


# ─── ledger ─────────────────────────────────────────────────────────────────────────────────
def load_ledger() -> dict:
    try:
        data = json.loads(LEDGER.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ledger(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LEDGER)


def _entry(data: dict, name: str) -> dict:
    skills = data.setdefault("skills", {})
    e = skills.setdefault(name, {})
    e.setdefault("uses", 0)
    e.setdefault("last_used", 0)
    e.setdefault("created", time.time())
    e.setdefault("pinned", False)
    e.setdefault("state", "active")
    e.setdefault("lessons", [])
    return e


def record_use(name: str) -> None:
    with _LOCK:
        data = load_ledger()
        e = _entry(data, name)
        e["uses"] += 1
        e["last_used"] = time.time()
        if e["state"] == "stale":
            e["state"] = "active"
        save_ledger(data)


def learned_dir(store) -> Path:
    return store.root / "learned"


def archive_dir(store) -> Path:
    return store.root / ".archive"


def is_agent_authored(store, skill: dict | None) -> bool:
    if not skill:
        return False
    try:
        skill["path"].relative_to(learned_dir(store))
        return True
    except ValueError:
        return False


# ─── snapshots ──────────────────────────────────────────────────────────────────────────────
def snapshot(store, reason: str) -> Path:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    dest = SNAPSHOTS / f"skills-{time.strftime('%Y%m%d-%H%M%S')}-{reason}.tar.gz"
    n = 1
    while dest.exists():
        dest = SNAPSHOTS / f"skills-{time.strftime('%Y%m%d-%H%M%S')}-{reason}-{n}.tar.gz"
        n += 1
    with tarfile.open(dest, "w:gz") as tar:
        if store.root.exists():
            tar.add(store.root, arcname="skills")
        if LEDGER.exists():
            tar.add(LEDGER, arcname="skills-ledger.json")
    for old in sorted(SNAPSHOTS.glob("skills-*.tar.gz"), key=lambda p: p.stat().st_mtime)[:-20]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def rollback(store, which: str = "") -> str:
    snaps = sorted(SNAPSHOTS.glob("skills-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    if which:
        snaps = [p for p in snaps if which in p.name]
    else:
        snaps = [p for p in snaps if "-rollback" not in p.name]
    if not snaps:
        raise ToolError("There is no skills snapshot to roll back to.")
    target = snaps[-1]
    with _LOCK:
        before = snapshot(store, "rollback")  # so the rollback itself can be undone
        tmp = store.root.parent / ".skills-restore"
        shutil.rmtree(tmp, ignore_errors=True)
        with tarfile.open(target, "r:gz") as tar:
            members = [m for m in tar.getmembers()
                       if not (m.name.startswith("/") or ".." in Path(m.name).parts) and (m.isfile() or m.isdir())]
            tar.extractall(tmp, members=members)
        if (tmp / "skills").exists():
            shutil.rmtree(store.root, ignore_errors=True)
            shutil.move(str(tmp / "skills"), str(store.root))
        if (tmp / "skills-ledger.json").exists():
            shutil.copy2(tmp / "skills-ledger.json", LEDGER)
        shutil.rmtree(tmp, ignore_errors=True)
        store.reload()
    return f"Rolled the skills back to {target.name}. The state before the rollback is saved as {before.name}."


# ─── curator ────────────────────────────────────────────────────────────────────────────────
def curate_phase1(store, now: float | None = None) -> dict:
    """No model: agent-authored, unpinned skills fade with disuse. Never deletes."""
    now = now or time.time()
    moved = {"stale": [], "archived": [], "active": []}
    with _LOCK:
        data = load_ledger()
        for skill in list(store.list().values()):
            if not is_agent_authored(store, skill):
                continue
            e = _entry(data, skill["name"])
            if e["pinned"]:
                if e["state"] != "active":
                    e["state"] = "active"
                    moved["active"].append(skill["name"])
                continue
            idle = (now - max(e["last_used"], e["created"])) / 86400
            if idle >= ARCHIVE_DAYS:
                dest = archive_dir(store) / skill["path"].name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    dest = dest.with_name(f"{dest.stem}-{int(now)}.md")
                shutil.move(str(skill["path"]), str(dest))
                e["state"], e["archived_path"] = "archived", str(dest)
                moved["archived"].append(skill["name"])
            elif idle >= STALE_DAYS and e["state"] != "stale":
                e["state"] = "stale"
                moved["stale"].append(skill["name"])
            elif idle < STALE_DAYS and e["state"] != "active":
                e["state"] = "active"
                moved["active"].append(skill["name"])
        save_ledger(data)
        store.reload()
    return moved


REVIEW_PROMPT = """You maintain a skill: a Markdown procedure an AI assistant follows on a Windows PC.
Below is the skill, then lessons recorded while using it (things that went wrong or were missing).
Rewrite the skill so the lessons are part of the steps. Keep the same title line ("# ...") and the "> ..."
summary line, keep it short and concrete (commands, paths, checks), and drop nothing that still applies.
Reply with ONLY the new Markdown.

SKILL:
{skill}

LESSONS:
{lessons}
"""


async def ask_llm(router, prompt: str, max_tokens: int = 1500) -> str:
    ok, model, err = await router.ensure(True)
    if not ok:
        raise RuntimeError(err or "no brain available")
    return await router.chat_once(model, [{"role": "user", "content": prompt}], num_predict=max_tokens, temperature=0.2)


async def curate_phase2(store, router) -> list[str]:
    """Model review: fold collected lessons into up to 8 agent-authored skills."""
    data = load_ledger()
    todo = []
    for name, e in data.get("skills", {}).items():
        skill = store.get(name)
        if skill and is_agent_authored(store, skill) and e.get("lessons") and e.get("state") != "archived":
            todo.append((name, skill, e))
    done = []
    for name, skill, e in todo[:REVIEW_MAX]:
        try:
            text = await ask_llm(router, REVIEW_PROMPT.format(skill=skill["full"][:8000],
                                                               lessons="\n".join("- " + l["text"] for l in e["lessons"])))
        except Exception as exc:
            log.warning("curator review of %s failed: %s", name, exc)
            continue
        text = re.sub(r"^```\w*\n|\n```\s*$", "", text.strip())
        if not text.startswith("#") or len(text) < 80:
            continue
        with _LOCK:
            skill["path"].write_text(text.rstrip() + "\n", encoding="utf-8")
            d = load_ledger()
            ent = _entry(d, name)
            ent["folded"] = ent.get("folded", []) + ent["lessons"]
            ent["lessons"] = []
            ent["revised"] = time.time()
            save_ledger(d)
        done.append(name)
    store.reload()
    return done


async def run_curator(store, router=None, force: bool = False, idle_seconds: float = 0) -> dict | None:
    """One curator pass, if due (or forced). Returns what changed, or None when it isn't time yet."""
    data = load_ledger()
    last = float(data.get("curator_last_run") or 0)
    if not force and (time.time() - last < CURATE_EVERY or idle_seconds < IDLE_NEEDED):
        return None
    snap = await asyncio.to_thread(snapshot, store, "curate")
    moved = await asyncio.to_thread(curate_phase1, store)
    reviewed = await curate_phase2(store, router) if router is not None else []
    with _LOCK:
        data = load_ledger()
        data["curator_last_run"] = time.time()
        save_ledger(data)
    log.info("curator: moved %s, reviewed %s, snapshot %s", moved, reviewed, snap.name)
    return {"moved": moved, "reviewed": reviewed, "snapshot": snap.name}


# ─── learning from finished tasks ───────────────────────────────────────────────────────────
REFLECT_PROMPT = """An AI assistant on a Windows PC just finished a task. Decide whether it found a REUSABLE
procedure worth saving as a skill for next time (a multi-step way to do a kind of task: build/run a type of
project, automate a website, use an app, fix a class of error). One-off answers, chit-chat, and things that
failed are NOT skills.

Existing skills: {existing}

The request: {request}

What it did (tool calls in order, with results):
{actions}

Its final answer: {answer}

If a skill was used and the run showed it was wrong or incomplete, reply:
{{"lesson": {{"skill": "<existing name>", "text": "<one-sentence correction>"}}}}
If there is a NEW reusable procedure, reply:
{{"skill": {{"name": "<kebab-case>", "title": "<Title>", "description": "<when to use it, one line>",
"steps": ["<concrete step with commands/paths/checks>", "..."]}}}}
Otherwise reply {{}}. Reply with JSON only."""


def write_skill(store, name: str, title: str, description: str, steps: list[str] | str, author: str = "nova") -> Path:
    name = slug(name)
    if not name:
        raise ToolError("The skill needs a name.")
    existing = store.get(name)
    if existing and not is_agent_authored(store, existing):
        raise ToolError(f"'{name}' is a bundled skill; record a lesson on it with skill_update instead.")
    if isinstance(steps, str):
        body = steps.strip()
    else:
        body = "\n".join(f"{i}. {str(s).strip()}" for i, s in enumerate(steps, 1) if str(s).strip())
    if len(body) < 20:
        raise ToolError("Write out the steps - a skill needs real instructions.")
    text = f"# {title.strip() or name}\n\n> {description.strip()}\n\n{body}\n\n<!-- written by {author} {time.strftime('%Y-%m-%d')} -->\n"
    with _LOCK:
        path = existing["path"] if existing else learned_dir(store) / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if existing:
            snapshot(store, "update")
        path.write_text(text, encoding="utf-8")
        data = load_ledger()
        e = _entry(data, name)
        e["state"] = "active"
        e["last_used"] = time.time()
        save_ledger(data)
        store.reload()
    return path


def add_lesson(store, name: str, text: str) -> None:
    if not store.get(name):
        raise ToolError(f"No skill named '{name}'.")
    with _LOCK:
        data = load_ledger()
        e = _entry(data, name)
        if not any(l["text"] == text for l in e["lessons"]):
            e["lessons"].append({"text": text[:400], "at": time.time()})
            e["lessons"] = e["lessons"][-20:]
        save_ledger(data)


async def reflect(store, router, request: str, actions: list[str], answer: str, used: list[str]) -> str | None:
    """After a multi-step task: save a new skill or a lesson. Returns a one-line note for the user, or None."""
    existing = ", ".join(sorted(store.list()))[:2000] or "(none)"
    prompt = REFLECT_PROMPT.format(existing=existing, request=request[:1500], actions="\n".join(actions[-30:])[:6000],
                                   answer=answer[:1500])
    reply = await ask_llm(router, prompt, 900)
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if isinstance(data.get("lesson"), dict):
        name = slug(data["lesson"].get("skill", ""))
        if name in used and data["lesson"].get("text"):
            await asyncio.to_thread(add_lesson, store, name, str(data["lesson"]["text"]))
            return f"Noted a lesson for the '{name}' skill."
        return None
    sk = data.get("skill")
    if isinstance(sk, dict) and sk.get("name") and isinstance(sk.get("steps"), list) and len(sk["steps"]) >= 2:
        name = slug(sk["name"])
        if store.get(name):
            return None  # never overwrite from a background reflection
        await asyncio.to_thread(write_skill, store, name, str(sk.get("title") or name),
                                str(sk.get("description") or ""), [str(x) for x in sk["steps"][:20]], "reflection")
        return f"Saved what worked as a new skill: '{name}'."
    return None


# ─── tools ──────────────────────────────────────────────────────────────────────────────────
def _store(env: ToolEnv):
    if getattr(env, "skills", None) is None:
        raise ToolError("The skills library is not available.")
    return env.skills


def _t_skill_create(args: dict, env: ToolEnv) -> str:
    store = _store(env)
    steps = args.get("steps")
    if isinstance(steps, str):
        steps = [s for s in re.split(r"\n+", steps) if s.strip()]
    path = write_skill(store, str(args.get("name") or ""), str(args.get("title") or args.get("name") or ""),
                       str(args.get("description") or ""), steps or [])
    return f"Saved skill '{slug(args.get('name'))}' to {path}. Next time, find_skill will offer it."


def _t_skill_update(args: dict, env: ToolEnv) -> str:
    store = _store(env)
    name = slug(args.get("name") or "")
    skill = store.get(name)
    if skill is None:
        raise ToolError(f"No skill named '{name}'.")
    if args.get("content"):
        if not is_agent_authored(store, skill):
            raise ToolError("Bundled skills can't be rewritten; add a lesson instead (pass `lesson`).")
        with _LOCK:
            snapshot(store, "update")
            skill["path"].write_text(str(args["content"]).rstrip() + "\n", encoding="utf-8")
            store.reload()
        return f"Rewrote '{name}' (the old version is in a snapshot)."
    lesson = str(args.get("lesson") or "").strip()
    if not lesson:
        raise ToolError("Give either `lesson` (a correction learned this time) or `content` (the full new text).")
    add_lesson(store, name, lesson)
    return f"Recorded the lesson on '{name}'. The curator folds lessons into the steps; until then use_skill shows them."


def _t_skill_manage(args: dict, env: ToolEnv) -> str:
    store = _store(env)
    action = str(args.get("action") or "status").lower()
    name = slug(args.get("name") or "")
    if action in ("pin", "unpin"):
        if not store.get(name):
            raise ToolError(f"No skill named '{name}'.")
        with _LOCK:
            data = load_ledger()
            _entry(data, name)["pinned"] = action == "pin"
            save_ledger(data)
        return f"{'Pinned' if action == 'pin' else 'Unpinned'} '{name}'."
    if action == "restore":
        hits = sorted(archive_dir(store).glob(f"{name}*.md")) if archive_dir(store).exists() else []
        if not hits:
            raise ToolError(f"'{name}' is not in the archive.")
        with _LOCK:
            snapshot(store, "restore")
            dest = learned_dir(store) / f"{name}.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(hits[-1]), str(dest))
            data = load_ledger()
            e = _entry(data, name)
            e["state"], e["last_used"] = "active", time.time()
            save_ledger(data)
            store.reload()
        return f"Restored '{name}' from the archive."
    if action == "archive":
        skill = store.get(name)
        if not is_agent_authored(store, skill):
            raise ToolError("Only skills Nova wrote can be archived.")
        with _LOCK:
            snapshot(store, "archive")
            dest = archive_dir(store) / skill["path"].name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(skill["path"]), str(dest))
            data = load_ledger()
            _entry(data, name)["state"] = "archived"
            save_ledger(data)
            store.reload()
        return f"Archived '{name}' (skill_manage action=restore brings it back)."
    if action == "rollback":
        return rollback(store, str(args.get("snapshot") or ""))
    if action == "curate":
        router, loop = getattr(env, "llm", None), getattr(env, "loop", None)
        if router is not None and loop is not None:
            res = asyncio.run_coroutine_threadsafe(run_curator(store, router, force=True), loop).result(timeout=600)
        else:
            res = asyncio.run(run_curator(store, None, force=True))
        return "Curator pass done: " + json.dumps(res)
    # status
    data = load_ledger().get("skills", {})
    rows = []
    for s in sorted(store.list().values(), key=lambda s: s["name"]):
        e = data.get(s["name"], {})
        tag = "learned" if is_agent_authored(store, s) else "bundled"
        rows.append(f"- {s['name']} [{tag}, {e.get('state', 'active')}{', pinned' if e.get('pinned') else ''}] "
                    f"uses={e.get('uses', 0)} lessons={len(e.get('lessons', []))}")
    arch = sorted(p.stem for p in archive_dir(store).glob("*.md")) if archive_dir(store).exists() else []
    snaps = sorted(p.name for p in SNAPSHOTS.glob("*.tar.gz")) if SNAPSHOTS.exists() else []
    last = load_ledger().get("curator_last_run")
    return ("Skills:\n" + "\n".join(rows) + f"\nArchived: {', '.join(arch) or 'none'}"
            + f"\nSnapshots: {len(snaps)} (latest {snaps[-1] if snaps else '-'})"
            + f"\nCurator last ran: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last)) if last else 'never'}")


def lessons_for(name: str) -> list[str]:
    e = load_ledger().get("skills", {}).get(name) or {}
    return [l["text"] for l in e.get("lessons", [])]


def learning_tools(cfg: dict) -> list[Tool]:
    s = {"type": "string"}
    return [
        Tool("skill_create", "Save a procedure that just worked as a reusable skill (name, title, description = when "
             "to use it, steps = concrete steps with commands/paths/checks). Do this after finishing a multi-step "
             "task you'd want to repeat.", {"name": s, "title": s, "description": s,
                                            "steps": {"type": "array", "items": s}}, ["name", "description", "steps"],
             _t_skill_create, "skills"),
        Tool("skill_update", "Improve a skill: `lesson` records a correction learned while using it (any skill); "
             "`content` rewrites a skill Nova wrote. Snapshots are kept.", {"name": s, "lesson": s, "content": s},
             ["name"], _t_skill_update, "skills"),
        Tool("skill_manage", "Skill library housekeeping: action=status|pin|unpin|archive|restore|curate|rollback "
             "(rollback restores the last snapshot; snapshot= picks one).", {"action": s, "name": s, "snapshot": s},
             ["action"], _t_skill_manage, "skills",
             readonly=False),
    ]
