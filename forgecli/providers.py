"""OpenAI-compatible provider calls (result dict or None, never raises),
response screening, and the failover router."""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.request
import uuid

from . import images
from . import quality as _quality
from .config import ANON_KEY, HOME_DIR, ProviderConfig, Settings
from .keys import CooldownStore, KeyPool, seconds_until_utc_midnight, tail

STATS_FILE = HOME_DIR / "roster_stats.json"   # same file roster.py writes
LIMITS_FILE = HOME_DIR / "model_limits.json"  # learned: largest request each model accepts / rejects


def request_size(messages, tools) -> int:
    """Characters sent in one request: a stable stand-in for tokens (~4 chars each)."""
    return len(json.dumps(messages, ensure_ascii=False)) + len(json.dumps(tools or [], ensure_ascii=False))


class Thought(str):
    """A piece of the model's reasoning, passed to on_text for display only."""


class Action(str):
    """The name of a tool the model has started calling, passed to on_text for display only."""


class Limits:
    """What each model has shown it can and can't take, kept across runs.

    'fail' is the smallest request the model rejected as too large; 'ok' the largest it handled.
    A request at or above 'fail' isn't sent to that model again: it would only be rejected."""

    def __init__(self, path=None):
        self.path = path = path or LIMITS_FILE
        try:
            self.data: dict[str, dict] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        except OSError:
            pass

    def fits(self, provider: str, model: str, size: int) -> bool:
        cap = self.data.get(f"{provider}:{model}", {}).get("fail")
        return cap is None or size < cap * 0.95

    def cap(self, provider: str, model: str) -> int | None:
        return self.data.get(f"{provider}:{model}", {}).get("fail")

    def too_big(self, provider: str, model: str, size: int) -> None:
        d = self.data.setdefault(f"{provider}:{model}", {})
        # Never set the cap below a size the model has already handled.
        d["fail"] = max(min(d.get("fail", size), size), d.get("ok", 0) + 1)
        self._save()

    def handled(self, provider: str, model: str, size: int) -> None:
        d = self.data.setdefault(f"{provider}:{model}", {})
        if size > d.get("ok", 0):
            d["ok"] = size
            if d.get("fail") and d["fail"] <= size:
                d["fail"] = size + 1
            self._save()

# --- screening -------------------------------------------------------------

PSEUDO_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|$)", re.S)
TOOL_JUNK = [
    re.compile(r"<tool_call>.*?(?:</tool_call>|$)", re.S),
    re.compile(r"\[TOOL_CALLS?\].*?(?:\[/TOOL_CALLS?\]|$)", re.S | re.I),
    re.compile(r"<\|python_tag\|>.*$", re.S),
    re.compile(r"<\|tool_calls?_begin\|>.*?(?:<\|tool_calls?_end\|>|$)", re.S),
    re.compile(r"<function=.*?(?:</function>|$)", re.S),
]
VERDICT = re.compile(r"^\s*(?:user\s+|assistant\s+)?safety\s*:\s*(?:safe|unsafe)\b[^\n]*\n*", re.I)


def auth_headers(key: str) -> dict:
    """Keyless gateways get no Authorization header at all."""
    h = {"User-Agent": UA}
    if key and key != ANON_KEY:
        h["Authorization"] = f"Bearer {key}"
    return h


def new_call_id() -> str:
    return uuid.uuid4().hex[:9]


_ID_OK = re.compile(r"[A-Za-z0-9]{9}")


def portable(messages: list[dict]) -> list[dict]:
    """History any provider accepts: only standard fields, and tool-call ids in the
    strictest format (9 alphanumerics), mapped consistently. Old sessions and
    replies from other providers then work everywhere."""
    def fix(i: str) -> str:
        return i if _ID_OK.fullmatch(i or "") else hashlib.md5((i or "").encode()).hexdigest()[:9]

    out = []
    for m in messages:
        n = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            n["tool_calls"] = [{"id": fix(tc["id"]), "type": "function", "function": tc["function"]}
                               for tc in m["tool_calls"]]
        if m["role"] == "tool":
            n["tool_call_id"] = fix(m.get("tool_call_id", ""))
        out.append(n)
    return out


