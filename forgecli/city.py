"""The pixel city: what Forge is doing, acted out by tiny tiger-cub workers on a neon street.

There are no spinners here. Every animation is driven by a real state change reported by the
agent (ui.py calls City.set when a model call starts, a tool runs, a provider fails, a turn ends):

    idle        sits on the bench under the lamp, tail swishing
    queued      waits by the depot, tapping a foot, "..." over its head
    thinking    at the desk, thought bubble; every few seconds gets up and paces
    coding      at the desk typing, code scrolling up the monitor
    processing  pushes a wheelbarrow of glowing parcels from the depot to the FORGE door and back
    researching walks to the archive, pulls a document, reads it under a magnifier
    delegating  hands parcels to helper cubs who wheel them down the street (several at once)
    waiting     on the bench with a coffee while rate limits pass, countdown under the sign
    completed   jumps and cheers, confetti
    failed      drops the parcels, red "!"
    cancelled   holds up a paw, STOP

Sprites are small string sheets, drawn by code, so nothing is generated per task. The street
behind them is cached and redrawn only twice a second. Timing comes from the clock alone, so
the same moment always draws the same frame. Rendering happens on the UI thread from plain
attributes, so it never blocks the agent. FORGE_MOTION=reduce holds still poses (no walking,
flicker or confetti). Terminals without truecolor get a one-line text version.
"""
from __future__ import annotations

import math
import os
import threading
import time

from . import art
from .art import (AMBER, CYAN, GREEN, INK, LAVENDER, MAGENTA, NIGHT, RED, Canvas, hash32, mix, scale)

H = 18            # scene height in pixels = 9 terminal rows
FEET = 14         # the sidewalk row characters stand on
HORIZON = 12      # buildings end here; sidewalk 13-14, curb 15, puddles 16-17
SEAT = 12         # bench and stool seats
MIN_WIDTH = 90    # narrower terminals get the one-line version

STATES = ("idle", "queued", "thinking", "coding", "processing", "researching", "delegating", "waiting",
          "completed", "failed", "cancelled")
FINAL = ("completed", "failed", "cancelled")
SIGN = {"idle": "IDLE", "queued": "QUEUE", "thinking": "THINK", "coding": "CODE", "processing": "RUN",
        "researching": "READ", "delegating": "TEAM", "waiting": "WAIT", "completed": "DONE",
        "failed": "OOPS", "cancelled": "STOP"}
SIGN_COL = {"completed": GREEN, "failed": RED, "cancelled": AMBER, "waiting": AMBER, "thinking": CYAN,
            "researching": CYAN, "idle": LAVENDER}

WALK_SPEED = 22.0     # pixels per second when walking somewhere
PUSH_SPEED = 15.0     # with a loaded wheelbarrow

# ---------------------------------------------------------------------------------------------
# Sprite sheet. Facing right, 8 wide; the body is drawn from these rows, arms and legs by pose.
# f fur  s ear  k stripe  e eye  n nose  w muzzle  c cloth  C strap  d cloth shade
HEAD = [".s..s...",
        ".ffkff..",
        ".fffefn.",
        ".kffwww."]
BODY = ["..cCcc..",
        "..cccc..",
        "..dddd.."]
LEGS = [((1, 0), (5, 0)), ((3, 0), (4, 0)), ((2, 0), (6, 0)), ((3, 0), (4, 0))]   # stride frames
# arms: list of (dx, dy, key) on top of the body; dy counts from the head's top row
ARMS = {
    "stand": [(3, 5, "f"), (3, 6, "p")],
    "walk0": [(2, 5, "f"), (1, 6, "p")],
    "walk1": [(4, 5, "f"), (5, 6, "p")],
    "push": [(5, 5, "f"), (6, 5, "f"), (7, 5, "p")],
    "pull": [(2, 5, "f"), (1, 5, "f"), (0, 5, "p")],
    "carry": [(5, 4, "f"), (6, 3, "p"), (7, 3, "p")],
    "type0": [(5, 5, "f"), (6, 5, "p")],
    "type1": [(5, 5, "f"), (6, 4, "p")],
    "think": [(5, 4, "f"), (6, 3, "p")],
    "cheer": [(5, 3, "f"), (6, 2, "f"), (6, 1, "p"), (1, 3, "f"), (0, 2, "f"), (0, 1, "p")],
    "startle": [(6, 3, "f"), (7, 2, "p"), (1, 3, "f"), (0, 2, "p")],
    "stop": [(5, 4, "f"), (6, 3, "f"), (7, 2, "p"), (7, 1, "p")],
    "reach": [(5, 3, "f"), (6, 2, "f"), (6, 1, "p")],
    "read": [(5, 5, "f"), (6, 5, "p")],
    "cup": [(5, 5, "f"), (6, 4, "p")],
}

CREW = [  # cloth colours: the main cub wears Forge cyan; helpers wear magenta, amber, lime
    {"c": (34, 176, 214), "C": (140, 240, 255), "d": (20, 104, 150)},
    {"c": (214, 58, 178), "C": (255, 150, 230), "d": (130, 30, 112)},
    {"c": (226, 150, 40), "C": (255, 214, 130), "d": (150, 90, 20)},
    {"c": (90, 200, 90), "C": (180, 255, 170), "d": (40, 120, 50)},
]
BASE = {"f": art.FUR, "s": art.FUR_SHADE, "k": art.STRIPE, "e": CYAN, "n": art.NOSE, "w": art.CREAM,
        "p": art.CREAM, "b": (36, 18, 46)}

