"""Forge Remote: drive this Forge from your phone while you're away.

The laptop and the phone meet in a room on a small relay (a Cloudflare Worker, remote/relay). Every
message is encrypted end to end with AES-256-GCM using a key derived from a secret that only the
paired laptop and phone know: the relay sees opaque bytes and a room id derived from the same
secret, so it can't read, change or guess anything. Pairing = scanning a QR code once.

The phone sees what the chat shows (replies, actions, approvals, questions), can send a message,
answer a question or approval, stop the task and ask for a screenshot.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import secrets
import socket
import threading
import time
from collections import deque

from .config import HOME_DIR

RELAY = os.environ.get("FORGE_RELAY", "wss://forge-relay.harrysigei.workers.dev")
APP_URL = os.environ.get("FORGE_REMOTE_APP", "https://forgecli.pages.dev/app/")
MAX_AGE = 300            # seconds: older messages are replays and are dropped
HISTORY = 200            # events kept so a phone that connects late sees the conversation


def _path() -> str:
    return os.path.join(os.environ.get("FORGE_HOME", str(HOME_DIR)), "remote.json")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load_secret(new: bool = False) -> str:
    """The pairing secret, made once and kept in ~/.forge/remote.json. new=True forgets every phone."""
    try:
        with open(_path(), encoding="utf-8") as f:
            s = json.load(f).get("secret", "")
    except Exception:
        s = ""
    if new or len(s) < 40:
        s = _b64(secrets.token_bytes(32))
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump({"secret": s}, f)
        try:
            os.chmod(_path(), 0o600)
        except OSError:
            pass
    return s


def room_of(secret: str) -> str:
    return hashlib.sha256(("forge-room:" + secret).encode()).hexdigest()[:48]


def key_of(secret: str) -> bytes:
    return hashlib.sha256(("forge-key:" + secret).encode()).digest()


def pair_link(secret: str) -> str:
    # The secret rides in the #fragment, which browsers never send to the server.
    return f"{APP_URL}#pair={secret}"


def seal(key: bytes, msg: dict) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    body = json.dumps({**msg, "ts": time.time()}, ensure_ascii=False).encode()
    return json.dumps({"n": _b64(nonce), "c": _b64(AESGCM(key).encrypt(nonce, body, b"forge"))})


def unseal(key: bytes, frame: str) -> dict | None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        f = json.loads(frame)
        if "relay" in f:
            return {"t": "relay", **f}
        msg = json.loads(AESGCM(key).decrypt(_unb64(f["n"]), _unb64(f["c"]), b"forge"))
    except Exception:
        return None           # not ours, tampered with, or junk
    if abs(time.time() - float(msg.get("ts", 0))) > MAX_AGE:
        return None
    return msg


def qr_text(data: str) -> str:
    """A QR code drawn with half-block characters, small enough for a normal terminal."""
    import qrcode
    q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    q.add_data(data)
    q.make(fit=True)
    m = q.get_matrix()
    if len(m) % 2:
        m.append([False] * len(m[0]))
    out = []
    for y in range(0, len(m), 2):
        row = ""
        for a, b in zip(m[y], m[y + 1]):
            row += {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}[(not a, not b)]
        out.append(row)
    return "\n".join(out)


def screenshot_jpeg(max_w: int = 1280) -> bytes:
    """The whole screen, scaled down, as JPEG bytes."""
    from PIL import Image
    try:
        import mss
        with mss.mss() as s:
            g = s.grab(s.monitors[0])
            img = Image.frombytes("RGB", g.size, g.bgra, "raw", "BGRX")
    except ImportError:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True).convert("RGB")
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)))
    import io
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=60)
    return buf.getvalue()


# what the chat shows, mirrored to the phone: UI method → event kind
MIRROR = {"assistant": "assistant", "action": "action", "info": "info", "ok": "ok", "warn": "warn",
          "error": "error", "failover": "warn", "plan": "plan", "tool_result": "result",
          "edited": "edited", "wrote": "wrote", "todos": "todos"}


class Remote:
    """One laptop's connection to the relay, running in a background thread."""

    def __init__(self, app, secret: str | None = None):
        self.app, self.ui = app, app.ui
        self.secret = secret or load_secret()
        self.key = key_of(self.secret)
        self.url = f"{RELAY}/room/{room_of(self.secret)}?role=laptop"
        self.history: deque = deque(maxlen=HISTORY)
        self.phones = 0
        self.ws = None
        self.lock = threading.Lock()
        self.running = False
        self.thread: threading.Thread | None = None
        self._asked = None          # the question the phone was last told about
        self._was_busy = None
        self._patched = False

    # mirroring ---------------------------------------------------------------------
    def attach(self) -> None:
        """Wrap the UI's output methods so whatever the chat shows also goes to the phone."""
        if self._patched:
            return
        self._patched = True
        ui = self.ui
        for name, kind in MIRROR.items():
            orig = getattr(ui, name, None)
            if orig is None:
                continue

            def wrap(*a, _orig=orig, _name=name, _kind=kind, **k):
                r = _orig(*a, **k)
                try:
                    self.event(_kind, _describe(_name, a, k))
                except Exception:
                    pass
                return r
            setattr(ui, name, wrap)
        orig_run = self.app.run

        def run(text, *a, _orig=orig_run, **k):
            self.event("user", text)
            return _orig(text, *a, **k)
        self.app.run = run
        for name in ("confirm", "ask"):
            orig = getattr(ui, name, None)
            if orig is None:
                continue

            def wrap_q(*a, _orig=orig, _name=name, **k):
                try:   # show the phone what the question is about before the buttons appear
                    self.event("plan", _describe(_name, a, k))
                except Exception:
                    pass
                return _orig(*a, **k)
            setattr(ui, name, wrap_q)
        orig_input = ui._input

        def _input(markup, label, _orig=orig_input):
            threading.Timer(0.05, self._sync_question).start()   # once ui.pending is set
            try:
                return _orig(markup, label)
            finally:
                self._sync_question()
        ui._input = _input

    def event(self, kind: str, text: str) -> None:
        if not text:
            return
        e = {"t": "event", "kind": kind, "text": text[:20000], "at": time.time()}
        self.history.append(e)
        self.send(e)

    def _sync_question(self) -> None:
        p = self.ui.pending
        q = (p.get("label"), id(p)) if p else None
        if q == self._asked:
            return
        self._asked = q
        if p:
            self.send({"t": "ask", "label": p.get("label", ""), "update": bool(p.get("update"))})
        else:
            self.send({"t": "asked"})

    def status(self) -> dict:
        busy = bool(self.app.busy())
        return {"t": "status", "busy": busy, "name": socket.gethostname(), "os": platform.system(),
                "cwd": os.getcwd(), "version": _version(), "model": _model(self.app)}

    # the connection ----------------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self.attach()
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="forge-remote")
        self.thread.start()
        threading.Thread(target=self._watch, daemon=True, name="forge-remote-watch").start()

    def stop(self) -> None:
        self.running = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def send(self, msg: dict) -> None:
        ws = self.ws
        if not ws or not self.phones:
            return
        try:
            with self.lock:
                ws.send(seal(self.key, msg))
        except Exception:
            pass

    def _watch(self) -> None:
        """Tell the phone when a task starts or ends, and about questions asked without _input."""
        while self.running:
            time.sleep(0.5)
            try:
                b = bool(self.app.busy())
                if b != self._was_busy:
                    self._was_busy = b
                    self.send(self.status())
                self._sync_question()
            except Exception:
                pass

    def _run(self) -> None:
        from websockets.sync.client import connect
        delay = 1
        while self.running:
            try:
                with connect(self.url, open_timeout=15, max_size=8 * 1024 * 1024,
                             ping_interval=25, ping_timeout=20) as ws:
                    self.ws, delay = ws, 1
                    for frame in ws:
                        if not self.running:
                            break
                        if isinstance(frame, bytes):
                            continue
                        msg = unseal(self.key, frame)
                        if msg:
                            self._handle(msg)
            except Exception:
                pass
            self.ws, self.phones = None, 0
            if self.running:
                time.sleep(delay)
                delay = min(delay * 2, 30)

    def _handle(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "relay":
            before, self.phones = self.phones, int(msg.get("phones") or 0)
            if self.phones > before:
                self.ui.info(f"📱 phone connected ({self.phones})")
                self._hello()
            elif self.phones < before:
                self.ui.info("📱 phone disconnected")
            return
        if t == "hello" or t == "sync":
            self._hello()
        elif t == "say":
            self._say(str(msg.get("text", "")).strip())
        elif t == "answer":
            self._answer(str(msg.get("text", "")))
        elif t == "stop":
            if self.app.busy():
                self.ui.info("📱 stop pressed on the phone")
                self.app.stop()
        elif t == "shot":
            threading.Thread(target=self._shot, daemon=True).start()

    def _hello(self) -> None:
        self._asked = None
        self.send({**self.status(), "t": "hello", "history": list(self.history)})
        self._sync_question()

    def _say(self, text: str) -> None:
        if not text:
            return
        if self.ui.pending:
            self._answer(text)
            return
        if text.startswith("/"):
            self.send({"t": "event", "kind": "warn", "text": "Commands only work at the laptop — just type what you want done.", "at": time.time()})
            return
        self.ui.user(f"📱 {text}")
        if self.app.busy():
            self.app.agent.inbox.append(text)
            self.ui.queued()
            self.event("user", text)
            return
        self.app.run(text)

    def _answer(self, text: str) -> None:
        p = self.ui.pending
        if not p:
            return
        if p.get("update"):
            self.send({"t": "event", "kind": "info", "text": "The update question is answered at the laptop.", "at": time.time()})
            return
        self.ui.info(f"📱 answered from the phone: {text or '(enter)'}")
        self.ui.answer(text)

    def _shot(self) -> None:
        try:
            img = screenshot_jpeg()
            self.send({"t": "shot", "jpeg": base64.b64encode(img).decode()})
        except Exception as e:
            self.send({"t": "event", "kind": "error", "text": f"Couldn't take a screenshot: {e}", "at": time.time()})


def _describe(name: str, a: tuple, k: dict) -> str:
    """Plain text for one UI call."""
    if name == "action":
        verb, target = (list(a) + ["", ""])[:2]
        note = a[2] if len(a) > 2 else k.get("note", "")
        return " ".join(x for x in (str(verb), str(target), f"({note})" if note else "") if x)
    if name == "plan":
        return f"{a[0]}\n{a[1]}" if len(a) > 1 else str(a[0])
    if name in ("edited", "wrote"):
        return f"{'edited' if name == 'edited' else 'wrote'} {a[0]}" if a else ""
    if name == "todos":
        items = a[0] if a else []
        return "\n".join(("☑ " if str(i.get("status", "")) in ("done", "completed") else "☐ ")
                         + str(i.get("task", i.get("content", ""))) for i in items if isinstance(i, dict))
    if name == "confirm":
        title, detail = (list(a) + ["", ""])[:2]
        why = a[2] if len(a) > 2 else k.get("why", "")
        lines = str(detail).splitlines()
        body = "\n".join(lines[:30]) + (f"\n… +{len(lines) - 30} more lines" if len(lines) > 30 else "")
        return "\n".join(x for x in (f"⚠ {title}", f"needs your OK: {why}" if why else "", body) if x)
    if name == "ask":
        q, opts = (list(a) + ["", []])[:2]
        return "\n".join([f"? {q}"] + [f"  {i}. {o}" for i, o in enumerate(opts or [], 1)])
    if name == "tool_result":
        t = str(a[0]) if a else ""
        return t[:1500] + ("…" if len(t) > 1500 else "")
    return str(a[0]) if a else ""


def _version() -> str:
    from . import __version__
    return __version__


def _model(app) -> str:
    for get in (lambda: app.agent.router.current_label(), lambda: app.ui.model_name):
        try:
            v = get()
            if v:
                return str(v)
        except Exception:
            pass
    return ""
