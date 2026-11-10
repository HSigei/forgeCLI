"""The neon courier: a pixel-art kid pushing a wheelbarrow, drawn with half-block characters.

It replaces the spinner while Forge works and acts out real agent events (the state machine is
city.Barrow): loads parcels at the pile while the model thinks, pushes them to the door when a tool
runs and waits there until it finishes, unloads, walks back empty. Waiting on rate limits he stands
dimmed; done turns the door green with a tick; a failure spills the parcels and turns it red.

The frames come from courier_data.py (baked from the GIF at several sizes); the size follows the
terminal: taller windows get a bigger, sharper courier. FORGE_COURIER=off falls back to the
one-line wheelbarrow; FORGE_COURIER_ROWS=4..12 fixes the size.
"""
from __future__ import annotations

import os

from rich.text import Text

from .courier_data import ALPHA, SIZES

# Parcels keep their own amber/cyan colours, like the original art. Set a kind here to recolour.
KIND_RGB: dict = {}
GROUND = (74, 52, 128)
DOOR = (156, 60, 140)
DOOR_LIT = (255, 79, 216)
OK = (125, 255, 154)
BAD = (255, 95, 109)
STEP_PX = 2.0          # pixels walked per animation frame, so the legs match the ground speed

_cache: dict = {}


def _hex(c) -> str:
    return "#%02x%02x%02x" % c


def _rgb(h: str):
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def frames(rows: int):
    """(w, h, [frame pixels], [cargo masks]) for a size; pixels are rgb tuples or None."""
    got = _cache.get(rows)
    if got:
        return got
    w, h, palette, fr, cargo = SIZES[rows]
    pal = [_rgb(p) for p in palette]
    idx = {ch: i for i, ch in enumerate(ALPHA)}
    px = [[[None if ch == " " else pal[idx[ch]] for ch in row] for row in f] for f in fr]
    masks = [[[ch == "1" for ch in row] for row in m] for m in cargo]
    _cache[rows] = got = (w, h, px, masks)
    return got


def pick_rows(term_h: int, term_w: int) -> int:
    """Courier size for this window: as big as fits without crowding the conversation."""
    env = os.environ.get("FORGE_COURIER_ROWS", "")
    if env.isdigit():
        return max(min(SIZES), min(max(SIZES), int(env)))
    rows = 5 if term_h < 24 else 6 if term_h < 30 else 8 if term_h < 40 else 10 if term_h < 52 else 12
    while rows > min(SIZES) and SIZES[rows][0] * 3 + 40 > term_w:   # lane plus the status text
        rows -= 1
    return rows


def enabled() -> bool:
    return os.environ.get("FORGE_COURIER", "on").lower() not in ("off", "0", "no", "false")


def _tint(c, kind_rgb, a: float = 1.0):
    lum = (0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]) / 255
    k = min(1.0, 0.25 + lum * 1.25) * a
    return tuple(min(255, int(v * k)) for v in kind_rgb)


