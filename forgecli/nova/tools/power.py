"""Power tools: seeing images, reading any document, checking that servers are really up, MCP on demand,
browser screenshots, YouTube transcripts, media editing, documents, notifications and quick Python."""

from __future__ import annotations

import asyncio
import io
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from forgecli.nova.tools.base import ROOT, Tool, ToolEnv, ToolError

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico"}
DOC_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xlsm", ".xls", ".pptx"}


def _clip(text: str, limit: int = 14_000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... ({len(text) - limit} more characters cut)"


def _text(data) -> str:
    return data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else (data or "")


def _file(env: ToolEnv, raw) -> Path:
    p = env.resolve_path(raw)
    if not p.is_file():
        raise ToolError(f"I can't find a file called {p.name}.")
    return p


def _out(env: ToolEnv, raw, default: Path) -> Path:
    return env.resolve_path(raw) if raw else default


# ── seeing ───────────────────────────────────────────────────────────────────────────────────────────

def _jpeg(data: bytes) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.thumbnail((1280, 1280))
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=82)
    return out.getvalue()


def ask_vision(env: ToolEnv, image: bytes, question: str) -> str:
    """Ask the online brains about a picture, on the server's own event loop (its HTTP clients live there)."""
    router, loop = getattr(env, "llm", None), getattr(env, "loop", None)
    jpeg = _jpeg(image)
    if router is not None and loop is not None:
        try:
            fut = asyncio.run_coroutine_threadsafe(router.ask_image(question, jpeg), loop)
            answer = fut.result(timeout=120)
            if answer and answer.strip():
                return answer.strip()
        except Exception as exc:                      # fall through to the screen helper's Gemini fallback
            last = exc
        else:
            last = None
    else:
        last = None
    if getattr(env, "screen", None) is not None:
        return env.screen.ask_vision(image, question)
    raise ToolError(f"No vision brain answered{f' ({last})' if last else ''}. Add OPENROUTER_API_KEY or GEMINI_API_KEY to .env.")


def _t_view_image(args: dict, env: ToolEnv) -> str:
    p = _file(env, args.get("path"))
    if p.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ToolError(f"{p.name} isn't an image.")
    question = str(args.get("question") or "Describe this image in detail. Transcribe any text exactly.")
    try:
        from PIL import Image
        with Image.open(p) as im:
            size = f"{im.width}x{im.height} {im.mode}"
    except Exception:
        size = "unknown size"
    return f"[{p.name}, {size}]\n" + ask_vision(env, p.read_bytes(), question)


# ── documents ────────────────────────────────────────────────────────────────────────────────────────

