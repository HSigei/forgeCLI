"""Voxel renderer for Forge's banner: a 3D pixel tiger and 3D gradient FORGE letters.
Dev-only (numpy + Pillow). Produces RGBA images with transparent background."""
import math
import numpy as np
from PIL import Image

N = 72  # grid size


def grid(n=N):
    z, y, x = np.mgrid[0:n, 0:n, 0:n].astype(np.float32) + 0.5
    return x, y, z


def ell(X, Y, Z, c, r):
    return ((X - c[0]) / r[0]) ** 2 + ((Y - c[1]) / r[1]) ** 2 + ((Z - c[2]) / r[2]) ** 2 <= 1.0


def capsule(X, Y, Z, a, b, r):
    a, b = np.array(a, np.float32), np.array(b, np.float32)
    ab = b - a
    t = ((X - a[0]) * ab[0] + (Y - a[1]) * ab[1] + (Z - a[2]) * ab[2]) / float(ab @ ab)
    t = np.clip(t, 0, 1)
    px, py, pz = a[0] + t * ab[0], a[1] + t * ab[1], a[2] + t * ab[2]
    rr = r if np.isscalar(r) else (r[0] + (r[1] - r[0]) * t)
    return (X - px) ** 2 + (Y - py) ** 2 + (Z - pz) ** 2 <= rr ** 2


ORANGE = np.array([236, 118, 34])
ORANGE_D = np.array([196, 84, 22])
CREAM = np.array([250, 236, 214])
BLACK = np.array([28, 14, 26])
PINK = np.array([255, 110, 160])
EYE = np.array([140, 255, 120])
PUPIL = np.array([10, 20, 10])