def _norm_tool_calls(raw) -> list[dict] | None:
    """Validate structured tool calls. None = malformed (treat as failure)."""
    out = []
    for tc in raw or []:
        fn = (tc or {}).get("function") or {}
        name = fn.get("name")
        args = fn.get("arguments", "{}")
        if not name:
            return None
        if isinstance(args, dict):
            args = json.dumps(args)
        try:
            json.loads(args or "{}")
        except ValueError:
            return None
        # Fresh 9-char alphanumeric ids: the strictest format any provider accepts (Mistral),
        # so a conversation started on one provider can be continued on any other.
        out.append({"id": new_call_id(),
                    "type": "function", "function": {"name": name, "arguments": args or "{}"}})
    return out


def screen(content: str | None, tool_calls: list[dict]) -> tuple[str, list[dict]] | None:
    text = content or ""
    # Some models write tool calls as text. Rescue them into real calls.
    if not tool_calls:
        for m in PSEUDO_TOOL_CALL.finditer(text):
            try:
                obj = json.loads(m.group(1))
                name = obj.get("name")
                args = obj.get("arguments", obj.get("parameters", {}))
                if name:
                    tool_calls.append({"id": new_call_id(), "type": "function",
                                       "function": {"name": name, "arguments": json.dumps(args)}})
            except ValueError:
                pass
    for rx in TOOL_JUNK:
        text = rx.sub("", text)
    text = VERDICT.sub("", text)
    if not text.strip() and not tool_calls:
        return None
    return text, tool_calls


# --- provider --------------------------------------------------------------

# Cloudflare (Groq) rejects the default Python-urllib agent with 403 / error 1010.
UA = "forge-cli/0.5"