def read_document(p: Path, pages: str | None = None) -> str:
    ext = p.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(p))
        n = len(reader.pages)
        lo, hi = 1, min(n, 30)
        if pages:
            m = re.match(r"\s*(\d+)\s*(?:-\s*(\d+))?", pages)
            if m:
                lo = max(1, int(m.group(1)))
                hi = min(n, int(m.group(2) or m.group(1)))
        parts = [f"--- page {i} ---\n{(reader.pages[i - 1].extract_text() or '').strip()}" for i in range(lo, hi + 1)]
        text = "\n".join(parts)
        if not text.replace("--- page", "").strip(" -\n0123456789"):
            text += "\n(no text layer: this PDF is probably scanned; use view_image on a screenshot of it)"
        return f"{p.name}: {n} pages, showing {lo}-{hi}\n{text}"
    if ext == ".docx":
        import docx
        d = docx.Document(str(p))
        lines = [para.text for para in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                lines.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(lines)
    if ext in (".xlsx", ".xlsm", ".xls", ".csv", ".tsv"):
        import pandas as pd
        if ext in (".csv", ".tsv"):
            sheets = {p.stem: pd.read_csv(p, sep="\t" if ext == ".tsv" else ",")}
        else:
            sheets = pd.read_excel(p, sheet_name=None)
        out = []
        for name, df in sheets.items():
            out.append(f"=== sheet {name}: {df.shape[0]} rows x {df.shape[1]} cols ===")
            out.append(df.head(60).to_string(max_cols=30))
        return "\n".join(out)
    if ext == ".pptx":
        try:
            from pptx import Presentation
        except ImportError:
            raise ToolError("Reading .pptx needs python-pptx (pip install python-pptx).")
        prs = Presentation(str(p))
        out = []
        for i, slide in enumerate(prs.slides, 1):
            texts = [sh.text_frame.text for sh in slide.shapes if getattr(sh, "has_text_frame", False)]
            out.append(f"--- slide {i} ---\n" + "\n".join(texts))
        return "\n".join(out)
    raise ToolError(f"I don't know how to read {ext} files.")


def _t_read_document(args: dict, env: ToolEnv) -> str:
    p = _file(env, args.get("path"))
    if p.suffix.lower() in IMAGE_EXTENSIONS:
        return _t_view_image(args, env)
    return _clip(read_document(p, args.get("pages")))


def _t_create_document(args: dict, env: ToolEnv) -> str:
    p = env.resolve_path(args.get("path"))
    content = str(args.get("content") or "")
    title = str(args.get("title") or "")
    p.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix.lower()
    if ext == ".docx":
        import docx
        d = docx.Document()
        if title:
            d.add_heading(title, 0)
        for line in content.splitlines():
            m = re.match(r"(#{1,4})\s+(.*)", line)
            if m:
                d.add_heading(m.group(2), len(m.group(1)))
            elif re.match(r"\s*[-*]\s+", line):
                d.add_paragraph(re.sub(r"^\s*[-*]\s+", "", line), style="List Bullet")
            elif line.strip():
                d.add_paragraph(line)
        d.save(str(p))
    elif ext in (".xlsx", ".csv"):
        import pandas as pd
        rows = args.get("rows")
        if isinstance(rows, str):
            rows = json.loads(rows)
        if not isinstance(rows, list) or not rows:
            raise ToolError("For a spreadsheet, pass rows: a list of objects (one per row).")
        df = pd.DataFrame(rows)
        df.to_excel(p, index=False) if ext == ".xlsx" else df.to_csv(p, index=False)
    else:
        p.write_text((f"# {title}\n\n" if title and ext == ".md" else "") + content, encoding="utf-8")
    return f"Created {p} ({p.stat().st_size} bytes)."


# ── is it really running? ────────────────────────────────────────────────────────────────────────────

def _t_check_url(args: dict, env: ToolEnv) -> str:
    url = str(args.get("url") or "").strip()
    if not re.match(r"https?://", url):
        url = "http://" + url
    wait = max(0, min(int(args.get("wait", 10) or 0), 120))
    end = time.time() + wait
    last = ""
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Nova"}), timeout=5) as r:
                body = r.read(600).decode("utf-8", "replace")
                return f"UP: {url} answered HTTP {r.status}. First bytes:\n{body}"
        except urllib.error.HTTPError as exc:
            return f"UP (with an error page): {url} answered HTTP {exc.code}."
        except Exception as exc:
            last = str(exc)
        if time.time() >= end:
            return f"DOWN: nothing answered at {url} ({last}). The server is not running: start it with process(action=start) and check its output."
        time.sleep(1)


def _t_check_port(args: dict, env: ToolEnv) -> str:
    host = str(args.get("host") or "127.0.0.1")
    port = int(args.get("port"))
    with socket.socket() as s:
        s.settimeout(2)
        busy = s.connect_ex((host, port)) == 0
    owner = ""
    if busy and sys.platform == "win32":
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10,
                                 creationflags=0x08000000).stdout
            pids = {ln.split()[-1] for ln in out.splitlines() if f":{port} " in ln and "LISTENING" in ln}
            if pids:
                owner = f" (process id {', '.join(sorted(pids))})"
        except Exception:
            pass
    return f"Port {port} on {host} is {'IN USE' + owner if busy else 'free'}."


# ── MCP on demand: hundreds of tools without flooding the prompt ─────────────────────────────────────

def _mcp(env: ToolEnv):
    m = getattr(env, "mcp", None)
    if m is None:
        raise ToolError("MCP isn't running.")
    return m


def _t_mcp_list(args: dict, env: ToolEnv) -> str:
    rows = _mcp(env).snapshot()
    if not rows:
        return f"No MCP servers are configured. Add one with mcp_add (saved to {ROOT / 'mcp.json'})."
    return "\n".join(f"- {r['name']}: {r['status']}{' - ' + r['error'] if r['error'] else ''}, {len(r['tools'])} tools"
                     for r in rows)


