"""First-class HTTP API calls and OpenAPI operation tools."""
from __future__ import annotations

import ipaddress
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request

SECRET = re.compile(r"\{\{env:([A-Za-z_][A-Za-z0-9_]*)\}\}")
MAX_DOWNLOAD = 50_000_000
MAX_RESPONSE = 16_000

HTTP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "http_request",
        "description": (
            "Call an HTTP API. Use only endpoints and parameters you have verified from the user's "
            "request, a tool result, or an imported OpenAPI schema. Supports JSON/form bodies, file "
            "uploads/downloads, pagination hints, and {{env:NAME}} secrets."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "HTTP method, e.g. GET, POST, PATCH, DELETE"},
                "url": {"type": "string"},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "query": {"type": "object", "description": "Query parameters"},
                "json_body": {"description": "JSON request body"},
                "form_body": {"type": "object", "description": "URL-encoded form fields"},
                "files": {"type": "array", "items": {"type": "object", "properties": {
                    "field": {"type": "string"}, "path": {"type": "string"},
                    "filename": {"type": "string"},
                }, "required": ["field", "path"]}},
                "download_path": {"type": "string", "description": "Save the response body to this file"},
                "timeout": {"type": "integer", "description": "Seconds, default 30, maximum 600"},
                "max_chars": {"type": "integer", "description": "Maximum response text to return, default 12000"},
            },
            "required": ["method", "url"],
        },
    },
}

OPENAPI_IMPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "openapi_import",
        "description": "Import an OpenAPI JSON/YAML spec from an HTTP(S) URL or local file. "
                       "Operations become callable tools based on their verified schemas.",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "HTTP(S) spec URL or local spec file"},
            "name": {"type": "string", "description": "Optional short name for this API"},
        }, "required": ["source"]},
    },
}

OPENAPI_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "openapi_search",
        "description": "Find callable operations in OpenAPI specs already imported into Forge.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
        }, "required": ["query"]},
    },
}


def _resolve_secrets(value, used: set[str], secret_env: dict[str, str] | None = None):
    if isinstance(value, str):
        def substitute(match):
            name = match.group(1)
            secret = (secret_env or {}).get(name, os.environ.get(name))
            if secret is None:
                raise ValueError(f"environment variable {name} is not set")
            if secret:
                used.add(secret)
            return secret
        return SECRET.sub(substitute, value)
    if isinstance(value, list):
        return [_resolve_secrets(item, used, secret_env) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_secrets(item, used, secret_env) for key, item in value.items()}
    return value