def tiger():
    X, Y, Z = grid()
    cx = 36
    occ = np.zeros(X.shape, bool)
    col = np.zeros(X.shape + (3,), np.float32)
    parts = {}
    # body (sitting): haunches, upright chest, head
    parts["haunch"] = ell(X, Y, Z, (cx, 12, 28), (15, 11.5, 14))
    parts["chest"] = ell(X, Y, Z, (cx, 25, 36), (11.5, 14, 10))
    parts["neck"] = ell(X, Y, Z, (cx, 34, 37), (10, 7, 9))
    parts["head"] = ell(X, Y, Z, (cx, 45, 40), (13.5, 11.5, 11.5))
    parts["cheekL"] = ell(X, Y, Z, (cx - 10, 40.5, 42), (6.5, 5.5, 7))
    parts["cheekR"] = ell(X, Y, Z, (cx + 10, 40.5, 42), (6.5, 5.5, 7))
    parts["muzzle"] = ell(X, Y, Z, (cx, 40.5, 50), (6.8, 4.8, 4.8))
    parts["chin"] = ell(X, Y, Z, (cx, 36.5, 46.5), (3.8, 2.5, 3.5))
    parts["earL"] = ell(X, Y, Z, (cx - 9.5, 56, 38), (4.2, 4.8, 2.6))
    parts["earR"] = ell(X, Y, Z, (cx + 9.5, 56, 38), (4.2, 4.8, 2.6))
    parts["legL"] = capsule(X, Y, Z, (cx - 5.5, 3, 43), (cx - 5, 26, 38), 3.6)
    parts["legR"] = capsule(X, Y, Z, (cx + 5.5, 3, 43), (cx + 5, 26, 38), 3.6)
    parts["pawL"] = ell(X, Y, Z, (cx - 5.8, 2.4, 45), (4.3, 2.6, 5))
    parts["pawR"] = ell(X, Y, Z, (cx + 5.8, 2.4, 45), (4.3, 2.6, 5))
    parts["hpawL"] = ell(X, Y, Z, (cx - 14, 2.4, 36), (4.5, 2.6, 7))
    parts["hpawR"] = ell(X, Y, Z, (cx + 14, 2.4, 36), (4.5, 2.6, 7))
    # tail curling round the right side onto the floor in front
    tail = np.zeros(X.shape, bool)
    pts = []
    for i in range(23):
        a = i / 22
        ang = math.pi * (0.95 - 1.15 * a)
        pts.append((cx + 17 * math.cos(ang) * -1 + 4, 2.6 + 3 * math.sin(a * math.pi) * 0.4,
                    30 + 15 * math.sin(ang) * 0.9 + a * 8))
    for p, q in zip(pts, pts[1:]):
        tail |= capsule(X, Y, Z, p, q, 2.2)
    parts["tail"] = tail
    for m in parts.values():
        occ |= m
    col[:] = ORANGE
    # shading of fur: slightly darker underneath and on the back
    col[(Y < 9)] = ORANGE_D
    # cream: belly / chest front, muzzle, cheeks, chin, paws, inner ears
    front = Z - 36
    cream = (parts["chest"] & (np.abs(X - cx) < 6.5) & (front > 4)) | parts["muzzle"] | parts["chin"] \
        | ((parts["cheekL"] | parts["cheekR"]) & (Y < 41)) | parts["pawL"] | parts["pawR"] \
        | (parts["head"] & (Y < 43) & (np.abs(X - cx) < 4) & (Z > 46)) \
        | ((parts["earL"] | parts["earR"]) & (Z > 39) & (Y < 58))
    col[cream] = CREAM
    # eye patches cream above the eyes
    for s in (-1, 1):
        col[ell(X, Y, Z, (cx + s * 5.2, 49, 49.5), (2.8, 1.4, 2.5)) & parts["head"]] = CREAM
    # stripes ----------------------------------------------------------------------
    body = parts["haunch"] | parts["chest"] | parts["neck"] | parts["legL"] | parts["legR"]
    side = np.abs(X - cx)
    s1 = np.sin(0.95 * Y + 1.6 * np.sin(0.21 * Z) + 0.35 * side) > 0.72
    s2 = np.sin(0.8 * Z + 0.9 * np.sin(0.3 * Y)) > 0.78
    stripe = body & ~cream & ((s1 & (side > 4.5)) | (parts["haunch"] & s2 & (side > 9)))
    # forehead: three vertical marks + side cheek bands
    head = parts["head"]
    fore = head & (Y > 50) & (Z > 44) & ((np.abs(X - cx) < 0.9) | (np.abs(np.abs(X - cx) - 3.3) < 0.8)) & (Y < 56 - np.abs(X - cx) * 0.4)
    cheek = (head | parts["cheekL"] | parts["cheekR"]) & (side > 9.5) & (np.sin(1.4 * Y - 0.4 * Z) > 0.55) & (Y < 48)
    ear_tip = (parts["earL"] | parts["earR"]) & (Y > 58)
    tail_bands = parts["tail"] & (np.sin(np.arctan2(Z - 36, X - cx) * 7) > 0.4)
    col[stripe | fore | cheek | ear_tip | tail_bands] = BLACK
    # eyes, nose, mouth
    for s in (-1, 1):
        e = ell(X, Y, Z, (cx + s * 5.2, 46.2, 50.4), (2.4, 1.8, 1.6))
        col[e] = EYE
        occ |= e
        col[ell(X, Y, Z, (cx + s * 5.2, 46.0, 51.6), (0.9, 1.7, 0.9))] = PUPIL
        col[ell(X, Y, Z, (cx + s * 5.2 - 1, 47.2, 51.7), (0.6, 0.6, 0.7))] = (255, 255, 255)
    nose = ell(X, Y, Z, (cx, 43, 54.4), (2.5, 1.5, 1.4))
    occ |= nose
    col[nose] = PINK
    col[ell(X, Y, Z, (cx, 39.8, 54.4), (2.6, 0.5, 1.5)) & parts["muzzle"]] = BLACK
    col[ell(X, Y, Z, (cx, 41.3, 54.6), (0.5, 1.3, 1.3)) & parts["muzzle"]] = BLACK
    # toes
    for s in (-1, 1):
        for t in (-1.6, 0, 1.6):
            col[(parts["pawL"] | parts["pawR"]) & (np.abs(X - (cx + s * 5.8 + t)) < 0.35) & (Z > 47) & (Y < 4)] = (200, 180, 160)
    return occ, col


