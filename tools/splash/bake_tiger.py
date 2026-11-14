"""Bake tools/splash/tiger.png into forgecli/tiger_data.bin: the start screen's tiger at a few box
sizes (n terminal rows tall, 2n columns wide = a square of 20n x 20n pixels in Windows Terminal),
255 indexed colours each, lzma-compressed. Dev-only (Pillow). Run: python tools/splash/bake_tiger.py"""
import lzma
import pathlib
import struct

from PIL import Image

HERE = pathlib.Path(__file__).parent
OUT = HERE.parent.parent / "forgecli" / "tiger_data.bin"
ROWS = (6, 8, 10, 12, 14, 16)          # box heights in terminal rows
CELL = 20                              # pixels per terminal row (and per two columns)

src = Image.open(HERE / "tiger.png").convert("RGB")
blob = b""
for n in ROWS:
    side = n * CELL
    im = src.resize((side, side), Image.LANCZOS)
    q = im.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = q.getpalette()[:255 * 3]
    pal += [0] * (255 * 3 - len(pal))
    blob += struct.pack("<HH", n, side) + bytes(pal) + q.tobytes()
    print(n, side)
OUT.write_bytes(lzma.compress(blob, preset=9 | lzma.PRESET_EXTREME))
print(OUT, OUT.stat().st_size // 1024, "KB")