def _redact(text: str, secrets: set[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[secret redacted]")
    return text


def _is_local(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_arguments(schema, value, path="arguments"):
    """Validate declared JSON Schema fields before dispatching imported tools."""
    if not isinstance(schema, dict):
        return "has an invalid input schema"
    expected = schema.get("type")
    types = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if expected in types and not types[expected](value):
        return f"{path} must be {expected}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {schema['enum']}"
    if "allOf" in schema:
        for choice in schema["allOf"]:
            error = validate_arguments(choice, value, path)
            if error:
                return error
    for key in ("anyOf", "oneOf"):
        if key in schema:
            matches = sum(not validate_arguments(choice, value, path) for choice in schema[key])
            if matches == 0 or (key == "oneOf" and matches != 1):
                return f"{path} does not match the imported {key} schema"
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                return f"{path}.{key} is required by the imported schema"
        for key, item in value.items():
            sub = props.get(key)
            if sub is None:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    return f"{path}.{key} is not declared in the imported schema"
                if isinstance(extra, dict):
                    sub = extra
            if sub is not None:
                error = validate_arguments(sub, item, f"{path}.{key}")
                if error:
                    return error
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            error = validate_arguments(schema["items"], item, f"{path}[{i}]")
            if error:
                return error
    return ""


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpAPI:
    def __init__(self, tools):
        self.tools = tools

    def request(self, method, url, headers=None, query=None, json_body=None, form_body=None,
                files=None, download_path=None, timeout=30, max_chars=12000):
        started = time.monotonic()
        headers = dict(headers or {})
        secret_env = {}
        api_base = getattr(self.tools, "github_api_base", "https://api.github.com")
        try:
            is_github = urllib.parse.urlsplit(str(url)).hostname == urllib.parse.urlsplit(api_base).hostname
        except ValueError:
            is_github = False
        if is_github and not any(k.lower() == "authorization" for k in headers):
            token = self.tools.github_token()
            if not token:
                return ("Error: GitHub API requires GITHUB_TOKEN, a key saved with /keys, "
                        "or an authenticated `gh` CLI account.")
            secret_env["GITHUB_TOKEN"] = token
            if self.tools.github_account is None:
                self.tools.github_account = self._github_login(token)
            headers["Authorization"] = "Bearer {{env:GITHUB_TOKEN}}"
        result = self._request(method, url, headers, query, json_body, form_body, files,
                               download_path, timeout, max_chars, secret_env)
        try:
            host = urllib.parse.urlsplit(str(url)).hostname or "invalid"
        except ValueError:
            host = "invalid"
        self.tools.usage.record("api", f"{str(method).upper()} {host}",
                                not result.startswith("Error:"), time.monotonic() - started)
        return result

    def _github_login(self, token: str) -> str:
        """Resolve the identity attached to this exact token for action labels."""
        api_base = getattr(self.tools, "github_api_base", "https://api.github.com").rstrip("/")
        request = urllib.request.Request(
            api_base + "/user",
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                     "User-Agent": "Forge"},
            method="GET")
        try:
            opener = urllib.request.build_opener(_SameOriginRedirect())
            with opener.open(request, timeout=5) as response:
                body = json.loads(response.read(64_000).decode("utf-8", "replace"))
            login = body.get("login", "") if isinstance(body, dict) else ""
            if isinstance(login, str) and re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
                return login
        except (OSError, urllib.error.URLError, ValueError, TypeError):
            pass
        return "unknown account"

    def _request(self, method, url, headers=None, query=None, json_body=None, form_body=None,
                 files=None, download_path=None, timeout=30, max_chars=12000, secret_env=None):
        method = str(method or "").strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}", method):
            return "Error: invalid HTTP method."
        used: set[str] = set()
        if (secret_env or {}).get("GITHUB_TOKEN"):
            headers = dict(headers or {})
            headers["Authorization"] = "Bearer {{env:GITHUB_TOKEN}}"
        try:
            url, headers, query = (_resolve_secrets(url, used, secret_env),
                                   _resolve_secrets(headers or {}, used, secret_env),
                                   _resolve_secrets(query or {}, used, secret_env))
            json_body = _resolve_secrets(json_body, used, secret_env)
            form_body = _resolve_secrets(form_body, used, secret_env)
        except ValueError as e:
            return f"Error: {e}"
        if not isinstance(headers, dict) or not isinstance(query, dict):
            return "Error: headers and query must be objects."
        parsed = urllib.parse.urlsplit(str(url))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "Error: http_request needs an http:// or https:// URL."
        if json_body is not None and form_body is not None:
            return "Error: provide either json_body or form_body, not both."
        if files and json_body is not None:
            return "Error: file uploads cannot be combined with json_body."

        try:
            timeout = max(1, min(int(timeout or 30), 600))
            max_chars = max(500, min(int(max_chars or 12000), MAX_RESPONSE))
        except (TypeError, ValueError):
            return "Error: timeout and max_chars must be integers."

        host = parsed.hostname
        api_base = getattr(self.tools, "github_api_base", "https://api.github.com")
        try:
            is_github = host == urllib.parse.urlsplit(api_base).hostname
        except ValueError:
            is_github = False
        github_risk = ""
        github_target = ""
        if is_github:
            from . import github
            github_risk = github.classify(method, parsed.path)
            match = re.match(r"^/repos/([^/]+/[^/]+)", parsed.path)
            github_target = match.group(1) if match else parsed.path.strip("/")
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                missing = self._github_missing_target(parsed, method, request_headers=headers)
                if missing:
                    return f"Error: GitHub target {missing} does not exist; no change was made."
        external_change = method in {"POST", "PUT", "PATCH", "DELETE"} and not _is_local(host)
        if is_github and github_risk == "destructive":
            denied = self.tools._confirm_destructive(
                f"GitHub {method} · {github_target}",
                f"Target: {github_target}\n{method} {parsed.path}")
            if denied:
                return denied
        elif external_change and (not is_github or github_risk == "write"):
            why = f"{method} request to {host}"
            if is_github:
                why = f"GitHub write to {github_target}"
            denied = self.tools._approve(
                "http_request", f"Send {method} request to {host}",
                f"{method} {parsed.path or '/'} (query, headers, and body are not shown)",
                why=why)
            if denied:
                return denied
        if files and not _is_local(host):
            names = ", ".join(pathlib.Path(str(item.get("path", ""))).name
                              for item in files if isinstance(item, dict))
            denied = self.tools._approve(
                "http_request", f"Upload file(s) to {host}",
                f"Files selected: {names or '(none)'}",
                why="uploads a local file to a remote service")
            if denied:
                return denied

        params = urllib.parse.urlencode(query, doseq=True)
        target = str(url)
        if params:
            target += ("&" if urllib.parse.urlsplit(target).query else "?") + params

        body = None
        request_headers = {str(k): str(v) for k, v in headers.items()}
        try:
            if files:
                body, boundary = self._multipart(form_body or {}, files, used)
                request_headers.setdefault("Content-Type", f"multipart/form-data; boundary={boundary}")
            elif form_body is not None:
                body = urllib.parse.urlencode(form_body, doseq=True).encode()
                request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            elif json_body is not None:
                body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
                request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("Accept", "application/json, text/plain, */*")
            request_headers.setdefault("User-Agent", "Forge/0.1")
            req = urllib.request.Request(target, data=body, headers=request_headers, method=method)
            opener = urllib.request.build_opener(_SameOriginRedirect())
            target_label = (f"{method} {github_target} as {self.tools.github_account or 'unknown account'}"
                            if is_github else f"{method} {host}")
            self.tools.ui.action("GitHub" if is_github else "HTTP", target_label)
            try:
                response = opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                response = e
            with response:
                raw = response.read(MAX_DOWNLOAD + 1)
                status = response.status
                response_headers = response.headers
            if len(raw) > MAX_DOWNLOAD:
                return "Error: response exceeds the 50 MB limit."
            if download_path:
                path = self.tools._path(download_path)
                denied = self.tools._approve("write_file", f"Save HTTP response to {path}",
                                             f"Write {len(raw)} response bytes to disk.", path=path)
                if denied:
                    return denied
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                return f"HTTP {status} · saved {len(raw)} bytes to {path}"
            charset = re.search(r"charset=([\w-]+)", response_headers.get("Content-Type", ""))
            text = raw.decode(charset.group(1) if charset else "utf-8", "replace")
            text = _redact(text, used)
            pagination = self._pagination(response_headers, text)
            more = f"\n\n… [{len(text) - max_chars} more characters]" if len(text) > max_chars else ""
            content_type = _redact(response_headers.get("Content-Type", "unknown type"), used)
            result = f"HTTP {status} · {content_type}\n\n{text[:max_chars]}{more}"
            if pagination:
                result += "\n\nPagination hint: " + _redact(pagination, used)
            return result
        except (OSError, urllib.error.URLError, ValueError, TypeError) as e:
            return f"Error: HTTP request failed: {_redact(str(e), used)[:500]}"

    @staticmethod
    def _github_missing_target(parsed, method, request_headers):
        match = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", parsed.path)
        if not match:
            return ""
        owner, repo, suffix = match.group(1), match.group(2), match.group(3) or ""
        targets = [f"/repos/{owner}/{repo}"]
        if method in {"PATCH", "DELETE", "PUT"} and suffix:
            resource = suffix
            merge = re.match(r"^/pulls/(\d+)/merge$", suffix)
            if merge:
                resource = f"/pulls/{merge.group(1)}"
            if re.match(r"^/(?:issues|pulls|branches|git/refs)/", resource):
                targets.append(f"/repos/{owner}/{repo}{resource}")
        for path in targets:
            request = urllib.request.Request(
                urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")),
                headers=request_headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    if not 200 <= response.status < 300:
                        return path
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return path
                if error.code in (401, 403):
                    return f"{path} (authorization failed: HTTP {error.code})"
                return f"{path} (preflight failed: HTTP {error.code})"
            except (OSError, urllib.error.URLError, ValueError) as error:
                return f"{path} (preflight failed: {type(error).__name__})"
        return ""

    @staticmethod
    def _multipart(fields, files, used):
        boundary = "forge-" + os.urandom(18).hex()
        parts = []
        for key, value in fields.items():
            resolved = _resolve_secrets(value, used)
            field = re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))
            for item in (resolved if isinstance(resolved, list) else [resolved]):
                parts.extend((f"--{boundary}\r\n".encode(),
                              f'Content-Disposition: form-data; name="{field}"\r\n\r\n'.encode(),
                              str(item).encode(), b"\r\n"))
        for item in files:
            if not isinstance(item, dict) or not item.get("field") or not item.get("path"):
                raise ValueError("each uploaded file needs field and path")
            path = pathlib.Path(os.path.expandvars(os.path.expanduser(str(item["path"])))).resolve()
            if not path.is_file():
                raise ValueError(f"upload file not found: {path}")
            if path.stat().st_size > MAX_DOWNLOAD:
                raise ValueError(f"upload file exceeds the 50 MB limit: {path.name}")
            data = path.read_bytes()
            field = re.sub(r"[^A-Za-z0-9_.-]", "_", str(item["field"]))
            filename = re.sub(r"[\r\n\"]", "_", str(item.get("filename") or path.name))
            parts.extend((f"--{boundary}\r\n".encode(),
                          (f'Content-Disposition: form-data; name="{field}"; '
                           f'filename="{filename}"\r\n').encode(),
                          b"Content-Type: application/octet-stream\r\n\r\n",
                          data, b"\r\n"))
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), boundary

    @staticmethod
    def _pagination(headers, text):
        links = headers.get("Link", "")
        for item in links.split(","):
            if 'rel="next"' in item:
                match = re.search(r"<([^>]+)>", item)
                if match:
                    return match.group(1)
        try:
            data = json.loads(text)
        except ValueError:
            return ""
        if isinstance(data, dict):
            for key in ("next", "next_url", "nextPage", "next_page", "next_cursor", "nextCursor"):
                if data.get(key):
                    return f"{key}={data[key]}"
        return ""
