"""Looking at a local web page the way a person would: a headless browser loads it, collects errors
and takes a screenshot. Playwright is used when installed (it also catches console and page errors);
otherwise Microsoft Edge or Chrome, which most computers already have, take the screenshot from the
command line. Only localhost pages are checked (tools.t_browser_check enforces that)."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import time

WIDTH, HEIGHT = 1280, 860


def shot_path(root: pathlib.Path) -> pathlib.Path:
    """Screenshots go in Forge's own folder, never the user's project."""
    d = pathlib.Path(os.environ.get("FORGE_HOME", pathlib.Path.home() / ".forge")) / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    olds = sorted(d.glob("page-*.png"))
    for old in olds[:-20]:            # keep the last 20
        try:
            old.unlink()
        except OSError:
            pass
    return d / f"page-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid() % 1000}.png"


def playwright_check(url: str, shot: pathlib.Path) -> str | None:
    """Status, title, errors and a full-page screenshot; None when Playwright isn't installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    console_errors, page_errors = [], []
    status = title = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
                page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: page_errors.append(str(e)))
                resp = page.goto(url, wait_until="networkidle", timeout=20_000)
                status = resp.status if resp else "no response"
                title = page.title()
                try:
                    page.screenshot(path=str(shot), full_page=True, timeout=10_000)
                except Exception:
                    pass
            finally:
                browser.close()
    except Exception as exc:
        return f"Browser check failed: {type(exc).__name__}: {exc}"
    lines = [f"HTTP {status}", f"title: {title or '(empty)'}",
             f"console errors: {len(console_errors)}", f"page errors: {len(page_errors)}"]
    lines += [f"console: {e[:300]}" for e in console_errors[:5]]
    lines += [f"page error: {e[:300]}" for e in page_errors[:5]]
    return "\n".join(lines)


def find_browser() -> str | None:
    env = os.environ.get("FORGE_BROWSER")
    if env and pathlib.Path(env).exists():
        return env
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    pf = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
    for base in filter(None, pf):
        for r in rel:
            p = pathlib.Path(base) / r
            if p.exists():
                return str(p)
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return mac if pathlib.Path(mac).exists() else None


def cli_screenshot(url: str, shot: pathlib.Path) -> str:
    """Screenshot with Edge/Chrome's own headless mode (no console errors this way)."""
    exe = find_browser()
    if not exe:
        return ("Skipped: no Playwright and no Edge/Chrome found to take a screenshot. "
                "Install Playwright (pip install playwright; playwright install chromium) for full checks.")
    with tempfile.TemporaryDirectory(prefix="forge-browser-") as prof:
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               "--hide-scrollbars", f"--user-data-dir={prof}", f"--window-size={WIDTH},{HEIGHT}",
               "--virtual-time-budget=4000", f"--screenshot={shot}", url]
        try:
            subprocess.run(cmd, capture_output=True, timeout=45)
        except Exception as exc:
            return f"Browser check failed: {type(exc).__name__}: {exc}"
    if shot.exists() and shot.stat().st_size > 0:
        return (f"Loaded {url} in headless {pathlib.Path(exe).stem} (Playwright not installed, so console "
                "errors were not collected).")
    return f"Browser check failed: {pathlib.Path(exe).stem} did not produce a screenshot of {url}."