PARCELS = [AMBER, CYAN, MAGENTA, (255, 140, 70)]


def cub(cv: Canvas, x: int, yb: int, facing: int = 1, pose: str = "stand", step: int = 0, crew: int = 0,
        sit: bool = False, blink: bool = False, look_up: bool = False, dim: float = 1.0) -> None:
    """Draw a tiger-cub worker. x is the left of its 8-pixel box, yb the row its feet are on."""
    pal = dict(BASE)
    pal.update(CREW[crew % len(CREW)])
    if dim < 1:
        pal = {k: scale(v, dim) for k, v in pal.items()}

    def P(dx, dy, key, top):
        X = x + (dx if facing > 0 else 7 - dx)
        cv.put(X, top + dy, pal[key])

    walking = pose.startswith("walk") or pose in ("push", "pull")
    bob = 1 if (walking and step % 2 == 1) else 0
    top = (yb - 7 - bob) if not sit else (yb - 7)
    # tail, behind the body; swishes with the stride or slowly when sitting
    ty = 4 + (step % 2 if not sit else 0)
    P(0, ty, "f", top)
    P(0, ty - 1, "k", top)
    for dy, row in enumerate(HEAD):
        for dx, ch in enumerate(row):
            if ch == ".":
                continue
            if ch == "e":
                if blink:
                    ch = "k"
                elif look_up:
                    P(dx, dy - 1, "e", top) if dy - 1 >= 1 else None
                    ch = "f"
            P(dx, dy, ch, top)
    for dy, row in enumerate(BODY):
        for dx, ch in enumerate(row):
            if ch != ".":
                P(dx, 4 + dy, ch, top)
    if sit:
        # sitting: legs forward over the seat edge
        for dx, dy in ((4, 7), (5, 7), (6, 7), (6, 8)):
            P(dx, dy, "b", top)
    else:
        for dx, _ in LEGS[step % 4]:
            P(dx, 7, "b", top)
            if bob:
                P(dx, 8, "b", top)
    for dx, dy, key in ARMS.get(pose, ARMS["stand"]):
        P(dx, dy, key, top)


def parcel(cv: Canvas, x: float, y: float, col, halo: float = 0.3) -> None:
    """A glowing 2x2 parcel with a soft halo."""
    xi, yi = int(round(x)), int(round(y))
    if halo:
        for dx in range(-1, 3):
            for dy in range(-1, 3):
                if dx in (-1, 2) or dy in (-1, 2):
                    cv.put(xi + dx, yi + dy, col, halo * (0.5 if dx in (-1, 2) and dy in (-1, 2) else 1))
    cv.put(xi, yi, mix(col, (255, 255, 255), 0.45))
    cv.put(xi + 1, yi, col)
    cv.put(xi, yi + 1, col)
    cv.put(xi + 1, yi + 1, scale(col, 0.7))


SPOKES = [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]   # clockwise


def barrow(cv: Canvas, x: float, yb: int, cargo: int = 0, dim: float = 1.0) -> None:
    """The wheelbarrow: handle on the left at x, wheel on the right. The wheel's spoke turns with
    the distance travelled, so it rolls exactly as fast as it moves."""
    xi = int(round(x))
    metal = scale((150, 158, 205), dim)
    body = scale((84, 88, 150), dim)
    dark = scale((48, 46, 96), dim)
    cv.put(xi, yb - 2, dark)
    cv.put(xi + 1, yb - 3, dark)
    for dx in range(2, 9):
        cv.put(xi + dx, yb - 4, metal)
        cv.put(xi + dx, yb - 3, body if dx < 8 else dark)
    cv.put(xi + 3, yb - 2, dark)
    cv.put(xi + 3, yb - 1, dark)
    cv.put(xi + 3, yb, dark)
    cx, cy = xi + 7, yb - 1
    for dx, dy in SPOKES:
        cv.put(cx + dx, cy + dy, scale((30, 20, 44), dim))
    cv.put(cx, cy, metal)
    sx, sy = SPOKES[int(math.floor(x / 1.15)) % 8]
    cv.put(cx + sx, cy + sy, scale((150, 230, 255), dim))
    for i in range(min(3, cargo)):
        parcel(cv, xi + 2 + i * 2, yb - 6, PARCELS[i], 0.28 * dim)


# ---------------------------------------------------------------------------------------------
class Layout:
    """Where everything stands, for a given width."""

    def __init__(self, w: int):
        self.w = w
        self.depot = 2                       # crates from x=2 to 10
        self.load = 12                       # the cub stands here to load the wheelbarrow
        self.door = w - 9                    # the FORGE door, 6 wide
        self.unload = self.door - 17         # worker x when the barrow's nose is at the door
        self.desk = int(w * 0.30)            # desk 9 wide, stool in front
        self.archive = int(w * 0.50)         # shelf 7 wide; reading spot to its right
        self.bench = int(w * 0.68)           # bench 9 wide, lamp 2 to its right
        self.sign = (w - 25, w - 2)          # neon sign above the door

    def spot(self, state: str) -> int:
        return {"idle": self.bench + 1, "waiting": self.bench + 1, "queued": self.load,
                "processing": self.load, "delegating": self.load, "thinking": self.desk - 7,
                "coding": self.desk - 7, "researching": self.archive - 9}.get(state, self.load)


