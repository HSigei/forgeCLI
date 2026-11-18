"""Reminders and timers, saved on disk so they survive restarts."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import datetime

from forgecli.nova.memory import stems
from forgecli.nova.timeparse import next_occurrence
from forgecli.nova.core.config import DATA_DIR

MAX_REMINDERS = 200


class ReminderStore:
    def __init__(self, path=None):
        DATA_DIR.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path or DATA_DIR / "schedule.db"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS reminders (
                       id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, due REAL NOT NULL,
                       recur TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'reminder', created REAL NOT NULL)""")
            if "lead" not in [c[1] for c in self._db.execute("PRAGMA table_info(reminders)")]:
                self._db.execute("ALTER TABLE reminders ADD COLUMN lead TEXT NOT NULL DEFAULT ''")
            self._db.commit()

    @staticmethod
    def _row(r) -> dict:
        return {"id": r["id"], "text": r["text"], "due": r["due"], "kind": r["kind"], "lead": r["lead"],
                "recur": json.loads(r["recur"]) if r["recur"] else None}

    def add(self, text: str, due, recur: dict | None = None, kind: str = "reminder", lead: str = "") -> int:
        due_ts = due.timestamp() if isinstance(due, datetime) else float(due)
        text = re.sub(r"\s+", " ", str(text)).strip()[:200] or "Reminder"
        with self._lock:
            if self._db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] >= MAX_REMINDERS:
                raise ValueError("You already have a lot of reminders - cancel a few first.")
            cur = self._db.execute(
                "INSERT INTO reminders (text, due, recur, kind, created, lead) VALUES (?,?,?,?,?,?)",
                (text, due_ts, json.dumps(recur) if recur else "", kind, time.time(),
                 lead if lead in ("to", "that", "about", "of") else ""))
            self._db.commit()
            return cur.lastrowid

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM reminders ORDER BY due").fetchall()
        return [self._row(r) for r in rows]

    def match(self, query: str) -> list[dict]:
        q = query.strip().lower()
        items = self.list()
        if q in ("__all__", "all", "everything", ""):
            return items
        if re.fullmatch(r"(?:reminders?|timers?)", q):
            kind = "timer" if q.startswith("timer") else "reminder"
            return [i for i in items if i["kind"] == kind]
        want = stems(q)
        return [i for i in items if want & stems(i["text"])]

    def cancel(self, query: str) -> list[dict]:
        found = self.match(query)
        with self._lock:
            for i in found:
                self._db.execute("DELETE FROM reminders WHERE id=?", (i["id"],))
            self._db.commit()
        return found

    def pop_due(self, now: float | None = None) -> list[dict]:
        """Everything that is due. One-offs are removed; repeating ones move to their next time."""
        now = now or time.time()
        out = []
        with self._lock:
            rows = self._db.execute("SELECT * FROM reminders WHERE due <= ? ORDER BY due", (now,)).fetchall()
            for r in rows:
                item = self._row(r)
                item["late"] = max(0.0, now - r["due"])
                out.append(item)
                if item["recur"]:
                    nxt = next_occurrence(item["recur"], datetime.fromtimestamp(now)).timestamp()
                    self._db.execute("UPDATE reminders SET due=? WHERE id=?", (nxt, r["id"]))
                else:
                    self._db.execute("DELETE FROM reminders WHERE id=?", (r["id"],))
            if rows:
                self._db.commit()
        return out
