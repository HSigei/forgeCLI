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

r = run([sys.executable, "-m", "pytest", "-q", "tests"], timeout=120)
if r.returncode != 0: fail(r.stdout[-400:])
run([sys.executable, "bank.py", "deposit", "50"]); run([sys.executable, "bank.py", "withdraw", "20"])
r = run([sys.executable, "bank.py", "balance", "--json"])
try:
    if json.loads(r.stdout)["balance"] != 30: fail("balance json: " + r.stdout)
    h = json.loads(run([sys.executable, "bank.py", "history", "--json"]).stdout)
    if h != [{"type": "deposit", "amount": 50}, {"type": "withdraw", "amount": 20}]: fail("history json: " + str(h))
except (ValueError, KeyError) as e:
    fail(f"not JSON: {e}: {r.stdout[-200:]}")
if run([sys.executable, "bank.py", "balance"]).stdout.strip() != "Balance: 30.00": fail("plain output changed")
ok()