def _t_mcp_search(args: dict, env: ToolEnv) -> str:
    words = [w for w in re.split(r"\W+", str(args.get("query") or "").lower()) if w]
    server = str(args.get("server") or "").lower()
    hits = []
    for s in _mcp(env).servers.values():
        if server and s.name.lower() != server:
            continue
        for t in s.tools:
            hay = (t.get("name", "") + " " + (t.get("description") or "")).lower()
            score = sum(hay.count(w) for w in words) if words else 1
            if score:
                hits.append((score, s.name, t))
    hits.sort(key=lambda h: -h[0])
    if not hits:
        return "No MCP tool matched. Try other words, or mcp_list to see servers."
    out = []
    for _, sname, t in hits[: int(args.get("limit", 8) or 8)]:
        schema = t.get("inputSchema") or {}
        props = {k: (v.get("type", "any") if isinstance(v, dict) else "any") for k, v in (schema.get("properties") or {}).items()}
        out.append(f"* {sname}/{t.get('name')}: {(t.get('description') or '')[:300]}\n  args {json.dumps(props)} required {schema.get('required') or []}")
    return "\n".join(out) + "\nCall one with mcp_call(server, tool, arguments)."


def _t_mcp_call(args: dict, env: ToolEnv) -> str:
    m = _mcp(env)
    sname = str(args.get("server") or "")
    tool = str(args.get("tool") or "")
    if "/" in tool and not sname:
        sname, tool = tool.split("/", 1)
    s = m.servers.get(sname)
    if s is None:
        raise ToolError(f"No MCP server called {sname}. Known: {', '.join(m.servers) or 'none'}.")
    if s.status != "connected":
        raise ToolError(f"MCP server {sname} is {s.status}: {s.error}")
    arguments = args.get("arguments") or {}
    if isinstance(arguments, str):
        arguments = json.loads(arguments or "{}")
    return _clip(s.call(tool, arguments))


def _t_mcp_add(args: dict, env: ToolEnv) -> str:
    name = re.sub(r"[^\w-]", "_", str(args.get("name") or "")).strip("_")
    if not name:
        raise ToolError("Give the server a name.")
    spec: dict = {}
    if args.get("url"):
        spec["url"] = str(args["url"])
    elif args.get("command"):
        spec["command"] = str(args["command"])
        a = args.get("args") or []
        spec["args"] = json.loads(a) if isinstance(a, str) and a.strip().startswith("[") else (a.split() if isinstance(a, str) else list(a))
    else:
        raise ToolError("Pass command (+args) for a local server, or url for a remote one.")
    path = ROOT / "mcp.json"
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {"mcpServers": {}}
    data.setdefault("mcpServers", {})[name] = spec
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    from forgecli.nova.tools.mcp import McpServer, _expand
    m = _mcp(env)
    server = McpServer(name, _expand(spec))
    m.servers[name] = server
    server.start()
    if server.status != "connected":
        return f"Saved {name} to mcp.json but it did not connect: {server.error}"
    reg = getattr(env, "registry", None)
    if reg is not None:
        for t in server.tools:
            tool = m._wrap(server, t, bool(spec.get("trust")))
            reg.tools[tool.name] = tool
    return f"Connected MCP server {name}: {len(server.tools)} tools. Use mcp_search to find them."


# ── web, video, media ────────────────────────────────────────────────────────────────────────────────

