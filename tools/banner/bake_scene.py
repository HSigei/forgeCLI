"""Bake tools/banner/scene.png into forgecli/scene_data.bin: the start-up picture at several
terminal widths, each as 255 indexed colours (index 255 = transparent), lzma-compressed.
Dev-only (Pillow). Run: python tools/banner/bake_scene.py"""
import lzma
import pathlib
import struct

from PIL import Image

HERE = pathlib.Path(__file__).parent
OUT = HERE.parent.parent / "forgecli" / "scene_data.bin"
COLS = (70, 90, 110, 130, 150, 170)       # terminal columns the picture is made for
CELL_W = 10                                # pixels per terminal column (Windows Terminal default)
BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]

src = Image.open(HERE / "scene.png").convert("RGB")
blob = b""
for cols in COLS:
    w = cols * CELL_W
    h = round(src.height * w / src.width)
    h -= h % 6                                     # whole sixel bands
    im = src.resize((w, h), Image.LANCZOS)
    q = im.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = q.getpalette()[:255 * 3]
    pal += [0] * (255 * 3 - len(pal))
    idx = bytearray(q.tobytes())
    fade_x, fade_y = max(12, w // 40), max(10, h // 7)
    for y in range(h):
        for x in range(w):
            a = min(1.0, min(x, w - 1 - x) / fade_x, (h - 1 - y) / fade_y)
            if a < 1.0 and (BAYER[y % 4][x % 4] + 0.5) / 16 > a:
                idx[y * w + x] = 255
    blob += struct.pack("<HHH", cols, w, h) + bytes(pal) + bytes(idx)
    print(cols, w, h)
OUT.write_bytes(lzma.compress(blob, preset=9 | lzma.PRESET_EXTREME))
print(OUT, OUT.stat().st_size // 1024, "KB")
