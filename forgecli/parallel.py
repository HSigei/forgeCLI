"""Parallel sub-agents. Off unless the user asks: /parallel <task> || <task>, or
/parallel on to let the main agent split work itself with the run_parallel tool.

Each sub-agent gets a copy of the whole conversation so far (so it knows what
was already done), works on its own task in a thread, and starts its failover
chain at a different provider so the load spreads across keys. Their reports
go back into the main conversation, so every later reply knows what they did."""
from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from rich.markdown import Markdown
from rich.text import Text

from .ui import UI, console

CONFIRM_LOCK = threading.Lock()   # one approval prompt on screen at a time
COLORS = ["#67e8f9", "#fbbf24", "#4ade80", "#f472b6", "#93c5fd", "#fb923c", "#a3e635", "#e879f9"]

SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_parallel",
        "description": (
            "Run 2 or more independent tasks at the same time, each by its own sub-agent with a copy of "
            "this conversation. Use only when the work splits into parts that don't depend on each other "
            "and don't edit the same files. You get back each agent's report."),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array", "minItems": 2,
                    "items": {"type": "string", "description": "Full, self-contained instructions for one agent"},
                },
                "isolate": {
                    "type": "boolean",
                    "description": ("Give each agent its own git worktree and branch so their edits can't "
                                    "collide (git projects only). Use when agents might touch the same files."),
                },
            },
            "required": ["tasks"],
        },
    },
}


DELEGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": (
            "Hand tasks to team members (other models) that run at the same time, each with a copy of this "
            "conversation. Pick the worker whose strengths fit each task, from the team list in your "
            "instructions. Tasks must be independent and must not edit the same files. You get back each "
            "worker's report; then check, combine, and finish the job."),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array", "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string", "description": "Full, self-contained instructions"},
                            "worker": {"type": "string", "description": "Worker id from the team list, e.g. nvidia:minimaxai/minimax-m3.1-flash"},
                            "why": {"type": "string", "description": "One short reason this worker fits"},
                        },
                        "required": ["task", "worker"],
                    },
                },
            },
            "required": ["tasks"],
        },
    },
}


def split_tasks(text: str) -> list[str]:
    return [t.strip() for t in text.split("||") if t.strip()]


def parse_tasks(items) -> list[dict]:
    """Normalise tasks to {task, worker, why}. Accepts plain strings, '@worker task'
    strings, or dicts from the delegate tool."""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            task = str(it.get("task", "")).strip()
            worker, why = str(it.get("worker", "")).strip(), str(it.get("why", "")).strip()
        else:
            task, worker, why = str(it).strip(), "", ""
            if task.startswith("@"):
                head, _, rest = task.partition(" ")
                worker, task = head[1:], rest.strip()
        if task:
            out.append({"task": task, "worker": worker, "why": why})
    return out


class SubUI(UI):
    """Quiet, labelled output for a sub-agent running in a thread."""

    def __init__(self, parent: UI, n: int):
        super().__init__(parent.verbose)
        self.n = n
        self.color = COLORS[(n - 1) % len(COLORS)]
        self.final = ""

    def _tag(self) -> Text:
        return Text(f"  [{self.n}] ", style=f"bold {self.color}")

    def start_spinner(self, label: str = "thinking") -> None: ...
    def stop_spinner(self) -> None: ...
    def tool_result(self, text: str) -> None: ...

    def log(self, msg: str) -> None:
        if self.verbose:
            console.print(self._tag().append(f"↳ {msg}", style="faint"))

    def tool(self, name: str, summary: str) -> None:
        console.print(self._tag().append(name, style="tool").append("  ").append(summary[:120], style="muted"))

    def assistant(self, text: str, meta: str = "") -> None:
        self.final = text   # shown when the agent finishes

    def info(self, msg: str) -> None:
        console.print(self._tag().append(msg, style="muted"))

    def warn(self, msg: str) -> None:
        console.print(self._tag().append(msg, style="warn"))

    def error(self, msg: str) -> None:
        console.print(self._tag().append("✗ " + msg, style="err"))

    def confirm(self, title: str, detail: str, why: str = ""):
        with CONFIRM_LOCK:
            return super().confirm(f"[agent {self.n}] {title}", detail, why)

    def action(self, verb: str, target: str, note: str = "") -> None:
        console.print(self._tag().append(verb, style="tool").append(f"({target[:110]})", style="muted"))

    def result(self, first: str, body=None) -> None:
        console.print(self._tag().append("  ⎿  " + first, style="faint"))

    def peek(self, kind: str, target: str) -> None: ...
    def flush(self) -> None: ...
    def failover(self, msg: str) -> None:
        console.print(self._tag().append("⚠ " + msg, style="warn"))


