"""The neon courier at full resolution: real pixels (sixel) instead of half-block characters.

Windows Terminal (and other sixel terminals) can show images. The live status above the message
box is drawn by prompt_toolkit, which only knows text, so the status keeps a few blank rows for the
lane and, right after each prompt_toolkit redraw, Overlay paints the pile, the door, the road and
the courier into those rows as sixel images. Frames are baked from the original GIF at up to its own
size (courier_hd_data.py + courier_hd_data.bin, lzma, no Pillow at runtime); encoded images are
cached, so a redraw is only a write.

Barrow (city.py) drives what he does; courier.py is the half-block fallback for other terminals.
FORGE_COURIER_HD=off forces the fallback, =on forces HD; FORGE_CELL_PX=10x20 sets the pixel size of
a character cell (Windows Terminal maps sixel images to 10x20 per cell whatever the font).
"""
from __future__ import annotations

import lzma
import os
import pathlib
import re
import threading
import time

from .courier_hd_data import CELL_H, FRAMES, ROAD_RGB, SIZES

T = 255                      # transparent index
FRAME_SECS = 0.14            # the GIF's own frame time
DOOR_RGB = (156, 60, 140)
DOOR_LIT = (255, 79, 216)
OK_RGB = (125, 255, 154)
BAD_RGB = (255, 95, 109)
PILE_KEEP = 0.9

_BIN = pathlib.Path(__file__).with_name("courier_hd_data.bin")
_lock = threading.Lock()
_sizes: dict = {}
_cache: dict = {}


def cell_px() -> tuple[int, int]:
    v = os.environ.get("FORGE_CELL_PX", "")
    m = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", v)
    if m:
        return max(4, int(m.group(1))), max(8, int(m.group(2)))
    return 10, CELL_H


def enabled() -> bool:
    """HD when asked, or by default in Windows Terminal (it shows sixel images)."""
    v = os.environ.get("FORGE_COURIER_HD", "").strip().lower()
    if v in ("0", "off", "no", "false"):
        return False
    if os.environ.get("FORGE_COURIER", "").strip().lower() in ("0", "off", "no", "false"):
        return False
    if v in ("1", "on", "yes", "true"):
        return True
    if os.environ.get("FORGE_GRAPHICS", "").strip().lower() in ("blocks", "plain", "text"):
        return False
    return bool(os.environ.get("WT_SESSION")) and _BIN.exists()


