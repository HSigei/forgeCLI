"""Bake tools/splash/cub.png (the courier cub, transparent background) into forgecli/cub_data.bin:
the start screen's mascot at a few heights (rows terminal rows = 20*rows pixels in Windows
Terminal), 254 colours + index 255 = transparent, lzma-compressed. Dev-only (Pillow).
Run: python tools/splash/bake_cub.py"""
import lzma
import pathlib
import struct

from PIL import Image

HERE = pathlib.Path(__file__).parent
OUT = HERE.parent.parent / "forgecli" / "cub_data.bin"
ROWS = (6, 7, 8, 9, 10, 11, 12, 13, 14)
CELL = 20                                  # pixels per terminal row; a column is 10 pixels

src = Image.open(HERE / "cub.png").convert("RGBA")
src = src.crop(src.getchannel("A").point(lambda a: 255 if a > 40 else 0).getbbox())
blob = b""
for rows in ROWS:
    h = rows * CELL
    w = round(src.width * h / src.height)
    w -= w % 10                            # whole terminal columns
    im = src.resize((w, h), Image.BOX)
    alpha = im.getchannel("A")
    rgb = Image.new("RGB", im.size, (0, 0, 0))
    rgb.paste(im.convert("RGB"), mask=alpha)
    q = rgb.quantize(colors=254, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = q.getpalette()[:254 * 3]
    pal += [0] * (255 * 3 - len(pal))
    idx = bytearray(q.tobytes())
    for i, a in enumerate(alpha.tobytes()):
        if a < 128:
            idx[i] = 255
    blob += struct.pack("<HHH", rows, w, h) + bytes(pal) + bytes(idx)
    print(rows, w, h)
OUT.write_bytes(lzma.compress(blob, preset=9 | lzma.PRESET_EXTREME))
print(OUT, OUT.stat().st_size // 1024, "KB")