def _handoff_packet(parent, task: str, reason: str) -> str:
    goal = parent._goal() if hasattr(parent, "_goal") else ""
    if not goal:
        goal = next((str(m.get("content") or "") for m in reversed(parent.messages)
                     if m.get("role") == "user"), "")
    lines = [f"Original user goal: {goal}",
             f"Why this task was handed off: {reason or 'independent tasks requested'}"]

    reads, changes, attempts = [], [], []
    for step in parent.tools.ledger.steps:
        path = str(step.args.get("path", "")).strip()
        if step.name == "read_file" and path:
            reads.append(path)
        elif step.name in ("write_file", "edit_file") and path:
            (changes if step.ok else attempts).append(path)
        if step.name == "run_shell":
            command = str(step.args.get("command", "")).strip()
            if command:
                status = "succeeded" if step.ok else "failed"
                detail = " ".join(step.out.split())[:220]
                attempts.append(f"command {command!r} ({status})" +
                                (f": {detail}" if detail else ""))
        elif not step.ok:
            detail = " ".join(step.out.split())[:220]
            attempts.append(f"{step.name} failed" + (f": {detail}" if detail else ""))

    if reads:
        lines.append("Files already read:\n" + "\n".join(f"- {p}" for p in dict.fromkeys(reads)))
    if changes:
        lines.append("Files already changed:\n" + "\n".join(f"- {p}" for p in dict.fromkeys(changes)))
    if attempts:
        lines.append("What was tried:\n" + "\n".join(f"- {item}" for item in dict.fromkeys(attempts)))

    skill_results = {m.get("tool_call_id"): m.get("content", "") for m in parent.messages
                     if m.get("role") == "tool"}
    skills = []
    for message in parent.messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            if fn.get("name") == "use_skill" and call.get("id") in skill_results:
                skills.append(skill_results[call["id"]])
    if skills:
        lines.append("Loaded skill instructions:\n" + "\n\n".join(dict.fromkeys(skills)))

    lines.append(
        f"Assigned task: {task}\n\nDo only this task. Avoid files changed above unless necessary. "
        "Return a short report of actions, changed files, verification, and anything unresolved.")
    return "\n\n".join(lines)


def coordinator_packet(parent, reason: str) -> str:
    return "[Coordinator hand-off packet]\n" + _handoff_packet(
        parent, "Continue the original user goal without repeating completed work.", reason)


