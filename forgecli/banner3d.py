"""The start-up scene: a 3D voxel tiger cub and a 3D gradient FORGE over the neon city, drawn
straight onto the terminal background (no box around it).

The tiger and the letters are rendered ahead of time from voxel models (see tools/bake_banner.py)
and stored in banner_data.bin as RGBA pixels; the city is drawn live by art.py. Where the
terminal shows sixel images the scene is real pixels with a transparent background, so it melts
into whatever colour the terminal is; elsewhere it falls back to half-block characters."""
from __future__ import annotations

import hashlib
import lzma
import os
import struct
from pathlib import Path

from . import art
from .courier_hd import T, encode

_DATA = Path(__file__).with_name("banner_data.bin")
_images: list | None = None


def _load() -> list:
    """[(w, h, rgba bytes)] for the tiger and the wordmark."""
    global _images
    if _images is None:
        raw = lzma.decompress(_DATA.read_bytes())
        out, i = [], 0
        while i < len(raw):
            w, h = struct.unpack_from("<HH", raw, i)
            i += 4
            out.append((w, h, raw[i:i + w * h * 4]))
            i += w * h * 4
        _images = out
    return _images


def _city(w: int, h: int, horizon: int):
    """Skyline and wet street at half resolution, plus which pixels are empty sky (transparent)."""
    cv = art.Canvas(w, h)
    windows = art.skyline(cv, horizon, seed=11, moon=(int(w * 0.93), max(3, horizon // 5)))
    sky = art.Canvas(w, h)
    sky.gradient(art.SKY, 0, horizon + 1)
    clear = [[y <= horizon and cv.px[y][x] == sky.px[y][x] for x in range(w)] for y in range(h)]
    art.windows_on(cv, windows, 0.0, motion=False)
    cv.bloom(radius=1, strength=0.45)
    art.street(cv, horizon + 1, 0.0, motion=False)
    return cv, clear


def compose(W: int, H: int) -> tuple[list, list]:
    """The scene at W x H real pixels: (rows of rgb tuples, rows of opaque flags)."""
    (tw, th, tiger), (ww, wh, word) = _load()
    cw, ch = (W + 1) // 2, (H + 1) // 2
    horizon = int(ch * 0.80)
    cv, clear = _city(cw, ch, horizon)
    rgb = [[cv.px[y // 2][x // 2] for x in range(W)] for y in range(H)]
    vis = [[not clear[y // 2][x // 2] for x in range(W)] for y in range(H)]
    # Soft edges without a frame: buildings thin out towards the left and right, the street's
    # reflection fades downwards (ordered dither, because sixel transparency is on/off).
    fade = max(8, W // 9)
    for y in range(H):
        row = vis[y]
        for x in range(W):
            if not row[x]:
                continue
            edge = min(x, W - 1 - x) / fade
            down = 1.0 - max(0.0, (y - horizon * 2) / max(1, H - horizon * 2)) * 0.85
            a = min(1.0, edge) * down
            if a < 1.0 and (art.BAYER4[y % 4][x % 4] + 0.5) / 16 > a:
                row[x] = False
    # The cub sits on the curb at the left, the word floats in the sky beside it.
    tx = max(4, W // 30)
    ty = min(H - th, horizon * 2 + 10 - th)
    _paste(rgb, vis, tiger, tw, th, tx, ty)
    wx = tx + tw + max(16, (W - tx - tw - ww) // 6)
    wy = max(4, int(horizon * 2 * 0.42 - wh / 2))
    if wx + ww < W - 4:
        _paste(rgb, vis, word, ww, wh, wx, wy)
    return rgb, vis


def _paste(rgb, vis, px: bytes, w: int, h: int, x0: int, y0: int) -> None:
    H, W = len(rgb), len(rgb[0])
    for y in range(h):
        yy = y0 + y
        if not 0 <= yy < H:
            continue
        o = y * w * 4
        for x in range(w):
            if px[o + x * 4 + 3] > 127:
                xx = x0 + x
                if 0 <= xx < W:
                    rgb[yy][xx] = (px[o + x * 4], px[o + x * 4 + 1], px[o + x * 4 + 2])
                    vis[yy][xx] = True


def _quantise(rgb, vis) -> tuple[bytes, list]:
    """Up to 254 colours, chosen by popularity on a 5-bit grid, every pixel mapped to the nearest."""
    count: dict = {}
    for row, vrow in zip(rgb, vis):
        for c, v in zip(row, vrow):
            if v:
                k = (c[0] >> 3, c[1] >> 3, c[2] >> 3)
                count[k] = count.get(k, 0) + 1
    keys = sorted(count, key=count.get, reverse=True)
    pal_keys = keys[:254]
    near = {k: i for i, k in enumerate(pal_keys)}
    for k in keys[254:]:
        near[k] = min(range(len(pal_keys)), key=lambda i: (pal_keys[i][0] - k[0]) ** 2 * 3 +
                      (pal_keys[i][1] - k[1]) ** 2 * 4 + (pal_keys[i][2] - k[2]) ** 2 * 2)
    pal = [((k[0] << 3) | 4, (k[1] << 3) | 4, (k[2] << 3) | 4) for k in pal_keys]
    pal += [(0, 0, 0)] * (256 - len(pal))
    idx = bytearray()
    for row, vrow in zip(rgb, vis):
        for c, v in zip(row, vrow):
            idx.append(near[(c[0] >> 3, c[1] >> 3, c[2] >> 3)] if v else T)
    return bytes(idx), pal


def sixel(cols: int, rows: int, cell=(10, 20)) -> str:
    """The scene as a sixel image cols x rows cells big. Built once per size, then cached."""
    W, H = cols * cell[0], rows * cell[1]
    key = hashlib.sha1(_DATA.read_bytes() + f"{W}x{H}v2".encode()).hexdigest()[:12]
    cache = _cache_dir() / f"banner-{key}.six"
    try:
        return cache.read_text(encoding="ascii")
    except OSError:
        pass
    rgb, vis = compose(W, H)
    idx, pal = _quantise(rgb, vis)
    out = encode(idx, W, H, pal).decode("ascii")
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(out, encoding="ascii")
    except OSError:
        pass
    return out


def halfblock(cols: int, rows: int) -> str:
    """The scene in half-block characters (two pixels per cell), for terminals without sixel."""
    W, H = cols * 4, rows * 8                          # compose a little bigger, then sample down
    rgb, vis = compose(W, H)
    lines = []
    for r in range(rows):
        parts = []
        for c in range(cols):
            x = c * 4 + 2
            top, bot = (r * 8 + 2, x), (r * 8 + 6, x)
            t = rgb[top[0]][x] if vis[top[0]][x] else None
            b = rgb[bot[0]][x] if vis[bot[0]][x] else None
            if t is None and b is None:
                parts.append("\x1b[0m ")
            elif b is None:
                parts.append(f"\x1b[0;38;2;{t[0]};{t[1]};{t[2]}m▀")
            elif t is None:
                parts.append(f"\x1b[0;38;2;{b[0]};{b[1]};{b[2]}m▄")
            else:
                parts.append(f"\x1b[38;2;{t[0]};{t[1]};{t[2]};48;2;{b[0]};{b[1]};{b[2]}m▀")
        lines.append("".join(parts) + "\x1b[0m")
    return "\n".join(lines)


def _cache_dir() -> Path:
    try:
        from .config import HOME_DIR
        return Path(HOME_DIR) / "cache"
    except Exception:
        return Path(os.path.expanduser("~")) / ".forge" / "cache"


def tiger_sixel() -> str:
    """Just the 3D cub, real size, transparent background (the goodbye screen)."""
    tw, th, px = _load()[0]
    rgb = [[(0, 0, 0)] * tw for _ in range(th)]
    vis = [[False] * tw for _ in range(th)]
    _paste(rgb, vis, px, tw, th, 0, 0)
    idx, pal = _quantise(rgb, vis)
    return encode(idx, tw, th, pal).decode("ascii")


def tiger_halfblock(rows: int = 12) -> str:
    """The 3D cub in half-blocks, rows lines tall."""
    tw, th, px = _load()[0]
    H = rows * 2
    W = max(1, round(tw * H / th))
    lines = []
    for r in range(rows):
        parts = []
        for c in range(W):
            x = min(tw - 1, int((c + 0.5) * tw / W))
            cells = []
            for yy in (r * 2, r * 2 + 1):
                y = min(th - 1, int((yy + 0.5) * th / H))
                o = (y * tw + x) * 4
                cells.append((px[o], px[o + 1], px[o + 2]) if px[o + 3] > 127 else None)
            t, b = cells
            if t is None and b is None:
                parts.append("\x1b[0m ")
            elif b is None:
                parts.append(f"\x1b[0;38;2;{t[0]};{t[1]};{t[2]}m▀")
            elif t is None:
                parts.append(f"\x1b[0;38;2;{b[0]};{b[1]};{b[2]}m▄")
            else:
                parts.append(f"\x1b[38;2;{t[0]};{t[1]};{t[2]};48;2;{b[0]};{b[1]};{b[2]}m▀")
        lines.append("".join(parts) + "\x1b[0m")
    return "\n".join(lines)
