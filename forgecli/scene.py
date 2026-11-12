"""The start-up picture: the tiger on the rooftop, FORGE over the neon city.

Baked ahead of time (tools/banner/bake_scene.py) at a few terminal widths into scene_data.bin,
255 indexed colours each, edges fading to transparent so it melts into the terminal. Shown as a
real-pixel sixel image where the terminal supports it, as half-block characters elsewhere."""
from __future__ import annotations

import lzma
import struct
from pathlib import Path

from .courier_hd import T, encode

_DATA = Path(__file__).with_name("scene_data.bin")
_sizes: list | None = None
_six: dict = {}


def _load() -> list:
    """[(cols, w, h, palette, indexed pixels)], smallest first."""
    global _sizes
    if _sizes is None:
        raw = lzma.decompress(_DATA.read_bytes())
        out, i = [], 0
        while i < len(raw):
            cols, w, h = struct.unpack_from("<HHH", raw, i)
            i += 6
            pal = [tuple(raw[i + k * 3:i + k * 3 + 3]) for k in range(255)] + [(0, 0, 0)]
            i += 255 * 3
            out.append((cols, w, h, pal, raw[i:i + w * h]))
            i += w * h
        _sizes = out
    return _sizes


def pick(cols: int):
    """The biggest baked size that fits in cols terminal columns."""
    sizes = _load()
    fit = [s for s in sizes if s[0] <= cols]
    return fit[-1] if fit else sizes[0]


def rows_for(cols: int, cell_h: int = 20) -> int:
    _, w, h, _, _ = pick(cols)
    return -(-h // cell_h)


def sixel(cols: int) -> str:
    c, w, h, pal, idx = pick(cols)
    if c not in _six:
        _six[c] = encode(idx, w, h, pal).decode("ascii")
    return _six[c]


def halfblock(cols: int) -> str:
    """Two pixels per character cell, sampled from the nearest baked size."""
    _, w, h, pal, idx = pick(cols)
    W = min(cols, w)
    H = max(2, round(h * W / w * 10 / 20) * 2)        # cells are about twice as tall as wide
    lines = []
    for r in range(H // 2):
        parts = []
        for c in range(W):
            x = min(w - 1, int((c + 0.5) * w / W))
            t = idx[min(h - 1, int((r * 2 + 0.5) * h / H)) * w + x]
            b = idx[min(h - 1, int((r * 2 + 1.5) * h / H)) * w + x]
            if t == T and b == T:
                parts.append("\x1b[0m ")
            elif b == T:
                p = pal[t]
                parts.append(f"\x1b[0;38;2;{p[0]};{p[1]};{p[2]}m▀")
            elif t == T:
                p = pal[b]
                parts.append(f"\x1b[0;38;2;{p[0]};{p[1]};{p[2]}m▄")
            else:
                p, q = pal[t], pal[b]
                parts.append(f"\x1b[38;2;{p[0]};{p[1]};{p[2]};48;2;{q[0]};{q[1]};{q[2]}m▀")
        lines.append("".join(parts) + "\x1b[0m")
    return "\n".join(lines)
