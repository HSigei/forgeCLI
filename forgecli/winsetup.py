"""The Windows download installs itself.

ForgeSetup.exe (the standalone forge.exe under another name) copies itself to
%LOCALAPPDATA%\\Programs\\Forge\\forge.exe, puts that folder on the user's PATH, adds a Start menu
entry and opens Forge. No admin rights, no Python. Running it again updates in place; that's also
how the .exe build updates itself (`setup_cmd`).

    ForgeSetup.exe                    double-clicked: install and open Forge
    forge.exe --install [--quiet]     the same from any copy (quiet: no window, no prompt)
    forge --uninstall                 take it off PATH and the Start menu, delete the folder
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

from . import __version__

SETUP_URL = os.environ.get("FORGE_SETUP_URL",
                           "https://github.com/HSigei/forgeCLI/releases/latest/download/ForgeSetup.exe")


def install_dir() -> pathlib.Path:
    base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
    return pathlib.Path(base) / "Programs" / "Forge"


def _shortcut() -> pathlib.Path:
    return pathlib.Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Forge.lnk"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def installed_here() -> bool:
    """This forge.exe is the installed one (so it can update itself with setup_cmd)."""
    try:
        return frozen() and pathlib.Path(sys.executable).resolve().parent == install_dir().resolve()
    except OSError:
        return False


def wanted(argv: list[str]) -> bool:
    """Run as the installer: asked for, or the downloaded ForgeSetup.exe opened with no arguments."""
    if not frozen():
        return False
    if "--install" in argv or "--uninstall" in argv:
        return True
    return not argv and pathlib.Path(sys.executable).stem.lower().startswith("forgesetup")


def run(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    try:
        if "--uninstall" in argv:
            return uninstall()
        return install(quiet)
    except Exception as e:
        if quiet:
            return 1
        print(f"\n  Forge couldn't install: {e}\n")
        _pause()
        return 1


def install(quiet: bool = False) -> int:
    d = install_dir()
    d.mkdir(parents=True, exist_ok=True)
    target = d / "forge.exe"
    src = pathlib.Path(sys.executable).resolve()
    if not quiet:
        print(f"\n  Installing Forge {__version__} …")
    fresh = not target.exists()
    if src != target.resolve():
        for attempt in range(60):          # an open Forge locks forge.exe: wait for it to close
            try:
                shutil.copy2(src, target)
                break
            except PermissionError:
                if attempt == 0 and not quiet:
                    print("  Waiting for an open Forge to close …")
                time.sleep(1)
        else:
            raise RuntimeError("forge.exe is in use — close Forge and run this again")
    _add_to_path(str(d))
    _make_shortcut(target)
    _record(update=not fresh)
    if quiet:
        return 0
    print(f"  ✓ Installed to {d}")
    print("  ✓ Added to your PATH — type  forge  in any new terminal")
    print("  ✓ Added Forge to the Start menu\n")
    print("  Opening Forge …")
    try:
        subprocess.Popen([str(target)], cwd=str(pathlib.Path.home()),
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10), close_fds=True)
    except OSError:
        _pause()
    time.sleep(1.5)
    return 0


def uninstall() -> int:
    d = install_dir()
    _remove_from_path(str(d))
    try:
        _shortcut().unlink()
    except OSError:
        pass
    # forge.exe can't delete itself while it runs (nor while its one-file launcher, the parent
    # process, is still around): a hidden shell finishes once both have closed
    q = str(d).replace("'", "''")
    ps = (f"Wait-Process -Id {os.getpid()},{os.getppid()} -Timeout 60 -ErrorAction SilentlyContinue; "
          f"for ($i = 0; $i -lt 30 -and (Test-Path '{q}'); $i++) "
          f"{{ Remove-Item -Recurse -Force '{q}' -ErrorAction SilentlyContinue; Start-Sleep 1 }}")
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                     creationflags=0x08000000 | 0x00000200, close_fds=True)
    print("Forge is uninstalled (your settings in ~/.forge are kept).")
    return 0


def setup_cmd(pid: int | None = None) -> list[str]:
    """A command that waits for this Forge to close, downloads the newest ForgeSetup.exe and installs
    it quietly over this one."""
    pid = pid or os.getpid()
    url = SETUP_URL.replace("'", "''")
    ps = ("$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; "
          f"Wait-Process -Id {pid} -Timeout 604800 -ErrorAction SilentlyContinue; "
          "$f = Join-Path $env:TEMP 'ForgeSetup.exe'; "
          f"Invoke-WebRequest -UseBasicParsing '{url}' -OutFile $f; "
          "$p = Start-Process -FilePath $f -ArgumentList '--install','--quiet' -Wait -PassThru -WindowStyle Hidden; "
          "exit $p.ExitCode")
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps]


# --- helpers ----------------------------------------------------------------------------------------
def _pause() -> None:
    try:
        input("  Press Enter to close ")
    except (EOFError, KeyboardInterrupt):
        pass


def _user_path() -> tuple[str, int]:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        try:
            v, t = winreg.QueryValueEx(k, "Path")
            return str(v), t
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ


def _set_user_path(value: str, kind: int) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Path", 0, kind, value)
    try:   # tell Explorer, so new terminals see it without signing out
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 3000, None)
    except Exception:
        pass


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.expandvars(a).rstrip("\\/")) == os.path.normcase(b.rstrip("\\/"))


def _add_to_path(d: str) -> None:
    cur, kind = _user_path()
    parts = [p for p in cur.split(";") if p]
    if not any(_same(p, d) for p in parts):
        _set_user_path(";".join(parts + [d]), kind)


def _remove_from_path(d: str) -> None:
    cur, kind = _user_path()
    parts = [p for p in cur.split(";") if p]
    keep = [p for p in parts if not _same(p, d)]
    if keep != parts:
        _set_user_path(";".join(keep), kind)


def _make_shortcut(target: pathlib.Path) -> None:
    lnk = _shortcut()
    q = lambda s: str(s).replace("'", "''")   # noqa: E731
    ps = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}'); "
          f"$s.TargetPath = '{q(target)}'; $s.WorkingDirectory = '{q(pathlib.Path.home())}'; "
          "$s.Description = 'Forge, the terminal coding agent'; $s.Save()")
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000,
                       capture_output=True, timeout=30)
    except Exception:
        pass


def _record(update: bool) -> None:
    """Tell the updater which version is now installed (a quiet update greets "Updated to vX" next start)."""
    p = pathlib.Path(os.environ.get("FORGE_HOME") or pathlib.Path.home() / ".forge") / "update.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st["installed"] = __version__
    if not update:
        st["announced"] = __version__     # a fresh install isn't news
    auto = st.get("auto") or {}
    auto.pop("pid", None)
    auto["fails"] = 0
    st["auto"] = auto
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass
