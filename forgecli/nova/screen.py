"""Screen, window and camera access. Everything happens on demand; nothing is stored unless asked."""
from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import logging
import os
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from ctypes import wintypes

from forgecli.nova.tools.base import ToolError
from forgecli.nova.core.config import ROOT, SCRIPTS
from forgecli.nova.core.keypool import gemini_pool

log = logging.getLogger("brain.screen")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if os.name == "nt" else None


class ScreenTools:
    def __init__(self, cfg: dict):
        self.cfg = cfg["awareness"]
        self._user32 = None
        self._dpi_done = False
        self.brain_router = None  # set by brain/server.py so ask_vision can use OpenRouter first

    def _u32(self):
        if self._user32 is None:
            u = ctypes.WinDLL("user32", use_last_error=True)
            u.GetForegroundWindow.restype = wintypes.HWND
            u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            u.MonitorFromWindow.restype = wintypes.HANDLE
            u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
            u.IsWindowVisible.argtypes = [wintypes.HWND]
            u.IsIconic.argtypes = [wintypes.HWND]
            u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            u.EnumWindows.argtypes = [_ENUMPROC, wintypes.LPARAM]
            self._user32 = u
        return self._user32

    def _dpi_aware(self) -> None:
        if self._dpi_done or os.name != "nt":
            return
        self._dpi_done = True
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    def _describe(self, hwnd) -> dict:
        u = self._u32()
        length = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        u.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rect))
        process = ""
        try:
            import psutil
            process = psutil.Process(pid.value).name()
        except Exception:
            pass
        fullscreen = False
        try:
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(mi)
            u.GetMonitorInfoW(u.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
            m = mi.rcMonitor
            fullscreen = (abs(rect.left - m.left) <= 2 and abs(rect.top - m.top) <= 2
                          and abs(rect.right - m.right) <= 2 and abs(rect.bottom - m.bottom) <= 2)
        except Exception:
            pass
        return {"title": buf.value, "process": process, "pid": pid.value,
                "rect": (rect.left, rect.top, rect.right, rect.bottom), "fullscreen": fullscreen}

    def _top_other_window(self, own: set):
        u = self._u32()
        found = []

        def visit(hwnd, _lparam):
            if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd) or u.GetWindowTextLengthW(hwnd) == 0:
                return True
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in own or (u.GetWindowLongW(hwnd, -20) & 0x80):
                return True
            cloaked = wintypes.DWORD(0)
            try:
                ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            except Exception:
                pass
            cls = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, cls, 64)
            if cloaked.value or cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                return True
            found.append(hwnd)
            return False

        u.EnumWindows(_ENUMPROC(visit), 0)
        return found[0] if found else None

    def foreground_info(self) -> dict | None:
        if os.name != "nt":
            return None
        self._dpi_aware()
        try:
            u = self._u32()
            hwnd = u.GetForegroundWindow()
            if not hwnd:
                return None
            info = self._describe(hwnd)
            own = {os.getpid(), os.getppid()}
            if info["pid"] in own:
                other = self._top_other_window(own)
                if other:
                    info = self._describe(other)
                info["own"] = True
            return info
        except Exception:
            log.exception("foreground_info failed")
            return None

    def idle_seconds(self) -> float | None:
        if os.name != "nt":
            return None
        try:
            lii = _LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(lii)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                return None
            return ((ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0
        except Exception:
            return None

    @staticmethod
    def label(info: dict | None) -> str:
        if not info:
            return "unknown window"
        app = (info.get("process") or "").replace(".exe", "") or "an app"
        title = (info.get("title") or "").strip()
        return f"{app} - \"{title[:100]}\"" if title else app

    def capture_png(self, region: tuple | None = None) -> bytes:
        try:
            import mss
            import mss.tools
        except ImportError:
            raise ToolError("I need the 'mss' package to see the screen: python tools/install_vision.py") from None
        self._dpi_aware()
        with mss.mss() as sct:
            monitors = list(sct.monitors[1:]) or [sct.monitors[0]]
            ref = region or (self.foreground_info() or {}).get("rect")
            mon = monitors[0]
            if ref:
                cx, cy = (ref[0] + ref[2]) // 2, (ref[1] + ref[3]) // 2
                for m in monitors:
                    if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                        mon = m
                        break
            box = dict(mon)
            if region:
                l = max(region[0], mon["left"])
                t = max(region[1], mon["top"])
                r = min(region[2], mon["left"] + mon["width"])
                b = min(region[3], mon["top"] + mon["height"])
                if r - l >= 40 and b - t >= 40:
                    box = {"left": l, "top": t, "width": r - l, "height": b - t}
            shot = sct.grab(box)
            return mss.tools.to_png(shot.rgb, shot.size)

    def ocr_png(self, png: bytes) -> str:
        if os.name != "nt":
            raise ToolError("Reading screen text only works on Windows.")
        fd, path = tempfile.mkstemp(suffix=".png")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(png)
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(SCRIPTS / "ocr.ps1"), "-ImagePath", path],
                capture_output=True, timeout=60, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise ToolError("Reading the screen took too long.") from None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        out = proc.stdout.decode("utf-8", "replace").strip()
        if "__NO_OCR_ENGINE__" in out:
            raise ToolError("Windows has no text-recognition language installed "
                            "(Settings > Time & language > Language & region).")
        if proc.returncode != 0:
            raise ToolError("I couldn't read the screen text: "
                            + (proc.stderr.decode("utf-8", "replace").strip()[:150] or "OCR failed"))
        return out

    def camera_jpeg(self) -> bytes:
        if not self.cfg.get("camera_enabled"):
            raise ToolError("The camera is switched off in my settings (awareness.camera_enabled in config.json). "
                            "I'll still ask you each time before using it.")
        try:
            import cv2
        except ImportError:
            raise ToolError("I need OpenCV for the camera: python tools/install_vision.py") from None
        cap = cv2.VideoCapture(int(self.cfg.get("camera_index", 0)), cv2.CAP_DSHOW)
        if not cap.isOpened():
            raise ToolError("I couldn't open the camera. Is another app using it, or is it blocked in "
                            "Windows privacy settings?")
        try:
            for _ in range(6):
                cap.read()
            ok, frame = cap.read()
        finally:
            cap.release()
        if not ok or frame is None:
            raise ToolError("The camera didn't give me a picture.")
        h, w = frame.shape[:2]
        scale = 800.0 / max(h, w)
        if scale < 1:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            raise ToolError("I couldn't process the camera picture.")
        return buf.tobytes()

    @staticmethod
    def _shrink(image: bytes) -> bytes:
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(image))
            im.thumbnail((1024, 1024))
            out = io.BytesIO()
            im.convert("RGB").save(out, "JPEG", quality=80)
            return out.getvalue()
        except Exception:
            return image

    @staticmethod
    def _run_async(coro):
        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
        if not running:
            return asyncio.run(coro)
        box: dict = {}

        def runner():
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                box["result"] = loop.run_until_complete(coro)
            except BaseException as exc:
                box["error"] = exc
            finally:
                loop.close()

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join()
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def ask_vision(self, image: bytes, question: str) -> str:
        jpeg = self._shrink(image)

        router = self.brain_router
        if router is not None:
            try:
                answer = self._run_async(router.ask_image(question, jpeg))
                if answer and answer.strip():
                    return answer
            except Exception as exc:
                log.warning("cloud vision failed (%s); falling back to Gemini", exc)

        pool = gemini_pool()
        if not pool.keys():
            raise ToolError("No vision provider is available. Add OPENROUTER_API_KEY=... to .env, "
                            "or set GEMINI_API_KEY=... for the built-in fallback.")
        wanted = str(self.cfg.get("vision_model") or "gemini-3.5-flash-lite").strip()
        models = [wanted]
        body = json.dumps({
            "contents": [{"parts": [
                {"text": question},
                {"inline_data": {"mime_type": "image/jpeg",
                                 "data": base64.b64encode(jpeg).decode("ascii")}},
            ]}],
            "generationConfig": {"maxOutputTokens": 300, "temperature": 0.2},
        }).encode()
        last = "I couldn't reach the vision service."
        tried: set = set()
        for model in models:
            for key in pool.ordered():
                if (model, key) in tried:
                    continue
                tried.add((model, key))
                req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    data=body,
                    headers={"Content-Type": "application/json", "x-goog-api-key": key},
                )
                try:
                    with urllib.request.urlopen(req, timeout=40) as resp:
                        data = json.load(resp)
                except urllib.error.HTTPError as exc:
                    try:
                        detail = exc.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        detail = ""
                    if exc.code == 404:
                        last = (f"The vision model '{model}' wasn't found - check awareness.vision_model in "
                                f"config.json (and that your Gemini key has access to it).")
                        break
                    kind = pool.classify(key, f"{exc.code} {detail}") if exc.code in (401, 403, 429) else "other"
                    if kind == "quota":
                        last = ("All my Gemini keys have reached their limit for now."
                                if len(pool.keys()) > 1 else "The Gemini key has reached its limit for now.")
                        continue
                    if kind == "bad":
                        last = "Google rejected a Gemini key."
                        continue
                    last = f"The vision service failed (error {exc.code}): {detail[:200]}"
                    continue
                except (urllib.error.URLError, TimeoutError):
                    last = "I couldn't reach the vision service. Check the internet connection."
                    continue
                parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                answer = " ".join(str(p.get("text", "")) for p in parts).strip()
                if answer:
                    pool.ok(key)
                    return answer
                last = "The vision service didn't give an answer."
        raise ToolError(last)