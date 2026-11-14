"""The roster: every model Forge can use, and what each one is good at.

Three layers, later ones win:
  1. built-in profiles below (a starting guess from each model family's reputation)
  2. your edits in ~/.forge/roster.json
  3. measurements from /roster bench (speed, tool calling, a small reasoning check)

The coordinator (team mode) reads this to decide which model gets which task."""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

from .config import HOME_DIR

ROSTER_FILE = HOME_DIR / "roster.json"
STATS_FILE = HOME_DIR / "roster_stats.json"

STRENGTHS = ["coding", "reasoning", "math", "tool-use", "fast", "vision", "long-context",
             "writing", "multilingual", "review", "general"]

# (substring of the model id, strengths, notes). First match wins, so specific patterns go first.
PROFILES: list[tuple[str, list[str], str]] = [
    ("gpt-oss-120b", ["reasoning", "coding", "tool-use"], "Open-weight 120B reasoning model. Strong all-rounder for code and multi-step tool work; fast on Groq and Cerebras."),
    ("gpt-oss-20b", ["fast", "tool-use"], "Small and fast. Simple edits, lookups, summaries."),
    ("nemotron-3-ultra", ["reasoning", "coding", "long-context", "review"], "NVIDIA's largest Nemotron. Slower; good for hard planning, reviews and long files."),
    ("bolt", ["fast"], "Very small and fast. Quick reads, summaries, simple questions; not for hard reasoning or big edits."),
    ("nemotron", ["reasoning", "coding"], "NVIDIA Nemotron family. Solid reasoning and agentic work."),
    ("minimax", ["coding", "tool-use", "vision"], "MiniMax agentic model that also accepts images. Good for coding and multi-step tool use."),
    ("glm", ["coding", "reasoning", "tool-use"], "Zhipu GLM. Strong at coding and agentic work."),
    ("kimi", ["coding", "long-context", "tool-use"], "Moonshot Kimi. Long context, agentic coding."),
    ("deepseek", ["coding", "reasoning", "math"], "DeepSeek. Strong code and math."),
    ("qwen", ["coding", "multilingual", "reasoning"], "Qwen. Good coder, many languages."),
    ("codestral", ["coding", "fast"], "Mistral's code model. Fast completions and edits."),
    ("mistral", ["writing", "multilingual"], "Mistral. Good writer; especially strong in French and other European languages."),
    ("command", ["writing", "multilingual", "long-context"], "Cohere Command. Writing, summarising long documents, many languages (incl. Arabic)."),
    ("gemma", ["general", "multilingual", "fast"], "Google Gemma. Light general model, many languages."),
    ("gemini", ["long-context", "vision", "reasoning"], "Gemini. Very long context and images."),
    ("gpt-4.1", ["coding", "long-context", "tool-use"], "GPT-4.1. Precise instruction following, long context, reliable tool calls."),
    ("gpt-5", ["reasoning", "coding", "tool-use"], "GPT-5 family. Strong reasoning and code."),
    ("llama", ["general", "writing"], "Llama. General chat and writing."),
]
UNPROFILED = (["general"], "No built-in profile yet. Run /roster bench, and describe it in ~/.forge/roster.json.")


@dataclass
class Worker:
    provider: str
    model: str
    strengths: list[str]
    notes: str
    source: str = "built-in"            # built-in | roster.json | unknown
    stats: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def prefer(self) -> tuple[str, str]:
        return (self.provider, self.model)

    def measured(self) -> str:
        s = self.stats
        if not s:
            return "not benched"
        if not s.get("up"):
            return f"DOWN at last check ({s.get('error', 'no answer')[:60]})"
        bits = [f"{s.get('latency', 0):.1f}s",
                "tools ✓" if s.get("tools") else "tools ✗",
                "check ✓" if s.get("check") else "check ✗ (got a simple reasoning question wrong)"]
        return ", ".join(bits)


