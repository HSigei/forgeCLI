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

port = free_port()
p = subprocess.Popen([sys.executable, "api.py", "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
base = f"http://127.0.0.1:{port}"
def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            raw = resp.read(); return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None
try:
    if not wait_port(port): fail("API did not listen")
    st, n = req("POST", "/notes", {"text": "hi"})
    if st != 201 or not isinstance(n, dict) or not isinstance(n.get("id"), int): fail(f"POST -> {st} {n}")
    if req("POST", "/notes", {"text": ""})[0] != 400: fail("empty text must be 400")
    st, lst = req("GET", "/notes")
    if st != 200 or len(lst) != 1: fail(f"GET list -> {st} {lst}")
    if req("GET", f"/notes/{n['id']}")[1].get("text") != "hi": fail("GET one")
    if req("GET", "/notes/9999")[0] != 404: fail("unknown must be 404")
    if req("DELETE", f"/notes/{n['id']}")[0] != 204: fail("DELETE")
    if req("GET", "/notes")[1] != []: fail("not deleted")
    ok()
finally:
    p.kill()
