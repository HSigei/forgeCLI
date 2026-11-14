"""Lazy MCP stdio and streamable-HTTP clients."""
from __future__ import annotations

import atexit
import copy
import hashlib
import json
import os
import pathlib
import queue
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.error
import urllib.request

from . import skills
from .config import HOME_DIR
from .httpapi import _SameOriginRedirect, _redact, _resolve_secrets, validate_arguments

USAGE_FILE = HOME_DIR / "usage.json"
DORMANT_AFTER = 30 * 24 * 60 * 60
MAX_MCP_RESPONSE = 5_000_000

MCP_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mcp_search",
        "description": "Find MCP tools by describing the action you need. Forge will load the exact "
                       "tool schema before it can be called; do not guess tool names or parameters.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
        }, "required": ["query"]},
    },
}


class UsageStore:
    _registry_lock = threading.Lock()
    _registry = {}

    def __init__(self, path=None, warn=None):
        self.path = pathlib.Path(path or USAGE_FILE).resolve()
        self.warn = warn or (lambda _: None)
        migrated = False
        with self._registry_lock:
            shared = self._registry.get(self.path)
            if shared is None:
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        data = {}
                except (OSError, ValueError):
                    data = {}
                if not isinstance(data.get("records"), dict):
                    data["records"] = {}
                if not isinstance(data.get("servers"), dict):
                    data["servers"] = {}
                from . import keyvault
                records = data["records"]
                for name, row in list(records.items()):
                    if not isinstance(name, str) or not isinstance(row, dict):
                        continue
                    parts = name.split(":", 2)
                    if len(parts) != 3 or parts[0] != "key" or parts[2].startswith("sha256-"):
                        continue
                    safe_name = f"key:{parts[1]}:{keyvault.fingerprint(parts[2])}"
                    previous = records.get(safe_name)
                    if isinstance(previous, dict):
                        for field in ("calls", "failures", "latency_total"):
                            previous[field] = previous.get(field, 0) + row.get(field, 0)
                        previous["last_used"] = max(previous.get("last_used", 0), row.get("last_used", 0))
                    else:
                        records[safe_name] = row
                    del records[name]
                    migrated = True
                shared = (threading.Lock(), data)
                self._registry[self.path] = shared
        self.lock, self.data = shared
        if migrated:
            self._save()

    def record(self, category: str, name: str, success: bool = True, latency: float = 0.0) -> None:
        with self.lock:
            now = time.time()
            key = f"{category}:{name}"
            row = self.data["records"].setdefault(key, {
                "calls": 0, "failures": 0, "latency_total": 0.0, "last_used": 0.0,
                "consecutive_failures": 0, "recent_outcomes": [],
            })
            row["calls"] += 1
            row["failures"] += not success
            row["consecutive_failures"] = 0 if success else row.get("consecutive_failures", 0) + 1
            recent = row.setdefault("recent_outcomes", [])
            recent.append(bool(success))
            del recent[:-20]
            row["latency_total"] += max(0.0, float(latency))
            row["last_used"] = now
            self._save()

    def get(self, category: str, name: str) -> dict:
        with self.lock:
            return dict(self.data["records"].get(f"{category}:{name}", {}))

    def mark_seen(self, category: str, name: str) -> dict:
        return self.mark_seen_many(category, [name]).get(name, {})

    def mark_seen_many(self, category: str, names: list[str]) -> dict[str, dict]:
        with self.lock:
            now, changed, rows = time.time(), False, {}
            for name in names:
                row = self.data["records"].setdefault(f"{category}:{name}", {
                    "calls": 0, "failures": 0, "latency_total": 0.0, "last_used": 0.0,
                    "consecutive_failures": 0, "recent_outcomes": [],
                })
                if not row.get("first_seen"):
                    row["first_seen"] = now
                    changed = True
                rows[name] = dict(row)
            if changed:
                self._save()
            return rows

    def recent_failures(self, category: str, name: str, limit: int = 20) -> int:
        row = self.get(category, name)
        return sum(not success for success in row.get("recent_outcomes", [])[-limit:])

    def server_state(self, name: str) -> dict:
        with self.lock:
            return dict(self.data["servers"].get(name, {}))

    def update_server(self, name: str, **fields) -> None:
        with self.lock:
            self.data["servers"].setdefault(name, {}).update(fields)
            self._save()

    def report(self) -> list[tuple[str, dict]]:
        with self.lock:
            return [(key, dict(row)) for key, row in sorted(self.data["records"].items())]

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            temp.replace(self.path)
        except OSError as e:
            self.warn(f"could not save Forge usage: {e}")


