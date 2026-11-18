"""Nova's tools inside Forge: desktop control, screen and camera, browser automation, memory, reminders,
documents, media and dev helpers, plus code mode (run_code), which lets the model chain many tool calls in
one Python snippet instead of a round trip per call.

Nova's tools live in forgecli/nova (vendored from the Nova desktop companion). This module builds them once,
offers the ones that fit the request next to Forge's own tools, and runs them through Forge's approvals.
Forge's own tools always win on a name clash (read_file, run_shell, web_search ...)."""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
import threading

from . import images

# Nova tools Forge already has (or that only make sense inside Nova's own app).
SKIP = {"read_file", "write_file", "edit_file", "glob", "grep", "run_shell", "web_search", "web_fetch",
        "open_url", "list_dir", "todo", "process", "apply_patch", "use_skill", "find_skill", "undo",
        "bash_tool", "view", "create_file", "str_replace", "mcp_add", "mcp_call", "mcp_list", "mcp_search"}
MAX_OFFERED = 24            # Nova schemas offered per request (run_code reaches every one of them)
CODE_TIMEOUT = 600
CODE_MAX_OUTPUT = 6000


def disabled() -> bool:
    return os.environ.get("FORGE_NOVA", "1").strip().lower() in ("0", "off", "false", "no")


def code_mode() -> str:
    """'on' (default): run_code is offered next to the normal tools. 'only': run_code (plus ask_user) is the
    only tool, which keeps requests small with many tools. 'off': no run_code."""
    v = os.environ.get("FORGE_CODE_MODE", "on").strip().lower()
    return {"0": "off", "false": "off", "no": "off", "1": "on", "true": "on"}.get(v, v if v in ("on", "off", "only") else "on")


class _Loop:
    """One background event loop for code mode and for Nova helpers that expect one."""
    _loop: asyncio.AbstractEventLoop | None = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> asyncio.AbstractEventLoop:
        with cls._lock:
            if cls._loop is None:
                cls._loop = asyncio.new_event_loop()
                threading.Thread(target=cls._loop.run_forever, name="forge-nova-loop", daemon=True).start()
            return cls._loop