def _load(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def profile(model: str) -> tuple[list[str], str]:
    low = model.lower()
    for pat, strengths, notes in PROFILES:
        if pat in low:
            st = list(strengths)
            if re.search(r"(^|[-/_])(vl|vision)([-/_.:]|$)", low) and "vision" not in st:
                st.append("vision")
            return st, notes
    return list(UNPROFILED[0]), UNPROFILED[1]


def build(router) -> list[Worker]:
    """One worker per (provider, model) Forge can call."""
    edits, stats = _load(ROSTER_FILE), _load(STATS_FILE)
    out = []
    for p in router.providers:
        for m in p.all_models():
            strengths, notes = profile(m)
            source = "built-in" if notes != UNPROFILED[1] else "unknown"
            own = edits.get(f"{p.name}:{m}") or edits.get(m)
            if isinstance(own, dict):
                strengths = [str(x) for x in own.get("strengths", strengths)]
                notes = str(own.get("notes", notes))
                source = "roster.json"
            out.append(Worker(p.name, m, strengths, notes, source, stats.get(f"{p.name}:{m}", {})))
    return out


def resolve(workers: list[Worker], name: str) -> Worker | None:
    """Find a worker by id, model id, or an unambiguous part of either."""
    name = (name or "").strip().lstrip("@").lower()
    if not name:
        return None
    for w in workers:
        if name in (w.id.lower(), w.model.lower()):
            return w
    hits = [w for w in workers if name in w.id.lower()]
    return hits[0] if len(hits) == 1 else None


def usable(w: Worker, router) -> bool:
    if w.stats and not w.stats.get("up"):
        return False
    provider = router.get(w.provider)
    return bool(provider and provider.pool.any_available(w.model) and not router.is_down(*w.prefer))


def pick_coordinator(workers: list[Worker], router, wanted: str = "") -> Worker | None:
    def qualified(w: Worker) -> bool:
        measured = ("reasoning" in w.strengths and w.stats.get("tools") and w.stats.get("check"))
        profiled = "reasoning" in w.strengths and "tool-use" in w.strengths
        return bool(measured or profiled)

    if wanted:
        w = resolve(workers, wanted)
        if w and usable(w, router) and qualified(w):
            return w
    ok = [w for w in workers if usable(w, router) and qualified(w)]
    # Prefer a measured tool-capable reasoner; otherwise a profiled tool-capable reasoner.
    for cond in (lambda w: "reasoning" in w.strengths and w.stats.get("tools") and w.stats.get("check"),
                 lambda w: "reasoning" in w.strengths and "tool-use" in w.strengths,
                 ):
        hit = [w for w in ok if cond(w)]
        if hit:
            return min(hit, key=lambda w: w.stats.get("latency", 99)) if hit[0].stats else hit[0]
    return None


def describe(workers: list[Worker], router) -> str:
    lines = []
    for w in workers:
        if not usable(w, router):
            continue
        lines.append(f"- {w.id} — good at: {', '.join(w.strengths)}. {w.notes} Measured: {w.measured()}.")
    return "\n".join(lines)


def write_template(workers: list[Worker]) -> None:
    """Create ~/.forge/roster.json with the current profiles so it can be edited."""
    if ROSTER_FILE.exists():
        return
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    data = {"_help": ("Describe what each model is good at. Keys are provider:model (or just the model id). "
                      "strengths can use: " + ", ".join(STRENGTHS) + " (or your own words).")}
    for w in workers:
        data[w.id] = {"strengths": w.strengths, "notes": w.notes}
    ROSTER_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# --- bench ----------------------------------------------------------------------------

CHECK = ("Which is larger, 9.11 or 9.8? And what is 17 * 23? "
         "Reply with exactly two values separated by a comma, nothing else.")
TOOL_PROMPT = "Read the file notes.txt using the tool."
TOOL = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a text file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


def bench_one(router, w: Worker, timeout: float = 60) -> dict:
    p = router.get(w.provider)
    key = p.pool.next_key() if p else None
    if not key:
        return {"up": False, "error": "no usable key", "when": time.time()}
    t = time.time()
    r = p.call(key, [{"role": "user", "content": CHECK}], None, w.model, timeout)
    latency = time.time() - t
    if not r:
        return {"up": False, "error": p.last_error, "when": time.time()}
    text = r["content"]
    check = "391" in text and "9.8" in text and "9.11" not in text
    r2 = p.call(p.pool.next_key() or key, [{"role": "user", "content": TOOL_PROMPT}], TOOL, w.model, timeout)
    return {"up": True, "latency": round(latency, 2), "check": check,
            "tools": bool(r2 and r2["tool_calls"]), "answer": text[:80], "when": time.time()}


def bench(router, workers: list[Worker], on_done=None) -> dict[str, dict]:
    results: dict[str, dict] = {}
    lock = threading.Lock()

    def run(w: Worker):
        res = bench_one(router, w)
        with lock:
            results[w.id] = res
            w.stats = res
        if on_done:
            on_done(w, res)

    # A model failing its bench must not push its whole provider to the back of the chain.
    saved = {p.name: p.last_fail for p in router.providers}
    threads = [threading.Thread(target=run, args=(w,), daemon=True) for w in workers]
    for th in threads:
        th.start()
    for th in threads:
        while th.is_alive():
            th.join(0.2)   # short joins keep ctrl+c working on Windows
    for p in router.providers:
        p.last_fail = saved.get(p.name, p.last_fail)
    stats = _load(STATS_FILE)
    stats.update(results)
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    STATS_FILE.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return results
