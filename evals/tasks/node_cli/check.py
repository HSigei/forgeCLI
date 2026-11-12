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

for c in (["add", "buy milk"], ["add", "walk dog"], ["done", "1"]):
    r = run(["node", "todo.js", *c])
    if r.returncode != 0: fail("node todo.js " + " ".join(c) + ": " + r.stderr[-200:])
r = run(["node", "todo.js", "list"])
lines = [l for l in r.stdout.splitlines() if l.strip()]
if len(lines) != 2 or "[x]" not in lines[0] or "milk" not in lines[0] or "dog" not in lines[1]: fail("list: " + r.stdout)
run(["node", "todo.js", "remove", "1"])
if "milk" in run(["node", "todo.js", "list"]).stdout: fail("remove")
if run(["node", "todo.js", "frobnicate"]).returncode != 1: fail("bad command must exit 1")
ok()
