"""The start screen's picture in real pixels (sixel), for terminals that show images.

splash.py draws the whole start screen as half-block cells with the text on top. Where the terminal
shows sixel images, this paints the same picture (midnight city, moon, metallic FORGE, the tiger's
neon frame) at full pixel resolution and lays it over the cells. The rectangles where text sits are
left transparent, so the real text (and its dark panel) shows through, crisp and selectable.

The picture only depends on the layout, so it is cached in ~/.forge/cache and drawn once per size."""
from __future__ import annotations

import hashlib
import math
import os
import pathlib
import random

from . import art
from .courier_hd import T, encode

VERSION = 3          # bump when the drawing changes, so cached pictures are redrawn

SKY = [(5, 3, 16), (10, 5, 30), (20, 8, 50), (36, 12, 78), (62, 18, 108), (96, 26, 132)]
FAR, MID, NEAR = (34, 18, 70), (21, 11, 46), (10, 6, 22)
NEON, VIOLET, CYAN = (255, 79, 216), (150, 90, 255), (96, 228, 255)
METAL = [(255, 120, 232), (222, 150, 255), (150, 190, 255), (96, 236, 255)]


def _mix(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def _ramp(stops, t):
    n = len(stops) - 1
    t = min(max(t, 0.0), 1.0) * n
    i = min(n - 1, int(t))
    return _mix(stops[i], stops[i + 1], t - i)


class _Pic:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.px = [[(0.0, 0.0, 0.0)] * w for _ in range(h)]

    def fill(self, x0, y0, x1, y1, c):
        x0, x1 = max(0, int(x0)), min(self.w, int(x1))
        if x1 <= x0:
            return
        for y in range(max(0, int(y0)), min(self.h, int(y1))):
            self.px[y][x0:x1] = [c] * (x1 - x0)

    def blend(self, x, y, c, a):
        if 0 <= x < self.w and 0 <= y < self.h and a > 0:
            self.px[y][x] = _mix(self.px[y][x], c, min(1.0, a))

    def glow_rect(self, x0, y0, x1, y1, c, r, a):
        """A soft light around a rectangle, fading over r pixels."""
        for y in range(max(0, int(y0 - r)), min(self.h, int(y1 + r))):
            dy = max(y0 - y, 0, y - y1 + 1)
            for x in range(max(0, int(x0 - r)), min(self.w, int(x1 + r))):
                dx = max(x0 - x, 0, x - x1 + 1)
                if dx or dy:
                    d = math.hypot(dx, dy)
                    if d < r:
                        self.blend(x, y, c, a * (1 - d / r) ** 2)


# the city -----------------------------------------------------------------------------------------
def _sky(p: _Pic, rnd: random.Random):
    for y in range(p.h):
        t = y / max(1, p.h - 1)
        c = _ramp(SKY, t)
        p.px[y] = [c] * p.w
    for _ in range(p.w * p.h // 2600):                          # stars, a few with a sparkle
        x, y = rnd.randrange(p.w), rnd.randrange(int(p.h * 0.55))
        b = rnd.choice((0.35, 0.5, 0.7, 1.0))
        p.blend(x, y, (240, 228, 255), b)
        if b == 1.0 and rnd.random() < 0.3:
            for d in (1, 2, 3):
                for dx, dy in ((d, 0), (-d, 0), (0, d), (0, -d)):
                    p.blend(x + dx, y + dy, (220, 200, 255), 0.6 / d)
    for _ in range(4):                                          # thin haze streaks
        y = rnd.randrange(int(p.h * 0.15), int(p.h * 0.5))
        x0 = rnd.randrange(p.w)
        ln = rnd.randrange(p.w // 6, p.w // 3)
        for x in range(x0, min(p.w, x0 + ln)):
            f = math.sin((x - x0) / ln * math.pi)
            for dy in range(-2, 3):
                p.blend(x, y + dy, (90, 40, 140), 0.10 * f * (1 - abs(dy) / 3))


def _moon(p: _Pic, cx, cy, r, rnd: random.Random):
    R = r * 3.2
    for y in range(max(0, int(cy - R)), min(p.h, int(cy + R) + 1)):
        for x in range(max(0, int(cx - R)), min(p.w, int(cx + R) + 1)):
            d = math.hypot(x - cx, y - cy)
            if d > r:
                if d < R:
                    p.blend(x, y, (150, 60, 200), 0.42 * ((R - d) / (R - r)) ** 2.2)
                continue
            # a lit sphere: light from the upper left, soft lavender, darker limb
            nx, ny = (x - cx) / r, (y - cy) / r
            nz = math.sqrt(max(0.0, 1 - nx * nx - ny * ny))
            lum = max(0.0, -0.45 * nx - 0.4 * ny + 0.8 * nz)
            c = _mix((150, 96, 200), (250, 228, 255), min(1.0, 0.25 + 0.85 * lum))
            edge = r - d
            if edge < 1.5:                                       # anti-aliased rim
                c = _mix(p.px[y][x], c, edge / 1.5)
            p.px[y][x] = c
    for _ in range(9):                                          # craters
        a, dist = rnd.uniform(0, 2 * math.pi), rnd.uniform(0, r * 0.7)
        kx, ky, kr = cx + math.cos(a) * dist, cy + math.sin(a) * dist, rnd.uniform(r * 0.07, r * 0.2)
        for y in range(int(ky - kr), int(ky + kr) + 1):
            for x in range(int(kx - kr), int(kx + kr) + 1):
                d = math.hypot(x - kx, y - ky)
                if d < kr and math.hypot(x - cx, y - cy) < r - 1:
                    p.blend(x, y, (170, 120, 210), 0.35 * (1 - d / kr))


def _buildings(p: _Pic, rnd: random.Random):
    h = p.h
    layers = (  # (min height, max height, colour, width range, window chance, window colours)
        (0.45, 0.88, FAR, (26, 64), 0.10, [(110, 86, 180), (140, 104, 210)]),
        (0.30, 0.66, MID, (34, 80), 0.08, [(200, 150, 255), (255, 186, 120), (120, 200, 255)]),
        (0.12, 0.44, NEAR, (40, 100), 0.07, [(255, 186, 110), (255, 186, 110), CYAN, NEON]),
    )
    for li, (lo, hi, col, (wmin, wmax), chance, wins) in enumerate(layers):
        x = -rnd.randrange(20)
        while x < p.w:
            bw = rnd.randrange(wmin, wmax)
            top = int(h - h * rnd.uniform(lo, hi))
            p.fill(x, top, x + bw, h, col)
            rim = _mix(col, (150, 90, 230), 0.35)                # moonlit edge
            p.fill(x, top, x + 2, h, rim)
            if rnd.random() < 0.4:                               # set-back roof
                inset = bw // 4
                p.fill(x + inset, top - 10, x + bw - inset, top, col)
                top2 = top - 10
            else:
                top2 = top
            if rnd.random() < 0.3:                               # antenna + red light
                ax = x + bw // 2
                p.fill(ax, top2 - 18, ax + 2, top2, col)
                p.fill(ax - 1, top2 - 21, ax + 3, top2 - 17, (255, 70, 110))
                p.glow_rect(ax - 1, top2 - 21, ax + 3, top2 - 17, (255, 70, 110), 6, 0.35)
            for wy in range(top + 8, h - 6, 12):                 # window grid
                for wx in range(x + 6, x + bw - 6, 9):
                    if rnd.random() < chance:
                        c = rnd.choice(wins)
                        p.fill(wx, wy, wx + 4, wy + 6, c)
                        if li == 2:
                            p.glow_rect(wx, wy, wx + 4, wy + 6, c, 3, 0.18)
            if li == 2 and rnd.random() < 0.2 and h - top > 40:  # a neon strip down one edge
                nx = x + 3 if rnd.random() < 0.5 else x + bw - 5
                ny1 = min(h - 4, top + rnd.randrange(30, 70))
                c = rnd.choice((NEON, CYAN))
                p.fill(nx, top + 4, nx + 2, ny1, c)
                p.glow_rect(nx, top + 4, nx + 2, ny1, c, 7, 0.4)
            x += bw + rnd.randrange(2, 14)
    for y in range(h - h // 3, h):                              # magenta haze near the street
        t = (y - (h - h // 3)) / max(1, h // 3)
        row = p.px[y]
        for x in range(0, p.w):
            row[x] = _mix(row[x], (150, 40, 170), 0.20 * t * t)
    for x in range(p.w):                                        # neon street line
        c = _mix(NEON, VIOLET, x / max(1, p.w - 1))
        for dy in range(3):
            p.px[h - 1 - dy][x] = _mix(p.px[h - 1 - dy][x], c, (0.9, 0.6, 0.3)[dy])
        for dy in range(3, 14):
            p.blend(x, h - 1 - dy, c, 0.25 * (1 - dy / 14) ** 2)


# FORGE and the frame ---------------------------------------------------------------------------
def _forge(p: _Pic, x0: int, y0: int, s: int):
    """FORGE from the 5x7 pixel font, each font pixel an s x s block: metallic pink to cyan, polished
    top, bevelled edges, a violet shadow and a soft glow."""
    on = set()
    cx = 0
    for ch in "FORGE":
        for gy, row in enumerate(art.FONT5[ch]):
            for gx, b in enumerate(row):
                if b != ".":
                    on.add((cx + gx, gy))
        cx += 6
    fw = (cx - 1) * s
    for gx, gy in on:                                           # glow, then shadow
        p.glow_rect(x0 + gx * s, y0 + gy * s, x0 + (gx + 1) * s, y0 + (gy + 1) * s, (170, 60, 255), s * 0.9, 0.16)
    off = max(2, s // 4)
    for gx, gy in on:
        p.fill(x0 + gx * s + off, y0 + gy * s + off, x0 + (gx + 1) * s + off, y0 + (gy + 1) * s + off, (34, 8, 62))
    H = 7 * s
    bev = max(1, s // 6)
    for gx, gy in on:
        for y in range(y0 + gy * s, y0 + (gy + 1) * s):
            if not 0 <= y < p.h:
                continue
            v = (y - y0) / max(1, H - 1)
            for x in range(x0 + gx * s, x0 + (gx + 1) * s):
                if not 0 <= x < p.w:
                    continue
                c = _ramp(METAL, (x - x0) / max(1, fw - 1))
                if v < 0.32:
                    c = _mix(c, (255, 255, 255), 0.55 * (1 - v / 0.32))
                elif 0.46 < v < 0.52:
                    c = _mix(c, (255, 255, 255), 0.25)              # a chrome line across
                elif v > 0.7:
                    c = _mix(c, (60, 20, 110), 0.4 * (v - 0.7) / 0.3)
                lx, ly = x - x0 - gx * s, y - y0 - gy * s
                if ly < bev and (gx, gy - 1) not in on:
                    c = _mix(c, (255, 255, 255), 0.5)             # lit top bevel
                elif ly >= s - bev and (gx, gy + 1) not in on:
                    c = _mix(c, (40, 10, 80), 0.45)               # shaded bottom bevel
                elif lx < bev and (gx - 1, gy) not in on:
                    c = _mix(c, (255, 255, 255), 0.25)
                elif lx >= s - bev and (gx + 1, gy) not in on:
                    c = _mix(c, (40, 10, 80), 0.3)
                p.px[y][x] = c


def _frame(p: _Pic, x0, y0, x1, y1):
    """The tiger's neon frame: 3 pixels of pink to violet with a soft glow."""
    p.glow_rect(x0 - 3, y0 - 3, x1 + 3, y1 + 3, (190, 60, 255), 14, 0.45)
    p.fill(x0 - 2, y0 - 2, x1 + 2, y1 + 2, (8, 4, 18))
    for y in range(y0 - 6, y1 + 6):
        for x in range(x0 - 6, x1 + 6):
            if x0 - 6 <= x < x0 - 3 or x1 + 3 <= x < x1 + 6 or y0 - 6 <= y < y0 - 3 or y1 + 3 <= y < y1 + 6:
                t = ((x - x0) / max(1, x1 - x0) + (y - y0) / max(1, y1 - y0)) / 2
                if 0 <= x < p.w and 0 <= y < p.h:
                    p.px[y][x] = _mix(NEON, VIOLET, min(1.0, max(0.0, t)))


# pixels to sixel -----------------------------------------------------------------------------------
def _sixel(p: _Pic, holes) -> bytes:
    """Dither to at most 254 colours and encode; `holes` (x0, y0, x1, y1) stay transparent."""
    for shift in (3, 4, 5):
        pal: dict = {}
        idx = bytearray(p.w * p.h)
        q = 1 << shift
        ok = True
        for y in range(p.h):
            row = p.px[y]
            brow = art.BAYER4[y % 4]
            base = y * p.w
            for x in range(p.w):
                r, g, b = row[x]
                d = (brow[x % 4] + 0.5) / 16 * q - q / 2
                key = (min(255, max(0, int(r + d))) >> shift, min(255, max(0, int(g + d))) >> shift,
                       min(255, max(0, int(b + d))) >> shift)
                i = pal.get(key)
                if i is None:
                    if len(pal) >= 254:
                        ok = False
                        break
                    i = pal[key] = len(pal)
                idx[base + x] = i
            if not ok:
                break
        if ok:
            break
    colours = [None] * 255
    for (r, g, b), i in pal.items():
        colours[i] = (min(255, r * q + q // 2), min(255, g * q + q // 2), min(255, b * q + q // 2))
    for x0, y0, x1, y1 in holes:
        for y in range(max(0, y0), min(p.h, y1)):
            idx[y * p.w + max(0, x0):y * p.w + min(p.w, x1)] = bytes([T]) * (min(p.w, x1) - max(0, x0))
    colours = [c or (0, 0, 0) for c in colours]
    return encode(bytes(idx), p.w, p.h, colours)


def draw(L: dict, veil, cell=(10, 20)) -> _Pic:
    """The picture's pixels for layout L (see picture())."""
    cw, chh = cell
    w, h, n, k = L["w"], L["h"], L["n"], L["k"]
    p = _Pic(w * cw, h * chh)
    rnd = random.Random(11)
    _sky(p, rnd)
    _moon(p, p.w * 0.93, p.h * 0.24, p.h * 0.13, rnd)
    _buildings(p, rnd)
    tx, ty = L["tx"], L["ty"]
    _frame(p, tx * cw, ty * chh, (tx + 2 * n) * cw, (ty + n) * chh)
    s = k * cw                                                  # one font pixel = one half-block
    _forge(p, L["rx"] * cw, (2 if k == 2 else 3) * chh // 2, s)
    c0, r0, c1, r1 = veil
    px0, px1, py0, py1 = int(c0 * cw), int(c1 * cw), int(r0 * chh), int(r1 * chh)
    for y in range(max(0, py0), min(p.h, py1)):
        row = p.px[y]
        for x in range(max(0, px0), min(p.w, px1)):
            a = min(1.0, (px1 - x) / (4 * cw), (x - px0 + cw) / (2 * cw))
            row[x] = _mix(row[x], (12, 6, 28), a)
    return p


def picture(L: dict, veil, cell=(10, 20)) -> str:
    """The sixel for layout L. `veil` (col0, row0, col1, row1, in cells) is the dark panel the
    details sit on; it fades out at its edges like the half-block version."""
    cw, chh = cell
    key = repr((VERSION, cell, sorted(L.items()), veil))
    cache = pathlib.Path(os.environ.get("FORGE_HOME") or pathlib.Path.home() / ".forge") / "cache"
    f = cache / f"splash-{hashlib.sha1(key.encode()).hexdigest()[:16]}.six"
    try:
        return f.read_text(encoding="ascii")
    except Exception:
        pass
    p = draw(L, veil, cell)
    out = _sixel(p, []).decode("ascii")
    try:
        cache.mkdir(parents=True, exist_ok=True)
        for old in cache.glob("splash-*.six"):                 # keep the cache small
            if old != f and len(list(cache.glob("splash-*.six"))) > 6:
                old.unlink(missing_ok=True)
        f.write_text(out, encoding="ascii")
    except Exception:
        pass
    return out