def run_tasks(parent, tasks: list, reason: str = "", isolate: bool | None = None) -> str:
    """Run tasks concurrently. Tasks are strings ('@worker task' allowed) or
    {task, worker, why} dicts. Returns a combined report (also used as a tool result)."""
    from .agent import Agent   # local import: agent imports this module
    from . import roster

    ui: UI = parent.ui
    tasks = parse_tasks(tasks)
    if any(t["worker"] for t in tasks):
        workers = roster.build(parent.router)
        for t in tasks:
            selected = roster.resolve(workers, t["worker"]) if t["worker"] else None
            t["w"] = selected if selected and roster.usable(selected, parent.router) else None
    limit = parent.settings.max_agents
    was_limited = len(tasks) > limit
    if was_limited:
        tasks = tasks[:limit]
    cancel = threading.Event()
    from . import worktrees
    repo = worktrees.repo_root(parent.tools.root) if worktrees.wanted(isolate) and len(tasks) > 1 else None
    stamp = time.strftime("%Y%m%d-%H%M%S")

    ui.stop_spinner()
    if not getattr(parent, "_handoff_status_shown", False):
        targets = list(dict.fromkeys(t["w"].id for t in tasks if t.get("w")))
        target = ", ".join(targets[:3]) if targets else "automatic worker routing"
        if len(targets) > 3:
            target += f" +{len(targets) - 3}"
        status = f"◆ Handed to {target} · {reason or 'independent tasks requested'}"
        if was_limited:
            status += f" · capped at {limit} tasks"
        ui.info(status)
        parent._handoff_status_shown = True

    def work(i: int, t: dict) -> dict:
        task = t["task"]
        sub_ui = SubUI(ui, i)
        sub = Agent(parent.settings, sub_ui, router=parent.router, ephemeral=True)
        sub.tools.auto_approve = parent.tools.auto_approve
        sub.tools.always = parent.tools.always          # "always" answers are shared
        sub.offset = i - 1                               # start at a different provider
        sub.prefer = t["w"].prefer if t.get("w") else None   # assigned model goes first
        sub.cancel = cancel
        tree = worktrees.create(repo, i, stamp) if repo else None
        packet = _handoff_packet(parent, task, reason or t.get("why", ""))
        if tree:
            sub.tools.root = tree[0]
            sub.messages[0] = dict(sub.messages[0], content=str(sub.messages[0]["content"]).replace(
                str(parent.tools.root), str(tree[0])))
            packet += (f"\n\nYou work in your own git worktree: {tree[0]} (branch {tree[1]}). Use relative "
                       "paths or paths inside it and pass cwd for shell commands; other agents work in their "
                       "own copies. Don't commit: Forge commits your changes when you finish.")
        sub.messages = [sub.messages[0], {"role": "user", "content": packet}]
        started = time.time()
        try:
            sub._loop()
        except Exception as e:   # one agent failing must not take down the others
            sub_ui.final = sub_ui.final or f"Agent crashed: {e}"
        if tree:
            sub_ui.final = (sub_ui.final or "") + "\n\nWorktree: " + worktrees.finish(repo, tree[0], tree[1], task)
        who = sub.last_answer or "no provider answered"
        assigned = t["w"].id if t.get("w") else ""
        if assigned and sub.last_answer and sub.last_answer.replace(" · ", ":") != assigned:
            who += f" (assigned {assigned}, which failed, so failover answered)"
        return {"n": i, "task": task, "report": sub_ui.final or "(no report — it may have failed or been stopped)",
                "who": who, "assigned": assigned, "secs": time.time() - started,
                "evidence": copy.deepcopy(sub.tools.ledger.steps)}

    results: list[dict] = []
    city = getattr(ui, "city", None)
    if city is not None:              # helper cubs join the delivery run, one per agent
        city.set_helpers(len(tasks))
        ui.act("delegating")
    pool = ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="forge-agent")
    futures = [pool.submit(work, i, t) for i, t in enumerate(tasks, 1)]
    try:
        for f in futures:
            while not f.done():   # short sleeps keep ctrl+c responsive on Windows
                time.sleep(0.2)
            results.append(f.result())
    except KeyboardInterrupt:
        cancel.set()
        ui.warn("stopping agents after their current step…")
        results = [f.result() for f in futures]
    finally:
        pool.shutdown(wait=True)
        if city is not None:
            city.set_helpers(0)
            ui.act("thinking")        # back to the main agent, which reads the reports

    for result in results:
        parent.tools.ledger.steps.extend(result["evidence"])

    for r in results:
        ui.panel(Markdown(r["report"].strip(), code_theme="monokai"),
                 title=f"agent {r['n']} · {r['task'][:60]}", subtitle=f"{r['who'][:90]} · {r['secs']:.0f}s")
    return "\n\n".join(f"### Agent {r['n']}: {r['task']}\n(answered by {r['who']})\n\n{r['report']}"
                       for r in results)
