"""Nova's tools inside Forge, code mode, and Gemini 3 thought signatures."""
import json
import os
import pathlib
import sys
import tempfile

os.environ["FORGE_NOVA_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from forgecli import providers  # noqa: E402
from forgecli.tools import SCHEMAS, Tools  # noqa: E402
from forgecli.ui import UI  # noqa: E402


def main():
    t = Tools(UI(), "powershell")
    names = t.nova.names()
    assert {"click", "describe_screen", "set_reminder", "run_tests", "browser_open"} <= set(names), t.nova._error
    assert not {"read_file", "run_shell", "web_search"} & set(names)       # Forge's own tools win

    # one schema per name (Gemini rejects duplicates), dev helpers offered for coding requests
    for q in ("fix the failing test", "take a screenshot and click login", ""):
        all_names = [s["function"]["name"] for s in SCHEMAS + t.extra_schemas(q)]
        assert len(all_names) == len(set(all_names)), q
    assert "run_tests" in [s["function"]["name"] for s in t.extra_schemas("fix the failing test")]
    assert "click" in [s["function"]["name"] for s in t.extra_schemas("click the login button")]
    assert "It's" in t.execute("get_datetime", "{}")
    print("ok  Nova tools load, never shadow Forge's, one schema per name")

    class Agent:
        def _execute(self, name, raw):
            return t.execute(name, raw)

    a = Agent()
    out = t.nova.run_code(a, 'x = tools.get_datetime()\nprint(len(x) > 5)\nprint(os.getcwd())', SCHEMAS)
    assert "True" in out and str(t.root) in out, out
    assert "42" in t.nova.run_code(a, "print(len(x) * 0 + 42)", SCHEMAS)          # variables persist
    assert t.nova.run_code(a, "import subprocess", SCHEMAS).startswith("Error in run_code")
    t.nova.reset_code(a)
    assert "NameError" in t.nova.run_code(a, "print(x)", SCHEMAS)
    assert "- click(" in t.nova.code_prompt(SCHEMAS) and "- read_file(" in t.nova.code_prompt(SCHEMAS)
    print("ok  run_code chains Forge and Nova tools, keeps variables, blocks imports")

    calls = [{"id": "a1", "type": "function", "function": {"name": "f", "arguments": "{}"},
              "extra_content": {"google": {"thought_signature": "SIG"}}},
             {"id": "a2", "type": "function", "function": {"name": "g", "arguments": "{}"}}]
    msgs = [{"role": "assistant", "content": "", "tool_calls": calls}]
    g = providers.portable(msgs, gemini=True)[0]["tool_calls"]
    assert g[0]["extra_content"]["google"]["thought_signature"] == "SIG"
    assert g[1]["extra_content"] == providers._NO_SIGNATURE
    assert "extra_content" not in json.dumps(providers.portable(msgs))
    assert providers._norm_tool_calls(calls)[0]["extra_content"]["google"]["thought_signature"] == "SIG"
    print("ok  Gemini 3 thought signatures go back to Gemini only")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
