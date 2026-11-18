"""Nova's own browser: a visible Chromium window it drives with Playwright.

It keeps a persistent profile in data/browser-profile, so once you log in to Instagram, Gmail, etc. in that
window, Nova stays logged in. The agent reads a page with `browser_snapshot` (numbered interactive
elements plus the page text), acts with `browser_click` / `browser_type` / `browser_press` / `browser_scroll`,
and pulls data out with `browser_scrape`.

Playwright's sync API must stay on the thread that started it, so every call goes through one worker thread.
"""
from __future__ import annotations

import json
import queue
import threading
from forgecli.nova.core.config import ROOT
from forgecli.nova.tools.base import Tool, ToolEnv, ToolError

PROFILE = ROOT / "data" / "browser-profile"

# Marks every visible interactive element with data-nova-ref and returns them with their labels.
_SNAPSHOT_JS = r"""
(max) => {
  const sel = 'a[href],button,input,textarea,select,summary,[role=button],[role=link],[role=tab],[role=menuitem],' +
              '[role=checkbox],[role=option],[role=textbox],[role=combobox],[role=switch],[contenteditable=""],' +
              '[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  document.querySelectorAll('[data-nova-ref]').forEach(e => e.removeAttribute('data-nova-ref'));
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > innerHeight * 3) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
                   el.title || el.alt || el.name || el.getAttribute('href') || '').replace(/\s+/g, ' ').trim().slice(0, 90);
    const tag = el.tagName.toLowerCase();
    const kind = el.getAttribute('role') || (tag === 'input' ? 'input:' + (el.type || 'text') : tag);
    if (!label && !/input|textarea|select|textbox|combobox/.test(kind)) continue;
    n += 1; el.setAttribute('data-nova-ref', String(n));
    out.push({ref: n, kind, label, inView: r.top >= 0 && r.bottom <= innerHeight,
              href: tag === 'a' ? el.href : undefined});
    if (n >= max) break;
  }
  return out;
}
"""


class _Browser:
    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ctx = None
        self._pw = None
        self.headless = False
        self.current = None  # the tab Nova is working in

    def _loop(self) -> None:
        while True:
            fn, box, done = self._q.get()
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - handed back to the caller's thread
                box["error"] = exc
            done.set()

    def call(self, fn, timeout: float = 90):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="nova-browser", daemon=True)
            self._thread.start()
        box: dict = {}
        done = threading.Event()
        self._q.put((fn, box, done))
        if not done.wait(timeout):
            raise ToolError("The browser didn't respond in time.")
        if "error" in box:
            err = box["error"]
            if isinstance(err, ToolError):
                raise err
            raise ToolError(f"Browser error: {str(err).splitlines()[0][:300]}")
        return box.get("value")

    # everything below runs on the browser thread
    def page(self):
        if self._ctx is not None:
            try:
                if self.current is not None and not self.current.is_closed():
                    return self.current
                pages = [p for p in self._ctx.pages if not p.is_closed()]
                self.current = pages[-1] if pages else self._ctx.new_page()
                return self.current
            except Exception:
                self._ctx = None
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ToolError("Playwright isn't installed: pip install playwright && python -m playwright install chromium") from None
        PROFILE.mkdir(parents=True, exist_ok=True)
        if self._pw is None:
            self._pw = sync_playwright().start()
        kw = dict(headless=self.headless, args=["--start-maximized"], no_viewport=True)
        try:  # prefer the installed Chrome (fewer "unsupported browser" walls); fall back to Playwright's Chromium
            self._ctx = self._pw.chromium.launch_persistent_context(str(PROFILE), channel="chrome", **kw)
        except Exception:
            self._ctx = self._pw.chromium.launch_persistent_context(str(PROFILE), **kw)
        self.current = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self.current

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        finally:
            self._ctx = None
            self.current = None


BROWSER = _Browser()


def _snapshot(page, max_items: int = 80, text_chars: int = 3000) -> str:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    items = page.evaluate(_SNAPSHOT_JS, max_items)
    body = page.evaluate("() => (document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n')") or ""
    rows = [f"[{i['ref']}] {i['kind']} \"{i['label']}\"" + ("" if i["inView"] else " (below, scroll)") for i in items]
    head = f"Page: {page.title()[:120]} - {page.url}\n"
    text = body.strip()[:text_chars] + ("\n... (more text: browser_scrape)" if len(body) > text_chars else "")
    return head + "Interactive elements (use the number as ref):\n" + "\n".join(rows) + "\n\nVisible text:\n" + text


