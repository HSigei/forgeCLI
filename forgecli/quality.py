"""How capable each model is at building things, scored 1-10, worked out automatically.

Routing sends the work to the highest-scoring model that is available; speed only breaks ties.
Nobody has to maintain a list: any model a user adds gets a score from three layers.

  1. A first guess from the name: known model families, then the parameter count in the id
     ("70b", "397B-A17B"), lowered for "mini", "flash", "nano" and similar light versions.
  2. A quick automatic test (roster.bench_one): a small reasoning check and a tool-call check,
     run once in the background for models that haven't had one. Failing the tool call matters
     most: a model that can't call tools can't edit files or run commands.
  3. Its track record in real work: empty or junk replies, broken tool calls, claims the check
     caught, getting stuck, versus turns that finished cleanly. Recorded per model and kept.

Override any score in ~/.forge/model_quality.json: {"gpt-oss-120b": 9, "groq:openai/gpt-oss-120b": 7}
"""
from __future__ import annotations

import json
import re
import threading
import time

from .config import HOME_DIR

OVERRIDES_FILE = HOME_DIR / "model_quality.json"
RECORD_FILE = HOME_DIR / "model_record.json"
STATS_FILE = HOME_DIR / "roster_stats.json"     # written by roster.bench
BENCH_EVERY = 14 * 86400                        # re-test a model after two weeks
RECORD_SIZE = 40                                # outcomes kept per model
MIN_RECORD = 6                                  # outcomes needed before the record counts

# Frontier families whose names don't say how big they are. First match wins.
FAMILIES = [
    (r"claude-(opus|sonnet)|gpt-5(?![\w.-]*(mini|nano))|gemini-[2-9][.\d]*-pro|grok-4|kimi-k2"
     r"|qwen3-coder-480|qwen3\.5-397|nemotron-3-ultra|deepseek-(v[3-9]|r1)(?!.*(flash|lite|distill))"
     r"|glm-[4-9][.\d]*(?![\w.-]*(flash|air))|o[34](-pro)?$", 10),
    (r"claude-haiku|gpt-4\.1(?!-nano)|gpt-4o(?!-mini)|gemini-[2-9][.\d]*-flash(?!-lite)|gpt-5[\w.-]*mini"
     r"|mistral-(large|medium)|command-a", 8),
]
FAMILIES.insert(1, (r"minimax-m\d|deepseek-v[3-9]|qwen3[.\d]*-(max|plus)|kimi", 9))
# Light versions, matched as whole words so "minimax" and "gemini" don't count as "mini".
LIGHT = [(r"(?<![a-z])(nano|tiny|e[1-4]b)(?![a-z])", -3),
         (r"(?<![a-z])(mini|lite|small|bolt|air)(?![a-z])", -2),
         (r"(?<![a-z])(flash|lightning|turbo|haiku|instant|fast)(?![a-z])", -1)]
SIZE_TIERS = [(300, 9), (100, 8), (30, 7), (13, 6), (7, 5), (3, 4), (0, 3)]
_SIZE = re.compile(r"(?<![a-z])(\d+(?:\.\d+)?)\s*b(?![a-z])", re.I)

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "over": {}, "stats": {}, "record": {}}


def _load(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _fresh() -> dict:
    with _lock:
        if time.time() - _cache["at"] > 30:
            over = {}
            for k, v in _load(OVERRIDES_FILE).items():
                try:
                    over[str(k).lower()] = int(v)
                except (TypeError, ValueError):
                    pass
            _cache.update(at=time.time(), over=over, stats=_load(STATS_FILE),
                          record=_load(RECORD_FILE) or _cache["record"])
        return _cache


def size_of(model: str) -> float:
    """Total parameters in billions, from the model id (0 when it doesn't say)."""
    sizes = [float(n) for n in _SIZE.findall(model.split("/")[-1].replace("_", "-"))]
    sizes = [s for s in sizes if s >= 0.1]
    return max(sizes) if sizes else 0.0


def guess(model: str) -> int:
    """Layer 1: a first guess from the model id alone."""
    m = model.lower()
    for pattern, score in FAMILIES:
        if re.search(pattern, m):
            base = score
            break
    else:
        size = size_of(m)
        if size:
            base = next(score for floor, score in SIZE_TIERS if size >= floor)
        elif re.search(r"auto|free$", m.split("/")[-1]) and "/" not in m.rstrip("/").split("/")[-1]:
            base = 5   # a router's "auto" model: could be anything
        else:
            base = 6   # an unknown name is usually a mid-sized current model
        for pattern, cut in LIGHT:
            if re.search(pattern, m):
                return max(1, base + cut)
        return base
    for pattern, cut in LIGHT:
        if re.search(pattern, m):
            return max(1, base + max(cut, -2))
    return base


def tested(provider: str, model: str) -> int:
    """Layer 2: adjustment from the automatic test (0 when not tested yet)."""
    s = _fresh()["stats"].get(f"{provider}:{model}") or {}
    if not s.get("up"):
        return 0
    return (-3 if s.get("tools") is False else 0) + (-1 if s.get("check") is False else 0)


def track(provider: str, model: str) -> int:
    """Layer 3: adjustment from how the model has done in real work."""
    rec = _fresh()["record"].get(f"{provider}:{model}") or []
    if len(rec) < MIN_RECORD:
        return 0
    rate = sum(1 for x in rec if x) / len(rec)
    return 1 if rate >= 0.9 else 0 if rate >= 0.7 else -1 if rate >= 0.5 else -2


def score(model: str, provider: str = "") -> int:
    over = _fresh()["over"]
    m = model.lower()
    for key in (f"{provider}:{m}", m):
        if key in over:
            return max(1, min(10, over[key]))
    return max(1, min(10, guess(model) + tested(provider, model) + track(provider, model)))


def explain(model: str, provider: str = "") -> str:
    """A short "why" for /roster."""
    over = _fresh()["over"]
    if f"{provider}:{model.lower()}" in over or model.lower() in over:
        return "set in model_quality.json"
    parts = [f"name {guess(model)}"]
    t, k = tested(provider, model), track(provider, model)
    if t:
        parts.append(f"test {t:+d}")
    if k:
        parts.append(f"record {k:+d}")
    n = len(_fresh()["record"].get(f"{provider}:{model}") or [])
    if n:
        parts.append(f"{n} graded")
    return " · ".join(parts)


def record(provider: str, model: str, good: bool) -> None:
    """Remember one outcome of real work (kept across sessions)."""
    if not provider or not model:
        return
    c = _fresh()
    with _lock:
        rec = c["record"].setdefault(f"{provider}:{model}", [])
        rec.append(bool(good))
        del rec[:-RECORD_SIZE]
        try:
            HOME_DIR.mkdir(parents=True, exist_ok=True)
            RECORD_FILE.write_text(json.dumps(c["record"]), encoding="utf-8")
        except OSError:
            pass


def untested(router) -> list:
    """Workers with no recent automatic test."""
    from . import roster
    stats = _fresh()["stats"]
    now = time.time()
    return [w for w in roster.build(router)
            if now - (stats.get(w.id) or {}).get("when", 0) > BENCH_EVERY]


def auto_bench(router, on_done=None) -> threading.Thread | None:
    """Test new models in the background (two tiny requests each), so their scores are measured."""
    todo = untested(router)
    if not todo:
        return None

    def run():
        from . import roster
        try:
            roster.bench(router, todo)
        except Exception:
            return
        with _lock:
            _cache["at"] = 0.0
        if on_done:
            on_done(len(todo))
    t = threading.Thread(target=run, daemon=True, name="forge-auto-bench")
    t.start()
    return t
