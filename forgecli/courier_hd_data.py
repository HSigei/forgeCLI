"""Baked from the neon courier GIF by bake_hd.py: do not edit by hand.

SIZES[rows] = (sprite_w, sprite_h, pile_w, pile_h, (road_y, road_h), offset, length) into
courier_hd.bin; each slice is lzma of palette (255 rgb) + 8 x (full frame, empty-barrow
frame) + pile, one byte per pixel, index 255 = transparent.
"""

CELL_H = 20
FRAMES = 8
ROAD_RGB = (156, 87, 214)

SIZES = {
    6: (140, 116, 53, 27, (118, 2), 0, 41508),
    8: (187, 154, 70, 36, (157, 2), 41508, 64944),
    10: (233, 193, 88, 46, (196, 3), 106452, 92812),
    12: (280, 232, 105, 55, (235, 3), 199264, 122528),
    14: (327, 270, 123, 64, (275, 4), 321792, 155372),
    16: (373, 309, 141, 73, (314, 4), 477164, 195736),
}
