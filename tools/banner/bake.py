import sys, os, lzma, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voxel as v
out = sys.argv[1]
o, c = v.tiger()
t = v.trim(v.render_faces(o, c, scale=2.2, bright=1.15, yaw=-20))
o, c = v.word()
w = v.trim(v.render_faces(o, c, scale=9, yaw=-18, pitch=14, ao=0.0, bright=1.05, rim=False, key=(0, 0.4, 1),
                          side=(0.45, (0.06, 0.0, 0.12))))
blob = b""
for im in (t, w):
    h, wd = im.shape[:2]
    blob += struct.pack("<HH", wd, h) + im.tobytes()
open(out, "wb").write(lzma.compress(blob, preset=9))
print("tiger", t.shape, "word", w.shape, "bytes", os.path.getsize(out))
