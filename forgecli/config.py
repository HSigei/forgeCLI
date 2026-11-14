"""Settings, .env loading and provider definitions."""
from __future__ import annotations

import os
import pathlib
import platform
from dataclasses import dataclass, field

APP_DIR = pathlib.Path(__file__).resolve().parent.parent
HOME_DIR = pathlib.Path.home() / ".forge"

# Built-in OpenAI-compatible providers: name -> (base_url, default model).
# Any other name works too if you set <NAME>_BASE_URL and <NAME>_MODEL.
KNOWN_PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "cerebras": ("https://api.cerebras.ai/v1", "gpt-oss-120b"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "minimaxai/minimax-m3.1-flash"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-medium-latest"),
    "github": ("https://models.github.ai/inference", "openai/gpt-4.1"),
    "sambanova": ("https://api.sambanova.ai/v1", "Meta-Llama-3.3-70B-Instruct"),
    "huggingface": ("https://router.huggingface.co/v1", "openai/gpt-oss-120b"),
    "together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "cohere": ("https://api.cohere.ai/compatibility/v1", "command-a-03-2025"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "openrouter": ("https://openrouter.ai/api/v1", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    "openai": ("https://api.openai.com/v1", "gpt-4.1-mini"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    # More free tiers (from github.com/mnfst/awesome-free-llm-apis); used once you add a key.
    "zai": ("https://open.bigmodel.cn/api/paas/v4", "glm-4.7-flash"),
    "gonka": ("https://proxy.gonkabroker.com/v1", "deepseek-ai/DeepSeek-V4-Flash-0731"),
    "aion": ("https://api.aionlabs.ai/v1", "aion-labs/aion-3.0-mini"),
    "modelscope": ("https://api-inference.modelscope.cn/v1", "Qwen/Qwen3.5-35B-A3B"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "Qwen/Qwen3-8B"),
    # No key needed at all. Tried last, as a safety net when every keyed provider is resting.
    # They are shared public gateways: FORGE_KEYLESS=0 turns them off.
    "kilo": ("https://api.kilo.ai/api/gateway", "kilo-auto/free"),
    "llm7": ("https://api.llm7.io/v1", "gpt-oss:20b"),
    "ovh": ("https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", "gpt-oss-120b"),
}

# Providers that work without a key (verified 2026-11-08 with tool calls), with extra models.
KEYLESS = {
    "kilo": ["nvidia/nemotron-3.5-lightning:free", "nvidia/nemotron-3-ultra-550b-a55b:free"],
    "llm7": [],
    "ovh": ["Qwen3.5-397B-A17B"],
}
ANON_KEY = "anonymous"   # placeholder key: no Authorization header is sent


