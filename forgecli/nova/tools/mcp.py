"""MCP client: connects to Model Context Protocol servers and adds their tools to Nova's registry.

Servers are listed in config.json under "mcp": {"servers": {...}}, or in mcp.json next to config.json using the
same shape Claude Code uses ({"mcpServers": {...}}). Each server is either a local program (stdio) or a web address
(streamable HTTP):

    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/Harry"]},
    "github":     {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}},

Optional per server: "env", "cwd", "disabled": true, "trust": true (its tools run without asking), "timeout".
Servers start in the background, so a slow `npx` download never holds up Nova starting; each server's tools appear
in the registry as mcp__<server>__<tool> the moment it is ready.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

from forgecli.nova.core.config import ROOT
from forgecli.nova.tools.base import Tool, ToolError

log = logging.getLogger("mcp")

PROTOCOL = "2025-06-18"
CLIENT = {"name": "nova", "version": "2.0"}


def _expand(value):
    """${VAR} and %VAR% in config values come from the environment, so tokens can live in .env."""
    if isinstance(value, str):
        value = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def server_configs(cfg: dict) -> dict:
    servers = {}
    path = ROOT / "mcp.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            servers.update(data.get("mcpServers") or data.get("servers") or {})
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("mcp.json could not be read (%s)", exc)
    servers.update(((cfg.get("mcp") or {}).get("servers")) or {})
    return {name: _expand(spec) for name, spec in servers.items()
            if isinstance(spec, dict) and not spec.get("disabled")}


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:40]


class _Stdio:
    """Newline-delimited JSON-RPC over a child process's stdin/stdout."""

    def __init__(self, name: str, spec: dict):
        cmd = spec["command"]
        exe = shutil.which(cmd) or cmd                      # npx -> npx.cmd on Windows
        env = {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}}
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen([exe, *map(str, spec.get("args") or [])], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                     cwd=spec.get("cwd") or None, creationflags=flags)
        self.name = name
        self.replies: dict[int, queue.Queue] = {}
        self.lock = threading.Lock()
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True, name=f"mcp-{name}").start()
        threading.Thread(target=self._drain, daemon=True, name=f"mcp-{name}-err").start()

    def _read(self):
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue                                    # servers sometimes print banners on stdout
            if isinstance(msg, dict) and msg.get("id") in self.replies and ("result" in msg or "error" in msg):
                self.replies[msg["id"]].put(msg)
            elif isinstance(msg, dict) and "method" in msg and "id" in msg:
                # server -> client request (ping, roots/list...): answer politely so it doesn't hang
                result = {"roots": []} if msg["method"] == "roots/list" else {}
                self._write({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        for q in self.replies.values():
            q.put({"error": {"message": "the server stopped"}})

    def _drain(self):
        for raw in self.proc.stderr:
            self.stderr = (self.stderr + [raw.decode("utf-8", "replace").rstrip()])[-20:]

    def _write(self, msg: dict):
        with self.lock:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.proc.stdin.flush()

    def request(self, rid: int, msg: dict, timeout: float) -> dict:
        if self.proc.poll() is not None:
            raise ToolError(f"MCP server '{self.name}' is not running")
        q = self.replies[rid] = queue.Queue()
        try:
            self._write(msg)
            return q.get(timeout=timeout)
        except queue.Empty:
            raise ToolError(f"MCP server '{self.name}' did not answer within {int(timeout)}s") from None
        except OSError as exc:
            raise ToolError(f"MCP server '{self.name}' is not reachable ({exc})") from None
        finally:
            self.replies.pop(rid, None)

    def notify(self, msg: dict):
        self._write(msg)

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class _Http:
    """Streamable HTTP: each JSON-RPC message is a POST; the reply is JSON or a short SSE stream."""

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.url = spec["url"]
        self.headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
        self.session = ""

    def _post(self, msg: dict, timeout: float) -> dict | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL, **self.headers}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, json.dumps(msg).encode("utf-8"), headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                body = resp.read().decode("utf-8", "replace")
                kind = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise ToolError(f"MCP server '{self.name}' answered HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"MCP server '{self.name}' is not reachable ({exc})") from None
        if not body.strip():
            return None
        if "text/event-stream" in kind:
            for line in body.splitlines():
                if line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict) and data.get("id") == msg.get("id"):
                        return data
            return None
        return json.loads(body)

    def request(self, rid: int, msg: dict, timeout: float) -> dict:
        reply = self._post(msg, timeout)
        if reply is None:
            raise ToolError(f"MCP server '{self.name}' sent no reply")
        return reply

    def notify(self, msg: dict):
        try:
            self._post(msg, 15)
        except ToolError:
            pass

    def close(self):
        pass


class McpServer:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.transport = "http" if spec.get("url") else "stdio"
        self.status = "connecting"
        self.error = ""
        self.tools: list[dict] = []
        self.conn = None
        self._id = 0
        self._id_lock = threading.Lock()
        self.timeout = float(spec.get("timeout") or 120)

    def _rpc(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        with self._id_lock:
            self._id += 1
            rid = self._id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        reply = self.conn.request(rid, msg, timeout or self.timeout)
        if reply.get("error"):
            raise ToolError(f"{self.name}: {reply['error'].get('message', 'error')}")
        return reply.get("result") or {}

    def start(self):
        try:
            if self.transport == "stdio" and not self.spec.get("command"):
                raise ToolError("needs a 'command' or a 'url'")
            self.conn = _Http(self.name, self.spec) if self.transport == "http" else _Stdio(self.name, self.spec)
            self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT},
                      timeout=float(self.spec.get("start_timeout") or 90))
            self.conn.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools, cursor = [], None
            while True:
                page = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
                tools += page.get("tools") or []
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            self.tools = tools
            self.status = "connected"
            log.info("MCP %s: %d tools", self.name, len(tools))
        except Exception as exc:
            self.status = "error"
            tail = getattr(self.conn, "stderr", None)
            self.error = str(exc) + (f" — {tail[-1]}" if tail else "")
            log.warning("MCP %s failed: %s", self.name, self.error)
            if self.conn:
                self.conn.close()

    def call(self, tool: str, args: dict) -> str:
        if self.status != "connected":
            raise ToolError(f"MCP server '{self.name}' is {self.status}: {self.error}".rstrip(": "))
        result = self._rpc("tools/call", {"name": tool, "arguments": args or {}})
        parts = []
        for c in result.get("content") or []:
            kind = c.get("type")
            if kind == "text":
                parts.append(c.get("text", ""))
            elif kind == "resource":
                res = c.get("resource") or {}
                parts.append(res.get("text") or f"[resource {res.get('uri', '')}]")
            elif kind in ("image", "audio"):
                parts.append(f"[{kind} {c.get('mimeType', '')}, {len(c.get('data', ''))} bytes base64]")
            elif kind == "resource_link":
                parts.append(f"[link {c.get('uri', '')}]")
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False)[:20000])
        text = "\n".join(parts).strip() or "(no output)"
        if result.get("isError"):
            raise ToolError(text[:4000])
        return text[:40000]

    def close(self):
        if self.conn:
            self.conn.close()
        self.status = "stopped"


