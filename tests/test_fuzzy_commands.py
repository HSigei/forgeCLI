"""Phase 6: a mistyped slash command runs what was meant, or suggests it.

Run:  python tests/test_fuzzy_commands.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from forgecli.cli import ALIASES, COMMANDS, App, closest_word, suggest_command  # noqa: E402

builtin = [*COMMANDS, *ALIASES]
others = ["/frontend-design", "/deploy"]


def sug(word, extra=others):
    return suggest_command(word, builtin, extra)


# the case from the screenshot
assert sug("/mode") == {"run": "/model", "how": "prefix"}, sug("/mode")
# prefixes pick the shortest command they start
assert sug("/mod")["run"] == "/model"
assert sug("/prov")["run"] == "/provider"
assert sug("/stat")["run"] == "/status"
assert sug("/bal")["run"] == "/balance"
# typos
for typo, want in [("/modle", "/model"), ("/halp", "/help"), ("/stauts", "/status"),
                   ("/provder", "/provider"), ("/balence", "/balance"), ("/sesions", "/sessions"),
                   ("/aprove", "/approve"), ("/modells", "/models")]:
    got = sug(typo)
    assert got.get("run") == want, (typo, got)
# risky commands are only ever suggested, never run on a guess
for typo, want in [("/exot", "/exit"), ("/ex", "/exit"), ("/delet", "/delete"), ("/undoo", "/undo"),
                   ("/updat", "/update"), ("/ne", "/new")]:
    got = sug(typo)
    assert "run" not in got and want in got.get("ask", []), (typo, got)
# skills and project commands are suggested, not run
got = sug("/frontend-desing")
assert "run" not in got and "/frontend-design" in got["ask"], got
got = sug("/deplyo")
assert "run" not in got and "/deploy" in got["ask"], got
# too short or nothing close: no guessing
assert "run" not in sug("/x")
assert sug("/zzzzqq") == {}
# an ambiguous prefix asks instead of picking
got = sug("/se")                         # /sessions and /setup
assert "run" not in got
got = sug("/de")                         # /delete, /deploy, /doctor... all plausible
assert "run" not in got

# subcommand words
assert closest_word("autoo", ["auto", "groq", "openai"]) == "auto"
assert closest_word("opnai", ["auto", "groq", "openai"]) == "openai"
assert closest_word("gr", ["auto", "groq", "openai"]) == "groq"
assert closest_word("zzz", ["auto", "groq", "openai"]) == ""
assert closest_word("o", ["auto", "groq", "openai"]) == ""        # one letter is not enough


# through the real dispatcher: the message shown, the command run, the arguments kept
class UI:
    def __init__(self): self.out = []
    def info(self, m): self.out.append(("info", m))
    def error(self, m): self.out.append(("error", m))
    def ok(self, m): self.out.append(("ok", m))
    warn = ok


app = App.__new__(App)
app.ui = UI()
app.agent = type("A", (), {"skills": {"frontend-design": object()}})()
ran = []
app.cmd_model = lambda arg: ran.append(("model", arg))
app.cmd_help = lambda arg: ran.append(("help", arg))
app.cmd_delete = lambda arg: ran.append(("delete", arg))

assert app.handle("/mode deepseek-chat") is True
assert ran == [("model", "deepseek-chat")], ran
assert ("info", "assuming /model — you typed /mode") in app.ui.out, app.ui.out
app.ui.out.clear(); ran.clear()
app.handle("/halp")
assert ran == [("help", "")]
app.ui.out.clear(); ran.clear()
app.handle("/delet 3")                       # risky: must not run
assert ran == [] and app.ui.out[-1][0] == "error" and "/delete" in app.ui.out[-1][1], app.ui.out
app.ui.out.clear()
app.handle("/qqqqqq")
assert app.ui.out[-1] == ("error", "unknown command /qqqqqq — /help lists them"), app.ui.out
print("fuzzy commands: all checks passed")