def background(w: int, t: float, motion: bool) -> Canvas:
    """The street: sky, skyline, props. Cached by the caller."""
    L = Layout(w)
    cv = Canvas(w, H)
    wins = art.skyline(cv, HORIZON, seed=w * 7 + 3, moon=(int(w * 0.16), 3))
    art.windows_on(cv, wins, t, motion)
    # sidewalk
    for y, col in ((13, (40, 26, 76)), (14, (34, 22, 66))):
        for x in range(w):
            cv.px[y][x] = scale(col, 0.85) if (x + (y * 3)) % 9 == 0 else col
    # depot: stacked crates
    crate, edge = (118, 66, 46), (170, 104, 64)
    for cx, cy in ((2, 12), (5, 12), (8, 12), (3, 9), (6, 9)):
        cv.rect(cx, cy, 3, 3, crate)
        cv.rect(cx, cy, 3, 1, edge)
        cv.put(cx + 1, cy + 1, (230, 160, 90))
    # the FORGE door: a lit doorway in a dark wall, cyan neon frame
    d = L.door
    cv.rect(d - 2, 3, 10, HORIZON - 2, (22, 12, 46))
    cv.rect(d, 7, 6, FEET - 6, (60, 24, 90))
    for y in range(7, FEET + 1):
        cv.put(d - 1, y, CYAN)
        cv.light(d - 1, y, CYAN, 0.7)
        cv.put(d + 6, y, CYAN)
        cv.light(d + 6, y, CYAN, 0.7)
    for x in range(d - 1, d + 7):
        cv.put(x, 6, CYAN)
        cv.light(x, 6, CYAN, 0.7)
    cv.rect(d + 1, 8, 4, FEET - 8, (120, 50, 150))
    # the sign above it: a dark board with a magenta border (the word is drawn each frame)
    s0, s1 = L.sign
    cv.rect(s0, 0, s1 - s0, 6, (14, 8, 30))
    for x in range(s0, s1):
        cv.put(x, 0, MAGENTA, 0.6)
        cv.put(x, 5, MAGENTA, 0.6)
    # desk with monitor, stool
    k = L.desk
    wood, leg = (88, 56, 110), (50, 34, 70)
    cv.rect(k, 10, 9, 1, wood)
    cv.put(k + 1, 11, leg); cv.put(k + 1, 12, leg); cv.put(k + 1, 13, leg); cv.put(k + 1, 14, leg)
    cv.put(k + 7, 11, leg); cv.put(k + 7, 12, leg); cv.put(k + 7, 13, leg); cv.put(k + 7, 14, leg)
    cv.rect(k + 3, 5, 6, 5, (30, 30, 60))
    cv.rect(k + 4, 6, 4, 3, (10, 20, 40))
    cv.put(k + 5, 9, (60, 60, 100)); cv.put(k + 6, 9, (60, 60, 100))
    cv.rect(k - 5, SEAT, 4, 1, (110, 80, 140))
    cv.put(k - 4, 13, leg); cv.put(k - 4, 14, leg); cv.put(k - 2, 13, leg); cv.put(k - 2, 14, leg)
    # archive shelf with books, a floor pile beside it
    a = L.archive
    cv.rect(a, 4, 7, FEET - 3, (54, 34, 84))
    for row, y in enumerate((5, 8, 11)):
        for i in range(5):
            h = 2 if (hash32(a, row, i) % 3) else 1
            col = (PARCELS + [GREEN, LAVENDER])[hash32(row, i, 9) % 6]
            cv.rect(a + 1 + i, y + (2 - h), 1, h, scale(col, 0.75))
        cv.rect(a, y + 2, 7, 1, (90, 60, 120))
    # bench and lamp
    b = L.bench
    cv.rect(b, SEAT, 9, 1, (120, 80, 60))
    cv.rect(b, SEAT - 2, 9, 1, (96, 62, 48))
    for x in (b + 1, b + 7):
        cv.put(x, 13, (60, 40, 40)); cv.put(x, 14, (60, 40, 40))
    lp = b + 11
    for y in range(4, FEET + 1):
        cv.put(lp, y, (70, 60, 100))
    cv.rect(lp - 1, 3, 3, 1, (90, 80, 120))
    cv.put(lp, 4, (255, 226, 160))
    cv.light(lp, 4, AMBER, 2.2)
    cv.light(lp, 5, AMBER, 1.2)
    cv.bloom(radius=1, strength=0.55)
    # a cone of lamplight on the pavement
    for y in range(6, FEET + 1):
        spread = (y - 5) // 2
        for x in range(lp - spread, lp + spread + 1):
            cv.put(x, y, AMBER, 0.06)
    return cv