def _locator(page, args: dict):
    if args.get("ref") not in (None, ""):
        loc = page.locator(f"[data-nova-ref=\"{int(float(args['ref']))}\"]")
        if loc.count() == 0:
            raise ToolError("That ref is stale (the page changed). Call browser_snapshot again.")
        return loc.first
    if args.get("selector"):
        return page.locator(str(args["selector"])).first
    if args.get("text"):
        t = str(args["text"])
        for loc in (page.get_by_role("button", name=t), page.get_by_role("link", name=t), page.get_by_label(t),
                    page.get_by_placeholder(t), page.get_by_text(t, exact=True), page.get_by_text(t)):
            try:
                if loc.count():
                    return loc.first
            except Exception:
                continue
        raise ToolError(f"Nothing on the page matches '{t}'. Call browser_snapshot to see what's there.")
    raise ToolError("Say which element: ref (number from browser_snapshot), text, or a CSS selector.")


def _t_browser_open(args: dict, env: ToolEnv) -> str:
    url = str(args.get("url") or "").strip()
    if url and "://" not in url:
        url = "https://" + url

    def go():
        page = BROWSER.page()
        if args.get("new_tab"):
            page = page.context.new_page()
            BROWSER.current = page
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
        page.bring_to_front()
        return _snapshot(page)
    return BROWSER.call(go)


def _t_browser_snapshot(args: dict, env: ToolEnv) -> str:
    return BROWSER.call(lambda: _snapshot(BROWSER.page(), int(args.get("max") or 80), int(args.get("text_chars") or 3000)))


def _t_browser_click(args: dict, env: ToolEnv) -> str:
    def go():
        page = BROWSER.page()
        loc = _locator(page, args)
        loc.scroll_into_view_if_needed(timeout=5000)
        loc.click(timeout=10000, button=str(args.get("button") or "left"),
                  click_count=2 if args.get("double") else 1)
        page.wait_for_timeout(int(float(args.get("wait") or 1.0) * 1000))
        return "Clicked. Now:\n" + _snapshot(page, 60, 1500)
    return BROWSER.call(go)


def _t_browser_type(args: dict, env: ToolEnv) -> str:
    def go():
        page = BROWSER.page()
        text = str(args.get("text") or "")
        if any(args.get(k) not in (None, "") for k in ("ref", "selector", "into")):
            loc = _locator(page, {**args, "text": args.get("into")} if args.get("into") else args)
            loc.click(timeout=10000)
            if args.get("clear", True):
                loc.fill("") if loc.evaluate("e => 'value' in e") else page.keyboard.press("Control+A")
        page.keyboard.type(text, delay=15)
        if args.get("submit"):
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
        return f"Typed {len(text)} characters" + (" and pressed Enter. Now:\n" + _snapshot(page, 60, 1500) if args.get("submit") else ".")
    return BROWSER.call(go)


def _t_browser_press(args: dict, env: ToolEnv) -> str:
    def go():
        page = BROWSER.page()
        for k in str(args.get("keys") or "").split():
            page.keyboard.press(k)
        page.wait_for_timeout(600)
        return f"Pressed {args.get('keys')}."
    return BROWSER.call(go)


def _t_browser_scroll(args: dict, env: ToolEnv) -> str:
    def go():
        page = BROWSER.page()
        d = str(args.get("direction") or "down").lower()
        if d in ("top", "bottom"):
            page.evaluate("d => window.scrollTo(0, d === 'top' ? 0 : document.body.scrollHeight)", d)
        else:
            px = int(float(args.get("pages") or 1) * 800) * (-1 if d == "up" else 1)
            page.mouse.wheel(0, px)
        page.wait_for_timeout(900)
        return "Scrolled. Now:\n" + _snapshot(page, 60, 2000)
    return BROWSER.call(go)


def _t_browser_scrape(args: dict, env: ToolEnv) -> str:
    what = str(args.get("what") or "text").lower()
    selector = str(args.get("selector") or "")
    limit = int(args.get("limit") or 20000)

    def go():
        page = BROWSER.page()
        if what == "links":
            data = page.evaluate("s => [...document.querySelectorAll(s || 'a[href]')].map(a => [a.innerText.trim().slice(0,100), a.href])"
                                 ".filter(x => x[1])", selector or "a[href]")
            out = "\n".join(f"- {t or '(no text)'}: {h}" for t, h in data)
        elif what == "tables":
            out = page.evaluate("""() => [...document.querySelectorAll('table')].map((t, i) => '## table ' + (i + 1) + '\\n' +
                [...t.rows].map(r => [...r.cells].map(c => c.innerText.trim().replace(/\\s+/g, ' ')).join(' | ')).join('\\n')).join('\\n\\n')""")
        elif what == "html":
            out = page.locator(selector).first.inner_html() if selector else page.content()
        elif what == "images":
            data = page.evaluate("() => [...document.images].filter(i => i.naturalWidth > 60).map(i => [i.alt, i.currentSrc || i.src])")
            out = "\n".join(f"- {a or '(no alt)'}: {s}" for a, s in data)
        else:
            if selector:
                out = "\n\n".join(page.locator(selector).all_inner_texts())
            else:
                out = page.evaluate("() => document.body.innerText")
        out = out or "(nothing found)"
        if args.get("save_to"):
            dest = env.resolve_path(str(args["save_to"]))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(out, encoding="utf-8")
            return f"Saved {len(out)} characters from {page.url} to {dest}.\n" + out[:1500]
        return f"From {page.url}:\n" + out[:limit] + ("\n... (cut; pass save_to to keep everything)" if len(out) > limit else "")
    return BROWSER.call(go)


