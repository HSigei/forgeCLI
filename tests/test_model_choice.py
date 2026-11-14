"""Phase 5: /model picks the provider from the model name and then uses exactly that.

Run:  python tests/test_model_choice.py
"""
import pathlib
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from forgecli.cli import resolve_model_choice as pick  # noqa: E402
from forgecli.providers import Router  # noqa: E402


def P(name, model, models=()):
    return NS(name=name, model=model, models=list(models))


ovh = P("ovh", "gpt-oss-120b", ["Meta-Llama-3_3-70B-Instruct"])
openai = P("openai", "gpt-4o-mini", ["gpt-4o"])
deepseek = P("deepseek", "deepseek-chat", ["deepseek-reasoner"])
kilo = P("kilo", "gpt-oss-120b")
provs = [ovh, openai, deepseek, kilo]
cache = {"openrouter": ["qwen/qwen3-coder"]}

# the bug: the model name decides the provider, not whoever happens to be first
r = pick("deepseek-chat", provs, cache, active="ovh")
assert r["provider"] is deepseek and r["model"] == "deepseek-chat", r
r = pick("deepseek-reasoner", provs, cache, active="ovh")
assert r["provider"] is deepseek and r["model"] == "deepseek-reasoner"
r = pick("gpt-4o", provs, cache, active="ovh")
assert r["provider"] is openai and r["model"] == "gpt-4o"

# naming only a provider switches to it, on its current model
r = pick("deepseek", provs, cache, active="ovh")
assert r["provider"] is deepseek and r["model"] == "deepseek-chat" and r.get("switch_only")

# explicit provider + model, in both spellings
r = pick("deepseek deepseek-reasoner", provs, cache, active="ovh")
assert r["provider"] is deepseek and r["model"] == "deepseek-reasoner"
r = pick("deepseek:deepseek-reasoner", provs, cache, active="ovh")
assert r["provider"] is deepseek and r["model"] == "deepseek-reasoner"

# the same id on two providers: the one in use wins, else the first configured
assert pick("gpt-oss-120b", provs, cache, active="kilo")["provider"] is kilo
assert pick("gpt-oss-120b", provs, cache, active="deepseek")["provider"] is ovh

# a partial or misspelled name finds the one model it means, and says so
r = pick("reasoner", provs, cache, active="ovh")
assert r["provider"] is deepseek and r["model"] == "deepseek-reasoner" and "matched" in r["note"]
r = pick("deepseek-chta", provs, cache, active="ovh")
assert r["provider"] is deepseek and r["model"] == "deepseek-chat", r
r = pick("deepseek reasoner", provs, cache, active="ovh")          # provider + partial model
assert r["provider"] is deepseek and r["model"] == "deepseek-reasoner"

# ids seen with /models count too
r = pick("qwen3-coder", provs + [P("openrouter", "x")], cache, active="ovh")
assert r["provider"].name == "openrouter" and r["model"] == "qwen/qwen3-coder"

# several matches: ask, don't guess
r = pick("gpt", provs, cache, active="ovh")
assert "choices" in r and ("openai", "gpt-4o") in r["choices"] and "provider" not in r, r

# unlisted id: a provider named in it owns it, else the active provider keeps it
r = pick("deepseek-v9-preview", provs, cache, active="ovh")
assert r["provider"] is deepseek and r["model"] == "deepseek-v9-preview" and r["unlisted"]
r = pick("totally-new-model", provs, cache, active="openai")
assert r["provider"] is openai and r["unlisted"]
assert "error" in pick("", provs, cache)


# router: a chosen model means that model only, with no silent alternates or team overrides
def router(strict, pinned):
    rt = Router.__new__(Router)
    rt.strict, rt.pinned, rt.usage = strict, pinned, None
    return rt

assert router(True, "deepseek")._models_of(deepseek) == ["deepseek-chat"]
assert router(False, "deepseek")._models_of(deepseek) == ["deepseek-chat", "deepseek-reasoner"]
assert router(True, "ovh")._models_of(deepseek) == ["deepseek-chat", "deepseek-reasoner"]  # not the pinned one
print("model choice: all checks passed")
