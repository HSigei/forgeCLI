"""Build docs/docs.html — the Forge guide as its own page — from README.md.

    python tools/build_docs.py        (needs:  pip install markdown)

README "## " headings become sidebar groups; each <details><summary><b>Name</b> — note</summary>
block becomes a nested sidebar entry. Re-run after editing the README, then deploy docs/.
"""
from __future__ import annotations

import html
import pathlib
import re

import markdown

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = "https://github.com/HSigei/forgeCLI"
SKIP = {"Licence and terms"}          # lives in the footer instead


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>|`", "", s).lower()).strip("-")


def version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "forgecli/__init__.py").read_text(encoding="utf-8"))
    return m.group(1) if m else ""


def prepare(md: str) -> str:
    md = md.replace("\r\n", "\n")
    md = md[md.index("\n## "):]                                   # drop the GitHub banner block
    # <details><summary><b>X</b> — note</summary> ... </details>  ->  ### X + lead paragraph
    def det(m: re.Match) -> str:
        title, note, body = m.group(1).strip(), (m.group(2) or "").strip(" —-—"), m.group(3)
        lead = f'\n<p class="lead">{note}</p>\n' if note else ""
        return f"\n### {title}\n{lead}\n{body.strip()}\n"
    md = re.sub(r"<details>\s*<summary><b>(.*?)</b>(.*?)</summary>(.*?)</details>", det, md, flags=re.S)
    md = re.sub(r"\]\(docs/(media/[^)]+)\)", r"](\1)", md)
    md = md.replace('src="docs/media/', 'src="media/')
    # repo-relative links (LICENSE, SECURITY.md, …) go to GitHub
    md = re.sub(r"\]\((?!https?:|#|media/|mailto:)([^)\s]+)\)", lambda m: f"]({REPO}/blob/main/{m.group(1)})", md)
    return md


def sections(md: str) -> list[tuple[str, str]]:
    parts = re.split(r"^## (.+)$", md, flags=re.M)[1:]
    return [(parts[i].strip(), parts[i + 1]) for i in range(0, len(parts), 2) if parts[i].strip() not in SKIP]


def build() -> str:
    md = prepare((ROOT / "README.md").read_text(encoding="utf-8"))
    conv = markdown.Markdown(extensions=["tables", "fenced_code", "md_in_html"])
    nav, body = [], []
    for title, text in sections(md):
        sid = slug(title)
        subs = re.findall(r"^### (.+)$", text, flags=re.M)
        conv.reset()
        h = conv.convert(text)
        h = re.sub(r"<h3>(.+?)</h3>", lambda m: f'<h3 id="{sid}--{slug(m.group(1))}"><a class="anchor" href="#{sid}--{slug(m.group(1))}">#</a>{m.group(1)}</h3>', h)
        h = re.sub(r'<a href="(https?://[^"]+)"', r'<a href="\1" target="_blank" rel="noopener"', h)
        h = re.sub(r"<table>", '<div class="tbl"><table>', h).replace("</table>", "</table></div>")
        body.append(f'<section class="doc" id="{sid}"><h2><a class="anchor" href="#{sid}">#</a>{html.escape(title)}</h2>\n{h}\n</section>')
        kids = "".join(f'<li><a href="#{sid}--{slug(s)}">{re.sub(r"<[^>]+>", "", s)}</a></li>' for s in subs)
        nav.append(f'<li class="grp"><a class="top" href="#{sid}">{html.escape(title)}</a>'
                   + (f'<ul class="sub">{kids}</ul>' if kids else "") + "</li>")
    return TEMPLATE.replace("{{NAV}}", "\n".join(nav)).replace("{{BODY}}", "\n".join(body)).replace("{{VER}}", version())


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Forge Docs</title>
<meta name="description" content="The Forge guide: install, first run, commands, failover, approvals, skills, team mode, sandbox, GitHub, MCP, config and environment variables.">
<meta name="theme-color" content="#0a0912">
<link rel="icon" href="media/favicon.svg" type="image/svg+xml">
<link rel="icon" href="media/favicon-32.png" sizes="32x32" type="image/png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Silkscreen&family=Geist:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0912; --bg-2: #0e0c18; --panel: #14111f; --line: #221d36; --line-2: #3b3460;
    --text: #ece9f7; --muted: #a19bbd; --dim: #6f6990; --accent: #ff4fd8; --cyan: #3ef0ff;
    --body: "Geist", system-ui, sans-serif; --mono: "JetBrains Mono", ui-monospace, monospace; --pixel: "Silkscreen", monospace;
    --top: 60px; --side: 280px; --toc: 220px;
  }
  * { box-sizing: border-box; }
  html { scroll-padding-top: calc(var(--top) + 20px); scroll-behavior: smooth; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.7 var(--body); -webkit-font-smoothing: antialiased; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }

  /* top bar */
  .bar { position: fixed; inset: 0 0 auto 0; height: var(--top); z-index: 30; display: flex; align-items: center; gap: 18px; padding: 0 22px;
    background: #0a0912e6; backdrop-filter: blur(10px); border-bottom: 1px solid var(--line); }
  .logo { font: 400 18px var(--pixel); letter-spacing: .18em; color: var(--accent); text-shadow: 0 0 14px #ff4fd880; }
  .logo:hover { text-decoration: none; }
  .crumb { font: 13px var(--mono); color: var(--dim); }
  .crumb b { color: var(--muted); font-weight: 600; }
  .bar .sp { flex: 1; }
  .bar nav a { color: var(--muted); font-size: 14px; margin-left: 18px; }
  .bar nav a:hover { color: var(--text); text-decoration: none; }
  .bar .btn { background: var(--accent); color: #14001a; font-weight: 600; padding: 7px 14px; border-radius: 8px; }
  .menu { display: none; background: none; border: 1px solid var(--line-2); color: var(--text); border-radius: 8px; padding: 5px 10px; font: 13px var(--mono); cursor: pointer; }

  /* sidebar — GSAP-docs style: groups, then indented children on a guide line */
  .side { position: fixed; top: var(--top); bottom: 0; left: 0; width: var(--side); overflow-y: auto; padding: 22px 14px 40px 22px;
    border-right: 1px solid var(--line); background: var(--bg-2); scrollbar-width: thin; scrollbar-color: var(--line-2) transparent; }
  .side .search { width: 100%; margin-bottom: 18px; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--line-2); background: var(--bg);
    color: var(--text); font: 13px var(--mono); outline: none; }
  .side .search:focus { border-color: var(--accent); }
  .side .label { font: 11px var(--mono); color: var(--dim); letter-spacing: .14em; text-transform: uppercase; margin: 4px 0 8px 10px; }
  .side ul { list-style: none; margin: 0; padding: 0; }
  .side .grp { margin-bottom: 2px; }
  .side a { display: block; color: var(--muted); font-size: 14px; line-height: 1.35; padding: 6px 10px; border-radius: 6px; }
  .side a:hover { color: var(--text); background: #ffffff08; text-decoration: none; }
  .side a.top { color: var(--text); font-weight: 500; }
  .side .sub { margin: 2px 0 8px 14px; padding-left: 10px; border-left: 1px solid var(--line-2); }
  .side .sub a { font-size: 13.5px; padding: 5px 10px; }
  .side a.on { color: var(--accent); background: #ff4fd814; }
  .side .sub a.on { box-shadow: -11px 0 0 -9px var(--accent); }
  .side .hide { display: none; }

  /* content */
  main { margin-left: var(--side); margin-right: var(--toc); padding: calc(var(--top) + 40px) 56px 120px; }
  .inner { max-width: 780px; margin: 0 auto; }
  .eyebrow { font: 12px var(--mono); color: var(--cyan); letter-spacing: .2em; text-transform: uppercase; }
  h1 { font-size: clamp(34px, 4vw, 48px); line-height: 1.1; margin: 10px 0 14px; letter-spacing: -.02em; }
  .intro { color: var(--muted); font-size: 18px; margin: 0 0 18px; }
  .doc { padding-top: 34px; margin-top: 34px; border-top: 1px solid var(--line); }
  h2 { font-size: 30px; line-height: 1.2; margin: 0 0 16px; letter-spacing: -.01em; position: relative; }
  h3 { font-size: 21px; margin: 40px 0 10px; position: relative; color: var(--text); }
  .anchor { position: absolute; left: -24px; color: var(--dim); opacity: 0; font-weight: 400; }
  h2:hover .anchor, h3:hover .anchor { opacity: 1; text-decoration: none; }
  p, li { color: #d6d2e8; }
  .lead { color: var(--muted); margin-top: -4px; font-style: italic; }
  ul, ol { padding-left: 22px; }
  li { margin: 4px 0; }
  li::marker { color: var(--accent); }
  strong { color: var(--text); }
  code { font: .88em var(--mono); background: #ffffff0d; border: 1px solid var(--line); padding: .08em .36em; border-radius: 5px; color: #ffd1f4; }
  pre { background: #07060d; border: 1px solid var(--line); border-left: 2px solid var(--accent); border-radius: 8px; padding: 16px 18px; overflow-x: auto; }
  pre code { background: none; border: 0; padding: 0; color: #ece9f7; font-size: 13.5px; line-height: 1.6; }
  .tbl { overflow-x: auto; margin: 16px 0; }
  table { border-collapse: collapse; width: 100%; font-size: 14.5px; }
  th { text-align: left; font: 600 12px var(--mono); color: var(--cyan); letter-spacing: .08em; text-transform: uppercase; padding: 8px 12px; border-bottom: 1px solid var(--line-2); }
  td { padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: top; color: #d6d2e8; }
  tr:hover td { background: #ffffff05; }
  img { max-width: 100%; height: auto; border-radius: 8px; }
  blockquote { margin: 16px 0; padding: 4px 18px; border-left: 2px solid var(--cyan); color: var(--muted); }
  hr { border: 0; border-top: 1px solid var(--line); margin: 30px 0; }
  .pager { display: flex; justify-content: space-between; gap: 12px; margin-top: 60px; padding-top: 20px; border-top: 1px solid var(--line); font-size: 14px; }
  .foot { margin-top: 40px; color: var(--dim); font-size: 13px; }

  /* right: on this page */
  .toc { position: fixed; top: var(--top); right: 0; width: var(--toc); bottom: 0; overflow-y: auto; padding: 40px 22px 40px 10px; }
  .toc .label { font: 11px var(--mono); color: var(--dim); letter-spacing: .14em; text-transform: uppercase; margin-bottom: 10px; }
  .toc a { display: block; color: var(--dim); font-size: 13px; padding: 4px 0 4px 12px; border-left: 1px solid var(--line); }
  .toc a:hover { color: var(--text); text-decoration: none; }
  .toc a.on { color: var(--accent); border-left-color: var(--accent); }

  @media (max-width: 1200px) { .toc { display: none; } main { margin-right: 0; } }
  @media (max-width: 860px) {
    .side { transform: translateX(-100%); transition: transform .25s; z-index: 25; width: 300px; }
    body.open .side { transform: none; box-shadow: 30px 0 60px #000a; }
    main { margin-left: 0; padding: calc(var(--top) + 28px) 18px 90px; }
    .menu { display: inline-block; }
    .crumb, .bar nav a:not(.btn) { display: none; }
    .anchor { display: none; }
  }
</style>
</head>
<body>
<header class="bar">
  <button class="menu" type="button" aria-label="Open the menu" onclick="document.body.classList.toggle('open')">menu</button>
  <a class="logo" href="./">FORGE</a>
  <span class="crumb">/ <b>docs</b> · v{{VER}}</span>
  <span class="sp"></span>
  <nav><a href="./">Home</a><a href="https://github.com/HSigei/forgeCLI" target="_blank" rel="noopener">GitHub</a><a href="https://github.com/HSigei/forgeCLI/releases/latest" target="_blank" rel="noopener">Releases</a><a href="https://github.com/HSigei/forgeCLI/issues/new/choose" target="_blank" rel="noopener">Feedback</a><a class="btn" href="./#install">Install</a></nav>
</header>

<aside class="side" id="side">
  <input class="search" id="q" type="search" placeholder="Filter the docs…" aria-label="Filter the docs">
  <div class="label">Guide</div>
  <ul id="nav">
{{NAV}}
  </ul>
</aside>

<main>
  <div class="inner">
    <div class="eyebrow">documentation · v{{VER}}</div>
    <h1>Forge Docs</h1>
    <p class="intro">Everything about Forge: installing it, the commands at the prompt, how failover works, approvals, skills, team mode, the sandbox, GitHub, MCP servers, and every setting.</p>
{{BODY}}
    <p class="foot">© 2026 HK Labs · <a href="https://github.com/HSigei/forgeCLI/issues/new/choose" target="_blank" rel="noopener">Feedback</a> · <a href="https://github.com/HSigei/forgeCLI/blob/main/LICENSE" target="_blank" rel="noopener">Apache-2.0</a> · <a href="https://github.com/HSigei/forgeCLI/blob/main/TERMS.md" target="_blank" rel="noopener">Terms</a> · <a href="https://github.com/HSigei/forgeCLI/blob/main/PRIVACY.md" target="_blank" rel="noopener">Privacy</a> · <a href="https://github.com/HSigei/forgeCLI/blob/main/DISCLAIMER.md" target="_blank" rel="noopener">Disclaimer</a> · <a href="https://github.com/HSigei/forgeCLI/edit/main/README.md" target="_blank" rel="noopener">Edit this page</a></p>
  </div>
</main>

<aside class="toc" aria-label="On this section"><div class="label">On this section</div><div id="toc"></div></aside>

<script>
(function () {
  var links = [].slice.call(document.querySelectorAll("#nav a"));
  var secs = [].slice.call(document.querySelectorAll(".doc"));
  var toc = document.getElementById("toc");
  var current = null;

  function buildToc(sec) {
    if (sec === current) return; current = sec;
    var hs = sec ? sec.querySelectorAll("h3[id]") : [];
    toc.innerHTML = [].map.call(hs, function (h) {
      return '<a href="#' + h.id + '">' + h.textContent.replace(/^#/, "") + "</a>";
    }).join("") || '<a href="#' + (sec ? sec.id : "") + '">' + (sec ? sec.querySelector("h2").textContent.replace(/^#/, "") : "") + "</a>";
  }

  // scroll spy: highlight the section and sub-entry you are reading
  function spy() {
    var y = window.scrollY + 120, sec = secs[0], sub = null;
    secs.forEach(function (s) { if (s.offsetTop <= y) sec = s; });
    if (sec) [].forEach.call(sec.querySelectorAll("h3[id]"), function (h) { if (h.getBoundingClientRect().top + window.scrollY <= y) sub = h; });
    var ids = [sec && sec.id, sub && sub.id];
    links.forEach(function (a) { a.classList.toggle("on", ids.indexOf(a.getAttribute("href").slice(1)) >= 0); });
    buildToc(sec);
    [].forEach.call(toc.querySelectorAll("a"), function (a) { a.classList.toggle("on", sub && a.getAttribute("href") === "#" + sub.id); });
  }
  window.addEventListener("scroll", spy, { passive: true });
  spy();

  // close the mobile menu after picking a page
  links.forEach(function (a) { a.addEventListener("click", function () { document.body.classList.remove("open"); }); });

  // sidebar filter
  document.getElementById("q").addEventListener("input", function () {
    var q = this.value.trim().toLowerCase();
    [].forEach.call(document.querySelectorAll("#nav .grp"), function (g) {
      var any = false;
      [].forEach.call(g.querySelectorAll(".sub li"), function (li) {
        var hit = !q || li.textContent.toLowerCase().indexOf(q) >= 0; li.classList.toggle("hide", !hit); any = any || hit;
      });
      var self = !q || g.querySelector(".top").textContent.toLowerCase().indexOf(q) >= 0;
      if (self) [].forEach.call(g.querySelectorAll(".sub li"), function (li) { li.classList.remove("hide"); });
      g.classList.toggle("hide", !(self || any));
    });
  });
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    out = ROOT / "docs" / "docs.html"
    out.write_text(build(), encoding="utf-8", newline="\n")
    print("wrote", out)
