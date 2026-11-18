"""Shared settings, logging and the local auth token."""
from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import time
from shutil import copy as shutil_copy
from pathlib import Path

# Forge keeps Nova's state (apps.json, mcp.json, skills, models, data) under ~/.forge/nova, not in the package.
ROOT = Path(os.environ.get("FORGE_NOVA_HOME") or Path.home() / ".forge" / "nova")
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"   # PowerShell helpers shipped with Forge
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    "agent_name": "Nova",
    "user_name": "",
    "brain": {"host": "127.0.0.1", "port": 8765},
    "conversation": {"max_history_messages": 12},
    "face": {"sleep_after_minutes": 5, "scale": 1.0, "roam_default": False},
    "tools": {
        "enabled": True,
        "gate": True,
        "fast_path": True,
        "max_steps": 4,
        "enable_shell": False,
        "workspace": "workspace",
        "allowed_folders": [],
        "confirm_timeout": 90,
        "search_seconds": 30,
        "search_roots": [],
        "app_aliases": {},
        "close_wait": 3.0,            # seconds to wait for a window to close before asking about force close
        "close_background": "ask",    # app still running in the tray after its window closed: ask | quit | leave
        "close_ask_over": 2,          # "close chrome" with MORE windows than this asks "close all N windows?"
        "permissions": {},
    },
    "cloud": {
        "enabled": True,
        "order": ["openrouter", "abliteration", "pollinations"],
        "share_private_content": False,
        "share_memories": True,
        "timeout_seconds": 60,
        "key_preferences": {},
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "models": ["meta-llama/llama-3.3-70b-instruct:free", "mistralai/mistral-small-3.1-24b-instruct:free"],
        },
        "abliteration": {"base_url": "https://api.abliteration.ai/v1", "models": []},
        "pollinations": {"base_url": "https://text.pollinations.ai/openai", "models": ["openai"]},
    },
    # code mode: online brains get one run_code tool and write Python that calls the tools (keeps context small)
    "code_mode": {"enabled": True, "local": False, "timeout_seconds": 600, "max_output_chars": 6000},
    "schedule": {"enabled": True, "snooze_minutes": 10},
    "memory": {"enabled": True, "auto_extract": True, "learn_mode": "online", "idle_seconds": 45, "prompt_chars": 600, "max_items": 500, "remember_conversation": True},
    "awareness": {
        "enabled": True,
        "watch_default": False,
        "poll_seconds": 3,
        "ocr_interval_seconds": 25,
        "ocr_interval_other_seconds": 60,
        "min_idle_seconds": 4,
        "suggest_cooldown_minutes": 10,
        "watch_apps": ["*"],
        "exclude_apps": ["keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe", "lastpass.exe",
                         "dashlane.exe"],
        "exclude_title_words": ["password", "bank", "paypal", "mpesa", "incognito", "private browsing"],
        "vision_model": "gemini-3.5-flash-lite",   # Gemini looks at the screen/camera picture (sent to Google for that one question)
        "vision_fallback": True,
        "camera_enabled": False,
        "camera_index": 0,
    },
    "voice": {
        "speak": True,
        "tts_engine": "auto",
        "piper_voice": "voices/en_US-lessac-medium.onnx",
        "sapi_rate": 0,
        "stt_enabled": True,
        "stt_model": "small.en",
        "stt_language": "en",
        "stt_threads": 4,
        "stt_beam_size": 5,
        "stt_preprocess": True,
        "stt_vad": True,
        "stt_vocabulary": [],
        "stt_corrections": {},
        "stt_confirm_low_confidence": True,
        "stt_confidence_floor": -0.85,
        "mic_device": "",
        "mic_silence_seconds": 1.2,
        "mic_threshold": 0,
    },
    "live": {
        "enabled": True,           # use Gemini Live for instant voice when a GEMINI_API_KEY is in .env
        "start_on_launch": True,   # connect as soon as I start (otherwise: right-click > Live voice)
        "model": "",               # blank = find the right Live model automatically
        "voice": "Aoede",          # Aoede, Puck, Charon, Kore, Fenrir, Leda, Orus, Zephyr ...
        "barge_in": False,         # True = you can talk over her (use headphones, or she hears herself)
        "idle_minutes": 10,        # go back to sleep after this long without conversation (0 = never)
        "mic_tail_ms": 600,
    },
    "wake": {
        "enabled": True,
        "model": "tiny.en",
        "allow_bare_name": False,
        "extra_phrases": [],
        "sensitivity": 0.7,
        "window_seconds": 2.2,
        "command_wait_seconds": 6.0,
    },
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_env() -> None:
    """Reads KEY=VALUE lines from .env into the environment (never overriding what is already set)."""
    for name in (".env", ".env.local"):
        path = ROOT / name
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip().strip("\"'")
                if key.strip() and value and key.strip() not in os.environ:
                    os.environ[key.strip()] = value
        except OSError:
            pass


def env_key_entries(prefix: str) -> list[tuple[str, str]]:
    """Return unique (environment-variable label, key) pairs for a provider."""
    entries: list[tuple[str, str]] = []
    for var in sorted(os.environ, key=lambda v: (len(v), v)):
        if var == f"{prefix}_API_KEYS":
            entries += [(f"{var}#{i}", key) for i, key in enumerate(
                (k for k in re.split(r"[,\s]+", os.environ[var]) if k), 1
            )]
        elif var == f"{prefix}_API_KEY" or re.fullmatch(rf"{prefix}_API_KEY_\d+", var):
            key = os.environ[var].strip()
            if key:
                entries.append((var, key))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for label, key in entries:
        if key not in seen:
            seen.add(key)
            unique.append((label, key))
    return unique


def env_keys(prefix: str) -> list[str]:
    """All keys for a provider: PREFIX_API_KEYS (comma separated) plus numbered variables."""
    return [key for _label, key in env_key_entries(prefix)]


class ConfigError(Exception):
    """config.json can't be read (for example a missing comma). The message says where."""


def load_config_strict() -> dict:
    load_env()
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config.json has a mistake near line {exc.lineno} ({exc.msg})") from None
        except OSError as exc:
            raise ConfigError(f"couldn't read config.json ({exc})") from None
        cfg = _merge(cfg, user)
    return cfg


def save_user_setting(path: str, value) -> None:
    """Changes one setting (like "awareness.camera_enabled") in config.json, keeping everything else.
    Refuses to touch a file that has a typo in it, and writes atomically."""
    raw: dict = {}
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config.json has a mistake near line {exc.lineno}, so I didn't change it") from None
        try:
            shutil_copy(CONFIG_PATH, CONFIG_PATH.with_suffix(".json.bak"))
        except OSError:
            pass
    node = raw
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def load_config() -> dict:
    load_env()
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            cfg = _merge(cfg, user)
        except (OSError, json.JSONDecodeError) as exc:
            logging.getLogger("config").warning(
                "Could not read config.json (%s); using defaults.", exc
            )
    return cfg


def get_token() -> str:
    """Random secret shared by the brain and the face (stored in data/token.txt).

    The brain refuses connections that don't present it, so a web page open in
    your browser cannot talk to the agent.
    """
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / "token.txt"
    try:
        with open(path, "x", encoding="utf-8") as fh:
            fh.write(secrets.token_urlsafe(24))
    except FileExistsError:
        pass
    for _ in range(20):
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
        time.sleep(0.05)
    raise RuntimeError("data/token.txt is empty - delete it and start again.")


def setup_logging(name: str, level: int = logging.INFO) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"{name}.log", maxBytes=512_000, backupCount=2, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