# ---------------------------------------------------------------------------- 3D letters
FONT = {
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "G": [".####", "#....", "#....", "#.###", "#...#", "#...#", ".###."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
}


def word(text="FORGE", depth=3, gap=1):
    cols = sum(len(FONT[c][0]) for c in text) + gap * (len(text) - 1)
    W, H, D = cols + 2, 9, depth + 2
    occ = np.zeros((D, H, W), bool)
    col = np.zeros((D, H, W, 3), np.float32)
    x0 = 1
    for ch in text:
        g = FONT[ch]
        for r, row in enumerate(g):
            for c, v in enumerate(row):
                if v == "#":
                    occ[1:1 + depth, 7 - r + 1, x0 + c] = True
        x0 += len(g[0]) + gap
    a, b = np.array([255, 79, 216]), np.array([62, 240, 255])
    front_z = depth  # last filled z slice faces the camera
    for x in range(W):
        for y in range(H):
            t = min(1, max(0, (x / (W - 1)) * 0.85 + (y / H) * 0.15))
            col[:, y, x] = a + (b - a) * t
    return occ, col


# ---------------------------------------------------------------------------- renderer
def render(occ, col, scale=3.0, yaw=-28, pitch=22, key=(-0.55, 0.7, 0.75), rim=True,
           outline=(16, 8, 26), ssaa=1, face_flat=True, ao=0.55):
    """Orthographic ray-march through the voxel grid. Returns RGBA uint8."""
    D, H, W = occ.shape
    yaw, pitch = math.radians(yaw), math.radians(pitch)
    # camera basis: view dir d (from camera into scene)
    d = np.array([math.sin(yaw) * math.cos(pitch), -math.sin(pitch), -math.cos(yaw) * math.cos(pitch)])
    d = -d  # looking toward -z means camera sits at +z
    d = np.array([-math.sin(yaw) * math.cos(pitch), -math.sin(pitch), -math.cos(yaw) * math.cos(pitch)])
    up0 = np.array([0, 1, 0])
    r = np.cross(d, up0); r /= np.linalg.norm(r)
    u = np.cross(r, d)
    center = np.array([W / 2, H / 2, D / 2])
    # extents: project corners
    corners = np.array([[x, y, z] for x in (0, W) for y in (0, H) for z in (0, D)]) - center
    pr, pu = corners @ r, corners @ u
    s = scale * ssaa
    w_img = int((pr.max() - pr.min()) * s) + 4
    h_img = int((pu.max() - pu.min()) * s) + 4
    jj, ii = np.meshgrid(np.arange(w_img), np.arange(h_img))
    a = (jj + 0.5) / s + pr.min() - 2 / s
    b = (pu.max() + 2 / s) - (ii + 0.5) / s
    far = math.sqrt(W * W + H * H + D * D)
    o = center + a[..., None] * r + b[..., None] * u - d * far / 2
    o = o.reshape(-1, 3)
    n = o.shape[0]
    hit = np.zeros(n, bool)
    hv = np.zeros((n, 3), int)
    nrm = np.zeros((n, 3), int)
    prev = np.floor(o).astype(int)
    step = 0.12
    steps = int(far / step) + 2
    alive = np.ones(n, bool)
    for k in range(steps):
        idx = np.nonzero(alive)[0]
        if idx.size == 0:
            break
        p = o[idx] + d * (k * step)
        v = np.floor(p).astype(int)
        inside = (v[:, 0] >= 0) & (v[:, 0] < W) & (v[:, 1] >= 0) & (v[:, 1] < H) & (v[:, 2] >= 0) & (v[:, 2] < D)
        sel = idx[inside]
        vi = v[inside]
        h = occ[vi[:, 2], vi[:, 1], vi[:, 0]]
        if h.any():
            got = sel[h]
            hv[got] = vi[h]
            diff = vi[h] - prev[got]
            # choose the axis that changed (largest abs)
            ax = np.argmax(np.abs(diff), axis=1)
            nn = np.zeros((got.size, 3), int)
            sign = -np.sign(diff[np.arange(got.size), ax])
            sign[sign == 0] = 1
            nn[np.arange(got.size), ax] = sign
            nrm[got] = nn
            hit[got] = True
            alive[got] = False
        prev[idx] = v
    img = np.zeros((n, 4), np.float32)
    hi = np.nonzero(hit)[0]
    v, nn = hv[hi], nrm[hi]
    base = col[v[:, 2], v[:, 1], v[:, 0]] / 255.0
    L = np.array(key, float); L /= np.linalg.norm(L)
    lam = np.clip(nn @ L, 0, 1)
    # ambient occlusion: count filled voxels around the empty cell in front of the face
    f = v + nn
    occp = np.pad(occ, 2)
    cnt = np.zeros(len(hi))
    for dz in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                cnt += occp[f[:, 2] + dz + 2, f[:, 1] + dy + 2, f[:, 0] + dx + 2]
    aof = 1 - ao * np.clip(cnt / 9.0, 0, 1)
    shade = (0.38 + 0.75 * lam)[:, None] * aof[:, None]
    c = base * shade
    # top faces a touch brighter, warm key
    c *= np.array([1.04, 1.0, 0.96])
    if rim:
        pink = np.array([1.0, 0.31, 0.85]); cyan = np.array([0.24, 0.94, 1.0])
        rl = np.array([-1, 0.15, -0.35]); rl /= np.linalg.norm(rl)
        rr = np.array([1, 0.2, -0.3]); rr /= np.linalg.norm(rr)
        c += (np.clip(nn @ rl, 0, 1) * 0.55)[:, None] * pink * base.mean(1, keepdims=True).clip(0.25, 1)
        c += (np.clip(nn @ rr, 0, 1) * 0.6)[:, None] * cyan * base.mean(1, keepdims=True).clip(0.25, 1)
        # bottom faces pick up purple bounce from the street
        c += (np.clip(-nn[:, 1], 0, 1) * 0.12)[:, None] * np.array([0.6, 0.2, 0.9])
    img[hi, :3] = np.clip(c, 0, 1)
    img[hi, 3] = 1
    img = img.reshape(h_img, w_img, 4)
    # voxel edge darkening: where neighbour pixel belongs to a different voxel/face, darken a bit
    vid = np.full((n,), -1, np.int64)
    vid[hi] = (v[:, 2] * H + v[:, 1]) * W + v[:, 0] + (np.argmax(np.abs(nn), 1) * 10 ** 9)
    vid = vid.reshape(h_img, w_img)
    if ssaa > 1:
        img = img.reshape(h_img // ssaa, ssaa, w_img // ssaa, ssaa, 4).mean((1, 3))
    a = img[..., 3] > 0.5
    if outline is not None:
        # 1px dark outline around the silhouette
        pad = np.pad(a, 1)
        nb = pad[:-2, 1:-1] | pad[2:, 1:-1] | pad[1:-1, :-2] | pad[1:-1, 2:]
        ring = nb & ~a
        img[ring, :3] = np.array(outline) / 255
        img[ring, 3] = 1
    out = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    return out


def trim(rgba):
    ys, xs = np.nonzero(rgba[..., 3])
    return rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


if __name__ == "__main__":
    import sys
    out = sys.argv[1]
    o, c = tiger()
    t = trim(render(o, c, scale=float(sys.argv[2]) if len(sys.argv) > 2 else 2.5))
    Image.fromarray(t).save(out + "_tiger.png")
    o, c = word()
    w = trim(render(o, c, scale=8, yaw=-18, pitch=14, ao=0.2))
    Image.fromarray(w).save(out + "_word.png")
    print(t.shape, w.shape)


def render_faces(occ, col, scale=3.0, yaw=-28, pitch=22, key=(-0.5, 0.75, 0.8), outline=(14, 6, 24),
                 rim=True, ao=0.45, bright=1.0, side=None):
    """Crisp voxel render: every exposed face facing the camera drawn as a polygon, far to near."""
    from PIL import ImageDraw
    D, H, W = occ.shape
    yaw, pitch = math.radians(yaw), math.radians(pitch)
    d = np.array([-math.sin(yaw) * math.cos(pitch), -math.sin(pitch), -math.cos(yaw) * math.cos(pitch)])
    r = np.cross(d, [0, 1, 0]); r /= np.linalg.norm(r)
    u = np.cross(r, d)
    occp = np.pad(occ, 2)
    zz, yy, xx = np.nonzero(occ)
    P = np.stack([xx, yy, zz], 1)
    L = np.array(key, float); L /= np.linalg.norm(L)
    rl = np.array([-1, 0.15, -0.35]); rl /= np.linalg.norm(rl)
    rr = np.array([1, 0.2, -0.3]); rr /= np.linalg.norm(rr)
    faces = []
    axes = [(np.array([1, 0, 0]),), (np.array([-1, 0, 0]),), (np.array([0, 1, 0]),), (np.array([0, -1, 0]),),
            (np.array([0, 0, 1]),), (np.array([0, 0, -1]),)]
    for (n,) in axes:
        if n @ d >= 0:
            continue
        nb = P + n
        empty = ~occp[nb[:, 2] + 2, nb[:, 1] + 2, nb[:, 0] + 2]
        Q = P[empty]
        base = col[Q[:, 2], Q[:, 1], Q[:, 0]] / 255.0
        f = Q + n
        cnt = np.zeros(len(Q))
        for dz in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    cnt += occp[f[:, 2] + dz + 2, f[:, 1] + dy + 2, f[:, 0] + dx + 2]
        shade = (0.42 + 0.72 * max(0.0, n @ L)) * (1 - ao * np.clip(cnt / 9, 0, 1)) * bright
        c = base * shade[:, None]
        if rim:
            lum = base.mean(1, keepdims=True).clip(0.3, 1)
            c = c + max(0.0, n @ rl) * 0.5 * np.array([1.0, 0.31, 0.85]) * lum + \
                max(0.0, n @ rr) * 0.55 * np.array([0.24, 0.94, 1.0]) * lum + \
                max(0.0, -n[1]) * 0.1 * np.array([0.6, 0.2, 0.9])
        if side is not None and n[2] != 1:          # extruded letter sides: deep and dark
            c = c * side[0] + np.array(side[1])
        # the face's four corners
        a = np.argmax(np.abs(n))
        o1, o2 = [i for i in range(3) if i != a]
        e1 = np.zeros(3); e1[o1] = 1
        e2 = np.zeros(3); e2[o2] = 1
        off = np.where(n > 0, 1, 0).astype(float)
        for q, cc in zip(Q, np.clip(c, 0, 1)):
            p0 = q + off
            pts = [p0, p0 + e1, p0 + e1 + e2, p0 + e2]
            depth = (q + 0.5 + n * 0.5) @ d
            faces.append((depth, pts, tuple(int(v * 255) for v in cc)))
    allp = np.array([p for _, ps, _ in faces for p in ps])
    pr, pu = allp @ r, allp @ u
    w_img, h_img = int((pr.max() - pr.min()) * scale) + 5, int((pu.max() - pu.min()) * scale) + 5
    img = Image.new("RGBA", (w_img, h_img), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    for depth, pts, cc in sorted(faces, key=lambda t: -t[0]):
        xy = [((p @ r - pr.min()) * scale + 2, (pu.max() - p @ u) * scale + 2) for p in pts]
        dr.polygon(xy, fill=cc + (255,))
    out = np.array(img)
    if outline is not None:
        a = out[..., 3] > 0
        pad = np.pad(a, 1)
        ring = (pad[:-2, 1:-1] | pad[2:, 1:-1] | pad[1:-1, :-2] | pad[1:-1, 2:]) & ~a
        out[ring] = outline + (255,)
    return out