def pick_rows(term_h: int, term_w: int) -> int:
    """Biggest baked size that fits: about an eighth of the window's height, and the lane in its width."""
    sizes = sorted(SIZES)
    want = os.environ.get("FORGE_COURIER_ROWS", "").strip()
    if want.isdigit():
        n = int(want)
        rows = max([s for s in sizes if s <= n] or [sizes[0]])
    else:
        rows = max([s for s in sizes if s <= max(sizes[0], term_h // 8)] or [sizes[0]])
    while rows > sizes[0] and lane_cells(rows) + 4 > term_w:
        rows = max(s for s in sizes if s < rows)
    return rows


def _load(rows: int):
    """(w, h, pw, ph, road, palette, frames[(full, empty)], pile) for one size."""
    with _lock:
        got = _sizes.get(rows)
        if got:
            return got
        w, h, pw, ph, road, off, n = SIZES[rows]
        with open(_BIN, "rb") as fh:
            fh.seek(off)
            raw = lzma.decompress(fh.read(n))
        pal = [tuple(raw[i:i + 3]) for i in range(0, 255 * 3, 3)]
        p, size = 255 * 3, w * h
        frames = []
        for _ in range(FRAMES):
            full = raw[p:p + size]
            empty = raw[p + size:p + 2 * size]
            frames.append((full, empty))
            p += 2 * size
        pile = raw[p:p + pw * ph]
        got = (w, h, pw, ph, road, pal, frames, pile)
        _sizes[rows] = got
        return got


def geometry(rows: int) -> dict:
    """Where things sit in the lane, in cells: pile, travel, sprite, door."""
    cw, ch = cell_px()
    w, h, pw, ph = SIZES[rows][:4]
    sprite_c = -(-w // cw)
    pile_c = -(-pw // cw) + 1
    door_c = max(4, -(-rows * ch // 3 // cw) // 2 + 2)
    travel_c = max(6, sprite_c // 2 + 6)
    return {"cw": cw, "ch": ch, "sprite_c": sprite_c, "pile_c": pile_c, "door_c": door_c,
            "travel_c": travel_c, "lane_c": pile_c + travel_c + sprite_c + door_c}


def lane_cells(rows: int) -> int:
    return geometry(rows)["lane_c"]


# ---------------------------------------------------------------- sixel encoding

_TO_CHAR = bytes(range(63, 127)) + bytes(192)
_RLE = re.compile(rb"(.)\1{3,}")
_TABLES: dict = {}


def _table(c: int, k: int) -> bytes:
    key = (c, k)
    t = _TABLES.get(key)
    if t is None:
        b = bytearray(256)
        b[c] = 1 << k
        t = _TABLES[key] = bytes(b)
    return t


def _rle(m: re.Match) -> bytes:
    run = m.group(0)
    return b"!%d%s" % (len(run), run[:1])


def encode(idx: bytes, w: int, h: int, pal) -> bytes:
    """Indexed pixels (T = transparent) to a sixel image with a transparent background."""
    out = [b'\x1bP0;1;0q"1;1;%d;%d' % (w, h)]
    used = sorted(set(idx) - {T})
    for c in used:
        r, g, b = pal[c]
        out.append(b"#%d;2;%d;%d;%d" % (c, r * 100 // 255, g * 100 // 255, b * 100 // 255))
    for y0 in range(0, h, 6):
        rows = [idx[y * w:(y + 1) * w] for y in range(y0, min(h, y0 + 6))]
        here = set()
        for r in rows:
            here.update(r)
        here.discard(T)
        first = True
        for c in sorted(here):
            acc = 0
            for k, r in enumerate(rows):
                acc |= int.from_bytes(r.translate(_table(c, k)), "big")
            line = acc.to_bytes(w, "big").translate(_TO_CHAR).rstrip(b"?")
            if not line:
                continue
            out.append((b"" if first else b"$") + b"#%d" % c + _RLE.sub(_rle, line))
            first = False
        out.append(b"-")
    out.append(b"\x1b\\")
    return b"".join(out)


def _dimmed(pal, k: float):
    return [(int(r * k), int(g * k), int(b * k)) for r, g, b in pal]


# ---------------------------------------------------------------- the pieces

def courier_image(rows: int, frame: int, flip: bool, fill: int, dim: bool, spilled: bool) -> tuple[bytes, int, int]:
    """The kid and his barrow; fill 0..4 = how full the tub is (parcels appear from the bottom)."""
    key = ("c", rows, frame, flip, fill, dim, spilled)
    got = _cache.get(key)
    if got:
        return got
    w, h, _, _, _, pal, frames, _ = _load(rows)
    full, empty = frames[frame % len(frames)]
    if fill >= 4 and not spilled:
        px = bytearray(full)
    elif fill <= 0 or spilled:
        px = bytearray(empty)
    else:
        # parcel pixels are where the full frame has something the empty one does not
        px = bytearray(empty)
        rows_with = [y for y in range(h) if any(full[y * w + x] != T and empty[y * w + x] == T for x in range(w))]
        if rows_with:
            top, bot = rows_with[0], rows_with[-1]
            show_from = bot - (bot - top + 1) * fill / 4
            for y in rows_with:
                if y < show_from:
                    continue
                for x in range(y * w, (y + 1) * w):
                    if empty[x] == T and full[x] != T:
                        px[x] = full[x]
    if flip:
        px = bytearray(b"".join(bytes(px[y * w:(y + 1) * w])[::-1] for y in range(h)))
    got = (encode(bytes(px), w, h, _dimmed(pal, 0.55) if dim else pal), w, h)
    if len(_cache) > 600:
        _cache.clear()
    _cache[key] = got
    return got


def pile_image(rows: int, dim: bool, red: bool) -> tuple[bytes, int, int]:
    key = ("p", rows, dim, red)
    got = _cache.get(key)
    if got:
        return got
    _, _, pw, ph, _, pal, _, pile = _load(rows)
    p = pal
    if red:
        p = [(min(255, int(r * 0.5 + 128)), int(g * 0.35), int(b * 0.35)) for r, g, b in pal]
    if dim:
        p = _dimmed(p, 0.55)
    # Sit the pile on the road: pad with transparent rows above it.
    road_y = SIZES[rows][4][0]
    hh = max(ph, road_y)
    px = bytes([T]) * (pw * (hh - ph)) + bytes(pile)
    got = (encode(px, pw, hh, p), pw, hh)
    _cache[key] = got
    return got


def door_image(rows: int, state: str, lit: bool) -> tuple[bytes, int, int]:
    """A neon doorway; green with a tick when done, red when it failed, glowing while a tool runs."""
    key = ("d", rows, state, lit)
    got = _cache.get(key)
    if got:
        return got
    g = geometry(rows)
    w = (g["door_c"] - 1) * g["cw"]
    lane_h = rows * g["ch"]
    road_y = SIZES[rows][4][0]
    h = min(lane_h, max(24, int(lane_h * 0.62)))
    top = max(0, road_y - h)
    rgb = OK_RGB if state == "completed" else BAD_RGB if state == "failed" else DOOR_LIT if lit else DOOR_RGB
    pal = [rgb, tuple(v // 3 for v in rgb)]
    t = max(2, w // 12)
    full_h = road_y
    px = bytearray([T]) * (w * full_h)
    for y in range(top, full_h):
        for x in range(w):
            edge = y < top + t or x < t or x >= w - t
            if edge:
                px[y * w + x] = 0
            elif lit or state in ("completed", "failed"):
                px[y * w + x] = 1
    if state == "completed":                       # a tick in the doorway
        cx, cy, s = w // 2, top + (full_h - top) * 2 // 3, max(3, w // 6)
        pts = [(cx - s + i, cy - s + i) for i in range(s)] + [(cx + i, cy - i) for i in range(2 * s)]
        for (x, y) in pts:
            for dy in range(-t // 2, t // 2 + 1):
                for dx in range(-t // 2, t // 2 + 1):
                    if 0 <= x + dx < w and 0 <= y + dy < full_h:
                        px[(y + dy) * w + x + dx] = 0
    got = (encode(bytes(px), w, full_h, pal), w, full_h)
    _cache[key] = got
    return got


def road_image(rows: int, cells: int) -> tuple[bytes, int, int]:
    """The dashed neon road under the whole lane (static, like the GIF's)."""
    key = ("r", rows, cells)
    got = _cache.get(key)
    if got:
        return got
    g = geometry(rows)
    w = cells * g["cw"]
    road_y, road_h = SIZES[rows][4]
    h = road_y + road_h
    px = bytearray([T]) * (w * h)
    dash, gap = g["cw"] * 3, g["cw"]
    for y in range(road_y, h):
        for x in range(w):
            if (x % (dash + gap)) < dash:
                px[y * w + x] = 0
    got = (encode(bytes(px), w, h, [ROAD_RGB]), w, h)
    _cache[key] = got
    return got


def lane_images(barrow, now: float, rows: int) -> tuple[list, tuple]:
    """[(cell column, sixel)] for everything in the lane right now, and a key that changes when
    the picture does (so unchanged frames are not sent again)."""
    snap = barrow.snapshot(now)
    g = geometry(rows)
    st = snap["state"]
    dim = st in ("waiting", "cancelled")
    failed = st == "failed"
    x = g["pile_c"] + round(snap["pos"] / snap["track"] * g["travel_c"])
    frame = int(now / FRAME_SECS) % FRAMES if snap["moving"] else 0
    fill = round(snap["fill"] * 4)
    flip = not snap["right"]
    lit = snap["at_door"] and (snap["busy"] or snap["parked"])
    pulse = lit and ((int(now * 3) % 2 == 0) or not snap["motion"])
    items = [(0, road_image(rows, g["lane_c"])[0]),
             (0, pile_image(rows, dim, False)[0]),
             (g["lane_c"] - g["door_c"] + 1, door_image(rows, st, pulse)[0])]
    if failed:      # the load tipped out on the road behind him
        items.append((max(0, x - g["pile_c"] + 1), pile_image(rows, False, True)[0]))
    items.append((x, courier_image(rows, frame, flip, fill, dim, failed)[0]))
    key = (rows, x, frame, fill, flip, st, pulse, dim)
    return items, key


# ---------------------------------------------------------------- painting over prompt_toolkit

class Overlay:
    """Paints the lane into the blank rows the status keeps for it, after each prompt_toolkit redraw.

    The UI sets `lane` = (first line of the lane inside the prompt's message, rows, indent) while it
    wants the courier shown, or None. The overlay saves the cursor, moves up from where
    prompt_toolkit left it to the lane, wipes those rows (images there are erased with the cells),
    draws the images, and restores the cursor, so prompt_toolkit never notices.
    """

    MIN_REDRAW = 0.35        # repaint at least this often (prompt_toolkit may have cleared it)

    def __init__(self, ui, app):
        self.ui = ui
        self.app = app
        self.drawn = None        # (row offset above cursor at draw time, rows, indent, cells)
        self.last_key = None
        self.last_at = 0.0
        self.failed = False
        app.after_render += self._after
        r = app.renderer
        self._erase, self._reset = r.erase, r.reset

        def erase(*a, **k):
            self._wipe()
            return self._erase(*a, **k)

        def reset(*a, **k):
            self.drawn = None
            self.last_key = None
            return self._reset(*a, **k)

        r.erase, r.reset = erase, reset

    def _write(self, s: str) -> None:
        out = self.app.output
        out.write_raw(s)
        out.flush()

    def _cursor_y(self) -> int:
        try:
            return self.app.renderer._cursor_pos.y
        except Exception:
            return -1

    def _wipe(self) -> None:
        """Clear what was painted (before prompt_toolkit erases or the lane goes away)."""
        d = self.drawn
        if not d:
            return
        top, rows, indent, cells = d
        up = self._cursor_y() - top
        self.drawn = None
        self.last_key = None
        if up <= 0:
            return
        s = ["\x1b7", f"\x1b[{up}A\r"]
        for i in range(rows):
            s.append(f"\x1b[{indent + 1}G\x1b[{cells}X")
            if i < rows - 1:
                s.append("\x1b[1B")
        s.append("\x1b8")
        try:
            self._write("".join(s))
        except Exception:
            pass

    def _after(self, app) -> None:
        if self.failed:
            return
        try:
            self._paint()
        except Exception:
            self.failed = True      # never break the prompt over a picture
            self.ui.courier_hd_failed = True

    def _paint(self) -> None:
        lane = getattr(self.ui, "hd_lane", None)
        if not lane:
            if self.drawn:
                self._wipe()
            return
        top, rows, indent = lane
        up = self._cursor_y() - top
        try:
            screen = self.app.output.get_size().rows
        except Exception:
            screen = 0
        if up <= 0 or (screen and up >= screen):
            return          # the lane is scrolled off the top: nothing safe to paint
        now = time.monotonic()
        items, key = lane_images(self.ui.barrow, now, rows)
        cells = lane_cells(rows)
        if key == self.last_key and self.drawn == (top, rows, indent, cells) and now - self.last_at < self.MIN_REDRAW:
            return
        s = ["\x1b7", f"\x1b[{up}A\r"]
        for i in range(rows):          # wipe the old picture
            s.append(f"\x1b[{indent + 1}G\x1b[{cells}X")
            if i < rows - 1:
                s.append("\x1b[1B")
        if rows > 1:
            s.append(f"\x1b[{rows - 1}A")
        for col, img in items:
            s.append(f"\x1b[{indent + col + 1}G")
            s.append(img.decode("ascii"))
            s.append("\x1b8\x1b7" + f"\x1b[{up}A")   # back to the lane's first row after each image
        s.append("\x1b8")
        self._write("".join(s))
        self.drawn = (top, rows, indent, cells)
        self.last_key = key
        self.last_at = now


def attach(ui, app) -> Overlay | None:
    """Hook the overlay onto a prompt_toolkit Application if HD is on."""
    if not enabled():
        return None
    ov = Overlay(ui, app)
    ui.courier_overlay = ov
    try:
        ui.barrow.speed = 2.5     # the lane is wider in pixels: walk, don't sprint
    except Exception:
        pass
    return ov
