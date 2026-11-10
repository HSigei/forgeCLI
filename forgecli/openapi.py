"""Import OpenAPI descriptions and expose their declared operations as tools."""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request

from . import skills
from .httpapi import validate_arguments

MAX_SPEC = 5_000_000
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


class OpenAPIs:
    def __init__(self, http, root: pathlib.Path):
        self.http = http
        self.root = root
        self.operations: dict[str, dict] = {}
        self.active: set[str] = set()
        self.api_names: set[str] = set()

    def import_spec(self, source: str, name: str = "") -> str:
        source = str(source or "").strip()
        if not source:
            return "Error: provide an OpenAPI spec URL or file path."
        try:
            base_url = ""
            if urllib.parse.urlsplit(source).scheme in ("http", "https"):
                req = urllib.request.Request(source, headers={"User-Agent": "Forge/0.1"})
                with urllib.request.urlopen(req, timeout=30) as response:
                    raw = response.read(MAX_SPEC + 1)
                    if len(raw) > MAX_SPEC:
                        return "Error: OpenAPI spec exceeds the 5 MB limit."
                    base_url = response.geturl()
            else:
                raw = pathlib.Path(source).expanduser().resolve().read_bytes()
                if len(raw) > MAX_SPEC:
                    return "Error: OpenAPI spec exceeds the 5 MB limit."
            text = raw.decode("utf-8-sig")
            try:
                spec = json.loads(text)
            except json.JSONDecodeError:
                import yaml
                spec = yaml.safe_load(text)
            if not isinstance(spec, dict) or not str(spec.get("openapi", "")).startswith("3."):
                return "Error: this is not a supported OpenAPI 3.x document."
        except (OSError, UnicodeError, urllib.error.URLError, ValueError, ImportError) as e:
            return f"Error: could not read OpenAPI spec: {type(e).__name__}: {e}"

        api_name = self._slug(name or self._default_name(source))
        if not api_name:
            api_name = "api"
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:6]
        prefix = f"{api_name}_{digest}"
        paths = spec.get("paths") or {}
        if not isinstance(paths, dict):
            return "Error: OpenAPI paths must be an object."
        components = spec.get("components") or {}
        if not isinstance(components, dict):
            return "Error: OpenAPI components must be an object."
        refs = components.get("schemas") or {}
        if not isinstance(refs, dict):
            return "Error: OpenAPI component schemas must be an object."
        servers = spec.get("servers") or [{"url": "/"}]
        operations = {}
        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                return f"Error: OpenAPI path {path} must be an object."
            if "$ref" in path_item:
                return f"Error: path-item references are not supported for {path}."
            for method, operation in path_item.items():
                if method.lower() not in METHODS:
                    continue
                if not isinstance(operation, dict):
                    return f"Error: OpenAPI operation {method.upper()} {path} must be an object."
                try:
                    item = self._operation(spec, components, base_url, prefix, str(path),
                                           method.lower(), path_item, operation, servers)
                except (KeyError, IndexError, TypeError, ValueError) as e:
                    return f"Error: invalid OpenAPI operation {method.upper()} {path}: {e}"
                if item["name"] in operations:
                    return f"Error: duplicate OpenAPI operation ID {item['name']}."
                operations[item["name"]] = item
        if not operations:
            return "Error: the OpenAPI spec contains no supported operations."
        self.operations.update(operations)
        self.api_names.add(api_name)
        return (f"Imported OpenAPI API '{api_name}' with {len(operations)} operations. "
                "Use openapi_search with a task or operation keyword to load matching tool schemas.")

    @staticmethod
    def _default_name(source: str) -> str:
        path = urllib.parse.urlsplit(source).path
        return pathlib.Path(path or source).stem or "api"

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_]+", "_", value)).strip("_").lower()[:32]

    @staticmethod
    def _schema(schema, refs, depth=0):
        if depth > 12:
            raise ValueError("schema reference nesting exceeds 12 levels")
        if not isinstance(schema, dict):
            return {}
        ref = schema.get("$ref")
        if ref:
            if not ref.startswith("#/components/schemas/"):
                raise ValueError(f"unsupported schema reference {ref}")
            component_name = ref.split("/", 3)[-1].replace("~1", "/").replace("~0", "~")
            target = refs.get(component_name)
            if target is None:
                raise ValueError(f"unresolved schema reference {ref}")
            resolved = OpenAPIs._schema(target, refs, depth + 1)
            if schema.get("description"):
                resolved["description"] = schema["description"]
            return resolved
        out = {k: schema[k] for k in ("type", "description", "format", "enum", "default",
                                      "minimum", "maximum", "minLength", "maxLength")
               if k in schema}
        for key in ("allOf", "anyOf", "oneOf"):
            if key in schema:
                out[key] = [OpenAPIs._schema(item, refs, depth + 1) for item in schema[key]]
        if "properties" in schema:
            out["properties"] = {k: OpenAPIs._schema(v, refs, depth + 1)
                                 for k, v in schema["properties"].items()}
        if "items" in schema:
            out["items"] = OpenAPIs._schema(schema["items"], refs, depth + 1)
        if schema.get("required"):
            out["required"] = list(schema["required"])
        if "additionalProperties" in schema:
            additional = schema["additionalProperties"]
            out["additionalProperties"] = OpenAPIs._schema(additional, refs, depth + 1) \
                if isinstance(additional, dict) else additional
        return out

    @classmethod
    def _component_ref(cls, components, section, item):
        ref = item.get("$ref")
        if not ref:
            return item
        prefix = f"#/components/{section}/"
        if not ref.startswith(prefix):
            raise ValueError(f"unsupported {section} reference {ref}")
        name = ref[len(prefix):].replace("~1", "/").replace("~0", "~")
        target = (components.get(section) or {}).get(name)
        if not isinstance(target, dict):
            raise ValueError(f"unresolved {section} reference {ref}")
        return target

    @classmethod
    def _operation(cls, spec, components, base_url, prefix, path, method, path_item, operation, servers):
        refs = components.get("schemas") or {}
        operation_id = str(operation.get("operationId") or f"{method}_{path}")
        slug = cls._slug(operation_id)
        head = f"openapi__{prefix}__"
        max_slug_len = 64 - len(head)
        if len(slug) > max_slug_len:
            suffix = "_" + hashlib.sha1(operation_id.encode()).hexdigest()[:6]
            slug = slug[:max_slug_len - len(suffix)] + suffix
        name = head + slug
        properties, required = {"path_params": {"type": "object", "properties": {}, "required": []},
                                "query": {"type": "object", "properties": {}},
                                "headers": {"type": "object", "properties": {}}}, []
        params = {}
        for p in (path_item.get("parameters") or []) + (operation.get("parameters") or []):
            if not isinstance(p, dict):
                continue
            p = cls._component_ref(components, "parameters", p)
            key = (p.get("name"), p.get("in"))
            params[key] = p
        for (param_name, location), param in params.items():
            if not param_name or location not in ("path", "query", "header"):
                continue
            target = {"path": "path_params", "query": "query", "header": "headers"}[location]
            schema = cls._schema(param.get("schema") or {}, refs)
            if param.get("description"):
                schema["description"] = param["description"]
            properties[target]["properties"][param_name] = schema
            if param.get("required"):
                properties[target].setdefault("required", []).append(param_name)
        for key in ("path_params", "query", "headers"):
            if properties[key]["properties"]:
                properties[key]["additionalProperties"] = False
                required.append(key) if key == "path_params" and properties[key].get("required") else None
            elif key != "path_params":
                del properties[key]

        request_body = operation.get("requestBody") or {}
        if isinstance(request_body, dict):
            request_body = cls._component_ref(components, "requestBodies", request_body)
        content = request_body.get("content") or {}
        media_type = next((key for key in content if key == "application/json" or key.endswith("+json")),
                          next((key for key in ("application/x-www-form-urlencoded", "multipart/form-data")
                                if key in content), ""))
        media = content.get(media_type)
        if media:
            body_schema = cls._schema(media.get("schema") or {}, refs)
            if media_type == "multipart/form-data":
                body_properties = dict(body_schema.get("properties") or {})
                body_required = list(body_schema.get("required") or [])
                binary = [key for key, value in body_properties.items()
                          if value.get("format") in ("binary", "base64")]
                for key in binary:
                    body_properties.pop(key, None)
                body_required = [key for key in body_required if key not in binary]
                if body_properties:
                    properties["form_body"] = {**body_schema, "properties": body_properties,
                                               "required": body_required}
                    if request_body.get("required"):
                        required.append("form_body")
                if binary:
                    properties["files"] = {"type": "array", "items": {"type": "object",
                        "properties": {"field": {"type": "string", "enum": binary},
                                       "path": {"type": "string"}, "filename": {"type": "string"}},
                        "required": ["field", "path"], "additionalProperties": False}}
                    if request_body.get("required") and (
                            any(k in body_schema.get("required", []) for k in binary) or not body_properties):
                        required.append("files")
                if request_body.get("required") and not body_properties and not binary:
                    raise ValueError("required multipart body has no supported fields")
            elif media_type == "application/x-www-form-urlencoded":
                properties["form_body"] = body_schema or {"type": "object"}
                if request_body.get("required"):
                    required.append("form_body")
            elif media_type == "application/json" or media_type.endswith("+json"):
                properties["json_body"] = body_schema or {"type": "object"}
                if request_body.get("required"):
                    required.append("json_body")
            else:
                raise ValueError(f"unsupported request body media type {media_type}")
        elif request_body:
            raise ValueError("request body has no supported content")

        server_list = operation.get("servers") or path_item.get("servers") or servers or [{"url": "/"}]
        if not isinstance(server_list, list) or not server_list:
            raise ValueError("server list is empty or invalid")
        server_config = server_list[0]
        if not isinstance(server_config, dict):
            raise ValueError("server entry must be an object")
        server = server_config.get("url", "/")
        if not isinstance(server, str):
            raise ValueError("server URL must be a string")
        server_vars = server_config.get("variables") or {}
        for var, definition in server_vars.items():
            server = server.replace("{" + var + "}", str(definition.get("default", "")))
        if re.search(r"\{[^{}]+\}", server):
            raise ValueError("server URL has a variable without a default")
        if urllib.parse.urlsplit(server).scheme not in ("http", "https"):
            if base_url:
                origin = urllib.parse.urlsplit(base_url)
                server = urllib.parse.urljoin(f"{origin.scheme}://{origin.netloc}/", server)
            else:
                server = urllib.parse.urljoin("http://localhost/", server)
        if urllib.parse.urlsplit(server).scheme not in ("http", "https"):
            raise ValueError("unsupported server URL")

        summary = operation.get("summary") or operation.get("description") or operation_id
        description = str(operation.get("description") or summary)[:800]
        schema = {"type": "function", "function": {
            "name": name,
            "description": f"OpenAPI {method.upper()} {path}: {summary}. Call only with parameters from this schema.",
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False},
        }}
        return {"name": name, "api": prefix, "method": method.upper(), "path": path,
                "server": server.rstrip("/"), "summary": str(summary), "description": description,
                "schema": schema}

    def search(self, query: str, limit: int = 8) -> list[dict]:
        if not self.operations:
            return []
        skills_index = {}
        for name, operation in self.operations.items():
            path = pathlib.Path(name.replace(":", "_"))
            skills_index[name] = skills.Skill(name, f"{operation['summary']} {operation['description']} "
                                                     f"{operation['method']} {operation['path']}",
                                               path, "openapi")
        matcher = skills.Matcher(skills_index)
        ranked = matcher.scores(query)
        results = []
        for score, skill in ranked[:limit]:
            if score < matcher.min_score * 0.45:
                continue
            operation = self.operations[skill.name]
            self.active.add(skill.name)
            results.append(operation)
        return results

    def schemas(self, query: str) -> list[dict]:
        matches = self.search(query)
        names = {item["name"] for item in matches} | self.active
        return [self.operations[name]["schema"] for name in sorted(names) if name in self.operations]

    def report(self) -> str:
        if not self.operations:
            return ""
        total = sum(len(json.dumps(item["schema"], ensure_ascii=False))
                    for item in self.operations.values())
        active = sum(len(json.dumps(self.operations[name]["schema"], ensure_ascii=False))
                     for name in self.active if name in self.operations)
        deferred = max(0, total - active) // 4
        return (f"OpenAPI operations registered: {len(self.operations)}; "
                f"deferred schemas save about {deferred:,} tokens per unmatched request")

    def execute(self, name: str, args: dict) -> str:
        operation = self.operations.get(name)
        if not operation:
            return f"Error: OpenAPI operation {name!r} is not registered."
        if name not in self.active:
            return "Error: this OpenAPI operation was not loaded from a matching spec search."
        invalid = validate_arguments(operation["schema"]["function"]["parameters"], args)
        if invalid:
            return f"Error: {invalid}"
        path = operation["path"]
        path_params = args.get("path_params") or {}
        for match in re.findall(r"\{([^{}]+)\}", path):
            if match not in path_params:
                return f"Error: required path parameter {match!r} is missing."
            path = path.replace("{" + match + "}", urllib.parse.quote(str(path_params[match]), safe=""))
        url = operation["server"] + "/" + path.lstrip("/")
        request_args = {"method": operation["method"], "url": url,
                        "query": args.get("query"), "headers": args.get("headers"),
                        "json_body": args.get("json_body"), "form_body": args.get("form_body"),
                        "files": args.get("files")}
        return self.http.request(**request_args)
