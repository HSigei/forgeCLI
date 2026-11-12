"""Terminal rendering with rich: logo, actions, coloured diffs, live status, tables."""
from __future__ import annotations

import difflib
import os
import pathlib
import re
import sys
import threading
import time

from . import courier
from . import courier_hd

if os.name == "nt":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# Neon night palette (shared with art.py / city.py): one ground (the terminal's own dark), one ink,
# one accent (magenta neon) and cyan for anything live; amber for warnings, lime for done.
ACCENT = "#ff4fd8"
EMBER = "#3ef0ff"
THEME = Theme({
    "accent": f"bold {ACCENT}",
    "accent.dim": "#9b4fc8",
    "ember": f"bold {EMBER}",
    "muted": "#a096d2",
    "faint": "#6b6290",
    "tool": "bold #ece4ff",
    "tool.write": "bold #5bff9d",
    "tool.shell": "bold #ffc857",
    "tool.web": "bold #3ef0ff",
    "target": "#d8d0f5",
    "tree": "#4a3a72",
    "ok": "#4ade80",
    "warn": "#fbbf24",
    "err": "bold #f87171",
    "user": "bold #e2e8f0",
    "add": "#d1fae5 on #14532d",
    "del": "#fee2e2 on #7f1d1d",
    "lineno": "grey46",
    "markdown.code": "#fda4af",
    "markdown.h1": f"bold {ACCENT}",
    "markdown.h2": f"bold {ACCENT}",
    "markdown.link": "#93c5fd",
})

console = Console(theme=THEME, highlight=False, soft_wrap=False)

# How each tool is shown, Claude Code style: ● Verb(target)
VERBS = {"write_file": "Write", "edit_file": "Update", "run_shell": "Shell", "open_url": "Open",
         "process": "Process", "use_skill": "Skill", "delegate": "Delegate", "run_parallel": "Parallel"}
PEEK = {"read": ("read", "file", "files"), "search": ("searched", "pattern", "patterns"),
        "list": ("listed", "folder", "folders"), "fetch": ("fetched", "page", "pages")}
LEXERS = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript", ".tsx": "tsx",
          ".jsx": "jsx", ".html": "html", ".css": "css", ".json": "json", ".md": "markdown", ".sh": "bash",
          ".ps1": "powershell", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".sql": "sql", ".go": "go",
          ".rs": "rust", ".java": "java", ".c": "c", ".cpp": "cpp", ".cs": "csharp", ".php": "php", ".rb": "ruby"}

PREVIEW_LINES = 10   # lines of a new file shown on screen (display only, costs no tokens)
DIFF_LINES = 40      # lines of an edit's diff shown on screen


def _gutter(mark: Text, body) -> Table:
    t = Table.grid(padding=(0, 1))
    t.add_column(width=1, no_wrap=True)
    t.add_column(ratio=1)
    t.add_row(mark, body)
    return t


def short(path: str) -> str:
    """Path relative to the current folder when inside it."""
    try:
        rel = os.path.relpath(path)
        return path if rel.startswith("..") else rel
    except ValueError:
        return path


def _brief(target: str) -> str:
    """A short readable name for what was looked at: a file's name, a pattern, a host."""
    t = str(target).strip()
    m = re.match(r"https?://([^/]+)(/[^?#]*)?", t)
    if m:
        tail = (m.group(2) or "").rstrip("/").rsplit("/", 1)[-1]
        return (m.group(1) + ("/…/" + tail if tail else ""))[:48]
    if " in " in t:                       # "pattern in C:\some\folder"
        pat, _, where = t.rpartition(" in ")
        return f"{pat[:30]} in {pathlib.PurePath(where).name or where}"[:48]
    p = pathlib.PurePath(t)
    return (p.name or t)[:48] if ("\\" in t or "/" in t) else t[:48]


def ktok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# --- logo ----------------------------------------------------------------------------

STREAM_LINES = 8   # how much of a reply-in-progress to show
PURPLES = ["#7b2cff", "#ff4fd8", "#e6fdff"]   # violet → neon magenta, plus a cyan-white shimmer


def _blend(a: str, b: str, t: float) -> str:
    x = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    y = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(p + (q - p) * t):02x}" for p, q in zip(x, y))


# Forge's mascot: a small red tiger head, drawn as pixel art. Each character cell shows two
# pixels stacked (the upper half block takes the top colour, its background the bottom one).
_TIGER_HALF = [
    ".KK.....",
    "KRWK.KKK",
    "KRRKKRRR",
    "KRRRRRKR",
    "KRKRRRRR",
    "KRWWKRRR",
    "KRWYKKRR",
    ".KRRRRRW",
    "KRKRRRWW",
    "KWRRRWWK",
    ".KWWWWKP",
    "..KWWWKM",
    "...KWWWK",
    "....KKKK",
]
TIGER = [row + row[::-1] for row in _TIGER_HALF]          # 16 x 14 pixels, mirrored
TIGER_COLORS = {"K": "#14100f", "R": "#ef2b2d", "W": "#f7f3ee", "Y": "#f5b301",
                "P": "#f472b6", "M": "#7f1d1d", "T": "#ffffff"}

