"""Desktop control: Nova moves the real mouse and types on the real keyboard.

The loop the agent follows: `screen_elements` (the controls of the active window, numbered, from Windows
UI Automation) or `screen_find` (text on screen, located with Windows OCR) -> `click` a number, a piece of
text, or x,y -> `type_text` / `press_keys` / `scroll` -> look again. Works on any app or web page,
including ones with no API (Instagram, YouTube, settings dialogs). Coordinates are physical screen pixels.

Everything here goes through ctypes SendInput: no extra packages.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import tempfile
import time
from ctypes import wintypes

from forgecli.nova.core.config import ROOT, SCRIPTS
from forgecli.nova.tools.base import Tool, ToolEnv, ToolError

NO_WINDOW = 0x08000000

# ─── raw input ──────────────────────────────────────────────────────────────────────────────
if os.name == "nt":
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # physical pixels, same as mss and UI Automation
    except Exception:
        try:
            _u32.SetProcessDPIAware()
        except Exception:
            pass

ULONG_PTR = ctypes.c_size_t


class _MOUSE(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KEYB(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _HW(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", _MOUSE), ("ki", _KEYB), ("hi", _HW)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


MOUSE_MOVE, MOUSE_ABS, MOUSE_VIRTUAL = 0x0001, 0x8000, 0x4000
MOUSE_WHEEL, MOUSE_HWHEEL = 0x0800, 0x1000
BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
KEY_UP, KEY_UNICODE, KEY_EXTENDED = 0x0002, 0x0004, 0x0001

VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "capslock": 0x14, "printscreen": 0x2C,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD, "playpause": 0xB3, "nexttrack": 0xB0,
    "prevtrack": 0xB1, "menu": 0x5D, "apps": 0x5D,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B, 0x5D}


def _send(*inputs: _INPUT) -> None:
    arr = (_INPUT * len(inputs))(*inputs)
    if _u32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT)) != len(inputs):
        raise ToolError("Windows blocked the input (an elevated/admin window is in front, or the screen is locked).")


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    return _INPUT(type=0, u=_U(mi=_MOUSE(dx, dy, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)))


def _key(vk: int = 0, up: bool = False, scan: int = 0, unicode: bool = False) -> _INPUT:
    flags = (KEY_UP if up else 0) | (KEY_UNICODE if unicode else 0) | (KEY_EXTENDED if vk in EXTENDED else 0)
    return _INPUT(type=1, u=_U(ki=_KEYB(vk, scan, flags, 0, 0)))


def _virtual_screen() -> tuple[int, int, int, int]:
    g = _u32.GetSystemMetrics
    return g(76), g(77), max(1, g(78)), max(1, g(79))  # left, top, width, height of all monitors


def move_to(x: int, y: int) -> None:
    left, top, w, h = _virtual_screen()
    nx = round((x - left) * 65535 / max(1, w - 1))
    ny = round((y - top) * 65535 / max(1, h - 1))
    _send(_mouse(MOUSE_MOVE | MOUSE_ABS | MOUSE_VIRTUAL, nx, ny))


def cursor() -> tuple[int, int]:
    pt = wintypes.POINT()
    _u32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> None:
    down, up = BUTTONS.get(button, BUTTONS["left"])
    move_to(x, y)
    time.sleep(0.05)
    for i in range(max(1, min(3, clicks))):
        _send(_mouse(down), _mouse(up))
        if i + 1 < clicks:
            time.sleep(0.06)


def type_unicode(text: str) -> None:
    for ch in text:
        if ch == "\n":
            _send(_key(0x0D), _key(0x0D, up=True))
        elif ch == "\t":
            _send(_key(0x09), _key(0x09, up=True))
        else:
            for unit in (ch.encode("utf-16-le")[i:i + 2] for i in range(0, len(ch.encode("utf-16-le")), 2)):
                code = int.from_bytes(unit, "little")
                _send(_key(scan=code, unicode=True), _key(scan=code, up=True, unicode=True))
        time.sleep(0.004)


def _vk_for(name: str) -> int:
    n = name.strip().lower()
    if n in VK:
        return VK[n]
    if len(n) == 1:
        res = _u32.VkKeyScanW(ord(n))
        if res != -1:
            return res & 0xFF
    raise ToolError(f"Unknown key '{name}'. Use names like ctrl, shift, alt, win, enter, tab, esc, f5, up, a, 1.")


def press_combo(combo: str) -> None:
    keys = [_vk_for(k) for k in re.split(r"\s*\+\s*", combo.strip()) if k]
    if not keys:
        raise ToolError("No keys given.")
    _send(*[_key(k) for k in keys], *[_key(k, up=True) for k in reversed(keys)])


def scroll(amount: int, horizontal: bool = False) -> None:
    # positive = down/right, in "notches"; Windows wheel units are 120 per notch, up is positive
    data = -int(amount) * 120 if not horizontal else int(amount) * 120
    _send(_mouse(MOUSE_HWHEEL if horizontal else MOUSE_WHEEL, data=data))


# ─── seeing: OCR boxes and UI Automation ────────────────────────────────────────────────────
def _ps(script: str, *args: str, timeout: int = 60) -> str:
    try:
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(SCRIPTS / script), *args],
                              capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise ToolError("Reading the screen took too long.") from None
    return proc.stdout.decode("utf-8", "replace")


def _grab(env: ToolEnv) -> tuple[bytes, int, int]:
    """PNG of the monitor under the active window, plus that monitor's top-left in screen pixels."""
    import mss
    import mss.tools
    info = env.screen.foreground_info() if env.screen else None
    with mss.mss() as sct:
        mons = list(sct.monitors[1:]) or [sct.monitors[0]]
        mon = mons[0]
        if info and info.get("rect"):
            r = info["rect"]
            cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
            for m in mons:
                if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                    mon = m
                    break
        shot = sct.grab(mon)
        return mss.tools.to_png(shot.rgb, shot.size), mon["left"], mon["top"]


