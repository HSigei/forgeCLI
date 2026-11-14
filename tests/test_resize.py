"""Phase 3: zooming the terminal must not leave the old box behind.

Run:  python tests/test_resize.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.data_structures import Point, Size  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from forgecli.cli import _erase_for_resize, _install_resize_fix, _resize_extra_rows  # noqa: E402

# narrower window: each full-width row re-wraps into ceil(old/new) rows
assert _resize_extra_rows(120, 60, 3) == 3          # 2 rows each -> 1 extra per row
assert _resize_extra_rows(120, 50, 4) == 8          # 3 rows each -> 2 extra per row
assert _resize_extra_rows(120, 119, 2) == 2         # one column less still wraps once
# wider, same width, or nothing above the input: nothing extra
assert _resize_extra_rows(60, 120, 3) == 0
assert _resize_extra_rows(80, 80, 3) == 0
assert _resize_extra_rows(120, 60, 0) == 0


class FakeOut:
    def __init__(self, cols):
        self.cols, self.log = cols, []

    def get_size(self):
        return Size(rows=30, columns=self.cols)

    def write_raw(self, d): self.log.append(("raw", d))
    def cursor_up(self, n): self.log.append(("up", n))
    def erase_down(self): self.log.append(("erase_down",))
    def reset_attributes(self): self.log.append(("reset",))
    def enable_autowrap(self): self.log.append(("autowrap",))
    def flush(self): self.log.append(("flush",))


class FakeRenderer:
    def __init__(self, out, y):
        self.output, self._cursor_pos, self.resets = out, Point(x=7, y=y), 0

    def reset(self, leave_alternate_screen=True):
        self.resets += 1
        self._cursor_pos = Point(x=0, y=0)


# zoomed in 120 -> 60 with the cursor 4 rows down and 3 full-width rows above the input line:
# climb 4 + 3 extra rows, then erase everything below
r = FakeRenderer(FakeOut(60), y=4)
_erase_for_resize(r, 120, 3)
assert ("up", 7) in r.output.log and ("erase_down",) in r.output.log, r.output.log
assert r.output.log.index(("up", 7)) < r.output.log.index(("erase_down",))
assert r.resets == 1 and r._cursor_pos.y == 0

# zoomed out: plain climb, nothing extra
r = FakeRenderer(FakeOut(200), y=4)
_erase_for_resize(r, 120, 3)
assert ("up", 4) in r.output.log, r.output.log


# the installed handler: erases only when a box is on screen, once per draw, then redraws
class FakeApp:
    def __init__(self, renderer):
        self.renderer, self.calls = renderer, []
        self._on_resize = lambda: self.calls.append("original")

    def _request_absolute_cursor_position(self): self.calls.append("cpr")
    def _redraw(self): self.calls.append("redraw")


state = {"cols": 120, "above": 3, "drawn": True}
fa = FakeApp(FakeRenderer(FakeOut(60), y=4))
_install_resize_fix(fa, state)
fa._on_resize()
assert fa.calls == ["cpr", "redraw"] and ("up", 7) in fa.renderer.output.log
assert state["drawn"] is False
fa.renderer.output.log.clear()
fa._on_resize()                                      # second resize before the redraw happened
assert not any(e[0] == "up" for e in fa.renderer.output.log), "must not climb into the scrollback"

# a failure inside falls back to prompt_toolkit's own handler
bad = FakeApp(object())
_install_resize_fix(bad, {"drawn": True, "cols": 100, "above": 2})
bad._on_resize()
assert "original" in bad.calls

# and the real PromptSession still starts, takes input and returns with the handler installed
with create_pipe_input() as inp:
    inp.send_text("ok\r")
    sess = PromptSession(input=inp, output=DummyOutput())
    _install_resize_fix(sess.app, {"drawn": False})
    assert sess.prompt() == "ok"
print("resize: all checks passed")
