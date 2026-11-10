"""Forge's pixel-art engine: the neon-city look.

A tiny canvas with soft-edged shapes, ordered dithering and neon bloom, and two ways to put it
on screen:
  * half blocks (▀ with a foreground and a background colour = two pixels per character), which
    work in any truecolor terminal and are what the live city scene uses;
  * sixel, real pixels, for the startup banner where the terminal supports it (Windows Terminal
    1.22+, WezTerm, foot, mlterm, iTerm2...). Pick with FORGE_GRAPHICS=auto|sixel|blocks|plain.

Everything is drawn in code from shapes and small sprite strings, so it is crisp at any size and
ships no image files. Pure Python, no dependencies.
"""
from __future__ import annotations

import math
import os
import random
import sys

# The night palette. One ground (night indigo), one ink (lavender white), two neons: magenta is
# Forge itself, cyan is the machine. Amber is kept for warm, human things: windows, cargo, lamps.
NIGHT = (11, 7, 30)
INK = (236, 228, 255)
MAGENTA = (255, 79, 216)
CYAN = (62, 240, 255)
AMBER = (255, 200, 87)
GREEN = (91, 255, 157)
RED = (255, 77, 109)
LAVENDER = (160, 150, 210)
SKY = [(8, 5, 26), (20, 9, 54), (44, 13, 84), (92, 22, 112), (150, 36, 128)]

BAYER4 = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]


def mix(a, b, t: float):
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t), int(a[2] + (b[2] - a[2]) * t))


def scale(c, k: float):
    return (min(255, int(c[0] * k)), min(255, int(c[1] * k)), min(255, int(c[2] * k)))


def hash32(*parts: int) -> int:
    """Cheap deterministic integer hash (same input, same answer, every run)."""
    h = 2166136261
    for p in parts:
        h ^= p & 0xFFFFFFFF
        h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 13
    return h