class Provider:
    def __init__(self, cfg: ProviderConfig, store: CooldownStore, settings: Settings, log):
        self.name = cfg.name
        self.base_url = cfg.base_url
        self.model = cfg.model
        self.models = list(cfg.models)   # extra models usable with the same keys
        self.pool = KeyPool(cfg.name, cfg.keys, store)
        self.last_error = ""
        self.limits: dict[str, tuple[float, dict]] = {}   # key tail -> (when, rate-limit headers)
        self.settings = settings
        self.log = log
        self.last_fail = 0.0

    def list_models(self) -> list[str]:
        """Model ids from the provider's /models endpoint. Raises RuntimeError on failure."""
        key = self.pool.next_key() or self.pool.keys[0]
        req = urllib.request.Request(f"{self.base_url}/models",
                                     headers=auth_headers(key))
        try:
            with urllib.request.urlopen(req, timeout=self.settings.request_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{self.name}: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            raise RuntimeError(f"{self.name}: {e}") from None
        items = data.get("data", data if isinstance(data, list) else [])
        ids = [(m.get("id") if isinstance(m, dict) else str(m)) for m in items]
        return sorted({i.removeprefix("models/") for i in ids if i})

    def _fail(self, key: str, why: str):
        self.last_fail = time.time()
        self.last_error = why
        self.pool.store.count(self.pool.kid(key), ok=False)
        self.log(f"{self.name} {tail(key)}: {why}")
        return None

    def _note_limits(self, key: str, headers) -> None:
        if not headers:
            return
        vals = {k.lower(): v for k, v in headers.items()
                if k.lower().startswith(("x-ratelimit", "ratelimit", "x-ratelimit"))}
        if vals:
            self.limits[tail(key)] = (time.time(), vals)

    def remaining(self, key_tail: str | None = None) -> dict:
        """Latest rate-limit numbers the provider reported: {'req': (left, limit), 'tok': (left, limit)}."""
        items = [self.limits[key_tail]] if key_tail in self.limits else list(self.limits.values())
        if not items:
            return {}
        _, h = max(items, key=lambda x: x[0])
        out = {}
        for kind, names in (("req", "requests"), ("tok", "tokens")):
            left = h.get(f"x-ratelimit-remaining-{names}") or h.get(f"ratelimit-remaining-{names}")
            lim = h.get(f"x-ratelimit-limit-{names}") or h.get(f"ratelimit-limit-{names}")
            if left is not None:
                out[kind] = (left, lim)
        return out

    def credits(self) -> str:
        """Account balance, where the provider has an API for it (OpenRouter does)."""
        if self.name != "openrouter":
            return ""
        out = []
        for key in self.pool.keys:
            req = urllib.request.Request(f"{self.base_url}/key",
                                         headers=auth_headers(key))
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = json.loads(r.read().decode("utf-8")).get("data") or {}
            except Exception as e:
                out.append(f"{tail(key)}: couldn't check ({type(e).__name__})")
                continue
            used = float(d.get("usage") or 0)
            left = d.get("limit_remaining")
            bits = [("free tier" if d.get("is_free_tier") else "paid"), f"${used:.2f} used"]
            if left is not None:
                bits.append(f"${float(left):.2f} left")
            out.append(f"{tail(key)}: " + ", ".join(bits))
        return " · ".join(out)

    def all_models(self) -> list[str]:
        return [self.model] + [m for m in self.models if m != self.model]

    def _stream(self, r, on_text) -> dict:
        """Read a server-sent-events reply, passing text to on_text as it arrives.
        Returns a non-streaming-shaped body so the normal checks below apply."""
        content, reasoning, calls, finish, usage = [], 0, {}, None, {}
        for raw_line in r:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if chunk.get("error"):
                return {"error": chunk["error"]}
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    content.append(d["content"])
                    on_text(d["content"])
                think = d.get("reasoning_content") or d.get("reasoning")
                if isinstance(think, str) and think:
                    reasoning += len(think)
                    on_text(Thought(think))   # shown live to the user; never kept in the conversation
                for tc in d.get("tool_calls") or []:
                    idx = tc.get("index", len(calls))
                    if idx not in calls and (tc.get("function") or {}).get("name"):
                        on_text(Action(tc["function"]["name"]))   # "about to edit a file" etc.
                    slot = calls.setdefault(idx,
                                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    slot["function"]["arguments"] += fn.get("arguments") or ""
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        msg = {"content": "".join(content)}
        if not usage.get("completion_tokens"):
            # Some providers skip usage when streaming; estimate (~4 characters per token).
            usage = dict(usage, completion_tokens=max(1, (len(msg["content"]) + reasoning
                         + sum(len(c["function"]["arguments"]) for c in calls.values())) // 4))
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage}

    def call(self, key: str, messages: list[dict], tools: list[dict] | None,
             model: str | None = None, timeout: float | None = None, on_text=None) -> dict | None:
        """on_text(str) streams the reply as it is written; without it the reply comes in one piece."""
        model = model or self.model
        body = {"model": model, "messages": portable(images.parts(messages, model)),
                "max_tokens": self.settings.max_tokens, "temperature": self.settings.temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        stream = on_text is not None and self.settings.stream
        if stream:
            body["stream"] = True
        headers = {"Content-Type": "application/json", **auth_headers(key)}
        if self.name == "openrouter":
            headers["X-Title"] = "forge-cli"
        req = urllib.request.Request(f"{self.base_url}/chat/completions",
                                     data=json.dumps(body).encode(), headers=headers, method="POST")
        status, raw, resp_headers, data = 0, b"", {}, None
        try:
            # The timeout is per read: it fires if the server goes silent that long, so a streaming
            # reply can run as long as it keeps sending.
            with urllib.request.urlopen(req, timeout=timeout or self.settings.request_timeout) as r:
                status, resp_headers = r.status, r.headers
                if stream and "text/event-stream" in (r.headers.get("Content-Type") or ""):
                    data = self._stream(r, on_text)
                else:
                    raw = r.read()
        except urllib.error.HTTPError as e:
            status, raw, resp_headers = e.code, e.read(), e.headers
        except Exception as e:  # network, DNS, timeout, TLS...
            why = "timed out" if "timed out" in str(e).lower() else f"network error: {e}"
            return self._fail(key, f"{why} ({model})")
        self._note_limits(key, resp_headers)

        text = raw.decode("utf-8", "replace") if data is None else "{}"
        low = text.lower()
        if status == 429:
            retry_after = (resp_headers or {}).get("Retry-After")
            if any(s in low for s in ("per-day", "per day", "daily", "tokens per day", "requests per day", "rpd")):
                secs = seconds_until_utc_midnight()
                why = "daily limit"
            elif retry_after and retry_after.replace(".", "", 1).isdigit():
                secs, why = float(retry_after), "rate limit (Retry-After)"
            else:
                secs, why = 60, "per-minute limit"
            # Free tiers count limits per model, so only this model rests on this key.
            self.pool.bench(key, secs, model)
            return self._fail(key, f"429 {why}, benched {int(secs)}s")
        if status in (401, 403):
            self.pool.bench(key, 3600)
            return self._fail(key, f"{status} bad or revoked key, benched 1h")
        if status == 402:
            self.pool.bench(key, seconds_until_utc_midnight())
            return self._fail(key, "402 out of credits, benched until UTC midnight")
        if status == 413 or (status == 400 and ("too large" in low or "context length" in low
                                                 or "maximum context" in low or "too many tokens" in low)):
            return self._fail(key, f"HTTP {status} ({model}): conversation too large for this model/plan")
        if status != 200:
            return self._fail(key, f"HTTP {status} ({model}): {text[:300]}")

        if data is None:
            try:
                data = json.loads(text)
            except ValueError:
                return self._fail(key, "invalid JSON body")
        if data.get("error"):
            return self._fail(key, f"error in 200 body: {str(data['error'])[:300]}")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = _norm_tool_calls(msg.get("tool_calls"))
        if tool_calls is None:
            return self._fail(key, "malformed tool call")
        screened = screen(msg.get("content"), tool_calls)
        if not screened:
            return self._fail(key, "empty / junk / moderation-only reply")
        content, tool_calls = screened
        self.pool.store.count(self.pool.kid(key), ok=True)
        u = data.get("usage") or {}
        usage = {"in": int(u.get("prompt_tokens") or 0), "out": int(u.get("completion_tokens") or 0)}
        return {"content": content, "tool_calls": tool_calls,
                "truncated": choice.get("finish_reason") == "length" and not tool_calls,
                "provider": self.name, "key": tail(key), "model": model, "usage": usage}


# --- router ----------------------------------------------------------------

CONTINUE_PROMPT = ("Your previous reply was cut off by the length limit. Continue EXACTLY where it "
                   "stopped. No preamble, no repetition of earlier text, do not restart, and do not "
                   "open a new code fence if you were inside one.")


def quality(model: str, provider: str = "") -> int:
    """Capability 1-10, worked out automatically (see forgecli/quality.py)."""
    return _quality.score(model, provider)


STICKY_WAIT = 25   # seconds the task's own model is waited for (short rate limit) before switching


HANDOFF_NOTE = ("\n\n[Forge: you are taking over this task from another model ({who}), which became "
                "unavailable. Read the conversation above: the user's request, the work already done and "
                "its results. Continue the same plan, structure and style; don't redo finished work or "
                "start over.{todo}]")


class Router:
    def __init__(self, settings: Settings, log, store: CooldownStore | None = None):
        self.settings = settings
        self.log = log
        self.store = store or CooldownStore()
        self.providers = [Provider(p, self.store, settings, log) for p in settings.providers]
        self.pinned: str | None = None   # provider name, or None for automatic failover
        self.model_down: dict[tuple[str, str], float] = {}   # (provider, model) -> skip until
        self._speeds: dict[str, float] | None = None
        self.by_speed = settings.fastest_first
        self.by_quality = getattr(settings, "routing", "quality") != "speed"
        self.sticky: tuple[str, str] | None = None   # the model doing the current task
        self.todo = None                             # callable -> open checklist items, for handoffs
        self.limits = Limits()
        self.junk: dict[tuple[str, str], int] = {}   # consecutive empty/junk replies per model
        self.usage = None
        self.too_big = False     # last complete(): every model skipped or refused it for size
        self.size_cap = 0        # …and the biggest request any of them is known to take
        self._skipped_size: list[int] = []

    def get(self, name: str) -> Provider | None:
        return next((p for p in self.providers if p.name == name), None)

    def add_key(self, name: str, key: str, base_url: str = "", model: str = "") -> bool:
        """Use a key from now on (NEXT 7: /keys add, pasted keys). A new provider is placed before
        the keyless safety-net ones. False when the provider has no known endpoint."""
        p = self.get(name)
        if p:
            with p.pool._lock:
                if p.pool.keys == [ANON_KEY]:
                    p.pool.keys[:] = [key]
                elif key not in p.pool.keys:
                    p.pool.keys.append(key)
            return True
        from .config import KNOWN_PROVIDERS
        base, default = KNOWN_PROVIDERS.get(name, ("", ""))
        base, model = base_url or base, model or default
        if not (base and model):
            return False
        cfg = ProviderConfig(name, base.rstrip("/"), model, [key])
        self.settings.providers.append(cfg)
        new = Provider(cfg, self.store, self.settings, self.log)
        at = next((i for i, q in enumerate(self.providers) if q.pool.keys == [ANON_KEY]), len(self.providers))
        self.providers.insert(at, new)
        return True

    def remove_key(self, name: str, key: str) -> None:
        p = self.get(name)
        if p:
            with p.pool._lock:
                if key in p.pool.keys and len(p.pool.keys) > 1:
                    p.pool.keys.remove(key)
                elif key in p.pool.keys:
                    self.providers.remove(p)

    def speed(self, provider: str, model: str) -> float:
        """Measured seconds per reply (from /roster bench and real use); unknown = 5s."""
        if self._speeds is None:
            try:
                stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
                self._speeds = {k: float(v["latency"]) for k, v in stats.items()
                                if v.get("up") and v.get("latency")}
            except (OSError, ValueError, KeyError, TypeError):
                self._speeds = {}
        return self._speeds.get(f"{provider}:{model}", 5.0)

    def observe(self, provider: str, model: str, secs: float) -> None:
        """Blend a real reply time into the speed estimate used for ordering."""
        self.speed(provider, model)
        k = f"{provider}:{model}"
        old = self._speeds.get(k)
        self._speeds[k] = secs if old is None else 0.7 * old + 0.3 * secs

    def _ordered(self, offset: int = 0) -> list[Provider]:
        if self.pinned and self.get(self.pinned):
            return [self.get(self.pinned)]
        # Healthy providers first, fastest first; one that failed in the last 5 minutes goes
        # to the back. Ties keep the FORGE_PROVIDERS order.
        recent = time.time() - 300
        best = (lambda p: -max(quality(m, p.name) for m in self._models_of(p))) if self.by_quality \
            else (lambda p: 0)
        order = sorted(self.providers, key=lambda p: (self._health_rank(p.name, p.model),
                                                      p.last_fail > recent, best(p),
                                                      self.speed(p.name, p.model) if self.by_speed else 0))
        if offset and order:
            # Parallel agents start at different providers; the rest stay as fallbacks.
            k = offset % len(order)
            order = order[k:] + order[:k]
        return order

    def lead(self) -> Provider | None:
        """The provider that will be tried first."""
        order = self._ordered()
        return order[0] if order else None

    def any_available(self) -> bool:
        return any(p.pool.any_available() for p in self._ordered())

    def _try(self, p: Provider, messages, tools, model: str | None = None, ev=None,
             on_text=None) -> dict | None:
        ev = ev or (lambda *a: None)
        m = model or p.model
        if self.is_down(p.name, m):
            return None
        size = request_size(messages, tools)
        if not self.limits.fits(p.name, m, size):
            # It already told us it can't take a conversation this big; don't ask again.
            self.log(f"skipping {p.name}:{m} — {size:,} chars is over what it accepts")
            self._skipped_size.append(self.limits.cap(p.name, m) or 0)
            return None
        for _ in range(min(self.settings.keys_per_provider, len(p.pool.keys))):
            key = p.pool.next_key(m)
            if key is None:
                ev("benched", p.name, m, "all keys resting after limits")
                return None  # every key of this provider is benched
            ev("try", p.name, m, p.pool.kid(key).split(":", 1)[1])
            t0 = time.time()
            r = p.call(key, messages, tools, model, on_text=on_text)
            if r:
                self.observe(p.name, m, time.time() - t0)
                self.limits.handled(p.name, m, size)
                self.junk.pop((p.name, m), None)
                if self.usage is not None:
                    self.usage.record("timeout", f"{p.name}:{m}", True)
                return r
            ev("fail", p.name, m, p.last_error)
            if "too large" in p.last_error:
                self.limits.too_big(p.name, m, size)   # remembered, so it's skipped next time
                self._skipped_size.append(self.limits.cap(p.name, m) or 0)
                return None
            if p.last_error.startswith("timed out"):
                # A model that went silent is usually overloaded; skip it for 3 min (its sibling
                # models on the same provider are tried next).
                self.model_down[(p.name, m)] = time.time() + 180
                if self.usage is not None:
                    self.usage.record("timeout", f"{p.name}:{m}", False)
                return None
            if p.last_error.startswith("empty / junk"):
                _quality.record(p.name, m, False)
                n = self.junk[(p.name, m)] = self.junk.get((p.name, m), 0) + 1
                if n >= 2:   # it keeps failing at this job; let the others take it for a while
                    self.model_down[(p.name, m)] = time.time() + 300
                    self.junk.pop((p.name, m), None)
                return None
            if any(f"HTTP {c}" in p.last_error for c in (404, 410)):
                # the model is gone from this provider; stop asking it for an hour
                self.model_down[(p.name, m)] = time.time() + 3600
                return None
            if any(f"HTTP {c}" in p.last_error for c in (400, 413)):
                # Rejects how Forge builds the request (e.g. "System message must be at the
                # beginning"). It would fail the same way on every step, costing a round trip
                # each time, so rest it for 10 minutes.
                self.model_down[(p.name, m)] = time.time() + 600
                return None  # the model itself is the problem; other keys won't help
        return None

    def _all_too_small(self, messages, tools) -> bool:
        """True when every model still in play is known to reject a request this big."""
        size = request_size(messages, tools)
        return all(not self.limits.fits(p.name, p.model, size) or self.is_down(p.name, p.model)
                   for p in self._ordered())

    def is_down(self, provider: str, model: str) -> bool:
        return self.model_down.get((provider, model), 0) > time.time()

    def chain_once(self, messages, tools, offset: int = 0,
                   prefer: tuple[str, str] | None = None, ev=None, on_text=None) -> dict | None:
        # A preferred worker (team mode) goes first; the normal chain is the fallback.
        if prefer and not self.is_down(*prefer):
            p = self.get(prefer[0])
            if p:
                r = self._try(p, messages, tools, prefer[1], ev, on_text)
                if r:
                    return r
                self.model_down[prefer] = time.time() + 300   # skip it for 5 min
                self.log(f"{prefer[0]}:{prefer[1]} failed, falling back to the normal chain")
        pairs = self._pairs(offset)
        if images.has_images(messages):
            # The user attached an image: models that can see it go first.
            vis = [(p, m) for p, m in pairs if images.sees_images(m)]
            if not vis and not self.pinned:
                vis = [(p, m) for p in self._ordered(offset) for m in p.all_models()
                       if images.sees_images(m)][:3]
            pairs = vis + [pm for pm in pairs if pm not in vis]
        for p, m in pairs:
            r = self._try(p, messages, tools, m, ev, on_text)
            if r:
                return r
        return None

    def _pairs(self, offset: int = 0) -> list[tuple[Provider, str]]:
        """Every (provider, model) to try, in order. Quality routing ranks all models across all
        providers: healthy first, then the most capable, then the fastest."""
        pairs = [(p, m) for p in self._ordered(offset) for m in self._models_of(p)]
        if self.by_quality and not self.pinned and not offset:
            recent = time.time() - 300
            pairs.sort(key=lambda pm: (self._health_rank(pm[0].name, pm[1]), pm[0].last_fail > recent,
                                       (pm[0].name, pm[1]) in self.model_down
                                       and self.model_down[(pm[0].name, pm[1])] > time.time(),
                                       -quality(pm[1], pm[0].name),
                                       self.speed(pm[0].name, pm[1]) if self.by_speed else 0))
        return pairs

    def escalate(self, minutes: int = 20) -> str:
        """The task's model is stuck (looping, failing edits): set it aside for a while so the next
        request goes to the next best model, which gets the hand-off note. Returns who was dropped."""
        if self.pinned or not self.sticky:
            return ""
        who = self.sticky
        self.model_down[who] = time.time() + minutes * 60
        self.log(f"{who[0]}:{who[1]} is stuck on this task, handing it to the next model")
        return f"{who[0]} · {who[1]}"

    def new_turn(self) -> None:
        """Start of a user turn: keep the task's model unless a more capable one is usable again."""
        if not self.sticky or not self.by_quality:
            return
        now = time.time()
        for p, m in self._pairs():
            if p.pool.wait_for(m) <= 0 and self.model_down.get((p.name, m), 0) <= now:
                if quality(m, p.name) > quality(self.sticky[1], self.sticky[0]):
                    self.sticky = None
                return

    def _stick(self, messages, tools, ev, on_text) -> dict | None:
        """Try the model already doing this task (waiting briefly through a short rate limit)."""
        s = self.sticky
        p = self.get(s[0]) if s and not self.pinned else None
        if not p or self.model_down.get(s, 0) > time.time():
            return None
        wait = p.pool.wait_for(s[1])
        if wait > STICKY_WAIT:
            return None
        if wait > 0:
            if ev:
                ev("wait", p.name, s[1], f"{wait:.0f}")
            time.sleep(wait + 0.2)
        return self._try(p, messages, tools, s[1], ev, on_text)

    def _handoff(self, messages: list[dict]) -> list[dict]:
        """A copy of the conversation telling the next model it is taking over mid-task."""
        todo = ""
        try:
            items = self.todo() if self.todo else []
        except Exception:
            items = []
        if items:
            todo = " Still open on the checklist: " + "; ".join(items[:6]) + "."
        note = HANDOFF_NOTE.format(who=f"{self.sticky[0]} · {self.sticky[1]}", todo=todo)
        out = list(messages)
        if out and out[0].get("role") == "system" and isinstance(out[0].get("content"), str):
            out[0] = dict(out[0], content=out[0]["content"] + note)
        return out

    def _models_of(self, p: Provider) -> list[str]:
        """The provider's main model, then (when pinned via automatic failover) up to 3 of its
        other models: a limit or outage on one model rarely hits the others."""
        alts = [m for m in p.models if m != p.model][:3]
        return sorted([p.model] + alts, key=lambda model: self._health_rank(p.name, model))

    def _health_rank(self, provider: str, model: str) -> int:
        """Models with repeated timeouts or recent claim-check failures wait behind healthy ones."""
        if self.usage is None:
            return 0
        timed_out = self.usage.get("timeout", f"{provider}:{model}").get("consecutive_failures", 0) >= 3
        guard_failures = self.usage.recent_failures("guard", f"{provider}:{model}", 20) >= 3
        return int(timed_out) + int(guard_failures)

    def soonest(self) -> tuple[float, str]:
        """(seconds until something can be tried again, "provider·model"); (inf, "") if nothing will."""
        best, who = float("inf"), ""
        now = time.time()
        for p in self._ordered():
            for m in self._models_of(p):
                w = max(p.pool.wait_for(m), self.model_down.get((p.name, m), 0) - now, 0.0)
                if w < best:
                    best, who = w, f"{p.name}·{m.split('/')[-1]}"
        return best, who

    def complete(self, messages, tools, offset: int = 0,
                 prefer: tuple[str, str] | None = None, ev=None, on_text=None) -> dict | None:
        """ev(kind, provider, model, detail) reports progress: try / fail / benched / wait.
        on_text(str) receives the reply as it streams; after a 'fail' event the text so far is void."""
        rounds = self.settings.retry_rounds
        self.too_big, self.size_cap = False, 0
        deadline = time.time() + max(0, getattr(self.settings, "max_wait", 150))
        attempt = 0
        sticky = not offset and not prefer and not self.pinned   # one model per task (main agent)
        while True:
            self._skipped_size: list[int] = []
            r = self._stick(messages, tools, ev, on_text) if sticky else None
            if not r:
                msgs = self._handoff(messages) if sticky and self.sticky else messages
                r = self.chain_once(msgs, tools, offset, prefer, ev, on_text)
            if r:
                if sticky:
                    if self.sticky and self.sticky != (r["provider"], r["model"]):
                        self.log(f"task moved from {self.sticky[0]}:{self.sticky[1]} "
                                 f"to {r['provider']}:{r['model']}")
                    self.sticky = (r["provider"], r["model"])
                return self._continue(r, messages, tools, offset, prefer, ev, on_text)
            if self._skipped_size and self._all_too_small(messages, tools):
                # Waiting won't help: no model takes a request this size. The caller shrinks it.
                self.too_big, self.size_cap = True, max(self._skipped_size)
                return None
            left = deadline - time.time()
            wait, who = self.soonest()
            if wait > 0.5:
                # Everything is resting after limits: wait for the first one to come back
                # (instead of failing while /status says "ready" a minute later).
                if wait > left:
                    self.log(f"nothing frees up within FORGE_MAX_WAIT ({wait:.0f}s > {left:.0f}s left)")
                    break
                delay = wait + 0.5
            else:
                # Something is usable but just failed (5xx, network, junk): short jittered backoff.
                if attempt >= rounds or left <= 0:
                    break
                delay = min(20.0, 1.5 * 2 ** attempt) * random.uniform(0.5, 1.5)
                delay, who = min(delay, max(left, 0.5)), ""
            attempt += 1
            self.log(f"all sources failed, retrying in {delay:.1f}s" + (f" ({who})" if who else ""))
            if ev:
                ev("wait", "", who, f"{delay:.0f}")
            time.sleep(delay)
        return None

    def _continue(self, first: dict, messages, tools, offset: int = 0,
                  prefer: tuple[str, str] | None = None, ev=None, on_text=None) -> dict:
        parts, current = [first["content"]], first
        for _ in range(self.settings.max_continuations):
            if not current["truncated"]:
                break
            self.log("reply cut off by length limit, continuing")
            follow = messages + [{"role": "assistant", "content": "".join(parts)},
                                 {"role": "user", "content": CONTINUE_PROMPT}]
            # Finish with the same model that started the reply when possible.
            nxt = self.chain_once(follow, tools, offset, (first["provider"], first["model"]), ev, on_text)
            if not nxt:
                break
            parts.append(nxt["content"])
            for k in ("in", "out"):
                first["usage"][k] += nxt.get("usage", {}).get(k, 0)
            current = nxt
            if nxt["tool_calls"]:
                first["tool_calls"] = nxt["tool_calls"]
                break
        first["content"] = "".join(parts)
        first["truncated"] = current["truncated"]
        return first

    def status_lines(self) -> list[str]:
        out = []
        for p in self.providers:
            out.append(f"{p.name}  model={p.model}" + (f"  (+{', '.join(p.models)})" if p.models else ""))
            out.extend(p.pool.status_lines())
        return out
