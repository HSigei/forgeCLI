"""Offline end-to-end test against fake OpenAI-compatible servers.

Run:  python tests/test_failover.py
No real API keys or quota are used.
"""
import copy
import http.server
import json
import pathlib
import re
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from forgecli import guards as guardsmod, mcp as mcpmod, parallel, roster, sessions, skills  # noqa: E402
from forgecli.agent import Agent  # noqa: E402
from forgecli.config import ProviderConfig, Settings  # noqa: E402
from forgecli.keys import CooldownStore  # noqa: E402
from forgecli.providers import Router, portable, screen  # noqa: E402
from forgecli.ui import UI  # noqa: E402
from forgecli import quality as qualitymod  # noqa: E402

# Model scores learned during tests must not land in the user's real ~/.forge.
_QDIR = pathlib.Path(tempfile.mkdtemp(prefix="forge-quality-"))
qualitymod.RECORD_FILE = _QDIR / "record.json"
qualitymod.STATS_FILE = _QDIR / "stats.json"
qualitymod.OVERRIDES_FILE = _QDIR / "over.json"
qualitymod._cache.update(at=0.0, record={})

CALLS = []
BODIES = []


def reply(content=None, tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg, "finish_reason": finish}]}


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        key = self.headers["Authorization"].split()[1]
        CALLS.append((self.server.name, key))
        BODIES.append((self.server.name, body))
        msgs = body["messages"]
        last = msgs[-1]
        if key.startswith("pm"):   # limited per model: "busy" is rate-limited, every other model answers
            if body["model"] == "busy":
                return self.send(429, {"error": {"message": "Rate limit reached: requests per minute"}})
            return self.send(200, reply("hi from " + body["model"]))
        if key.startswith("daily"):
            return self.send(429, {"error": {"message": "Rate limit reached: requests per day (RPD)"}})
        if key.startswith("minute"):
            return self.send(429, {"error": {"message": "Rate limit reached: requests per minute"}})
        if key.startswith("revoked"):
            return self.send(401, {"error": "invalid key"})
        if key.startswith("flaky") and getattr(self.server, "fail_coordinator", False):
            return self.send(503, {"error": "coordinator temporarily unavailable"})
        if key.startswith("empty"):
            return self.send(200, reply(""))
        if key.startswith("verdict"):
            return self.send(200, reply("User Safety: safe"))
        if key.startswith("silent"):
            time.sleep(3)          # longer than the test timeout
            return self.send(200, reply("too late"))
        if key.startswith("sse") and body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"choices": [{"delta": {"reasoning_content": "hmm"}}]},
                      {"choices": [{"delta": {"content": "Hello "}}]},
                      {"choices": [{"delta": {"content": "world"}}]},
                      {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {
                          "name": "list_dir", "arguments": "{\"pa"}}]}}]},
                      {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
                          "arguments": "th\": \".\"}"}}]}, "finish_reason": "tool_calls"}]},
                      {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 5}}]
            for c in chunks:
                self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            return
        # good key: behave like a tool-using model
        if last["role"] == "user" and "continue exactly" in last["content"].lower():
            return self.send(200, reply(" second half."))
        if last["role"] == "user" and "long" in last["content"]:
            return self.send(200, reply("First half,", finish="length"))
        if last["role"] == "user":
            path = re.search(r"read (\S+)", last["content"]).group(1)
            return self.send(200, reply(None, [{"id": "c1", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": path})}}]))
        if last["role"] == "tool":
            return self.send(200, reply("The file says: " + last["content"].split("\t", 1)[-1].strip()))
        self.send(500, {"error": "unexpected"})


def serve(name):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    srv.name = name
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def main():
    tmp = pathlib.Path(tempfile.mkdtemp()).resolve()   # long path: CI temp dirs come back as 8.3 short names
    sessions.SESSION_DIR = tmp / "sessions"   # keep the user's real sessions untouched
    mcpmod.USAGE_FILE = tmp / "usage.json"
    import forgecli.providers as provmod
    provmod.LIMITS_FILE = tmp / "model_limits.json"
    note = tmp / "note.txt"
    note.write_text("hello from forge", encoding="utf-8")

    _, url_a = serve("A")
    _, url_b = serve("B")
    s = Settings(retry_rounds=0, auto_approve=True)
    s.providers = [
        ProviderConfig("A", url_a, "m", ["daily1", "minute1", "revoked1", "empty1", "verdict1"]),
        ProviderConfig("B", url_b, "m", ["goodB1", "goodB2"]),
    ]
    s.keys_per_provider = 5
    logs = []
    ui = UI(verbose=False)
    ui.log = logs.append
    store = CooldownStore(tmp / "state.json")
    router = Router(s, logs.append, store)
    agent = Agent(s, ui, router)

    # 1. Every A key fails differently; B answers; tool call is executed.
    agent.run_turn(f"read {note}")
    assert agent.messages[-1]["content"] == "The file says: hello from forge", agent.messages[-1]
    assert any("daily limit" in l for l in logs), logs
    assert any("per-minute" in l for l in logs), logs
    assert any("401" in l for l in logs), logs
    assert any("empty / junk" in l for l in logs), logs
    print("ok  failover across 5 bad keys -> provider B, tool executed")

    # 2. Benched keys are skipped next time (only empty1/verdict1 remain on A).
    CALLS.clear()
    agent.run_turn(f"read {note}")
    tried_a = {k for name, k in CALLS if name == "A"}
    assert tried_a <= {"empty1", "verdict1"}, tried_a
    print("ok  benched keys skipped:", sorted(tried_a), "tried on A")

    # 3. Round-robin on B alternates keys.
    b_keys = [k for name, k in CALLS if name == "B"]
    assert len(set(b_keys)) == 2, b_keys
    print("ok  round-robin across B keys:", b_keys)

    # 4. Cooldowns persisted to disk, without the keys themselves.
    raw = (tmp / "state.json").read_text()
    assert "daily1" not in raw and len(json.loads(raw)["until"]) == 3
    print("ok  3 cooldowns persisted (keys hashed, not stored)")

    # 5. Truncated reply is continued and stitched.
    r = router.complete([{"role": "user", "content": "say something long"}], None)
    assert r["content"] == "First half, second half.", r
    print("ok  cut-off reply continued:", repr(r["content"]))

    # 6. Screening.
    assert screen("User Safety: safe", []) is None
    assert screen("User Safety: safe\nReal answer", [])[0] == "Real answer"
    assert screen("<tool_call>garbage", []) is None
    t, calls = screen('<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>', [])
    assert calls and calls[0]["function"]["name"] == "list_dir"
    print("ok  screening: verdicts, junk, rescued text tool calls")

    # 7. Shell tool.
    out = agent.tools.execute("run_shell", json.dumps({"command": "echo forge-shell-ok"}))
    assert "forge-shell-ok" in out and "exit code 0" in out, out
    print("ok  run_shell works with", s.shell)
    # A server that never exits must not hang the chat: it gets moved to the background.
    srv = tmp / "srv.py"
    srv.write_text("import time\nprint('Running on http://127.0.0.1:5999', flush=True)\ntime.sleep(300)\n")
    t0 = time.time()
    out = agent.tools.execute("run_shell", json.dumps({"command": f'python "{srv}"', "timeout": 60}))
    assert time.time() - t0 < 20, f"server call took {time.time() - t0:.0f}s"
    assert "background as process" in out and "http://127.0.0.1:5999" in out, out
    pid = int(re.search(r"process (\d+)", out).group(1))
    assert "running" in agent.tools.execute("process", json.dumps({"action": "list"}))
    assert "Stopped" in agent.tools.execute("process", json.dumps({"action": "stop", "id": pid}))
    print(f"ok  server command moved to background in {time.time() - t0:.0f}s, then stopped")

    # 8. Sessions: turns were autosaved, can be listed, resumed and deleted.
    items = sessions.list_sessions()
    assert len(items) == 1 and items[0]["count"] == 2, items
    again = sessions.Session.load(sessions.find("1"))
    assert again.messages == agent.messages[1:] and again.title.startswith("read "), again.title
    agent2 = Agent(s, ui, router, session=again)
    assert agent2.messages[1:] == agent.messages[1:]
    agent2.reset()
    assert len(agent2.messages) == 1 and len(sessions.list_sessions()) == 1  # empty sessions not saved
    print("ok  sessions autosaved, listed, resumed:", repr(again.title[:30]))

    # 9. Skills: discovered from a folder, offered as a tool, loaded on request.
    sk = tmp / "skills" / "greet"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: greet\ndescription: Say hi nicely\n---\nSay hello in French.",
                                 encoding="utf-8")
    _real_skill_dirs = skills.skill_dirs
    skills.skill_dirs = lambda: [("user", tmp / "skills")]
    agent.reload_skills()
    assert "greet: Say hi nicely" in agent.messages[0]["content"]
    assert any(t["function"]["name"] == "use_skill" for t in agent._schemas())
    assert agent._execute("use_skill", '{"name": "greet"}').endswith("Say hello in French.")
    assert "Say hello in French." in agent.skill_prompt("greet", "to Bob")
    print("ok  skills discovered, in system prompt, loadable via use_skill")

    # 10. Model switching and provider pinning.
    router.pinned = "B"
    assert [p.name for p in router._ordered()] == ["B"] and router.lead().name == "B"
    router.get("B").model = "other-model"
    r = router.complete([{"role": "user", "content": "say something long"}], None)
    assert r["provider"] == "B" and r["model"] == "other-model", r
    router.pinned = None
    print("ok  pinned provider + switched model used for the request")

    # 11. Shared history is portable: any provider can continue another's conversation.
    ids = [tc["id"] for m in agent.messages if m.get("tool_calls") for tc in m["tool_calls"]]
    assert ids and all(re.fullmatch(r"[0-9a-f]{9}", i) for i in ids), ids
    old = [{"role": "assistant", "content": None, "reasoning": "x",
            "tool_calls": [{"id": "call_from_another_provider_123", "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"}}]},
           {"role": "tool", "tool_call_id": "call_from_another_provider_123", "content": "ok"}]
    p = portable(old)
    assert "reasoning" not in p[0] and p[0]["content"] == ""
    assert p[0]["tool_calls"][0]["id"] == p[1]["tool_call_id"] and len(p[1]["tool_call_id"]) == 9
    print("ok  history normalised so any provider accepts another's tool calls")

    # 12. Parallel sub-agents: shared context, spread over providers, reports returned.
    _, url_c = serve("C")
    _, url_d = serve("D")
    s2 = Settings(retry_rounds=0, auto_approve=True)
    s2.providers = [ProviderConfig("C", url_c, "gpt-oss-120b", ["goodC"]),
                    ProviderConfig("D", url_d, "gpt-oss-120b", ["goodD"])]
    s2.coordinator = "C:gpt-oss-120b"
    parent = Agent(s2, ui, Router(s2, logs.append, CooldownStore(tmp / "state2.json")))
    parent.messages.append({"role": "user", "content": "earlier question"})
    parent.messages.append({"role": "assistant", "content": "earlier answer"})
    parent._turn_goal = "Inspect the note and summarize its contents."
    parent.tools.ledger.add("read_file", {"path": "notes.txt"}, "Read the note.")
    parent.tools.ledger.add("edit_file", {"path": "changed.py"}, "Edited changed.py.")
    parent.tools.ledger.add("run_shell", {"command": "python tests/test_failover.py"},
                            "Error: test command failed.")
    before = len(parent.messages)
    BODIES.clear()
    class CaptureUI(UI):
        def __init__(self):
            super().__init__()
            self.info_lines = []

        def info(self, msg):
            self.info_lines.append(msg)

        def confirm(self, title, detail, why=""):
            raise AssertionError("a hand-off must not request confirmation")

    capture = CaptureUI()
    parent.ui = capture
    report = parallel.run_tasks(parent, [f"read {note}", f"read {note}"], reason="independent reads")
    parent.ui = ui
    assert report.count("The file says: hello from forge") == 2, report
    assert {name for name, _ in BODIES} == {"C", "D"}, [n for n, _ in BODIES]
    assert any(step.name == "read_file" for step in parent.tools.ledger.steps)
    firsts = [b["messages"] for _, b in BODIES if b["messages"][-1]["role"] == "user"]
    assert all("Original user goal: Inspect the note and summarize its contents." in ms[-1]["content"]
               for ms in firsts)
    assert all("Assigned task: read " in ms[-1]["content"] for ms in firsts)
    assert all("Files already read:" in ms[-1]["content"] and "notes.txt" in ms[-1]["content"]
               for ms in firsts)
    assert all("Files already changed:" in ms[-1]["content"] and "changed.py" in ms[-1]["content"]
               for ms in firsts)
    assert all("What was tried:" in ms[-1]["content"] and "test command failed" in ms[-1]["content"]
               for ms in firsts)
    assert all("Why this task was handed off: independent reads" in ms[-1]["content"] for ms in firsts)
    assert len(capture.info_lines) == 1 and "independent reads" in capture.info_lines[0]
    assert len(parent.messages) == before
    assert parallel.split_tasks("a || b ||  || c") == ["a", "b", "c"]
    parent.parallel = True
    # Hand-off tools are only offered when there's a reason (here: a request listing independent parts).
    parent._turn_goal = "- read a\n- read b"
    assert any(t["function"]["name"] == "run_parallel" for t in parent._schemas())
    parent._turn_goal = ""
    print("ok  parallel agents: both saw earlier context, ran on C and D, reports returned")

    # 13. Team mode: assigned workers go first (even against the offset), coordinator + delegate tool.
    assert parallel.parse_tasks(["@D:m read x", {"task": "y", "worker": "C:m", "why": "fast"}]) == [
        {"task": "read x", "worker": "D:m", "why": ""}, {"task": "y", "worker": "C:m", "why": "fast"}]
    BODIES.clear()
    report = parallel.run_tasks(parent, [{"task": f"read {note}", "worker": "D:gpt-oss-120b"},
                                         {"task": f"read {note}", "worker": "D:gpt-oss-120b"}])
    assert report.count("The file says: hello from forge") == 2, report
    assert {name for name, _ in BODIES} == {"D"}, [n for n, _ in BODIES]
    lead = parent.set_team(True)
    assert lead == "C:gpt-oss-120b" and parent.prefer == tuple(lead.split(":"))
    assert "TEAM MODE" in parent.messages[0]["content"] and lead in parent.messages[0]["content"]
    parent.router.get("C").pool.bench("goodC", 60, "gpt-oss-120b")
    assert parent._refresh_coordinator() == "D:gpt-oss-120b"
    assert "coordinator (D:gpt-oss-120b)" in parent.messages[0]["content"]
    assert "C:gpt-oss-120b" not in roster.describe(roster.build(parent.router), parent.router)
    lead = parent.coordinator.id
    parent._turn_goal = "Inspect the project and summarize its layout."
    parent.tools.new_turn()
    assert not any(t["function"]["name"] == "delegate" for t in parent._schemas())
    parent._turn_goal = "- Inspect the project layout\n- Summarize its main modules"
    assert any(t["function"]["name"] == "delegate" for t in parent._schemas())
    parent._turn_goal = "Inspect the project and summarize its layout."
    parent.tools.ledger.add("read_file", {"path": "README.md"}, "Read 20 lines.")
    assert not any(t["function"]["name"] == "delegate" for t in parent._schemas())
    failed = "Error: file does not exist."
    parent.tools.ledger.add("read_file", {"path": "missing.txt"}, failed)
    parent.tools.ledger.add("read_file", {"path": "missing.txt"}, failed)
    assert any(t["function"]["name"] == "delegate" for t in parent._schemas())
    refused = parent._execute("delegate", json.dumps({"tasks": [
        {"task": parent._turn_goal, "worker": lead}]}))
    assert "do it yourself first" in refused.lower()
    parent._handoff_count = 3
    assert not any(t["function"]["name"] == "delegate" for t in parent._schemas())
    capped = parent._execute("delegate", json.dumps({"tasks": [
        {"task": "Review the README for inconsistencies.", "worker": lead}]}))
    assert "limit of 3" in capped.lower()
    BODIES.clear()
    parallel.run_tasks(parent, [{"task": f"read {note}", "worker": "C:gpt-oss-120b"}],
                       reason="selected worker unavailable")
    assert {name for name, _ in BODIES} == {"D"}
    parent.set_team(False)
    assert parent.prefer is None and "TEAM MODE" not in parent.messages[0]["content"]
    print("ok  team mode: assigned workers, evidence-gated hand-offs, task packet, whole-task refusal, cap")

    # 13b. Coordinator failover is automatic, updates the prompt, and returns to a recovered preference.
    server_c, flaky_url = serve("C")
    server_d, good_url = serve("D")
    server_c.fail_coordinator = True
    s3 = Settings(retry_rounds=0, auto_approve=True, team=False)
    s3.providers = [ProviderConfig("C", flaky_url, "gpt-oss-120b", ["flakyC"]),
                    ProviderConfig("D", good_url, "gpt-oss-120b", ["goodD"])]
    s3.coordinator = "C:gpt-oss-120b"
    router3 = Router(s3, logs.append, CooldownStore(tmp / "state3.json"))
    coordinator_ui = CaptureUI()
    agent3 = Agent(s3, coordinator_ui, router3)
    assert agent3.set_team(True) == "C:gpt-oss-120b"
    CALLS.clear()
    BODIES.clear()
    coordinator_ui.info_lines.clear()
    agent3.run_turn(f"read {note}")
    assert agent3.coordinator.id == "D:gpt-oss-120b"
    assert any("coordinator (D:gpt-oss-120b)" in body["messages"][0]["content"]
               for name, body in BODIES if name == "D" and body["messages"][-1]["role"] == "tool")
    assert any("Coordinator hand-off packet" in message.get("content", "") and
               "Files already read:" in message.get("content", "")
               for name, body in BODIES if name == "D"
               for message in body["messages"] if message.get("role") == "user")
    assert coordinator_ui.info_lines.count(
        "coordinator C:gpt-oss-120b is unavailable; D:gpt-oss-120b leads this turn") == 1
    assert CALLS[0][0] == "C" and any(name == "D" for name, _ in CALLS)

    router3.model_down[("C", "gpt-oss-120b")] = time.time() - 1
    server_c.fail_coordinator = True
    router3.chain_once([{"role": "user", "content": f"read {note}"}], None,
                       prefer=("C", "gpt-oss-120b"))
    assert router3.model_down[("C", "gpt-oss-120b")] > time.time() + 250
    server_c.fail_coordinator = False
    router3.model_down[("C", "gpt-oss-120b")] = time.time() - 1
    CALLS.clear()
    BODIES.clear()
    coordinator_ui.info_lines.clear()
    agent3.run_turn(f"read {note}")
    assert agent3.coordinator.id == "C:gpt-oss-120b"
    assert CALLS[0][0] == "C"
    assert coordinator_ui.info_lines.count(
        "coordinator C:gpt-oss-120b is available again; it leads this turn") == 1

    s4 = Settings(retry_rounds=0)
    s4.providers = [ProviderConfig("C", flaky_url, "m", ["goodC"])]
    solo = Agent(s4, UI(), Router(s4, logs.append, CooldownStore(tmp / "state4.json")))
    assert solo.set_team(True) == "" and solo.team and solo.coordinator is None
    solo._turn_goal = "- part one\n- part two"
    solo.parallel = True
    assert not any(t["function"]["name"] in ("delegate", "run_parallel") for t in solo._schemas())
    assert "TEAM MODE" not in solo.messages[0]["content"]
    print("ok  coordinator: automatic failover/re-election, prompt refresh, recovery, solo fallback, down refresh")

    # 14. Smart approval: safe things run, risky things ask.
    from forgecli import safety
    root = pathlib.Path.cwd()
    assert not safety.needs_ok("smart", "run_shell", root, command="python app.py")
    assert not safety.needs_ok("smart", "run_shell", root, command="npm test")
    assert safety.needs_ok("smart", "run_shell", root, command="Remove-Item build -Recurse")
    assert safety.needs_ok("smart", "run_shell", root, command="git push --force")
    assert not safety.needs_ok("smart", "write_file", root, root / "x.py")
    assert safety.needs_ok("smart", "write_file", root, pathlib.Path(tempfile.gettempdir()).parent / "x.py") or \
        safety.inside(pathlib.Path(tempfile.gettempdir()), root)
    assert safety.needs_ok("ask", "write_file", root, root / "x.py")
    assert not safety.needs_ok("auto", "run_shell", root, command="rm -rf x")
    print("ok  smart approval: safe commands/edits run, deletes/pushes/outside-folder writes ask")

    # 15. A stuck request can be abandoned: cancel returns right away.
    slow = Agent(s, UI(), router=router)
    slow.router = type("Slow", (), {"complete": lambda *a, **k: time.sleep(30)})()
    slow.cancel = threading.Event()
    threading.Timer(0.5, slow.cancel.set).start()
    t0 = time.time()
    assert slow._ask() is None and time.time() - t0 < 3
    print(f"ok  stuck request abandoned after {time.time() - t0:.1f}s")

    # 16. Streaming: text arrives in pieces; tool-call fragments are stitched back together.
    s2 = Settings(retry_rounds=0)
    s2.providers = [ProviderConfig("S", url_a, "m", ["sse1"])]
    r2 = Router(s2, lambda *a: None, CooldownStore(tmp / "s2.json"))
    pieces = []
    out = r2.complete([{"role": "user", "content": "hi"}], None, on_text=pieces.append)
    from forgecli.providers import Action as _Act, Thought as _Th
    assert "".join(p for p in pieces if type(p) is str) == "Hello world", pieces
    assert [p for p in pieces if isinstance(p, _Th)] == ["hmm"], pieces          # reasoning: display only
    assert [p for p in pieces if isinstance(p, _Act)] == ["list_dir"], pieces    # tool hint: display only
    assert out["content"] == "Hello world", out
    tc = out["tool_calls"][0]
    assert tc["function"]["name"] == "list_dir" and json.loads(tc["function"]["arguments"]) == {"path": "."}, tc
    print("ok  streaming: text shown as it arrives, tool call reassembled:", pieces)

    # 17. A model that goes silent times out, and is skipped for the next 10 minutes.
    s3 = Settings(retry_rounds=0, request_timeout=1)
    s3.providers = [ProviderConfig("Q", url_a, "slow", ["silent1"]), ProviderConfig("R", url_b, "m", ["goodB1"])]
    r3 = Router(s3, lambda *a: None, CooldownStore(tmp / "s3.json"))
    CALLS.clear()
    t0 = time.time()
    out = r3.complete([{"role": "user", "content": "say hi long"}], None)
    assert out and out["provider"] == "R" and time.time() - t0 < 2.5, (out, time.time() - t0)
    assert r3.is_down("Q", "slow")
    CALLS.clear()
    r3.complete([{"role": "user", "content": "say hi long"}], None)
    assert not any(k == "silent1" for _, k in CALLS), CALLS
    print("ok  silent model timed out, then skipped instead of waited on again")

    # 18. Fastest healthy provider goes first.
    s4 = Settings(retry_rounds=0)
    s4.providers = [ProviderConfig("Slow", url_b, "m", ["goodB1"]), ProviderConfig("Fast", url_b, "m", ["goodB2"])]
    r4 = Router(s4, lambda *a: None, CooldownStore(tmp / "s4.json"))
    r4._speeds = {"Slow:m": 7.0, "Fast:m": 0.8}
    assert [p.name for p in r4._ordered()] == ["Fast", "Slow"]
    r4.by_speed = False
    assert [p.name for p in r4._ordered()] == ["Slow", "Fast"]
    print("ok  fastest provider tried first (and FORGE_FASTEST_FIRST=0 keeps your order)")

    # 19. Several reads in one step run at the same time.
    par = Agent(s, UI(), router=router)
    seen_threads = set()
    real = par._execute

    def slow_exec(name, args):
        seen_threads.add(threading.get_ident())
        time.sleep(0.4)
        return real(name, args)
    par._execute = slow_exec
    calls = [{"id": f"r{i}", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": str(note)})}} for i in range(4)]
    replies = iter([{"content": "", "tool_calls": calls, "provider": "B", "model": "m", "key": "k",
                     "truncated": False, "usage": {}},
                    {"content": "done", "tool_calls": [], "provider": "B", "model": "m", "key": "k",
                     "truncated": False, "usage": {}}])
    par._ask = lambda messages=None: next(replies)
    par.messages.append({"role": "user", "content": "read four"})
    t0 = time.time()
    par._loop()
    took = time.time() - t0
    assert took < 1.2 and len(seen_threads) > 1, (took, seen_threads)
    assert [m["tool_call_id"] for m in par.messages if m["role"] == "tool"] == ["r0", "r1", "r2", "r3"]
    print(f"ok  4 reads ran in parallel ({took:.1f}s instead of 1.6s), results kept in order")

    # 20. A model that said "too large" is skipped for requests that big, without being asked again.
    lr = Router(s, lambda *_: None, CooldownStore(tmp / "state20.json"))
    msgs = [{"role": "user", "content": f"read {note} " + "x" * 5000}]
    size = provmod.request_size(msgs, [])
    lr.limits.too_big("A", "m", size - 1000)
    tried = []
    r = lr.complete(msgs, [], ev=lambda k, p, m, d: tried.append((k, p)))
    assert r and r["provider"] == "B" and ("try", "A") not in tried, tried
    assert not lr.limits.fits("A", "m", size) and lr.limits.fits("A", "m", 1000)
    print("ok  model that refused a big conversation is skipped for big ones, still used for small")

    # 21. When every model is too small, the router says so at once instead of retrying for a minute.
    big = [{"role": "user", "content": f"read {note} " + "x" * 40000}]
    bsize = provmod.request_size(big, [])
    lr.limits.too_big("B", "m", size - 1000)      # B already handled `size`: cap can't go below it
    assert lr.limits.fits("B", "m", size)
    lr.limits.too_big("B", "m", bsize - 1000)
    t0 = time.time()
    assert lr.complete(big, []) is None and lr.too_big and size < lr.size_cap < bsize
    assert time.time() - t0 < 0.5
    assert lr.complete(msgs, [])                   # a smaller conversation still goes through
    print("ok  all models too small -> reported at once (caller trims the conversation)")

    # 22. Linux pipes are rewritten for PowerShell.
    from forgecli.tools import unixy_to_powershell
    assert unixy_to_powershell("ls -R x | head -n 50") == "Get-ChildItem -Recurse -Name x | Select-Object -First 50"
    assert unixy_to_powershell("git status") == "git status"
    print("ok  head/tail/grep/wc in a PowerShell command are translated")

    # 23. Claude Code's skills are copied into Forge and keep working without Claude Code.
    from forgecli import skills as sk
    import shutil as _sh
    fake = tmp / "claude"
    (fake / "skills" / "demo").mkdir(parents=True)
    (fake / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: a demo\n---\nDo it.",
                                                       encoding="utf-8")
    (fake / "skills" / "demo" / "ref.md").write_text("reference", encoding="utf-8")
    sk.CLAUDE_DIR, sk.LIBRARY = fake, tmp / "library"
    assert "demo" in sk.discover() and (tmp / "library" / "skills" / "demo" / "ref.md").is_file()
    _sh.rmtree(fake)
    found = sk.discover()
    assert "demo" in found and "library" in str(found["demo"].path), found.get("demo")
    print("ok  Claude skills copied into Forge's library, still there after Claude Code is gone")

    # 23b. Engineering skills ship inside Forge itself: there with no Claude Code and no library copy.
    _sh.rmtree(tmp / "library", ignore_errors=True)
    sk.skill_dirs = lambda: [d for d in _real_skill_dirs() if d[0] != "user"]   # the real ones, minus ~/.forge
    own = sk.discover()
    for name in ("awwwards-tier-web-application", "test-driven-development", "debugging-and-error-recovery", "code-review-and-quality",
                 "planning-and-task-breakdown", "performance-optimization", "security-and-hardening",
                 "system-design", "improve-code-quality"):
        assert own[name].source == "bundled", (name, own.get(name))
    assert not any(s.source == "bundled" for s in sk.listed(own))   # found on demand, not in the prompt
    assert "run_shell" in own["test-driven-development"].full()
    refs = re.findall(r"[A-Za-z]:[^\s`)\]]*_references[^\s`)\]]*\.md", own["security-and-hardening"].body())
    assert refs and all(pathlib.Path(p).is_file() for p in refs), refs
    assert "test-driven-development" in [s.name for s in sk.find(own, "test driven red green", 3)]
    print("ok  engineering skills are built into Forge, work without Claude Code, links resolve")

    # 24. A page built in a turn is opened in the browser without being asked — once.
    import webbrowser
    opened = []
    real_open, webbrowser.open = webbrowser.open, lambda u, *a, **k: opened.append(u) or True
    try:
        t = agent.tools
        t.new_turn()
        page = tmp / "site" / "index.html"
        t.t_write_file(str(page), "<h1>hi</h1>")
        assert t.show_result() == page.resolve().as_uri() and opened == [page.resolve().as_uri()]
        t.new_turn()
        t.t_write_file(str(page), "<h1>hi again</h1>")
        assert t.show_result() == "refresh" and len(opened) == 1       # no second tab per edit
        t.new_turn()
        t.urls.append("http://127.0.0.1:5000")
        t.t_write_file(str(tmp / "site" / "about.html"), "<p>x</p>")
        assert t.show_result() == "http://127.0.0.1:5000"               # a server wins over a file
        t.new_turn()
        t.t_write_file(str(tmp / "notes.txt"), "plain")
        assert t.show_result() is None                                   # nothing viewable, nothing opened
    finally:
        webbrowser.open = real_open
    print("ok  built page / started server opened automatically, once, without asking")

    # 25. Resume picker + goodbye screen on exit.
    from forgecli import cli as climod, sessions as sessmod, ui as uimod
    sessmod.SESSION_DIR = tmp / "sessions"
    sessmod.SESSION_DIR.mkdir(exist_ok=True)
    old = sessmod.Session(messages=[{"role": "user", "content": "build a landing page"}])
    old.save()
    app = climod.App(agent, agent.ui)
    real_input = uimod.console.input
    uimod.console.input = lambda *a, **k: ""                    # enter = most recent
    try:
        assert app.pick_session() == old.id
        uimod.console.input = lambda *a, **k: "q"
        assert app.pick_session() is None
    finally:
        uimod.console.input = real_input
    __import__("os").environ["FORGE_BYE_ANIMATION"] = "0"
    app.agent.session.messages.append({"role": "user", "content": "hi"})
    with uimod.console.capture() as cap:
        assert app.handle("/exit") is False and app.goodbye() == 0
    assert cap.get().strip() == f"forge -r {app.agent.session.id}"     # only the exact resume command
    print("ok  resume picker chooses a saved conversation; exit leaves only the resume command")

    # 26. The skill a request needs is found locally and loaded without a model call.
    import time as _t
    sk_all = {
        "web-build": sk.Skill("web-build", "Build or change a website, landing page, portfolio or web app (HTML/CSS/JS)",
                              tmp / "wb.md", "builtin"),
        "pptx": sk.Skill("pptx", "Create or edit PowerPoint presentations, slide decks and .pptx files",
                         tmp / "pp.md", "claude"),
        "commit": sk.Skill("commit", "Commit changes with a clear git commit message", tmp / "cm.md", "builtin"),
    }
    for s_ in sk_all.values():
        s_.path.write_text(f"---\nname: {s_.name}\ndescription: x\n---\nSteps for {s_.name}.", encoding="utf-8")
    agent.skills, agent._matcher = sk_all, None
    agent.reset()
    start = _t.perf_counter()
    agent.messages.append({"role": "user", "content": "build me a landing page for my bakery"})
    agent._auto_skill("build me a landing page for my bakery")
    took = _t.perf_counter() - start
    call = agent.messages[-2]["tool_calls"][0]["function"]
    assert call["name"] == "use_skill" and "web-build" in call["arguments"], agent.messages[-2:]
    assert "Steps for web-build" in agent.messages[-1]["content"] and took < 0.5, took   # no model round-trip: a real call takes seconds
    n = len(agent.messages)
    agent._auto_skill("now make the landing page hero bigger")     # already loaded: not added twice
    assert len(agent.messages) == n
    agent._auto_skill("hi there, how are you")                     # nothing fits: nothing added
    assert len(agent.messages) == n
    print(f"ok  skill picked and loaded automatically in {took * 1000:.1f} ms, no model call")

    # 27. A 429 rests only that model on that key; the same key still serves the provider's other model.
    s5 = Settings(retry_rounds=0)
    s5.max_wait = 0
    s5.providers = [ProviderConfig("P", url_a, "busy", ["pm1"], ["other"])]
    r5 = Router(s5, lambda *a: None, CooldownStore(tmp / "s5.json"))
    out = r5.complete([{"role": "user", "content": "hi"}], None)
    assert out and out["model"] == "other", out
    pool5 = r5.providers[0].pool
    assert pool5.usable("pm1", "other") and not pool5.usable("pm1", "busy")
    print("ok  rate limit rests one model, the same key keeps serving another")

    # 28. Everything resting for a moment -> Forge waits for it instead of failing.
    s6 = Settings(retry_rounds=0)
    s6.max_wait = 10
    s6.providers = [ProviderConfig("W", url_a, "m", ["pmW"])]
    r6 = Router(s6, lambda *a: None, CooldownStore(tmp / "s6.json"))
    r6.providers[0].pool.bench("pmW", 1.5, "m")
    waits = []
    t0 = _t.time()
    out = r6.complete([{"role": "user", "content": "hi"}], None, ev=lambda k, *a: waits.append(k))
    assert out and out["content"] == "hi from m" and "wait" in waits and _t.time() - t0 >= 1.4, (out, waits)
    print(f"ok  all models resting -> waited {_t.time() - t0:.1f}s and then answered")

    # 29. Keyless gateways get no Authorization header.
    from forgecli.config import ANON_KEY
    assert "Authorization" not in provmod.auth_headers(ANON_KEY)
    assert provmod.auth_headers("k1")["Authorization"] == "Bearer k1"
    print("ok  keyless providers are called without an Authorization header")

    # 30. Context compaction preserves the original goal and summarizes omitted work.
    context_agent = Agent(s, UI(), router=router)
    context_agent.messages = [
        context_agent.messages[0],
        {"role": "user", "content": "Build the page using the hallmark skill."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "read-old", "type": "function",
         "function": {"name": "read_file", "arguments": json.dumps({"path": "src\\old.py"})}}]},
        {"role": "tool", "tool_call_id": "read-old", "content": "old file output " * 120},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "shell-old", "type": "function",
         "function": {"name": "run_shell", "arguments": json.dumps({"command": "python -m pytest"})}}]},
        {"role": "tool", "tool_call_id": "shell-old", "content": "2 tests passed"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "read-new", "type": "function",
         "function": {"name": "read_file", "arguments": json.dumps({"path": "src\\page.py"})}}]},
        {"role": "tool", "tool_call_id": "read-new", "content": "recent file output " * 120},
        {"role": "user", "content": "Make the page accessible."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "shell-new", "type": "function",
         "function": {"name": "run_shell", "arguments": json.dumps({"command": "python -m compileall ."})}}]},
        {"role": "tool", "tool_call_id": "shell-new", "content": "compile succeeded"},
    ]
    context_agent.tools.seen[("src\\old.py", 1, 500)] = (1.0, 0)
    before_messages = copy.deepcopy(context_agent.messages)
    before_seen = context_agent.tools.seen.copy()
    compacted = context_agent._compact(1000)
    assert compacted[1]["content"] == "Build the page using the hallmark skill.", compacted
    summary = "\n".join(m.get("content", "") for m in compacted if m.get("role") == "system")
    assert "src\\old.py" in summary and "python -m pytest" in summary, summary
    assert context_agent.messages == before_messages and before_seen
    # Trimmed reads may be read again (the old "use that copy" reply deadlocked the model).
    assert context_agent.tools.recent_only
    # ... while a file read in the last few calls still gets "use that copy" (no re-read loops).
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "a.txt"
        f.write_text("hello", encoding="utf-8")
        from forgecli.tools import Tools as ReadTools
        rt = ReadTools(UI(), "powershell")
        rt.recent_only = True
        args = json.dumps({"path": str(f)})
        assert "hello" in rt.execute("read_file", args)
        assert "Unchanged" in rt.execute("read_file", args)
        rt.last_error = "TypeError: boom"
        again = rt.execute("read_file", args)
        assert again.startswith("Error: not read") and "TypeError: boom" in again, again
        for _ in range(7):
            rt.execute("list_dir", json.dumps({"path": d}))
        assert "hello" in rt.execute("read_file", args)
    assert sum(m.get("role") == "system" for m in compacted) == 1 and compacted[0]["role"] == "system"
    print("ok  request-local compaction keeps the original goal and summarizes files/commands")

    # 31. Skill instructions are kept whole even when they are older than the retained tail.
    skill_agent = Agent(s, UI(), router=router)
    skill_text = "Complete skill instructions\n" + ("Never skip this rule. " * 400)
    skill_agent.messages = [
        skill_agent.messages[0],
        {"role": "user", "content": "Build a page."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "skill-call", "type": "function",
         "function": {"name": "use_skill", "arguments": "{\"name\":\"web-build\"}"}}]},
        {"role": "tool", "tool_call_id": "skill-call", "content": skill_text},
        {"role": "user", "content": "Continue."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "recent-read", "type": "function",
         "function": {"name": "read_file", "arguments": "{\"path\":\"index.html\"}"}}]},
        {"role": "tool", "tool_call_id": "recent-read", "content": "latest output"},
        {"role": "assistant", "content": "I will continue."},
        {"role": "user", "content": "Finish the page."},
    ]
    compacted_skill = skill_agent._compact(1000)
    preserved_skill = next(m["content"] for m in compacted_skill
                           if m.get("role") == "tool" and m.get("tool_call_id") == "skill-call")
    assert preserved_skill == skill_text
    print("ok  compaction keeps full use_skill instructions")

    # 32. A later request can use the full history after a compact request was made.
    sent = []
    context_agent.router.complete = lambda messages, *args, **kwargs: sent.append(copy.deepcopy(messages)) or {}
    context_agent._ask(compacted)
    context_agent._ask()
    assert sent[0] == compacted and sent[1] == before_messages, [len(sent[0]), len(sent[1])]
    print("ok  full history is sent again after a compacted request")

    # 33. HTTP API calls resolve named secrets without returning or logging their values.
    api_calls = []

    class APIFake(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            api_calls.append((self.path, self.headers.get("Authorization"), body))
            payload = json.dumps({"echo": body.decode("utf-8"), "next_cursor": "page-2"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            api_calls.append((self.path, None, b""))
            payload = b"downloaded"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    api_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), APIFake)
    threading.Thread(target=api_server.serve_forever, daemon=True).start()
    api_url = f"http://127.0.0.1:{api_server.server_address[1]}"
    import os as _os
    old_token = _os.environ.get("FORGE_TEST_TOKEN")
    _os.environ["FORGE_TEST_TOKEN"] = "test-secret-value"
    api_tools = Agent(s, UI(), router=router).tools
    result = api_tools.t_http_request(
        method="POST", url=api_url + "/items", query={"tag": ["one", "two"]},
        headers={"Authorization": "Bearer {{env:FORGE_TEST_TOKEN}}"},
        json_body={"token": "{{env:FORGE_TEST_TOKEN}}"})
    assert api_calls and api_calls[0][0] == "/items?tag=one&tag=two", api_calls
    assert api_calls[0][1] == "Bearer test-secret-value" and b"test-secret-value" in api_calls[0][2]
    assert "test-secret-value" not in result and "[secret redacted]" in result and "next_cursor=page-2" in result
    download = tmp / "api-download.bin"
    api_tools.mode = "auto"
    saved = api_tools.t_http_request(method="GET", url=api_url + "/download", download_path=str(download))
    assert download.read_bytes() == b"downloaded" and "saved 10 bytes" in saved
    upload_file = tmp / "api-upload.bin"
    upload_file.write_bytes(b"upload-body")
    api_tools.t_http_request(method="POST", url=api_url + "/upload", form_body={"caption": "hello"},
                             files=[{"field": "file", "path": str(upload_file)}])
    assert b"upload-body" in api_calls[-1][2] and b"filename=\"api-upload.bin\"" in api_calls[-1][2]
    spec = tmp / "api.yaml"
    spec.write_text(f"""openapi: 3.0.3
info:
  title: Test API
  version: 1.0.0
servers:
  - url: {api_url}
paths:
  /items/{{item_id}}:
    get:
      operationId: getItem
      summary: Get an item by ID
      parameters:
        - name: item_id
          in: path
          required: true
          schema:
            type: string
        - name: limit
          in: query
          schema:
            type: integer
      responses:
        '200':
          description: Item
  /items:
    post:
      operationId: createItem
      summary: Create an item
      requestBody:
        $ref: '#/components/requestBodies/NewItemBody'
      responses:
        '201':
          description: Created
components:
  schemas:
    NewItem:
      type: object
      properties:
        name:
          type: string
      required: [name]
  requestBodies:
    NewItemBody:
      required: true
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/NewItem'
""", encoding="utf-8")
    imported = api_tools.t_openapi_import(str(spec), "testapi")
    assert "Imported OpenAPI API 'testapi'" in imported, imported
    operations = api_tools.openapi.search("getItem retrieve item by id")
    assert len(operations) == 1, operations
    op = operations[0]
    op_props = op["schema"]["function"]["parameters"]["properties"]
    assert op_props["path_params"]["required"] == ["item_id"] and "query" in op_props
    assert "url" not in op_props and "method" not in op_props
    before_invalid = len(api_calls)
    invalid_op = api_tools.execute(op["name"], json.dumps(
        {"path_params": {"item_id": "a/b"}, "query": {"limit": 5, "invented": 1}}))
    assert "not declared in the imported schema" in invalid_op and len(api_calls) == before_invalid, \
        (invalid_op, api_calls)
    op_result = api_tools.execute(op["name"], json.dumps(
        {"path_params": {"item_id": "a/b"}, "query": {"limit": 5}}))
    assert api_calls[-1][0] == "/items/a%2Fb?limit=5" and "HTTP 200" in op_result, (api_calls, op_result)
    create_ops = api_tools.openapi.search("createItem create item")
    create_op = next(item for item in create_ops if item["method"] == "POST")
    create_body = create_op["schema"]["function"]["parameters"]["properties"]["json_body"]
    assert create_body["properties"]["name"]["type"] == "string" and create_body["required"] == ["name"]
    create_result = api_tools.execute(create_op["name"], json.dumps({"json_body": {"name": "new"}}))
    assert "HTTP 200" in create_result and api_calls[-1][0] == "/items", (create_result, api_calls)
    bad_spec = tmp / "bad-api.json"
    bad_spec.write_text(json.dumps({"openapi": "3.0.0", "paths": {
        "/broken": {"get": {"operationId": "broken", "parameters": [
            {"name": "id", "in": "query", "schema": {"$ref": "#/components/schemas/Missing"}},
        ]}},
    }}), encoding="utf-8")
    before_operations = len(api_tools.openapi.operations)
    assert "unresolved schema reference" in api_tools.t_openapi_import(str(bad_spec), "badapi")
    assert len(api_tools.openapi.operations) == before_operations
    api_server.shutdown()
    api_server.server_close()
    if old_token is None:
        del _os.environ["FORGE_TEST_TOKEN"]
    else:
        _os.environ["FORGE_TEST_TOKEN"] = old_token
    print("ok  HTTP API handles query/secrets/pagination and saves downloads")

    # 34. Remote write requests still go through the existing approval policy.
    approval_tool = Agent(s, UI(), router=router).tools
    approval_tool.mode = "smart"
    prompts = []
    approval_tool.ui.confirm = lambda *args: prompts.append(args) or ("n", "")
    denied = approval_tool.t_http_request(method="POST", url="https://api.example.test/items")
    assert "denied" in denied.lower() and prompts and "POST" in prompts[0][0]
    print("ok  remote HTTP writes require approval")

    # 35. MCP tools load lazily from a project config and stdio server.
    mcp_root = tmp / "mcp-project"
    mcp_root.mkdir()
    mcp_script = tmp / "mcp_fake.py"
    mcp_script.write_text(
        "import json, os, sys\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    method = message['method']\n"
        "    if method == 'initialize':\n"
        "        result = {'protocolVersion': '2025-03-26', 'capabilities': {}, "
        "'serverInfo': {'name': 'fake', 'version': '1'}}\n"
        "    elif method == 'tools/list':\n"
        "        result = {'tools': [{'name': 'lookup_item', 'description': 'Look up item details by id', "
        "'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']}, "
        "'annotations': {'readOnlyHint': True}}]}\n"
        "    elif method == 'tools/call':\n"
        "        result = {'content': [{'type': 'text', 'text': "
        "'item ' + message['params']['arguments']['id'] + ' token=' + os.environ['MCP_SECRET']}]}\n"
        "    else:\n"
        "        continue\n"
        "    if 'id' in message:\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)\n",
        encoding="utf-8")
    mcp_http_calls = []

    class MCPHTTPFake(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            mcp_http_calls.append((message["method"], self.headers.get("Mcp-Session-Id"),
                                   self.headers.get("Mcp-Protocol-Version")))
            method = message["method"]
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {},
                          "serverInfo": {"name": "http fake", "version": "1"}}
            elif method == "tools/list":
                if message.get("params", {}).get("cursor") == "more":
                    result = {"tools": [{"name": "extra_status", "description": "Read additional status details",
                                         "inputSchema": {"type": "object", "properties": {}},
                                         "annotations": {"readOnlyHint": True}}]}
                else:
                    result = {"tools": [{"name": "ping_status", "description": "Ping remote status",
                                         "inputSchema": {"type": "object", "properties": {}},
                                         "annotations": {"readOnlyHint": True}}],
                              "nextCursor": "more"}
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": "remote status is ready"}]}
            else:
                self.send_response(202)
                self.end_headers()
                return
            payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if method == "initialize":
                self.send_header("Mcp-Session-Id", "fake-session")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    mcp_http_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MCPHTTPFake)
    threading.Thread(target=mcp_http_server.serve_forever, daemon=True).start()
    mcp_http_url = f"http://127.0.0.1:{mcp_http_server.server_address[1]}/mcp"
    mcp_config = {
        "mcpServers": {
            "demo": {"command": sys.executable, "args": [str(mcp_script)],
                     "description": "Demo lookup item server", "env": {"MCP_SECRET": "mcp-secret"}},
            "httpdemo": {"type": "streamable-http", "url": mcp_http_url,
                         "description": "Remote ping status server"},
            "hidden": {"command": "not-started"},
        },
        "disabledMcpServers": ["hidden"],
    }
    home_config = tmp / "mcp-home"
    home_config.mkdir()
    (home_config / ".claude.json").write_text(json.dumps({
        "mcpServers": {"global": {"command": "not-launched"},
                       "disabled-global": {"command": "not-launched"}},
        "projects": {str(mcp_root): {
            "mcpServers": {"project-extra": {"command": "not-launched"}},
            "disabledMcpServers": ["disabled-global"],
        }},
    }), encoding="utf-8")
    forge_config = tmp / "mcp-forge"
    forge_config.mkdir()
    (forge_config / "mcp.json").write_text(json.dumps({
        "mcpServers": {"forge-extra": {"command": "not-launched"}},
    }), encoding="utf-8")
    (mcp_root / ".mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")
    mcp_tools = Agent(s, UI(), router=router).tools
    mcp_tools.mode = "smart"
    mcp_tools.ui.confirm = lambda *args: ("y", "")
    mcp_usage = mcpmod.UsageStore(tmp / "mcp-usage.json")
    mcp_manager = mcpmod.MCPManager(mcp_tools, mcp_root, mcp_usage,
                                    home_dir=home_config, forge_dir=forge_config)
    assert {"global", "project-extra", "forge-extra", "demo", "httpdemo"} <= set(mcp_manager.configs)
    assert "hidden" not in mcp_manager.configs and "disabled-global" not in mcp_manager.configs
    cold = mcp_manager.schemas("weather forecast")
    assert not mcp_manager.clients and all(not schema["function"]["name"].startswith("mcp__")
                                           for schema in cold), cold
    found = mcp_manager.search("demo lookup item by id")
    stdio_id = "mcp__demo__lookup_item"
    assert stdio_id in found and stdio_id in mcp_manager.tools_by_id, found
    assert "not declared" in mcp_manager.execute(stdio_id, {"id": "42", "invented": 1})
    assert mcp_manager.execute(stdio_id, {"id": "42"}) == "item 42 token=[secret redacted]"
    assert mcp_usage.server_state("demo").get("last_used")
    http_found = mcp_manager.search("remote ping status")
    http_id = "mcp__httpdemo__ping_status"
    assert http_id in http_found and mcp_manager.execute(http_id, {}) == "remote status is ready"
    assert mcp_http_calls == [("initialize", None, None),
                              ("notifications/initialized", "fake-session", "2025-03-26"),
                              ("tools/list", "fake-session", "2025-03-26"),
                              ("tools/list", "fake-session", "2025-03-26"),
                              ("tools/call", "fake-session", "2025-03-26")], mcp_http_calls
    mcp_manager.exposed.clear()
    mcp_usage.update_server("demo", last_used=time.time() - mcpmod.DORMANT_AFTER - 1)
    dormant_schemas = mcp_manager.schemas("weather forecast")
    assert all("demo: Demo lookup item server" not in schema["function"]["description"]
               for schema in dormant_schemas)
    assert stdio_id in mcp_manager.search("demo lookup item by id")
    assert "Deferred MCP schemas" in mcp_manager.report()
    mcp_manager.close()
    mcp_http_server.shutdown()
    mcp_http_server.server_close()
    print("ok  MCP stdio/HTTP servers load on demand, resolve exact tools, redact secrets, and wake when matched")

    # 36. A repeatedly failing MCP server sleeps until its configuration changes.
    broken_root = tmp / "broken-mcp-project"
    broken_root.mkdir()
    (broken_root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "broken": {"command": "forge-command-that-does-not-exist", "description": "Broken lookup service"},
    }}), encoding="utf-8")
    broken_manager = mcpmod.MCPManager(mcp_tools, broken_root, mcpmod.UsageStore(tmp / "broken-usage.json"),
                                      home_dir=tmp / "home", forge_dir=tmp / "forge")
    for _ in range(3):
        assert "could not start" in broken_manager._load_tools("broken")
    assert "dormant" in broken_manager._load_tools("broken")
    broken_manager.configs["broken"]["config"].update(command=sys.executable, args=[str(mcp_script)])
    assert not broken_manager._load_tools("broken")
    assert broken_manager.usage.server_state("broken")["failures"] == 0
    broken_manager.close()
    print("ok  MCP startup failures go dormant after three attempts and reset on config change")

    # 37. /doctor exposes persistent API/model/key/skill telemetry.
    with uimod.console.capture() as diagnostic_output:
        assert app.handle("/doctor")
    diagnostic_text = diagnostic_output.get()
    assert "api:" in diagnostic_text and "model:" in diagnostic_text, diagnostic_text
    print("ok  /doctor reports stored Forge usage")

    # 38. Final claims, paths and named functions need evidence from the project or this turn.
    guard_root = tmp / "guard-project"
    guard_root.mkdir()
    ledger = guardsmod.Ledger()
    assert any("no test command showed a passing result" in p
               for p in guardsmod.check_claims("All tests passed.", ledger, guard_root))
    ledger.add("run_shell", {"command": "python -m pytest"}, "exit code 0\n2 passed")
    assert not guardsmod.check_claims("All tests passed.", ledger, guard_root)
    # Naming the folder the files were written into is not an unverified claim.
    site = guard_root / "site"
    folder_ledger = guardsmod.Ledger()
    folder_ledger.add("edit_file", {"path": str(site / "index.html")}, "Edited index.html")
    assert not any("no successful file write" in p for p in guardsmod.check_claims(
        f"Files created in `{site}/`:", folder_ledger, guard_root))
    assert any("no successful file write" in p for p in guardsmod.check_claims(
        f"Files created in `{guard_root / 'other'}/`:", folder_ledger, guard_root))
    ledger.add("run_shell", {"command": "python -m pytest"}, "exit code 1\n1 failed")
    assert any("last test run did not show a passing result" in p
               for p in guardsmod.check_claims("All tests passed.", ledger, guard_root))
    for command, output in (
        ("python -m pytest", "exit code 0\nRan 0 tests\nOK"),
        ("echo pytest", "exit code 0\n2 passed"),
    ):
        weak_evidence = guardsmod.Ledger()
        weak_evidence.add("run_shell", {"command": command}, output)
        assert any("passing result" in p for p in
                   guardsmod.check_claims("All tests passed.", weak_evidence, guard_root))
    fake_build = guardsmod.Ledger()
    fake_build.add("run_shell", {"command": "echo build"}, "exit code 0\nbuild succeeded")
    assert any("no build ran successfully" in p
               for p in guardsmod.check_claims("The build succeeded.", fake_build, guard_root))
    read_only_ledger = guardsmod.Ledger()
    read_only_ledger.add("read_file", {"path": "sample.py"}, "1 def sample(): pass")
    assert any("you said it works" in p
               for p in guardsmod.check_claims("The fix works.", read_only_ledger, guard_root))
    # a pure question (no tools used) describing how something works is not a fix claim
    assert not any("you said it works" in p for p in guardsmod.check_claims(
        "Each skill works by loading its instructions. Everything works like this.", guardsmod.Ledger(), guard_root))
    # paste: text first, then Explorer files as quoted paths, then a saved screenshot path
    class _Buf:
        def __init__(self): self.text = ""
        def insert_text(self, t): self.text += t
    class _Ev:
        def __init__(self): self.current_buffer = _Buf()
    from forgecli import cli as climod
    ev = _Ev(); climod._paste_clipboard(ev, lambda: "a\r\nb", lambda: ["x"], lambda: "y")
    assert ev.current_buffer.text == "a\nb"
    ev = _Ev(); climod._paste_clipboard(ev, lambda: "", lambda: [r"C:\My Docs\a.png", r"C:\b.txt"], lambda: "")
    assert ev.current_buffer.text == r'"C:\My Docs\a.png" C:\b.txt', ev.current_buffer.text
    ev = _Ev(); climod._paste_clipboard(ev, lambda: "", lambda: [], lambda: r"C:\p\shot.png")
    assert ev.current_buffer.text == r"C:\p\shot.png"
    ev = _Ev(); assert climod._paste_clipboard(ev, lambda: "", lambda: [], lambda: "") == ""
    print("ok  paste inserts text, copied file paths, or a saved screenshot path")
    from forgecli.tools import unixy_to_powershell
    assert unixy_to_powershell(r"cd C:\x && npm install") == r"cd C:\x; if ($?) { npm install }"
    assert unixy_to_powershell("a || b") == "a; if (-not $?) { b }"
    assert unixy_to_powershell('echo "a && b"') == 'echo "a && b"'   # quoted: left alone
    print("ok  && and || are rewritten for Windows PowerShell 5.1")
    from forgecli.agent import Agent as _A
    reads = [{"function": {"name": "read_file"}}]
    idle, acts = 0, []
    for _ in range(_A.STALL_STOP):
        idle, a = _A._stall(_A, reads, idle)
        acts.append(a)
    assert acts.index("nudge") == _A.STALL_NUDGE - 1 and acts[-1] == "stop", acts
    assert _A._stall(_A, [{"function": {"name": "edit_file"}}], 9) == (0, "")
    print("ok  a read-only loop is nudged, then stopped; any real action resets it")
    delegated_ledger = guardsmod.Ledger()
    delegated_ledger.add("delegate", {}, "A worker says all tests passed.")
    assert any("no test command showed a passing result" in p
               for p in guardsmod.check_claims("All tests passed.", delegated_ledger, guard_root))
    missing = guardsmod.check_claims(
        "I created src/ghost.py. The function `ghost_function()` is defined there.",
        guardsmod.Ledger(), guard_root)
    assert any("created a file" in p for p in missing), missing
    assert any("don't exist" in p for p in missing), missing
    assert any("function names were not found" in p for p in missing), missing
    (guard_root / "src").mkdir()
    (guard_root / "src" / "created.py").write_text("value = 1\n", encoding="utf-8")
    written_ledger = guardsmod.Ledger()
    written_ledger.add("write_file", {"path": "src/created.py"}, "Wrote file.")
    assert not guardsmod.check_claims("I created src/created.py.", written_ledger, guard_root)
    (guard_root / "sample.py").write_text("def real_function():\n    return True\n", encoding="utf-8")
    assert not guardsmod.missing_symbols("The function `real_function()` is defined.", guard_root)
    docs_ledger = guardsmod.Ledger()
    docs_ledger.add("web_fetch", {}, "The external_api() function is documented.")
    assert not guardsmod.missing_symbols("The function `external_api()` is documented.", guard_root, docs_ledger)
    print("ok  final test claims require a successful test run; invented paths and symbols are caught")

    # 39. Placeholder retries and no-op file changes remain blocked.
    guard_tools = Agent(s, UI(), router=router).tools
    guard_tools.root = guard_root
    placeholder = "# TODO: implement the logic\n"
    placeholder_path = str(guard_root / "placeholder.py")
    assert "placeholder text" in guard_tools.t_write_file(placeholder_path, placeholder)
    assert "placeholder text" in guard_tools.t_write_file(placeholder_path, placeholder)
    stub_path = str(guard_root / "stub.py")
    stub = "def unfinished():\n    pass\n"
    assert "placeholder text" in guard_tools.t_write_file(stub_path, stub)
    assert guardsmod.placeholders("# TODO\n", "")
    assert guardsmod.placeholders(stub, "")
    assert not guardsmod.placeholders("def complete():\n    return True\n", "")
    assert not guardsmod.placeholders(stub, stub)
    existing_path = guard_root / "existing.py"
    existing_path.write_text("value = 1\n", encoding="utf-8")
    assert "change nothing" in guard_tools.t_edit_file(str(existing_path), "value = 1", "value = 1")
    assert "nothing was changed" in guard_tools.t_write_file(str(existing_path), "value = 1\n")
    assert existing_path.read_text(encoding="utf-8") == "value = 1\n"
    print("ok  placeholder retries and no-op writes/edits do not land")

    # 40. An unsupported final answer gets one repair attempt, then an explicit warning.
    guard_agent = Agent(s, UI(), router=router)
    guard_agent.tools.root = guard_root
    unsupported = {"content": "All tests passed.", "provider": "fake", "model": "test"}
    assert guard_agent._guard(unsupported) and guard_agent.bounced
    assert guard_agent._guard(unsupported) is False and guard_agent.unverified
    assert "no test command showed a passing result" in " ".join(guard_agent.unverified)
    print("ok  unsupported final claims are bounced once, then marked unverified")

    # 41. Streamed text, reasoning and tool hints are display-only: shown live in the status area,
    # never printed to the scrollback or added to the conversation before the claim check.
    from forgecli.providers import Action, Thought

    class LiveRouter:
        def complete(self, messages, schemas, **kwargs):
            on_text = kwargs["on_text"]
            on_text(Thought("let me check the tests first"))
            on_text(Action("run_shell"))
            on_text("All tests ")
            on_text("passed.")
            return {"content": "All tests passed.", "tool_calls": [], "provider": "fake",
                    "model": "test", "key": "key", "usage": {}, "truncated": False}

    live_ui = UI()
    live_agent = Agent(s, live_ui, router=LiveRouter())
    before = len(live_agent.messages)
    assert live_agent._ask()["content"] == "All tests passed."
    assert live_ui.thinking == "let me check the tests first", live_ui.thinking
    assert live_ui.activity == "Preparing: running a command", live_ui.activity
    assert live_ui.streamed == "All tests passed."
    assert len(live_agent.messages) == before
    assert not any("let me check" in str(m.get("content")) for m in live_agent.messages)
    live_ui.stop_spinner()
    assert live_ui.thinking == "" and live_ui.streamed == ""
    print("ok  reasoning, tool hints and the reply show live but never enter the conversation")

    # 42. NEXT 7 foundation: key prefixes, masking, unlimited slots, redaction before the model.
    from forgecli import keyvault, github
    assert keyvault.detect_provider("sk-or-v1-abc") == "openrouter"
    assert keyvault.detect_provider("sk-ant-api03-x") == "anthropic"
    assert keyvault.detect_provider("gsk_abc") == "groq" and keyvault.detect_provider("zzz") is None
    for unverified_prefix in ("csk-", "xai-"):
        unverified_key = unverified_prefix + "A1b2C3d4E5f6G7h8J9k0"
        assert keyvault.detect_provider(unverified_key) is None
        assert unverified_key not in keyvault.redact(f"api key {unverified_key}")[0]
    assert keyvault.detect_provider("hf_A1b2C3d4E5f6G7h8J9k0") == "huggingface"
    fake_key = "gsk_" + "A1b2C3d4E5f6G7h8J9k0"
    assert keyvault.mask(fake_key) == "…" + fake_key[-4:] and fake_key[:-4] not in keyvault.mask(fake_key)
    kv_dir = pathlib.Path(tempfile.mkdtemp())
    store = keyvault.KeyStore(kv_dir / "keys.json")
    for i in range(20):
        assert store.add(f"gsk_{i:02d}" + "x" * 20) == "groq"
    assert len(store.load()["groq"]) == 20 and store.remove("groq", 19) and len(store.load()["groq"]) == 19
    assert all(fake_key not in line for line in store.listing())

    class SeenRouter:
        seen = []
        def complete(self, messages, schemas, **kwargs):
            SeenRouter.seen.append(json.dumps(messages))
            return {"content": "noted", "tool_calls": [], "provider": "fake", "model": "test",
                    "key": "key", "usage": {}, "truncated": False}

    key_agent = Agent(s, UI(), router=SeenRouter())
    key_agent.keystore_path = kv_dir / "keys2.json"
    key_agent.save = lambda: None
    key_agent.run_turn(f"here is my key {fake_key} please use it")
    assert SeenRouter.seen and all(fake_key not in m for m in SeenRouter.seen)
    assert fake_key not in json.dumps(key_agent.messages)
    assert keyvault.KeyStore(kv_dir / "keys2.json").load() == {"groq": [fake_key]}
    print("ok  pasted keys are saved, masked and never sent to the model; 20 keys per provider fit")

    # 42b. Key checks use the real key without exposing it in their result; storage falls back cleanly.
    expected_probe_key = "probe-key-with-random-suffix-1234567890"
    key_probe_headers = []
    class KeyProbe(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            header = self.headers.get("Authorization", "")
            key_probe_headers.append(header)
            valid = header == "Bearer " + expected_probe_key
            payload = b'{"data":[]}' if valid else b'{"error":"unauthorized"}'
            self.send_response(200 if valid else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    key_probe_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), KeyProbe)
    key_probe_thread = threading.Thread(target=key_probe_server.serve_forever, daemon=True)
    key_probe_thread.start()
    try:
        key_probe_url = f"http://127.0.0.1:{key_probe_server.server_port}/v1"
        assert keyvault.test_key(key_probe_url, expected_probe_key) == (True, "works")
        ok, why = keyvault.test_key(key_probe_url, "wrong-probe-key")
        assert not ok and "rejected" in why and "wrong-probe-key" not in why
        assert key_probe_headers == ["Bearer " + expected_probe_key, "Bearer wrong-probe-key"]
    finally:
        key_probe_server.shutdown()
        key_probe_server.server_close()
        key_probe_thread.join(timeout=2)
    old_key_file = _os.environ.get("FORGE_KEYS_FILE")
    old_keyring_setting = _os.environ.get("FORGE_KEYRING")
    try:
        _os.environ["FORGE_KEYS_FILE"] = str(kv_dir / "fallback.json")
        _os.environ["FORGE_KEYRING"] = "0"
        fallback_store = keyvault.KeyStore()
        assert fallback_store.backend == "file"
        assert fallback_store.add("fallback-test-secret-1234567890", "groq") == "groq"
        assert fallback_store.load()["groq"] == ["fallback-test-secret-1234567890"]
    finally:
        if old_key_file is None:
            _os.environ.pop("FORGE_KEYS_FILE", None)
        else:
            _os.environ["FORGE_KEYS_FILE"] = old_key_file
        if old_keyring_setting is None:
            _os.environ.pop("FORGE_KEYRING", None)
        else:
            _os.environ["FORGE_KEYRING"] = old_keyring_setting
    saved_keyring = keyvault._keyring

    class MemoryKeyring:
        def __init__(self):
            self.values = {}

        def set_password(self, service, username, password):
            self.values[(service, username)] = password

        def get_password(self, service, username):
            return self.values.get((service, username))

        def delete_password(self, service, username):
            self.values.pop((service, username), None)

    memory_keyring = MemoryKeyring()
    keyvault._keyring = lambda: memory_keyring
    try:
        keyring_store = keyvault.KeyStore(kv_dir / "keyring.json", backend="keyring")
        keyring_secret = "keyring-test-secret-1234567890"
        assert keyring_store.backend == "keyring"
        assert keyring_store.add(keyring_secret, "groq") == "groq"
        assert keyring_secret not in (kv_dir / "keyring.json").read_text(encoding="utf-8")
        assert keyring_store.load()["groq"] == [keyring_secret]
        assert keyring_store.remove("groq", 0)
        assert not memory_keyring.values
    finally:
        keyvault._keyring = saved_keyring
    print("ok  live key checks authenticate correctly and rejected-key messages don't leak secrets")

    # 42c. Custom keys merge with environment keys, unknown keys can be filed, and /setup is usable.
    from forgecli import config as configmod
    from forgecli.cli import App
    key_settings = {
        "FORGE_KEYS_FILE": _os.environ.get("FORGE_KEYS_FILE"),
        "FORGE_KEYRING": _os.environ.get("FORGE_KEYRING"),
        "FORGE_PROVIDERS": _os.environ.get("FORGE_PROVIDERS"),
        "FORGE_KEYLESS": _os.environ.get("FORGE_KEYLESS"),
        "NEXT7CUSTOM_API_KEYS": _os.environ.get("NEXT7CUSTOM_API_KEYS"),
    }
    try:
        _os.environ["FORGE_KEYS_FILE"] = str(kv_dir / "settings-keys.json")
        _os.environ["FORGE_KEYRING"] = "0"
        _os.environ["FORGE_PROVIDERS"] = "next7custom"
        _os.environ["FORGE_KEYLESS"] = "0"
        _os.environ["NEXT7CUSTOM_API_KEYS"] = "custom-key,env-key"
        config_store = keyvault.KeyStore()
        config_store.add_custom("next7custom", "https://example.test/v1", "model")
        config_store.add("custom-key", "next7custom")
        config_store.add("stored-key", "next7custom")
        merged = configmod.load_settings().providers
        assert len(merged) == 1 and merged[0].name == "next7custom"
        assert merged[0].keys == ["custom-key", "env-key", "stored-key"], merged[0].keys

        unknown_agent = Agent(Settings(retry_rounds=0), UI(), router=Router(
            Settings(retry_rounds=0), lambda *args: None, CooldownStore(kv_dir / "unknown-state.json")))
        unknown_agent.keystore_path = kv_dir / "unknown-keys.json"
        unknown_agent.key_probe = lambda *args: None
        unknown_agent.key_tester = lambda *args: (True, "works")
        unknown_app = App(unknown_agent, UI())
        unknown_app.line_input = lambda _prompt: "groq"
        unknown_key = "unprefixed-secret-value-123456789"
        assert unknown_app.add_key(unknown_key)
        assert keyvault.KeyStore(kv_dir / "unknown-keys.json").load() == {"groq": [unknown_key]}

        class CaptureKeyListUI(UI):
            def __init__(self):
                super().__init__()
                self.rows = []

            def table(self, _title, _headers, rows, _styles=None):
                self.rows = rows

            def info(self, _message):
                pass

        key_list_ui = CaptureKeyListUI()
        unknown_app.ui = key_list_ui
        unknown_app.cmd_keys("")
        key_rows = repr(key_list_ui.rows)
        assert unknown_key not in key_rows and keyvault.mask(unknown_key) in key_rows

        setup_agent = Agent(Settings(retry_rounds=0), UI(), router=Router(
            Settings(retry_rounds=0), lambda *args: None, CooldownStore(kv_dir / "setup-state.json")))
        setup_agent.keystore_path = kv_dir / "setup-keys.json"
        setup_agent.key_tester = lambda *args: (True, "works")
        setup_app = App(setup_agent, UI())
        prompts = iter(["1", ""])
        setup_app.line_input = lambda _prompt: next(prompts)
        setup_app.secret_input = lambda _prompt: "gsk_" + "Z9y8X7w6V5u4T3s2R1q0"
        import webbrowser
        original_open = webbrowser.open
        webbrowser.open = lambda _url: True
        try:
            setup_app.cmd_setup("")
        finally:
            webbrowser.open = original_open
        assert keyvault.KeyStore(kv_dir / "setup-keys.json").load()["groq"]
        assert setup_agent.router.get("groq")

        from types import SimpleNamespace
        from forgecli.mcp import UsageStore
        legacy_secret = "legacy-usage-secret-1234567890"
        legacy_usage_path = kv_dir / "legacy-usage.json"
        legacy_usage_path.write_text(json.dumps({
            "records": {f"key:groq:{legacy_secret}": {
                "calls": 2, "failures": 2, "latency_total": 1.5, "last_used": 10
            }}, "servers": {}
        }), encoding="utf-8")
        migrated_usage = UsageStore(legacy_usage_path)
        legacy_usage_text = legacy_usage_path.read_text(encoding="utf-8")
        assert legacy_secret not in legacy_usage_text
        assert migrated_usage.report()[0][0] == f"key:groq:{keyvault.fingerprint(legacy_secret)}"

        usage_agent = Agent(Settings(retry_rounds=0), UI(), router=Router(
            Settings(retry_rounds=0), lambda *args: None, CooldownStore(kv_dir / "usage-state.json")))
        usage_path = kv_dir / "usage.json"
        usage_agent.tools.usage = UsageStore(usage_path)
        health_key = "health-secret-key-value-123456789"
        for _ in range(3):
            usage_agent._attempt_keys[("groq", "model")] = keyvault.fingerprint(health_key)
            usage_agent._event("fail", "groq", "model", "401 rejected")
        health_provider = SimpleNamespace(name="groq", pool=SimpleNamespace(keys=[health_key]))
        usage_agent.router.providers = [health_provider]
        usage_app = App(usage_agent, UI())
        fingerprint = keyvault.fingerprint(health_key)
        assert health_key not in usage_path.read_text(encoding="utf-8")
        assert usage_app.key_health("groq", health_key) == (0, 3)
        assert usage_app.failing_keys() == [f"groq {keyvault.mask(health_key)} (3 failures, 0 ok)"]
    finally:
        for name, value in key_settings.items():
            if value is None:
                _os.environ.pop(name, None)
            else:
                _os.environ[name] = value
    assert f"key:groq:{fingerprint}" in usage_path.read_text(encoding="utf-8")
    print("ok  stored/environment keys merge; unknown-provider, setup and safe key-health flows work")

    # 43. NEXT 9 foundation: GitHub actions sorted by risk, PR claims need a returned URL.
    assert github.classify("GET", "/repos/a/b/pulls") == github.READ
    assert github.classify("POST", "/repos/a/b/pulls") == github.WRITE
    assert github.classify("PUT", "/repos/a/b/pulls/7/merge") == github.DESTRUCTIVE
    assert github.classify("DELETE", "https://api.github.com/repos/a/b") == github.DESTRUCTIVE
    assert github.classify("PATCH", "/repos/a/b") == github.DESTRUCTIVE
    assert github.git_risk("git push --force origin feat") == github.DESTRUCTIVE
    assert github.git_risk("git push origin main") == github.DESTRUCTIVE
    assert github.git_risk("gh pr create --fill") == github.WRITE
    assert github.git_risk("git status") == github.READ
    url = "https://github.com/a/b/pull/12"
    assert github.pr_url_ok(url, [f"created {url}"]) and not github.pr_url_ok(url, ["ok"])
    print("ok  GitHub calls are classed read/write/destructive and PR claims need the returned URL")

    # 44. File checkpoints restore exact bytes and rewind the matching conversation operation.
    undo_root = tmp / "next6"
    undo_root.mkdir()
    undo_agent = Agent(s, UI(), router=router)
    undo_agent.tools.root = undo_root
    original = b"before\r\n"
    undo_path = undo_root / "existing.txt"
    undo_path.write_bytes(original)
    undo_agent.messages.extend([
        {"role": "user", "content": "change the file"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "edit1", "type": "function",
             "function": {"name": "edit_file", "arguments": json.dumps(
                 {"path": str(undo_path), "old_string": "before", "new_string": "after"})}}]},
    ])
    assert undo_agent._execute(
        "edit_file", json.dumps({"path": str(undo_path), "old_string": "before", "new_string": "after"})
    ).startswith("Edited ")
    assert undo_path.read_bytes() != original
    assert "restored" in undo_agent.undo_last()
    assert undo_path.read_bytes() == original
    assert [m.get("role") for m in undo_agent.messages] == ["system", "user"]
    created = undo_root / "created.txt"
    assert undo_agent.tools.t_write_file(str(created), "created\n").startswith("Wrote ")
    assert "removed" in undo_agent.undo_last()
    assert not created.exists()
    after_reset = undo_root / "after-reset.txt"
    assert undo_agent.tools.t_write_file(str(after_reset), "still here\n").startswith("Wrote ")
    undo_agent.reset()
    assert "no file changes" in undo_agent.undo_last()
    assert after_reset.exists()
    from forgecli.cli import App, COMMANDS
    assert "/undo" in COMMANDS
    command_agent = Agent(s, UI(), router=router)
    command_agent.tools.root = undo_root
    command_file = undo_root / "command.txt"
    command_agent.messages.extend([
        {"role": "user", "content": "create a file"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "write1", "type": "function",
             "function": {"name": "write_file", "arguments": json.dumps(
                 {"path": str(command_file), "content": "command\n"})}}]},
    ])
    assert command_agent._execute(
        "write_file", json.dumps({"path": str(command_file), "content": "command\n"})
    ).startswith("Wrote ")
    assert App(command_agent, UI()).handle("/undo")
    assert not command_file.exists()
    print("ok  /undo restores original bytes, removes created files and rewinds the tool conversation")

    # 45. apply_patch tolerates whitespace drift, rejects bad context and handles multiple files.
    patch_agent = Agent(s, UI(), router=router)
    patch_tools = patch_agent.tools
    assert any(schema["function"]["name"] == "apply_patch" for schema in patch_agent._schemas())
    patch_tools.root = undo_root
    first = undo_root / "first.py"
    second = undo_root / "second.py"
    first.write_text("def greet():  \n    return 'old'\n", encoding="utf-8")
    second.write_text("value = 1\n", encoding="utf-8")
    patch = (f"*** Begin Patch\n*** Update File: {first}\n@@\n def greet():\n"
             "-    return 'old'\n+    return 'new'\n"
             f"*** Update File: {second}\n@@\n-value = 1\n+value = 2\n*** End Patch")
    patch_agent.messages.extend([
        {"role": "user", "content": "patch both files"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "patch1", "type": "function",
             "function": {"name": "apply_patch", "arguments": json.dumps({"patch": patch})}}]},
    ])
    patch_result = patch_agent._execute("apply_patch", json.dumps({"patch": patch}))
    assert "Patched 2 file(s)" in patch_result, patch_result
    assert first.read_text(encoding="utf-8") == "def greet():  \n    return 'new'\n"
    assert second.read_text(encoding="utf-8") == "value = 2\n"
    assert "restored" in patch_agent.undo_last()
    assert first.read_text(encoding="utf-8") == "def greet():  \n    return 'old'\n"
    assert second.read_text(encoding="utf-8") == "value = 1\n"
    bad = (f"*** Begin Patch\n*** Update File: {first}\n@@\n"
           "-missing context\n+not applied\n*** End Patch")
    before_bad = first.read_bytes()
    assert "no matching context" in patch_tools.t_apply_patch(bad)
    assert first.read_bytes() == before_bad
    eof_bad = (f"*** Begin Patch\n*** Update File: {first}\n@@\n"
               "-def greet():\n+def greeting():\n*** End of File\n*** End Patch")
    assert "does not match the file end" in patch_tools.t_apply_patch(eof_bad)
    assert first.read_bytes() == before_bad
    added_path = undo_root / "added.py"
    add_patch = (f"*** Begin Patch\n*** Add File: {added_path}\n"
                 "+def added():\n+    return True\n*** End Patch")
    add_result = patch_tools.execute("apply_patch", json.dumps({"patch": add_patch}))
    assert "Patched 1 file(s)" in add_result and added_path.exists(), add_result
    assert not guardsmod.check_claims(
        f"I created `{added_path}`.", patch_tools.ledger, undo_root)
    print("ok  fuzzy multi-file patches apply; a mismatched hunk is rejected without changing files")

    # 46. Recursive searches refuse home/drive roots, while scoped handoff searches work.
    from forgecli.tools import Tools
    search_tools = Tools(UI(), "powershell")
    broad_glob = search_tools.t_glob("**\\HANDOFF*", str(pathlib.Path.home()))
    assert "refusing a recursive search" in broad_glob
    broad_grep = search_tools.t_grep("handoff", str(pathlib.Path.home()))
    assert "refusing a recursive search" in broad_grep
    search_root = tmp / "search-project"
    handoff_dir = search_root / "docs"
    handoff_dir.mkdir(parents=True)
    handoff_file = handoff_dir / "HANDOFF.md"
    handoff_file.write_text("handoff", encoding="utf-8")
    scoped_glob = search_tools.t_glob("**/HANDOFF*", str(search_root))
    assert str(handoff_file.resolve()) in scoped_glob, scoped_glob
    print("ok  recursive searches reject home roots and find handoff files within a project")

    # 47. Persisted health demotes failing keys/models, stale skills stay searchable.
    health_path = tmp / "health-usage.json"
    health_usage = mcpmod.UsageStore(health_path)
    bad_key, good_key = "health-key-bad-1101", "health-key-good-2202"
    for _ in range(3):
        health_usage.record("key", f"health:{keyvault.fingerprint(bad_key)}", False)
        health_usage.record("timeout", "health:timed-out", False)
        health_usage.record("guard", "health:guarded", False)
    assert health_usage.get("key", f"health:{keyvault.fingerprint(bad_key)}")[
        "consecutive_failures"] == 3
    assert health_usage.recent_failures("guard", "health:guarded") == 3
    health_settings = Settings(providers=[ProviderConfig(
        "health", "http://127.0.0.1:1/v1", "primary",
        [bad_key, good_key], ["alternate", "guarded", "timed-out"])])
    health_router = Router(health_settings, lambda *_: None,
                           CooldownStore(tmp / "health-state.json"))
    health_provider = health_router.providers[0]
    health_router.usage = health_usage
    assert health_router._health_rank("health", "timed-out") == 1
    assert health_router._health_rank("health", "guarded") == 1
    ordered_models = health_router._models_of(health_provider)
    assert ordered_models.index("guarded") > ordered_models.index("alternate")
    assert ordered_models.index("timed-out") > ordered_models.index("alternate")
    health_usage.record("timeout", "health:timed-out", True)
    assert health_router._health_rank("health", "timed-out") == 0
    assert health_router._models_of(health_provider).index("timed-out") < ordered_models.index("timed-out")
    for _ in range(20):
        health_usage.record("guard", "health:guarded", True)
    assert health_usage.recent_failures("guard", "health:guarded") == 0

    class CaptureInfoUI(UI):
        def __init__(self):
            super().__init__()
            self.infos = []

        def info(self, message):
            self.infos.append(message)

    stale_file = tmp / "stale-skill.md"
    stale_file.write_text("stale skill instructions", encoding="utf-8")
    active_file = tmp / "active-skill.md"
    active_file.write_text("active skill instructions", encoding="utf-8")
    discovered = {
        "stale-skill": skills.Skill("stale-skill", "old skill", stale_file, "built-in"),
        "active-skill": skills.Skill("active-skill", "current skill", active_file, "built-in"),
    }
    original_discover = skills.discover
    skills.discover = lambda: discovered
    try:
        health_ui = CaptureInfoUI()
        health_agent = Agent(health_settings, health_ui, router=health_router)
    finally:
        skills.discover = original_discover
    health_agent.tools.usage = health_usage
    health_router.usage = health_usage
    health_usage.mark_seen("skill", "stale-skill")
    stale_row = health_usage.data["records"]["skill:stale-skill"]
    stale_row["last_used"] = time.time() - 31 * 24 * 60 * 60
    stale_row["first_seen"] = time.time() - 40 * 24 * 60 * 60
    health_usage._save()
    health_agent._apply_usage_preferences()
    assert health_provider.pool.keys == [good_key, bad_key]
    active_names = {skill.name for skill in health_agent._active_skills().values()}
    assert "stale-skill" not in active_names and "active-skill" in active_names
    assert "stale-skill" in health_agent._execute(
        "find_skill", json.dumps({"query": "stale skill"}))
    assert not health_ui.infos                     # no alarming start-up line any more
    assert any("stale-skill" in r for r in health_agent.resting) and any("health " in r for r in health_agent.resting)
    print("ok  repeated failures demote keys/models; 30d-unused skills stay available via find_skill")

    # 48. Repo map is bounded, scoped and lists source definitions.
    from forgecli import repomap
    map_root = tmp / "repo-map"
    map_src = map_root / "src"
    map_src.mkdir(parents=True)
    (map_src / "app.py").write_text(
        "class App:\n    def run(self):\n        return True\n\ndef helper():\n    pass\n",
        encoding="utf-8")
    (map_src / "client.ts").write_text(
        "export function start() {}\nconst handler = (value) => value;\n", encoding="utf-8")
    ignored_root = map_root / "node_modules"
    ignored_root.mkdir()
    (ignored_root / "vendor.py").write_text("class Vendor:\n    pass\n", encoding="utf-8")
    repo_map = search_tools.t_repo_map(str(map_root))
    assert "App" in repo_map and "run" in repo_map and "helper" in repo_map
    assert "start" in repo_map and "handler" in repo_map
    assert "Vendor" not in repo_map
    assert "refusing to map a home or drive root" in search_tools.t_repo_map(str(pathlib.Path.home()))
    assert "map truncated" in repomap.build(map_root, max_files=1)
    print("ok  repo map lists definitions, prunes dependency trees and stops at configured limits")

    # 49. Plan mode only exposes read tools, displays the plan and proceeds automatically.
    plan_root = tmp / "plan-project"
    plan_root.mkdir()
    plan_target = plan_root / "planned.txt"

    class PlanSequenceRouter:
        def __init__(self):
            self.schemas = []
            self.calls = 0

        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            names = {tool["function"]["name"] for tool in tools}
            self.schemas.append(names)
            common = {"provider": "fake", "model": "plan-test", "key": "plan-key",
                      "usage": {}, "truncated": False}
            if self.calls == 1:
                return dict(common, content="1. Create the requested file.", tool_calls=[])
            if self.calls == 2:
                return dict(common, content="", tool_calls=[{
                    "id": "plan-write", "type": "function", "function": {
                        "name": "write_file", "arguments": json.dumps(
                            {"path": str(plan_target), "content": "planned\n"})}}])
            return dict(common, content="Created the planned file.", tool_calls=[])

    class CapturePlanUI(UI):
        def __init__(self):
            super().__init__()
            self.plans = []

        def plan(self, title, body):
            self.plans.append((title, body))

    plan_router = PlanSequenceRouter()
    plan_ui = CapturePlanUI()
    plan_agent = Agent(Settings(auto_approve=True, retry_rounds=0), plan_ui, router=plan_router)
    plan_agent.tools.root = plan_root
    plan_agent.run_plan("Create planned.txt")
    assert plan_router.calls == 3 and plan_target.read_text(encoding="utf-8") == "planned\n"
    assert "write_file" not in plan_router.schemas[0]
    assert "write_file" in plan_router.schemas[1]
    assert len(plan_ui.plans) == 2 and "Create" in plan_ui.plans[1][1]
    assert "/plan" in __import__("forgecli.cli", fromlist=["COMMANDS"]).COMMANDS
    print("ok  read-only plan display automatically transitions to normal execution")

    # 50. Existing process control supports background launch, later output reads and stop.
    import os
    from forgecli import procs
    from forgecli.tools import Tools
    command = ("echo process-ready & ping -n 11 127.0.0.1 >nul" if os.name == "nt"
               else "printf 'process-ready\\n'; sleep 10")
    process = procs.start("cmd" if os.name == "nt" else "sh", command, str(tmp))
    try:
        deadline = time.time() + 3
        while "process-ready" not in process.output() and time.time() < deadline:
            time.sleep(0.05)
        assert "process-ready" in process.output()
        process_tools = Tools(UI(), "powershell")
        assert str(process.id) in process_tools.t_process("list")
        assert "process-ready" in process_tools.t_process("output", process.id)
        assert f"Stopped process {process.id}" in process_tools.t_process("stop", process.id)
    finally:
        if process.alive():
            procs.kill(process)
        procs.forget(process)
    print("ok  background process output can be read later and the process can be stopped")

    # 51. Headless one-shot mode emits a single JSON record and accurate exit codes.
    import contextlib
    import io
    from forgecli import cli as climod
    from forgecli import ui as uimod
    original_agent, original_load, original_apply = climod.Agent, climod.load_settings, climod.apply_prefs
    original_prefs = climod.load_prefs
    original_cli_console, original_ui_console = climod.console, uimod.console
    original_stdin = climod.sys.stdin

    class IsolatedAgent(Agent):
        def __init__(self, settings, ui, session=None):
            super().__init__(settings, ui, router=Router(
                settings, ui.log, CooldownStore(tmp / "headless-state.json")), session=session)

        def run_turn(self, prompt):
            self.messages.append({"role": "user", "content": prompt})
            self.messages.append({"role": "assistant", "content": "headless answer"})
            self.last_turn_ok = True
            self.last_answer = "json-test · model"

    json_settings = Settings(
        providers=[ProviderConfig("json-test", url_a, "model", ["good"])],
        auto_open=False)
    climod.Agent = IsolatedAgent
    climod.load_settings = lambda: json_settings
    climod.apply_prefs = lambda *_: None
    climod.load_prefs = lambda: {}
    stdout, stderr = io.StringIO(), io.StringIO()
    class NonInteractiveInput(io.StringIO):
        def isatty(self):
            return False

    climod.sys.stdin = NonInteractiveInput("")
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = climod.main(["--json", "read", str(note)])
        payload = json.loads(stdout.getvalue())
        assert exit_code == 0 and payload["ok"] and payload["answer"] == "headless answer"
        assert stdout.getvalue().count("\n") == 1

        climod.load_settings = lambda: Settings()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = climod.main(["--json", "unavailable"])
        failure = json.loads(stdout.getvalue())
        assert exit_code == 1 and not failure["ok"] and failure["error"] == "No providers configured."
    finally:
        climod.Agent, climod.load_settings, climod.apply_prefs = original_agent, original_load, original_apply
        climod.load_prefs = original_prefs
        climod.console, uimod.console = original_cli_console, original_ui_console
        climod.sys.stdin = original_stdin
    print("ok  headless mode returns JSON on stdout and nonzero when providers are unavailable")

    # 52. GitHub API calls use stored/env auth, redact secrets, and require explicit destructive approval.
    github_calls = []
    github_auth_headers = []
    github_token = "github-test-secret-123456789"

    class GitHubFake(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            github_calls.append(("GET", self.path, self.headers.get("Authorization")))
            if self.path.startswith("/repos/owner/missing"):
                return self.respond(404, {"message": "Not Found"})
            return self.respond(200, {"login": "test-owner", "secret": github_token})

        def do_POST(self):
            github_auth_headers.append(self.headers.get("Authorization"))
            github_calls.append(("POST", self.path, self.headers.get("Authorization")))
            payload = {"html_url": "https://github.com/owner/repo/pull/42",
                       "echo": self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
                       "secret": github_token}
            self.respond(201, payload)

        def do_DELETE(self):
            github_calls.append(("DELETE", self.path, self.headers.get("Authorization")))
            self.respond(204, {})

    github_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GitHubFake)
    threading.Thread(target=github_server.serve_forever, daemon=True).start()
    github_old_token = _os.environ.get("GITHUB_TOKEN")
    _os.environ["GITHUB_TOKEN"] = github_token
    github_tools = Agent(s, UI(), router=router).tools
    github_tools.github_api_base = f"http://127.0.0.1:{github_server.server_port}"
    github_tools.mode = "auto"
    confirmation_calls = []
    github_actions = []
    github_tools.ui.action = lambda *args: github_actions.append(args)
    github_tools.ui.confirm = lambda *args: confirmation_calls.append(args) or ("n", "")
    try:
        pr_result = github_tools.t_github_request(
            "POST", "/repos/owner/repo/pulls", json_body={"title": "test"})
        assert "HTTP 201" in pr_result and "github-test-secret" not in pr_result
        assert "[secret redacted]" in pr_result
        assert github_auth_headers[-1] == f"Bearer {github_token}"
        assert github_calls[-1] == ("POST", "/repos/owner/repo/pulls", f"Bearer {github_token}")
        assert github_tools.github_account == "test-owner"
        assert github_actions[-1] == ("GitHub", "POST owner/repo as test-owner")
        assert not confirmation_calls

        github_agent = Agent(s, UI(), router=router)
        github_agent.tools.ledger.add(
            "github_request", {}, pr_result)
        assert not guardsmod.check_claims(
            "Opened pull request https://github.com/owner/repo/pull/42.", github_agent.tools.ledger, tmp)
        assert any("returned GitHub URL" in issue for issue in guardsmod.check_claims(
            "Opened a pull request.", github_agent.tools.ledger, tmp))

        before_delete = len(github_calls)
        denied_delete = github_tools.t_github_request("DELETE", "/repos/owner/repo/issues/7")
        assert "denied this destructive action" in denied_delete
        assert confirmation_calls, denied_delete
        delete_preflights = github_calls[before_delete:]
        assert sum(call[0] == "GET" for call in delete_preflights) >= 2
        assert not any(call[0] == "DELETE" for call in delete_preflights)

        before_missing = len(github_calls)
        missing = github_tools.t_github_request("PATCH", "/repos/owner/missing/pulls/99",
                                                json_body={"title": "no"})
        assert "does not exist" in missing
        assert github_calls[before_missing:] and all(
            call[0] == "GET" for call in github_calls[before_missing:])
    finally:
        github_server.shutdown()
        github_server.server_close()
        if github_old_token is None:
            _os.environ.pop("GITHUB_TOKEN", None)
        else:
            _os.environ["GITHUB_TOKEN"] = github_old_token
    print("ok  GitHub CLI/API auth is redacted, actions identify their target, and destructive writes prompt")

    import os as _os
    from forgecli import hooks as hooksmod, memory as memorymod
    from forgecli import keyvault as keyvaultmod, tools as toolsmod
    from forgecli.cli import App, COMMANDS, _paste_clipboard
    from forgecli.tools import Tools
    from forgecli.ui import logo

    from types import SimpleNamespace
    from unittest.mock import patch
    with patch.dict(_os.environ, {"GITHUB_TOKEN": ""}), \
            patch.object(keyvaultmod.KeyStore, "load", return_value={}), \
            patch.object(toolsmod.shutil, "which", return_value="gh.exe"), \
            patch.object(toolsmod.subprocess, "run",
                         return_value=SimpleNamespace(returncode=0, stdout="gho-gh-cli-token-123456\n",
                                                      stderr="")):
        gh_tools = Tools(UI(), "powershell")
        assert gh_tools.github_token() == "gho-gh-cli-token-123456"
    with patch.dict(_os.environ, {"GITHUB_TOKEN": ""}), \
            patch.object(keyvaultmod.KeyStore, "load", return_value={}), \
            patch.object(toolsmod.shutil, "which", return_value="gh.exe"), \
            patch.object(toolsmod.subprocess, "run",
                         return_value=SimpleNamespace(returncode=1, stdout="", stderr="not logged in")):
        assert Tools(UI(), "powershell").github_token() == ""

    class PasteBuffer:
        text = ""
        def insert_text(self, value):
            self.text += value

    paste_buffer = PasteBuffer()
    pasted = _paste_clipboard(SimpleNamespace(current_buffer=paste_buffer),
                              read_text=lambda: "first line\r\nsecond line")
    assert pasted == "first line\r\nsecond line"
    assert paste_buffer.text == "first line\nsecond line"
    import io
    from rich.console import Console as _LogoConsole
    _lc = _LogoConsole(width=100, record=True, color_system=None, file=io.StringIO())
    _lc.print(logo("0.7.0", "multi-model coding agent · failover · team mode"))
    rendered_logo = _lc.export_text()
    assert "v0.7.0" in rendered_logo and "multi-model coding agent" in rendered_logo 
    assert "v0.7.0" in rendered_logo and "▀" in rendered_logo and 6 <= len(rendered_logo.splitlines()) <= 12
    print("ok  tiger logo and clipboard paste preserve multiline text")

    # pixel city: every state renders, follows real transitions, finishes, and falls back
    from forgecli import city as citymod, art as artmod
    _c = citymod.City()
    for _st in citymod.STATES:
        _c.set(_st, wait=5)
        _fr = _c.frame(120, now=100.0)
        assert _fr.count("\n") == citymod.H // 2 - 1 and "\x1b[" in _fr, _st
    assert _c.frame(120, now=1.0) == _c.frame(120, now=1.0), "frames must be deterministic"
    _c.set("processing"); _c.set_helpers(2); _c.frame(150, now=5.0); _c.set_helpers(0)
    _c.set("completed")
    assert _c.state == "completed" and not _c.settled()
    _c.since -= citymod.City.LINGER + 1
    assert _c.settled(), "done/failed/stopped linger briefly, then the city goes idle"
    _c.set("thinking")
    assert _c.state == "thinking" and not _c.settled(), "a running state never settles by itself"
    assert "\n" not in _c.frame(60).rstrip("\n"), "narrow terminals get the one-line fallback"
    _u = UI()
    _u.act("coding"); assert _u.city.state == "coding"
    _u.failover("all models are resting; waiting 12s for the soonest one")
    assert _u.city.state == "waiting"
    _cv = artmod.banner(200, 64, "0.9.0")
    assert _cv.w == 200 and _cv.h == 64 and len(_cv.sixel(3)) > 1000
    assert 10 <= artmod.mascot(24).ansi().count("\n") <= 13
    print("ok  pixel city states, timing, linger, fallback and high-res banner")

    # The wheelbarrow follows real events: loads while thinking, waits at the door during a tool,
    # unloads after it, rests while rate-limited, ticks when done, goes red on failure.
    from forgecli.city import Barrow, TRACK
    wb, clock = Barrow(), [0.0]
    def roll(state, secs):
        wb.on(state)
        out = ""
        for _ in range(int(secs * 10)):
            clock[0] += 0.1
            out = wb.text(clock[0])
        return out
    roll("thinking", 2.6)
    parked = roll("processing", 4)
    assert abs(wb.pos - TRACK) < 0.01 and wb.loaded and parked.endswith("⌂"), parked
    roll("thinking", 2)
    assert wb.delivered >= 1 and wb.pos < TRACK
    assert roll("waiting", 0.3).endswith("zZ")
    assert roll("completed", 3).endswith("✓")
    # The pixel courier draws that same state at every baked size, and fits the window.
    from forgecli import courier
    from forgecli.courier_data import SIZES
    for rows in SIZES:
        lines = courier.scene(wb, 100.0, rows)
        assert len(lines) == rows and len({ln.cell_len for ln in lines}) == 1, rows
    assert courier.pick_rows(20, 60) < courier.pick_rows(60, 200) <= max(SIZES)
    snap = wb.snapshot(100.0)
    assert snap["state"] == "completed" and snap["at_door"] and snap["fill"] == 0.0, snap
    assert "⌂" in roll("failed", 0.3) and not wb.text(clock[0]).count("▆")
    assert len(wb.text(clock[0]).replace(" zZ", "")) <= 16      # about as wide as "Thinking…" plus ends
    # The full-resolution courier (sixel): every baked size encodes to valid images that fit the lane.
    from forgecli import courier_hd
    from forgecli.courier_hd_data import SIZES as HD_SIZES
    for rows in HD_SIZES:
        items, key = courier_hd.lane_images(wb, 100.0, rows)
        g = courier_hd.geometry(rows)
        for col, img in items:
            assert img.startswith(b"P0;1;0q") and img.endswith(b"\\"), rows
            w, h = map(int, img.split(b'"1;1;', 1)[1].split(b"#", 1)[0].split(b";"))
            assert h <= rows * g["ch"] and col * g["cw"] + w <= g["lane_c"] * g["cw"], (rows, col, w, h)
    big = courier_hd.courier_image(max(HD_SIZES), 0, False, 4, False, False)
    assert big[1] >= 360 and big[2] >= 300      # the GIF's own scale, not a thumbnail
    assert courier_hd.pick_rows(24, 120) < courier_hd.pick_rows(70, 240) <= max(HD_SIZES)
    os.environ["FORGE_COURIER_HD"] = "off"
    assert not courier_hd.enabled()
    os.environ["FORGE_COURIER_HD"] = "on"
    assert courier_hd.enabled()
    del os.environ["FORGE_COURIER_HD"]
    print("ok  wheelbarrow follows thinking, tool runs, waits, done and failure")

    old_hooks_path = hooksmod.HOOK_CONFIG
    old_memory_home = memorymod.HOME_DIR
    old_session_dir = sessions.SESSION_DIR
    old_cwd = pathlib.Path.cwd()
    original_denials = {key: _os.environ.get(key) for key in ("FORGE_DENY_TOOLS", "FORGE_DENY_COMMANDS")}
    try:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            hooks_path = root / "hooks.json"
            hook_script = root / "block.py"
            hook_script.write_text("import sys\nsys.stdin.read()\nsys.exit(1)\n", encoding="utf-8")
            hooks_path.write_text(json.dumps({"pre_tool": [[sys.executable, str(hook_script)]]}),
                                  encoding="utf-8")
            hooksmod.HOOK_CONFIG = hooks_path
            blocked_tools = Tools(UI(), "powershell", auto_approve=True)
            target = root / "blocked.txt"
            blocked = blocked_tools.execute("write_file", json.dumps({
                "path": str(target), "content": "should not exist"}))
            assert "pre_tool hook" in blocked and not target.exists()

            hooks_path.write_text(json.dumps({"pre_tool": []}), encoding="utf-8")
            _os.environ["FORGE_DENY_TOOLS"] = "write_file"
            denied_tools = Tools(UI(), "powershell", auto_approve=True)
            denied = denied_tools.execute("write_file", json.dumps({
                "path": str(target), "content": "should not exist"}))
            assert "FORGE_DENY_TOOLS" in denied and not target.exists()
            _os.environ.pop("FORGE_DENY_TOOLS", None)
            _os.environ["FORGE_DENY_COMMANDS"] = "blocked-command"
            denied_command = denied_tools.execute("run_shell", json.dumps({"command": "blocked-command"}))
            assert "FORGE_DENY_COMMANDS" in denied_command
            _os.environ.pop("FORGE_DENY_COMMANDS", None)

            memorymod.HOME_DIR = root / "forge-home"
            memory_tools = Tools(UI(), "powershell")
            saved_memory = memory_tools.t_memory("remember", "project", "Prefer concise test reports")
            assert "Saved project memory" in saved_memory
            next_session_notes = memorymod.read("project", str(pathlib.Path.cwd()))
            from forgecli.agent import system_prompt
            assert "Prefer concise test reports" in system_prompt(Settings(), {}, next_session_notes)
            assert "/memory" in COMMANDS

            sessions.SESSION_DIR = root / "sessions"
            usage_session = sessions.Session(messages=[{"role": "user", "content": "usage check"}],
                                             usage_in=123, usage_out=45)
            usage_session.save()
            resumed_usage = sessions.Session.load(usage_session.id)
            assert (resumed_usage.usage_in, resumed_usage.usage_out) == (123, 45)

            os.chdir(root)
            command_dir = root / ".forge" / "commands"
            command_dir.mkdir(parents=True)
            (command_dir / "check.md").write_text("Check: $ARGUMENTS", encoding="utf-8")
            app = App.__new__(App)
            app.ui = UI()
            command_calls = []
            app.run = lambda text, echo="": command_calls.append((text, echo))
            assert app._project_command("check", "the parser")
            assert command_calls == [("Check: the parser", "command: check")]
            assert app._project_command("../outside", "ignored") is False

            browser_tools = Tools(UI(), "powershell")
            assert "only accepts localhost" in browser_tools.t_browser_check("https://example.com")
            assert "only accepts localhost" in browser_tools.t_browser_check(
                "http://user:password@localhost:8000")
            os.chdir(old_cwd)
        print("ok  hooks and deny policies block writes, memory persists, and project commands stay scoped")
    finally:
        hooksmod.HOOK_CONFIG = old_hooks_path
        memorymod.HOME_DIR = old_memory_home
        sessions.SESSION_DIR = old_session_dir
        os.chdir(old_cwd)
        for key, value in original_denials.items():
            if value is None:
                _os.environ.pop(key, None)
            else:
                _os.environ[key] = value
    print("ok  session token totals persist across resume for /cost")

    # --- the message box stays open while a task runs (docked mode) ---
    import threading as _th
    import time as _t
    from forgecli.cli import App as _App
    dock_ui = UI()
    dock_ui.docked = True
    dock_ui.start_spinner("thinking")
    assert dock_ui.busy and dock_ui._status is None          # no rich live display fighting the box
    assert "Thinking" in __import__("re").sub(r"\x1b\[[0-9;]*m", "", dock_ui.status_ansi())
    dock_ui.stop_spinner()
    assert dock_ui.status_ansi() == ""
    got = {}
    asker = _th.Thread(target=lambda: got.update(c=dock_ui.confirm("Run", "$ ls")))
    asker.start()
    for _ in range(50):
        if dock_ui.pending:
            break
        _t.sleep(0.02)
    assert dock_ui.pending and "Allow?" in dock_ui.pending["label"]
    assert dock_ui.answer("a")
    asker.join(2)
    assert got["c"] == ("a", "")
    assert not dock_ui.answer("stray")                       # nothing is waiting any more

    class _InboxAgent:
        def __init__(self):
            self.inbox, self.turns = [], []
        def run_turn(self, text):
            self.turns.append(text)
            if len(self.turns) == 1:
                self.inbox.append("also add tests")          # typed while the first turn ran
            if text == "forever":
                while True:
                    _t.sleep(0.02)
    dock_app = object.__new__(_App)
    dock_app.ui, dock_app.agent = dock_ui, _InboxAgent()
    dock_app.run("build it")
    dock_app.worker.join(3) if dock_app.worker else None
    assert dock_app.agent.turns == ["build it", "also add tests"]
    assert not dock_app.busy()
    dock_app.run("forever")
    _t.sleep(0.1)
    assert dock_app.busy()
    dock_app.stop()                                            # esc / ctrl+c while it works
    for _ in range(100):
        if not dock_app.busy():
            break
        _t.sleep(0.02)
    assert not dock_app.busy()
    stop_ui = UI()
    stop_ui.docked = True
    stop_app = object.__new__(_App)
    stop_app.ui, stop_app.agent = stop_ui, _InboxAgent()
    stop_app.agent.turns = ["x"]
    stop_app.work(lambda: got.update(s=stop_ui.confirm("Run", "$ rm -rf build")))
    for _ in range(50):
        if stop_ui.pending:
            break
        _t.sleep(0.02)
    stop_app.stop()                                            # a stop never counts as "yes"
    for _ in range(100):
        if not stop_app.busy():
            break
        _t.sleep(0.02)
    assert got.get("s", ("n",))[0] == "n"
    agent_inbox = Agent(s, UI(), router=router)
    agent_inbox.inbox.append("use port 9000")
    agent_inbox._read_inbox()
    assert agent_inbox.messages[-1] == {"role": "user", "content": "use port 9000"} and not agent_inbox.inbox
    print("ok  the message box stays open while Forge works: answers, added messages and stop")

    # Lessons from watching a real run (astryx landing page, 2026-11-10).
    from forgecli import tools as toolsmod, procs as procsmod
    assert unixy_to_powershell('x | grep -B 5 "landing"') == 'x | Select-String -Context 5,0 "landing"'
    assert unixy_to_powershell("x | grep -A 3 id:") == "x | Select-String -Context 3 id:"
    assert unixy_to_powershell("x | grep -i -C 2 foo") == "x | Select-String -Context 2 foo"
    assert toolsmod._LONG_COMMAND.search("pnpm install") and toolsmod._LONG_COMMAND.search("git clone u")
    assert toolsmod._LONG_COMMAND.search("pip install flask") and not toolsmod._LONG_COMMAND.search("ls")

    class _Srv:
        id, command = 18, "python -m http.server 8080"
        def urls(self): return ["http://[::]:8080/"]
        def output(self, n=0): return "Serving"
        def state(self): return "running"
    tl = agent_inbox.tools
    tl.opened.add("http://localhost:8080/landing-page.html")
    real_get, real_kill, real_forget = procsmod.get, procsmod.kill, procsmod.forget
    killed = []
    procsmod.get, procsmod.kill, procsmod.forget = (lambda i: _Srv()), killed.append, (lambda p: None)
    try:
        first = tl.t_process("stop", 18)
        assert first.startswith("Not stopped") and not killed      # page the user is looking at
        assert tl.t_process("stop", 18).startswith("Stopped") and killed   # second ask goes through
    finally:
        procsmod.get, procsmod.kill, procsmod.forget = real_get, real_kill, real_forget

    real_http = toolsmod.http_get
    toolsmod.http_get = lambda url: (200, "text/html", "<html><body><div id=root></div>"
                                     "<script type=module src=app.js></script></body></html>")
    try:
        assert "built by JavaScript" in tl.t_web_fetch("http://localhost:8080/")
    finally:
        toolsmod.http_get = real_http
    print("ok  mentor lessons: grep context on PowerShell, long installs, keep the user's server, JS pages")

    # Run 1 follow-up: JSX in a no-build page, Claude-only skills, screenshots Forge couldn't see.
    import shutil
    from forgecli import images as imagesmod
    from forgecli.agent import _CLAUDE_ONLY
    with tempfile.TemporaryDirectory() as d:
        dp = pathlib.Path(d)
        if shutil.which("node"):
            t = Tools(UI(), "powershell")
            bad = dp / "page.html"
            bad.write_text('<html><body><div id="root"></div>\n<script type="module">\n'
                           'const App = () => <div>hi</div>;\n</script></body></html>', encoding="utf-8")
            note = t._syntax_note(bad)
            assert "does not parse" in note and "line 3" in note and "htm" in note, note
            good = dp / "ok.html"
            good.write_text('<script type="module">\nconst x = 1;\n</script>\n'
                            '<script type="text/babel">const y = <b/>;</script>', encoding="utf-8")
            assert t._syntax_note(good) == "", t._syntax_note(good)
        assert _CLAUDE_ONLY.search("Automates your Chrome browser... Claude in Chrome extension")
        assert not _CLAUDE_ONLY.search("Build or change a website, landing page or web app")

        png = dp / "Screenshot (1).png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        msg = imagesmod.user_message(f"why is it stuck {png} ?", dp)
        assert msg["images"] == [str(png.resolve())], msg
        assert imagesmod.user_message("fix logo.png please", dp) == {"role": "user", "content": "fix logo.png please"}
        hist = [{"role": "system", "content": "s"}, msg]
        sent = imagesmod.parts(hist, "nvidia/nemotron-3.5-super-vl-120b-a12b")[-1]["content"]
        assert isinstance(sent, list) and sent[1]["image_url"]["url"].startswith("data:image/png;base64,")
        blind = imagesmod.parts(hist, "openai/gpt-oss-120b")[-1]["content"]
        assert isinstance(blind, str) and "can't see images" in blind
        assert hist[-1]["content"] == f"why is it stuck {png} ?"         # history itself unchanged
        assert portable(imagesmod.parts(hist, "x"))[-1].get("images") is None

        # Attachments: pasted paths (even glued to "continue") reach the model as inputs, with
        # document text extracted; the pinned brief keeps requests and answers through trimming.
        import zipfile
        from forgecli import attach as attachmod, brief as briefmod
        from forgecli.tools import unixy_to_powershell
        cv = dp / "My CV.docx"
        with zipfile.ZipFile(cv, "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>HARRY '
                       'SIGEI</w:t></w:r></w:p><w:p><w:r><w:t>Business IT student</w:t></w:r></w:p>'
                       '</w:body></w:document>')
        clip = dp / "cosmos_1.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        m = attachmod.user_message(f"{clip}continue", dp)
        assert m.get("attachments") == [str(clip.resolve())], m
        assert "<video" in m["content"] and "inputs to use, not questions" in m["content"], m["content"]
        assert briefmod.is_continuation(m["content"]) and not briefmod.is_continuation("build a site")
        m2 = attachmod.user_message(f'use "{cv}" for the about section', dp)
        assert "HARRY SIGEI" in m2["content"] and "Business IT student" in m2["content"], m2["content"]
        assert "HARRY SIGEI" in t.t_read_file(str(cv))                    # docx read as text, not zip bytes
        assert "binary video" in t.t_read_file(str(clip))
        assert attachmod.strip_paths(f"{clip}continue", dp) == "continue"

        convo = [{"role": "system", "content": "s"},
                 {"role": "user", "content": "build my portfolio site"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": "q1", "type": "function",
                  "function": {"name": "ask_user", "arguments": '{"question": "Which Hallmark theme resonates most?"}'}}]},
                 {"role": "tool", "tool_call_id": "q1", "content": "The user answered: Bloom"},
                 m]
        pinned = briefmod.build(convo)
        assert "Current request: build my portfolio site" in pinned and "A: Bloom" in pinned, pinned
        assert str(clip.resolve()) in pinned and "don't interview again" in pinned, pinned
        assert "Bloom" in briefmod.reuse(convo, "Which visual style appeals to you?")
        assert briefmod.reuse(convo, "Where should the contact form send messages?") is None
        many = convo + [{"role": "assistant", "content": "", "tool_calls": [
            {"id": f"x{i}", "function": {"name": "ask_user", "arguments": '{"question": "q"}'}}
            for i in range(briefmod.MAX_QUESTIONS)]}]
        assert "Stop interviewing" in briefmod.reuse(many, "Where should the contact form send messages?")
        assert unixy_to_powershell('cd "C:\\My App" && npm i') == 'cd "C:\\My App"; if ($?) { npm i }'
        assert unixy_to_powershell('echo "a && b"') == 'echo "a && b"'
        if os.name == "nt":
            assert unixy_to_powershell("python3 x.py && pip install docx") == \
                "python x.py; if ($?) { python -m pip install docx }"
        print("ok  attachments + docx text, pinned brief, no repeat questions, quoted && / python3 on Windows")

        # The router puts a model that can see images first when the newest message has one.
        tried = []
        r = Router(Settings(providers=[ProviderConfig("a", "http://x", "openai/gpt-oss-120b", ["k"]),
                                       ProviderConfig("b", "http://y", "plain-model", ["k"],
                                                      models=["vendor/thing-vl-7b"])]),
                   CooldownStore(dp / "cd.json"))
        for p in r.providers:
            p.call = lambda key, m, tools, model=None, on_text=None, _p=p: tried.append(model or _p.model) or None
        r.chain_once(hist, [])
        assert tried and tried[0] == "vendor/thing-vl-7b", tried
    print("ok  JS syntax check on writes, Claude-only skills hidden, images reach a model that can see them")

    # Run 3: a cloned skill repo is flagged so the model reads its SKILL.md instead of copying demos.
    with tempfile.TemporaryDirectory() as d:
        repo = pathlib.Path(d)
        (repo / "skills" / "hallmark").mkdir(parents=True)
        (repo / "skills" / "hallmark" / "SKILL.md").write_text("---\nname: hallmark\n---\n", encoding="utf-8")
        (repo / "site").mkdir()
        lt = Tools(UI(), "powershell")
        listing = lt.execute("list_dir", json.dumps({"path": str(repo)}))
        assert "agent skill" in listing and "SKILL.md" in listing, listing
        plain = lt.execute("list_dir", json.dumps({"path": str(repo / "site")}))
        assert "agent skill" not in plain, plain
    print("ok  skill repos point to their SKILL.md")
    from forgecli.tools import local_only_server
    assert local_only_server(r"python -m http.server 8000 --directory C:\Users\Harry") == \
        r"python -m http.server --bind 127.0.0.1 8000 --directory C:\Users\Harry"
    assert local_only_server("python -m http.server -b 0.0.0.0 80").endswith("-b 0.0.0.0 80")
    print("ok  preview servers only listen on this machine")

    # Quality routing: the most capable model goes first (not the fastest provider), the task keeps
    # its model across steps, and a model taking over mid-task is told so with the open checklist.
    from forgecli import providers as provmod
    assert provmod.quality("openai/gpt-oss-120b") > provmod.quality("gpt-oss:20b")
    assert provmod.quality("nvidia/nemotron-3-ultra-550b-a55b:free") > provmod.quality("openai/gpt-oss-120b")
    assert provmod.quality("nvidia/nemotron-3.5-bolt-e4b") < provmod.quality("kilo-auto/free")
    with tempfile.TemporaryDirectory() as d:
        qs = Settings(retry_rounds=0, providers=[
            ProviderConfig("fast", "http://x", "gpt-oss:20b", ["k"]),
            ProviderConfig("mid", "http://y", "openai/gpt-oss-120b", ["k"]),
            ProviderConfig("big", "http://z", "nvidia/nemotron-3-ultra-550b-a55b:free", ["k"])])
        qr = Router(qs, lambda *a: None, CooldownStore(pathlib.Path(d) / "q.json"))
        calls, down, seen_sys = [], set(), []

        def fake(_p):
            def call(key, messages, tools, model=None, on_text=None):
                m = model or _p.model
                calls.append(m)
                seen_sys.append(messages[0]["content"])
                if m in down:
                    _p.last_error = "HTTP 500 boom"
                    return None
                return {"provider": _p.name, "model": m, "content": "ok", "tool_calls": [],
                        "truncated": False, "usage": {}}
            return call
        for p in qr.providers:
            p.call = fake(p)
        conv = [{"role": "system", "content": "sys"}, {"role": "user", "content": "build it"}]
        r = qr.complete(conv, [])
        assert r["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free", calls
        qr.complete(conv, [])
        assert calls[-1] == "nvidia/nemotron-3-ultra-550b-a55b:free"      # same model next step
        qr.todo = lambda: ["write the footer"]
        down.add("nvidia/nemotron-3-ultra-550b-a55b:free")
        r = qr.complete(conv, [])
        assert r["model"] == "openai/gpt-oss-120b", calls                  # next-best, not fastest
        assert "taking over this task" in seen_sys[-1] and "write the footer" in seen_sys[-1]
        assert conv[0]["content"] == "sys"                                 # history untouched
        speed = Router(Settings(routing="speed", providers=qs.providers), lambda *a: None,
                       CooldownStore(pathlib.Path(d) / "s.json"))
        assert not speed.by_quality
    print("ok  quality routing: most capable model first, kept for the task, handoff note on switch")

    # Automatic scores: from the name, then the quick test, then the track record; overrides win.
    g = qualitymod.guess
    assert g("minimaxai/minimax-m3.1-flash") >= 8 and g("gemini-2.5-pro") == 10      # "mini" inside a word
    assert g("gpt-4o-mini") < g("gpt-4o") and g("llama-3.3-70b-versatile") == 7
    assert g("brand-new-model") == 6 and g("vendor/tiny-3b") < 4
    qualitymod.STATS_FILE.write_text(json.dumps({
        "p:vendor/model-70b": {"up": True, "tools": False, "check": True, "when": time.time()},
        "q:vendor/model-70b": {"up": True, "tools": True, "check": True, "when": time.time()}}), encoding="utf-8")
    qualitymod._cache["at"] = 0.0
    assert qualitymod.score("vendor/model-70b", "p") == 4      # can't call tools: big drop
    assert qualitymod.score("vendor/model-70b", "q") == 7
    for _ in range(10):
        qualitymod.record("q", "vendor/model-70b", False)
    assert qualitymod.score("vendor/model-70b", "q") == 5      # keeps failing in real work
    for _ in range(40):
        qualitymod.record("q", "vendor/model-70b", True)
    assert qualitymod.score("vendor/model-70b", "q") == 8      # reliably finishes work
    assert "record +1" in qualitymod.explain("vendor/model-70b", "q")
    assert json.loads(qualitymod.RECORD_FILE.read_text(encoding="utf-8"))["q:vendor/model-70b"]
    qualitymod.OVERRIDES_FILE.write_text(json.dumps({"vendor/model-70b": 2}), encoding="utf-8")
    qualitymod._cache["at"] = 0.0
    assert qualitymod.score("vendor/model-70b", "q") == 2
    assert qualitymod.explain("vendor/model-70b", "q") == "set in model_quality.json"
    qualitymod.OVERRIDES_FILE.unlink()
    qualitymod._cache["at"] = 0.0
    print("ok  model scores are worked out automatically: name, quick test, track record")

    # Short "build me X" requests get interview-me first, so Forge asks before inventing content.
    from forgecli.agent import _vague_build, _INTERVIEW_NOTE
    for t in ["build me a portfolio website using this skill https://github.com/Nutlope/hallmark.git",
              "build alanding page for forge now", "just build me a simple landing page"]:
        assert _vague_build(t), t
    for t in ["fix the login bug in app.py", "make sure the app starts", "make the tests pass",
              "explain how the router works",
              "build a dashboard for my shop that shows daily sales from orders.csv grouped by product with "
              "a dark theme, a date filter, a chart of the last 30 days and an export button for the accountant"]:
        assert not _vague_build(t), t
    assert _INTERVIEW_NOTE.startswith("\n\n[Forge:") and "ask_user" in _INTERVIEW_NOTE
    print("ok  vague build requests are recognised")

    # Test 59b: asking is opt-in — cheap questions get the model's own default, exits are never answers.
    from forgecli import brief as _b
    from forgecli.agent import _DEFAULTS_NOTE, wants_questions
    def _u(t): return [{"role": "user", "content": t}]
    assert not wants_questions("just build me a simple landing page") and wants_questions("help me plan it, ask me")
    assert _b.gate(_u("build me a landing page"), {"question": "Colours?", "default": "dark", "stakes": "preference"}).startswith("Not asked")
    assert _b.gate(_u("build me a landing page"), {"question": "For what?"}).startswith("Not asked")        # no stakes given
    assert _b.gate(_u("just make a simple page"), {"question": "Who for?", "stakes": "costly_rework"}).startswith("Not asked")
    assert _b.gate(_u("tidy my folder"), {"question": "Delete 40 files?", "default": "no", "stakes": "irreversible"}) is None
    assert _b.gate(_u("deploy it"), {"question": "Account id?", "stakes": "personal_fact"}) is None
    assert _b.gate(_u("interview me about the page"), {"question": "Colours?", "stakes": "preference"}) is None
    assert "without asking" in _DEFAULTS_NOTE
    print("ok  only costly, irreversible or personal questions reach the user")


    # Test 60: sponsor lines — off switches, escape-code cleaning, signatures that can't be forged.
    import os as _os
    from forgecli import ads as adsmod
    seed = bytes(range(32))
    pub = adsmod.public_from_seed(seed).hex()
    ad = {"id": "a1", "text": "Try \x1b[2J\x1b]8;;https://evil\x07Acme\x1b]8;;\x07 ‮db‬ now", "url": "https://acme.dev/x",
          "exp": int(time.time()) + 3600}
    ad["sig"] = adsmod.sign(seed, adsmod.signed_message(ad)).hex()
    good = adsmod.check(ad, pub)
    assert good and good["text"] == "Try Acme db now" and good["url"] == "https://acme.dev/x", good
    assert "\x1b" not in good["text"] and "2J" not in good["text"] and "evil" not in good["text"]
    assert adsmod.check(dict(ad, text="Free money"), pub) is None                 # tampered text
    assert adsmod.check(dict(ad, sig="00" * 64), pub) is None                      # bad signature
    assert adsmod.check(ad, adsmod.public_from_seed(bytes(32)).hex()) is None      # someone else's key
    assert adsmod.check(ad, pub, now=time.time() + 7200) is None                   # expired
    assert adsmod.clean_url("javascript:alert(1)") == "" and adsmod.clean_url("https://xn--pple-43d.com") == ""
    assert len(adsmod.clean_text("x" * 500)) <= adsmod.MAX_TEXT
    _saved = {k: _os.environ.get(k) for k in ("FORGE_ADS", "FORGE_ADS_URL", "FORGE_ADS_KEY", "CI", "GITHUB_ACTIONS")}
    try:
        for k in _saved:
            _os.environ.pop(k, None)
        old = {"id": "x", "since": time.time() - 30 * 86400}
        _real = (adsmod.AD_SERVER, adsmod.AD_PUBLIC_KEY)
        adsmod.AD_SERVER, adsmod.AD_PUBLIC_KEY = "", ""
        assert adsmod.allowed(old, True) == (False, "no ad server configured")
        adsmod.AD_SERVER, adsmod.AD_PUBLIC_KEY = _real
        _os.environ["FORGE_ADS_URL"], _os.environ["FORGE_ADS_KEY"] = "https://ads.example", pub
        assert adsmod.allowed(old, True) == (True, "")
        assert not adsmod.allowed(old, False)[0]                                       # pipes, scripts, -p
        assert not adsmod.allowed({"id": "x", "since": time.time()}, True)[0]          # first week quiet
        assert not adsmod.allowed(dict(old, off=True), True)[0]                         # /ads off
        _os.environ["FORGE_ADS"] = "0"
        assert not adsmod.allowed(old, True)[0]
        _os.environ.pop("FORGE_ADS"); _os.environ["CI"] = "true"
        assert not adsmod.allowed(old, True)[0]
    finally:
        for k, v in _saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
    print("ok  sponsor lines: off switches, cleaned of escape codes, signature checked")

    # Test 61: /cost turns tokens into money; free tiers cost nothing; unknown prices say so.
    from forgecli import prices as pricesmod
    assert pricesmod.price("groq", "openai/gpt-oss-120b") == (0.0, 0.0)
    assert pricesmod.price("openrouter", "x/y:free") == (0.0, 0.0)
    assert pricesmod.price("openai", "gpt-4o-mini") == (0.15, 0.6)
    assert pricesmod.price("acme", "mystery-model") is None
    lines, total, unknown = pricesmod.summary({"openai:gpt-4o-mini": (1_000_000, 100_000),
                                               "acme:mystery-model": (10, 10)})
    assert abs(total - 0.21) < 1e-9 and unknown and any("price unknown" in l for l in lines)
    print("ok  /cost shows dollars per model")

    # Test 62: Python and JSON files are syntax-checked right after Forge writes them.
    from forgecli.tools import Tools as _T
    _tt = _T.__new__(_T)
    bad_py, bad_json, ok_py = tmp / "bad.py", tmp / "bad.json", tmp / "ok.py"
    bad_py.write_text("def f(:\n    pass\n", encoding="utf-8")
    bad_json.write_text('{"a": 1,}', encoding="utf-8")
    ok_py.write_text("x = 1\n", encoding="utf-8")
    assert "bad.py" in _tt._syntax_note(bad_py) or "syntax" in _tt._syntax_note(bad_py).lower()
    assert _tt._syntax_note(bad_json)
    assert not _tt._syntax_note(ok_py)
    print("ok  .py and .json files are parse-checked after edits")

    # Test 63: Docker sandbox command line — project mounted, nothing outside it, ports local only.
    from forgecli import sandbox as sandboxmod
    _sb = {k: _os.environ.get(k) for k in ("FORGE_SANDBOX_NET", "FORGE_SANDBOX_PORTS", "FORGE_SANDBOX_IMAGE")}
    try:
        for k in _sb:
            _os.environ.pop(k, None)
        (tmp / "proj" / "sub").mkdir(parents=True, exist_ok=True)
        cmd = sandboxmod.argv(tmp / "proj", tmp / "proj" / "sub", "npm test")
        assert isinstance(cmd, list) and cmd[:3] == ["docker", "run", "--rm"]
        assert f"{(tmp / 'proj').resolve()}:/work" in cmd and cmd[cmd.index("-w") + 1] == "/work/sub"
        assert cmd[-3:] == ["sh", "-lc", "npm test"] and "no-new-privileges" in cmd
        assert all(c.startswith("127.0.0.1:") for c, prev in zip(cmd, [""] + cmd) if prev == "-p")
        assert isinstance(sandboxmod.argv(tmp / "proj", tmp, "ls"), str)          # outside: refused
        _os.environ["FORGE_SANDBOX_NET"] = "none"
        cmd = sandboxmod.argv(tmp / "proj", tmp / "proj", "ls")
        assert "--network" in cmd and "-p" not in cmd
    finally:
        for k, v in _sb.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
    print("ok  docker sandbox only sees the project folder")

    # Test 64: parallel agents in git worktrees — each on its own branch, main checkout untouched.
    import shutil as _sh, subprocess as _sp
    from forgecli import worktrees as wtmod
    if _sh.which("git"):
        repo = tmp / "wtrepo"
        repo.mkdir()
        g = lambda *a: _sp.run(["git", *a], cwd=repo, capture_output=True, text=True)
        g("init", "-q"); (repo / "a.txt").write_text("a\n", encoding="utf-8"); g("add", "-A")
        g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        assert wtmod.repo_root(repo) is not None
        p1, b1 = wtmod.create(repo, 1, "t")
        p2, b2 = wtmod.create(repo, 2, "t")
        (p1 / "b.txt").write_text("from agent 1\n", encoding="utf-8")
        assert not (repo / "b.txt").exists() and not (p2 / "b.txt").exists()
        assert "committed on branch" in wtmod.finish(repo, p1, b1, "add b")
        assert "no file changes" in wtmod.finish(repo, p2, b2, "nothing")
        branches = g("branch", "--list").stdout
        assert b1 in branches and b2 not in branches and not p1.exists()
        assert g("status", "--porcelain").stdout.strip() == ""
        print("ok  parallel agents work in separate git worktrees")
    else:
        print("skip worktrees (git not installed)")

    # Test 65: GitHub device-flow sign-in waits politely and reports refusals in plain words.
    from forgecli import ghlogin
    replies = iter([{"error": "authorization_pending"}, {"error": "slow_down", "interval": 7},
                    {"access_token": "gho_test"}])
    waits = []
    tok = ghlogin.poll("cid", {"device_code": "d", "interval": 5, "expires_in": 900},
                       post=lambda url, f: next(replies), sleep=waits.append)
    assert tok == "gho_test" and waits == [5, 5, 7], waits
    try:
        ghlogin.poll("cid", {"device_code": "d", "interval": 1}, post=lambda u, f: {"error": "access_denied"},
                     sleep=lambda s: None)
        raise AssertionError("expected refusal")
    except RuntimeError as e:
        assert "declined" in str(e)
    try:
        ghlogin.start("cid", post=lambda u, f: {"error": "unauthorized_client",
                                                "error_description": "device flow disabled"})
        raise AssertionError("expected error")
    except RuntimeError as e:
        assert "device flow disabled" in str(e)
    print("ok  /github login device flow")

    # Test 66: browser_check screenshots land in Forge's folder, never the project.
    from forgecli import browsercheck as bcmod
    _fh = _os.environ.get("FORGE_HOME")
    _os.environ["FORGE_HOME"] = str(tmp / "fhome")
    try:
        shot = bcmod.shot_path(tmp / "proj")
        assert (tmp / "fhome" / "screenshots") in shot.parents and shot.suffix == ".png"
    finally:
        if _fh is None:
            _os.environ.pop("FORGE_HOME", None)
        else:
            _os.environ["FORGE_HOME"] = _fh
    print("ok  browser_check screenshots stay out of the project")

    # A follow-up stays on the latest work; links are resources; paths don't pick skills.
    from forgecli import attach as _at, brief as _br, skills as _sk
    _w = lambda i, p: {"role": "assistant", "content": "", "tool_calls": [  # noqa: E731
        {"id": i, "function": {"name": "write_file", "arguments": json.dumps({"path": p, "content": "x"})}}]}
    _req = r"C:\Users\Harry\Downloads\greeter-tiger.png this should be the logo and add https://shadergradient.co/"
    _m = _at.user_message(_req)
    assert "the user gave a link: https://shadergradient.co/" in _m["content"]
    assert "Don't just paste the URL" in _m["content"] and _br.own_text(_m["content"]) == _req
    _convo = [{"role": "user", "content": "build me a simple styled calculator like the ios one"},
              _w("1", "ios-calculator.html"),
              {"role": "user", "content": "build a website instead not a calculator"},
              _w("2", "website/index.html"), _m]
    assert _br.recent_files(_convo) == ["website/index.html"]
    _pin = _br.build(_convo)
    assert "Latest work (newest first): website/index.html." in _pin and "never go back to it" in _pin
    _words = _sk.request_words(_req)
    assert "Users" not in _words and "shadergradient" not in _words and "png" in _words
    assert "pdf" in _sk.request_words("summarize ./docs/report.pdf for me")
    print("ok  follow-ups stay on the latest work, links are resources, paths don't pick skills")

    # The start screen: tiger left, FORGE + arrow details right, city behind; small windows fall back.
    import io as _io
    from rich.console import Console as _C
    from rich.text import Text as _T
    from forgecli import splash as _sp
    _lines = [_T("v0"), *[_T(_sp.ARROW + f"model     some/model-{i}") for i in range(5)], _T("/help commands")]
    for _wd in (90, 128, 200):
        _buf = _io.StringIO()
        _con = _C(file=_buf, width=_wd, force_terminal=True, color_system="truecolor")
        assert _sp.render(_con, _lines), _wd
        _rows = _buf.getvalue().rstrip(chr(10)).split(chr(10))
        assert any("model-4" in _T.from_ansi(r).plain for r in _rows)
        assert all(_T.from_ansi(r).cell_len <= _wd for r in _rows), _wd
    assert not _sp.render(_C(file=_io.StringIO(), width=60, force_terminal=True), _lines)
    print("ok  start screen: tiger, FORGE and arrow details over the city; tiny windows fall back")

    # Loading screen, sponsor line and the HD start picture.
    from forgecli import loader as loadmod, splash as splmod, splash_hd as hdmod, ui as uimod2
    fr = loadmod.frame(1.0, 80, 24)
    assert "\n" not in fr and "█" in fr and "S C A L E" in fr      # rows placed by cursor moves only
    st = uimod2.sponsor_text("sponsor · Ship faster  https://example.com")
    assert "sponsored" in st.plain and st.plain.endswith("https://example.com") and st.no_wrap
    Lh = splmod.layout(90, 8, 60)
    pic = hdmod.draw(Lh, (Lh["rx"] - 2, 5, 80, 14))
    assert (pic.w, pic.h) == (Lh["w"] * 10, Lh["h"] * 20)
    print("ok  loading screen, sponsor tag and the sharp start picture")

    # The kvlite session: same reads and test runs over and over, bash syntax in PowerShell,
    # a false "deletes files" prompt on an inline script, and a leaked <channel|> token.
    from forgecli.agent import Agent as _Ag
    from forgecli.tools import bashism
    from forgecli.safety import shell_risk
    from forgecli import ui as _ui
    ag = _Ag.__new__(_Ag)
    rd = [{"function": {"name": "run_shell", "arguments": '{"command": "cat store.py"}'}}]
    acts = [ag._repeats(rd) for _ in range(5)]
    assert acts[2] == "nudge" and acts[4] == "stop", acts
    ag._repeats([{"function": {"name": "edit_file", "arguments": "{}"}}])
    assert ag._repeats(rd) == "", "an edit resets the repeat count"
    assert _Ag._readish(ag, rd[0]) and not _Ag._readish(ag, {"function": {"name": "run_shell", "arguments": '{"command": "pytest"}'}})
    assert bashism("python << 'EOF'" + chr(10) + "print(1)" + chr(10) + "EOF") and bashism("mkdir -p a/{x,y}") and not bashism("git status")
    assert shell_risk('python -c "import os; del x"') == "" and shell_risk("rm -rf build")
    assert _ui._strip_think("done<channel|> ok") == "done ok"
    print("ok  repeat loop guard, PowerShell bash-isms, inline-script approval, leaked tokens")

    # A missed edit shows the real text; a stuck model is set aside for the next one.
    from forgecli.tools import _closest
    hint = _closest("a = 1\ndef set(self, k, v):\n    self.data[k] = v\n", "def set(self, key, v):\n    self.data[key] = v")
    assert "lines 2-3" in hint and "self.data[k] = v" in hint, hint
    assert "read the file again" in _closest("x = 1\n", "totally different text here")
    import time as _t
    class _R:
        pinned, sticky, model_down, log = False, ("groq", "m1"), {}, staticmethod(lambda *a: None)
    who = Router.escalate(_R)
    assert who == "groq · m1" and _R.model_down[("groq", "m1")] > _t.time() + 60
    _R.pinned = True
    assert Router.escalate(_R) == ""
    print("ok  edit misses show the closest text, stuck model handed on")

    # The kvlite loop: slices of one unchanged file, pytest with extra cwd/cd, across 'continue'.
    import json as _j, tempfile as _tf
    def _c(name, **a):
        return {"function": {"name": name, "arguments": _j.dumps(a)}}
    with _tf.TemporaryDirectory() as d:
        f = pathlib.Path(d, "protocol.py"); f.write_text("x = 1\n")
        class _K:
            _sig, _repeats, _failing = Agent._sig, Agent._repeats, Agent._failing
            _FAILING, FAIL_NUDGE, FAIL_STOP = Agent._FAILING, Agent.FAIL_NUDGE, Agent.FAIL_STOP
            REPEAT_NUDGE, REPEAT_STOP = Agent.REPEAT_NUDGE, Agent.REPEAT_STOP
            tools = type("T", (), {"_path": staticmethod(pathlib.Path)})()
        k = _K()
        assert k._sig(_c("read_file", path=str(f), offset=1)) == k._sig(_c("read_file", path=str(f), offset=200))
        assert (k._sig(_c("run_shell", command=f"cd {d} && pytest tests -v 2>&1", timeout=60))
                == k._sig(_c("run_shell", command="pytest tests -v", cwd=d)))
        k._repeats([_c("read_file", path=str(f))])
        acts = [k._repeats([_c("read_file", path=str(f), offset=i)]) for i in range(5)]
        assert "nudge" in acts and "stop" in acts, acts
        f.write_text("x = 2\n")   # the file changed: reading it again is fine
        k._repeat_steps = 0
        assert k._repeats([_c("read_file", path=str(f))]) == ""
        assert k._FAILING.search("==== 3 failed, 39 passed in 3.36s ====")
        k._since_fail = 0
        acts = [k._failing([_c("read_file", path="a")]) for _ in range(7)]
        assert acts[3] == "nudge" and acts[6] == "stop", acts
        k._failing([_c("edit_file", path="a")])
        assert k._since_fail is None
    print("ok  kvlite loop: slices, reworded test runs, failing tests with no fix")

    # 0.8.5: the error goes into the nudge, tests stay put while failing, repeat reads are refused.
    tb = ('Traceback (most recent call last):\n  File "kv\\server.py", line 300, in handle_client\n'
          '    x()\n  File "kv\\persistence.py", line 56, in write_increment\n    len(key)\n'
          "TypeError: object of type 'RespBulkString' has no len()\n")
    e = Agent._error_of(tb)
    assert "persistence.py\", line 56" in e and "RespBulkString" in e, e
    py = "tests\\test_kv.py:153: AssertionError\nE       AssertionError: assert -1 == -2\n==== 1 failed, 4 passed ===="
    assert "assert -1 == -2" in Agent._error_of(py) and "test_kv.py:153" in Agent._error_of(py)
    assert Agent._error_of("==== 42 passed in 3.1s ====") == ""
    with _tf.TemporaryDirectory() as d:
        t = pathlib.Path(d, "tests"); t.mkdir(); tf = t / "test_kv.py"; tf.write_text("def test(): pass\n")
        class _G:
            _test_guard, _TEST_FILE, _TEST_ASK = Agent._test_guard, Agent._TEST_FILE, Agent._TEST_ASK
            tools = type("T", (), {"_path": staticmethod(pathlib.Path), "last_error": "assert -1 == -2"})()
            messages = [{"role": "user", "content": "Build kvlite. Do not skip or weaken any test."}]
            _tests_failing = True
        g = _G()
        blocked = g._test_guard("edit_file", _j.dumps({"path": str(tf)}))
        assert blocked.startswith("Error: not changed") and "-1 == -2" in blocked, blocked
        assert g._test_guard("edit_file", _j.dumps({"path": str(pathlib.Path(d, "store.py"))})) == ""
        assert g._test_guard("write_file", _j.dumps({"path": str(t / "test_new.py")})) == ""
        g.messages.append({"role": "user", "content": "the decr test is wrong, fix the test"})
        assert g._test_guard("edit_file", _j.dumps({"path": str(tf)})) == ""
        g.messages.pop(); g._tests_failing = False
        assert g._test_guard("edit_file", _j.dumps({"path": str(tf)})) == ""
    print("ok  error in the nudge, tests kept while failing, repeat read refused")

    # 0.8.6: honest edit-miss wording, apply_patch hands over to edit_file, empty calls explained.
    with _tf.TemporaryDirectory() as d:
        from forgecli.tools import Tools as _T
        tt = _T(UI(), "powershell")
        f = pathlib.Path(d, "p.py"); f.write_text("def write(key):\n    return len(key)\n", encoding="utf-8")
        tt.execute("read_file", _j.dumps({"path": str(f)}))
        miss = tt.execute("edit_file", _j.dumps({"path": str(f), "old_string": "return len(keys)",
                                                  "new_string": "return len(str(key))"}))
        assert "has not changed since you read it" in miss, miss
        f.write_text("def write(key):\n    return len(key)  # x\n", encoding="utf-8")
        import os as _os; _os.utime(f, (1, 1))
        miss = tt.execute("edit_file", _j.dumps({"path": str(f), "old_string": "return len(keys)",
                                                  "new_string": "return len(str(key))"}))
        assert "has changed since you read it" in miss, miss
        bad = "*** Begin Patch\n*** Update File: " + str(f) + "\n@@\n-nothing here\n+x\n*** End Patch"
        tt.execute("apply_patch", _j.dumps({"patch": bad}))
        assert "Use edit_file" in tt.execute("apply_patch", _j.dumps({"patch": bad}))
        assert "no arguments" in tt.execute("edit_file", "{}")
    print("ok  edit-miss wording, apply_patch hands over to edit_file, empty calls explained")

    # 0.8.7: "done" needs the thing itself run, not only its unit tests (the kvlite false finish).
    with _tf.TemporaryDirectory() as d:
        srv = pathlib.Path(d, "server.py")
        srv.write_text("import asyncio\nasync def main():\n    await asyncio.start_server(h, '127.0.0.1', 6399)\n",
                       encoding="utf-8")
        L = guardsmod.Ledger()
        L.add("write_file", {"path": str(srv)}, "Wrote 3 lines")
        L.add("run_shell", {"command": "python -m pytest tests -q"}, "exit code 0\n42 passed in 0.8s")
        done = "Done. kvlite is built and all 42 tests pass."
        probs = guardsmod.check_claims(done, L, pathlib.Path(d))
        assert any("never ran what you built" in p for p in probs), probs
        L.add("run_shell", {"command": "python demo.py"},
              "exit code 1\nTraceback (most recent call last):\nConnectionRefusedError: [WinError 10061]")
        probs = guardsmod.check_claims(done, L, pathlib.Path(d))
        assert any("last time you ran it, it failed" in p and "ConnectionRefused" in p for p in probs), probs
        L.add("run_shell", {"command": "python demo.py"}, "exit code 0\nPING -> PONG\nSET/GET -> bar")
        assert not any("ran" in p for p in guardsmod.check_claims(done, L, pathlib.Path(d)))
        # Not a claim of done, or nothing runnable changed: no complaint.
        L2 = guardsmod.Ledger(); L2.add("write_file", {"path": str(srv)}, "Wrote 3 lines")
        assert not guardsmod.unrun_build("I haven't run it yet; start it with python server.py.", L2, pathlib.Path(d))
        lib = pathlib.Path(d, "util.py"); lib.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        L3 = guardsmod.Ledger(); L3.add("write_file", {"path": str(lib)}, "Wrote 2 lines")
        assert not guardsmod.unrun_build("Done.", L3, pathlib.Path(d))
    # unittest prints "Ran N tests in Xs" then "OK": that is a passing run (an eval caught this false bounce).
    assert guardsmod.test_succeeded(guardsmod.Step("run_shell", {"command": "python tests/test_kv.py"}, True,
                                                   "test_a ... ok\n----\nRan 26 tests in 0.004s\n\nOK\n"))
    print("ok  done check: the server/CLI/page must have been run, and not failed")

    # 0.8.7: code_graph is a built-in tool (graphify) with clear errors.
    from forgecli import codegraph as _cg
    assert any(s["function"]["name"] == "code_graph" for s in __import__("forgecli.tools", fromlist=["SCHEMAS"]).SCHEMAS)
    with _tf.TemporaryDirectory() as d:
        assert "needs a target" in _cg.answer(pathlib.Path(d), "explain")
        assert "action must be" in _cg.answer(pathlib.Path(d), "bogus")
        assert "no source files" in _cg.answer(pathlib.Path(d), "query", "x")
        if _cg.command():
            pathlib.Path(d, "app.py").write_text(
                "def load(p):\n    return open(p).read()\n\ndef main():\n    print(load('x'))\n", encoding="utf-8")
            out = _cg.answer(pathlib.Path(d), "affected", "load")
            assert "main" in out, out
            assert not _cg.stale(pathlib.Path(d))[0]
    print("ok  code_graph built in (graphify), refreshed when stale")

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