def load_dotenv(path: pathlib.Path) -> None:
    """Minimal .env reader. Real environment variables always win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    model: str
    keys: list[str]
    models: list[str] = field(default_factory=list)   # extra models on the same keys (<NAME>_MODELS)


@dataclass
class Settings:
    providers: list[ProviderConfig] = field(default_factory=list)
    max_tokens: int = 4096
    temperature: float = 0.2
    request_timeout: int = 30        # give up on a silent provider after this many seconds
    stream: bool = True               # show replies as they are written
    fastest_first: bool = True        # among equally capable models, try the fastest first
    routing: str = "quality"          # "quality": most capable model first, kept for the whole task; "speed": fastest first
    auto_open: bool = True            # open pages/servers a turn built in the browser
    auto_skill: bool = True           # load the skill a request needs automatically
    keys_per_provider: int = 4        # cap pool attempts per provider per chain pass
    retry_rounds: int = 3             # whole-chain retries (jittered backoff)
    max_wait: int = 150               # longest to wait for a rate limit to clear before giving up
    max_continuations: int = 3        # for replies cut off by max_tokens
    max_steps: int = 40               # tool-use steps per user turn
    guards: bool = True               # check final answers' claims against what tools did (FORGE_GUARDS=0 turns off)
    max_agents: int = 5               # parallel sub-agents at once
    coordinator: str = ""             # "provider:model" that plans and delegates in team mode
    team: bool = False                # start in team (coordinator) mode
    context_chars: int = 80_000       # compact history beyond this (~20k tokens)
    approval: str = "smart"           # smart | ask | auto  (see safety.py)
    shell: str = "powershell"
    auto_approve: bool = False
    verbose: bool = False


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def load_settings() -> Settings:
    # Only trusted locations: the app folder and ~/.forge. A .env in whatever
    # project you are working in is NOT loaded, so a repo cannot redirect your
    # keys to another server.
    load_dotenv(APP_DIR / ".env")
    load_dotenv(HOME_DIR / ".env")

    s = Settings()
    # Default: every built-in provider, in the order above. Ones without keys are skipped.
    order = os.environ.get("FORGE_PROVIDERS") or ",".join(KNOWN_PROVIDERS)
    names = [n.strip().lower() for n in order.split(",") if n.strip()]
    keyless_on = os.environ.get("FORGE_KEYLESS", "1").strip().lower() not in ("0", "off", "false", "no")
    # Keys added with /keys or /setup (NEXT 7): OS keychain or ~/.forge/keys.json, any number per
    # provider, plus custom OpenAI-compatible providers. A broken store never stops startup.
    stored: dict[str, list[str]] = {}
    custom: dict[str, dict] = {}
    try:
        from . import keyvault
        store = keyvault.KeyStore()
        stored, custom = store.load(), store.custom()
    except Exception:
        pass
    names += [n for n in list(stored) + list(custom) if n not in names and n not in KEYLESS]
    if keyless_on:
        names += [n for n in KEYLESS if n not in names]   # always last unless you place them
    for name in names:
        up = name.upper()
        base, model = KNOWN_PROVIDERS.get(name, ("", ""))
        if name in custom:
            base, model = custom[name].get("base_url", base), custom[name].get("model", model)
        base = os.environ.get(f"{up}_BASE_URL", base).rstrip("/")
        model = os.environ.get(f"{up}_MODEL", model)
        raw = os.environ.get(f"{up}_API_KEYS") or os.environ.get(f"{up}_API_KEY", "")
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        keys += [k for k in stored.get(name, []) if k not in keys]
        extra = [m.strip() for m in os.environ.get(f"{up}_MODELS", "").split(",") if m.strip()]
        if name in KEYLESS and not keys:
            if not keyless_on:
                continue
            keys = [ANON_KEY]
            extra = extra or list(KEYLESS[name])
        if base and model and keys:
            s.providers.append(ProviderConfig(name, base, model, keys, [m for m in extra if m != model]))

    s.max_tokens = _int("FORGE_MAX_TOKENS", s.max_tokens)
    s.request_timeout = _int("FORGE_TIMEOUT", s.request_timeout)
    s.stream = os.environ.get("FORGE_STREAM", "1").strip().lower() not in ("0", "off", "false", "no")
    s.auto_open = os.environ.get("FORGE_AUTO_OPEN", "1").strip().lower() not in ("0", "off", "false", "no")
    s.auto_skill = os.environ.get("FORGE_AUTO_SKILL", "1").strip().lower() not in ("0", "off", "false", "no")
    s.fastest_first = os.environ.get("FORGE_FASTEST_FIRST", "1").strip().lower() not in ("0", "off", "false", "no")
    s.routing = "speed" if os.environ.get("FORGE_ROUTING", "").strip().lower() == "speed" else "quality"
    s.keys_per_provider = _int("FORGE_KEYS_PER_PROVIDER", s.keys_per_provider)
    s.retry_rounds = _int("FORGE_RETRY_ROUNDS", s.retry_rounds)
    s.max_wait = _int("FORGE_MAX_WAIT", s.max_wait)
    s.max_continuations = _int("FORGE_MAX_CONTINUATIONS", s.max_continuations)
    s.max_steps = _int("FORGE_MAX_STEPS", s.max_steps)
    s.guards = os.environ.get("FORGE_GUARDS", "1").strip().lower() not in ("0", "false", "no", "off")
    s.max_agents = max(1, _int("FORGE_MAX_AGENTS", s.max_agents))
    s.coordinator = os.environ.get("FORGE_COORDINATOR", "").strip()
    s.team = os.environ.get("FORGE_TEAM", "").strip().lower() in ("1", "on", "true", "yes")
    s.context_chars = _int("FORGE_CONTEXT_CHARS", s.context_chars)
    s.approval = (os.environ.get("FORGE_APPROVAL") or s.approval).strip().lower()
    default_shell = "powershell" if platform.system() == "Windows" else "bash"
    s.shell = os.environ.get("FORGE_SHELL", default_shell).lower()
    return s


# --- user preferences (chosen models, pinned provider) ----------------------
PREFS_FILE = HOME_DIR / "config.json"


def load_prefs() -> dict:
    try:
        import json
        return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_prefs(prefs: dict) -> None:
    import json
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def apply_prefs(settings: Settings, prefs: dict) -> None:
    """Models picked with /model override the .env defaults."""
    for p in settings.providers:
        if p.name in prefs.get("models", {}):
            p.model = prefs["models"][p.name]