# ---------------------------------------------------------------------------------------------
class City:
    """Holds the current state and draws frames. set()/blip() are called from any thread; frame()
    is called by the UI's redraw timer."""

    LINGER = 3.5   # completed / failed / cancelled stay on screen this long

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"
        self.since = time.monotonic()
        self.detail = ""
        self.helpers = 0
        self.wait_until = 0.0
        self.wait_total = 0.0
        self.swap_at = -99.0
        self.x = None            # where the main cub is
        self.facing = 1
        self.arrived = None      # when it reached the spot for the current state
        self.walked = 0.0        # distance walked, drives the stride
        self.last = None
        self.anchor = None       # where an end-of-turn animation is played
        self._bg = (None, None)
        self.motion = os.environ.get("FORGE_MOTION", "").lower() not in ("reduce", "off", "0", "no")

    # -- state changes (called by the agent's UI hooks) ----------------------------------------
    def set(self, state: str, detail: str = "", wait: float = 0.0) -> None:
        if state not in STATES:
            return
        with self.lock:
            now = time.monotonic()
            if state != self.state:
                self.state = state
                self.since = now
                self.arrived = None
                self.anchor = None
            self.detail = detail
            if state == "waiting" and wait:
                self.wait_until = now + wait
                self.wait_total = wait

    def blip(self) -> None:
        """A provider failed and Forge is switching to the next one: a spark, the sign flickers."""
        with self.lock:
            self.swap_at = time.monotonic()

    def set_helpers(self, n: int) -> None:
        with self.lock:
            self.helpers = max(0, min(3, n))

    def settled(self) -> bool:
        """True once an end-of-turn animation has finished playing."""
        return self.state in FINAL and time.monotonic() - self.since > self.LINGER

    # -- drawing ----------------------------------------------------------------------------------
    def frame(self, width: int, now: float | None = None) -> str:
        """The scene as half-block ANSI text (H/2 lines), or one plain line if it doesn't fit."""
        now = time.monotonic() if now is None else now
        w = min(width, 240)
        if w < MIN_WIDTH:
            return self.line(width, now)
        with self.lock:
            cv = self.draw(w, now)
        return cv.ansi()

    def draw(self, w: int, now: float) -> Canvas:
        motion = self.motion
        L = Layout(w)
        bucket = int(now * 2) if motion else 0
        key = (w, bucket)
        if self._bg[0] != key:
            self._bg = (key, background(w, bucket / 2, motion))
        cv = self._bg[1].copy()
        t = now if motion else 0.0
        state = self.state
        age = now - self.since
        self._move(L, state, now)
        here = self.arrived is not None
        at = (now - self.arrived) if here else 0.0
        if not motion:
            at = 1.6
        x = self.x
        blink = motion and (now + 0.37 * hash32(int(x))) % 4.2 < 0.14

        # the wheelbarrow is parked at the depot unless someone is using it
        working_barrow = state in ("processing",) and here
        if not working_barrow and state != "delegating":
            barrow(cv, L.load + 8, FEET, 0)

        # helpers: in delegating, and alongside processing when tasks run in parallel
        crew = self.helpers if state in ("delegating", "processing") else 0
        if state == "delegating":
            crew = max(1, crew)
        if crew and here:
            start = L.load + 9 if state == "delegating" else L.load
            for i in range(crew):
                ph = self._loop(at + (i + 1) * 2.9, start, L.unload, L, cv, from_hand=(x + 7, FEET - 5)
                                if state == "delegating" else None)
                self._cub_with_barrow(cv, ph, crew=i + 1, dim=0.82)

        draw_main = True
        if not here:
            pose = "walk%d" % ((int(self.walked / 3)) % 2)
            cub(cv, int(x), FEET, self.facing, pose, int(self.walked / 2.2), blink=blink)
            draw_main = False
        if draw_main:
            self._act(cv, L, state, t, at, age, x, blink, working_barrow)
        self._sign(cv, L, state, t, now)
        art.street(cv, 15, t, motion)
        return cv

    def _move(self, L: Layout, state: str, now: float) -> None:
        """Walk the main cub towards the spot for this state; record when it arrives."""
        target = L.spot(state)
        if state in FINAL:
            if self.anchor is None:
                self.anchor = self.x if self.x is not None else target
            target = self.anchor
        if self.x is None:
            self.x = float(target)
        last = self.last if self.last is not None else now
        dt = max(0.0, min(0.25, now - last))
        self.last = now
        if state == "processing" and self.arrived is not None:
            return       # the delivery loop moves it now
        if not self.motion:
            self.x = float(target)
        gap = target - self.x
        if abs(gap) > 0.5:
            stepx = math.copysign(min(abs(gap), WALK_SPEED * dt), gap)
            self.x += stepx
            self.walked += abs(stepx)
            self.facing = 1 if gap > 0 else -1
            self.arrived = None
        elif self.arrived is None:
            self.x = float(target)
            self.arrived = now
            self.facing = 1

    def _loop(self, t: float, xa: float, xb: float, L: Layout, cv: Canvas, from_hand=None) -> dict:
        """One wheelbarrow delivery cycle: load at xa, push to xb, unload into the door, pull back.
        Pure function of t, so several cubs can run it with different offsets."""
        d = max(4.0, xb - xa)
        travel = d / PUSH_SPEED
        back = d / (PUSH_SPEED * 1.25)
        T_load, T_unload = 1.3, 1.1
        period = T_load + travel + T_unload + back
        p = t % period
        res = {"x": xa, "facing": 1, "cargo": 0, "pose": "push", "moving": False}
        if p < T_load:
            n = int(p / 0.4)
            res["cargo"] = min(3, n)
            f = (p % 0.4) / 0.4
            if n < 3:
                src = from_hand or (L.depot + 4 + (n % 2) * 3, 9)
                dst = (xa + 10 + n * 2, FEET - 6)
                bx = src[0] + (dst[0] - src[0]) * f
                by = src[1] + (dst[1] - src[1]) * f - math.sin(f * math.pi) * 4
                parcel(cv, bx, by, PARCELS[n], 0.3)
        elif p < T_load + travel:
            f = (p - T_load) / travel
            f = f * f * (3 - 2 * f) * 0.35 + f * 0.65      # a little ease at each end
            res.update(x=xa + d * f, cargo=3, moving=True)
        elif p < T_load + travel + T_unload:
            q = p - T_load - travel
            n = int(q / 0.35)
            res.update(x=xb, cargo=max(0, 3 - n))
            if n < 3:
                f = (q % 0.35) / 0.35
                sx, sy = xb + 10 + (2 - n) * 2, FEET - 6
                tx, ty = L.door + 2, FEET - 4
                parcel(cv, sx + (tx - sx) * f, sy + (ty - sy) * f - math.sin(f * math.pi) * 3,
                       PARCELS[2 - n], 0.3 * (1 - f))
            glow = 0.25 + 0.25 * math.sin(q * 12)
            for y in range(8, FEET):
                for xx in range(L.door + 1, L.door + 5):
                    cv.put(xx, y, (180, 255, 255), glow * 0.5)
        else:
            f = (p - T_load - travel - T_unload) / back
            res.update(x=xb - d * f, facing=-1, pose="pull", moving=True)
        return res

    def _cub_with_barrow(self, cv: Canvas, ph: dict, crew: int = 0, dim: float = 1.0) -> None:
        x = ph["x"]
        step = int(x / 2.2) if ph["moving"] else 1
        barrow(cv, x + 8, FEET, ph["cargo"], dim)
        cub(cv, int(round(x)), FEET, ph["facing"], ph["pose"], step, crew=crew, dim=dim)

    def _act(self, cv, L, state, t, at, age, x, blink, working_barrow) -> None:
        """The main cub's activity once it is in place."""
        xi = int(round(x))
        if state == "processing":
            ph = self._loop(at, L.load, L.unload, L, cv)
            self._cub_with_barrow(cv, ph)
            self.x = ph["x"]  # keep track of it so the next state walks from here
            self.arrived = self.arrived  # still in place for the loop
            return
        if state == "delegating":
            # the dispatcher: lifts a parcel from the depot and hands it over
            ph = (at % 1.2) / 1.2
            cub(cv, xi, FEET, 1, "carry" if ph > 0.3 else "reach", 1, blink=blink)
            if ph > 0.3:
                parcel(cv, xi + 6, FEET - 7, PARCELS[int(at / 1.2) % 4], 0.3)
            return
        if state in ("thinking", "coding"):
            self._desk(cv, L, state, t, at, xi, blink)
            return
        if state == "researching":
            self._research(cv, L, t, at, blink)
            return
        if state in ("idle", "waiting"):
            sx = L.bench + 1
            cub(cv, sx, SEAT - 1, 1, "cup" if state == "waiting" else "stand", 0, sit=True, blink=blink)
            if state == "waiting":
                cx, cy = sx + 7, SEAT - 5
                cv.put(cx, cy, (240, 240, 250)); cv.put(cx, cy + 1, (240, 240, 250))
                cv.put(cx + 1, cy, (200, 120, 60))
                for i in range(3):
                    k = (t * 1.6 + i / 3) % 1
                    cv.put(cx + int(math.sin(k * 6 + i) * 1.2), int(cy - 1 - k * 4), (220, 220, 255), 0.5 * (1 - k))
            return
        if state == "queued":
            step = int(t * 3) % 2
            cub(cv, xi, FEET, 1, "stand", step * 2, blink=blink)
            for i in range(3):
                on = int(t * 2.5) % 4 > i
                cv.put(xi + 3 + i * 2, FEET - 10, INK, 0.9 if on else 0.25)
            return
        if state == "completed":
            hop = abs(math.sin(age * 7)) * 2.5 if age < 2.2 else 0
            cub(cv, xi, FEET - int(hop), 1, "cheer", 1, blink=False)
            self._confetti(cv, xi + 4, FEET - 8, age)
            return
        if state == "failed":
            cub(cv, xi, FEET, 1, "startle", 1)
            for i in range(3):
                vx = (hash32(i, 5) % 7 - 3) * 6
                tt = min(age, 0.9)
                bx = xi + 6 + vx * tt
                by = min(FEET - 1, FEET - 7 - 8 * tt + 26 * tt * tt)
                parcel(cv, bx, by, PARCELS[i], 0.2)
            if int(age * 3) % 2 == 0 or not self.motion:
                cv.text("!", xi + 4, FEET - 13, RED, art.FONT3, 1)
            if age < 1.0 and self.motion:
                for i in range(6):
                    a = i * 1.05 + age * 3
                    r = 2 + age * 5
                    cv.put(int(xi + 8 + math.cos(a) * r), int(FEET - 3 + math.sin(a) * r * 0.5), (140, 120, 160), 0.6 * (1 - age))
            return
        if state == "cancelled":
            cub(cv, xi, FEET, 1, "stop", 1, blink=blink)
            ox, oy = xi + 9, FEET - 12
            for dx in range(5):
                for dy in range(5):
                    if (dx, dy) not in ((0, 0), (4, 0), (0, 4), (4, 4)):
                        cv.put(ox + dx, oy + dy, (220, 40, 60))
            cv.rect(ox + 1, oy + 2, 3, 1, (255, 240, 240))
            return
        cub(cv, xi, FEET, 1, "stand", 1, blink=blink)

    def _desk(self, cv, L, state, t, at, xi, blink) -> None:
        k = L.desk
        mon = (k + 4, 6)
        if state == "coding":
            pose = "type%d" % (int(t * 9) % 2)
            cub(cv, k - 7, SEAT - 1, 1, pose, 0, sit=True, blink=blink)
            for r in range(3):
                line = int(t * 5) + r
                n = 1 + hash32(line, 77) % 4
                col = (CYAN, MAGENTA, GREEN, AMBER)[hash32(line, 3) % 4]
                for i in range(n):
                    cv.put(mon[0] + i, mon[1] + r, col)
            cv.put(mon[0] + 3, mon[1] + 2, INK, 0.9 if int(t * 3) % 2 else 0.2)
            return
        # thinking: sit and look up with a thought bubble; every few seconds, pace about
        cyc = at % 9.0
        if cyc < 6.0 or not self.motion:
            cub(cv, k - 7, SEAT - 1, 1, "think", 0, sit=True, blink=blink, look_up=int(t) % 3 == 0)
            bx, by = k - 1, 2
            cv.put(k - 2, 5, INK, 0.6)
            cv.put(k - 1, 4, INK, 0.7)
            cv.ellipse(bx + 4, by + 1, 5, 2.6, (230, 222, 255), 0.95)
            for i in range(3):
                lit = int(t * 3) % 3 == i
                cv.put(bx + 2 + i * 2, by + 1, CYAN if lit else (120, 110, 170))
            cv.put(mon[0] + int(t * 2) % 4, mon[1] + 1, CYAN, 0.4)
        else:
            q = cyc - 6.0
            px = k - 7 + (q * 8 if q < 1.5 else (3 - q) * 8)
            face = 1 if q < 1.5 else -1
            cub(cv, int(px), FEET, face, "walk%d" % (int(q * 4) % 2), int(q * 6), blink=blink)

    def _research(self, cv, L, t, at, blink) -> None:
        a = L.archive
        shelf, read = a - 7, a + 9
        cyc = at % 7.0
        if not self.motion:
            cyc = 4.0
        if cyc < 1.0:            # reach up and pull a document
            cub(cv, shelf, FEET, 1, "reach", 1, blink=blink)
            if cyc > 0.5:
                cv.rect(shelf + 6, FEET - 9, 2, 2, (240, 240, 250))
        elif cyc < 2.4:          # walk to the reading spot holding it
            f = (cyc - 1.0) / 1.4
            x = shelf + (read - shelf) * f
            cub(cv, int(x), FEET, 1, "read", int(x / 2.2))
            cv.rect(int(x) + 7, FEET - 4, 2, 3, (240, 240, 250))
        elif cyc < 5.6:          # read it under a magnifier
            cub(cv, read, FEET, 1, "read", 1, blink=blink)
            cv.rect(read + 7, FEET - 5, 2, 3, (240, 240, 250))
            cv.put(read + 7, FEET - 4, (120, 120, 160))
            g = (t * 1.4) % 1
            cv.put(read + 7 + int(g * 2), FEET - 5 + int(g * 3), CYAN)
            cv.light(read + 7, FEET - 4, CYAN, 0.2)
        else:                    # walk back for the next one
            f = (cyc - 5.6) / 1.4
            x = read + (shelf - read) * f
            cub(cv, int(x), FEET, -1, "walk%d" % (int(x / 3) % 2), int(x / 2.2))
        pile = int(at / 7.0) % 5
        for i in range(pile):
            cv.rect(read + 10, FEET - i, 3, 1, (220, 220, 235) if i % 2 else (190, 190, 215))

    def _confetti(self, cv, ox, oy, age) -> None:
        if age > 2.6:
            return
        for i in range(22):
            ang = (hash32(i, 41) % 360) * math.pi / 180
            sp = 8 + hash32(i, 7) % 10
            tt = age if self.motion else 0.35
            x = ox + math.cos(ang) * sp * tt
            y = oy + math.sin(ang) * sp * tt * 0.6 - 10 * tt + 14 * tt * tt
            col = (MAGENTA, CYAN, AMBER, GREEN, INK)[i % 5]
            cv.put(int(x), int(y), col, max(0.0, 1 - tt / 2.6))

    def _sign(self, cv, L, state, t, now) -> None:
        s0, s1 = L.sign
        word = SIGN[state]
        col = SIGN_COL.get(state, MAGENTA)
        swap = now - self.swap_at
        if 0 <= swap < 1.6:
            word, col = "SWAP", CYAN
            if self.motion and int(swap * 10) % 3 == 0:
                col = scale(CYAN, 0.35)
        # a rare neon flicker
        if self.motion and hash32(int(now * 8), 99) % 97 == 0:
            col = scale(col, 0.4)
        wd = len(word) * 4 - 1
        x0 = s0 + (s1 - s0 - wd) // 2
        tmp = Canvas(wd, 5, (0, 0, 0))
        tmp.text(word, 0, 0, col, art.FONT3, 1)
        for y in range(5):
            for x in range(wd):
                if tmp.px[y][x] != (0, 0, 0):
                    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        cv.put(x0 + x + dx, 0 + y + dy + 0, col, 0.18)
        for y in range(5):
            for x in range(wd):
                if tmp.px[y][x] != (0, 0, 0):
                    cv.put(x0 + x, y, mix(col, (255, 255, 255), 0.35))
        if state == "waiting" and self.wait_total:
            left = max(0.0, self.wait_until - now) / self.wait_total
            n = int((s1 - s0 - 4) * left)
            cv.rect(s0 + 2, 5, n, 1, AMBER)
        if 0 <= swap < 0.8 and self.x is not None:
            # a spark jumps over the worker: the model behind it changed
            xi = int(self.x)
            for i, (dx, dy) in enumerate(((3, -11), (4, -10), (3, -9), (5, -9), (4, -8))):
                cv.put(xi + dx, FEET + dy, (255, 240, 120))

    # -- terminals without colour --------------------------------------------------------------
    def line(self, width: int, now: float | None = None) -> str:
        """One plain line: a tiny text street for terminals that can't draw the city."""
        now = time.monotonic() if now is None else now
        st = self.state
        t = now if self.motion else 0.0
        if st in ("processing", "delegating"):
            n = 18
            p = t * 6 % (2 * n)
            pos = int(p if p < n else 2 * n - p)
            cart = "o=[■]" if p < n else "o=[ ]"
            track = "." * pos + cart + "." * (n - pos)
            return f"  ▤ {track} ▯  {SIGN[st].lower()}"
        faces = {"thinking": "(=･ω･=) ?", "coding": "(=^‿^=)⌨", "researching": "(=･ᴥ･=)📄",
                 "waiting": "(=-ω-=)☕", "queued": "(=･ω･=) …", "completed": "\\(=^▽^=)/ ✦",
                 "failed": "(=;ω;=) !", "cancelled": "(=･ω･=)✋", "idle": "(=^ω^=)"}
        return f"  {faces.get(st, '(=^ω^=)')}  {SIGN[st].lower()}"


