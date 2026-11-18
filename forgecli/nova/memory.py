"""Long-term memory: a small local SQLite database you can read, edit and delete from.

Deliberately simple for a CPU-only PC: keyword matching (no embedding model), a hard limit on how much
memory goes into each prompt, and a filter that refuses passwords, card numbers and ID numbers.

Memory is layered:
    core      pinned memories and standing rules ("always cc Ann on invoices") - in every prompt, and checked
              again right before any action they mention, however long ago they were said
    active    used or confirmed in the last 30 days - offered when the request matches
    stale     unused for 30 days - still searchable, only offered on a strong match
    archived  unused for 90 days - kept, never deleted, out of prompts until restored
`curate()` moves memories between layers without any model, and snapshots everything first.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

from forgecli.nova.core.config import DATA_DIR

log = logging.getLogger("brain.memory")

KINDS = ("fact", "preference", "project", "note", "rule")
TIERS = ("active", "stale", "archived")
STALE_DAYS, ARCHIVE_DAYS = 30, 90
RULE_HINT = re.compile(
    r"\b(from now on|going forward|whenever|every time|each time|make sure (?:you|to)|remember to|don'?t ever|do not ever|"
    r"never (?:send|post|email|use|delete|share|reply|commit|push|cc|book|schedule|call|text|message|put|write|run)|"
    r"always (?:send|post|email|use|reply|cc|bcc|commit|push|ask|book|schedule|put|save|write|run|check|include|add|sign))\b",
    re.I)
SENSITIVE = re.compile(
    r"\b(password|passcode|pin|cvv|api[ _-]?key|secret|token|ssn|passport|id number|credit card|card number)\b", re.I)
LONG_DIGITS = re.compile(r"\d[\d \-]{11,}\d")
STOP = set("""the a an and or but of to in on at for with is are was were be been am i you he she it we they me my
your our this that these those do does did have has had will would can could should not no yes just so very also
than then there here what when where which who how about from by as if into out up down over user users""".split())


def _stem(w: str) -> str:
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith(("sses", "xes", "zes", "ches", "shes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        w = w[:-1]
    if w.endswith("ing") and len(w) > 5:
        w = w[:-3]
    elif w.endswith("ed") and len(w) > 4:
        w = w[:-2]
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    return w


def stems(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9']+", text.lower()):
        w = w.replace("'s", "").strip("'")
        if len(w) < 3 or w in STOP:
            continue
        out.add(_stem(w))
    return out


def guess_kind(text: str) -> str:
    t = text.lower()
    if RULE_HINT.search(t):
        return "rule"
    if re.search(r"\b(prefer|like|love|hate|dislike|enjoy|always|never|favou?rite|can't stand)\b", t):
        return "preference"
    if re.search(r"\b(working on|building|developing|my project|project|studying|learning)\b", t):
        return "project"
    return "fact"


def refusal_reason(text: str) -> str | None:
    if SENSITIVE.search(text) or LONG_DIGITS.search(text):
        return "I don't store passwords, secret keys or ID and card numbers."
    return None


# ------------------------------------------------------------------------ learning without any brain
HEALTH_MONEY = re.compile(r"\b(sick|ill|disease|diagnos\w*|medication|pills?|doctor|hospital|depress\w*|anxi\w*|"
                          r"salary|debt|loan|owe|bank|broke|pregnan\w*|religio\w*|party|vote|voting)\b", re.I)
_REL = (r"girlfriend|boyfriend|wife|husband|partner|fiance|brother|sister|mother|mom|mum|father|dad|son|daughter|"
        r"friend|best friend|dog|cat|boss")
LEARN_RULES: list[tuple[re.Pattern, str, bool]] = [   # (pattern, sentence, lowercase the captured words?)
    (re.compile(r"(?i:\bmy name is) ([A-Z][a-zA-Z'-]{1,20}(?: [A-Z][a-zA-Z'-]{1,20})?)"), "The user's name is {0}.", False),
    (re.compile(rf"(?i:\bmy ({_REL}) (?:is (?:called|named)|is|called|named)) ([A-Z][a-zA-Z'-]{{1,20}})\b"),
     "The user's {0} is called {1}.", False),
    (re.compile(r"\bi (?:really |absolutely )?(love|like|enjoy|prefer|hate|dislike) ([a-zA-Z0-9][^.!?,;]{2,50}?)"
                r"(?= because| but| and i | so |[.!?,;]|$)", re.I), "{0}s {1}.", True),
    (re.compile(r"\bi(?:'m| am) (?:currently |now )?(working on|building|developing|making|creating|studying|learning) "
                r"([^.!?,;]{3,60}?)(?= because| but| and i |[.!?,;]|$)", re.I), "Is {0} {1}.", True),
    (re.compile(r"(?i:\bi live in) ([A-Z][a-zA-Z .'-]{2,30}?)(?= and | but |[.!?,;]|$)"), "Lives in {0}.", False),
    (re.compile(r"(?i:\bi(?:'m| am) from) ([A-Z][a-zA-Z .'-]{2,30}?)(?= and | but |[.!?,;]|$)"), "Is from {0}.", False),
    (re.compile(r"\bi work (as|at|for) ([^.!?,;]{3,40}?)(?= because| but | and i |[.!?,;]|$)", re.I), "Works {0} {1}.", True),
]
_VAGUE_END = re.compile(r"\b(?:it|that|this|them|there|you|something|anything|everything)\.?$", re.I)


def learn_from_text(text: str) -> list[str]:
    """Pulls plainly-stated lasting facts out of one message with simple patterns (no model, no network)."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text or text.endswith("?") or len(text) > 400:
        return []
    out: list[str] = []
    for pattern, template, lower in LEARN_RULES:
        for m in pattern.finditer(text):
            groups = [g.strip() for g in m.groups()]
            if lower and groups:
                groups[0] = groups[0].lower()      # the verb ("LOVE" -> "love"); names and objects keep their case
            item = template.format(*groups)
            item = item[0].upper() + item[1:]
            if refusal_reason(item) or HEALTH_MONEY.search(item) or len(item) < 8 or _VAGUE_END.search(item):
                continue
            if item not in out:
                out.append(item[:240])
    return out[:3]


