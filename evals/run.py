"""Forge task suite: run real tasks end to end and measure how Forge does.

Each task in evals/tasks/<name>/ has:
  task.yaml   prompt, timeout, optional `protect` (globs that must not change) and `needs` (tools on PATH)
  seed/       optional starting files (an existing project to fix or extend)
  check.py    run in the finished workspace; exit 0 = the result really works for a user

For every task the runner copies seed/ to a fresh workspace, runs `forge -y --json "<prompt>"` there
(headless: no window, no browser, nothing on your desktop), then runs check.py and reads the saved
session to record what happened. Results go to evals/.runs/<stamp>/ as results.json and report.md.

    python evals/run.py                       # every task, 2 at a time
    python evals/run.py kv_server fix_bug     # only these
    python evals/run.py --jobs 1 --model groq:llama-3.3-70b-versatile
    python evals/run.py --compare             # also show the change against the previous run

A Forge change is an improvement when the pass count goes up (or holds) and steps / loops / errors
go down across the whole suite - not when one task happens to finish.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import fnmatch
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

def read_spec(text: str) -> dict:
    """The small YAML subset task.yaml uses (no PyYAML needed): `key: value`, `key: |` blocks,
    `key:` followed by `  - item` lists, and `key: [a, b]`."""
    spec, key, block, items = {}, None, None, None
    for line in text.splitlines():
        if block is not None:
            if line.startswith("  ") or not line.strip():
                block.append(line[2:])
                continue
            spec[key] = "\n".join(block).strip() + "\n"
            block = None
        if items is not None and line.lstrip().startswith("- "):
            items.append(line.split("- ", 1)[1].strip().strip('"'))
            continue
        items = None
        if ":" not in line or line.startswith(" "):
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val == "|":
            block = []
        elif val == "":
            items = spec[key] = []
        elif val.startswith("["):
            spec[key] = [v.strip().strip('"') for v in val.strip("[]").split(",") if v.strip()]
        else:
            spec[key] = int(val) if val.isdigit() else val.strip('"')
    if block is not None:
        spec[key] = "\n".join(block).strip() + "\n"
    return spec


HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
TASKS = HERE / "tasks"
RUNS = HERE / ".runs"
SESSIONS = pathlib.Path.home() / ".forge" / "sessions"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def load_tasks(names: list[str]) -> list[dict]:
    tasks = []
    for d in sorted(p for p in TASKS.iterdir() if (p / "task.yaml").exists()):
        if names and d.name not in names:
            continue
        spec = read_spec((d / "task.yaml").read_text(encoding="utf-8"))
        spec.update(name=d.name, dir=d)
        tasks.append(spec)
    missing = set(names) - {t["name"] for t in tasks}
    if missing:
        sys.exit("unknown task(s): " + ", ".join(sorted(missing)))
    return tasks


def _hashes(root: pathlib.Path, globs: list[str]) -> dict[str, str]:
    out = {}
    for f in root.rglob("*"):
        rel = f.relative_to(root).as_posix()
        if f.is_file() and any(fnmatch.fnmatch(rel, g) for g in globs):
            out[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def _env() -> dict:
    env = dict(os.environ, FORGE_FRESH_PAGE="off", FORGE_LOADER="off", FORGE_AUTO_OPEN="off",
               NO_COLOR="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1", FORGE_EVAL="1")
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def session_stats(session_id: str, protect: list[str], work: pathlib.Path) -> dict:
    f = SESSIONS / f"{session_id}.json"
    stats = {"steps": 0, "tool_errors": 0, "nudges": 0, "bounces": 0, "test_edits": 0, "models": [],
             "tools": {}}
    if not session_id or not f.exists():
        return stats
    data = json.loads(f.read_text(encoding="utf-8"))
    for m in data.get("messages", []):
        role, content = m.get("role"), str(m.get("content") or "")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                stats["steps"] += 1
                stats["tools"][name] = stats["tools"].get(name, 0) + 1
                if name in ("write_file", "edit_file") and protect:
                    try:
                        path = json.loads(fn.get("arguments") or "{}").get("path", "")
                    except ValueError:
                        path = ""
                    p = pathlib.Path(path)
                    rel = (p.relative_to(work) if p.is_absolute() and work in p.parents else p).as_posix()
                    if any(fnmatch.fnmatch(rel, g) for g in protect):
                        stats["test_edits"] += 1
        elif role == "tool" and content.lstrip().startswith("Error"):
            stats["tool_errors"] += 1
        elif role == "user" and content.startswith("[Forge check]"):
            stats["bounces"] += 1
        elif role == "user" and content.startswith("[Forge]"):
            stats["nudges"] += 1
    stats["models"] = sorted({m.get("model", "") for m in data.get("messages", []) if m.get("model")})
    return stats


def run_task(task: dict, out_dir: pathlib.Path, extra: list[str]) -> dict:
    name = task["name"]
    work = out_dir / name
    if (task["dir"] / "seed").exists():
        shutil.copytree(task["dir"] / "seed", work)
    else:
        work.mkdir(parents=True)
    for tool in task.get("needs", []):
        if shutil.which(tool) is None:
            return {"task": name, "status": "skipped", "why": f"{tool} not on PATH"}
    protect = task.get("protect", [])
    before = _hashes(work, protect)
    timeout = int(task.get("timeout", 900))
    cmd = [sys.executable, "-m", "forgecli.cli", "-y", "--json", *extra, task["prompt"].strip()]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=_env(), stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        stdout, code, timed_out = p.stdout, p.returncode, False
        (out_dir / f"{name}.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr, encoding="utf-8")
    except subprocess.TimeoutExpired as e:
        stdout, code, timed_out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) \
            else (e.stdout or ""), -1, True
    seconds = round(time.time() - t0)
    result = {}
    for line in reversed(stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                result = json.loads(line)
                break
            except ValueError:
                pass
    check = subprocess.run([sys.executable, str(task["dir"] / "check.py")], cwd=work, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=180, env=_env(),
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    changed = [k for k, v in before.items() if _hashes(work, protect).get(k) != v]
    works = check.returncode == 0 and not changed
    stats = session_stats(result.get("session_id", ""), protect, work)
    return {
        "task": name, "status": "pass" if works else "fail", "seconds": seconds, "timed_out": timed_out,
        "forge_ok": bool(result.get("ok")), "exit": code,
        # Forge said it finished but the result doesn't work: the failure users notice most.
        "false_done": bool(result.get("ok")) and not works,
        "protected_changed": changed, "check": (check.stdout + check.stderr).strip()[-600:],
        "answer": (result.get("answer") or "")[:600], "session_id": result.get("session_id", ""), **stats,
    }


def report(results: list[dict], previous: list[dict] | None) -> str:
    done = [r for r in results if r["status"] != "skipped"]
    passed = sum(r["status"] == "pass" for r in done)
    tot = lambda k: sum(r.get(k, 0) or 0 for r in done)
    lines = [f"# Forge task suite - {passed}/{len(done)} pass", "",
             f"false 'done': {sum(r.get('false_done', False) for r in done)} · steps {tot('steps')} · "
             f"tool errors {tot('tool_errors')} · nudges {tot('nudges')} · claim bounces {tot('bounces')} · "
             f"protected-file edits {tot('test_edits')}", "",
             "| task | result | forge said | steps | errors | nudges | bounces | test edits | time |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if r["status"] == "skipped":
            lines.append(f"| {r['task']} | skipped ({r['why']}) | | | | | | | |")
            continue
        lines.append(f"| {r['task']} | {'PASS' if r['status'] == 'pass' else 'FAIL'} | "
                     f"{'done' if r['forge_ok'] else 'not done'}{' ⚠' if r['false_done'] else ''} | {r['steps']} | "
                     f"{r['tool_errors']} | {r['nudges']} | {r['bounces']} | {r['test_edits']} | {r['seconds']}s |")
    if previous:
        prev = {r["task"]: r for r in previous}
        lines += ["", "## Against the previous run", ""]
        for r in done:
            p = prev.get(r["task"])
            if p and p.get("status") != r["status"]:
                lines.append(f"- {r['task']}: {p['status']} → {r['status']}")
        pp = [r for r in previous if r.get("status") != "skipped"]
        lines.append(f"- pass {sum(r['status'] == 'pass' for r in pp)} → {passed}; steps "
                     f"{sum(r.get('steps', 0) for r in pp)} → {tot('steps')}")
    fails = [r for r in done if r["status"] != "pass"]
    if fails:
        lines += ["", "## Failures", ""]
        for r in fails:
            lines.append(f"### {r['task']}\n- check: `{r['check'][-300:]}`\n- protected changed: "
                         f"{r['protected_changed'] or 'none'}\n- answer: {r['answer'][:300]!r}\n")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tasks", nargs="*")
    ap.add_argument("--jobs", type=int, default=2, help="tasks at once (free tiers rate-limit; keep it low)")
    ap.add_argument("--model", help="pass -m to forge")
    ap.add_argument("--provider", help="pass -p to forge")
    ap.add_argument("--compare", action="store_true", help="compare with the previous run")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    tasks = load_tasks(a.tasks)
    if a.list:
        for t in tasks:
            print(f"{t['name']:<18} {t['prompt'].strip().splitlines()[0][:90]}")
        return 0
    extra = (["-m", a.model] if a.model else []) + (["-p", a.provider] if a.provider else [])
    previous = None
    if a.compare and RUNS.exists():
        olds = sorted(RUNS.glob("*/results.json"))
        previous = json.loads(olds[-1].read_text(encoding="utf-8")) if olds else None
    out = RUNS / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    results = []
    with cf.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        futs = {pool.submit(run_task, t, out, extra): t["name"] for t in tasks}
        for fut in cf.as_completed(futs):
            try:
                r = fut.result()
            except Exception as e:      # one broken task must not lose the others' results
                r = {"task": futs[fut], "status": "fail", "check": f"runner error: {e!r}", "seconds": 0,
                     "timed_out": False, "forge_ok": False, "false_done": False, "protected_changed": [],
                     "answer": "", "steps": 0, "tool_errors": 0, "nudges": 0, "bounces": 0, "test_edits": 0}
            results.append(r)
            print(f"{r['task']:<18} {r['status'].upper():<8} {r.get('seconds', '')}s  steps={r.get('steps', '')}",
                  flush=True)
    order = {t["name"]: i for i, t in enumerate(tasks)}
    results.sort(key=lambda r: order[r["task"]])
    (out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    text = report(results, previous)
    (out / "report.md").write_text(text, encoding="utf-8")
    print("\n" + text + f"\nworkspaces, logs and report: {out}")
    return 0 if all(r["status"] != "fail" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
