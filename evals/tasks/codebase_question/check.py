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

got = {l.strip().strip("-*` ").split("(")[0] for l in (HERE / "answer.txt").read_text(encoding="utf-8").splitlines() if l.strip()} if (HERE / "answer.txt").exists() else set()
want = {"notify_owner", "close_ticket", "escalate", "nightly_job"}
if not want <= got: fail(f"missing {sorted(want - got)}")
if got & {"render", "log_event", "reopen_ticket"}: fail(f"wrong: {sorted(got & {'render','log_event','reopen_ticket'})}")
ok()
