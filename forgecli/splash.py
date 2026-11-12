"""The start screen: one midnight-purple neon city filling the whole box, the tiger framed on the left, FORGE
in big pixel letters on the right and the session details under it, each on its own arrow.

     ┌────────────────────────────────────────────────────────┐
     │  ┌────────┐   █▀▀ █▀█ █▀█ █▀▀ █▀▀                       │
     │  │ tiger  │   → model     …                     (pink   │
     │  │        │   → routing   …                      city   │
     │  └────────┘   → folder    …                      behind │
     └────────────────────────────────────────────────────────┘

The city and the letters are half-block pixels with the text laid on top, so it works in any
truecolor terminal. The tiger is a real picture (tiger_data.bin, baked by
tools/splash/bake_tiger.py) drawn as sixel where the terminal can, and as half-blocks elsewhere."""
from __future__ import annotations

import lzma
import pathlib
import os
import random
import struct
import sys
from functools import lru_cache

from . import art

ARROW = "→ "
PANEL = (12, 6, 28)            # what text sits on: a midnight veil over the city
SKY = [(6, 3, 18), (12, 6, 34), (24, 10, 56), (44, 14, 86), (76, 22, 118)]
FAR = (38, 20, 76)
NEAR = (13, 7, 28)
WIN_FAR = [(120, 92, 196), (150, 110, 220)]
WIN = [(255, 186, 110), (255, 186, 110), (96, 228, 255), (255, 92, 214)]
NEON = (255, 79, 216)
METAL = [(255, 120, 232), (222, 150, 255), (150, 190, 255), (96, 236, 255)]   # FORGE, left to right
SUBTITLE = "BUILD · DEPLOY · SCALE"


# the tiger ---------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _tigers() -> dict:
    raw = lzma.decompress((pathlib.Path(__file__).with_name("tiger_data.bin")).read_bytes())
    out, i = {}, 0
    while i < len(raw):
        n, side = struct.unpack_from("<HH", raw, i)
        i += 4
        pal = [tuple(raw[i + 3 * k:i + 3 * k + 3]) for k in range(255)]
        i += 255 * 3
        out[n] = (side, pal, raw[i:i + side * side])
        i += side * side
    return out


def tiger_sizes() -> list[int]:
    try:
        return sorted(_tigers())
    except Exception:
        return []


def tiger_sixel(n: int) -> str:
    from . import courier_hd
    side, pal, idx = _tigers()[n]
    return courier_hd.encode(idx, side, side, pal).decode("ascii")


def _paint_tiger(cv: art.Canvas, n: int, x0: int, y0: int) -> None:
    """The tiger in half-block pixels: 2n columns x 2n pixel rows."""
    side, pal, idx = _tigers()[n]
    k = side / (2 * n)
    for y in range(2 * n):
        sy = min(side - 1, int((y + 0.5) * k))
        for x in range(2 * n):
            sx = min(side - 1, int((x + 0.5) * k))
            cv.put(x0 + x, y0 + y, pal[idx[sy * side + sx]])


