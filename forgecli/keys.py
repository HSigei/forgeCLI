"""Round-robin key pools with per-key cooldowns that survive restarts."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import threading
import time

from .config import HOME_DIR


def tail(key: str) -> str:
    """Only ever show/log the last 4 characters of a key."""
    return "..." + key[-4:]


def key_id(provider: str, key: str) -> str:
    # Keys are never written to disk; only a short hash identifies them.
    return f"{provider}:{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def seconds_until_utc_midnight() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    nxt = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return int((nxt - now).total_seconds())


class CooldownStore:
    """Cooldowns and daily usage counts, persisted to ~/.forge/state.json."""

    def __init__(self, path: pathlib.Path | None = None):
        self.path = path or HOME_DIR / "state.json"
        self._lock = threading.Lock()
        self.until: dict[str, float] = {}
        self.usage: dict[str, dict[str, int]] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.until = {k: v for k, v in data.get("until", {}).items() if v > time.time()}
            today = dt.date.today().isoformat()
            self.usage = {today: data.get("usage", {}).get(today, {})}
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"until": self.until, "usage": self.usage}), encoding="utf-8")
        except OSError:
            pass

    def available(self, kid: str) -> bool:
        return time.time() >= self.until.get(kid, 0)

    def bench(self, kid: str, seconds: float) -> None:
        with self._lock:
            self.until[kid] = max(self.until.get(kid, 0), time.time() + seconds)
            self._save()

    def count(self, kid: str, ok: bool) -> None:
        with self._lock:
            today = dt.date.today().isoformat()
            day = self.usage.setdefault(today, {})
            day[kid + (":ok" if ok else ":fail")] = day.get(kid + (":ok" if ok else ":fail"), 0) + 1
            self._save()

    def remaining(self, kid: str) -> float:
        return max(0.0, self.until.get(kid, 0) - time.time())


class KeyPool:
    def __init__(self, provider: str, keys: list[str], store: CooldownStore):
        self.provider = provider
        self.keys = keys
        self.store = store
        self._idx = 0
        self._lock = threading.Lock()

    def kid(self, key: str) -> str:
        return key_id(self.provider, key)

    def usable(self, key: str, model: str | None = None) -> bool:
        """Key not resting, and (when a model is given) not resting for that model either.
        Most free tiers (NVIDIA, Groq) count limits per model, so a 429 on one model
        leaves the same key free for the others."""
        kid = self.kid(key)
        return self.store.available(kid) and (model is None or self.store.available(f"{kid}|{model}"))

    def next_key(self, model: str | None = None) -> str | None:
        """Next usable key, round-robin. None if every key is benched (for this model)."""
        with self._lock:
            for _ in range(len(self.keys)):
                key = self.keys[self._idx % len(self.keys)]
                self._idx += 1
                if self.usable(key, model):
                    return key
            return None

    def any_available(self, model: str | None = None) -> bool:
        return any(self.usable(k, model) for k in self.keys)

    def wait_for(self, model: str | None = None) -> float:
        """Seconds until some key is usable again (for this model); 0 if one is now."""
        waits = []
        for k in self.keys:
            kid = self.kid(k)
            w = self.store.remaining(kid)
            if model:
                w = max(w, self.store.remaining(f"{kid}|{model}"))
            waits.append(w)
        return min(waits) if waits else 0.0

    def bench(self, key: str, seconds: float, model: str | None = None) -> None:
        self.store.bench(self.kid(key) + (f"|{model}" if model else ""), seconds)

    def model_benches(self) -> dict[str, list[float]]:
        """model -> seconds left, one entry per key resting on that model."""
        out: dict[str, list[float]] = {}
        prefixes = {self.kid(k) + "|": k for k in self.keys}
        for kid in list(self.store.until):
            for pre in prefixes:
                if kid.startswith(pre):
                    rem = self.store.remaining(kid)
                    if rem > 0:
                        out.setdefault(kid[len(pre):], []).append(rem)
        return out

    def status_rows(self) -> list[tuple[str, bool, str, int, int]]:
        """(key tail, ready?, state text, ok today, failed today) per key."""
        today = dt.date.today().isoformat()
        day = self.store.usage.get(today, {})
        rows = []
        for k in self.keys:
            kid = self.kid(k)
            rem = self.store.remaining(kid)
            state = "ready" if rem <= 0 else (f"benched {int(rem // 3600)}h{int(rem % 3600 // 60)}m" if rem >= 3600
                                              else f"benched {int(rem // 60)}m{int(rem % 60)}s")
            rows.append((tail(k), rem <= 0, state, day.get(kid + ":ok", 0), day.get(kid + ":fail", 0)))
        return rows

    def status_lines(self) -> list[str]:
        today = dt.date.today().isoformat()
        day = self.store.usage.get(today, {})
        out = []
        for k in self.keys:
            kid = self.kid(k)
            rem = self.store.remaining(kid)
            state = "ready" if rem <= 0 else f"benched {int(rem // 60)}m{int(rem % 60)}s"
            out.append(f"  {self.provider:<11} {tail(k)}  {state:<16} "
                       f"ok={day.get(kid + ':ok', 0)} fail={day.get(kid + ':fail', 0)}")
        return out
