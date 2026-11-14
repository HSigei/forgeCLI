"""Phase 2: enter on an empty box sends nothing and draws nothing new.

Run:  python tests/test_enter.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.key_binding import KeyBindings  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from forgecli.cli import _on_enter  # noqa: E402


def run(keys: str, has_question: bool, multiline=True):
    """Feed keys to a real prompt_toolkit session wired like Forge's box; return (result, submits)."""
    kb = KeyBindings()
    log = []

    @kb.add("enter")
    def _(event):
        log.append(_on_enter(event.current_buffer, has_question))

    with create_pipe_input() as inp:
        inp.send_text(keys)
        sess = PromptSession(input=inp, output=DummyOutput(), key_bindings=kb, multiline=multiline)
        return sess.prompt(), log


# five empty enters then a real message: only the real one is ever submitted
text, log = run("\r\r\r\r\rhello\r", has_question=False)
assert text == "hello", text
assert log.count("ignored") == 5 and log[-1] == "submitted", log

# spaces-only enters are ignored too, and the stray spaces are cleared
text, log = run("   \r  \rhi there\r", has_question=False)
assert text == "hi there", repr(text)
assert log[:2] == ["ignored", "ignored"], log

# a question is waiting: an empty enter is an answer and must go through
text, log = run("\r", has_question=True)
assert text == "" and log == ["submitted"], (text, log)

# a trailing backslash still makes a new line instead of sending
text, log = run("one\\\rtwo\r", has_question=False)
assert text == "one\ntwo" and log == ["newline", "submitted"], (text, log)
print("enter: all checks passed")
