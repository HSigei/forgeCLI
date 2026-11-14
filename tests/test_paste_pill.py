"""Phase 4: long pastes become a pill; the full text goes out when the message is sent.

Run:  python tests/test_paste_pill.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.buffer import Buffer  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.key_binding import KeyBindings  # noqa: E402
from prompt_toolkit.keys import Keys  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from forgecli.cli import (PasteStore, _delete_pill, _insert_paste, _paste_clipboard,  # noqa: E402
                          _pill_fragments)

ten = "\n".join(f"line {i}" for i in range(10))          # exactly 10 lines: stays as text
eleven = "\n".join(f"line {i}" for i in range(11))       # 11 lines: becomes a pill
big = "\n".join(f"row {i} some content" for i in range(42)) + "\n"

st = PasteStore()
assert not st.wants_pill(ten) and st.wants_pill(eleven)
assert not st.wants_pill("one\ntwo\nthree")

# short paste goes in untouched
b = Buffer(); _insert_paste(b, ten, st); assert b.text == ten

# long paste -> one pill with the line and character counts, no text in the box
b = Buffer(); _insert_paste(b, big, st)
assert b.text.startswith("[Pasted text #1 · 42 lines · ") and b.text.endswith(" chars]"), b.text
assert "row 5" not in b.text and "\n" not in b.text
assert f"{len(big.rstrip(chr(10))):,} chars" in b.text

# typing around the pill, then sending: the pill expands back to exactly what was pasted
b.insert_text(" explain this")
assert st.expand(b.text) == big + " explain this"
# a pill the store does not know (an old session's, or typed by hand) stays plain text
assert st.expand("[Pasted text #99 · 1 lines · 5 chars]") == "[Pasted text #99 · 1 lines · 5 chars]"
# two pastes keep separate numbers and contents
second = "\n".join(f"other {i}" for i in range(15))
b2 = Buffer(); _insert_paste(b2, big, st); _insert_paste(b2, second, st)
assert "#2" in b2.text and "#3" in b2.text
assert st.expand(b2.text) == big + second

# windows line endings and surrogate halves are cleaned before counting
crlf = "\r\n".join(f"l{i} \ud83d\ude00" for i in range(12))
b3 = Buffer(); _insert_paste(b3, crlf, st)
assert "12 lines" in b3.text and "\r" not in st.expand(b3.text)
st.expand(b3.text).encode("utf-8")

# backspace removes the whole pill in one press; delete does too from the left of it
b = Buffer(); _insert_paste(b, big, st); b.insert_text("x")
assert not _delete_pill(b, backward=True)                 # "x" is last, not a pill
b.delete_before_cursor(1)
assert _delete_pill(b, backward=True) and b.text == ""
b = Buffer(); b.insert_text("hi "); _insert_paste(b, big, st); b.cursor_position = 3
assert _delete_pill(b, backward=False) and b.text == "hi "
assert not _delete_pill(Buffer(), backward=True)
# an unrelated bracket is never treated as a pill
b = Buffer(); b.insert_text("[not a pill]")
assert not _delete_pill(b, backward=True) and b.text == "[not a pill]"

# highlighted chip in the box
frags = _pill_fragments("see " + "[Pasted text #1 · 42 lines · 800 chars]" + " ok")
assert frags == [("", "see "), ("class:pill", "[Pasted text #1 · 42 lines · 800 chars]"), ("", " ok")], frags
assert _pill_fragments("plain") == [("", "plain")]

# clipboard paste (alt+v / ctrl+v) makes a pill too, and without a store behaves as before
class Ev:
    def __init__(self): self.current_buffer = Buffer()
ev = Ev(); _paste_clipboard(ev, lambda: big, lambda: [], lambda: "", store=PasteStore())
assert ev.current_buffer.text.startswith("[Pasted text #1")
ev = Ev(); _paste_clipboard(ev, lambda: big, lambda: [], lambda: "")
assert ev.current_buffer.text == big.replace("\r\n", "\n")

# end to end in a real prompt session: bracketed paste -> pill -> type -> backspace -> enter
store = PasteStore()
kb = KeyBindings()
seen = []

@kb.add(Keys.BracketedPaste)
def _(event): _insert_paste(event.current_buffer, event.data, store)

@kb.add("backspace")
def _(event):
    if not _delete_pill(event.current_buffer, True):
        event.current_buffer.delete_before_cursor(event.arg)

@kb.add("enter")
def _(event):
    seen.append(event.current_buffer.text)
    event.current_buffer.validate_and_handle()

with create_pipe_input() as inp:
    inp.send_text("\x1b[200~" + big + "\x1b[201~" + " fix it\r")
    sess = PromptSession(input=inp, output=DummyOutput(), key_bindings=kb, multiline=True)
    out = store.expand(sess.prompt())
assert seen[0].startswith("[Pasted text #1 · 42 lines") and seen[0].endswith(" fix it"), seen
assert out == big + " fix it", repr(out[-40:])

store2 = PasteStore(); seen.clear(); store = store2
with create_pipe_input() as inp:
    inp.send_text("\x1b[200~" + big + "\x1b[201~" + "\x7f" + "ok\r")
    sess = PromptSession(input=inp, output=DummyOutput(), key_bindings=kb, multiline=True)
    out = store2.expand(sess.prompt())
assert seen[0] == "ok" and out == "ok", (seen, out)       # one backspace removed the whole pill
print("paste pill: all checks passed")