class McpManager:
    def __init__(self, cfg: dict):
        self.servers = {name: McpServer(name, spec) for name, spec in server_configs(cfg).items()}

    def start(self, registry) -> None:
        """Connect every server on its own thread; add its tools to the registry once it is ready."""
        for server in self.servers.values():
            threading.Thread(target=self._start_one, args=(server, registry), daemon=True,
                             name=f"mcp-start-{server.name}").start()

    def _start_one(self, server: McpServer, registry) -> None:
        server.start()
        if server.status != "connected":
            return
        trust = bool(server.spec.get("trust"))
        for t in server.tools:
            tool = self._wrap(server, t, trust)
            registry.tools[tool.name] = tool

    @staticmethod
    def _wrap(server: McpServer, t: dict, trust: bool) -> Tool:
        schema = t.get("inputSchema") or {}
        hints = t.get("annotations") or {}
        readonly = bool(hints.get("readOnlyHint"))
        name = f"mcp__{_safe(server.name)}__{_safe(t.get('name', 'tool'))}"[:64]
        remote = t.get("name", "")

        def handler(args, env, _s=server, _n=remote):
            return _s.call(_n, args)

        desc = (t.get("description") or t.get("title") or remote).strip()
        return Tool(name, f"[MCP {server.name}] {desc}"[:1024], schema.get("properties") or {},
                    list(schema.get("required") or []), handler, "mcp", readonly=readonly, untrusted=True,
                    policy="allow" if (trust or readonly) else "confirm",
                    summarize=lambda a, e, _s=server.name, _n=remote:
                        f"Run {_n} on MCP server {_s} with {json.dumps(a, ensure_ascii=False)[:300]}?")

    def snapshot(self) -> list[dict]:
        return [{"id": s.name, "name": s.name, "transport": s.transport, "status": s.status,
                 "error": s.error or None, "tools": [t.get("name", "") for t in s.tools]}
                for s in self.servers.values()]

    def close(self) -> None:
        for s in self.servers.values():
            s.close()


def wait_ready(manager: McpManager, seconds: float) -> None:
    """For tests and the CLI: block until every server has finished connecting (or failed)."""
    end = time.time() + seconds
    while time.time() < end and any(s.status == "connecting" for s in manager.servers.values()):
        time.sleep(0.1)