def scene(barrow, now: float, rows: int) -> list[Text]:
    """The lane (pile, courier, door) as `rows` lines of rich Text."""
    snap = barrow.snapshot(now)
    w, h, px, masks = frames(rows)
    travel = w + 4                                   # how far he walks: a bit more than his length
    pile_w, door_w = 4, 3
    lane_w = pile_w + 1 + travel + w + 1 + door_w
    grid = [[None] * lane_w for _ in range(h)]

    st = snap["state"]
    x0 = pile_w + 1 + round(snap["pos"] / snap["track"] * travel)
    moving = snap["moving"]
    f = int(snap["walked"] * travel / snap["track"] / STEP_PX) % len(px) if moving else 0
    sprite, mask = px[f], masks[f]
    dim = 0.55 if st in ("waiting", "cancelled") else 1.0
    kind_rgb = KIND_RGB.get(snap["kind"])
    fill = snap["fill"]                              # 0..1: how much of the load is in the tub
    cargo_rows = [y for y in range(h) if any(mask[y])]
    top = cargo_rows[0] if cargo_rows else 0
    span = (cargo_rows[-1] - top + 1) if cargo_rows else 1
    show_from = top + span - span * fill             # parcels appear from the bottom up

    # ground: a dashed neon line along the whole lane
    for x in range(lane_w):
        if (x // 2) % 3 != 2:
            grid[h - 1][x] = GROUND
    # the pile of parcels at the left
    pile_rgb = kind_rgb or (255, 150, 60)
    for (px_, py_, bw, bh) in ((0, h - 5, 3, 3), (1, h - 8, 3, 3)):
        for yy in range(max(0, py_), min(h - 1, py_ + bh)):
            for xx in range(px_, min(pile_w, px_ + bw)):
                edge = yy == py_ or xx == px_
                grid[yy][xx] = _tint((255, 200, 120) if edge else (200, 110, 40), pile_rgb, 0.9 * dim)
    # the door at the right
    door_x = lane_w - door_w
    door_h = max(4, int(h * 0.7))
    lit = snap["at_door"] and (snap["busy"] or snap["parked"])
    pulse = (int(now * 3) % 2 == 0) or not snap["motion"]
    door = OK if st == "completed" else BAD if st == "failed" else (DOOR_LIT if lit and pulse else DOOR)
    for yy in range(h - 1 - door_h, h - 1):
        for xx in range(door_x, lane_w):
            if yy == h - 1 - door_h or xx in (door_x, lane_w - 1):
                grid[yy][xx] = door
    if st == "completed":                          # a tick in the doorway
        cx, cy = door_x + 1, h - 3
        for (dx, dy) in ((-1, -1), (0, 0), (1, -1), (2, -2)):
            if 0 <= cy + dy < h and 0 <= cx + dx < lane_w:
                grid[cy + dy][cx + dx] = OK
    # the courier (mirrored when walking back)
    flip = not snap["right"]
    for y in range(h):
        row, mrow = sprite[y], mask[y]
        for x in range(w):
            sx = w - 1 - x if flip else x
            c = row[sx]
            if c is None:
                continue
            if mrow[sx]:
                if st == "failed" or y < show_from:
                    continue
                if kind_rgb:
                    c = _tint(c, kind_rgb)
            if dim < 1:
                c = tuple(int(v * dim) for v in c)
            gx = x0 + x
            if 0 <= gx < lane_w:
                grid[y][gx] = c
    if st == "failed":                             # spilled parcels on the ground behind him
        for i, xx in enumerate(range(x0 - 3, x0 + w + 3, 4)):
            if 0 <= xx < lane_w - door_w:
                grid[h - 2][xx] = BAD if i % 2 else (255, 150, 60)
    if st == "waiting" and snap["motion"]:         # a little z drifting up
        zx, zy = x0 + w // 3, max(0, 1 - int(now * 2) % 2)
        for xx in range(zx, zx + 3):
            if 0 <= xx < lane_w:
                grid[zy][xx] = (160, 150, 220)
    return _to_text(grid, lane_w, h)


def _to_text(grid, w: int, h: int) -> list[Text]:
    lines = []
    styles: dict = {}
    for y in range(0, h - 1, 2):
        t = Text()
        top, bot = grid[y], grid[y + 1]
        for x in range(w):
            a, b = top[x], bot[x]
            if a is None and b is None:
                t.append(" ")
                continue
            key = (a, b)
            st = styles.get(key)
            if st is None:
                if a is not None and b is not None:
                    st = (_hex(a) + " on " + _hex(b), "▀") if a != b else (_hex(a), "█")
                elif a is not None:
                    st = (_hex(a), "▀")
                else:
                    st = (_hex(b), "▄")
                styles[key] = st
            t.append(st[1], style=st[0])
        lines.append(t)
    return lines
