"""What a session's tokens would cost. Most of Forge's providers are free tiers (cost $0); for paid
ones this is an estimate from public list prices per million tokens, which change - put your own in
~/.forge/prices.json as {"provider:model" or "model": [input_per_million, output_per_million]}.
Cached-input discounts are not counted, so real bills can be lower."""
from __future__ import annotations

import json
import os
import pathlib

# (input $, output $) per 1M tokens. Matched against "provider:model", then the model name, then a
# substring of the model name. Unknown paid models show "price unknown" rather than a guess.
LIST = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "claude-haiku": (1.00, 5.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-opus": (15.00, 75.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}
# Providers whose keys in Forge are free tiers: no money changes hands.
FREE_PROVIDERS = {"groq", "cerebras", "nvidia", "llm7", "ovh", "kilo", "github", "cloudflare", "sambanova",
                  "mistral", "cohere", "chutes", "pollinations"}


def _user_prices() -> dict:
    p = pathlib.Path(os.environ.get("FORGE_HOME", pathlib.Path.home() / ".forge")) / "prices.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k).lower(): (float(v[0]), float(v[1])) for k, v in data.items()}
    except (OSError, ValueError, TypeError, IndexError, AttributeError):
        return {}


def price(provider: str, model: str) -> tuple[float, float] | None:
    """(input, output) dollars per million tokens; (0, 0) for free; None when unknown."""
    prov, mod = (provider or "").lower(), (model or "").lower()
    user = _user_prices()
    for key in (f"{prov}:{mod}", mod):
        if key in user:
            return user[key]
    if prov in FREE_PROVIDERS or mod.endswith(":free") or "/free" in mod or mod == "free":
        return (0.0, 0.0)
    if prov == "openrouter" and mod.endswith(":free"):
        return (0.0, 0.0)
    for name in sorted(LIST, key=len, reverse=True):
        if name in mod:
            return LIST[name]
    return None


def summary(by_model: dict) -> tuple[list[str], float, bool]:
    """Lines for /cost, the total in dollars, and whether any model's price was unknown."""
    lines, total, unknown = [], 0.0, False
    for key, (tin, tout) in sorted(by_model.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
        prov, _, mod = key.partition(":")
        p = price(prov, mod)
        if p is None:
            unknown = True
            cost = "price unknown"
        else:
            dollars = (tin * p[0] + tout * p[1]) / 1_000_000
            total += dollars
            cost = "free" if p == (0.0, 0.0) else f"${dollars:,.4f}"
        lines.append(f"{prov} · {mod}: {tin:,} in / {tout:,} out — {cost}")
    return lines, total, unknown
