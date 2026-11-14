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
p = subprocess.Popen([sys.executable, "kv.py", "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    if not wait_port(port): fail("server did not listen: " + (p.stderr.read().decode()[-300:] if p.poll() is not None else "timeout"))
    s = socket.create_connection(("127.0.0.1", port), 5)
    def cmd(*parts):
        s.sendall(("*%d\r\n" % len(parts) + "".join("$%d\r\n%s\r\n" % (len(x), x) for x in parts)).encode())
        time.sleep(0.15); return s.recv(4096).decode()
    if "PONG" not in cmd("PING"): fail("PING")
    if not cmd("SET", "a", "hello").startswith("+OK"): fail("SET")
    if "hello" not in cmd("GET", "a"): fail("GET")
    if not cmd("GET", "nope").startswith("$-1"): fail("GET missing must be $-1")
    if not cmd("INCR", "n").startswith(":1"): fail("INCR 1")
    if not cmd("INCR", "n").startswith(":2"): fail("INCR 2")
    if not cmd("INCR", "a").startswith("-"): fail("INCR on text must be an error")
    if not cmd("DEL", "a", "n", "zz").startswith(":2"): fail("DEL count")
    s2 = socket.create_connection(("127.0.0.1", port), 5); s2.sendall(b"*1\r\n$4\r\nPING\r\n"); time.sleep(0.15)
    if b"PONG" not in s2.recv(100): fail("second client")
    ok()
finally:
    p.kill()
