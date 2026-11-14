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

probe = (HERE / "_probe.mjs")
probe.write_text('''
import {addTodo, toggleTodo, removeTodo, visibleTodos} from "./todos.js";
let a = []; const b = addTodo(a, "milk"); if (a.length !== 0 || b.length !== 1) throw new Error("add mutates or fails");
let c = addTodo(b, "   "); if (c.length !== 1) throw new Error("blank added");
c = addTodo(b, "eggs"); const id = c[0].id; const d = toggleTodo(c, id);
if (visibleTodos(d, "done").length !== 1 || visibleTodos(d, "active").length !== 1 || visibleTodos(d, "all").length !== 2) throw new Error("filters");
if (c[0].done) throw new Error("toggle mutates");
if (removeTodo(d, id).length !== 1) throw new Error("remove");
console.log("ok");
''', encoding="utf-8")
r = run(["node", str(probe)])
if r.returncode != 0: fail(r.stderr[-400:])
html = (HERE / "index.html").read_text(encoding="utf-8")
if "app.js" not in html or 'type="module"' not in html: fail("index.html must load app.js as a module")
if "localStorage" not in (HERE / "app.js").read_text(encoding="utf-8"): fail("no localStorage")
ok()
