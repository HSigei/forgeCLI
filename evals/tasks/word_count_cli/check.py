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

(HERE / "a.txt").write_text("The cat. The dog!\nthe end\n", encoding="utf-8")
(HERE / "b.txt").write_text("one two\n", encoding="utf-8")
r = run([sys.executable, "wc.py", "a.txt"])
if r.returncode != 0 or not all(x in r.stdout.split() for x in ["2", "6", "a.txt"]): fail("single file: " + r.stdout + r.stderr)
r = run([sys.executable, "wc.py", "a.txt", "b.txt"])
if "total" not in r.stdout.lower(): fail("no total line")
r = run([sys.executable, "wc.py", "--top", "1", "a.txt"])
if not r.stdout.strip().lower().startswith("the") or "3" not in r.stdout: fail("--top: " + r.stdout)
r = run([sys.executable, "wc.py", "nope.txt"])
if r.returncode != 1 or not r.stderr.strip(): fail("missing file must exit 1 with stderr")
ok()
