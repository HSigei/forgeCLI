"""API keys (NEXT 7): spot them, mask them, store them, test them, and keep them away from models.

- detect_provider / candidates: which provider a key belongs to, from its prefix.
- redact: replace keys pasted into text (known prefixes, plus long secret-looking tokens when the
  text talks about a key/token) before that text reaches a model, a session file or input history.
- KeyStore: unlimited keys per provider plus custom OpenAI-compatible providers. Secrets live in the
  OS keychain (`keyring`: Windows Credential Manager, macOS Keychain, Secret Service) when it is
  installed; otherwise in ~/.forge/keys.json (owner-only permissions where the OS supports it).
- test_key / identify: a live check against the provider (models list, else a 1-token call).
Keys are only ever shown as their last 4 characters.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import pathlib
import re
import urllib.error
import urllib.request

# Prefix -> provider names it can belong to (the first is the most likely). Longest prefix first.
# Checked against each provider's key format as of 2026-11; a bare "sk-" is shared by several
# providers, so those keys are identified with a live test (identify()).
PREFIXES: list[tuple[str, list[str]]] = [
    ("github_pat_", ["github"]), ("ghp_", ["github"]), ("gho_", ["github"]),
    ("sk-ant-", ["anthropic"]), ("sk-or-", ["openrouter"]), ("sk-proj-", ["openai"]),
    ("sk-svcacct-", ["openai"]), ("nvapi-", ["nvidia"]), ("gsk_", ["groq"]), ("AIza", ["gemini"]),
    ("hf_", ["huggingface"]), ("tgp_v1_", ["together"]),
    ("sk-", ["openai", "deepseek", "siliconflow"]),
]
PREFIXES.sort(key=lambda p: -len(p[0]))
_UNVERIFIED_PREFIXES = ("csk-", "xai-")
_KNOWN = re.compile(r"(?<![A-Za-z0-9_-])(?:" +
                    "|".join(re.escape(p) for p in [*(p for p, _ in PREFIXES), *_UNVERIFIED_PREFIXES]) +
                    r")[A-Za-z0-9_\-]{16,}")
# Keys without a known prefix (Mistral, Cohere, SambaNova…): long mixed tokens, only redacted when
# the text is about a key, so code hashes and ids in ordinary messages are left alone.
_LOOSE = re.compile(r"(?<![A-Za-z0-9_\-/.])[A-Za-z0-9][A-Za-z0-9_\-]{30,}(?![A-Za-z0-9_\-/.])")
_ABOUT_KEYS = re.compile(r"\b(api|key|keys|token|secret|credential)s?\b", re.I)

# Shown by the setup walkthrough: free tier?, what it's good at, where to get a key.
SIGNUP: dict[str, tuple[str, str, str]] = {
    "groq": ("free", "very fast replies", "https://console.groq.com/keys"),
    "cerebras": ("free", "fastest large models", "https://cloud.cerebras.ai/platform"),
    "gemini": ("free", "long context, vision", "https://aistudio.google.com/apikey"),
    "openrouter": ("free models", "many models behind one key", "https://openrouter.ai/keys"),
    "nvidia": ("free credits", "big open models", "https://build.nvidia.com/settings/api-keys"),
    "github": ("free", "GPT-4.1 and others via GitHub Models", "https://github.com/settings/tokens"),
    "mistral": ("free tier", "coding (Codestral, Devstral)", "https://console.mistral.ai/api-keys"),
    "sambanova": ("free", "fast Llama/DeepSeek", "https://cloud.sambanova.ai/apis"),
    "huggingface": ("free credits", "open models router", "https://huggingface.co/settings/tokens"),
    "cohere": ("trial", "Command A", "https://dashboard.cohere.com/api-keys"),
    "deepseek": ("paid, cheap", "strong coding/reasoning", "https://platform.deepseek.com/api_keys"),
    "openai": ("paid", "GPT models", "https://platform.openai.com/api-keys"),
}

SERVICE = "forge-cli"
ANON = "anonymous"   # same as config.ANON_KEY: keyless providers' placeholder
UA = "forge-cli/0.5"


def default_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("FORGE_KEYS_FILE") or pathlib.Path.home() / ".forge" / "keys.json")


def candidates(key: str) -> list[str]:
    key = key.strip()
    for prefix, names in PREFIXES:
        if key.startswith(prefix):
            return list(names)
    return []


def detect_provider(key: str) -> str | None:
    """The provider a key surely belongs to; None when unknown or shared (bare "sk-")."""
    c = candidates(key)
    return c[0] if len(c) == 1 else None


def mask(key: str) -> str:
    """Only ever show the last 4 characters."""
    return "…" + key[-4:] if len(key) > 8 else "…"


def _secret_like(tok: str) -> bool:
    if re.fullmatch(r"[0-9a-fA-F\-]+", tok):          # git hashes, uuids, checksums
        return False
    kinds = sum(bool(re.search(rx, tok)) for rx in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    return kinds >= 3 or (kinds == 2 and len(tok) >= 40)


def find_keys(text: str) -> list[str]:
    text = text or ""
    found = [m.group(0) for m in _KNOWN.finditer(text)]
    if _ABOUT_KEYS.search(text):
        found += [m.group(0) for m in _LOOSE.finditer(text)
                  if _secret_like(m.group(0)) and not any(m.group(0) in f or f in m.group(0) for f in found)]
    return list(dict.fromkeys(found))


def redact(text: str) -> tuple[str, list[str]]:
    """Text with every key replaced by a masked placeholder, plus the keys found."""
    found = find_keys(text)
    for k in sorted(found, key=len, reverse=True):
        text = text.replace(k, f"[{detect_provider(k) or 'api'} key {mask(k)} saved, hidden from the model]")
    return text, found


# --- live checks -----------------------------------------------------------------------------

def _request(url: str, key: str, body: dict | None, timeout: float) -> int:
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_key(base_url: str, key: str, model: str = "", timeout: float = 8.0) -> tuple[bool, str]:
    """(works, reason). Lists models; where a provider has no models list, sends a 1-token request."""
    base = base_url.rstrip("/")
    try:
        code = _request(base + "/models", key, None, timeout)
        if code in (404, 405) and model:
            code = _request(base + "/chat/completions", key, {
                "model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}, timeout)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"could not reach {base} ({getattr(e, 'reason', e)})"
    if 200 <= code < 300:
        return True, "works"
    if code == 429:
        return True, "works (rate-limited right now)"
    if code in (401, 403):
        return False, f"rejected by the provider ({code}): wrong or revoked key"
    if code == 402:
        return False, "accepted but the account has no credit (402)"
    return False, f"unexpected answer {code} from {base}"


def identify(key: str, endpoints: dict[str, tuple[str, str]], names: list[str] | None = None,
             timeout: float = 6.0) -> str | None:
    """Find which provider accepts a key by testing it against each candidate at once.
    endpoints: name -> (base_url, model). Returns the first candidate (in order) that works."""
    names = [n for n in (names or list(endpoints)) if n in endpoints]
    if not names:
        return None
    with cf.ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        futures = {n: pool.submit(test_key, *endpoints[n][:1], key, endpoints[n][1], timeout) for n in names}
        results = {n: f.result()[0] for n, f in futures.items()}
    return next((n for n in names if results[n]), None)


# --- storage ---------------------------------------------------------------------------------

def _keyring():
    if os.environ.get("FORGE_KEYRING", "1").strip().lower() in ("0", "off", "false", "no"):
        return None
    try:
        import keyring  # type: ignore
        from keyring.backends import fail  # type: ignore
        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring) or "null" in type(kr).__module__:
            return None
        return keyring
    except Exception:
        return None


def fingerprint(key: str) -> str:
    """Stable, non-reversible identifier for per-key usage records."""
    return "sha256-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def _kid(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


UNFILED = "_unfiled"


class KeyStore:
    """{"version": 2, "keys": {provider: [{"id", "tail", "secret"?}]}, "custom": {name: {...}}}
    Secrets go to the OS keychain when available ("secret" is then absent from the file)."""

    def __init__(self, path: pathlib.Path | str | None = None, backend: str | None = None):
        self.path = pathlib.Path(path) if path else default_path()
        if backend is None:
            backend = "file" if path else "auto"
        self.kr = _keyring() if backend in ("auto", "keyring") else None
        self.backend = "keyring" if self.kr else "file"

    # raw file ---------------------------------------------------------------
    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if "version" not in data:      # v1: {"groq": ["gsk_…"]}
            data = {"version": 2, "custom": {},
                    "keys": {p: [{"id": _kid(k), "tail": k[-4:], "secret": k} for k in ks if isinstance(k, str)]
                             for p, ks in data.items() if isinstance(ks, list)}}
        data.setdefault("keys", {})
        data.setdefault("custom", {})
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def _secret(self, provider: str, row: dict) -> str:
        if row.get("secret"):
            return row["secret"]
        if self.kr:
            try:
                return self.kr.get_password(SERVICE, f"{provider}:{row['id']}") or ""
            except Exception:
                return ""
        return ""

    # api --------------------------------------------------------------------
    def load(self) -> dict[str, list[str]]:
        """provider -> full keys (unfiled keys excluded)."""
        out = {}
        for p, rows in self._read()["keys"].items():
            if p == UNFILED:
                continue
            keys = [s for s in (self._secret(p, r) for r in rows) if s]
            if keys:
                out[p] = keys
        return out

    def add(self, key: str, provider: str | None = None, guesses: list[str] | None = None) -> str | None:
        """Store a key under its provider; returns that provider, or None when it was filed as
        unidentified (see unfiled() / file())."""
        key = key.strip()
        provider = provider or detect_provider(key)
        bucket = provider or UNFILED
        data = self._read()
        rows = data["keys"].setdefault(bucket, [])
        kid = _kid(key)
        if not any(r["id"] == kid for r in rows):
            row = {"id": kid, "tail": key[-4:]}
            if not provider:
                row["guesses"] = guesses if guesses is not None else candidates(key)
            stored = False
            if self.kr:
                try:
                    self.kr.set_password(SERVICE, f"{bucket}:{kid}", key)
                    stored = True
                except Exception:
                    stored = False
            if not stored:
                row["secret"] = key
            rows.append(row)
            self._write(data)
        return provider

    def remove(self, provider: str, index: int) -> bool:
        data = self._read()
        rows = data["keys"].get(provider, [])
        if not 0 <= index < len(rows):
            return False
        row = rows.pop(index)
        if self.kr and not row.get("secret"):
            try:
                self.kr.delete_password(SERVICE, f"{provider}:{row['id']}")
            except Exception:
                pass
        if not rows:
            data["keys"].pop(provider, None)
        self._write(data)
        return True

    def unfiled(self) -> list[tuple[str, list[str]]]:
        """(key, guesses) for keys whose provider is not known yet."""
        return [(self._secret(UNFILED, r), r.get("guesses", [])) for r in self._read()["keys"].get(UNFILED, [])]

    def file(self, index: int, provider: str) -> str | None:
        """Move unfiled key #index to provider; returns the key."""
        items = self.unfiled()
        if not 0 <= index < len(items):
            return None
        key = items[index][0]
        self.remove(UNFILED, index)
        self.add(key, provider)
        return key

    def custom(self) -> dict[str, dict]:
        return dict(self._read()["custom"])

    def add_custom(self, name: str, base_url: str, model: str) -> None:
        data = self._read()
        data["custom"][name] = {"base_url": base_url.rstrip("/"), "model": model}
        self._write(data)

    def rows(self) -> list[tuple[str, int, str]]:
        """(provider, index, masked) for display; never the key itself."""
        return [(p, i, "…" + r.get("tail", "")) for p, rs in sorted(self._read()["keys"].items())
                for i, r in enumerate(rs)]

    def listing(self) -> list[str]:
        return [f"{p} #{i}: {m}" for p, i, m in self.rows()]
