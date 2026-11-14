"""Phase 1: pasted text with surrogates must never crash history, sessions or input.

Run:  python tests/test_textsafe.py
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from forgecli import sessions  # noqa: E402
from forgecli.textsafe import clean, clean_deep, has_surrogates  # noqa: E402

pair = "\ud83d\ude00"          # a smiley split into UTF-16 halves, as a Windows paste delivers it
lone = "\ud83d"

assert clean(pair) == "\U0001F600", "a valid pair is joined back into the emoji"
assert clean("a" + lone + "b") == "a\ufffdb", "a lone half becomes U+FFFD"
assert clean("plain ascii é ü 中") == "plain ascii é ü 中"
assert clean("x\x00y") == "xy"
assert clean(5) == 5 and clean(None) is None
assert not has_surrogates(clean("t" + pair + lone + pair))
clean("t" + pair + lone).encode("utf-8")           # must not raise
big = ("line %d " % 1 + pair + "\n") * 3000        # ~17k chars like the screenshot
clean(big).encode("utf-8")

d = clean_deep({"k" + lone: [pair, ("a", lone)], "n": 1})
assert d == {"k\ufffd": ["\U0001F600", ("a", "\ufffd")], "n": 1}

# a session holding surrogates saves without raising
with tempfile.TemporaryDirectory() as tmp:
    sessions.SESSION_DIR = pathlib.Path(tmp)
    s = sessions.Session()
    s.messages = [{"role": "user", "content": "hi " + pair + lone}]
    s.save()
    assert s.path.exists() and "\U0001F600" in s.path.read_text(encoding="utf-8")

# the history writer swallows what it cannot store and cleans what it can
try:
    from prompt_toolkit.history import FileHistory
except ImportError:
    print("prompt_toolkit missing: skipped history check")
else:
    from forgecli import keyvault
    with tempfile.TemporaryDirectory() as tmp:
        class H(FileHistory):
            def append_string(self, string):
                try:
                    super().append_string(clean(keyvault.redact(clean(string))[0]))
                except (UnicodeError, OSError):
                    pass
        h = H(str(pathlib.Path(tmp) / "history"))
        h.append_string("pasted " + pair + lone)
        assert "\U0001F600" in (pathlib.Path(tmp) / "history").read_text(encoding="utf-8")
print("textsafe: all checks passed")
