import json, os, pathlib, socket, subprocess, sys, time, urllib.request
HERE = pathlib.Path(".").resolve()
def fail(msg):
    print("FAIL:", msg); sys.exit(1)
def ok(msg="ok"):
    print(msg); sys.exit(0)
def run(cmd, inp=None, timeout=60):
    return subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", shell=isinstance(cmd, str))
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
def wait_port(port, secs=15):
    end = time.time() + secs
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close(); return True
        except OSError:
            time.sleep(0.3)
    return False

import re
h = (HERE / "index.html")
if not h.exists(): fail("no index.html")
t = h.read_text(encoding="utf-8").lower()
if "styles.css" not in t or not (HERE / "styles.css").exists(): fail("styles.css missing or not linked")
if "crumb" not in t: fail("name missing")
if len(re.findall(r"[$£€]\s?\d|\d+\.\d\d", t)) < 4: fail("fewer than 4 prices")
if "<form" not in t or 'type="email"' not in t or "<textarea" not in t: fail("contact form incomplete")
if 'name="viewport"' not in t: fail("no viewport meta")
css = (HERE / "styles.css").read_text(encoding="utf-8").lower()
if "@media" not in css and "clamp(" not in css and "grid" not in css: fail("nothing responsive in CSS")
ok()