class Canvas:
    __slots__ = ("w", "h", "px", "glow")

    def __init__(self, w: int, h: int, bg=NIGHT):
        self.w, self.h = w, h
        self.px = [[bg] * w for _ in range(h)]
        self.glow = None

    def copy(self) -> "Canvas":
        c = Canvas.__new__(Canvas)
        c.w, c.h = self.w, self.h
        c.px = [row[:] for row in self.px]
        c.glow = None
        return c

    # pixels -------------------------------------------------------------------------
    def put(self, x: int, y: int, c, a: float = 1.0) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            if a >= 0.999:
                self.px[y][x] = c
            elif a > 0.004:
                self.px[y][x] = mix(self.px[y][x], c, a)

    def light(self, x: int, y: int, c, s: float = 1.0) -> None:
        """Add neon light at a pixel; bloom() spreads it into a glow."""
        if 0 <= x < self.w and 0 <= y < self.h:
            if self.glow is None:
                self.glow = [[0.0] * (self.w * 3) for _ in range(self.h)]
            g = self.glow[y]
            i = x * 3
            g[i] += c[0] * s
            g[i + 1] += c[1] * s
            g[i + 2] += c[2] * s

    def bloom(self, radius: int = 1, strength: float = 0.6) -> None:
        """Blur the light layer (box blur, twice) and add it on top: the neon glow."""
        if self.glow is None:
            return
        w, h, g = self.w, self.h, self.glow
        for _ in range(2):
            # horizontal
            for y in range(h):
                row = g[y]
                out = [0.0] * (w * 3)
                for ch in range(3):
                    acc = 0.0
                    win = []
                    vals = row[ch::3]
                    pref = [0.0]
                    for v in vals:
                        pref.append(pref[-1] + v)
                    for x in range(w):
                        lo, hi = max(0, x - radius), min(w, x + radius + 1)
                        out[x * 3 + ch] = (pref[hi] - pref[lo]) / (2 * radius + 1)
                g[y] = out
            # vertical
            for x3 in range(w * 3):
                col = [g[y][x3] for y in range(h)]
                pref = [0.0]
                for v in col:
                    pref.append(pref[-1] + v)
                for y in range(h):
                    lo, hi = max(0, y - radius), min(h, y + radius + 1)
                    g[y][x3] = (pref[hi] - pref[lo]) / (2 * radius + 1)
        for y in range(h):
            row, gr = self.px[y], g[y]
            for x in range(w):
                i = x * 3
                r, gg, b = gr[i], gr[i + 1], gr[i + 2]
                if r + gg + b > 3:
                    c = row[x]
                    row[x] = (min(255, int(c[0] + r * strength)), min(255, int(c[1] + gg * strength)),
                              min(255, int(c[2] + b * strength)))
        self.glow = None

    # shapes (float pixel coordinates, soft one-pixel edges) ------------------------------
    def rect(self, x: int, y: int, w: int, h: int, c, a: float = 1.0) -> None:
        for yy in range(max(0, y), min(self.h, y + h)):
            for xx in range(max(0, x), min(self.w, x + w)):
                self.put(xx, yy, c, a)

    def ellipse(self, cx: float, cy: float, rx: float, ry: float, c, a: float = 1.0, glow: float = 0.0) -> None:
        if rx <= 0 or ry <= 0:
            return
        r = min(rx, ry)
        for y in range(max(0, int(cy - ry - 1)), min(self.h, int(cy + ry + 2))):
            py = (y + 0.5 - cy) / ry
            for x in range(max(0, int(cx - rx - 1)), min(self.w, int(cx + rx + 2))):
                d = math.hypot((x + 0.5 - cx) / rx, py)
                cov = 0.5 - (d - 1.0) * r
                if cov > 0:
                    self.put(x, y, c, a * min(1.0, cov))
                    if glow:
                        self.light(x, y, c, glow * min(1.0, cov))

    def poly(self, pts, c, a: float = 1.0, glow: float = 0.0) -> None:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        edges = list(zip(pts, pts[1:] + pts[:1]))
        for y in range(max(0, int(min(ys)) - 1), min(self.h, int(max(ys)) + 2)):
            py = y + 0.5
            for x in range(max(0, int(min(xs)) - 1), min(self.w, int(max(xs)) + 2)):
                px = x + 0.5
                inside = False
                dist = 1e9
                for (x1, y1), (x2, y2) in edges:
                    if (y1 > py) != (y2 > py) and px < (x2 - x1) * (py - y1) / ((y2 - y1) or 1e-9) + x1:
                        inside = not inside
                    dx, dy = x2 - x1, y2 - y1
                    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / ((dx * dx + dy * dy) or 1e-9)))
                    dist = min(dist, math.hypot(px - x1 - t * dx, py - y1 - t * dy))
                cov = 0.5 + dist if inside else 0.5 - dist
                if cov > 0:
                    self.put(x, y, c, a * min(1.0, cov))
                    if glow:
                        self.light(x, y, c, glow * min(1.0, cov))

    def gradient(self, stops, y0: int, y1: int, dither: bool = True) -> None:
        """Vertical sky gradient in hard bands with ordered (Bayer) dithering between them."""
        n = len(stops) - 1
        span = max(1, y1 - y0)
        for y in range(max(0, y0), min(self.h, y1)):
            t = (y - y0) / span * n
            i = min(n - 1, int(t))
            f = t - i
            row = self.px[y]
            for x in range(self.w):
                th = (BAYER4[y % 4][x % 4] + 0.5) / 16 if dither else 0.5
                row[x] = stops[i + 1] if f > th else stops[i]

    def sprite(self, rows, palette: dict, x: int, y: int, flip: bool = False, glow: dict | None = None,
               a: float = 1.0) -> None:
        """Draw a pixel sprite given as strings ('.' is transparent)."""
        for dy, row in enumerate(rows):
            if flip:
                row = row[::-1]
            for dx, ch in enumerate(row):
                if ch == "." or ch == " ":
                    continue
                c = palette.get(ch)
                if c is None:
                    continue
                self.put(x + dx, y + dy, c, a)
                if glow and ch in glow:
                    self.light(x + dx, y + dy, c, glow[ch])

    def text(self, word: str, x: int, y: int, c, font=None, k: int = 1, glow: float = 0.0, gap: int = 1) -> int:
        """Pixel text; returns the width drawn."""
        font = font or FONT3
        cx = x
        for chr_ in word.upper():
            glyph = font.get(chr_)
            if glyph is None:
                cx += 2 * k
                continue
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit != ".":
                        for sy in range(k):
                            for sx in range(k):
                                self.put(cx + gx * k + sx, y + gy * k + sy, c)
                                if glow:
                                    self.light(cx + gx * k + sx, y + gy * k + sy, c, glow)
            cx += (len(glyph[0]) + gap) * k
        return cx - x - gap * k

    # output -------------------------------------------------------------------------
    def ansi(self, x0: int = 0, w: int | None = None) -> str:
        """Half-block rendering: each character shows two pixels stacked."""
        w = self.w if w is None else w
        out = []
        esc_fg, esc_bg = {}, {}
        for y in range(0, self.h - 1, 2):
            top, bot = self.px[y], self.px[y + 1]
            line = []
            fg = bg = None
            for x in range(x0, x0 + w):
                t, b = top[x], bot[x]
                if b != bg:
                    s = esc_bg.get(b)
                    if s is None:
                        s = esc_bg[b] = f"\x1b[48;2;{b[0]};{b[1]};{b[2]}m"
                    line.append(s)
                    bg = b
                if t == b:
                    line.append(" ")
                    continue
                if t != fg:
                    s = esc_fg.get(t)
                    if s is None:
                        s = esc_fg[t] = f"\x1b[38;2;{t[0]};{t[1]};{t[2]}m"
                    line.append(s)
                    fg = t
                line.append("▀")
            line.append("\x1b[0m")
            out.append("".join(line))
        return "\n".join(out)

    def sixel(self, k: int = 3) -> str:
        """Real pixels: each canvas pixel becomes a k x k square. Colours are quantised to a
        6x6x6 cube with ordered dithering, which reads as deliberate pixel-art shading."""
        w, h = self.w, self.h
        pal: dict = {}
        grid = []
        for y in range(h):
            row = []
            for x in range(w):
                c = self.px[y][x]
                th = (BAYER4[y % 4][x % 4] + 0.5) / 16 - 0.5
                q = tuple(max(0, min(5, int(v * 5 / 255 + 0.5 + th * 0.9))) for v in c)
                i = pal.get(q)
                if i is None:
                    i = pal[q] = len(pal)
                row.append(i)
            grid.append(row)
        W, H = w * k, h * k
        out = ["\x1bP0;1;0q", f'"1;1;{W};{H}']
        for q, i in pal.items():
            out.append(f"#{i};2;{q[0] * 20};{q[1] * 20};{q[2] * 20}")
        for band in range(0, H, 6):
            per: dict = {}
            for x in range(w):
                for kk in range(6):
                    yy = band + kk
                    if yy >= H:
                        break
                    ci = grid[yy // k][x]
                    bits = per.setdefault(ci, [0] * w)
                    bits[x] |= 1 << kk
            chunks = []
            for ci, bits in per.items():
                s = [f"#{ci}"]
                x = 0
                while x < w:
                    b = bits[x]
                    run = 1
                    while x + run < w and bits[x + run] == b:
                        run += 1
                    n = run * k
                    ch = chr(63 + b)
                    s.append(f"!{n}{ch}" if n > 3 else ch * n)
                    x += run
                chunks.append("".join(s))
            out.append("$".join(chunks) + "-")
        out.append("\x1b\\")
        return "".join(out)


# fonts: '#' is a lit pixel ---------------------------------------------------------------
FONT3 = {
    "A": ["###", "#.#", "###", "#.#", "#.#"], "C": ["###", "#..", "#..", "#..", "###"],
    "D": ["##.", "#.#", "#.#", "#.#", "##."], "E": ["###", "#..", "##.", "#..", "###"],
    "G": ["###", "#..", "#.#", "#.#", "###"], "H": ["#.#", "#.#", "###", "#.#", "#.#"],
    "I": ["###", ".#.", ".#.", ".#.", "###"], "K": ["#.#", "#.#", "##.", "#.#", "#.#"],
    "L": ["#..", "#..", "#..", "#..", "###"], "M": ["#.#", "###", "###", "#.#", "#.#"],
    "N": ["##.", "#.#", "#.#", "#.#", "#.#"], "O": ["###", "#.#", "#.#", "#.#", "###"],
    "P": ["###", "#.#", "###", "#..", "#.."], "Q": ["###", "#.#", "#.#", "###", "..#"],
    "R": ["##.", "#.#", "##.", "#.#", "#.#"], "S": ["###", "#..", "###", "..#", "###"],
    "T": ["###", ".#.", ".#.", ".#.", ".#."], "U": ["#.#", "#.#", "#.#", "#.#", "###"],
    "V": ["#.#", "#.#", "#.#", ".#.", ".#."], "W": ["#.#", "#.#", "###", "###", "#.#"],
    "F": ["###", "#..", "##.", "#..", "#.."], "!": ["#", "#", "#", ".", "#"],
    "?": ["##.", "..#", ".#.", "...", ".#."], ".": [".", ".", ".", ".", "#"],
    "0": ["###", "#.#", "#.#", "#.#", "###"], "1": [".#.", "##.", ".#.", ".#.", "###"],
    "2": ["##.", "..#", ".#.", "#..", "###"], "3": ["##.", "..#", ".#.", "..#", "##."],
    "4": ["#.#", "#.#", "###", "..#", "..#"], "5": ["###", "#..", "##.", "..#", "##."],
    "6": [".##", "#..", "###", "#.#", "###"], "7": ["###", "..#", ".#.", ".#.", ".#."],
    "8": ["###", "#.#", "###", "#.#", "###"], "9": ["###", "#.#", "###", "..#", "##."],
    "V_": ["#.#", "#.#", "#.#", ".#.", ".#."],
}
FONT5 = {
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
}


# the mascot: a tiger cub's head, drawn from shapes ------------------------------------------
FUR = (255, 132, 44)
FUR_LIGHT = (255, 170, 82)
FUR_SHADE = (222, 88, 30)
CREAM = (255, 236, 214)
WHITE = (255, 248, 240)
STRIPE = (44, 12, 32)
OUTLINE = (26, 8, 30)
NOSE = (255, 92, 150)


def tiger(cv: Canvas, cx: float, cy: float, size: float, glow: bool = True) -> None:
    """A tiger cub's head, about `size` pixels tall, centred at (cx, cy), lit by the city:
    magenta neon on its left edge, cyan on its right, eyes glowing cyan."""
    s = size / 84.0

    def P(x, y):
        return (cx + x * s, cy + y * s)

    def E(x, y, rx, ry, c, a=1.0, g=0.0):
        cv.ellipse(cx + x * s, cy + y * s, rx * s, ry * s, c, a, g if glow else 0.0)

    def Q(pts, c, g=0.0):
        cv.poly([P(x, y) for x, y in pts], c, 1.0, g if glow else 0.0)

    # rim light from the neon city, then the outline, then the fur
    for sx, col in ((-1, MAGENTA), (1, CYAN)):
        E(sx * 27 + sx * 2.6, -26, 14.5, 14.5, col, 1.0, 0.5)
        E(sx * 3.2, 2, 42, 37, col, 1.0, 0.35)
    for sx in (-1, 1):
        E(sx * 27, -26, 14.5, 14.5, OUTLINE)
    E(0, 2, 41, 36, OUTLINE)
    for sx in (-1, 1):
        E(sx * 27, -26, 12.5, 12.5, FUR)
        E(sx * 26, -23.5, 7, 7, CREAM)
        Q([(sx * 34, -34), (sx * 39, -27), (sx * 31, -30)], STRIPE)
    E(0, 2, 39.5, 34.5, FUR)
    E(0, 15, 36, 20, FUR_SHADE)
    E(0, 4, 37, 22, FUR)
    E(-7, -12, 24, 14, FUR_LIGHT)
    # stripes: forehead, brows, sides
    Q([(-3.5, -32), (3.5, -32), (0, -16)], STRIPE)
    Q([(-15, -29), (-9, -31), (-8, -19)], STRIPE)
    Q([(9, -31), (15, -29), (8, -19)], STRIPE)
    for sx in (-1, 1):
        Q([(sx * 40, -4), (sx * 40, 4), (sx * 26, 1)], STRIPE)
        Q([(sx * 38.5, 10), (sx * 36.5, 16), (sx * 27, 11)], STRIPE)
        Q([(sx * 24, -12.5), (sx * 10, -14), (sx * 13, -9.5)], STRIPE)
    # cheek fluff and muzzle
    for sx in (-1, 1):
        E(sx * 19, 18, 14, 10, CREAM)
        Q([(sx * 31, 12), (sx * 43, 19), (sx * 30, 22)], CREAM)
        Q([(sx * 30, 20), (sx * 38, 28), (sx * 24, 26)], CREAM)
        Q([(sx * 41, 13), (sx * 44, 18), (sx * 35, 16)], STRIPE)
    E(0, 19, 10.5, 7.5, WHITE)
    E(0, 29, 8, 4.5, CREAM)
    # eyes: glowing cyan with a slit pupil and a white catchlight
    for sx in (-1, 1):
        E(sx * 15, 1.5, 9, 9.5, OUTLINE)
        E(sx * 15, 2, 7.2, 7.8, CYAN, 1.0, 0.9)
        E(sx * 15, 3.6, 4.6, 4.4, (24, 170, 210))
        E(sx * 15, 2.5, 2.2, 6.2, (12, 4, 20))
        E(sx * 15 - 2.6, -1.8, 2.1, 2.1, (255, 255, 255))
    # nose and mouth
    Q([(-7, 10.5), (7, 10.5), (0, 17.5)], NOSE)
    E(-1.8, 12.3, 1.6, 1.0, (255, 170, 205))
    Q([(-0.6, 17), (0.6, 17), (0.6, 21), (-0.6, 21)], STRIPE)
    Q([(-0.5, 20.2), (0.3, 21.2), (-6, 24), (-7.4, 22.6)], STRIPE)
    Q([(0.5, 20.2), (-0.3, 21.2), (6, 24), (7.4, 22.6)], STRIPE)
    for sx in (-1, 1):
        for dx, dy in ((7, 18), (10, 20), (8.5, 22)):
            E(sx * dx, dy, 0.9, 0.9, (200, 160, 150))


# the neon city behind the banner ------------------------------------------------------------
def skyline(cv: Canvas, horizon: int, seed: int = 7, neon: bool = True, stars: bool = True,
            moon=None) -> list:
    """Sky, stars, moon and two rows of buildings down to `horizon`. Returns lit windows as
    (x, y, colour) so animated scenes can switch them on and off."""
    rnd = random.Random(seed)
    w = cv.w
    cv.gradient(SKY, 0, horizon + 1)
    if stars:
        for _ in range(max(4, w * horizon // 90)):
            x, y = rnd.randrange(w), rnd.randrange(max(1, horizon * 2 // 3))
            cv.put(x, y, mix(cv.px[y][x], (255, 230, 255), rnd.choice((0.35, 0.55, 0.8))))
    if moon is not False:
        mx, my = moon or (int(w * 0.9), max(2, horizon // 5))
        mr = max(1.2, horizon / 11)
        cv.ellipse(mx, my, mr * 1.9, mr * 1.9, (110, 40, 130), 0.35)
        cv.ellipse(mx, my, mr, mr, (255, 220, 248), 1.0, 0.25)
        cv.ellipse(mx + mr * 0.35, my - mr * 0.2, mr * 0.25, mr * 0.25, (230, 190, 225), 1.0)
    windows = []
    for layer, (lo, hi, col, lit_col, chance) in enumerate((
            (0.35, 0.75, (34, 16, 72), (120, 80, 170), 0.25),
            (0.15, 0.5, (17, 9, 40), None, 0.33))):
        x = -rnd.randrange(4)
        while x < w:
            bw = rnd.randrange(5, 12) if horizon > 14 else rnd.randrange(4, 9)
            top = horizon - int(horizon * rnd.uniform(lo, hi))
            cv.rect(x, top, bw, horizon - top + 1, col)
            if layer == 1 and rnd.random() < 0.3 and top > 2:
                ax = x + bw // 2
                cv.rect(ax, top - 2, 1, 2, (60, 40, 90))
                windows.append((ax, top - 2, RED, "blink"))
            step = 2
            for wy in range(top + 2, horizon - 1, step):
                for wx in range(x + 1, x + bw - 1, 2):
                    if rnd.random() < chance:
                        c = lit_col or rnd.choice((AMBER, AMBER, AMBER, CYAN, MAGENTA, (255, 170, 120)))
                        windows.append((wx, wy, c, "win" if layer else "far"))
            if neon and layer == 1 and rnd.random() < 0.22 and horizon - top > 6:
                nx = x + (0 if rnd.random() < 0.5 else bw - 1)
                ncol = rnd.choice((MAGENTA, CYAN, (255, 120, 60)))
                for ny in range(top + 2, min(horizon - 1, top + 2 + rnd.randrange(3, 7))):
                    windows.append((nx, ny, ncol, "neon"))
            x += bw + (rnd.randrange(0, 2) if layer else rnd.randrange(-1, 2))
    return windows


def windows_on(cv: Canvas, windows, now: float, motion: bool = True) -> None:
    """Light the windows; some switch on and off slowly, antenna lights blink, neon flickers."""
    t = now if motion else 0.0
    for i, (x, y, c, kind) in enumerate(windows):
        if kind == "blink":
            if int(t * 1.2 + i) % 2 == 0:
                cv.put(x, y, c)
                cv.light(x, y, c, 0.8)
            continue
        if kind == "neon":
            flick = hash32(i, int(t * 6)) % 41 == 0
            if not flick:
                cv.put(x, y, c)
                cv.light(x, y, c, 0.7)
            continue
        period = 4 + (hash32(i, 3) % 9)
        on = hash32(i, x, int((t + i * 0.37) / period)) % 100 < (78 if kind == "far" else 70)
        if on:
            cv.put(x, y, scale(c, 0.55) if kind == "far" else c)
            if kind == "win":
                cv.light(x, y, c, 0.18)


def street(cv: Canvas, y0: int, now: float, motion: bool = True) -> None:
    """Wet street from row y0 down: a neon curb, then the city mirrored in the puddles."""
    if y0 >= cv.h:
        return
    t = now if motion else 0.0
    curb = mix((60, 30, 110), CYAN, 0.35)
    for x in range(cv.w):
        cv.px[y0][x] = curb
    for k, y in enumerate(range(y0 + 1, cv.h)):
        src = y0 - 1 - k
        if src < 0:
            break
        shift = int(round(1.2 * math.sin(t * 2.1 + y * 1.7)))
        srow = cv.px[src]
        row = cv.px[y]
        for x in range(cv.w):
            c = srow[min(cv.w - 1, max(0, x + shift))]
            shimmer = 0.34 + 0.1 * math.sin(x * 0.9 + t * 3.0 + y * 2.3) - k * 0.04
            row[x] = mix((9, 6, 24), c, max(0.12, shimmer))


def neon_word(cv: Canvas, word: str, x: int, y: int, k: int, tube=MAGENTA, core=(255, 214, 246),
              font=None) -> int:
    """A neon-tube sign: continuous tubes with a hot pale core where the stroke is thick enough,
    and bloom around them."""
    font = font or FONT5
    lit = set()
    cx = x
    for ch in word:
        glyph = font[ch]
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "#":
                    for sy in range(k):
                        for sx in range(k):
                            lit.add((cx + gx * k + sx, y + gy * k + sy))
        cx += (len(glyph[0]) + 1) * k
    for (px, py) in lit:
        inner = k >= 3 and all(n in lit for n in ((px - 1, py), (px + 1, py), (px, py - 1), (px, py + 1),
                                                  (px - 1, py - 1), (px + 1, py + 1), (px - 1, py + 1), (px + 1, py - 1)))
        cv.put(px, py, core if inner else tube)
        cv.light(px, py, tube, 1.0)
    return cx - x - k


def banner(w: int, h: int, version: str = "") -> Canvas:
    """The startup scene: a big neon-lit tiger cub over the night city, FORGE as a neon sign."""
    cv = Canvas(w, h)
    horizon = int(h * 0.82)
    s = (horizon - 1) / 80.0          # the head spans about -41..+38 in tiger() units
    size = 84 * s
    tx = 46 * s + 2
    left = int(tx + 46 * s) + 3
    room = w - left - 2
    k = max(1, min(int(h * 0.48 / 7), room // 30))
    word_w = 5 * 6 * k - k
    wy = max(1, int(horizon * 0.46 - 3.5 * k))
    sx = left + max(0, (room - word_w) // 2)
    windows = skyline(cv, horizon, seed=11, moon=(min(w - 4, sx + word_w + max(4, k * 3)), max(3, wy // 2 + 2))
                      if sx + word_w + k * 5 < w else False)
    windows_on(cv, windows, 0.0, motion=False)
    tiger(cv, tx, horizon - 38.5 * s - 1, size)
    neon_word(cv, "FORGE", sx, wy, k)
    if version:
        vy = wy + 7 * k + max(2, k)
        if vy + 5 < horizon:
            cv.text(version, sx + k, vy, CYAN, FONT3, 1, 0.6)
    cv.bloom(radius=max(1, h // 22), strength=0.55)
    street(cv, horizon + 1, 0.0, motion=False)
    return cv


def mascot(h: int) -> Canvas:
    """Just the tiger, on transparent-looking night, for the goodbye screen."""
    w = int(h * 1.25)
    cv = Canvas(w, h)
    cv.gradient([(8, 5, 26), (20, 9, 54), (44, 13, 84)], 0, h)
    tiger(cv, w / 2, h * 0.52, h * 0.94)
    cv.bloom(radius=1, strength=0.5)
    return cv


# what the terminal can show ------------------------------------------------------------------
def graphics(console=None) -> str:
    """'sixel', 'blocks' or 'plain' for this terminal (FORGE_GRAPHICS overrides)."""
    pick = os.environ.get("FORGE_GRAPHICS", "auto").strip().lower()
    if pick in ("sixel", "blocks", "plain"):
        return pick
    if os.environ.get("NO_COLOR"):
        return "plain"
    if console is not None:
        if not console.is_terminal or console.color_system not in ("truecolor", "256"):
            return "plain"
    elif not sys.stdout.isatty():
        return "plain"
    if os.environ.get("WT_SESSION") and os.environ.get("TERM_PROGRAM", "").lower() != "vscode":
        return "sixel"   # Windows Terminal 1.22+ draws sixel images
    term = (os.environ.get("TERM_PROGRAM", "") + " " + os.environ.get("TERM", "")).lower()
    if any(t in term for t in ("wezterm", "foot", "mlterm", "contour", "iterm")):
        return "sixel"
    return "blocks"