# --- the little wheelbarrow -------------------------------------------------------------------
# One line, about as wide as the word "Thinking", standing in for the spinner next to the status
# word. It follows what Forge is really doing:
#   thinking / coding   wheels loads from the pile (left) to the house (right) and back
#   a tool is running   pushes a load to the house and waits at the door until the tool finishes
#   tool finished       unloads at the house, walks back empty for the next load
#   waiting (limits)    stops where it is, zZ
#   completed           delivers the last load, the house shows a tick
#   failed              the load spills, the house goes red
#   cancelled           stops where it is
# Cargo colour shows the kind of work: lavender thinking, amber running, cyan reading, magenta helpers.

TRACK = 6                  # cells the barrow travels: about as wide as the word 'Thinking'
_SPEED = 5.0               # cells per second
_PAUSE = 0.5               # seconds spent loading / unloading at each end
_WHEEL = "◐◓◑◒"            # quarter turns, so the wheel rolls as it goes
TOOL_STATES = ("processing", "researching", "delegating")
_CARGO = {"thinking": "#c3a6ff", "coding": "#c3a6ff", "processing": "#ffb347",
          "researching": "#3ef0ff", "delegating": "#ff4fd8"}


class Barrow:
    """State follows UI.act(); pieces(now) moves it towards where that state wants it."""

    def __init__(self, motion: bool = True):
        self.lock = threading.Lock()
        self.motion = motion
        self.state = "idle"
        self.pos = 0.0
        self.right = True          # facing / pushing towards the house
        self.loaded = False
        self.kind = "thinking"     # what the current load is
        self.pause_until = 0.0     # loading / unloading in progress until then
        self.pause_kind = ""       # "load" | "unload"
        self.last = None
        self.delivered = 0
        self.walked = 0.0          # total distance pushed (drives the walk-cycle frame)

    def on(self, state: str) -> None:
        with self.lock:
            if state == self.state:
                return
            self.state = state
            if state in TOOL_STATES:
                self.kind = state
            elif state in ("thinking", "coding") and not self.loaded:
                self.kind = state
            if state == "idle":
                self.pos, self.loaded, self.right, self.pause_until = 0.0, False, True, 0.0

    def _target(self) -> float | None:
        st = self.state
        if st in ("waiting", "failed", "cancelled", "idle", "queued"):
            return None                                 # stay put
        if st == "completed":
            return float(TRACK)                         # the last load goes in
        if st in TOOL_STATES:                           # fetch a load if empty, then wait at the door
            return float(TRACK) if self.loaded else 0.0
        # thinking / coding: shuttle back and forth
        return float(TRACK) if self.loaded else 0.0

    def _step(self, now: float) -> None:
        last = self.last if self.last is not None else now
        dt = max(0.0, min(0.25, now - last))
        self.last = now
        if not self.motion:
            self.pos, self.loaded = TRACK / 2, self.state not in ("failed", "idle")
            return
        if now < self.pause_until:
            return
        if self.pause_kind == "load":
            self.loaded = True
        elif self.pause_kind == "unload":
            if self.loaded:
                self.delivered += 1
            self.loaded = False
        self.pause_kind = ""
        target = self._target()
        if target is None:
            return
        gap = target - self.pos
        if abs(gap) > 0.01:
            self.right = gap > 0
            step = min(abs(gap), getattr(self, "speed", _SPEED) * dt)
            self.pos += math.copysign(step, gap)
            self.walked += step
            return
        # arrived
        at_pile, at_house = self.pos <= 0.01, self.pos >= TRACK - 0.01
        if at_pile and not self.loaded and self.state != "completed":
            self.pause_kind, self.pause_until = "load", now + _PAUSE
        elif at_house and self.loaded:
            # tools: park at the door until the tool is done; otherwise unload now
            if self.state not in TOOL_STATES:
                self.pause_kind, self.pause_until = "unload", now + _PAUSE
        elif at_pile and self.state in ("thinking", "coding"):
            self.pause_kind, self.pause_until = "load", now + _PAUSE

    def pieces(self, now: float) -> list[tuple[str, str]]:
        """(text, style) pieces for the line at time now."""
        with self.lock:
            self._step(now)
            st, pos, right, loaded = self.state, self.pos, self.right, self.loaded
            busy = now < self.pause_until
            kind = self.kind
        x = max(0, min(TRACK, int(round(pos))))
        moving = self.motion and not busy and self._target() is not None and abs((self._target() or 0) - pos) > 0.01
        if st == "failed":
            loaded = False
        wheel = _WHEEL[(int(pos * 2) if right else -int(pos * 2)) % 4] if moving else "◐"
        cargo = ("▆", "bold " + _CARGO.get(kind, "#ffb347")) if loaded else ("▂", "#6b5f9e")
        if st == "failed":
            cargo = ("▂", "#ff5f6d")
        if self.motion and busy:                       # lifting a parcel in or out: it bobs
            cargo = ("▄" if int(now * 6) % 2 else cargo[0], cargo[1])
        worker = ("ȯ" if moving and int(pos * 2) % 2 else "o", "bold #3ef0ff")
        if st == "cancelled":
            worker = ("o", "#6b5f9e")
        cart = [worker, ("╼" if right else "╾", "#a096d2"), cargo, (wheel, "#e8e0ff")]
        if not right:
            cart.reverse()
        track_w = TRACK + 4
        lane = [("·", "#3a2f5c")] * track_w
        for i, piece in enumerate(cart):
            lane[min(track_w - 1, x + i)] = piece
        at_house = x >= TRACK
        pulse = self.motion and int(now * 3) % 2
        pile = ("▤", "bold #ffb347" if (x == 0 and busy) else "#b8813a")
        house_style = "#9c3c8c"
        if at_house and (busy or (st in TOOL_STATES and loaded)):
            house_style = "bold #ff4fd8" if pulse or not self.motion else "#ff4fd8"
        house = ("⌂", house_style)
        if st == "completed" and not loaded and at_house:
            house = ("✓", "bold #7dff9a")
        elif st == "failed":
            house = ("⌂", "bold #ff5f6d")
        out = [pile, (" ", "")] + lane + [(" ", ""), house]
        if st == "waiting":
            out.append((" zZ", "#6b5f9e"))
        return out

    def snapshot(self, now: float) -> dict:
        """Where everything is at time now, for the pixel courier (courier.py)."""
        with self.lock:
            self._step(now)
            busy = now < self.pause_until
            left = max(0.0, self.pause_until - now) / _PAUSE if busy else 0.0
            if busy and self.pause_kind == "load":
                fill = 1.0 - left
            elif busy and self.pause_kind == "unload":
                fill = left
            else:
                fill = 1.0 if self.loaded else 0.0
            target = self._target()
            moving = self.motion and not busy and target is not None and abs(target - self.pos) > 0.01
            at_door = self.pos >= TRACK - 0.01
            return {"state": self.state, "pos": self.pos, "track": TRACK, "right": self.right,
                    "moving": moving, "walked": self.walked, "kind": self.kind, "fill": fill,
                    "loaded": self.loaded, "busy": busy, "at_door": at_door,
                    "parked": at_door and self.loaded and self.state in TOOL_STATES,
                    "motion": self.motion, "delivered": self.delivered}

    def text(self, now: float) -> str:
        """Plain text of the line (tests, terminals without colour)."""
        return "".join(s for s, _ in self.pieces(now))