def _t_browser_screenshot(args: dict, env: ToolEnv) -> str:
    url = str(args.get("url") or "")
    if not re.match(r"https?://|file:", url):
        url = "http://" + url
    out = _out(env, args.get("path"), env.workspace / "screenshots" / f"page_{int(time.time())}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception:
            browser = pw.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": int(args.get("width", 1366)), "height": int(args.get("height", 860))})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(url, wait_until="networkidle", timeout=30_000)
        title = page.title()
        text = page.inner_text("body")[:1500]
        page.screenshot(path=str(out), full_page=bool(args.get("full_page")))
        browser.close()
    result = f"Saved {out}. Title: {title!r}. Console errors: {errors[:5] or 'none'}.\nVisible text:\n{text}"
    if args.get("question"):
        result += "\nWhat it looks like: " + ask_vision(env, out.read_bytes(), str(args["question"]))
    return result


def _t_youtube_transcript(args: dict, env: ToolEnv) -> str:
    import tempfile
    import yt_dlp
    url = str(args.get("url") or "")
    lang = str(args.get("lang") or "en")
    with tempfile.TemporaryDirectory() as tmp:
        opts = {"skip_download": True, "writesubtitles": True, "writeautomaticsub": True, "subtitleslangs": [lang, lang + ".*"],
                "subtitlesformat": "vtt", "outtmpl": str(Path(tmp) / "v"), "quiet": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = sorted(Path(tmp).glob("*.vtt"))
        if not files:
            raise ToolError("That video has no subtitles I can fetch.")
        lines, prev = [], ""
        for ln in files[0].read_text(encoding="utf-8", errors="replace").splitlines():
            ln = re.sub(r"<[^>]+>", "", ln).strip()
            if not ln or "-->" in ln or ln.startswith(("WEBVTT", "Kind:", "Language:")) or ln == prev:
                continue
            lines.append(ln)
            prev = ln
    return _clip(f"{info.get('title')} ({info.get('duration')}s, {info.get('uploader')})\n" + " ".join(lines), 20_000)


def _t_ffmpeg(args: dict, env: ToolEnv) -> str:
    from forgecli.nova.tools.builtin import run_capture
    a = args.get("args")
    argv = json.loads(a) if isinstance(a, str) and a.strip().startswith("[") else (str(a).split() if isinstance(a, str) else list(a or []))
    r = run_capture(["ffmpeg", "-hide_banner", "-y", *argv], cwd=env.workspace, timeout=int(args.get("timeout", 600)))
    return f"ffmpeg exit {r.returncode}\n" + _clip((_text(r.stdout) + _text(r.stderr))[-4000:])


def _t_image_edit(args: dict, env: ToolEnv) -> str:
    from PIL import Image, ImageFilter, ImageOps
    p = _file(env, args.get("path"))
    im = Image.open(p)
    for op in [o.strip() for o in str(args.get("ops") or "").split(";") if o.strip()]:
        name, _, rest = op.partition(" ")
        nums = [int(float(x)) for x in re.findall(r"-?\d+(?:\.\d+)?", rest)]
        if name == "resize":
            im = im.resize((nums[0], nums[1]) if len(nums) > 1 else (nums[0], int(im.height * nums[0] / im.width)))
        elif name == "crop":
            im = im.crop(tuple(nums[:4]))
        elif name == "rotate":
            im = im.rotate(-nums[0], expand=True)
        elif name in ("grayscale", "greyscale"):
            im = ImageOps.grayscale(im)
        elif name == "flip":
            im = ImageOps.mirror(im)
        elif name == "blur":
            im = im.filter(ImageFilter.GaussianBlur(nums[0] if nums else 2))
        elif name == "thumbnail":
            im.thumbnail((nums[0], nums[-1]))
        else:
            raise ToolError(f"Unknown image op {name}. Use resize W H; crop L T R B; rotate DEG; grayscale; flip; blur R; thumbnail W H.")
    out = _out(env, args.get("out"), p)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() in (".jpg", ".jpeg") and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.save(out)
    return f"Saved {out} ({im.width}x{im.height})."


def _t_run_python(args: dict, env: ToolEnv) -> str:
    from forgecli.nova.tools.builtin import run_capture
    code = str(args.get("code") or "")
    scratch = env.workspace / ".nova_tmp"
    scratch.mkdir(exist_ok=True)
    f = scratch / f"snippet_{int(time.time() * 1000)}.py"
    f.write_text(code, encoding="utf-8")
    try:
        r = run_capture([sys.executable, str(f)], cwd=env.workspace, timeout=int(args.get("timeout", 120)))
    finally:
        f.unlink(missing_ok=True)
    return f"exit {r.returncode}\n" + _clip(_text(r.stdout) + (("\n[stderr]\n" + _text(r.stderr)) if r.stderr else ""))


def _t_notify(args: dict, env: ToolEnv) -> str:
    title = str(args.get("title") or "Nova").replace("'", "''")
    msg = str(args.get("message") or "").replace("'", "''")
    script = ("Add-Type -AssemblyName System.Windows.Forms; $n=New-Object System.Windows.Forms.NotifyIcon; "
              "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
              f"$n.ShowBalloonTip(8000,'{title}','{msg}','Info'); Start-Sleep 8; $n.Dispose()")
    subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script], creationflags=0x08000000)
    return "Notification shown."