# The name in 5x7 pixel letters, each letter a step along a red -> amber gradient.
_FONT = {
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "G": [".####", "#....", "#....", "#.###", "#...#", "#...#", ".###."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
}


def wordmark(word: str = "FORGE", a: str = "#ef2b2d", b: str = "#f5b301") -> list[Text]:
    keys = "abcdefghij"
    colors = {keys[i]: _blend(a, b, i / max(1, len(word) - 1)) for i in range(len(word))}
    rows = ["." .join(_FONT[ch][r].replace("#", keys[i]) for i, ch in enumerate(word)) for r in range(7)]
    return pixels(rows, colors)


def pixels(grid: list[str], colors: dict[str, str], scale: int = 1) -> list[Text]:
    """Render a pixel grid ('.' = transparent) as lines of half-block characters."""
    grid = [("".join(c * scale for c in row)) for row in grid for _ in range(scale)]
    if len(grid) % 2:
        grid.append("." * len(grid[0]))
    out = []
    for top, bottom in zip(grid[0::2], grid[1::2]):
        line = Text()
        for a, b in zip(top, bottom):
            if a == "." and b == ".":
                line.append(" ")
            elif b == ".":
                line.append("▀", style=colors[a])
            elif a == ".":
                line.append("▄", style=colors[b])
            else:
                line.append("▀", style=f"{colors[a]} on {colors[b]}")
        out.append(line)
    return out


def tiger(scale: int = 1) -> list[Text]:
    return pixels(TIGER, TIGER_COLORS, scale)


def _gradient(word: str, a: str = EMBER, b: str = ACCENT) -> Text:
    t = Text()
    for i, ch in enumerate(word):
        t.append(ch, style=f"bold {_blend(a, b, i / max(1, len(word) - 1))}")
    return t


def banner_art(width: int, rows: int = 10) -> Text:
    """The neon city banner (tiger cub, FORGE sign, skyline) as half-block text, rows lines tall."""
    from . import art
    w = max(40, min(width, 120))
    return Text.from_ansi(art.banner(w, rows * 2).ansi().rstrip("\n"))


def banner_sixel(width_cells: int) -> str | None:
    """The same banner as a sixel image (several real pixels per cell) when the terminal can show
    one, else None. Sized to about width_cells columns."""
    from . import art
    if art.graphics(console) != "sixel":
        return None
    try:
        cols = max(40, min(width_cells, 110))
        w = int(cols * 10 / 3)                 # ~10px per cell, 3 image pixels per canvas pixel
        return art.banner(w, int(w * 0.30)).sixel(3)
    except Exception:
        return None


def logo(version: str, tagline: str, info: list[Text] | None = None, width: int | None = None,
         picture: bool = True):
    """The start-up header: the neon city banner, then the version line and the facts. With
    picture=False (a sixel image was already drawn above) only the text part."""
    width = width or console.width
    line = _gradient(f"forge v{version}", ACCENT, EMBER).append("  ").append(tagline, style="muted")
    if width < 60 or not picture:
        head = Group(*wordmark(a=ACCENT, b=EMBER), line) if (width < 60 and picture) else line
        return Group(head, Text(""), *(info or []))
    return Group(banner_art(width - 8), line, Text(""), *(info or []))


def goodbye(lines: list[tuple[str, str]], animate: bool = True) -> None:
    """Clear the terminal and fill it with a goodbye: the mascot waving, centred, plus a few notes.
    lines: (text, style) shown under the mascot."""
    from rich.align import Align
    from . import art as artmod

    from . import scene
    six = None
    pic_cols = min(90, console.width - 2)
    if console.is_terminal and artmod.graphics(console) == "sixel":
        try:
            six = scene.sixel(pic_cols)        # the same picture as the start screen, real pixels
        except Exception:
            six = None
    try:
        cub = Text.from_ansi(scene.halfblock(pic_cols)) if not six else Text("")
    except Exception:
        cub = Text.from_ansi(artmod.mascot(24).ansi().rstrip("\n"))

    def frame(wave: str = "|") -> Group:
        art = cub
        word = _gradient("see you soon", ACCENT, EMBER)
        notes = [Align.center(Text(t, style=s)) for t, s in lines]
        # No screen clear and no full-height padding: the conversation above stays in the
        # scrollback so you can still read and copy it after quitting.
        return Group(Text(""), Align.center(art), Align.center(word), Text(""), *notes)

    if animate and console.is_terminal and os.environ.get("FORGE_BYE_ANIMATION") == "1":
        from rich.live import Live
        try:
            with Live(frame("\\"), console=console, transient=True, auto_refresh=False) as live:
                for wave in ("|", "/", "|", "\\"):
                    time.sleep(0.22)
                    live.update(frame(wave), refresh=True)
                time.sleep(0.22)
        except KeyboardInterrupt:
            pass
    if six:
        pad = max(0, (console.width - scene.pick(pic_cols)[0]) // 2)
        console.print()
        sys.stdout.write(" " * pad + six + "\n")
        sys.stdout.flush()
    console.print(frame("^"))


# --- live status line -----------------------------------------------------------------

class _Live:
    """Re-rendered by rich several times a second: shows what is happening right now."""

    SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, ui: "UI", spinner: bool = False, docked: bool | None = None):
        self.ui = ui
        self.spinner = spinner   # draw the wheelbarrow in front of the status word
        self.docked = spinner if docked is None else docked   # inside the message-box layout

    def __rich__(self):
        u = self.ui
        now = time.time()
        secs = now - u.t0
        # Purple gradient with a soft shimmer that sweeps slowly across the words.
        word = f"{u.shown_activity(now)}…"
        t = Text("  ")
        sprite = None
        u._hd_rows = 0
        if (self.spinner and self.docked and getattr(u, "courier_overlay", None) is not None
                and not getattr(u, "courier_hd_failed", False)):
            try:
                # Real pixels: keep blank rows here; courier_hd.Overlay paints into them.
                rows = courier_hd.pick_rows(console.height, console.width)
                sprite = [Text(" " * courier_hd.lane_cells(rows)) for _ in range(rows)]
                u._hd_rows = rows
            except Exception:
                sprite = None
        if sprite is None and self.spinner and courier.enabled():
            try:
                rows = courier.pick_rows(console.height, console.width)
                sprite = courier.scene(u.barrow, time.monotonic(), rows)
                t = Text("  ")
            except Exception:
                sprite = None
        if self.spinner and sprite is None:
            try:
                for ch, style in u.barrow.pieces(time.monotonic()):
                    t.append(ch, style=style or None)
                t.append("  ")
            except Exception:
                t.append(self.SPIN[int(now * 8) % len(self.SPIN)] + " ", style=f"bold {PURPLES[1]}")
        sweep = (now * 5) % (len(word) + 14) - 7
        for i, ch in enumerate(word):
            base = _blend(ACCENT, EMBER, i / max(1, len(word) - 1))
            glow = max(0.0, 1 - abs(i - sweep) / 5)
            t.append(ch, style=f"bold {_blend(base, PURPLES[2], glow * 0.55)}")
        # A fixed-width slot for the word, so the numbers beside it don't hop sideways.
        t.append(" " * max(1, 16 - len(word)))
        bits = [f"{secs:>3.0f}s"]
        if u.tokens_hint:
            bits.append(u.tokens_hint)
        if u.model:
            bits.append(u.model)
        stop = "esc to stop · type to add to the task" if self.docked else "ctrl+c to stop"
        t.append("  " + " · ".join(bits), style="muted").append("   " + stop, style="faint")
        parts = []
        if u.trail or self.docked:
            # The last few steps, so a fast run of reads is still readable.
            trail = Text()
            steps = u.trail[-TRAIL_LINES:]
            if self.docked:
                steps = [""] * (TRAIL_LINES - len(steps)) + steps
            for i, step in enumerate(steps):
                if not step:
                    trail.append("\n" if i else "")
                    continue
                trail.append(("\n" if i else "") + "    ✓ ", style=f"{_blend(PURPLES[0], PURPLES[1], 0.4)}")
                trail.append(step, style="faint")
            parts.append(trail)
        live = _strip_think(u.streamed)
        width = max(20, console.width - 8)
        if live.strip():
            # The reply as it is being written: the last few lines, under the status line.
            parts.append(Text("\n".join("    " + ln for ln in _pad(_wrap(live, width)[-(THINK_LINES if self.docked else STREAM_LINES):],
                                                       THINK_LINES, self.docked)),
                              style="text"))
        else:
            thought = u.thinking or _think_part(u.streamed)
            if thought.strip():
                # The model's reasoning as it happens. Display only: it is never added to the
                # conversation, so it costs no context tokens.
                lines = [("    ✻ " if i == 0 else "      ") + ln
                         for i, ln in enumerate(_wrap(thought, width)[-THINK_LINES:])]
                parts.append(Text("\n".join(_pad(lines, THINK_LINES, self.docked)), style="faint italic"))
            elif self.docked:
                parts.append(Text("\n" * (THINK_LINES - 1)))
        if not self.docked:
            # Not docked (rich draws this live): the city goes on top here too, if turned on.
            scene = u._scene_ansi(max(20, console.width))
            if scene:
                parts.insert(0, Text.from_ansi(scene.rstrip("\n")))
        # One faint sponsor line under the status, only once a task has run a few seconds.
        tail = []
        sp = getattr(u, "sponsor", None)
        if sp and secs >= SPONSOR_AFTER:
            try:
                line = sp()
            except Exception:
                line = None
            if line:
                tail.append(sponsor_text(line))
        if self.docked and not tail:
            tail.append(Text(""))           # the sponsor's row stays reserved, so the courier never hops
        if sprite:
            # The courier stands on the status line: words sit beside his bottom row.
            lines = [Text("  ") + ln for ln in sprite]
            lines[-1] = lines[-1] + t
            lines[-1].no_wrap, lines[-1].overflow = True, "ellipsis"
            return Group(*parts, *lines, *tail)
        # One row, always: a status line that wraps would add a row the height budget never counted.
        t.no_wrap, t.overflow = True, "ellipsis"
        return Group(*parts, t, *tail) if (parts or tail) else t


SPONSOR_AFTER = 2          # seconds into a task before the sponsor line appears (instant replies never show it)


def sponsor_text(line: str) -> Text:
    """The sponsor line: a small violet "sponsored" tag, the message in soft pink and the link in
    cyan. Easy to spot under the status, never brighter than Forge's own words. One row."""
    body = line.removeprefix("sponsor · ")
    url = ""
    if "  " in body:
        body, url = body.rsplit("  ", 1)
        if not url.startswith(("http://", "https://", "www.")):
            body, url = body + "  " + url, ""
    t = Text("    ")
    t.append(" sponsored ", style="bold #f5d0fe on #4c1d95")
    t.append("  " + body, style="#f9a8d4")
    if url:
        t.append("  " + url, style="underline #67e8f9")
    t.no_wrap, t.overflow = True, "ellipsis"
    return t


THINK_LINES = 4
TRAIL_LINES = 3      # recent steps kept under the status line
HOLD_SECS = 1.2      # each activity stays on screen at least this long, so it can be read


def _pad(lines: list[str], n: int, on: bool) -> list[str]:
    """Top-pad to n lines (when on), so a growing preview doesn't push the screen around."""
    return [""] * (n - len(lines)) + lines if on and len(lines) < n else lines


def _wrap(text: str, width: int) -> list[str]:
    """Hard-wrap by terminal cells (wide characters and emoji count double), so no preview line is
    wider than the window and the terminal never wraps it into an extra, uncounted row."""
    from rich.cells import chop_cells
    lines: list[str] = []
    for para in text.rstrip().expandtabs(4).split("\n"):
        lines += chop_cells(para, width) if para else [""]
    return lines


_TEMPLATE_TOKEN = re.compile(r"<\|?[a-z_]{2,20}\|>")


def _strip_think(text: str) -> str:
    """Reply text without a <think>…</think> block (some models put their reasoning there),
    and without stray chat-template tokens like <channel|> or <|end|> that leak into replies."""
    text = _TEMPLATE_TOKEN.sub("", text)
    if "<think>" not in text:
        return text
    head, _, rest = text.partition("<think>")
    _, closed, tail = rest.partition("</think>")
    return head + tail if closed else head


def _think_part(text: str) -> str:
    if "<think>" not in text:
        return ""
    return text.partition("<think>")[2].partition("</think>")[0]


# Which city activity a tool call means, as soon as the model starts calling it.
_TOOL_STATE = {
    "read_file": "researching", "list_dir": "researching", "glob": "researching", "grep": "researching",
    "web_fetch": "researching", "web_search": "researching", "open_url": "researching",
    "write_file": "coding", "edit_file": "coding", "run_shell": "processing", "process": "processing",
    "delegate": "delegating", "run_parallel": "delegating", "use_skill": "thinking", "todo": "thinking",
}

_ACTION_WORDS = {
    "read_file": "reading a file", "list_dir": "listing a folder", "glob": "finding files",
    "grep": "searching the code", "write_file": "writing a file", "edit_file": "editing a file",
    "run_shell": "running a command", "web_fetch": "fetching a page", "web_search": "searching the web",
    "delegate": "handing work to a helper", "use_skill": "loading a skill", "todo": "updating the plan",
}


class UI:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.console = console
        self._status = None
        self.lock = threading.RLock()
        self.activity = "Thinking"
        self.model = ""
        self.tokens_hint = ""
        self.t0 = time.time()
        self.peeks: dict[str, list[str]] = {}
        self.trail: list[str] = []
        self._shown = ("Thinking", 0.0)
        self.streamed = ""
        self.thinking = ""
        self.think_t0 = 0.0
        self.think_secs = 0.0
        # Docked: the message box stays open while Forge works. The status is then drawn above the
        # box by prompt_toolkit (see status_ansi) instead of by a rich live display, and questions
        # (approvals, "Forge asks") are answered in that same box.
        self.docked = False
        self.busy = False
        self.pending: dict | None = None
        # The pixel city above the message box: tiny workers acting out what Forge is doing.
        # FORGE_SCENE=off turns it off; it also hides itself in short or narrow terminals.
        from .city import City
        self.city = City()
        # The big city is opt-in now (FORGE_SCENE=city); by default a small wheelbarrow in front of
        # the status word acts out the same events.
        self.scene = os.environ.get("FORGE_SCENE", "").lower() in ("city", "on", "1", "yes", "true")
        from .city import Barrow
        self.barrow = Barrow(self.city.motion)

    # the pixel city ---------------------------------------------------------------------
    def act(self, state: str, detail: str = "", wait: float = 0.0) -> None:
        """Tell the city what is really happening (only ever called from real agent events)."""
        try:
            self.city.set(state, detail, wait)
            self.barrow.on(state)
        except Exception:
            pass

    def _scene_ansi(self, width: int) -> str:
        """The city frame, or "" when it shouldn't be shown (off, idle, terminal too small)."""
        c = self.city
        if not self.scene or not console.is_terminal:
            return ""
        if c.settled():
            c.set("idle")
        if c.state == "idle" and not self.busy:
            return ""
        try:
            height = console.size.height
        except Exception:
            height = 40
        if height < 26:
            return ""
        try:
            return c.frame(width)
        except Exception:
            return ""

    # docked mode ------------------------------------------------------------------
    input_rows = 1          # lines in the message box right now (the prompt sets it each redraw)

    def status_budget(self) -> int:
        """Rows the live status may use: the window minus the message box, the completion menu's
        six reserved rows, the box's top edge, the footer and one spare row."""
        try:
            height = console.size.height
        except Exception:
            height = 40
        return max(0, height - self.input_rows - 6 - 1 - 1 - 1)

    def status_ansi(self) -> str:
        """The live status (spinner, activity, reasoning or reply preview) as ANSI text, for the
        area above the message box. Empty when Forge is idle."""
        width = max(20, console.width)
        scene = self._scene_ansi(width)
        budget = self.status_budget()
        if scene.count(chr(10)) > budget:
            scene = ""
        if not self.busy:
            self.hd_lane = None
            return scene
        import io
        c = getattr(self, "_status_console", None)
        if c is None or c.width != width:
            # Reused between redraws: building a Console every frame cost a quarter of a CPU core.
            c = Console(file=io.StringIO(), theme=THEME, force_terminal=True, color_system="truecolor",
                        width=width, highlight=False, soft_wrap=False)
            self._status_console = c
        buf = c.file
        buf.seek(0)
        buf.truncate()
        c.print(_Live(self, spinner=True))
        out = scene + buf.getvalue()
        # Height budget. prompt_toolkit draws this status, the message box and the footer as one
        # block at the bottom of the window. If that block is taller than the window, the box and
        # footer are pushed off screen and later output lands on top of them, so the status never
        # takes more rows than the box, its completion menu and the footer leave free.
        if out.count(chr(10)) > budget and scene:          # the city goes first…
            out = buf.getvalue()
        if out.count(chr(10)) > budget:                    # …then the oldest status rows
            self._hd_rows = 0                              # no room for the pixel courier either
            lines = out.split(chr(10))[:-1] if out.endswith(chr(10)) else out.split(chr(10))
            out = "".join(ln + chr(10) for ln in (lines[-budget:] if budget > 0 else []))
        rows = getattr(self, "_hd_rows", 0)
        # Where the courier's blank rows sit inside the prompt's message (they end the status).
        self.hd_lane = (out.count(chr(10)) - rows, rows, 2) if rows else None
        return out

    def answer(self, text: str) -> bool:
        """The message box was submitted while a question is waiting: deliver it. False = no question."""
        p = self.pending
        if not p:
            return False
        p["answer"] = text
        p["event"].set()
        return True

    def _input(self, markup: str, label: str) -> str:
        """Read one answer: from the open message box when docked, else a normal console prompt."""
        if not self.docked:
            return console.input(markup)
        p = {"label": label, "event": threading.Event(), "answer": ""}
        self.pending = p
        try:
            while not p["event"].wait(0.1):   # short waits so a stop (ctrl+c) gets through
                pass
        finally:
            self.pending = None
        if p.get("stopped"):
            raise KeyboardInterrupt   # a stop is never an answer (an empty answer means "yes")
        return p["answer"]

    def queued(self) -> None:
        """A message typed while Forge works is read by the model at its next step."""
        console.print(Text("  ↳ added to the task — Forge reads it at its next step", style="faint"))

    # live status ------------------------------------------------------------------
    def start_spinner(self, label: str = "Thinking") -> None:
        self.activity = label[:1].upper() + label[1:]
        if label.lower().startswith("think"):
            self.act("thinking")
        elif self.city.state in ("idle", "queued") + ("completed", "failed", "cancelled"):
            self.act("processing")
        if self.docked:
            if not self.busy:
                self.t0 = time.time()
                self.trail = []
            self.busy = True
            return
        if not console.is_terminal:
            return
        if self._status:
            return
        self.t0 = time.time()
        self.trail = []
        self._status = Live(_Live(self, spinner=True, docked=False), console=console, transient=True,
                            refresh_per_second=12)
        self._status.start()

    def set_activity(self, label: str) -> None:
        self.activity = label[:1].upper() + label[1:]

    def shown_activity(self, now: float | None = None) -> str:
        """The activity on screen. A new one replaces the old only after HOLD_SECS, so a burst of
        quick steps doesn't flicker past unreadably (the trail lists the ones in between)."""
        now = time.time() if now is None else now
        label, since = self._shown
        if self.activity != label and now - since >= HOLD_SECS:
            self._shown = (self.activity, now)
        return self._shown[0]

    def _step_done(self, text: str) -> None:
        with self.lock:
            if not self.trail or self.trail[-1] != text:
                self.trail = (self.trail + [text])[-TRAIL_LINES:]

    def stream(self, piece: str) -> None:
        """Called as the model's reply arrives. Reply text, reasoning (a Thought) and the name of a
        tool it starts calling (an Action) are shown live in the status area and then dropped;
        none of it is added to the conversation."""
        if not piece:
            return
        kind = type(piece).__name__
        if kind == "Thought":
            if not self.thinking:
                self.think_t0 = time.time()
            self.thinking = (self.thinking + piece)[-4000:]
            self.think_secs = time.time() - self.think_t0
            return
        if kind == "Action":
            self.set_activity(f"Preparing: {_ACTION_WORDS.get(str(piece), str(piece).replace('_', ' '))}")
            self.act(_TOOL_STATE.get(str(piece), "processing"))
            return
        self.streamed += piece
        if "<think>" in self.streamed and "</think>" not in self.streamed:
            return   # still reasoning inside a <think> block
        if self.activity == "Thinking":
            self.activity = "Writing"

    def stream_reset(self) -> None:
        self.streamed = ""
        self.thinking = ""

    def thought_note(self) -> None:
        """After a model step: one quiet line saying how long it reasoned (its text is not kept)."""
        secs, self.think_secs = self.think_secs, 0.0
        if secs >= 1:
            self._print(Text(f"  ✻ thought for {secs:.0f}s", style="faint italic"))

    def stop_spinner(self) -> None:
        self.streamed = ""
        self.thinking = ""
        self.busy = False
        if self._status:
            self._status.stop()
            self._status = None

    def log(self, msg: str) -> None:
        """Failover diagnostics. Shown with --verbose (or /verbose)."""
        if self.verbose:
            console.print(Text(f"  ↳ {msg}", style="faint"))

    # read-only actions are grouped into one quiet line ---------------------------------
    def peek(self, kind: str, target: str) -> None:
        name = _brief(target)
        with self.lock:
            self.peeks.setdefault(kind, []).append(name)
        self.act("researching", name)
        verb = {"read": "Reading", "search": "Searching", "list": "Listing", "fetch": "Fetching"}.get(kind, "Looking at")
        done = PEEK.get(kind, (kind,))[0]
        self.set_activity(f"{verb} {name}")
        self._step_done(f"{done[:1].upper() + done[1:]} {name}")

    def flush(self) -> None:
        """Print the reads/searches since the last change as one quiet line that names them."""
        with self.lock:
            if not self.peeks:
                return
            peeks, self.peeks = self.peeks, {}
        line = Text("   ")
        line.append("○ ", style="accent.dim")
        for i, (kind, names) in enumerate(peeks.items()):
            done = PEEK.get(kind, (kind,))[0]
            unique = list(dict.fromkeys(names))
            if i:
                line.append("  ·  ", style="faint")
            line.append(done[:1].upper() + done[1:] + " ", style="muted")
            line.append(", ".join(unique[:3]), style="faint")
            if len(unique) > 3:
                line.append(f" +{len(unique) - 3} more", style="faint italic")
        self._print(line)

    def _print(self, *items) -> None:
        live = self._status is not None
        if live:
            self._status.stop()
        for it in items:
            console.print(it)
        if live:
            self._status.start()

    # conversation -------------------------------------------------------------------
    def user(self, text: str) -> None:
        console.print(_gutter(Text("❯", style="accent"), Text(text, style="user")))

    def assistant(self, text: str, meta: str = "") -> None:
        self.flush()
        self.stop_spinner()
        body = Markdown(text.strip(), code_theme="monokai", inline_code_theme="monokai")
        console.print()
        console.print(Padding(_gutter(Text("◆", style="accent"), body), (0, 0, 0, 1)))
        if meta:
            console.print(Text(f"     {meta}", style="faint"))
        console.print()

    def action(self, verb: str, target: str, note: str = "") -> None:
        """● Verb(target) — one line per change or command."""
        self.flush()
        self.act("coding" if verb in ("Write", "Update", "Delete", "Move") else
                 "processing" if verb in ("Shell", "Process", "Run") else
                 "researching" if verb in ("Fetch", "Search", "Open", "Browse") else
                 "delegating" if verb in ("Delegate", "Parallel", "Team") else
                 "thinking" if verb == "Skill" else self.city.state, target)
        kind = ("tool.write" if verb in ("Write", "Update", "Delete", "Move") else
                "tool.shell" if verb in ("Shell", "Process", "Run") else
                "tool.web" if verb in ("Fetch", "Search", "Open", "Browse") else "accent")
        line = Text(verb, style=kind).append("  ").append(target[:160], style="target")
        if note:
            line.append("  · " + note, style="faint")
        self._print(Text(""), Padding(_gutter(Text("●", style=kind), line), (0, 0, 0, 1)))

    def tool(self, name: str, summary: str) -> None:
        self.action(VERBS.get(name, name), summary)

    def result(self, first: str, body=None) -> None:
        """  ⎿ first line, then an optional renderable indented under it."""
        items = [Padding(Text("└─ ", style="tree").append(first, style="muted"), (0, 0, 0, 3))]
        if body is not None:
            items.append(Padding(body, (0, 0, 0, 6)))
        self._print(*items)

    def tool_result(self, text: str) -> None:
        lines = text.splitlines() or ["(no output)"]
        body = Text()
        for ln in lines[1:4]:
            body.append(ln[:200] + "\n", style="faint")
        if len(lines) > 4:
            body.append(f"… +{len(lines) - 4} lines\n", style="faint")
        body.rstrip()
        self.result(lines[0][:200], body if body.plain else None)

    # files ---------------------------------------------------------------------------
    def wrote(self, path: str, content: str, old: str | None) -> None:
        if old is not None:
            return self.edited(path, old, content)
        lines = content.splitlines()
        self.action("Write", short(path))
        lexer = LEXERS.get(pathlib.Path(path).suffix.lower(), "text")
        shown = "\n".join(lines[:PREVIEW_LINES])
        body = [Syntax(shown, lexer, theme="monokai", line_numbers=True, background_color="default",
                       word_wrap=False)] if shown else []
        if len(lines) > PREVIEW_LINES:
            body.append(Text(f"… +{len(lines) - PREVIEW_LINES} lines", style="faint"))
        self.result(f"Wrote {len(lines)} lines to {short(path)}", Group(*body) if body else None)

    def edited(self, path: str, old: str, new: str) -> None:
        a, b = old.splitlines(), new.splitlines()
        added = removed = 0
        out = Text()
        shown = 0
        for group in difflib.SequenceMatcher(None, a, b).get_grouped_opcodes(2):
            if shown:
                out.append("   ⋮\n", style="faint")
            for tag, i1, i2, j1, j2 in group:
                rows = []
                if tag == "equal":
                    rows = [(j1 + k + 1, " ", a[i1 + k], None) for k in range(i2 - i1)]
                if tag in ("replace", "delete"):
                    removed += i2 - i1
                    rows += [(i + 1, "-", a[i], "del") for i in range(i1, i2)]
                if tag in ("replace", "insert"):
                    added += j2 - j1
                    rows += [(j + 1, "+", b[j], "add") for j in range(j1, j2)]
                for no, sign, txt, style in rows:
                    shown += 1
                    if shown > DIFF_LINES:
                        continue
                    out.append(f"{no:>5} ", style="lineno")
                    out.append(f"{sign} {txt[:180]}".ljust(60) + "\n", style=style or "muted")
        if shown > DIFF_LINES:
            out.append(f"… +{shown - DIFF_LINES} more diff lines\n", style="faint")
        out.rstrip()
        self.action("Update", short(path))
        summary = ", ".join(x for x in (f"Added {added} line{'s' * (added != 1)}" if added else "",
                                        f"removed {removed} line{'s' * (removed != 1)}" if removed else "") if x)
        self.result(summary or "No changes", out if out.plain else None)

    # status messages ----------------------------------------------------------------
    def info(self, msg: str) -> None:
        self.flush()
        self._print(Text(msg, style="muted"))

    def plan(self, title: str, body: str) -> None:
        self.flush()
        self._print(Panel(Markdown(body), title=title, border_style="accent", expand=False))

    def ok(self, msg: str) -> None:
        self.flush()
        self._print(Text("✓ ", style="ok").append(msg))

    def warn(self, msg: str) -> None:
        self.flush()
        self._print(Text(msg, style="warn"))

    def failover(self, msg: str) -> None:
        low = msg.lower()
        m = re.search(r"waiting (\d+(?:\.\d+)?)\s*s", low)
        if "resting" in low or m:
            self.act("waiting", wait=float(m.group(1)) if m else 30.0)
        elif "checking the answer" in low:
            self.act("thinking")
        elif "failed" in low or "trying the next" in low:
            self.city.blip()
        self._print(Padding(Text("↻ ", style="warn").append(msg, style="faint"), (0, 0, 0, 3)))

    def error(self, msg: str) -> None:
        self.flush()
        self.act("failed")
        self.stop_spinner()
        console.print(Text("✗ ", style="err").append(msg, style="err"))

    def rule(self, title: str = "") -> None:
        console.rule(Text(title, style="faint") if title else "", style="faint")

    # panels and tables ---------------------------------------------------------------
    def panel(self, body, title: str = "", subtitle: str = "") -> None:
        console.print(Panel(body, title=Text(title, style="accent") if title else None,
                            subtitle=Text(subtitle, style="faint") if subtitle else None,
                            title_align="left", subtitle_align="right",
                            border_style="accent.dim", box=box.ROUNDED, padding=(0, 1)))

    def table(self, title: str, columns: list[str], rows: list[list], styles: list[str] | None = None) -> None:
        t = Table(title=Text(title, style="accent") if title else None, title_justify="left",
                  box=box.SIMPLE_HEAD, header_style="muted", border_style="faint", pad_edge=False)
        for i, col in enumerate(columns):
            t.add_column(col, style=(styles[i] if styles else None), overflow="fold")
        for r in rows:
            t.add_row(*[x if isinstance(x, Text) else str(x) for x in r])
        console.print(t)

    def replay(self, messages: list[dict], last: int = 6) -> None:
        """Show the tail of a resumed conversation."""
        shown = [m for m in messages if m["role"] in ("user", "assistant") and (m.get("content") or "").strip()]
        if len(shown) > last:
            console.print(Text(f"  … {len(shown) - last} earlier messages", style="faint"))
        for m in shown[-last:]:
            if m["role"] == "user":
                self.user(m["content"].split("\n\n[Forge: skills")[0])
            else:
                self.assistant(m["content"])

    # permission ----------------------------------------------------------------------
    def ask(self, question: str, options: list[str]) -> str:
        """The model asks the user something; returns the answer (or '' if nobody can answer)."""
        self.flush()
        self.stop_spinner()
        if not self.docked and not sys.stdin.isatty():
            return ""
        body = [Text(question, style="text")]
        for i, o in enumerate(options, 1):
            body.append(Text(f"  {i}. {o}", style="accent"))
        console.print(Panel(Group(*body), title=Text("? Forge asks", style="accent"), title_align="left",
                            border_style="accent", box=box.ROUNDED, padding=(0, 1)))
        hint = " (number or your own answer)" if options else ""
        try:
            ans = self._input(f"  [muted]Answer{hint}[/] [accent]›[/] ", f"Answer{hint}").strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        if ans.split()[:1] and ans.split()[0].lower() in ("/exit", "/quit", "/q", "/stop", "/cancel"):
            raise KeyboardInterrupt      # a way out, not an answer: stop the task like ctrl+c
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1]
        return ans

    def todos(self, items: list[dict]) -> None:
        marks = {"done": ("✔", "ok"), "doing": ("◐", "accent"), "todo": ("☐", "muted")}
        lines = Text()
        for it in items:
            mark, style = marks.get(it.get("status", "todo"), marks["todo"])
            done = it.get("status") == "done"
            lines.append(f"  {mark} ", style=style).append(
                str(it.get("task", ""))[:120] + "\n", style="faint strike" if done else "text")
        lines.rstrip()
        self._print(lines if lines.plain else Text("  (empty list)", style="muted"))

    def confirm(self, title: str, detail: str, why: str = "") -> tuple[str, str]:
        """Returns (choice, reason). choice is 'y', 'a' (always) or 'n'."""
        self.flush()
        self.stop_spinner()
        if not self.docked and not sys.stdin.isatty():
            return "n", "non-interactive session; rerun with --yes to allow"
        lines = detail.splitlines()
        body = []
        if why:
            body.append(Text(f"needs your OK: {why}", style="warn"))
        if lines:
            clipped = "\n".join(lines[:40])
            lexer = "diff" if any(l[:1] in "+-@" for l in lines[:5]) else "powershell"
            body.append(Syntax(clipped, lexer, theme="monokai", background_color="default", word_wrap=True))
            if len(lines) > 40:
                body.append(Text(f"… +{len(lines) - 40} more lines", style="faint"))
        console.print(Panel(Group(*body) if body else Text(""), title=Text(f"? {title}", style="warn"),
                            title_align="left", border_style="warn", box=box.ROUNDED, padding=(0, 1)))
        try:
            ans = self._input(
                "  [warn]Allow?[/] [bold]y[/][muted]es[/] · [bold]a[/][muted]lways for this kind[/] · "
                "[bold]n[/][muted]o[/] [accent]›[/] ", "Allow? y = yes · a = always for this kind · n = no"
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "n", ""
        if ans in ("y", "yes", ""):
            return "y", ""
        if ans in ("a", "always"):
            return "a", ""
        try:
            reason = self._input("  [muted]What should it do instead? (optional)[/] [accent]›[/] ",
                                 "What should it do instead? (optional, enter to skip)").strip()
        except (EOFError, KeyboardInterrupt):
            reason = ""
        return "n", reason
