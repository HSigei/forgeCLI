"""Closing apps and windows (Phase 4).

What "close chrome", "close the terminal", "close this window" really do, step by step:

1. Find the windows. Names are matched like in the launcher (nicknames, spoken variants, mis-hearings) but against
   what is RUNNING: the program name, the app's registry names, and the window title. Console windows (PowerShell,
   cmd, Windows Terminal) are recognised from their title, so "close powershell" finds the right one.
2. Ask politely, the way the X button does (WM_CLOSE). Nothing is killed. Then watch for three outcomes:
     - the window closes: done;
     - a "Do you want to save changes?" box appears: Nova asks you (yes/no questions, spoken or on screen):
       "Save them before closing?" yes -> presses Save. no -> "Close without saving?" yes -> presses Don't Save,
       no -> presses Cancel and leaves the app open. Nothing is ever discarded without a clear yes;
     - nothing happens (stuck or "not responding"): Nova asks "Force close it?". Only a yes kills the program.
3. Apps that keep running in the tray after their window closes (Spotify, Discord...): Nova asks if you want it
   quit completely (setting: tools.close_background = ask | quit | leave).

Safety: Nova never closes itself or the program/terminal it is running in, never closes Windows system parts, and
never closes "everything" at once. All Windows calls go through a small Backend, so the logic is tested with a fake
Windows (tools/test_close.py).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

from forgecli.nova.tools import apps
from forgecli.nova.tools.base import NeedsChoice, ToolError

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# programs that make up Windows itself: never closed, never killed
SYSTEM_EXES = {"dwm", "csrss", "winlogon", "services", "lsass", "svchost", "system", "smss", "wininit",
               "fontdrvhost", "sihost", "taskhostw", "searchhost", "searchapp", "startmenuexperiencehost",
               "shellexperiencehost", "textinputhost", "runtimebroker", "securityhealthsystray", "ctfmon",
               "lockapp", "logonui"}
# shared host programs: closing their window is fine, killing the process is not
NEVER_KILL = SYSTEM_EXES | {"explorer", "applicationframehost", "windowsterminal"}
CONSOLE_HOSTS = {"conhost", "openconsole", "windowsterminal", "cmd", "powershell", "pwsh", "wsl", "wslhost"}
SHELL_CLASSES = {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd", "button", "tooltips_class32",
                 "ime", "msctfime ui", "default ime"}
ACTIVE_WORDS = {"this", "that", "it", "this one", "that one", "current", "current window", "active", "active window",
                "this window", "that window", "the window", "window", "this app", "that app", "current app",
                "active app", "this program", "that program", "this thing"}
EVERYTHING = {"all", "everything", "all apps", "all windows", "all of them", "all programs", "every app",
              "every window", "all the apps", "all the windows", "everything open"}
TERMINAL_WORDS = {"terminal", "terminals", "console", "consoles", "shell", "shells", "command line"}
_FILLER = {"this", "that", "it", "one", "current", "active", "window", "app", "program", "thing", "the", "a", "an"}

# stems the registry does not give (the running program is named differently from the name used to start it)
EXTRA_STEMS = {
    "Calculator": ["calculatorapp", "calculator"], "Settings": ["systemsettings"], "Photos": ["microsoft.photos"],
    "Snipping Tool": ["snippingtool", "screenclippinghost"], "Microsoft Edge": ["msedge"],
    "Visual Studio Code": ["code"], "Word": ["winword"], "Excel": ["excel"], "PowerPoint": ["powerpnt"],
    "Outlook": ["outlook", "olk"], "OneNote": ["onenote"], "Teams": ["ms-teams", "teams"],
    "WhatsApp": ["whatsapp", "whatsapp.root"], "Paint": ["mspaint"], "Task Manager": ["taskmgr"],
    "Notepad": ["notepad"], "Windows Media Player": ["wmplayer", "microsoft.media.player"],
    "File Explorer": ["explorer"], "Google Chrome": ["chrome"], "Firefox": ["firefox"], "Spotify": ["spotify"],
}


# ---------------------------------------------------------------------------------------------- data
@dataclass
class Win:
    hwnd: int
    pid: int
    exe: str                 # program name without .exe, lowercase ("notepad")
    title: str
    cls: str = ""
    real_pid: int = 0        # for Store apps shown through ApplicationFrameHost: the app's own process
    protected: bool = False  # Nova itself, or the program Nova is running in

    @property
    def target_pid(self) -> int:
        return self.real_pid or self.pid


@dataclass
class Dialog:
    hwnd: int
    title: str
    text: str
    buttons: list = field(default_factory=list)   # button labels as shown
    via: str = "win32"                           # win32 | uia
    kind: str = "other"                          # save | other
    roles: dict = field(default_factory=dict)    # save/discard/cancel -> button label


def _btn(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace("&", "").replace("’", "'").replace("`", "'")).strip().lower()


_SAVE_NAMES = {"save", "save all", "save changes", "yes", "yes to all", "keep"}
_DISCARD_NAMES = {"don't save", "dont save", "do not save", "no", "discard", "don't keep", "close without saving",
                  "exit without saving", "don't save changes", "discard changes", "no to all", "lose changes"}
_CANCEL_NAMES = {"cancel", "stay", "go back", "keep editing", "back"}


def classify_dialog(title: str, text: str, buttons: list) -> tuple[str, dict]:
    """Is this a 'save changes?' box? Returns (kind, {role: button label}). Unknown boxes are never clicked blindly."""
    names = {_btn(b): b for b in buttons}
    mentions_save = bool(re.search(r"\bsave|\bchanges\b|unsaved", f"{title} {text}", re.I))
    save = next((names[n] for n in names if n in _SAVE_NAMES and (n.startswith("save") or mentions_save)), None)
    discard = next((names[n] for n in names if n in _DISCARD_NAMES and (n not in ("no", "no to all") or mentions_save)),
                   None)
    cancel = next((names[n] for n in names if n in _CANCEL_NAMES), None)
    if save and discard and cancel and mentions_save:
        return "save", {"save": save, "discard": discard, "cancel": cancel}
    if save and discard and mentions_save and re.search(r"save\b.*\?|want to save|save changes", f"{title} {text}", re.I):
        return "save", {"save": save, "discard": discard, **({"cancel": cancel} if cancel else {})}
    return "other", {"cancel": cancel} if cancel else {}


# ---------------------------------------------------------------------------------------------- real Windows
_UIA_HEAD = ("Add-Type -AssemblyName UIAutomationClient; Add-Type -AssemblyName UIAutomationTypes; "
             "$ae=[System.Windows.Automation.AutomationElement]; $D=[System.Windows.Automation.TreeScope]::Descendants; "
             "$ErrorActionPreference='Stop'; ")
_UIA_LIST = (_UIA_HEAD + "try { $root=$ae::FromHandle([IntPtr]{hwnd}); "
             "$bc=New-Object System.Windows.Automation.PropertyCondition($ae::ControlTypeProperty,"
             "[System.Windows.Automation.ControlType]::Button); "
             "$tc=New-Object System.Windows.Automation.PropertyCondition($ae::ControlTypeProperty,"
             "[System.Windows.Automation.ControlType]::Text); "
             "$b=@(); foreach($x in $root.FindAll($D,$bc)){ if(-not $x.Current.IsOffscreen){ $b+=[string]$x.Current.Name } }; "
             "$t=@(); foreach($x in $root.FindAll($D,$tc)){ if(-not $x.Current.IsOffscreen){ $t+=[string]$x.Current.Name } }; "
             "ConvertTo-Json -Compress -InputObject @{ buttons=@($b); texts=@($t | Select-Object -First 8) } } "
             "catch { Write-Output '{\"buttons\":[],\"texts\":[]}' }")
_UIA_CLICK = (_UIA_HEAD + "try { $root=$ae::FromHandle([IntPtr]{hwnd}); $want=$env:NOVA_UIA_BTN; "
              "$bc=New-Object System.Windows.Automation.PropertyCondition($ae::ControlTypeProperty,"
              "[System.Windows.Automation.ControlType]::Button); $done=$false; "
              "foreach($x in $root.FindAll($D,$bc)){ if([string]$x.Current.Name -eq $want -and -not $x.Current.IsOffscreen){ "
              "$x.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); $done=$true; break } }; "
              "if($done){ Write-Output 'ok' } else { Write-Output 'none' } } catch { Write-Output 'error' }")
_UIA_CLOSE = (_UIA_HEAD + "try { $root=$ae::FromHandle([IntPtr]{hwnd}); "
              "$root.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close(); Write-Output 'ok' } "
              "catch { Write-Output 'error' }")


class RealBackend:
    """Talks to Windows (ctypes + psutil + PowerShell UI Automation). Only created on Windows."""

    WM_CLOSE, BM_CLICK = 0x0010, 0x00F5
    GW_OWNER, GA_ROOT = 4, 2

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.u = ctypes.windll.user32
        self.k = ctypes.windll.kernel32
        try:
            self.dwm = ctypes.windll.dwmapi
        except OSError:
            self.dwm = None
        u, wt = self.u, wintypes
        self.ENUM = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        u.EnumWindows.argtypes = [self.ENUM, wt.LPARAM]
        u.EnumChildWindows.argtypes = [wt.HWND, self.ENUM, wt.LPARAM]
        u.IsWindowVisible.argtypes = [wt.HWND]
        u.IsWindow.argtypes = [wt.HWND]
        u.GetWindowTextLengthW.argtypes = [wt.HWND]
        u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.GetWindow.argtypes = [wt.HWND, wt.UINT]
        u.GetWindow.restype = wt.HWND
        u.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        u.GetAncestor.restype = wt.HWND
        u.GetForegroundWindow.restype = wt.HWND
        u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.IsHungAppWindow.argtypes = [wt.HWND]
        u.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        k = self.k
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.OpenProcess.restype = wt.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        k.CloseHandle.argtypes = [wt.HANDLE]
        self._own: tuple[float, set] = (0.0, set())
        self._names: dict[int, str] = {}

    # -- small helpers --------------------------------------------------------------------------
    def _title(self, h) -> str:
        n = self.u.GetWindowTextLengthW(h)
        buf = self.ct.create_unicode_buffer(n + 2)
        self.u.GetWindowTextW(h, buf, n + 2)
        return buf.value

    def _class(self, h) -> str:
        buf = self.ct.create_unicode_buffer(256)
        self.u.GetClassNameW(h, buf, 256)
        return buf.value

    def _pid(self, h) -> int:
        pid = self.wt.DWORD(0)
        self.u.GetWindowThreadProcessId(h, self.ct.byref(pid))
        return int(pid.value)

    def exe_of(self, pid: int) -> str:
        if pid in self._names:
            return self._names[pid]
        name = ""
        handle = self.k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                buf = self.ct.create_unicode_buffer(1024)
                size = self.wt.DWORD(1024)
                if self.k.QueryFullProcessImageNameW(handle, 0, buf, self.ct.byref(size)):
                    base = buf.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
                    name = base[:-4] if base.endswith(".exe") else base
            finally:
                self.k.CloseHandle(handle)
        if len(self._names) > 500:
            self._names.clear()
        self._names[pid] = name
        return name

    def _cloaked(self, h) -> bool:
        if self.dwm is None:
            return False
        val = self.ct.c_int(0)
        try:
            self.dwm.DwmGetWindowAttribute(self.wt.HWND(h), 14, self.ct.byref(val), 4)   # DWMWA_CLOAKED
        except Exception:
            return False
        return bool(val.value)

    def _real_pid(self, h, pid: int, exe: str) -> int:
        if exe != "applicationframehost":
            return 0
        found: list[int] = []

        def cb(ch, _lp):
            p = self._pid(ch)
            if p and p != pid:
                found.append(p)
                return False
            return True
        self.u.EnumChildWindows(h, self.ENUM(cb), 0)
        return found[0] if found else 0

    # -- the protected set: Nova itself and the program it runs in ----------------------------------
    def own_pids(self) -> set:
        now = time.monotonic()
        if now - self._own[0] < 30 and self._own[1]:
            return self._own[1]
        pids = {os.getpid()}
        try:
            import psutil
            me = psutil.Process(os.getpid())
            skip = {"explorer.exe", "services.exe", "svchost.exe", "winlogon.exe", "wininit.exe", "system",
                    "userinit.exe", "sihost.exe", "taskeng.exe", "taskhostw.exe", "runtimebroker.exe"}
            roots = [me]
            for parent in me.parents():
                try:
                    if parent.name().lower() in skip:
                        break
                    roots.append(parent)
                except psutil.Error:
                    break
            for p in roots:
                pids.add(p.pid)
                try:
                    pids.update(c.pid for c in p.children(recursive=True))
                except psutil.Error:
                    pass
        except Exception:
            pass
        self._own = (now, pids)
        return pids

    # -- Backend interface ------------------------------------------------------------------------
    def windows(self) -> list[Win]:
        own = self.own_pids()
        out: list[Win] = []

        def cb(h, _lp):
            h = int(h or 0)
            if not h or not self.u.IsWindowVisible(h) or self.u.GetWindow(h, self.GW_OWNER):
                return True
            title = self._title(h).strip()
            cls = self._class(h)
            if not title or cls.lower() in SHELL_CLASSES or title == "Program Manager":
                return True
            ex = self.u.GetWindowLongW(h, -20)
            if ex & 0x80 and not ex & 0x40000:            # tool window without APPWINDOW: not an app window
                return True
            if self._cloaked(h):
                return True
            rect = self.wt.RECT()
            self.u.GetWindowRect(h, self.ct.byref(rect))
            if rect.right - rect.left <= 1 and rect.bottom - rect.top <= 1 and not (self.u.GetWindowLongW(h, -16) & 0x20000000):
                return True
            pid = self._pid(h)
            exe = self.exe_of(pid)
            if exe == "explorer" and cls != "CabinetWClass":
                return True                                # the desktop, taskbar, widgets: not folder windows
            real = self._real_pid(h, pid, exe)
            w = Win(h, pid, exe, title, cls, real)
            w.protected = pid in own or (real in own if real else False)
            out.append(w)
            return True

        self.u.EnumWindows(self.ENUM(cb), 0)
        return out                                         # front to back (z-order)

    def foreground(self) -> int:
        h = self.u.GetForegroundWindow()
        if not h:
            return 0
        root = self.u.GetAncestor(h, self.GA_ROOT)
        return int(root or h)

    def exists(self, hwnd: int) -> bool:
        return bool(self.u.IsWindow(hwnd))

    def hung(self, hwnd: int) -> bool:
        try:
            return bool(self.u.IsHungAppWindow(hwnd))
        except Exception:
            return False

    def post_close(self, hwnd: int) -> None:
        self.u.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)

    def alive(self, pid: int) -> bool:
        try:
            import psutil
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    def kill_tree(self, pid: int) -> bool:
        try:
            import psutil
            proc = psutil.Process(pid)
            procs = proc.children(recursive=True) + [proc]
            for p in procs:
                try:
                    p.terminate()
                except psutil.Error:
                    pass
            _gone, left = psutil.wait_procs(procs, timeout=2.0)
            for p in left:
                try:
                    p.kill()
                except psutil.Error:
                    pass
            return True
        except Exception:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10,
                               creationflags=NO_WINDOW)
                return True
            except Exception:
                return False

    def dialogs(self, win: Win) -> list[Dialog]:
        """Visible boxes belonging to this window: owned windows, or dialog-class windows of the same program."""
        found: list[int] = []

        def cb(h, _lp):
            h = int(h or 0)
            if not h or h == win.hwnd or not self.u.IsWindowVisible(h):
                return True
            owner = int(self.u.GetWindow(h, self.GW_OWNER) or 0)
            pid = self._pid(h)
            if owner == win.hwnd or (pid in (win.pid, win.real_pid) and self._class(h) == "#32770"):
                found.append(h)
            return True

        self.u.EnumWindows(self.ENUM(cb), 0)
        return [d for d in (self._read_dialog(h) for h in found) if d is not None]

    def _read_dialog(self, h: int) -> Dialog | None:
        buttons: list[str] = []
        texts: list[str] = []

        def cb(ch, _lp):
            ch = int(ch or 0)
            cls = self._class(ch).lower()
            label = self._title(ch).strip()
            if cls == "button" and label and (self.u.GetWindowLongW(ch, -16) & 0xF) in (0, 1):
                buttons.append(label)
            elif cls in ("static", "edit", "richedit20w", "directuihwnd") and label:
                texts.append(label)
            return True

        self.u.EnumChildWindows(h, self.ENUM(cb), 0)
        title = self._title(h)
        kind, roles = classify_dialog(title, " ".join(texts), buttons)
        return Dialog(h, title, " ".join(texts)[:300], buttons, "win32", kind, roles)

    def uia_dialog(self, win: Win) -> Dialog | None:
        """Boxes drawn inside the window itself (Windows 11 Notepad, Word...): read their buttons with UI Automation."""
        data = self._ps(_UIA_LIST.replace("{hwnd}", str(int(win.hwnd))), timeout=8)
        try:
            info = json.loads(data) if data else {}
        except ValueError:
            return None
        buttons = [str(b) for b in (info.get("buttons") or []) if str(b).strip()]
        texts = [str(t) for t in (info.get("texts") or []) if str(t).strip()]
        if not buttons:
            return None
        text = " ".join(texts)
        kind, roles = classify_dialog("", text, buttons)
        if kind != "save":
            return None                                     # ordinary window controls: not a box that blocks closing
        return Dialog(win.hwnd, "", text[:300], buttons, "uia", kind, roles)

    def click(self, dlg: Dialog, role: str) -> bool:
        label = dlg.roles.get(role)
        if dlg.via == "uia":
            if not label:
                return False
            return self._ps(_UIA_CLICK.replace("{hwnd}", str(int(dlg.hwnd))), timeout=8,
                            env={"NOVA_UIA_BTN": label}).strip() == "ok"
        if not label:
            if role == "cancel":
                self.u.PostMessageW(dlg.hwnd, self.WM_CLOSE, 0, 0)     # the X on a box counts as Cancel
                return True
            return False
        target: list[int] = []

        def cb(ch, _lp):
            ch = int(ch or 0)
            if self._class(ch).lower() == "button" and _btn(self._title(ch)) == _btn(label):
                target.append(ch)
                return False
            return True

        self.u.EnumChildWindows(dlg.hwnd, self.ENUM(cb), 0)
        if not target:
            return False
        self.u.PostMessageW(target[0], self.BM_CLICK, 0, 0)
        return True

    def close_alt(self, hwnd: int) -> None:
        """A second way to ask a window to close (for windows that ignore the first)."""
        self._ps(_UIA_CLOSE.replace("{hwnd}", str(int(hwnd))), timeout=8)

    def _ps(self, script: str, timeout: float = 8, env: dict | None = None) -> str:
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
                capture_output=True, timeout=timeout, creationflags=NO_WINDOW,
                env={**os.environ, **(env or {})})
            return proc.stdout.decode("utf-8", "replace").strip()
        except Exception:
            return ""


# ---------------------------------------------------------------------------------------------- names
def _stems_of(app) -> list[str]:
    out = []
    for t in app.targets:
        m = re.search(r"--processStart\s+(\S+?)(?:\.exe)?\s*$", t, re.I)
        if m:
            out.append(m.group(1).lower())
            continue
        base = re.split(r"[\\/]", t.split(" --")[0].split(" ")[0] if "\\" not in t and "/" not in t else t)[-1].lower()
        if base.endswith(".exe"):
            out.append(base[:-4])
    out += EXTRA_STEMS.get(app.name, [])
    return list(dict.fromkeys(x for x in out if x))


class NameBook:
    """Which names belong to which running program, built from the app registry (with your apps.json) and extras."""

    def __init__(self, catalog):
        self.by_stem: dict[str, list] = {}
        self.registry: dict = {}
        try:
            self.registry = catalog.registry()
            seen = {}
            for app in self.registry.values():
                seen[id(app)] = app
            for app in seen.values():
                for stem in _stems_of(app):
                    self.by_stem.setdefault(stem, []).append(app)
        except Exception:
            pass

    def label_and_names(self, w: Win) -> tuple[str, set]:
        exe = w.exe
        title_l = w.title.lower()
        if exe in CONSOLE_HOSTS:
            kind, extra = console_kind(w.title, exe)
            names = {apps.norm(x) for x in (kind, *extra) if x}
            reg = self.registry
            for key in (kind, "Terminal"):                    # your own words for it (apps.json, the Arabic and Swahili ones)
                app = reg.get(apps.norm(key))
                if app is not None:
                    names.update(apps.norm(x) for x in (app.name, *app.aliases))
            return kind, {n for n in names if n}
        if exe == "explorer":
            return "File Explorer", {"file explorer", "explorer", "folder", "folders", "file manager", "files"}
        names: set = set()
        label = ""
        for app in self.by_stem.get(exe, []):
            names.update(apps.norm(x) for x in (app.name, *app.aliases))
            label = label or app.name
        if exe == "applicationframehost" or not label:
            seg = _title_segments(w.title)
            if exe == "applicationframehost" and seg:
                label = label or seg[-1]
            label = label or _pretty(exe) or (seg[-1] if seg else "that app")
        names.update({apps.norm(exe), apps.norm(label)})
        return label, {n for n in names if n}


def _pretty(stem: str) -> str:
    s = re.sub(r"[._-]+", " ", stem).strip()
    return s[:1].upper() + s[1:] if s else ""


def _title_segments(title: str) -> list[str]:
    segs = [s.strip() for s in re.split(r"\s+[-–—|]\s+", title) if s.strip()]
    return segs or ([title.strip()] if title.strip() else [])


def console_kind(title: str, exe: str = "") -> tuple[str, list]:
    t = title.lower()
    if "powershell" in t or "pwsh" in t or exe in ("powershell", "pwsh"):
        return "PowerShell", ["powershell", "power shell", "shell", "terminal", "console", "windows terminal",
                              "command line", "pwsh", "ps"]
    if "command prompt" in t or re.match(r"^(?:administrator: )?(?:c:\\|cmd)", t) or exe == "cmd":
        return "Command Prompt", ["command prompt", "cmd", "terminal", "console", "command line", "dos"]
    if "ubuntu" in t or "wsl" in t or exe in ("wsl", "wslhost"):
        return "WSL", ["wsl", "ubuntu", "linux", "terminal", "console"]
    if "git bash" in t or "mingw" in t:
        return "Git Bash", ["git bash", "bash", "terminal", "console"]
    return "Terminal", ["terminal", "console", "windows terminal", "command line", "shell"]


# ---------------------------------------------------------------------------------------------- the logic
class Closer:
    def __init__(self, backend, catalog=None, cfg: dict | None = None, sleep=time.sleep, clock=time.monotonic):
        self.b = backend
        self.cfg = cfg or {}
        self.sleep, self.clock = sleep, clock
        self.cat = catalog or apps.get_catalog(cfg)
        self._book: NameBook | None = None

    @property
    def book(self) -> NameBook:
        if self._book is None:
            self._book = NameBook(self.cat)
        return self._book

    def _wait(self) -> float:
        try:
            return max(0.5, float(self.cfg.get("close_wait", 3.0)))
        except (TypeError, ValueError):
            return 3.0

    # -- listing ------------------------------------------------------------------------------------
    def list_open(self) -> str:
        counts: dict[str, int] = {}
        for w in self.b.windows():
            if w.protected:
                continue
            label, _n = self.book.label_and_names(w)
            counts[label] = counts.get(label, 0) + 1
        if not counts:
            return "I don't see any open app windows."
        parts = [f"{k} ({v} windows)" if v > 1 else k for k, v in counts.items()]
        return "Open right now: " + ", ".join(parts) + "."

    # -- matching -----------------------------------------------------------------------------------
    def _rank(self, q: str, w: Win, titles_only: bool = False) -> float:
        label, names = self.book.label_and_names(w)
        best = 0.0
        if not titles_only:
            for n in names:
                best = max(best, apps.score(q, n))
        for seg in _title_segments(w.title):
            n = apps.norm(seg)
            if not n:
                continue
            s = apps.score(q, n)
            if re.search(rf"(?<!\w){re.escape(q)}(?!\w)", n):
                s = max(s, 0.9)
            best = max(best, s * 0.99)
        return best

    def _pick(self, q: str, wins: list[Win], titles_only: bool = False) -> tuple[list[Win], str]:
        ranked = [(self._rank(q, w, titles_only), w) for w in wins]
        ranked = [(s, w) for s, w in ranked if s >= apps.AUTO_SCORE]
        if not ranked:
            opts = self._suggest(q, wins)
            extra = f" Did you mean {apps.AppCatalog._say_list(opts, 'or')}?" if opts else ""
            raise ToolError(f"I couldn't find an open window for '{q}'.{extra}")
        ranked.sort(key=lambda r: -r[0])
        top = ranked[0][0]
        groups: dict[str, list] = {}
        for s, w in ranked:
            if s >= top - 0.03:
                groups.setdefault(self.book.label_and_names(w)[0], []).append(w)
        if len(groups) > 1 and top < 0.97:
            raise ToolError(f"More than one open window fits '{q}': {apps.AppCatalog._say_list(list(groups)[:4])}. "
                            "Which one should I close?")
        if len(groups) > 1:
            if top < 0.97:
                raise ToolError(f"More than one open window fits '{q}': "
                                f"{apps.AppCatalog._say_list(list(groups)[:4])}. Which one should I close?")
            if q in TERMINAL_WORDS:                           # "close the terminal": every console-type window
                return [w for g in groups.values() for w in g], "terminal"
            raise ToolError(f"'{q}' fits more than one open app: {apps.AppCatalog._say_list(list(groups)[:4])}. "
                            "Which one should I close?")
        label = next(iter(groups))
        return groups[label], label

    def _suggest(self, q: str, wins: list[Win]) -> list[str]:
        best: dict[str, float] = {}
        for w in wins:
            if w.protected:
                continue
            s = self._rank(q, w)
            if s >= apps.ASK_SCORE:
                best[self.book.label_and_names(w)[0]] = max(s, best.get(self.book.label_and_names(w)[0], 0))
        return [k for k, _v in sorted(best.items(), key=lambda kv: -kv[1])[:3]]

    # -- observing what a window does after we ask it to close -----------------------------------------
    def _find_dialog(self, w: Win, uia: bool = False) -> Dialog | None:
        for d in self.b.dialogs(w):
            return d
        return self.b.uia_dialog(w) if uia else None

    def _observe(self, w: Win, post: bool) -> tuple[str, Dialog | None]:
        """Ask the window to close and see what happens: ('closed'|'dialog'|'hung'|'stuck', dialog)."""
        if post:
            self.b.post_close(w.hwnd)
        start, deadline = self.clock(), self.clock() + self._wait()
        uia_done = alt_done = False
        while True:
            if not self.b.exists(w.hwnd):
                return "closed", None
            d = None
            for d in self.b.dialogs(w):
                break
            if d is not None:
                return "dialog", d
            elapsed = self.clock() - start
            if not uia_done and elapsed >= 0.8:
                uia_done = True
                d = self.b.uia_dialog(w)
                if d is not None:
                    return "dialog", d
            if not alt_done and elapsed >= 1.6 and self.b.exists(w.hwnd):
                alt_done = True
                self.b.close_alt(w.hwnd)
            if self.clock() >= deadline:
                break
            self.sleep(0.15)
        if not self.b.exists(w.hwnd):
            return "closed", None
        return ("hung" if self.b.hung(w.hwnd) else "stuck"), None

    # -- one window ----------------------------------------------------------------------------------
    def _close_one(self, w: Win, label: str, args: dict, ctx: dict) -> dict:
        """Returns a status {kind, text}. May raise NeedsChoice (the runner asks, then calls the tool again)."""
        dec = (args.get("_decisions") or {}).get(str(w.hwnd))
        if dec == "force":
            return self._force(w, label)
        if dec == "leave":
            return {"kind": "open", "text": f"I left {label} open."}
        dlg = None
        if dec in ("save", "discard", "cancel"):
            dlg = self._find_dialog(w, uia=True)
            if dlg is None:
                state, dlg = self._observe(w, post=True)
                if state == "closed":
                    return {"kind": "closed", "text": ""}
        else:
            state, dlg = None, self._find_dialog(w)
            if dlg is None:
                state, dlg = self._observe(w, post=True)
                if state == "closed":
                    return {"kind": "closed", "text": ""}
                if state in ("hung", "stuck"):
                    why = "isn't responding" if state == "hung" else "didn't close"
                    raise self._choice(args, ctx, [(f"{label} {why}. Force close it? Anything unsaved will be lost.",
                                                    self._dec(args, w, "force"))], self._dec(args, w, "leave"))
        if dlg is None:
            return {"kind": "open", "text": f"{label} is still open."}
        if dlg.kind != "save":
            what = f" ('{dlg.title}')" if dlg.title else ""
            return {"kind": "manual", "text": f"{label} is asking something I don't recognise{what}, so I left it "
                                              "for you to answer."}
        if dec is None:
            raise self._choice(
                args, ctx,
                [(f"{label} has unsaved changes. Save them before closing?", self._dec(args, w, "save")),
                 (f"Close {label} without saving? Your changes will be lost.", self._dec(args, w, "discard"))],
                self._dec(args, w, "cancel"))
        return self._answer_save(w, dlg, dec, label)

    def _answer_save(self, w: Win, dlg: Dialog, dec: str, label: str) -> dict:
        if not self.b.click(dlg, dec):
            return {"kind": "manual", "text": f"I couldn't press the button in {label}'s box, so it's still waiting "
                                              "for you."}
        if dec == "cancel":
            return {"kind": "kept", "text": f"Okay, I left {label} open."}
        state, d2 = self._observe(w, post=False)
        if state == "closed":
            return {"kind": "saved" if dec == "save" else "discarded", "text": ""}
        if state == "dialog" and d2 is not None:
            t = f"{d2.title} {d2.text}".lower()
            if "save as" in t or "file name" in t or "save" in t:
                return {"kind": "manual", "text": f"{label} needs a file name before it can save, so I left the Save As "
                                                  "box open for you."}
            return {"kind": "manual", "text": f"{label} is asking one more thing, so I left it for you."}
        return {"kind": "open", "text": f"{label} is still open after that."}

    def _force(self, w: Win, label: str) -> dict:
        pid = w.target_pid
        exe = self.b.exe_of(pid) if hasattr(self.b, "exe_of") else w.exe
        if w.protected or exe in NEVER_KILL or w.exe in NEVER_KILL:
            return {"kind": "open", "text": f"{label} is part of Windows or a program I share with, so I won't force "
                                            "it to close."}
        self.b.kill_tree(pid)
        for _ in range(10):
            if not self.b.exists(w.hwnd):
                return {"kind": "forced", "text": f"Force closed {label}."}
            self.sleep(0.2)
        return {"kind": "open", "text": f"I couldn't force {label} to close."}

    # -- questions for the runner --------------------------------------------------------------------
    @staticmethod
    def _dec(args: dict, w: Win, value: str) -> dict:
        return {"_decisions": {**(args.get("_decisions") or {}), str(w.hwnd): value}}

    def _choice(self, args: dict, ctx: dict, steps: list, decline: dict | None) -> NeedsChoice:
        carry = {"_part": ctx["part"], "_done": list(ctx["results"]), "_skip": sorted(ctx["skip"]),
                 "_quit": None, "_qlabel": ""}
        steps = [(p, {**carry, **u}) for p, u in steps]
        return NeedsChoice(steps[0][0], steps, {**carry, **decline} if decline else None)

    # -- groups and the whole request -----------------------------------------------------------------
    def _close_group(self, group: list[Win], label: str, args: dict, ctx: dict, app_scope: bool) -> None:
        results, skip = ctx["results"], ctx["skip"]
        usable = []
        for w in group:
            if w.hwnd in skip:
                continue
            if w.protected:
                results.append({"kind": "open", "label": label,
                                "text": f"{label} is the window I'm running in, so closing it would close me. "
                                        "Close that one yourself if you want it gone."})
                skip.add(w.hwnd)
            elif w.exe in SYSTEM_EXES:
                results.append({"kind": "open", "label": label, "text": f"{label} is part of Windows, so I won't close it."})
                skip.add(w.hwnd)
            else:
                usable.append(w)
        try:
            limit = int(self.cfg.get("close_ask_over", 2))
        except (TypeError, ValueError):
            limit = 2
        part = ctx["part"]
        if part in (args.get("_none_parts") or []):
            results.append({"kind": "kept", "label": label, "text": f"Okay, I left your {label} windows open."})
            skip.update(w.hwnd for w in usable)
            return
        if len(usable) > limit and part not in (args.get("_all_parts") or []):
            raise self._choice(args, ctx, [(f"You have {len(usable)} {label} windows open. Close all of them?",
                                            {"_all_parts": [*(args.get("_all_parts") or []), part]})],
                               {"_none_parts": [*(args.get("_none_parts") or []), part]})
        closed_pids: list[int] = []
        for w in usable:
            st = self._close_one(w, label, args, ctx)
            st["label"] = label
            results.append(st)
            skip.add(w.hwnd)
            if st["kind"] in ("closed", "saved", "discarded", "forced"):
                closed_pids.append(w.target_pid)
        if app_scope and closed_pids and self.cfg.get("close_background", "ask") != "leave":
            self._background(closed_pids, label, args, ctx)

    def _background(self, pids: list[int], label: str, args: dict, ctx: dict) -> None:
        """The windows are gone but the program may live on in the tray (Spotify, Discord...)."""
        self.sleep(1.0)
        still_windows = {w.target_pid for w in self.b.windows()}
        own = self.b.own_pids() if hasattr(self.b, "own_pids") else set()
        left = []
        for pid in dict.fromkeys(pids):
            if pid in still_windows or pid in own or not self.b.alive(pid):
                continue
            exe = self.b.exe_of(pid) if hasattr(self.b, "exe_of") else ""
            if exe in NEVER_KILL or exe in CONSOLE_HOSTS:
                continue
            left.append(pid)
        if not left:
            return
        if self.cfg.get("close_background", "ask") == "quit":
            self._quit(left, label, ctx["results"])
            return
        raise self._choice(args, ctx, [(f"{label} is still running in the background. Quit it completely?",
                                        {"_quit": left, "_qlabel": label})], {"_quit": [], "_qlabel": label})

    def _quit(self, pids: list[int], label: str, results: list) -> None:
        for pid in pids:
            self.b.kill_tree(pid)
        results.append({"kind": "quit", "label": label, "text": f"Quit {label} completely."})

    def _compose(self, results: list[dict]) -> str:
        closed: dict[str, int] = {}
        lines: list[str] = []
        failure = False
        for r in results:
            k, label = r["kind"], r.get("label", "")
            if k == "closed":
                closed[label] = closed.get(label, 0) + 1
            elif k == "saved":
                lines.append(f"Saved your changes and closed {label}.")
            elif k == "discarded":
                lines.append(f"Closed {label} without saving.")
            elif k == "forced":
                lines.append(r["text"])
            elif k == "quit":
                lines.append(r["text"])
            elif k in ("kept",):
                lines.append(r["text"])
            elif k in ("open", "manual", "error"):
                lines.append(r["text"])
                failure = True
        head = []
        for label, n in closed.items():
            head.append(f"Closed {label}." if n == 1 else f"Closed all {n} {label} windows.")
        text = " ".join(head + lines).strip()
        good = bool(head) or any(r["kind"] in ("saved", "discarded", "forced", "quit", "kept") for r in results)
        if failure and not good:
            raise ToolError(text or "I couldn't close that.")
        return text or "Done."

    # -- tool entry points ------------------------------------------------------------------------------
    def close_app(self, name: str, args: dict) -> str:
        parts = self._split(name)
        ctx = {"part": int(args.get("_part", 0) or 0), "results": list(args.get("_done") or []),
               "skip": set(args.get("_skip") or [])}
        first = ctx["part"]
        if args.get("_quit") is not None:                     # the answer to "quit it completely?"
            label = args.get("_qlabel") or "it"
            if args["_quit"]:
                self._quit(list(args["_quit"]), label, ctx["results"])
            else:
                ctx["results"].append({"kind": "kept", "label": label,
                                       "text": f"Closed its window. {label} keeps running in the background."})
            first += 1
        for i in range(first, len(parts)):
            ctx["part"] = i
            try:
                self._close_part(parts[i], args, ctx)
            except NeedsChoice:
                raise
            except ToolError as exc:
                ctx["results"].append({"kind": "error", "label": "", "text": str(exc)})
        return self._compose(ctx["results"])

    def _split(self, name: str) -> list[str]:
        q = str(name or "").strip()
        if not apps.norm(q):
            return [""]
        parts = [p for p in re.split(r"\s*(?:,|&|\band\b|\bthen\b|\bna\b)\s*", q, flags=re.I) if p.strip()]
        if len(parts) > 1 and not self._known_whole(q):
            return parts
        return [q]

    def _known_whole(self, q: str) -> bool:
        try:
            plan = self.cat.plan(q)
        except ToolError:
            return False
        except Exception:
            return False
        return plan is not None and plan.score >= 0.97

    @staticmethod
    def _is_active(raw: str) -> bool:
        q = apps.norm(raw)
        return not q or q in ACTIVE_WORDS or bool(re.match(r"^\s*(?:this|that|current|active)\b", str(raw).lower()))

    def _close_part(self, raw: str, args: dict, ctx: dict) -> None:
        q = apps.norm(raw)
        if q in EVERYTHING:
            raise ToolError("I won't close everything at once. Tell me which apps to close.")
        wins = self.b.windows()
        if self._is_active(raw):
            rest = apps.norm(re.sub(r"^\s*(?:this|that|current|active)\s+", "", str(raw).lower()))
            return self._close_active(rest, wins, args, ctx, bare=not q)
        group, label = self._pick(q, wins)
        self._close_group(group, label, args, ctx, app_scope=True)

    def close_window(self, title: str, args: dict) -> str:
        ctx = {"part": 0, "results": list(args.get("_done") or []), "skip": set(args.get("_skip") or [])}
        q = apps.norm(title)
        wins = self.b.windows()
        if self._is_active(title):
            rest = apps.norm(re.sub(r"^\s*(?:this|that|current|active)\s+", "", str(title).lower()))
            self._close_active(rest, wins, args, ctx, bare=not q)
        else:
            group, label = self._pick(q, wins)
            ranked = sorted(group, key=lambda w: -self._rank(q, w, titles_only=True))
            best = self._rank(q, ranked[0], titles_only=True)
            titles = {w.title for w in ranked if self._rank(q, w, titles_only=True) >= best - 0.01}
            if len(titles) > 1 and best < 0.97:
                raise ToolError("Several windows fit that: " + "; ".join(sorted(t[:50] for t in titles)[:4])
                                + ". Which one?")
            self._close_group(ranked[:1], label, args, ctx, app_scope=False)
        return self._compose(ctx["results"])

    def _close_active(self, q: str, wins: list[Win], args: dict, ctx: dict, bare: bool = False) -> None:
        """'close this window': the one in front (never Nova's own). If the user also said a name that doesn't
        fit it ('close this partial'), check first: 'Do you mean the PowerShell window?'"""
        fg = self.b.foreground()
        cands = [w for w in wins if not w.protected]
        pref = args.get("_active_hwnd")
        target = (next((w for w in cands if w.hwnd == pref), None) if pref else None) \
            or next((w for w in cands if w.hwnd == fg), None) or (cands[0] if cands else None)
        if target is None:
            raise ToolError("I can't see an open window to close.")
        label, names = self.book.label_and_names(target)
        rest = " ".join(x for x in q.split() if x not in _FILLER)
        part_ok = ctx["part"] in (args.get("_active_ok_parts") or [])
        if not part_ok:
            fit = max([apps.score(rest, n) for n in names] + [self._rank(rest, target)]) if rest else 1.0
            if bare or fit < apps.AUTO_SCORE:           # a bare "close", or a name that doesn't fit: check first
                ask = f"Close the {label} window?" if bare else f"Do you mean the {label} window?"
                raise self._choice(args, ctx, [(ask, {"_active_ok_parts": [*(args.get("_active_ok_parts") or []),
                                                                           ctx["part"]],
                                                       "_active_hwnd": target.hwnd})], None)
        self._close_group([target], label, args, ctx, app_scope=False)
