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

import csv
if not (HERE / "people_clean.csv").exists():
    r = run([sys.executable, "clean.py"])
    if not (HERE / "people_clean.csv").exists(): fail("no output: " + r.stderr[-300:])
rows = list(csv.DictReader(open(HERE / "people_clean.csv", encoding="utf-8")))
if [r["email"] for r in rows] != ["ada@x.io", "bo@x.io", "cy@x.io"]: fail("rows: " + str(rows))
if rows[0]["name"] != "Ada Lovelace" or rows[0]["age"] != "36" or rows[2]["age"] != "": fail("values: " + str(rows))
ok()