# the city ----------------------------------------------------------------------------------------
def _city(w: int, h: int, seed: int = 11) -> art.Canvas:
    """Midnight-purple skyline: a far violet layer, a near dark layer with sparse lit windows,
    a few neon strips and antenna lights, a lavender moon and a magenta haze on the horizon."""
    rnd = random.Random(seed)
    cv = art.Canvas(w, h, SKY[0])
    cv.gradient(SKY, 0, h)
    for _ in range(w * h // 110):                                 # faint stars, top third
        x, y = rnd.randrange(w), rnd.randrange(max(1, h // 3))
        cv.put(x, y, art.mix(cv.px[y][x], (235, 220, 255), rnd.choice((0.3, 0.5, 0.8))))
    mx, my, mr = int(w * 0.92), max(3, h // 5), max(2.0, h / 10)  # moon, top right
    cv.ellipse(mx, my, mr * 2.2, mr * 2.2, (110, 40, 170), 0.3)
    cv.ellipse(mx, my, mr, mr, (236, 204, 255), 1.0, 0.3)
    cv.ellipse(mx + mr * 0.3, my - mr * 0.25, mr * 0.3, mr * 0.3, (212, 176, 240), 1.0)
    for y in range(h - h // 3, h):                                # horizon haze
        t = (y - (h - h // 3)) / max(1, h // 3)
        for x in range(w):
            cv.put(x, y, (150, 40, 170), 0.22 * t)
    for layer, (lo, hi, col, wins, chance) in enumerate(((0.35, 0.8, FAR, WIN_FAR, 0.10),
                                                        (0.12, 0.5, NEAR, WIN, 0.09))):
        x = -rnd.randrange(4)
        while x < w:
            bw = rnd.randrange(4, 10) if layer == 0 else rnd.randrange(5, 12)
            top = h - int(h * rnd.uniform(lo, hi))
            cv.rect(x, top, bw, h - top, col)
            if layer == 0 and rnd.random() < 0.35:                # stepped roof
                cv.rect(x + 1, top - 2, max(1, bw - 2), 2, col)
            if rnd.random() < 0.25:                               # antenna with a red light
                cv.rect(x + bw // 2, top - 3, 1, 3, col)
                cv.put(x + bw // 2, top - 4, (255, 70, 110))
            for wy in range(top + 2, h - 1, 2):
                for wx in range(x + 1, x + bw - 1, 2):
                    if rnd.random() < chance:
                        cv.put(wx, wy, rnd.choice(wins))
            if layer == 1 and rnd.random() < 0.18 and h - top > 6:  # a neon strip down one edge
                nx = x if rnd.random() < 0.5 else x + bw - 1
                ncol = rnd.choice((NEON, (96, 228, 255)))
                for ny in range(top + 1, min(h - 1, top + rnd.randrange(4, 9))):
                    cv.put(nx, ny, ncol)
            x += bw + (rnd.randrange(0, 3) if layer else rnd.randrange(-1, 2))
    for x in range(w):                                            # neon street line
        cv.put(x, h - 1, art.mix(NEON, (120, 60, 255), x / max(1, w - 1)), 0.55)
    return cv


def _forge(cv: art.Canvas, x0: int, y0: int, k: int) -> int:
    """FORGE in metallic letters: pink to cyan across, a bright top edge, a darker base, a deep
    violet drop shadow and a soft purple glow. Returns the width."""
    m = art.Canvas(cv.w, cv.h, (0, 0, 0))
    fw = m.text("FORGE", x0, y0, (255, 255, 255), font=art.FONT5, k=k)
    on = {(x, y) for y in range(max(0, y0), min(cv.h, y0 + 7 * k))
          for x in range(max(0, x0), min(cv.w, x0 + fw)) if m.px[y][x] == (255, 255, 255)}
    for x, y in on:                                               # glow
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (1, -1), (-1, 1)):
            if (x + dx, y + dy) not in on and 0 <= x + dx < cv.w and 0 <= y + dy < cv.h:
                cv.put(x + dx, y + dy, (170, 60, 255), 0.18)
    for x, y in on:                                               # shadow
        if (x + 1, y + 1) not in on and x + 1 < cv.w and y + 1 < cv.h:
            cv.put(x + 1, y + 1, (36, 8, 64))
    n = len(METAL) - 1
    for x, y in on:
        t = (x - x0) / max(1, fw - 1) * n
        i = min(n - 1, int(t))
        c = art.mix(METAL[i], METAL[i + 1], t - i)
        v = (y - y0) / max(1, 7 * k - 1)
        if v < 0.3:
            c = art.mix(c, (255, 255, 255), 0.5 * (1 - v / 0.3))  # polished top
        elif v > 0.7:
            c = art.mix(c, (60, 20, 110), 0.35 * (v - 0.7) / 0.3)  # darker base
        cv.put(x, y, c)
    return fw


# composing ---------------------------------------------------------------------------------------
def _fg(rgb) -> str:
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def _bg(rgb) -> str:
    return f"\x1b[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def _cells(console, text) -> list[tuple[str, tuple | None, bool]]:
    """A rich Text as (char, fg rgb, bold) cells, theme styles resolved."""
    out = []
    for seg in text.render(console):
        st = seg.style
        rgb = None
        if st and st.color and not st.color.is_default:
            rgb = tuple(st.color.get_truecolor())
        for ch in seg.text:
            if ch != "\n":
                out.append((ch, rgb, bool(st and st.bold)))
    return out


def layout(width: int, lines: int, span: int = 52) -> dict | None:
    """Sizes for a box `width` columns wide holding `lines` lines of details, the longest `span`
    columns. None when they do not fit beside even the smallest tiger."""
    sizes = tiger_sizes()
    if not sizes or width < 60:
        return None
    k = 2 if width >= 100 else 1                     # FORGE letter scale
    title_rows = (7 * k + 1) // 2 + 2               # letters, BUILD · DEPLOY · SCALE, a gap
    rows = max(title_rows + lines + 2, sizes[0] + 2)
    for n in reversed(sizes):
        if n + 2 <= rows and 2 * n + 7 + min(span, 58) <= width:
            break
    else:
        return None
    rows = max(rows, n + 2)
    return {"w": width, "h": rows, "n": n, "k": k, "tx": 3, "ty": max(1, (rows - n) // 2), "rx": 3 + 2 * n + 4,
            "title_rows": title_rows}


def render_card(console, lines: list, pad: int = 1) -> bool:
    """Draw the start screen: `lines` (rich Text) go under FORGE, one arrow each. False when the
    terminal is too small or the art is missing (the caller then shows the plain box)."""
    width = min(console.width - 2 * pad, 132)
    L = layout(width, len(lines), max((t.cell_len for t in lines), default=0))
    if not L:
        return False
    w, h, n, k = L["w"], L["h"], L["n"], L["k"]
    tx, ty, rx = L["tx"], L["ty"], L["rx"]
    room = w - rx - 2                                # long details end in … rather than overflow
    lines = [t if t.cell_len <= room else t.copy() for t in lines]
    for t in lines:
        if t.cell_len > room:
            t.truncate(room, overflow="ellipsis")
    cv = _city(w, 2 * h)

    # frame and tiger (left)
    fx0, fy0, fx1, fy1 = tx - 1, 2 * ty - 1, tx + 2 * n, 2 * ty + 2 * n
    for x in range(fx0 - 1, fx1 + 2):                 # soft glow, then the neon frame
        cv.put(x, fy0 - 1, (170, 60, 255), 0.45)
        cv.put(x, fy1 + 1, (170, 60, 255), 0.45)
    for y in range(fy0 - 1, fy1 + 2):
        cv.put(fx0 - 1, y, (170, 60, 255), 0.45)
        cv.put(fx1 + 1, y, (170, 60, 255), 0.45)
    for x in range(fx0, fx1 + 1):
        c = art.mix(NEON, (150, 90, 255), (x - fx0) / max(1, fx1 - fx0))
        cv.put(x, fy0, c)
        cv.put(x, fy1, c)
    for y in range(fy0, fy1 + 1):
        cv.put(fx0, y, NEON)
        cv.put(fx1, y, (150, 90, 255))
    _paint_tiger(cv, n, tx, 2 * ty)

    # the words first, so the veil behind them can end where they do
    words: dict[int, tuple[int, list]] = {}
    first = 1 + L["title_rows"]
    for i, t in enumerate(lines):
        words[first + i] = (rx, _cells(console, t))
    span = max((len(c) for _, c in words.values()), default=0)

    # a midnight veil behind the details so they read clearly; the city shows around it
    vx1 = min(w - 1, rx + span + 3)
    vy0, vy1 = 2 * (first - 2), min(2 * h - 1, 2 * (first + len(lines)) + 1)
    for y in range(vy0, vy1):
        for x in range(rx - 2, vx1):
            edge = min(1.0, (vx1 - x) / 4, (x - rx + 3) / 2)
            cv.put(x, y, PANEL, edge)

    # FORGE in metallic letters, BUILD · DEPLOY · SCALE centred under it between two thin rules
    tyx = 2 if k == 2 else 3
    fw = _forge(cv, rx, tyx, k)
    from rich.text import Text
    sub = " ".join(SUBTITLE) if k == 2 else SUBTITLE
    rule = max(2, min(8, (fw - len(sub)) // 2 - 2))
    st = Text("─" * rule + "  ", style="#6d3fb0").append(sub, style="bold #c9b6ff").append("  " + "─" * rule, style="#6d3fb0")
    words[first - 2] = (rx + max(0, (fw - st.cell_len) // 2), _cells(console, st))

    # the words, laid over the picture cell by cell
    out = []
    margin = " " * pad
    for row in range(h):
        top, bot = cv.px[2 * row], cv.px[2 * row + 1]
        wx, cells = words.get(row, (rx, None))
        s = [margin]
        for x in range(w):
            ch = None
            if cells is not None and wx <= x < wx + len(cells):
                ch, rgb, bold = cells[x - wx]
                if ch == " ":
                    ch = None
            if ch is not None:
                back = art.mix(top[x], bot[x], 0.5)
                s.append(_bg(back) + ("\x1b[1m" if bold else "") + _fg(rgb or (236, 228, 255)) + ch + "\x1b[22m")
            elif top[x] == bot[x]:
                s.append(_bg(top[x]) + " ")
            else:
                s.append(_bg(bot[x]) + _fg(top[x]) + "▀")
        s.append("\x1b[0m")
        out.append("".join(s))

    sixel = hd = None
    if art.graphics(console) == "sixel":
        try:
            from . import courier_hd
            if courier_hd.cell_px() == (10, 20):
                sixel = tiger_sixel(n)
                if os.environ.get("FORGE_SPLASH_HD", "on").lower() not in ("off", "0", "no"):
                    from . import splash_hd
                    hd = splash_hd.picture(L, (rx - 2, vy0 / 2, vx1, vy1 / 2))
        except Exception:
            hd = None
    f = console.file
    f.write("\n".join(out) + "\n")
    if sixel:
        if hd:
            # the whole picture in real pixels (city, moon, FORGE, frame) over the half-block one
            f.write(f"\x1b7\x1b[{h}A\x1b[{pad + 1}G" + hd + "\x1b8")
        # back up to the tiger's box, draw the real picture over the half-block one, come back
        f.write(f"\x1b7\x1b[{h - ty}A\x1b[{pad + tx + 1}G" + sixel + "\x1b8")
        if hd:
            # the words again on top: text written into a cell replaces the image there, so they
            # stay sharp, selectable text on the dark panel.
            for row, (wx, cells) in sorted(words.items()):
                text = "".join(c[0] for c in cells)
                a, b = len(text) - len(text.lstrip(" ")), len(text.rstrip(" "))
                if a >= b:
                    continue
                seg = []
                for x in range(a, b):
                    ch, rgb, bold = cells[x]
                    back = art.mix(cv.px[2 * row][wx + x], cv.px[2 * row + 1][wx + x], 0.5)
                    seg.append(_bg(back) + ("\x1b[1m" if bold else "") + _fg(rgb or (236, 228, 255)) + ch + "\x1b[22m")
                up = f"\x1b[{h - row}A" if h - row else ""
                f.write("\x1b7" + up + f"\x1b[{pad + wx + a + 1}G" + "".join(seg) + "\x1b[0m\x1b8")
    f.flush()
    return True


# the compact start screen (default) ----------------------------------------------------------------
# No card and no background: the cub sits straight on the terminal, FORGE in small metallic pixel
# letters beside it, the details under FORGE. About a dozen rows, so the chat starts high up.
#
#      ▄▀▀▀▄       █▀▀ █▀█ █▀█ █▀▀ █▀▀
#     (  cub  )    BUILD · DEPLOY · SCALE
#      ▀▄▄▄▀       v0.8.3  multi-model coding agent
#                  → model     …
#                  → routing   …

@lru_cache(maxsize=1)
def _cubs() -> dict:
    """rows -> (w, h, palette, indexed pixels with 255 = transparent), from cub_data.bin."""
    raw = lzma.decompress((pathlib.Path(__file__).with_name("cub_data.bin")).read_bytes())
    out, i = {}, 0
    while i < len(raw):
        rows, w, h = struct.unpack_from("<HHH", raw, i)
        i += 6
        pal = [tuple(raw[i + 3 * k:i + 3 * k + 3]) for k in range(255)]
        i += 255 * 3
        out[rows] = (w, h, pal, raw[i:i + w * h])
        i += w * h
    return out


def cub_sixel(rows: int) -> str:
    from . import courier_hd
    w, h, pal, idx = _cubs()[rows]
    return courier_hd.encode(idx, w, h, pal).decode("ascii")


def _cub_cells(rows: int) -> list[str]:
    """The cub as half-block rows (transparent pixels show the terminal's own background)."""
    w, h, pal, idx = _cubs()[rows]
    cols = w // 10
    out = []
    for r in range(rows):
        s = []
        for c in range(cols):
            def px(yy):
                sy = min(h - 1, int((yy + 0.5) * h / (2 * rows)))
                sx = min(w - 1, int((c + 0.5) * w / cols))
                v = idx[sy * w + sx]
                return None if v == 255 else pal[v]
            top, bot = px(2 * r), px(2 * r + 1)
            if top is None and bot is None:
                s.append("\x1b[0m ")
            elif bot is None:
                s.append("\x1b[0m" + _fg(top) + "▀")
            elif top is None:
                s.append("\x1b[0m" + _fg(bot) + "▄")
            else:
                s.append(_bg(bot) + _fg(top) + "▀")
        out.append("".join(s) + "\x1b[0m")
    return out


def _title_rows() -> list[str]:
    """FORGE in the 5x7 pixel font as 4 half-block rows: pink to cyan, a bright top, darker base."""
    on = set()
    cx = 0
    for ch in "FORGE":
        for gy, row in enumerate(art.FONT5[ch]):
            for gx, b in enumerate(row):
                if b != ".":
                    on.add((cx + gx, gy))
        cx += 6
    w = cx - 1
    n = len(METAL) - 1

    def colour(x, y):
        t = x / max(1, w - 1) * n
        i = min(n - 1, int(t))
        c = art.mix(METAL[i], METAL[i + 1], t - i)
        if y <= 1:
            c = art.mix(c, (255, 255, 255), 0.45 - 0.2 * y)
        elif y >= 5:
            c = art.mix(c, (60, 20, 110), 0.25)
        return c

    rows = []
    for r in range(4):
        s = []
        for x in range(w):
            top, bot = (x, 2 * r) in on, (x, 2 * r + 1) in on
            if top and bot:
                s.append(_bg(colour(x, 2 * r + 1)) + _fg(colour(x, 2 * r)) + "▀\x1b[0m")
            elif top:
                s.append(_fg(colour(x, 2 * r)) + "▀\x1b[0m")
            elif bot:
                s.append(_fg(colour(x, 2 * r + 1)) + "▄\x1b[0m")
            else:
                s.append(" ")
        rows.append("".join(s))
    return rows


def render(console, lines: list, pad: int = 1) -> bool:
    """The compact start screen: cub on the left, FORGE and `lines` (rich Text) on the right.
    False when the window is too narrow or the art is missing (the caller shows the plain box)."""
    from rich.text import Text
    try:
        sizes = sorted(_cubs())
    except Exception:
        return False
    title = _title_rows()
    sub = Text(SUBTITLE, style="bold #c9b6ff")
    body = [*[None] * len(title), sub, *lines]                 # None = a FORGE row
    rows = min(max(sizes), max([z for z in sizes if z <= len(body) - 2] or [sizes[0]]))
    cub_cols = _cubs()[rows][0] // 10
    gap = 3
    x0 = pad + cub_cols + gap                                  # where FORGE and the details start
    room = console.width - x0 - 1
    if room < 40:
        return False
    cub_top = max(0, (len(body) - rows) // 2)
    sixel = False
    if art.graphics(console) == "sixel":
        try:
            from . import courier_hd
            sixel = courier_hd.cell_px() == (10, 20)
        except Exception:
            sixel = False
    cub = [" " * cub_cols] * rows if sixel else _cub_cells(rows)
    f = console.file
    out = []
    for i, item in enumerate(body):
        left = cub[i - cub_top] if 0 <= i - cub_top < rows else " " * cub_cols
        if item is None:
            right = title[i]
        else:
            t = item.copy() if item.cell_len > room else item
            if t.cell_len > room:
                t.truncate(room, overflow="ellipsis")
            right = "".join(c for c in _ansi(console, t))
        out.append(" " * pad + left + " " * gap + right + "\x1b[0m")
    f.write("\n".join(out) + "\n")
    if sixel:
        up = len(body) - cub_top
        f.write(f"\x1b7\x1b[{up}A\x1b[{pad + 1}G" + cub_sixel(rows) + "\x1b8")
    f.flush()
    return True


def _ansi(console, text) -> str:
    """rich Text to one line of ANSI."""
    import io
    from rich.console import Console
    c = Console(file=io.StringIO(), force_terminal=True, color_system="truecolor", width=10_000,
                highlight=False, soft_wrap=True)
    c.print(text, end="")
    return c.file.getvalue()