def _t_browser_eval(args: dict, env: ToolEnv) -> str:
    def go():
        result = BROWSER.page().evaluate(str(args.get("script") or ""))
        return json.dumps(result, ensure_ascii=False, default=str)[:20000]
    return BROWSER.call(go)


def _t_browser_tabs(args: dict, env: ToolEnv) -> str:
    def go():
        BROWSER.page()
        pages = [p for p in BROWSER._ctx.pages if not p.is_closed()]
        if args.get("switch") not in (None, ""):
            i = int(float(args["switch"])) - 1
            if not 0 <= i < len(pages):
                raise ToolError("No tab with that number.")
            pages[i].bring_to_front()
            BROWSER.current = pages[i]
            return "Switched. Now:\n" + _snapshot(pages[i], 60, 1500)
        if args.get("back"):
            page = BROWSER.page()
            page.go_back()
            return "Went back. Now:\n" + _snapshot(page, 60, 1500)
        return "\n".join(f"[{i}] {p.title()[:80]} - {p.url}" for i, p in enumerate(pages, 1))
    return BROWSER.call(go)


def browser_tools(cfg: dict) -> list[Tool]:
    s, n, b = {"type": "string"}, {"type": "number"}, {"type": "boolean"}
    return [
        Tool("browser_open", "Open a URL in Nova's own Chrome window (stays logged in between sessions) and return the "
             "page's numbered interactive elements and text. Use for anything on the web: Instagram, YouTube, forms, "
             "dashboards, scraping. new_tab=true keeps the current page.", {"url": s, "new_tab": b}, [], _t_browser_open,
             "browser", untrusted=True),
        Tool("browser_snapshot", "Re-read the current page in Nova's browser: numbered elements + visible text.",
             {"max": n, "text_chars": n}, [], _t_browser_snapshot, "browser", readonly=True, untrusted=True),
        Tool("browser_click", "Click an element in Nova's browser by ref (number from the snapshot), visible text, or CSS "
             "selector.", {"ref": n, "text": s, "selector": s, "double": b, "button": s, "wait": n}, [], _t_browser_click,
             "browser", untrusted=True),
        Tool("browser_type", "Type into Nova's browser: target a field by ref / into (its label or placeholder) / selector, "
             "or type into whatever is focused. submit=true presses Enter.",
             {"text": s, "ref": n, "into": s, "selector": s, "submit": b, "clear": b}, ["text"], _t_browser_type,
             "browser", untrusted=True),
        Tool("browser_press", "Press keys in Nova's browser, space-separated Playwright names: Enter, Escape, PageDown, "
             "Control+A, ArrowDown.", {"keys": s}, ["keys"], _t_browser_press, "browser", untrusted=True),
        Tool("browser_scroll", "Scroll Nova's browser: direction down/up/top/bottom, pages (default 1). Returns the new "
             "snapshot - repeat to load infinite feeds.", {"direction": s, "pages": n}, [], _t_browser_scroll, "browser",
             readonly=True, untrusted=True),
        Tool("browser_scrape", "Extract data from the current page: what=text|links|tables|images|html, optional CSS "
             "selector, save_to a workspace file for big results.", {"what": s, "selector": s, "save_to": s, "limit": n},
             [], _t_browser_scrape, "browser", readonly=True, untrusted=True),
        Tool("browser_eval", "Run JavaScript in the current page and return the JSON result (advanced scraping).",
             {"script": s}, ["script"], _t_browser_eval, "browser", untrusted=True, policy="confirm",
             summarize=lambda a, e: "Run this script in the browser page:\n" + str(a.get("script", ""))[:300]),
        Tool("browser_tabs", "List Nova's browser tabs; switch=N to bring one forward; back=true to go back.",
             {"switch": n, "back": b}, [], _t_browser_tabs, "browser", untrusted=True),
    ]