class _Stdio:
    def __init__(self, command, env, cwd, timeout=30):
        self.timeout = timeout
        self.lock = threading.Lock()
        self.responses = queue.Queue()
        self.next_id = 0
        self.proc = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.reader = threading.Thread(target=self._read, daemon=True, name="forge-mcp-reader")
        self.reader.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if "id" in message:
                    self.responses.put(message)
        except (OSError, ValueError):
            pass

    def request(self, method, params=None, notify=False):
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            message = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                message["params"] = params
            if not notify:
                message["id"] = request_id
            self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
            if notify:
                return {}
            end = time.time() + self.timeout
            while time.time() < end:
                try:
                    response = self.responses.get(timeout=max(0.01, end - time.time()))
                except queue.Empty:
                    break
                if response.get("id") == request_id:
                    if "error" in response:
                        raise RuntimeError(str(response["error"])[:500])
                    return response.get("result") or {}
            raise TimeoutError(f"MCP server did not answer {method} within {self.timeout}s")

    def close(self):
        if self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass


class _StreamableHTTP:
    def __init__(self, url, headers, timeout=30):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.session_id = ""
        self.protocol_version = ""
        self.next_id = 0

    def request(self, method, params=None, notify=False):
        self.next_id += 1
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            message["id"] = self.next_id
        headers = dict(self.headers)
        headers.update({"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["Mcp-Protocol-Version"] = self.protocol_version
        request = urllib.request.Request(self.url, json.dumps(message).encode(), headers, method="POST")
        try:
            opener = urllib.request.build_opener(_SameOriginRedirect())
            with opener.open(request, timeout=self.timeout) as response:
                self.session_id = response.headers.get("Mcp-Session-Id", self.session_id)
                raw = response.read(MAX_MCP_RESPONSE + 1)
                if len(raw) > MAX_MCP_RESPONSE:
                    raise ValueError("MCP response exceeds the 5 MB limit")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if notify and e.code in (202, 204):
                return {}
            self.session_id = e.headers.get("Mcp-Session-Id", self.session_id)
            raw, content_type = e.read(MAX_MCP_RESPONSE + 1), e.headers.get("Content-Type", "")
        if notify:
            return {}
        text = raw.decode("utf-8", "replace")
        if "text/event-stream" in content_type:
            records = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
            text = next((line for line in reversed(records) if line), "")
        response = json.loads(text)
        if response.get("id") != message["id"]:
            raise ValueError("MCP server response id did not match the request")
        if "error" in response:
            raise RuntimeError(str(response["error"])[:500])
        return response.get("result") or {}


class MCPManager:
    def __init__(self, tools, root: pathlib.Path, usage: UsageStore | None = None,
                 home_dir: pathlib.Path | None = None, forge_dir: pathlib.Path | None = None):
        self.tools = tools
        self.root = pathlib.Path(root).resolve()
        self.home_dir = pathlib.Path(home_dir or pathlib.Path.home())
        self.forge_dir = pathlib.Path(forge_dir or HOME_DIR)
        self.usage = usage or UsageStore(warn=tools.ui.warn)
        self.configs, self.config_errors = self._load_configs()
        self.clients = {}
        self.secrets = {}
        self.tools_by_id: dict[str, dict] = {}
        self.exposed: set[str] = set()
        self.matchers = {}
        self._closed = False
        atexit.register(self.close)
        self._startup_notice()

    def _load_configs(self):
        errors = []
        claude_file = self.home_dir / ".claude.json"
        claude = self._read_json(claude_file, errors)
        configs = self._servers(claude.get("mcpServers"), claude_file, errors)
        disabled = set()
        projects = claude.get("projects") if isinstance(claude.get("projects"), dict) else {}
        root_key = str(self.root)
        for key, value in projects.items():
            if self._same_path(key, root_key) and isinstance(value, dict):
                configs.update(self._servers(value.get("mcpServers"), claude_file, errors))
                self._disable(disabled, value.get("disabledMcpServers"))
        forge_file = self.forge_dir / "mcp.json"
        forge = self._read_json(forge_file, errors)
        configs.update(self._servers(forge.get("mcpServers"), forge_file, errors))
        project_file = self.root / ".mcp.json"
        project = self._read_json(project_file, errors)
        configs.update(self._servers(project.get("mcpServers"), project_file, errors))
        self._disable(disabled, project.get("disabledMcpServers"))
        self._disable(disabled, forge.get("disabledMcpServers"))
        for name in disabled:
            configs.pop(name, None)
        normalized = {}
        for name, config in configs.items():
            if isinstance(config, dict):
                normalized[str(name)] = {"config": config, "source": self._config_source(name)}
        return normalized, errors

    @staticmethod
    def _servers(value, path, errors):
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        errors.append(f"{path}: mcpServers must be a JSON object")
        return {}

    @staticmethod
    def _disable(disabled, value):
        if isinstance(value, (list, tuple, set)):
            disabled.update(str(name) for name in value)

    def _read_json(self, path, errors):
        try:
            if not path.is_file():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                errors.append(f"{path}: expected a JSON object")
                return {}
            return data
        except (OSError, ValueError) as e:
            errors.append(f"{path}: {type(e).__name__}: {e}")
            return {}

    @staticmethod
    def _same_path(a, b):
        try:
            return os.path.normcase(str(pathlib.Path(a).resolve())) == \
                os.path.normcase(str(pathlib.Path(b).resolve()))
        except OSError:
            return False

    def _config_source(self, name):
        for path in (self.root / ".mcp.json", self.forge_dir / "mcp.json", self.home_dir / ".claude.json"):
            try:
                if path.is_file():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if name in (data.get("mcpServers") or {}):
                        return str(path)
                    for project in (data.get("projects") or {}).values():
                        if name in (project.get("mcpServers") or {}):
                            return str(path)
            except (OSError, ValueError, AttributeError):
                continue
        return "configuration"

    @staticmethod
    def _slug(value):
        return re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_]+", "_", value)).strip("_").lower() or "server"

    def _fingerprint(self, config):
        return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()

    def _server_dormant(self, name, config):
        state = self.usage.server_state(name)
        fingerprint = self._fingerprint(config)
        failures = int(state.get("failures", 0))
        if state.get("config_hash") != fingerprint:
            if failures:
                self.usage.update_server(name, failures=0, config_hash=fingerprint)
            failures = 0
        last_used = float(state.get("last_used", 0))
        return failures >= 3 or bool(last_used and time.time() - last_used >= DORMANT_AFTER)

    def _startup_notice(self):
        unused = []
        for name in self.configs:
            state = self.usage.server_state(name)
            last = float(state.get("last_used", 0))
            if last and time.time() - last >= DORMANT_AFTER:
                unused.append(name)
        if unused:
            self.tools.ui.info(f"Resting unused MCP servers (30d): {', '.join(sorted(unused)[:8])}")

    def _server_description(self, name, row):
        config = row["config"]
        return str(config.get("description") or name)[:160]

    def _server_matches(self, query):
        docs = {}
        for name, row in self.configs.items():
            docs[name] = skills.Skill(name, self._server_description(name, row),
                                      pathlib.Path(name), "mcp")
        if not docs or not query:
            return []
        matcher = skills.Matcher(docs)
        return [(score, skill.name) for score, skill in matcher.scores(query)
                if score >= matcher.min_score * 0.35]

    def schemas(self, query=""):
        matching = self._server_matches(query)
        for _, name in matching[:3]:
            self._load_tools(name)
        out = []
        for name, desc in self.server_descriptions().items():
            if not self._server_dormant(name, self.configs[name]["config"]):
                out.append(f"{name}: {desc}")
        search_schema = json.loads(json.dumps(MCP_SEARCH_SCHEMA))
        search_schema["function"]["description"] += "\nConfigured servers:\n" + "\n".join(out)
        schemas = [search_schema] if self.configs else []
        for tool_id in sorted(self.exposed):
            entry = self.tools_by_id.get(tool_id)
            if entry:
                schemas.append(entry["schema"])
        if query:
            for name in list(self.clients):
                matches = self._matching_tools(name, query)
                for entry in matches[:5]:
                    self.exposed.add(entry["id"])
                    schemas.append(entry["schema"])
        unique = {}
        for schema in schemas:
            unique[schema["function"]["name"]] = schema
        return list(unique.values())

    def server_descriptions(self):
        return {name: self._server_description(name, row) for name, row in self.configs.items()}

    def search(self, query):
        matches = self._server_matches(query)
        results = []
        for _, server in matches[:3]:
            error = self._load_tools(server)
            if error:
                results.append(f"{server}: {error}")
                continue
            for entry in self._matching_tools(server, query)[:8]:
                self.exposed.add(entry["id"])
                results.append(f"{entry['id']}: {entry['description']}")
        if not results:
            return "No MCP tool matched. Check the configured server names/descriptions or try a more specific query."
        return "Exact MCP tools loaded (schemas available on the next step):\n" + "\n".join(
            "- " + item for item in results)

    def _matching_tools(self, server, query):
        entries = [e for e in self.tools_by_id.values() if e["server"] == server]
        if not entries:
            return []
        docs = {}
        for entry in entries:
            docs[entry["id"]] = skills.Skill(entry["id"],
                                              entry["description"] + " " + entry["tool_name"],
                                              pathlib.Path(entry["id"]), "mcp")
        matcher = skills.Matcher(docs)
        return [self.tools_by_id[skill.name] for score, skill in matcher.scores(query)
                if score >= matcher.min_score * 0.35]

    def _load_tools(self, name):
        if name in self.clients:
            return ""
        row = self.configs[name]
        config = row["config"]
        fingerprint = self._fingerprint(config)
        state = self.usage.server_state(name)
        if state.get("config_hash") == fingerprint and int(state.get("failures", 0)) >= 3:
            return "dormant after three startup failures; edit its configuration to retry"
        client = None
        try:
            client = self._connect(name, row)
            init = {"capabilities": {}, "clientInfo": {"name": "Forge", "version": "0.1"}}
            try:
                initialized = client.request("initialize", {"protocolVersion": "2025-03-26", **init})
            except RuntimeError as e:
                if "protocol" not in str(e).lower() and "version" not in str(e).lower():
                    raise
                initialized = client.request("initialize", {"protocolVersion": "2024-11-05", **init})
            if isinstance(client, _StreamableHTTP):
                client.protocol_version = str(initialized.get("protocolVersion") or "2025-03-26")
            client.request("notifications/initialized", notify=True)
            response = client.request("tools/list", {})
            if not isinstance(response.get("tools"), list):
                raise ValueError("MCP server returned no tools list")
            all_tools = list(response["tools"])
            cursor = response.get("nextCursor")
            seen_cursors = set()
            while cursor and len(seen_cursors) < 100:
                if not isinstance(cursor, str):
                    raise ValueError("MCP server returned an invalid pagination cursor")
                if cursor in seen_cursors:
                    break
                seen_cursors.add(cursor)
                page = client.request("tools/list", {"cursor": cursor})
                if not isinstance(page.get("tools"), list):
                    raise ValueError("MCP server returned an invalid tools page")
                all_tools.extend(page["tools"])
                cursor = page.get("nextCursor")
            self.clients[name] = client
            for tool in all_tools:
                self._register(name, tool)
            schema_chars = sum(len(json.dumps(entry["schema"], ensure_ascii=False))
                               for entry in self.tools_by_id.values() if entry["server"] == name)
            self.usage.update_server(name, failures=0, config_hash=fingerprint)
            self.usage.update_server(name, schema_chars=schema_chars)
            return ""
        except Exception as e:
            if isinstance(client, _Stdio):
                client.close()
            if isinstance(e, PermissionError):
                return "MCP server startup was not approved"
            self.usage.update_server(name, failures=int(state.get("failures", 0)) + 1,
                                     config_hash=fingerprint, last_error=type(e).__name__)
            return f"could not start MCP server ({type(e).__name__}: " + \
                _redact(str(e)[:180], self.secrets.get(name, set())) + ")"

    def _connect(self, name, row):
        config = row["config"]
        kind = str(config.get("type") or "").lower()
        used = set()
        if kind in ("http", "streamable-http") or config.get("url"):
            url = _resolve_secrets(str(config.get("url") or ""), used)
            if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
                raise ValueError("MCP HTTP server needs an http(s) URL")
            headers = _resolve_secrets(config.get("headers") or {}, used)
            if not isinstance(headers, dict):
                raise ValueError("MCP HTTP headers must be an object")
            used.update(str(v) for key, v in headers.items()
                        if v and re.search(r"(auth|token|key|secret|cookie|credential)", str(key), re.I))
            self.secrets[name] = used
            timeout = max(1, min(int(config.get("timeout", 30)), 600))
            return _StreamableHTTP(url, headers, timeout)

        command = config.get("command")
        args = config.get("args") or []
        if isinstance(command, list):
            args = command[1:] + list(args)
            command = command[0] if command else ""
        if not isinstance(command, str) or not command.strip():
            raise ValueError("MCP stdio server needs a command")
        argv = [command.strip()] + [str(v) for v in args]
        env = os.environ.copy()
        env_values = config.get("env") or {}
        env.update({str(k): str(_resolve_secrets(str(v), used)) for k, v in env_values.items()})
        for key, value in env_values.items():
            if re.search(r"(key|token|secret|pass|auth|credential)", str(key), re.I) and value:
                used.add(str(env[str(key)]))
        self.secrets[name] = used
        workdir = pathlib.Path(os.path.expandvars(os.path.expanduser(
            str(config.get("cwd") or self.root)))).resolve()
        if not workdir.is_dir():
            raise ValueError("MCP working directory does not exist")
        why = "starts a configured MCP process"
        safe_command = pathlib.Path(argv[0]).name
        denied = self.tools._approve("mcp_server", f"Start MCP server {name}",
                                     f"Configured command: {safe_command} (from {row['source']})",
                                     command="", why=why)
        if denied:
            raise PermissionError("server startup was not approved")
        return _Stdio(argv, env, workdir, max(1, min(int(config.get("timeout", 30)), 600)))

    def _register(self, server, tool):
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            return
        original = tool["name"]
        head = f"mcp__{self._slug(server)[:20]}__"
        tool_slug = self._slug(original)
        max_tool_len = 64 - len(head)
        if len(tool_slug) > max_tool_len:
            suffix = "_" + hashlib.sha1(f"{server}:{original}".encode()).hexdigest()[:6]
            tool_slug = tool_slug[:max_tool_len - len(suffix)] + suffix
        base = head + tool_slug
        tool_id = base
        if tool_id in self.tools_by_id:
            suffix = "__" + hashlib.sha1(f"{server}:{original}".encode()).hexdigest()[:6]
            tool_id = tool_id[:64 - len(suffix)] + suffix
        input_schema = copy.deepcopy(tool.get("inputSchema")) \
            if isinstance(tool.get("inputSchema"), dict) else {"type": "object", "properties": {}}
        self._strict_schema(input_schema)
        schema = {"type": "function", "function": {
            "name": tool_id,
            "description": str(tool.get("description") or f"{server} MCP tool {original}")[:1200],
            "parameters": input_schema,
        }}
        self.tools_by_id[tool_id] = {"id": tool_id, "server": server, "tool_name": original,
                                    "description": schema["function"]["description"],
                                    "schema": schema, "annotations": tool.get("annotations") or {}}

    @classmethod
    def _strict_schema(cls, schema):
        if not isinstance(schema, dict):
            return
        if schema.get("type") == "object":
            schema.setdefault("additionalProperties", False)
            for child in (schema.get("properties") or {}).values():
                cls._strict_schema(child)
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict):
                cls._strict_schema(extra)
        if isinstance(schema.get("items"), dict):
            cls._strict_schema(schema["items"])

    def execute(self, name, args):
        entry = self.tools_by_id.get(name)
        if not entry or name not in self.exposed:
            return f"Error: MCP tool {name!r} is not loaded from a server schema."
        invalid = validate_arguments(entry["schema"]["function"]["parameters"], args)
        if invalid:
            return f"Error: {invalid}"
        server = entry["server"]
        annotations = entry["annotations"]
        if annotations.get("readOnlyHint") is not True:
            denied = self.tools._approve(
                "mcp_tool", f"Run MCP tool {server}.{entry['tool_name']}",
                "Arguments are omitted from this prompt to avoid displaying credentials or private data.",
                why="MCP tool is not declared read-only")
            if denied:
                return denied
        started = time.monotonic()
        try:
            result = self.clients[server].request("tools/call", {
                "name": entry["tool_name"], "arguments": args,
            })
            content = result.get("content") or []
            text = "\n".join(str(item.get("text", "")) for item in content
                            if isinstance(item, dict) and item.get("type") == "text")
            if not text:
                text = json.dumps(result, ensure_ascii=False)[:MAX_MCP_RESPONSE]
            text = _redact(text, self.secrets.get(server, set()))
            is_error = bool(result.get("isError"))
            self.usage.record("mcp_tool", f"{server}/{entry['tool_name']}", not is_error,
                              time.monotonic() - started)
            self.usage.update_server(server, last_used=time.time())
            if is_error:
                return "MCP tool reported an error:\n" + text
            return text or "(MCP tool returned no text content)"
        except Exception as e:
            self.usage.record("mcp_tool", f"{server}/{entry['tool_name']}", False,
                              time.monotonic() - started)
            self.usage.update_server(server, last_used=time.time())
            return f"Error: MCP call failed ({type(e).__name__}: " + \
                _redact(str(e)[:300], self.secrets.get(server, set())) + ")"

    def report(self):
        lines = []
        now = time.time()
        for name, row in sorted(self.configs.items()):
            state = self.usage.server_state(name)
            age = now - float(state.get("last_used", now))
            dormant = age >= DORMANT_AFTER and state.get("last_used")
            failed = int(state.get("failures", 0)) >= 3
            status = "dormant after 3 failures" if failed else (
                "resting unused (30d)" if dormant else "ready" if name in self.clients else "not started")
            lines.append(f"{name}: {status}")
        for source in self.config_errors:
            lines.append("Config warning: " + source)
        for key, row in self.usage.report():
            lines.append(f"{key}: {row['calls']} calls, {row['failures']} failures, "
                         f"{row['latency_total']:.1f}s total")
        deferred = sum(int(self.usage.server_state(name).get("schema_chars", 0))
                       for name in self.configs) // 4
        if deferred:
            lines.append(f"Deferred MCP schemas: about {deferred:,} tokens are not sent on unmatched requests")
        return "\n".join(lines) or "No MCP servers or recorded Forge tool usage."

    def close(self):
        if self._closed:
            return
        self._closed = True
        for client in self.clients.values():
            if isinstance(client, _Stdio):
                client.close()
