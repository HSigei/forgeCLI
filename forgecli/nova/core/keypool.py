"""Shared key rotation for Gemini (live voice and vision use the same pool).

Keys are tried starting from the one that last worked. A key that hits its quota is rested for a while
(longer each time it happens in a row) and the next key is used; a key Google rejects is skipped until restart.
"""
from __future__ import annotations

import threading
import time

from forgecli.nova.core.config import env_key_entries, env_keys

QUOTA_WORDS = ("429", "quota", "resource_exhausted", "resource exhausted", "rate limit", "rate_limit",
               "too many requests", "usage limit")
BAD_KEY_WORDS = ("api key not valid", "api_key_invalid", "invalid api key", "api key", "401", "403",
                 "permission_denied", "permission denied", "unauthenticated")
REST_STEPS = (60, 15 * 60, 60 * 60, 6 * 60 * 60)   # seconds a key rests after 1st, 2nd, 3rd... quota hit in a row


def is_quota_error(text: str) -> bool:
    low = str(text).lower()
    return any(w in low for w in QUOTA_WORDS)


def is_bad_key_error(text: str) -> bool:
    low = str(text).lower()
    return not is_quota_error(low) and any(w in low for w in BAD_KEY_WORDS)


def mask(key: str | None) -> str:
    return "(no key)" if not key else f"{key[:6]}...{key[-4:]}"


class RotatingKeys:
    def __init__(self, loader, entry_loader=None):
        self._loader = loader            # () -> list[str]
        self._entry_loader = entry_loader
        self._lock = threading.Lock()
        self._until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}
        self._dead: set[str] = set()
        self._current: str | None = None
        self._preferred_id: str | None = None

    def keys(self) -> list[str]:
        out: list[str] = []
        for k in self._loader():
            if k and k not in out:
                out.append(k)
        return out

    def ordered(self) -> list[str]:
        """Keys to try, best first: rested keys starting from the last good one, then (as a last resort)
        keys that are still resting, soonest-ready first. Rejected keys are never returned."""
        with self._lock:
            keys = [k for k in self.keys() if k not in self._dead]
            if not keys:
                return []
            entries = self._entries()
            preferred = next((key for label, key in entries if label == self._preferred_id), None)
            if preferred in keys:
                i = keys.index(preferred)
                keys = keys[i:] + keys[:i]
            if self._current in keys:
                i = keys.index(self._current)
                current = keys[i:] + keys[:i]
                keys = [preferred] + [key for key in current if key != preferred] if preferred in keys else current
            now = time.monotonic()
            ready = [k for k in keys if self._until.get(k, 0) <= now]
            resting = sorted((k for k in keys if k not in ready), key=lambda k: self._until.get(k, 0))
            return ready + resting

    def _entries(self) -> list[tuple[str, str]]:
        if self._entry_loader is not None:
            return self._entry_loader()
        return [(str(i), key) for i, key in enumerate(self.keys(), 1)]

    def prefer(self, identifier: str | None) -> None:
        with self._lock:
            self._preferred_id = identifier

    def status(self, preferred_id: str | None = None) -> list[dict]:
        with self._lock:
            now = time.monotonic()
            return [
                {
                    "id": label,
                    "masked": f"{key[:5]}…{key[-4:]}",
                    "status": ("rejected" if key in self._dead else
                               "cooling" if self._until.get(key, 0) > now else
                               "last used" if key == self._current else "ready"),
                    "preferred": label == (preferred_id if preferred_id is not None else self._preferred_id),
                    "last_used": key == self._current,
                    "cooldown_seconds": max(0, int(self._until.get(key, 0) - now)),
                }
                for label, key in self._entries()
            ]

    def ok(self, key: str) -> None:
        with self._lock:
            self._current = key
            self._strikes.pop(key, None)
            self._until.pop(key, None)

    def quota(self, key: str) -> float:
        """The key hit its limit: rest it and move on. Returns how long it will rest (seconds)."""
        with self._lock:
            n = self._strikes.get(key, 0)
            self._strikes[key] = n + 1
            rest = REST_STEPS[min(n, len(REST_STEPS) - 1)]
            self._until[key] = time.monotonic() + rest
            if self._current == key:
                keys = [k for k in self.keys() if k != key]
                self._current = keys[0] if keys else key
            return rest

    def bad(self, key: str) -> None:
        with self._lock:
            self._dead.add(key)

    def classify(self, key: str, error_text: str) -> str:
        """Updates the pool from an error message. Returns 'quota', 'bad' or 'other'."""
        if is_quota_error(error_text):
            self.quota(key)
            return "quota"
        if is_bad_key_error(error_text):
            self.bad(key)
            return "bad"
        return "other"


_pools: dict[str, RotatingKeys] = {}
_pools_lock = threading.Lock()


def _gemini_keys() -> list[str]:
    return env_keys("GEMINI") + env_keys("GOOGLE")


def _gemini_entries() -> list[tuple[str, str]]:
    entries = env_key_entries("GEMINI") + env_key_entries("GOOGLE")
    seen: set[str] = set()
    unique = []
    for label, key in entries:
        if key not in seen:
            seen.add(key)
            unique.append((label, key))
    return unique


def gemini_pool() -> RotatingKeys:
    with _pools_lock:
        if "gemini" not in _pools:
            _pools["gemini"] = RotatingKeys(_gemini_keys, _gemini_entries)
        return _pools["gemini"]