class MemoryStore:
    def __init__(self, path: Path | None = None, max_items: int = 500, json_path: Path | None = None):
        DATA_DIR.mkdir(exist_ok=True)
        self.path = Path(path) if path else DATA_DIR / "memory.db"
        self.json_path = Path(json_path) if json_path else self.path.with_name("memory.json")
        self.max_items = max_items
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL DEFAULT 'fact',
                    text TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'you',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recent (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL
                );
                """
            )
            have = {r[1] for r in self._db.execute("PRAGMA table_info(memories)").fetchall()}
            for col, decl in (("uses", "INTEGER NOT NULL DEFAULT 0"), ("last_used", "REAL NOT NULL DEFAULT 0"),
                              ("tier", "TEXT NOT NULL DEFAULT 'active'")):
                if col not in have:  # databases from before memory layers
                    self._db.execute(f"ALTER TABLE memories ADD COLUMN {col} {decl}")
            self._db.commit()
        self._restore_from_json()
        self._export()

    # ---------------------------------------------------------------- the JSON copy
    # Everything Nova knows is also kept in data/memory.json. It does not belong to any brain: whichever
    # online brain is answering gets the relevant lines in its prompt each turn, and if memory.db is ever
    # lost or replaced, the next start reads this file and picks up where it left off.
    def _export(self) -> None:
        try:
            with self._lock:
                mems = [self._row(r) for r in self._db.execute(
                    "SELECT * FROM memories ORDER BY pinned DESC, updated DESC").fetchall()]
                recent = [{"role": r["role"], "content": r["content"]} for r in self._db.execute(
                    "SELECT role, content FROM recent ORDER BY id").fetchall()]
            data = {"version": 1, "saved": time.strftime("%Y-%m-%d %H:%M:%S"), "memories": mems, "recent": recent}
            tmp = self.json_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.json_path)
        except Exception:
            log.warning("couldn't write %s", self.json_path, exc_info=True)

    def _restore_from_json(self) -> None:
        """At start: anything in memory.json that the database doesn't have yet is added back."""
        if not self.json_path.exists():
            return
        try:
            data = json.loads(self.json_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            keep = self.json_path.with_name(f"memory.unreadable-{time.strftime('%Y%m%d-%H%M%S')}.json")
            try:
                os.replace(self.json_path, keep)     # keep the damaged copy so nothing is lost
                log.warning("couldn't read %s; kept it as %s", self.json_path.name, keep.name)
            except OSError:
                log.warning("couldn't read %s", self.json_path.name)
            return
        items = data.get("memories") if isinstance(data, dict) else None
        restored = 0
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            res = self.add(it.get("text", ""), it.get("kind"), str(it.get("source") or "you"),
                           bool(it.get("pinned")), _export=False)
            if res["status"] == "added" and it.get("tier") in TIERS and it.get("tier") != "active":
                with self._lock:
                    self._db.execute("UPDATE memories SET tier=? WHERE id=?", (it["tier"], res["id"]))
                    self._db.commit()
            restored += res["status"] == "added"
        with self._lock:
            have_recent = self._db.execute("SELECT COUNT(*) FROM recent").fetchone()[0]
        recent = data.get("recent") if isinstance(data, dict) else None
        if not have_recent and isinstance(recent, list):
            self.save_recent([m for m in recent if isinstance(m, dict)], _export=False)
        if restored:
            log.info("Restored %d memories from %s", restored, self.json_path.name)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def clean(text: str) -> str:
        text = re.sub(r"\s+", " ", str(text or "")).strip().strip("\"'")
        return text[:240]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        keys = r.keys()
        return {"id": r["id"], "kind": r["kind"], "text": r["text"], "source": r["source"],
                "pinned": bool(r["pinned"]), "created": r["created"], "updated": r["updated"],
                "uses": r["uses"] if "uses" in keys else 0, "last_used": r["last_used"] if "last_used" in keys else 0,
                "tier": r["tier"] if "tier" in keys else "active"}

    # ---------------------------------------------------------------- write
    def add(self, text: str, kind: str | None = None, source: str = "you", pinned: bool = False,
            _export: bool = True) -> dict:
        text = self.clean(text)
        if len(text) < 3:
            return {"status": "refused", "reason": "That's too short to remember."}
        reason = refusal_reason(text)
        if reason:
            return {"status": "refused", "reason": reason}
        kind = kind if kind in KINDS else guess_kind(text)
        now = time.time()
        with self._lock:
            rows = self._db.execute("SELECT id, text FROM memories").fetchall()
            low = text.lower()
            for r in rows:
                other = r["text"].lower()
                if low == other or SequenceMatcher(None, low, other).ratio() >= 0.86:
                    # said again: it is clearly still true, so it comes back to the active layer
                    self._db.execute("UPDATE memories SET updated=?, tier='active' WHERE id=?", (now, r["id"]))
                    self._db.commit()
                    return {"status": "duplicate", "id": r["id"]}
            if len(rows) >= self.max_items:
                return {"status": "refused", "reason": "My memory is full - delete a few things first."}
            cur = self._db.execute(
                "INSERT INTO memories (kind, text, source, pinned, created, updated) VALUES (?,?,?,?,?,?)",
                (kind, text, source, int(pinned), now, now))
            self._db.commit()
            new_id = cur.lastrowid
        if _export:
            self._export()
        return {"status": "added", "id": new_id, "text": text, "kind": kind}

    def update(self, mid: int, text: str | None = None, kind: str | None = None, pinned: bool | None = None) -> bool:
        sets, vals = [], []
        if text is not None:
            text = self.clean(text)
            if len(text) < 3 or refusal_reason(text):
                return False
            sets.append("text=?")
            vals.append(text)
        if kind in KINDS:
            sets.append("kind=?")
            vals.append(kind)
        if pinned is not None:
            sets.append("pinned=?")
            vals.append(int(bool(pinned)))
        if not sets:
            return False
        sets.append("updated=?")
        vals.append(time.time())
        with self._lock:
            cur = self._db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", (*vals, int(mid)))
            self._db.commit()
            changed = cur.rowcount > 0
        if changed:
            self._export()
        return changed

    def delete(self, mid: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM memories WHERE id=?", (int(mid),))
            self._db.commit()
            changed = cur.rowcount > 0
        if changed:
            self._export()
        return changed

    def clear(self) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM memories")
            self._db.commit()
            n = cur.rowcount
        self._export()
        return n

    # ---------------------------------------------------------------- read
    def list(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM memories ORDER BY pinned DESC, updated DESC").fetchall()
        return [self._row(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def search(self, query: str, limit: int = 5, archived: bool = True) -> list[dict]:
        q = stems(query)
        if not q:
            return []
        scored = []
        for m in self.list():
            if not archived and m["tier"] == "archived":
                continue
            score = len(q & stems(m["text"]))
            if score:
                scored.append((score, m["updated"], m))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [m for _s, _u, m in scored[:limit]]

    def touch(self, ids: list[int]) -> None:
        """These memories were just used: count it, and bring them back to the active layer."""
        if not ids:
            return
        with self._lock:
            self._db.executemany("UPDATE memories SET uses=uses+1, last_used=?, tier='active' WHERE id=?",
                                 [(time.time(), int(i)) for i in ids])
            self._db.commit()

    def rules(self) -> list[dict]:
        return [m for m in self.list() if m["kind"] == "rule" and m["tier"] != "archived"]

    def relevant(self, query: str, budget_chars: int = 600) -> list[str]:
        """Memory lines to put in the prompt, layer by layer: core (pinned + standing rules), then matches.

        Rules get their own budget on top of budget_chars, because a rule said once months ago still has to
        be followed today."""
        items = [m for m in self.list() if m["tier"] != "archived"]
        q = stems(query)
        chosen, used_ids = [], []
        rules = [m for m in items if m["kind"] == "rule"]
        rules.sort(key=lambda m: (len(q & stems(m["text"])), m["pinned"], m["updated"]), reverse=True)
        used = 0
        for m in rules:
            line = "- RULE: " + m["text"]
            if used + len(line) > max(600, budget_chars):
                break
            chosen.append(line)
            if q & stems(m["text"]):
                used_ids.append(m["id"])
            used += len(line)
        used = 0
        pinned = [m for m in items if m["pinned"] and m["kind"] != "rule"][:6]
        matches = [m for m in self.search(query, 8, archived=False)
                   if not m["pinned"] and m["kind"] != "rule"
                   and (m["tier"] == "active" or len(q & stems(m["text"])) >= 2)]
        for m in pinned + matches:
            line = "- " + m["text"]
            if used + len(line) > budget_chars:
                break
            chosen.append(line)
            used += len(line)
            if not m["pinned"]:
                used_ids.append(m["id"])
        self.touch(used_ids)
        return chosen

    def rules_for(self, tool: str, args: dict) -> list[dict]:
        """Standing rules that mention what this action touches (its tool, recipients, paths, words)."""
        rules = self.rules()
        if not rules:
            return []
        try:
            arg_text = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            arg_text = str(args)
        what = stems(tool.replace("_", " ") + " " + arg_text[:4000])
        what |= {w.lower() for w in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", arg_text)}
        hits = []
        for m in rules:
            overlap = what & (stems(m["text"]) | {w.lower() for w in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", m["text"])})
            if overlap:
                hits.append((len(overlap), m))
        hits.sort(key=lambda h: -h[0])
        found = [m for _n, m in hits[:4]]
        self.touch([m["id"] for m in found])
        return found

    # ---------------------------------------------------------------- layers
    def snapshot(self, reason: str = "curate") -> Path:
        """A full copy of every memory, taken before anything moves between layers."""
        folder = self.path.parent / "memory-snapshots"
        folder.mkdir(exist_ok=True)
        dest = folder / f"memory-{time.strftime('%Y%m%d-%H%M%S')}-{reason}.json"
        dest.write_text(json.dumps({"memories": self.list()}, ensure_ascii=False, indent=1), encoding="utf-8")
        for old in sorted(folder.glob("memory-*.json"))[:-30]:  # keep the last 30
            try:
                old.unlink()
            except OSError:
                pass
        return dest

    def curate(self, now: float | None = None) -> dict:
        """Deterministic layering, no model: unused 30 days -> stale, 90 days -> archived. Pinned memories and
        rules never fade. Nothing is deleted, and a snapshot is taken first."""
        now = now or time.time()
        moves = {"stale": 0, "archived": 0, "active": 0}
        snap = self.snapshot()
        with self._lock:
            rows = self._db.execute("SELECT * FROM memories").fetchall()
            for r in rows:
                m = self._row(r)
                if m["pinned"] or m["kind"] == "rule":
                    want = "active"
                else:
                    idle = (now - max(m["last_used"], m["updated"])) / 86400
                    want = "archived" if idle >= ARCHIVE_DAYS else "stale" if idle >= STALE_DAYS else "active"
                if want != m["tier"]:
                    self._db.execute("UPDATE memories SET tier=? WHERE id=?", (want, m["id"]))
                    moves[want] += 1
            self._db.commit()
        if any(moves.values()):
            self._export()
        return {"moved": moves, "snapshot": str(snap)}

    def set_tier(self, mid: int, tier: str) -> bool:
        if tier not in TIERS:
            return False
        with self._lock:
            cur = self._db.execute("UPDATE memories SET tier=?, updated=? WHERE id=?", (tier, time.time(), int(mid)))
            self._db.commit()
            changed = cur.rowcount > 0
        if changed:
            self._export()
        return changed

    def layers(self) -> dict:
        out = {"core": 0, "active": 0, "stale": 0, "archived": 0}
        for m in self.list():
            if m["tier"] == "archived":
                out["archived"] += 1
            elif m["pinned"] or m["kind"] == "rule":
                out["core"] += 1
            else:
                out[m["tier"]] += 1
        return out

    # ---------------------------------------------------------------- recent conversation
    def save_recent(self, messages: list[dict], _export: bool = True) -> None:
        rows = [(m["role"], m["content"]) for m in messages
                if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
        with self._lock:
            self._db.execute("DELETE FROM recent")
            self._db.executemany("INSERT INTO recent (role, content) VALUES (?,?)", rows)
            self._db.commit()
        if _export:
            self._export()

    def load_recent(self, limit: int = 12) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT role, content FROM recent ORDER BY id").fetchall()
        msgs = [{"role": r["role"], "content": r["content"]} for r in rows][-limit:]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs


# ------------------------------------------------------------------------ learning from conversation
def build_extraction_prompt(user_messages: list[str], known: list[str]) -> str:
    said = "\n".join(f"- {m}" for m in user_messages)
    have = "\n".join(f"- {k}" for k in known[:8]) or "- (nothing yet)"
    return (
        "You note lasting facts about a person from THEIR OWN messages.\n"
        f"Their messages:\n{said}\n\nAlready known:\n{have}\n\n"
        "Reply with only a JSON array of at most 3 short strings (under 15 words each). Each string must be a "
        "durable fact, preference, or ongoing project that the person clearly stated about themselves, "
        "written as a plain statement (e.g. \"Likes dark mode\", \"Is building a desktop companion app\"). "
        "Skip temporary things, questions, commands, anything about the assistant, and anything private: "
        "health, money, passwords, ID numbers, other people's details. If nothing qualifies, reply [].\n"
        "JSON:"
    )


def parse_extraction(reply: str, user_messages: list[str]) -> list[str]:
    m = re.search(r"\[.*?\]", reply, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    said = stems(" ".join(user_messages))
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, str):
            continue
        item = MemoryStore.clean(item)
        # must be grounded in what the person actually said, not invented by the model
        if len(item) < 6 or refusal_reason(item) or not (stems(item) & said):
            continue
        out.append(item)
        if len(out) == 3:
            break
    return out


class MemoryAgent:
    """Quietly learns from the conversation when you've been idle for a while."""

    def __init__(self, state, conn, busy, ensure_model):
        self.state = state
        self.conn = conn
        self.busy = busy
        self.ensure_model = ensure_model
        self.cfg = state.cfg["memory"]
        self.pending: list[str] = []
        self.last_activity = time.monotonic()
        self.task: asyncio.Task | None = None

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def note_user_message(self, text: str) -> None:
        if self.mode != "rules" and len(text.split()) >= 5:
            self.pending = (self.pending + [text[:300]])[-6:]

    @property
    def mode(self) -> str:
        """'rules' = learn only with simple patterns, nothing leaves the PC; 'online' = also ask the online brain."""
        return "rules" if str(self.cfg.get("learn_mode", "online")).lower() == "rules" else "online"

    async def learn_rules(self, text: str) -> None:
        """Instant learning with simple patterns: works with any brain (or none), and when offline."""
        if not self.cfg.get("auto_extract"):
            return
        added = []
        for item in learn_from_text(text):
            res = await asyncio.to_thread(self.state.memory.add, item, None, "auto")
            if res["status"] == "added":
                added.append({"id": res["id"], "text": res["text"]})
        if added:
            try:
                await self.conn.raw_send({"type": "memory_added", "items": added})
            except Exception:
                pass
            log.info("Learned %d thing(s) by pattern", len(added))

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def run(self) -> None:
        while True:
            await asyncio.sleep(5)
            if not (self.cfg.get("auto_extract") and self.pending and not self.busy()):
                continue
            if time.monotonic() - self.last_activity < float(self.cfg.get("idle_seconds", 45)):
                continue
            if self.state.gen_lock.locked():
                continue
            self.task = asyncio.create_task(self._extract())
            await self.task

    async def _extract(self) -> None:
        batch = list(self.pending)
        try:
            async with self.state.gen_lock:
                ok, model, _err = await self.ensure_model(self.state, allow_cloud=True)
                if not ok:
                    return
                known = [m["text"] for m in await asyncio.to_thread(self.state.memory.list)]
                reply = await self.state.llm.chat_once(
                    model, [{"role": "user", "content": build_extraction_prompt(batch, known)}],
                    num_predict=120, temperature=0.0)
            added = []
            for item in parse_extraction(reply, batch):
                res = await asyncio.to_thread(self.state.memory.add, item, None, "auto")
                if res["status"] == "added":
                    added.append({"id": res["id"], "text": res["text"]})
            self.pending = self.pending[len(batch):]
            if added:
                await self.conn.raw_send({"type": "memory_added", "items": added})
                log.info("Learned %d thing(s) from conversation", len(added))
        except asyncio.CancelledError:
            return  # you started talking again; try later
        except Exception:
            log.exception("memory extraction failed")
            self.pending = self.pending[len(batch):]
