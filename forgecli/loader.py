"""The loading screen shown while Forge starts: FORGE in block letters with a pink → violet → cyan
gradient and a light sweeping across it, and a gradient progress bar under it. It runs on its own
thread while settings, keys, providers and skills load, and is wiped before the start screen.

Plain ANSI only (no rich), so it can appear before anything heavy is imported. Off in pipes, with
NO_COLOR or FORGE_LOADER=off."""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time

GLYPHS = {
    "F": ["█████", "█    ", "████ ", "█    ", "█    "],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "G": [" ████", "█    ", "█  ██", "█   █", " ███ "],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
}
STOPS = [(255, 79, 216), (190, 110, 255), (120, 150, 255), (62, 240, 255)]
STEPS = ["reading settings", "unlocking keys", "lining up models", "loading skills", "warming up"]


def _ramp(t: float):
    n = len(STOPS) - 1
    t = min(max(t, 0.0), 1.0) * n
    i = min(n - 1, int(t))
    f = t - i
    a, b = STOPS[i], STOPS[i + 1]
    return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))


def _vt_on() -> None:
    """Turn on ANSI processing in a classic Windows console (Windows Terminal has it already)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def frame(t: float, cols: int, rows: int) -> str:
    """One frame at time t (seconds), centred in a cols x rows window. Every row is placed with
    an absolute cursor move, so the frame never depends on how the terminal treats newlines."""
    word = "FORGE"
    lines = ["".join(GLYPHS[c][r] + " " for c in word).rstrip() for r in range(5)]
    w = len(lines[0])
    sweep = (t * 0.9) % 1.6 - 0.3                           # a highlight moving left to right
    y0 = max(1, (rows - 9) // 2)
    out = []

    def put(y: int, text: str, cells: int) -> None:
        out.append(f"[{y};1H[2K[{max(1, (cols - cells) // 2 + 1)}G{text}[0m")

    for r, ln in enumerate(lines):
        row = []
        for x, ch in enumerate(ln):
            if ch == " ":
                row.append(" ")
                continue
            p = x / max(1, w - 1)
            c = _ramp(p)
            glow = max(0.0, 1 - abs(p - sweep) / 0.12)
            c = tuple(int(v + (255 - v) * 0.6 * glow) for v in c)
            row.append(f"[38;2;{c[0]};{c[1]};{c[2]}m{ch}")
        put(y0 + r, "".join(row), w)
    sub = "B U I L D  ·  D E P L O Y  ·  S C A L E" if cols >= 44 else "BUILD · DEPLOY · SCALE"
    put(y0 + 6, f"[38;2;201;182;255m{sub}", len(sub))
    bw = min(40, max(10, cols - 10))
    head = (t * 18) % (bw + 12) - 6                         # a bright head running along the bar
    bar = []
    for i in range(bw):
        c = _ramp(i / max(1, bw - 1))
        d = abs(i - head)
        k = 0.25 + 0.75 * max(0.0, 1 - d / 6)
        c = tuple(int(v * k) for v in c)
        bar.append(f"[38;2;{c[0]};{c[1]};{c[2]}m" + ("━" if d < 6 else "─"))
    put(y0 + 8, "".join(bar), bw)
    step = STEPS[min(len(STEPS) - 1, int(t / 0.6))]
    msg = step + "." * (1 + int(t * 3) % 3)
    put(y0 + 9, f"[38;2;140;120;180m{msg:<{len(step) + 3}}", len(step) + 3)
    return "".join(out)


class Loader:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> "Loader":
        if not sys.stdout.isatty() or os.environ.get("NO_COLOR") or \
                os.environ.get("FORGE_LOADER", "on").lower() in ("off", "0", "no", "false"):
            return self
        _vt_on()
        try:
            sys.stdout.write("\x1b[?25l\x1b[2J\x1b[3J\x1b[H")   # hide the cursor, clean page
            sys.stdout.flush()
        except Exception:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        t0 = time.time()
        while not self._stop.is_set():
            try:
                size = shutil.get_terminal_size((100, 30))
                sys.stdout.write(frame(time.time() - t0, size.columns, size.lines))
                sys.stdout.flush()
            except Exception:
                return
            self._stop.wait(1 / 24)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=0.5)
        self._thread = None
        try:
            sys.stdout.write("\x1b[0m\x1b[2J\x1b[3J\x1b[H\x1b[?25h")
            sys.stdout.flush()
        except Exception:
            pass


_active: Loader | None = None


def start() -> None:
    global _active
    if _active is None:
        _active = Loader().start()


def stop() -> None:
    global _active
    if _active is not None:
        _active.stop()
        _active = None