def power_tools(cfg: dict) -> list[Tool]:
    s = {"type": "string"}
    i = {"type": "integer"}
    b = {"type": "boolean"}
    o = {"type": "object"}
    return [
        Tool("view_image", "Look at an image file (png/jpg/webp/...) and describe it or answer a question about it; also reads text in it (OCR). "
             "Use for screenshots, photos, diagrams, UI mockups, scanned pages.", {"path": s, "question": s}, ["path"],
             _t_view_image, "files", readonly=True, untrusted=True),
        Tool("read_document", "Read a PDF, Word .docx, Excel .xlsx/.csv or PowerPoint .pptx (or an image). pages='3-7' for PDFs.",
             {"path": s, "pages": s}, ["path"], _t_read_document, "files", readonly=True, untrusted=True),
        Tool("create_document", "Create a .docx (markdown-style content), .xlsx/.csv (rows = list of objects), or .md/.txt file.",
             {"path": s, "title": s, "content": s, "rows": {"type": "array", "items": o}}, ["path"], _t_create_document, "files"),
        Tool("check_url", "Check that a web server really answers (e.g. after starting one). wait = seconds to keep retrying. "
             "Always do this before telling the user a site is running.", {"url": s, "wait": i}, ["url"], _t_check_url, "system", readonly=True),
        Tool("check_port", "Is a TCP port in use, and by which process id.", {"port": i, "host": s}, ["port"], _t_check_port, "system", readonly=True),
        Tool("mcp_list", "List connected MCP servers (e.g. rea = REA reverse-engineering toolkit) and their tool counts.", {}, [],
             _t_mcp_list, "agent", readonly=True),
        Tool("mcp_search", "Search the tools of connected MCP servers (REA has 200+ reverse-engineering tools: analyze apps, binaries, "
             "web bundles, APKs, decompile...). Returns names and arguments to use with mcp_call.", {"query": s, "server": s, "limit": i},
             ["query"], _t_mcp_search, "agent", readonly=True),
        Tool("mcp_call", "Call any MCP server tool found with mcp_search. arguments is an object.", {"server": s, "tool": s, "arguments": o},
             ["server", "tool"], _t_mcp_call, "agent", untrusted=True, policy="confirm",
             summarize=lambda a, e: f"Run {a.get('tool')} on MCP server {a.get('server')} with {json.dumps(a.get('arguments') or {}, ensure_ascii=False)[:300]}?"),
        Tool("mcp_add", "Connect a new MCP server and save it to mcp.json: command+args for a local one (e.g. npx -y pkg) or url for a remote one.",
             {"name": s, "command": s, "args": {"type": "array", "items": s}, "url": s}, ["name"], _t_mcp_add, "agent", policy="confirm",
             summarize=lambda a, e: f"Add MCP server {a.get('name')} ({a.get('url') or (str(a.get('command')) + ' ' + ' '.join(a.get('args') or []))})?"),
        Tool("browser_screenshot", "Open a URL in a headless browser, save a screenshot, return title, console errors and visible text; "
             "add question to have the screenshot looked at. Use to verify web apps you built.",
             {"url": s, "path": s, "full_page": b, "question": s, "width": i, "height": i}, ["url"], _t_browser_screenshot, "apps", untrusted=True),
        Tool("youtube_transcript", "Get the transcript (subtitles) of a YouTube video.", {"url": s, "lang": s}, ["url"],
             _t_youtube_transcript, "apps", readonly=True, untrusted=True),
        Tool("ffmpeg", "Run ffmpeg (video/audio convert, trim, merge, extract audio, gif...) in the workspace. args = list of ffmpeg arguments without 'ffmpeg'.",
             {"args": {"type": "array", "items": s}, "timeout": i}, ["args"], _t_ffmpeg, "system", policy="confirm",
             summarize=lambda a, e: "Run ffmpeg " + " ".join(map(str, a.get("args") or []))[:300] + "?"),
        Tool("image_edit", "Edit an image: ops separated by ';' e.g. 'resize 800; grayscale; crop 0 0 400 300; rotate 90; blur 2'. out = new file (default overwrite).",
             {"path": s, "ops": s, "out": s}, ["path", "ops"], _t_image_edit, "files"),
        Tool("run_python", "Run a Python snippet (full Python with pandas, PIL, requests...) in the workspace and return its output. "
             "Good for calculations, data work, quick scripts.", {"code": s, "timeout": i}, ["code"], _t_run_python, "system",
             untrusted=True, policy="confirm", summarize=lambda a, e: "Run this Python?\n" + str(a.get("code", ""))[:600]),
        Tool("notify", "Show a Windows notification.", {"title": s, "message": s}, ["message"], _t_notify, "system"),
    ]