def ocr_lines(env: ToolEnv) -> list[dict]:
    """Every OCR line on screen: {"text", "x", "y", "w", "h", "words": [...]}, screen pixels."""
    png, ox, oy = _grab(env)
    fd, path = tempfile.mkstemp(suffix=".png")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(png)
        out = _ps("ocr_boxes.ps1", "-ImagePath", path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if "__NO_OCR_ENGINE__" in out:
        raise ToolError("Windows has no text-recognition language installed (Settings > Time & language).")
    lines = []
    for raw in out.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        words = row.get("words") or []
        if isinstance(words, dict):
            words = [words]
        words = [dict(w, x=w["x"] + ox, y=w["y"] + oy) for w in words]
        if not words:
            continue
        x0, y0 = min(w["x"] for w in words), min(w["y"] for w in words)
        x1, y1 = max(w["x"] + w["w"] for w in words), max(w["y"] + w["h"] for w in words)
        lines.append({"text": row.get("text", ""), "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "words": words})
    return lines


def find_text(env: ToolEnv, needle: str) -> list[dict]:
    """Places on screen showing `needle` (case-insensitive), best match first, each with a center point."""
    want = re.sub(r"\s+", " ", needle).strip().lower()
    if not want:
        return []
    hits = []
    for line in ocr_lines(env):
        low = line["text"].lower()
        if want not in low:
            continue
        # narrow to the words that make up the match, so the click lands on it and not mid-line
        words, n = line["words"], len(want.split())
        box = line
        for i in range(len(words)):
            chunk = " ".join(w["t"] for w in words[i:i + n]).lower()
            if want in chunk or chunk in want and len(chunk) >= len(want) * 0.6:
                sel = words[i:i + n]
                x0, y0 = min(w["x"] for w in sel), min(w["y"] for w in sel)
                box = {"x": x0, "y": y0, "w": max(w["x"] + w["w"] for w in sel) - x0,
                       "h": max(w["y"] + w["h"] for w in sel) - y0}
                break
        exact = low.strip() == want
        hits.append({"text": line["text"], "cx": box["x"] + box["w"] // 2, "cy": box["y"] + box["h"] // 2,
                     "exact": exact, "len": len(low)})
    hits.sort(key=lambda h: (not h["exact"], h["len"]))
    return hits


def ui_elements(window: str = "", limit: int = 120) -> tuple[str, list[dict]]:
    out = _ps("uia.ps1", "-Max", str(limit), *(["-Window", window] if window else []), timeout=45)
    if "__NO_WINDOW__" in out:
        raise ToolError(f"No window matching '{window}'." if window else "No active window.")
    title, items = "", []
    for raw in out.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if "window" in row:
            title = row["window"]
        else:
            items.append(row)
    return title, items


# ─── tool handlers ──────────────────────────────────────────────────────────────────────────
def _need_windows() -> None:
    if os.name != "nt":
        raise ToolError("Desktop control only works on Windows.")


def _int(v, name: str) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        raise ToolError(f"'{name}' must be a number.") from None


def _t_screen_elements(args: dict, env: ToolEnv) -> str:
    _need_windows()
    title, items = ui_elements(str(args.get("window") or ""), int(args.get("limit") or 120))
    flt = str(args.get("filter") or "").lower().strip()
    if flt:
        items = [e for e in items if flt in (e.get("name") or "").lower() or flt in e.get("type", "").lower()]
    env._ui_elements = items  # click(element=N) uses this list
    if not items:
        return (f"Window '{title}': no labelled controls found (common in games, canvases, some Electron apps). "
                "Use screen_find with the text you want, or describe_screen.")
    rows = [f"[{i}] {e['type']} \"{e.get('name') or ''}\" at ({e['x'] + e['w'] // 2},{e['y'] + e['h'] // 2})"
            + ("" if e.get("enabled", True) else " (disabled)") for i, e in enumerate(items, 1)]
    return f"Window '{title}' - {len(items)} controls (click with element=N):\n" + "\n".join(rows)


def _t_screen_find(args: dict, env: ToolEnv) -> str:
    _need_windows()
    text = str(args.get("text") or "").strip()
    if not text:
        lines = ocr_lines(env)
        rows = [f"({l['x'] + l['w'] // 2},{l['y'] + l['h'] // 2}) {l['text']}" for l in lines[:150]]
        return "Text on screen with center points:\n" + "\n".join(rows) if rows else "No text recognised on screen."
    hits = find_text(env, text)
    if not hits:
        return f"'{text}' is not visible on screen. Scroll, open the right window, or try different wording."
    return f"Found '{text}' at:\n" + "\n".join(f"- ({h['cx']},{h['cy']}) in line: {h['text'][:120]}" for h in hits[:8])


def _target(args: dict, env: ToolEnv) -> tuple[int, int, str]:
    if args.get("element") not in (None, ""):
        items = getattr(env, "_ui_elements", None) or []
        n = _int(args["element"], "element")
        if not 1 <= n <= len(items):
            raise ToolError("Run screen_elements first, then click one of its numbers.")
        e = items[n - 1]
        return e["x"] + e["w"] // 2, e["y"] + e["h"] // 2, f"{e['type']} \"{e.get('name') or ''}\""
    if args.get("text"):
        hits = find_text(env, str(args["text"]))
        idx = _int(args.get("occurrence") or 1, "occurrence") - 1
        if not hits:
            raise ToolError(f"I can't see '{args['text']}' on screen. Use screen_find or screen_elements to look first.")
        h = hits[min(max(idx, 0), len(hits) - 1)]
        return h["cx"], h["cy"], f"'{h['text'][:60]}'"
    if args.get("x") is not None and args.get("y") is not None:
        x, y = _int(args["x"], "x"), _int(args["y"], "y")
        return x, y, f"({x},{y})"
    raise ToolError("Say what to click: element (number from screen_elements), text (visible words), or x and y.")


def _t_click(args: dict, env: ToolEnv) -> str:
    _need_windows()
    x, y, what = _target(args, env)
    button = str(args.get("button") or "left").lower()
    clicks = 2 if str(args.get("double") or "").lower() in ("1", "true", "yes") else 1
    click_at(x, y, button, clicks)
    time.sleep(float(args.get("wait") or 0.4))
    return f"{'Double-c' if clicks == 2 else 'C'}licked {what} at ({x},{y}) with the {button} button. Look again to check the result."


def _t_type_text(args: dict, env: ToolEnv) -> str:
    _need_windows()
    text = str(args.get("text") or "")
    if args.get("element") not in (None, "") or args.get("x") is not None or args.get("into"):
        if args.get("into"):
            args = {**args, "text": args["into"]}
        x, y, _what = _target(args, env)
        click_at(x, y)
        time.sleep(0.25)
    if str(args.get("clear") or "").lower() in ("1", "true", "yes"):
        press_combo("ctrl+a")
        _send(_key(0x08), _key(0x08, up=True))
    type_unicode(text)
    if str(args.get("submit") or "").lower() in ("1", "true", "yes"):
        press_combo("enter")
    return f"Typed {len(text)} characters" + (" and pressed Enter." if args.get("submit") else ".")


def _t_press_keys(args: dict, env: ToolEnv) -> str:
    _need_windows()
    keys = str(args.get("keys") or "").strip()
    times = max(1, min(50, _int(args.get("times") or 1, "times")))
    for _ in range(times):
        for combo in keys.split():
            press_combo(combo)
            time.sleep(0.05)
    return f"Pressed {keys}" + (f" x{times}." if times > 1 else ".")


def _t_scroll(args: dict, env: ToolEnv) -> str:
    _need_windows()
    if args.get("x") is not None and args.get("y") is not None:
        move_to(_int(args["x"], "x"), _int(args["y"], "y"))
    direction = str(args.get("direction") or "down").lower()
    amount = max(1, min(50, _int(args.get("amount") or 5, "amount")))
    sign = -1 if direction in ("up", "left") else 1
    scroll(sign * amount, horizontal=direction in ("left", "right"))
    time.sleep(0.3)
    return f"Scrolled {direction} {amount} notches."


def _t_drag(args: dict, env: ToolEnv) -> str:
    _need_windows()
    x1, y1, x2, y2 = (_int(args.get(k), k) for k in ("x1", "y1", "x2", "y2"))
    down, up = BUTTONS["left"]
    move_to(x1, y1)
    _send(_mouse(down))
    for i in range(1, 11):
        time.sleep(0.02)
        move_to(x1 + (x2 - x1) * i // 10, y1 + (y2 - y1) * i // 10)
    _send(_mouse(up))
    return f"Dragged from ({x1},{y1}) to ({x2},{y2})."


def _t_wait(args: dict, env: ToolEnv) -> str:
    secs = max(0.1, min(30.0, float(args.get("seconds") or 1)))
    time.sleep(secs)
    return f"Waited {secs:g}s."


def _sum_type(a: dict, e) -> str:
    return f"Type into the active window: {str(a.get('text', ''))[:160]!r}" + (" then press Enter" if a.get("submit") else "")


def computer_tools(cfg: dict) -> list[Tool]:
    s, n, b = {"type": "string"}, {"type": "number"}, {"type": "boolean"}
    where = {"element": n, "text": s, "x": n, "y": n}
    return [
        Tool("screen_elements", "List the buttons, links, text boxes and menu items of the active window (or one named "
             "by `window`), numbered with their centre points. Use before click/type_text on desktop apps and browsers.",
             {"window": s, "filter": s, "limit": n}, [], _t_screen_elements, "computer", readonly=True, untrusted=True),
        Tool("screen_find", "Find where text appears on screen (Windows OCR) and get click coordinates. With no text, "
             "lists every line of text on screen with its position.", {"text": s}, [], _t_screen_find, "computer",
             readonly=True, untrusted=True),
        Tool("click", "Click on the real screen: element=N from screen_elements, or text=visible words, or x,y pixels. "
             "Optional button (left/right/middle), double, occurrence (which match), wait seconds.",
             {**where, "button": s, "double": b, "occurrence": n, "wait": n}, [], _t_click, "computer",
             summarize=lambda a, e: f"Click {a.get('text') or ('element ' + str(a['element']) if a.get('element') else (str(a.get('x')) + ',' + str(a.get('y'))))} on your screen"),
        Tool("type_text", "Type text with the keyboard into the focused field (or first click `into`=visible words / "
             "element / x,y). clear=true empties the field first, submit=true presses Enter after.",
             {"text": s, "into": s, "element": n, "x": n, "y": n, "clear": b, "submit": b}, ["text"], _t_type_text,
             "computer", summarize=_sum_type),
        Tool("press_keys", "Press keys or shortcuts, space-separated, e.g. 'ctrl+l', 'alt+tab', 'enter', 'ctrl+shift+t', "
             "'pagedown'. `times` repeats.", {"keys": s, "times": n}, ["keys"], _t_press_keys, "computer",
             summarize=lambda a, e: f"Press {a.get('keys')} on your keyboard"),
        Tool("scroll", "Scroll the mouse wheel: direction up/down/left/right, amount in notches (default 5), "
             "optionally at x,y.", {"direction": s, "amount": n, "x": n, "y": n}, [], _t_scroll, "computer"),
        Tool("drag", "Drag with the left mouse button from x1,y1 to x2,y2.", {"x1": n, "y1": n, "x2": n, "y2": n},
             ["x1", "y1", "x2", "y2"], _t_drag, "computer"),
        Tool("wait", "Pause for a few seconds (page loads, animations).", {"seconds": n}, [], _t_wait, "computer",
             readonly=True),
    ]