class _Vision:
    """What Nova's view_image / describe_screen call as `router.ask_image`: ask a Forge model that sees images."""

    def __init__(self, router):
        self.router = router

    async def ask_image(self, question: str, image_jpeg: bytes, model: str | None = None) -> str:
        return await asyncio.get_running_loop().run_in_executor(None, self._ask, question, image_jpeg)

    def _ask(self, question: str, image_jpeg: bytes) -> str:
        pairs = [(p, m) for p, m in self.router._pairs() if images.sees_images(m)]
        if not pairs:
            raise RuntimeError("no configured model can see images")
        fd, tmp = tempfile.mkstemp(suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(image_jpeg)
            msgs = [{"role": "user", "content": question, "images": [tmp]}]
            r = self.router.chain_once(msgs, None, 0, prefer=(pairs[0][0].name, pairs[0][1]))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not r or not images.sees_images(r.get("model", "")):
            raise RuntimeError("no vision model answered")
        return str(r.get("content") or "")


class NovaBridge:
    def __init__(self, tools):
        self.tools = tools              # forgecli.tools.Tools
        self.router = None              # set by the agent: the vision tools use it
        self._registry = None
        self._env = None
        self._error = ""
        self._lock = threading.Lock()
        self._sessions: dict[int, object] = {}

    # setup ---------------------------------------------------------------------------------------
    def _load(self):
        if self._registry is not None or self._error:
            return self._registry
        with self._lock:
            if self._registry is not None or self._error:
                return self._registry
            try:
                from .nova.core.config import DATA_DIR, load_config
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                from .nova.tools.base import ToolEnv
                from .nova.tools.builtin import build_registry
                cfg = load_config()
                cfg["tools"]["workspace"] = str(self.tools.root)
                cfg["tools"]["owner_mode"] = True          # Forge works on whatever project you point it at
                cfg["tools"]["owner_roots"] = [str(self.tools.root)]
                env = ToolEnv(cfg)
                if cfg["awareness"].get("enabled"):
                    from .nova.screen import ScreenTools
                    env.screen = ScreenTools(cfg)
                if cfg["schedule"].get("enabled"):
                    from .nova.scheduler import ReminderStore
                    env.scheduler = ReminderStore()
                if cfg["memory"].get("enabled"):
                    from .nova.memory import MemoryStore
                    env.memory = MemoryStore(max_items=int(cfg["memory"].get("max_items", 500)))
                from .nova.tools.skills import SkillStore
                env.skills = SkillStore()
                env.loop = _Loop.get()
                if self.router is not None:
                    env.llm = _Vision(self.router)
                    if env.screen is not None:
                        env.screen.brain_router = env.llm
                registry = build_registry(cfg)
                for name in [n for n in registry.names() if n in SKIP]:
                    del registry.tools[name]
                env.registry = registry
                self._env, self._registry = env, registry
            except Exception as exc:          # a broken optional dependency must never stop Forge
                self._error = f"{type(exc).__name__}: {exc}"
                self.tools.ui.log(f"Nova tools unavailable: {self._error}")
        return self._registry

    def set_router(self, router) -> None:
        self.router = router
        if self._env is not None:
            self._env.llm = _Vision(router)
            if self._env.screen is not None:
                self._env.screen.brain_router = self._env.llm

    def names(self) -> list[str]:
        reg = None if disabled() else self._load()
        return reg.names() if reg else []

    def get(self, name: str):
        reg = None if disabled() else self._load()
        return reg.get(name) if reg else None

    def readonly(self) -> set[str]:
        reg = None if disabled() else self._load()
        return {t.name for t in reg.tools.values() if t.readonly} if reg else set()

    def schemas(self, query: str) -> list[dict]:
        reg = None if disabled() else self._load()
        if not reg:
            return []
        picked = reg.schemas_for(query or "")
        if len(picked) >= len(reg.names()):
            # Nothing desktop-specific was asked for. Forge is a coding agent, so offer the dev helpers;
            # run_code still reaches every other tool.
            dev = {"code", "web"}
            picked = [reg.get(n).schema() for n in reg.names() if reg.get(n).group in dev]
            picked += [reg.get(n).schema() for n in ("run_python", "check_port", "check_url", "view_image",
                                                     "read_document") if reg.get(n)]
        return picked[:MAX_OFFERED]

    # running -------------------------------------------------------------------------------------
    def execute(self, name: str, args: dict) -> str:
        from .nova.tools.base import NeedsAccess, NeedsChoice, NeedsInfo, NeedsSearch, ToolError
        tool = self.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'."
        env = self._env
        detail = json.dumps(args, ensure_ascii=False)[:400]
        if not tool.readonly:
            try:
                title = tool.summarize(args, env) if tool.summarize else f"{name}"
            except Exception:
                title = name
            forced = tool.policy == "confirm" or bool(tool.escalate and _safe(tool.escalate, args, env))
            why = "changes things on your computer" if forced else ""
            if tool.policy == "deny":
                return f"Error: {name} is switched off."
            if forced or self.tools.mode == "ask":
                denial = self.tools._approve(name, str(title), detail, why=why or "approval mode: ask")
                if denial:
                    return denial
            self.tools.ui.tool(name, str(title) if tool.summarize else detail[:120])
        else:
            self.tools.ui.peek("search" if "search" in name or "find" in name else "read", f"{name} {detail[:80]}")
        for _ in range(4):
            try:
                out = tool.handler(args, env)
                text = str(out if out is not None else "Done.")
                if not tool.readonly:
                    self.tools.ui.tool_result(text[:600])
                return text
            except NeedsAccess as exc:
                denial = self.tools._approve(name, f"Allow {exc.path}?", exc.prompt(), why="outside the project folder")
                if denial:
                    return denial
                env.grant(exc.path)
            except NeedsChoice as exc:
                for question, changes in exc.steps:
                    choice, _ = self.tools.ui.confirm(name, question, "the tool is asking")
                    if choice in ("y", "a"):
                        args = {**args, **changes}
                        break
                else:
                    if exc.decline is None:
                        return f"Stopped: {exc}"
                    args = {**args, **exc.decline}
            except NeedsSearch as exc:
                return f"Error: {exc} Use the find tool to search the drives for it."
            except NeedsInfo as exc:
                return f"Error: {exc} Ask the user, then call {name} again."
            except ToolError as exc:
                return f"Error: {exc}"
            except Exception as exc:
                return f"Error: {type(exc).__name__}: {exc}"
        return f"Error: {name} kept asking for more access."

    # code mode -------------------------------------------------------------------------------------
    def code_registry(self, forge_schemas: list[dict]):
        """Forge's tools and Nova's in one registry for run_code's index, describe() and search_tools()."""
        from .nova.tools.base import Registry, Tool
        tools = []
        for s in forge_schemas:
            f = s["function"]
            if f["name"] in ("run_code", "ask_user", "run_parallel", "delegate"):
                continue
            p = f.get("parameters") or {}
            tools.append(Tool(f["name"], f.get("description", ""), p.get("properties") or {},
                              p.get("required") or [], None, "forge", readonly=f["name"] in _forge_readonly(),
                              untrusted=f["name"] in ("read_file", "grep", "web_fetch", "web_search", "run_shell")))
        reg = Registry(tools)
        for name in self.names():
            reg.tools.setdefault(name, self.get(name))
        return reg

    def run_code(self, agent, code: str, forge_schemas: list[dict]) -> str:
        from .nova import codemode
        registry = self.code_registry(forge_schemas)
        key = id(agent)
        sess = self._sessions.get(key)
        if sess is None:
            sess = self._sessions[key] = codemode.CodeSession(registry, lambda: str(self.tools.root))
        sess.registry = registry
        loop = _Loop.get()

        async def invoke(name, args):
            text = await asyncio.get_running_loop().run_in_executor(
                None, agent._execute, name, json.dumps(args, ensure_ascii=False))
            text = str(text)
            return not text.startswith(("Error", "The user denied")), text

        self.tools.ui.tool("run_code", (code.strip().splitlines() or [""])[0][:120])
        fut = asyncio.run_coroutine_threadsafe(
            sess.run(code, invoke, timeout=CODE_TIMEOUT, max_output=CODE_MAX_OUTPUT), loop)
        try:
            ok, text, _used = fut.result(CODE_TIMEOUT + 30)
        except KeyboardInterrupt:
            sess.cancel.set()
            raise
        return text if ok else "Error in run_code:\n" + text

    def reset_code(self, agent) -> None:
        self._sessions.pop(id(agent), None)

    def code_prompt(self, forge_schemas: list[dict]) -> str:
        from .nova import codemode
        reg = self.code_registry(forge_schemas)
        rules = codemode.RULES.replace("ask_user, task and present_plan stay ordinary tool calls.",
                                       "ask_user stays an ordinary tool call. run_code is optional: plain tool "
                                       "calls still work, but use run_code when one step needs several tools.")
        return rules + "\n\n" + codemode.tool_index(reg, reg.names())


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return False


def _forge_readonly() -> set[str]:
    from .agent import READ_ONLY
    return READ_ONLY


def code_schema() -> dict:
    from .nova import codemode
    return codemode.SCHEMA


def home() -> pathlib.Path:
    from .nova.core.config import ROOT
    return ROOT
