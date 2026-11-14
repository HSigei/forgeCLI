"""Command-line entry point: styled interactive chat, or a one-shot prompt."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Protocol

from rich.console import Console, Group
from rich.text import Text

from . import __version__, keyvault, parallel, roster
from . import sessions as sess
from . import skills as skillmod
from .agent import Agent
from .config import APP_DIR, HOME_DIR, apply_prefs, load_prefs, load_settings, save_prefs
from .sessions import Session
from rich import box
from rich.padding import Padding
from rich.panel import Panel

from . import ui as uimod
from .textsafe import clean as _clean_text
from .ui import UI, console, ktok, logo

COMMANDS = {
    "/help": "show commands and shortcuts",
    "/new": "start a new conversation (the current one stays saved)",
    "/undo": "restore the last file change and rewind its conversation step",
    "/plan": "/plan <request> — plan read-only, then execute automatically",
    "/sessions": "list saved conversations",
    "/resume": "/resume <# or id> — continue a saved conversation",
    "/rename": "/rename <title> — rename this conversation",
    "/delete": "/delete <# or id> — delete a saved conversation",
    "/model": "/model [provider] <model> — change model (no args: show current)",
    "/models": "/models [provider] [filter] — list models a provider offers",
    "/provider": "/provider <name|auto> — use only one provider, or auto failover",
    "/skills": "list skills",
    "/skills sync": "copy Claude Code skills into Forge",
    "/skill": "/skill <name> [request] — run a skill · /skill new <name> — create one",
    "/parallel": "/parallel <task> || @worker <task> — run tasks at once · /parallel on|off — let forge split work",
    "/team": "/team on|off — a coordinator model hands work to the models best at it · /team lead <worker>",
    "/city": "/city — watch the pixel city act out every state (/city off · /city on · /city calm)",
    "/roster": "/roster — what each model is good at · /roster bench — measure them · /roster edit",
    "/status": "providers, models and per-key state",
    "/doctor": "MCP/API, model, key and skill usage; dormant connectors and token savings",
    "/keys": "/keys — your API keys (masked) · /keys add|test|remove <provider> <n>|file <n> <provider>|provider <name> <url> <model>",
    "/setup": "add API keys step by step (opens each provider's key page, tests every key)",
    "/approve": "/approve smart|ask|auto — when to ask before changes (smart: only risky ones)",
    "/balance": "tokens used, rate limits left and credits per provider",
    "/cost": "tokens this session, and the money they would cost on paid plans",
    "/ads": "small sponsor line under the working status: /ads status · off · on · reset",
    "/commands": "list project slash commands from .forge/commands/",
    "/memory": "/memory [user|project] — show saved notes",
    "/review": "review the current git diff for correctness issues",
    "/github": "/github status · login · logout — GitHub sign-in (device code, no gh needed)",
    "/yes": "toggle auto-approve for everything",
    "/verbose": "toggle the key rotation / failover log",
    "/update": "check for a newer Forge and install it: /update · /update check",
    "/feedback": "report a bug or suggest an idea: /feedback · /feedback idea · /feedback <what happened> · /feedback off",
    "/exit": "quit",
}
BOM = chr(0xFEFF)
APPROVAL_TEXT = {"smart": "smart — edits in this folder and safe commands just run; risky ones ask",
                 "ask": "ask — every write, edit and command asks first",
                 "auto": "AUTO — nothing asks (careful)"}
RELEASES = "https://github.com/HSigei/forgeCLI/releases/latest"

ALIASES = {"/clear": "/new", "/quit": "/exit", "/q": "/exit", "/m": "/model", "/s": "/sessions",
           "/r": "/resume", "/?": "/help", "/bug": "/feedback"}

SETUP = f"""No API keys configured.

Create {APP_DIR / '.env'} (or {HOME_DIR / '.env'}) with, for example:

  FORGE_PROVIDERS=groq,nvidia,openrouter
  GROQ_API_KEYS=gsk_key1,gsk_key2
  OPENROUTER_API_KEYS=sk-or-key1

See .env.example for every option."""


# Commands that lose or change something. A guess is never run for these: Forge asks first.
RISKY_COMMANDS = {"/exit", "/quit", "/q", "/delete", "/new", "/clear", "/undo", "/yes", "/update"}


def _ratio(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def suggest_command(typed: str, builtin, others=()) -> dict:
    """What to do with a slash command that does not exist.

    builtin: Forge's own command names (and aliases), others: skills and project commands.
    Returns {"run": name} when one built-in is clearly meant and is safe to run on a guess,
    {"ask": [names]} with up to three suggestions otherwise, or {} when nothing is close.

    - a prefix means the shortest command it starts ("/mod" -> /model, not /models)
    - a typo means the closest by similarity, if it beats the runner-up clearly
    - risky commands, skills and project commands are suggested, never run on a guess"""
    word = typed.lower()
    pool = [n for n in dict.fromkeys(builtin) if " " not in n]
    extra = [n for n in dict.fromkeys(others) if n not in pool]

    def safe(name: str) -> bool:
        target = ALIASES.get(name, name)
        return name not in RISKY_COMMANDS and target not in RISKY_COMMANDS

    # 1) a prefix of one command, or of a command and its longer relatives
    if len(word) >= 3:
        pre = sorted((n for n in pool if n.startswith(word)), key=len)
        if pre and all(n.startswith(pre[0]) for n in pre):
            return {"run": pre[0], "how": "prefix"} if safe(pre[0]) else {"ask": [pre[0]]}
        if pre:
            return {"ask": pre[:3]}
    # 2) a typo: rank everything by similarity
    ranked = sorted(((_ratio(word, n), n) for n in pool + extra), reverse=True)
    top = ranked[0] if ranked else (0.0, "")
    # /model and /models are one family: being close to both is no reason to hesitate
    second = max((r for r, n in ranked[1:] if not n.startswith(top[1]) and not top[1].startswith(n)),
                 default=0.0)
    if top[0] >= 0.8 and top[0] - second >= 0.08 and top[1] in pool and safe(top[1]) and len(word) >= 3:
        return {"run": top[1], "how": "close"}
    near = [n for r, n in ranked if r >= 0.55][:3]
    if len(word) >= 3:
        near += [n for n in extra + pool if n.startswith(word) and n not in near]
    return {"ask": near[:3]} if near else {}


def closest_word(word: str, options) -> str:
    """The one option a typed word clearly means (prefix or small typo), else ''."""
    word = word.lower()
    opts = list(options)
    if word in opts:
        return word
    pre = [o for o in opts if o.startswith(word)] if len(word) >= 2 else []
    if len(pre) == 1:
        return pre[0]
    ranked = sorted(((_ratio(word, o), o) for o in opts), reverse=True)
    if ranked and ranked[0][0] >= 0.75 and (len(ranked) < 2 or ranked[0][0] - ranked[1][0] >= 0.08):
        return ranked[0][1]
    return ""


def resolve_model_choice(arg: str, providers, cache: dict, active: str = "") -> dict:
    """Work out which provider and model a `/model` argument means.

    providers: objects with .name, .model and .models; cache: provider name -> model ids seen with
    /models; active: the provider in use now (wins ties when two providers offer the same id).
    Returns {"provider", "model", "note"} on success, or {"error"} / {"choices"} when it cannot
    decide. The provider comes from the model name, so `/model deepseek-chat` never ends up
    renaming some other provider's model."""
    import difflib
    arg = (arg or "").strip()
    by_name = {p.name.lower(): p for p in providers}

    def known(p) -> list:
        return list(dict.fromkeys([p.model, *p.models, *cache.get(p.name, [])]))

    def match_in(p, wanted: str):
        """(model, note) for `wanted` among p's known ids: exact, then one close match, else typed as is."""
        ids = known(p)
        low = {i.lower(): i for i in ids}
        if wanted.lower() in low:
            return low[wanted.lower()], ""
        subs = [i for i in ids if wanted.lower() in i.lower()]
        if len(subs) == 1:
            return subs[0], f"matched {subs[0]}"
        close = difflib.get_close_matches(wanted, ids, n=1, cutoff=0.75)
        if close and not subs:
            return close[0], f"matched {close[0]}"
        return wanted, ""

    if not arg:
        return {"error": "no model given"}
    first, _, rest = arg.partition(" ")
    rest = rest.strip()
    # "deepseek:deepseek-chat" / "deepseek deepseek-chat" -> explicit provider
    if ":" in first and first.split(":", 1)[0].lower() in by_name and not rest:
        pname, _, wanted = arg.partition(":")
        p = by_name[pname.strip().lower()]
        model, note = match_in(p, wanted.strip())
        return {"provider": p, "model": model, "note": note}
    if first.lower() in by_name:
        p = by_name[first.lower()]
        if not rest:
            return {"provider": p, "model": p.model, "note": "", "switch_only": True}
        model, note = match_in(p, rest)
        return {"provider": p, "model": model, "note": note}

    # a model on its own: find who offers it
    low_arg = arg.lower()
    owners: dict = {}                                    # model id -> providers that list it
    for p in providers:
        for i in known(p):
            owners.setdefault(i, []).append(p)

    def pick_owner(model_id: str):
        ps = owners[model_id]
        return next((p for p in ps if p.name == active), ps[0])

    exact = [i for i in owners if i.lower() == low_arg]
    if exact:
        return {"provider": pick_owner(exact[0]), "model": exact[0], "note": ""}
    subs = [i for i in owners if low_arg in i.lower()]
    if len(subs) == 1:
        return {"provider": pick_owner(subs[0]), "model": subs[0], "note": f"matched {subs[0]}"}
    if len(subs) > 1:
        return {"choices": [(pick_owner(i).name, i) for i in subs[:8]], "more": max(0, len(subs) - 8)}
    close = difflib.get_close_matches(arg, list(owners), n=3, cutoff=0.7)
    if len(close) == 1:
        return {"provider": pick_owner(close[0]), "model": close[0], "note": f"matched {close[0]}"}
    if len(close) > 1:
        return {"choices": [(pick_owner(i).name, i) for i in close]}
    # unknown id: a provider named in it ("deepseek-chat" -> deepseek) owns it, else the active one
    hint = next((p for n, p in by_name.items() if n in low_arg), None)
    prov = hint or by_name.get(active.lower()) or (providers[0] if providers else None)
    if not prov:
        return {"error": "no provider configured"}
    return {"provider": prov, "model": arg, "note": "", "unlisted": True}


class App:
    def __init__(self, agent: Agent, ui: UI):
        self.agent = agent
        self.ui = ui
        self.router = agent.router
        self.prefs = load_prefs()
        self.router.pinned = self.prefs.get("pinned") if self.router.get(self.prefs.get("pinned") or "") else None
        self.router.strict = bool(self.router.pinned and self.prefs.get("pinned_strict"))
        self.model_cache: dict[str, list[str]] = {}

    # header / footer --------------------------------------------------------------
    def model_label(self) -> str:
        if self.agent.team and self.agent.coordinator:
            return f"team · lead {self.agent.coordinator.model}"
        lead = self.router.lead()
        if not lead:
            return "no provider"
        return f"{lead.name} · {lead.model}"

    @staticmethod
    def fresh_page() -> None:
        """Start Forge on a clean page of its own: the screen and the scrollback are cleared, so
        whatever ran before (pip output, an old PS prompt) is gone and scrolling up shows only
        Forge. FORGE_FRESH_PAGE=off keeps the old screen."""
        if not console.is_terminal or os.environ.get("FORGE_FRESH_PAGE", "on").lower() in ("off", "0", "no"):
            return
        try:
            # erase the screen, erase the scrollback, cursor home (Windows Terminal, conhost with
            # VT on, xterm-likes). Done twice: some terminals push the screen into the scrollback
            # when it is erased, and the second ESC[3J removes that copy too.
            console.file.write("[2J[3J[H[3J")
            console.file.flush()
        except Exception:
            pass

    def banner(self) -> None:
        r = self.router
        live = [p for p in r.providers if p.pool.keys]
        chain = " → ".join(p.name for p in (r._ordered() if r.pinned else live))
        mode = self.agent.tools.mode

        def fact(label: str, value: str, style: str = "") -> Text:
            return Text(f"{label:<10}", style="#8a7aa8").append(value, style=style or "#e6e0f5")

        facts = [
                 fact("model", self.model_label()),
                 fact("routing", ("pinned " if r.pinned else "") + chain),
                 fact("folder", os.getcwd()),
                 fact("approval", mode, "bold #ff4fd8" if mode != "auto" else "warn")
                 .append({"smart": "  risky steps ask first", "ask": "  every change asks first",
                          "auto": "  nothing asks (careful)"}[mode], style="faint")]
        try:
            from . import update as upd
            newest = upd.cached()
        except Exception:
            newest = None
        if newest:
            facts.append(fact("update", f"{__version__} → {newest} is out  ", "warn").append("/update", style="accent"))
        if not self.agent.session.messages:
            last = next((s for s in sess.list_sessions()
                         if os.path.normcase(s["cwd"]) == os.path.normcase(os.getcwd())), None)
            if last:
                title = re.sub(r"\s*\[Forge:.*", "", (last["title"] or "").splitlines()[0] if last["title"] else "").strip()
                facts.append(fact("last", (title or "(untitled)")[:44])
                             .append(f"  {sess.ago(last['updated'])} · ", style="faint")
                             .append("/resume", style="accent"))
        from rich.rule import Rule
        head = uimod._gradient("forge", "#ff4fd8", "#ff7ae6").append("  ")
        head.append(f"v{__version__}", style="bold #3ef0ff").append("  multi-model coding agent · failover · team mode")
        hint = Text()
        for i, (key, what) in enumerate([("/help", "commands"), ("/model", "switch"), ("/balance", "limits"),
                                         ("ctrl+j", "new line"), ("esc", "stop")]):
            if i:
                hint.append("    ")
            hint.append(key, style="bold #ff4fd8").append(" " + what, style="muted")
        for t in (head, *facts):
            t.no_wrap, t.overflow = True, "ellipsis"        # long paths and titles end in …, never wrap
        pw = min(console.width - 2, 172)                   # as wide as the picture above it
        panel = Panel(Group(head, Rule(style="#3a1d63"), *facts, hint),
                      box=box.ROUNDED, border_style="#7b2cff",
                      padding=(1, 3) if pw >= 100 else (0, 2), width=pw)
        console.print()
        on = console.is_terminal and os.environ.get("FORGE_BANNER", "on").lower() not in ("off", "0", "no")
        # The start screen (splash.py): the cub straight on the terminal, FORGE and the arrow
        # details beside it, no card. FORGE_BANNER=card shows the framed city card instead,
        # FORGE_BANNER=scene the rooftop-tiger picture with the panel.
        if on and os.environ.get("FORGE_BANNER", "").lower() != "scene":
            try:
                from . import splash
                tagline = Text(f"v{__version__}", style="bold #3ef0ff").append(
                    "  multi-model coding agent · failover · team mode", style="#f3e8ff")
                arrows = [Text(splash.ARROW, style="bold #ff4fd8").append_text(t) for t in facts]
                draw = splash.render_card if os.environ.get("FORGE_BANNER", "").lower() == "card" else splash.render
                if draw(console, [tagline, *arrows, hint]):
                    console.print()
                    return
            except Exception:
                pass
        # The picture: tiger on the rooftop, FORGE / BUILD · DEPLOY · SCALE over the midnight city
        # (scene.py). Real pixels where the terminal shows sixel images, half-blocks elsewhere.
        # It is sized so the picture, the info panel, the message box and the footer all fit in
        # the window together; when there's no room for even the smallest size it is left out.
        if on and console.width >= 60:
            try:
                from . import art as artmod, scene
                panel_h = len(console.render_lines(panel, console.options.update(width=pw), pad=False))
                room = console.size.height - panel_h - 2 - 3          # blank lines, box, footer
                sixel_ok = artmod.graphics(console) == "sixel"
                for cols in sorted({console.width - 2, *[c for c, *_ in scene._load()]}, reverse=True):
                    if cols > console.width - 2:
                        continue
                    pic = None if sixel_ok else Text.from_ansi(scene.halfblock(cols))
                    rows = scene.rows_for(cols) if sixel_ok else len(pic.plain.split(chr(10)))
                    if rows <= room:
                        if sixel_ok:
                            sys.stdout.write(" " + scene.sixel(cols) + chr(10))
                            sys.stdout.flush()
                        else:
                            console.print(Padding(pic, (0, 0, 0, 1 + max(0, (console.width - 2 - pic.cell_len) // 2))))
                        break
            except Exception:
                pass
        console.print(panel)
        console.print()

    def toolbar(self):
        a = self.agent
        title = (a.session.title or "new conversation")[:32]
        mode = {"smart": "asks for risky only", "ask": "asks every change", "auto": "AUTO-APPROVE"}[a.tools.mode]
        bits = [("class:tb.accent", f" ◆ {self.model_label()} "),
                ("class:tb.sep", "│"), ("class:tb", f" ctx {ktok(a.ctx_tokens)} "),
                ("class:tb.sep", "│"), ("class:tb", f" used {ktok(a.used_in + a.used_out)} tok ")]
        lead = self.router.lead()
        if lead and lead.pool.keys:
            left = lead.remaining()
            if left.get("req"):
                bits.append(("class:tb", f"· {left['req'][0]} req left "))
        extra = (" · parallel" if a.parallel else "") + (" · team" if a.team else "")
        bits += [("class:tb.sep", "│"), ("class:tb.warn" if a.tools.mode == "auto" else "class:tb", f" {mode}{extra} "),
                 ("class:tb.sep", "│"), ("class:tb.dim", f" {title} ")]
        # One row, always: a footer wider than the window would wrap and grow, pushing the box up.
        room, out = max(10, _term_cols(getattr(self, "_pt_app", None)) - 1), []
        for cls, txt in bits:
            if room <= 0:
                break
            if len(txt) > room:
                txt = txt[:max(0, room - 1)] + "…"
            out.append((cls, txt))
            room -= len(txt)
        return out

    # commands ---------------------------------------------------------------------
    def handle(self, text: str) -> bool:
        """Run a slash command. Returns False to quit."""
        parts = text.split(maxsplit=1)
        cmd = ALIASES.get(parts[0].lower(), parts[0].lower())
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "/exit":
            return False
        if cmd == "/update" and arg.lower() not in ("check", "status"):
            return self.cmd_update(arg)
        if cmd == "/review":
            self.run("Review the current git diff for concrete bugs and regressions. "
                     "Start with git status and git diff, inspect relevant surrounding code, "
                     "and report findings only; do not modify files.")
            return True
        fn = getattr(self, "cmd_" + cmd[1:], None)
        if fn:
            fn(arg)
        elif cmd[1:] in self.agent.skills:
            self.run(self.agent.skill_prompt(cmd[1:], arg), echo=f"skill: {cmd[1:]}")
        elif self._project_command(cmd[1:], arg):
            pass
        else:
            skills = [f"/{n}" for n in self.agent.skills]
            project = pathlib.Path.cwd() / ".forge" / "commands"
            project_cmds = [f"/{f.stem}" for f in project.glob("*.md")] if project.is_dir() else []
            sug = suggest_command(cmd, [*COMMANDS, *ALIASES], [*skills, *project_cmds])
            if sug.get("run"):
                guess = sug["run"]
                self.ui.info(f"assuming {ALIASES.get(guess, guess)} — you typed {cmd}")
                return self.handle(guess + (" " + arg if arg else ""))
            if sug.get("ask"):
                options = " · ".join(sug["ask"])
                self.ui.error(f"unknown command {cmd} — did you mean {options}?")
            else:
                self.ui.error(f"unknown command {cmd} — /help lists them")
        return True

    def cmd_help(self, _):
        rows = [[k, v] for k, v in COMMANDS.items()]
        if self.agent.skills:
            rows.append(["/<skill>", "shortcut for /skill <skill> [request]"])
        self.ui.table("commands", ["", ""], rows, ["accent", "muted"])
        self.ui.table("keys", ["", ""], [
            ["enter", "send"], ["ctrl+j  (alt+enter)", "new line, or end a line with \\"],
            ["tab", "complete commands, models, skills, sessions"], ["↑ / ↓", "input history"],
            ["alt+v  (ctrl+v)", "paste text, copied files (as paths) or a screenshot (saved, path inserted)"],
            ["while Forge works", "keep typing: enter adds your message to the task"],
            ["esc / ctrl+c", "stop the current task (ctrl+c with text clears it)"],
            ["ctrl+d", "quit"]], ["accent", "muted"])

    def cmd_new(self, _):
        self.agent.reset()
        console.clear()
        self.banner()
        self.ui.ok("new conversation")

    def cmd_undo(self, _):
        result = self.agent.undo_last()
        (self.ui.error if result.startswith("Error:") else self.ui.ok)(result)

    def cmd_plan(self, arg):
        if not arg:
            self.ui.error("Use /plan <request>.")
            return
        self.work(lambda: self.agent.run_plan(arg), stopped="⏹ planning stopped")

    def cmd_sessions(self, arg):
        items = sess.list_sessions()
        if arg:
            items = [s for s in items if arg.lower() in (s["title"] + s["cwd"]).lower()]
        if not items:
            self.ui.info("no saved conversations yet")
            return
        rows = []
        for i, s in enumerate(items[:25], 1):
            here = "●" if s["id"] == self.agent.session.id else ""
            rows.append([f"{i}{here}", s["title"] or "(untitled)", s["count"], sess.ago(s["updated"]),
                         _short(s["cwd"])])
        self.ui.table("sessions", ["#", "title", "msgs", "updated", "folder"], rows,
                      ["accent", None, "muted", "muted", "faint"])
        self.ui.info("/resume <#> to continue one")

    def pick_session(self) -> str | None:
        """Numbered list of recent conversations (this folder's first); returns the chosen id."""
        items = sess.list_sessions()
        here = os.path.normcase(os.getcwd())
        items = sorted(items, key=lambda s: os.path.normcase(s["cwd"]) != here)[:15]   # stable: newest first
        items = [s for s in items if s["id"] != self.agent.session.id]
        if not items:
            self.ui.info("no saved conversations yet")
            return None
        rows = [[str(i), s["title"] or "(untitled)", s["count"], sess.ago(s["updated"]),
                 "this folder" if os.path.normcase(s["cwd"]) == here else _short(s["cwd"])]
                for i, s in enumerate(items, 1)]
        self.ui.table("pick a conversation to continue", ["#", "title", "msgs", "updated", "folder"], rows,
                      ["accent", None, "muted", "muted", "faint"])
        try:
            ans = console.input("  [muted]number (enter = 1, q = cancel)[/] [accent]›[/] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if ans in ("q", "quit", "n", "no"):
            return None
        if not ans:
            ans = "1"
        if ans.isdigit() and 1 <= int(ans) <= len(items):
            return items[int(ans) - 1]["id"]
        return sess.find(ans)

    def cmd_resume(self, arg):
        if arg:
            sid = sess.find(arg)
        elif sys.stdin.isatty():
            sid = self.pick_session()
            if not sid:
                return
        else:
            sid = sess.latest_for(os.getcwd())
        if not sid:
            self.ui.error("no matching session — see /sessions" if arg else "no saved session for this folder")
            return
        s = Session.load(sid)
        self.agent.load(s)
        console.clear()
        self.banner()
        self.ui.rule(s.title or sid)
        self.ui.replay(s.messages)
        if os.path.normcase(s.cwd) != os.path.normcase(os.getcwd()):
            self.ui.warn(f"this conversation started in {s.cwd}; you are now in {os.getcwd()}")
        self.ui.ok(f"resumed {s.title or sid}")

    def cmd_rename(self, arg):
        if not arg:
            self.ui.error("usage: /rename <title>")
            return
        self.agent.session.title = arg[:80]
        self.agent.save()
        self.ui.ok(f"renamed to {arg[:80]}")

    def cmd_delete(self, arg):
        sid = sess.find(arg) if arg else None
        if not sid:
            self.ui.error("usage: /delete <# or id> (see /sessions)")
            return
        if sid == self.agent.session.id:
            self.ui.error("that is the current conversation — /new first")
            return
        sess.delete(sid)
        self.ui.ok(f"deleted {sid}")

    def cmd_model(self, arg):
        r = self.router
        if not arg:
            rows = [[p.name, p.model, "◆" if p is r.lead() else ""] for p in r.providers]
            self.ui.table("models", ["provider", "model", "first"], rows, ["accent", None, "accent"])
            self.ui.info("routing: " + (f"only {r.pinned} · {r.get(r.pinned).model}" + (" (exact model)" if r.strict else "")
                                        if r.pinned and r.get(r.pinned) else "automatic failover"))
            self.ui.info("/model <model> picks the provider from the model name · /model <provider> [model] · "
                         "/models <provider> lists choices · /provider auto turns failover back on")
            return
        active = r.pinned or (r.lead().name if r.lead() else "")
        got = resolve_model_choice(arg, r.providers, self.model_cache, active)
        if got.get("error"):
            self.ui.error(got["error"])
            return
        if got.get("choices"):
            rows = [[prov, mid] for prov, mid in got["choices"]]
            self.ui.table(f"models matching '{arg}'", ["provider", "model"], rows, ["accent", None])
            self.ui.info("be more specific: /model <provider> <model>" +
                         (f" ({got['more']} more not shown)" if got.get("more") else ""))
            return
        prov, model = got["provider"], got["model"]
        prov.model = model
        self.prefs.setdefault("models", {})[prov.name] = model
        # The choice is a promise: this provider and this model, nothing else, until /provider auto.
        r.pinned, r.strict, r.sticky = prov.name, True, None
        self.prefs["pinned"], self.prefs["pinned_strict"] = prov.name, True
        save_prefs(self.prefs)
        if got.get("note"):
            self.ui.info(got["note"])
        self.ui.ok(f"using {prov.name} · {model} only — no failover; /provider auto to undo")
        known = self.model_cache.get(prov.name)
        if got.get("unlisted") and known and model not in known:
            self.ui.warn(f"{model!r} is not in {prov.name}'s model list — check the spelling with /models {prov.name}")
        elif got.get("unlisted") and not known:
            self.ui.info(f"{model!r} is not one Forge has seen for {prov.name}; /models {prov.name} lists what it offers")
        self.ui.info("the model must support tool calling, or Forge can't touch files or run commands")

    def cmd_models(self, arg):
        bits = arg.split(maxsplit=1)
        prov = self.router.get(bits[0].lower()) if bits else None
        flt = (bits[1] if prov and len(bits) > 1 else ("" if prov else arg)).lower()
        prov = prov or self.router.lead()
        if not prov:
            self.ui.error("no provider configured")
            return
        self.ui.start_spinner(f"asking {prov.name} for its models")
        try:
            ids = prov.list_models()
        except RuntimeError as e:
            self.ui.error(f"could not list models: {e}")
            return
        finally:
            self.ui.stop_spinner()
        self.model_cache[prov.name] = ids
        shown = [i for i in ids if flt in i.lower()]
        rows = [["◆" if i == prov.model else "", i] for i in shown[:60]]
        self.ui.table(f"{prov.name} models" + (f" matching '{flt}'" if flt else ""), ["", "id"], rows, ["accent", None])
        more = f" (showing 60 of {len(shown)}; add a filter)" if len(shown) > 60 else ""
        self.ui.info(f"/model {prov.name} <id> to switch · tab completes ids{more}")

    def cmd_provider(self, arg):
        name = arg.lower()
        if not name:
            self.ui.info(f"routing: {'pinned to ' + self.router.pinned if self.router.pinned else 'automatic failover'}"
                         f" · options: auto, {', '.join(p.name for p in self.router.providers)}")
            return
        names = [p.name for p in self.router.providers]
        if name not in ("auto", *names):
            guess = closest_word(name, ["auto", *names])
            if guess:
                self.ui.info(f"assuming {guess} — you typed {name}")
                name = guess
        if name == "auto":
            self.router.pinned, self.router.strict = None, False
        elif self.router.get(name):
            self.router.pinned, self.router.strict = name, False
        else:
            self.ui.error(f"no provider {name!r} (configured: {', '.join(p.name for p in self.router.providers)})")
            return
        self.prefs["pinned"] = self.router.pinned
        self.prefs["pinned_strict"] = self.router.strict
        save_prefs(self.prefs)
        self.ui.ok("automatic failover across all providers" if name == "auto"
                   else f"using only {name} ({self.router.get(name).model}); /provider auto to undo")

    def cmd_skills(self, arg):
        """/skills → Forge's own + a count per Claude plugin · /skills <words> → search all · /skills all"""
        from . import skills as skillmod
        self.agent.reload_skills()
        sk = self.agent.skills
        if not sk:
            self.ui.info("no skills yet — /skill new <name> creates one in ~/.forge/skills")
            return
        arg = (arg or "").strip()
        if arg == "sync":
            self.ui.info(f"copying Claude Code's skills from {skillmod.CLAUDE_DIR} …")
            try:
                info = skillmod.sync()
            except Exception as e:
                self.ui.error(f"sync failed, kept the old copy: {e}")
                return
            self.agent.reload_skills()
            n = len({s.path for s in self.agent.skills.values() if s.source == "claude"})
            self.ui.info(f"{n} skills from {len(info['plugins'])} plugins copied to {skillmod.LIBRARY} — "
                         "they now work even without Claude Code")
            return
        if arg and arg != "all":
            hits = skillmod.find(sk, arg, 15)
            rows = [[s.name, _clip(s.description, 110), s.origin or s.source] for s in hits]
            self.ui.table(f"skills matching '{arg}'", ["name", "description", "from"], rows,
                          ["accent", None, "faint"])
            return
        unique = {s.path: s for s in sk.values()}.values()
        if arg == "all":
            rows = sorted([s.name, _clip(s.description, 90), s.origin or s.source] for s in unique)
            self.ui.table(f"all {len(rows)} skills", ["name", "description", "from"], rows,
                          ["accent", None, "faint"])
            return
        rows = [[s.name, _clip(s.description, 110), s.source] for s in skillmod.listed(sk)]
        self.ui.table("Forge skills", ["name", "description", "source"], rows, ["accent", None, "faint"])
        bundled = sorted(s.name for s in unique if s.source == "bundled")
        if bundled:
            self.ui.info(f"plus {len(bundled)} engineering skills built into Forge (no Claude Code needed): "
                         + ", ".join(bundled))
        counts: dict[str, int] = {}
        for s in unique:
            if s.source == "claude":
                counts[s.origin] = counts.get(s.origin, 0) + 1
        if counts:
            synced = skillmod.synced_info().get("synced", "")
            where = (f"Forge's own copy in {skillmod.LIBRARY} (synced {synced.replace('T', ' ')[:16]})"
                     if synced else f"read from {skillmod.CLAUDE_DIR}")
            self.ui.info(f"plus {sum(counts.values())} from Claude Code, {where}: " +
                         ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
        self.ui.info("/skills <words> searches · /skills all lists everything · /skill <name> [request] "
                     "runs one · /skills sync refreshes the copy · the model finds and uses them itself too")

    def cmd_skill(self, arg):
        bits = arg.split(maxsplit=1)
        if not bits:
            return self.cmd_skills("")
        if bits[0] == "new":
            name = bits[1].strip() if len(bits) > 1 else ""
            project = name.endswith(" --project")
            name = name.removesuffix(" --project").strip()
            try:
                path = skillmod.create(name, project=project)
            except ValueError as e:
                self.ui.error(f"could not create skill: {e}")
                return
            self.agent.reload_skills()
            self.ui.ok(f"created {path}")
            self.ui.info("edit that file, then run it with /" + name)
            return
        self.agent.reload_skills()
        name = bits[0].lower()
        if name not in self.agent.skills:
            self.ui.error(f"no skill {name!r} — /skills lists them")
            return
        self.run(self.agent.skill_prompt(name, bits[1] if len(bits) > 1 else ""), echo=f"skill: {name}")

    def cmd_status(self, _):
        rows = []
        for p in self.router.providers:
            for i, (key, ready, state, ok, fail) in enumerate(p.pool.status_rows()):
                rows.append([p.name if i == 0 else "", p.model if i == 0 else "", key,
                             Text(state, style="ok" if ready else "warn"), ok, fail])
        self.ui.table("keys", ["provider", "model", "key", "state", "ok today", "failed"], rows,
                      ["accent", "muted", None, None, "muted", "muted"])
        resting = []
        now = time.time()
        for p in self.router.providers:
            for m, waits in p.pool.model_benches().items():
                resting.append(f"{p.name} · {m.split('/')[-1]}: {len(waits)} key(s), back in {int(min(waits))}s")
            for (pn, m), until in self.router.model_down.items():
                if pn == p.name and until > now:
                    resting.append(f"{pn} · {m.split('/')[-1]}: resting (slow/failed), back in {int(until - now)}s")
        if resting:
            self.ui.info("models resting after limits:\n  " + "\n  ".join(resting))
        self.ui.info("routing: " + (f"pinned to {self.router.pinned}" if self.router.pinned else "automatic failover"))

    # keys (NEXT 7) ----------------------------------------------------------------
    def _store(self) -> keyvault.KeyStore:
        return keyvault.KeyStore(getattr(self.agent, "keystore_path", None))

    def _secret(self, prompt: str) -> str:
        reader = getattr(self, "secret_input", None)
        if reader:
            return reader(prompt).strip()
        import getpass
        try:
            return getpass.getpass(f"  {prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def _line(self, prompt: str) -> str:
        reader = getattr(self, "line_input", None)
        if reader:
            return reader(prompt).strip()
        try:
            return console.input(f"  [muted]{prompt}[/] [accent]›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def key_health(self, provider: str, key: str) -> tuple[int, int]:
        """(successes, failures) for one key from ~/.forge/usage.json."""
        for name, row in self.agent.tools.usage.report():
            if name == f"key:{provider}:{keyvault.fingerprint(key)}":
                return row["calls"] - row["failures"], row["failures"]
        return 0, 0

    def failing_keys(self) -> list[str]:
        out = []
        for p in self.router.providers:
            for k in p.pool.keys:
                ok, fail = self.key_health(p.name, k)
                if fail >= 3 and ok == 0:
                    out.append(f"{p.name} {keyvault.mask(k)} ({fail} failures, 0 ok)")
        return out

    def add_key(self, key: str, provider: str | None = None) -> bool:
        """Identify, live-test and save one key, then use it right away. Returns True if saved."""
        store, a = self._store(), self.agent
        tester = getattr(a, "key_tester", None) or keyvault.test_key
        probe = getattr(a, "key_probe", None) or keyvault.identify
        eps = a.key_endpoints(store)
        guesses = keyvault.candidates(key)
        provider = provider or keyvault.detect_provider(key)
        if not provider:
            self.ui.info("checking which provider accepts this key…")
            provider = probe(key, eps, guesses or None)
        if not provider:
            ans = self._line("which provider is it for? (name, or the base URL of an OpenAI-compatible API · "
                             "enter = keep it unfiled)")
            if ans.lower().startswith(("http://", "https://")):
                name = self._line("short name for this provider").lower().replace(" ", "-")
                model = self._line("model id to use")
                if not (name and model):
                    self.ui.error("need a name and a model; key not saved")
                    return False
                store.add_custom(name, ans, model)
                eps[name] = (ans.rstrip("/"), model)
                provider = name
            elif ans:
                provider = ans.lower()
            else:
                store.add(key, None, guesses)
                self.ui.warn(f"saved unidentified key {keyvault.mask(key)} · /keys file <n> <provider> to use it")
                return True
        if provider in eps:
            ok, why = tester(eps[provider][0], key, eps[provider][1])
            if not ok and ("rejected" in why or "no credit" in why):
                self.ui.error(f"✗ {provider} key {keyvault.mask(key)}: {why} — not saved")
                return False
        else:
            ok, why = False, (f"no endpoint known for {provider!r} · /keys provider {provider} <base_url> <model>")
        store.add(key, provider)
        base, model = eps.get(provider, ("", ""))
        self.router.add_key(provider, key, base, model)
        (self.ui.ok if ok else self.ui.warn)(f"{'✓' if ok else '•'} {provider} key {keyvault.mask(key)} saved · {why}")
        return True

    def cmd_keys(self, arg):
        store = self._store()
        words = arg.split()
        sub = words[0].lower() if words else ""
        if sub == "add":
            key = " ".join(words[1:]) or self._secret("paste the key (hidden, enter = cancel)")
            if key:
                self.add_key(key)
            return
        if sub == "remove":
            if len(words) != 3 or not words[2].isdigit():
                self.ui.error("usage: /keys remove <provider> <n>   (numbers from /keys)")
                return
            prov = keyvault.UNFILED if words[1].lower() == "unfiled" else words[1].lower()
            keys = [k for k, _ in store.unfiled()] if prov == keyvault.UNFILED else store.load().get(prov, [])
            n = int(words[2])
            if not 0 <= n < len(keys):
                self.ui.error(f"no stored key {prov} #{n} (keys set in .env are removed by editing .env)")
                return
            store.remove(prov, n)
            self.router.remove_key(prov, keys[n])
            self.ui.ok(f"removed {prov} key {keyvault.mask(keys[n])}")
            return
        if sub == "file":
            if len(words) != 3 or not words[1].isdigit():
                self.ui.error("usage: /keys file <n> <provider>   (n from the unfiled list in /keys)")
                return
            key = store.file(int(words[1]), words[2].lower())
            if not key:
                self.ui.error(f"no unfiled key #{words[1]}")
                return
            c = store.custom().get(words[2].lower(), {})
            used = self.router.add_key(words[2].lower(), key, c.get("base_url", ""), c.get("model", ""))
            self.ui.ok(f"filed {keyvault.mask(key)} under {words[2].lower()}"
                       + ("" if used else " · no endpoint yet: /keys provider <name> <base_url> <model>"))
            return
        if sub == "provider":
            if len(words) != 4:
                self.ui.error("usage: /keys provider <name> <base_url> <model>")
                return
            name, url, model = words[1].lower(), words[2], words[3]
            store.add_custom(name, url, model)
            for k in store.load().get(name, []):
                self.router.add_key(name, k, url, model)
            self.ui.ok(f"provider {name} → {url} · {model}")
            return
        if sub == "test":
            tester = getattr(self.agent, "key_tester", None) or keyvault.test_key
            want = words[1].lower() if len(words) > 1 else ""
            for p in self.router.providers:
                if p.pool.keys == [keyvault.ANON] or (want and p.name != want):
                    continue
                for k in p.pool.keys:
                    ok, why = tester(p.base_url, k, p.model)
                    (self.ui.ok if ok else self.ui.error)(f"{'✓' if ok else '✗'} {p.name} {keyvault.mask(k)}: {why}")
            probe = getattr(self.agent, "key_probe", None) or keyvault.identify
            for i, (k, guesses) in reversed(list(enumerate(store.unfiled()))):
                found = probe(k, self.agent.key_endpoints(store), guesses or None)
                if found:
                    store.file(i, found)
                    self.router.add_key(found, k)
                    self.ui.ok(f"✓ unfiled key {keyvault.mask(k)} is a {found} key; filed and in use")
            return
        if sub:
            self.ui.error("unknown /keys option — /help lists them")
            return
        stored = store.load()
        rows = []
        for p in self.router.providers:
            if p.pool.keys == [keyvault.ANON]:
                continue
            for k in p.pool.keys:
                ok, fail = self.key_health(p.name, k)
                where = f"stored #{stored[p.name].index(k)}" if k in stored.get(p.name, []) else ".env"
                state = "ready" if p.pool.usable(k) else "resting"
                if fail >= 3 and ok == 0:
                    state = "failing: /keys test"
                rows.append([p.name, keyvault.mask(k), where, Text(state, style="ok" if state == "ready" else "warn"),
                             ok, fail])
        for name, keys in stored.items():
            if not self.router.get(name):
                for i, k in enumerate(keys):
                    rows.append([name, keyvault.mask(k), f"stored #{i}", Text("no endpoint", style="warn"), 0, 0])
        for i, (k, guesses) in enumerate(store.unfiled()):
            rows.append(["unfiled", keyvault.mask(k), f"#{i}", Text("maybe " + "/".join(guesses) if guesses
                                                                      else "unknown", style="warn"), 0, 0])
        if not rows:
            self.ui.info("no API keys yet — Forge is using the free keyless models · /setup or /keys add")
            return
        self.ui.table("api keys", ["provider", "key", "from", "state", "ok", "failed"], rows,
                      ["accent", None, "muted", None, "muted", "muted"])
        self.ui.info(f"stored in {'the OS keychain' if store.backend == 'keyring' else store.path} · "
                     "/keys add · /keys test · /keys remove <provider> <n>")

    def cmd_setup(self, _):
        names = list(keyvault.SIGNUP)
        have = {p.name for p in self.router.providers if p.pool.keys != [keyvault.ANON]}
        self.ui.info("Forge already works with free keyless models. Your own keys give faster, stronger ones.")
        self.ui.table("providers", ["#", "provider", "cost", "good at", "you have"],
                      [[i + 1, n, *keyvault.SIGNUP[n][:2], "✓" if n in have else ""] for i, n in enumerate(names)],
                      ["muted", "accent", None, "muted", "ok"])
        while True:
            ans = self._line("number = open its key page · k = paste a key · enter = done")
            if not ans:
                return
            if keyvault.find_keys(ans):           # pasted straight in: still accepted
                for k in keyvault.find_keys(ans):
                    self.add_key(k)
                continue
            provider = None
            if ans.isdigit() and 1 <= int(ans) <= len(names):
                provider = names[int(ans) - 1]
                url = keyvault.SIGNUP[provider][2]
                self.ui.info(f"opening {url}")
                try:
                    import webbrowser
                    webbrowser.open(url)
                except Exception:
                    pass
            elif ans.lower() != "k":
                continue
            key = self._secret(f"paste the {provider or 'API'} key (hidden, enter = skip)")
            if key:
                found = keyvault.detect_provider(key)
                self.add_key(key, provider if not found or found == provider else found)

    def cmd_doctor(self, _):
        a = self.agent
        lines = a.tools.doctor_report().splitlines()
        now = time.time()
        for provider in a.router.providers:
            for key, ready, state, ok, fail in provider.pool.status_rows():
                if fail:
                    lines.append(f"Key {provider.name}:{key}: {fail} recorded failure(s), {state}")
        for (provider, model), until in a.router.model_down.items():
            if until > now:
                lines.append(f"Model {provider}:{model}: resting for {int(until - now)}s after a failure")
        used_skills = {key.split(":", 1)[1] for key, _ in a.tools.usage.report()
                       if key.startswith("skill:")}
        unique_skills = {skill.path: skill for skill in a.skills.values()}
        unused = sorted(skill.name for skill in unique_skills.values() if skill.name not in used_skills)
        if unused:
            preview = ", ".join(unused[:10]) + ("…" if len(unused) > 10 else "")
            lines.append(f"Skills never used in recorded sessions: {len(unused)} ({preview})")
        for line in lines:
            self.ui.info(line)

    def cmd_yes(self, _):
        t = self.agent.tools
        self.cmd_approve("smart" if t.mode == "auto" else "auto")

    def cmd_approve(self, arg):
        t = self.agent.tools
        arg = arg.strip().lower()
        if arg not in ("smart", "ask", "auto"):
            self.ui.info(f"approval is {t.mode}: {APPROVAL_TEXT[t.mode]}")
            self.ui.info("change it with /approve smart | ask | auto")
            return
        t.mode = arg
        t.always.clear()
        (self.ui.warn if arg == "auto" else self.ui.ok)(f"approval: {APPROVAL_TEXT[arg]}")

    def cmd_balance(self, _):
        a = self.agent
        self.ui.info(f"this session: {a.used_in:,} tokens sent · {a.used_out:,} received · "
                     f"current context ~{a.ctx_tokens:,} tokens")
        rows = []
        for p in self.router.providers:
            if not p.pool.keys:
                continue
            left = p.remaining()
            req = f"{left['req'][0]}/{left['req'][1]}" if left.get("req") else "—"
            tok = f"{ktok(left['tok'][0])}/{ktok(left['tok'][1])}" if left.get("tok") else "—"
            ready = sum(1 for r in p.pool.status_rows() if r[1])
            rows.append([p.name, p.model, f"{ready}/{len(p.pool.keys)}", req, tok])
        self.ui.table("rate limits (from the last reply of each provider)",
                      ["provider", "model", "keys ready", "requests left", "tokens left"], rows,
                      ["accent", "muted", "", "", ""])
        or_ = self.router.get("openrouter")
        if or_ and or_.pool.keys:
            self.ui.start_spinner("Checking OpenRouter credits")
            try:
                lines = or_.credits()
            finally:
                self.ui.stop_spinner()
            for ln in lines:
                self.ui.info("  openrouter " + ln)
        self.ui.info("Most free providers don't publish a money balance; '—' means no reply from them yet.")
        resting = getattr(self.agent, "resting", None)
        if resting:
            self.ui.info("Used last, after slow or failed replies (Forge tries them again on its own): " +
                         ", ".join(resting[:6]) + ("…" if len(resting) > 6 else ""))

    def cmd_cost(self, _):
        self.ui.info(f"this session: {self.agent.used_in:,} input tokens · "
                     f"{self.agent.used_out:,} output tokens")
        from . import prices
        lines, total, unknown = prices.summary(getattr(self.agent, "by_model", {}))
        for ln in lines:
            self.ui.info("  " + ln)
        if lines:
            self.ui.info(f"money: about ${total:,.4f}" + (" plus models with unknown prices" if unknown else "") +
                         " (list prices, cached-input discounts not counted; free tiers cost $0; "
                         "your own prices go in ~/.forge/prices.json)")

    def cmd_feedback(self, arg):
        """Open a pre-filled GitHub issue. Only version, OS, Python and model go in — never the conversation."""
        import platform
        import urllib.parse
        import webbrowser
        words = arg.strip()
        if words.lower() in ("off", "on"):
            self.prefs["feedback_nudge"] = words.lower() == "on"
            save_prefs(self.prefs)
            self.ui.info("feedback reminders " + ("on — at most once a fortnight" if words.lower() == "on" else
                                                  "off — /feedback still works whenever you want it"))
            return
        idea = words.lower() in ("idea", "feature", "suggest", "suggestion")
        if idea:
            q = {"template": "idea.yml", "env": __version__}
        else:
            try:
                model = self.model_label()
            except Exception:
                model = ""
            q = {"template": "bug.yml", "what": words,
                 "env": f"Forge {__version__} · {platform.system()} {platform.release()} · Python {platform.python_version()}",
                 "model": model}
        url = "https://github.com/HSigei/forgeCLI/issues/new?" + urllib.parse.urlencode({k: v for k, v in q.items() if v})
        try:
            webbrowser.open(url)
            self.ui.info("opened a new GitHub issue in your browser — version and OS are filled in, nothing from this conversation is sent")
        except Exception:
            pass
        self.ui.info(f"if it didn't open: {url}")
        self.ui.info("questions and general thoughts: https://github.com/HSigei/forgeCLI/discussions")

    def cmd_update(self, arg):
        """/update check: say whether a newer version exists. /update: install it. Returns False to quit."""
        from . import update as upd
        self.ui.info("checking pypi.org for the newest Forge…")
        latest = upd.fetch()
        if not latest:
            self.ui.error("couldn't reach pypi.org — check your connection and try again")
            return True
        if not upd.newer(latest):
            self.ui.info(f"you're up to date: Forge {__version__} is the newest version")
            return True
        kind, cmd = upd.how_installed()
        self.ui.info(f"Forge {latest} is out (you have {__version__})")
        if arg.lower() in ("check", "status"):
            self.ui.info("type /update to install it")
            return True
        if kind == "exe":
            self.ui.info(f"download the new forge.exe zip from {RELEASES} and replace the old forge.exe")
            return True
        if kind == "source" or not cmd:
            self.ui.info("this Forge runs from a source folder: update it with  git pull  in that folder")
            return True
        line = " ".join(cmd)
        if os.name == "nt":
            if self.ui.confirm("Close Forge and update now?", line, "installs the new version")[0] == "n":
                return True
            if upd.run_after_exit(cmd):
                self.ui.info("Forge will close; the update runs in a new window. Your conversation is saved (forge -c).")
                return False
            self.ui.error(f"couldn't start the updater — close Forge and run:  {line}")
            return True
        if self.ui.confirm("Update Forge now?", line, "installs the new version")[0] == "n":
            return True
        code = subprocess.run(cmd).returncode
        if code == 0:
            self.ui.info(f"updated to {latest} — restart Forge to use it (forge -c continues this conversation)")
        else:
            self.ui.error(f"the update failed (exit {code}) — try running it yourself:  {line}")
        return True

    def cmd_ads(self, arg):
        from . import ads as adsmod
        word = arg.strip().lower()
        st = adsmod.load_state()
        if word == "off":
            st["off"] = True
            adsmod.save_state(st)
            self.ads = None
            self.ui.sponsor = None
            self.ui.info("Sponsor lines are off for good on this computer (/ads on brings them back).")
        elif word == "on":
            st["off"] = False
            adsmod.save_state(st)
            self.ui.info("Sponsor line allowed again: one faint line under the status while Forge works.")
        elif word == "reset":
            st.pop("id", None)
            st.pop("cached", None)
            adsmod.install_id(st)
            self.ui.info("New random install id made; the old one is forgotten here.")
        elif word in ("", "status"):
            ok, why = adsmod.allowed(st)
            self.ui.info(f"sponsor line: {'on' if ok else 'not shown — ' + why}")
            self.ui.info("Shown as one faint line under the working status (after a few seconds of work), never in pipes, scripts, CI or forge -p. The ad request "
                         "sends only Forge's version and a random install id — no prompts, code, paths or keys. "
                         "Ads must be signed by Forge's key, are cleaned to plain text and never reach the model.")
            self.ui.info("Turn off: /ads off, or FORGE_ADS=0. New install id: /ads reset.")
        else:
            self.ui.error("use /ads status, /ads off, /ads on or /ads reset")

    def _github_login(self):
        from . import ghlogin, keyvault
        cid = ghlogin.client_id()
        if not cid:
            self.ui.warn("GitHub sign-in isn't set up in this build (no OAuth client id). Set "
                         "FORGE_GITHUB_CLIENT_ID, run `gh auth login`, or add a token with /keys.")
            return
        try:
            device = ghlogin.start(cid)
        except Exception as e:
            self.ui.error(f"couldn't start GitHub sign-in: {e}")
            return
        url = device.get("verification_uri", "https://github.com/login/device")
        self.ui.info(f"Open {url} and enter this code:  {device['user_code']}")
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
        self.ui.start_spinner("waiting for you to approve on GitHub (ctrl+c cancels)")
        try:
            token = ghlogin.poll(cid, device)
        except KeyboardInterrupt:
            self.ui.stop_spinner()
            self.ui.warn("sign-in cancelled")
            return
        except Exception as e:
            self.ui.stop_spinner()
            self.ui.error(f"GitHub sign-in failed: {e}")
            return
        self.ui.stop_spinner()
        keyvault.KeyStore().add(token, "github")
        who = ghlogin.whoami(token)
        self.ui.info(f"signed in to GitHub{' as ' + who if who else ''}; the token is saved in Forge's key store")

    def cmd_github(self, arg):
        sub = arg.strip().lower()
        if sub == "login":
            return self._github_login()
        if sub == "logout":
            from . import keyvault
            store, n = keyvault.KeyStore(), 0
            while store.remove("github", 0):
                n += 1
            self.ui.info(f"removed {n} saved GitHub token(s)" if n else "no GitHub token was saved in Forge")
            return
        if sub not in ("", "status"):
            self.ui.error("use /github status, /github login or /github logout")
            return
        if not shutil.which("gh"):
            configured = bool(self.agent.tools.github_token())
            self.ui.info("GitHub token is configured." if configured else
                         "GitHub is not connected. Install gh and run `gh auth login` in a terminal, "
                         "or add a GitHub token with /keys.")
            return
        try:
            result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True,
                                    timeout=8, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.ui.error(f"could not check gh authentication: {exc}")
            return
        output = (result.stdout + result.stderr).strip()
        from . import keyvault
        output = keyvault.redact(output)
        if result.returncode:
            if self.agent.tools.github_token():
                self.ui.warn((output + "\n" if output else "") +
                             "The gh CLI is not connected, but a token is configured for GitHub API calls.")
            else:
                self.ui.warn((output + "\n" if output else "") +
                             "Connect with `gh auth login` in a separate terminal, or add a GitHub token with /keys.")
        else:
            self.ui.info(output or "gh reports an authenticated GitHub account.")

    def cmd_memory(self, arg):
        from . import memory as memorymod
        scope = arg.strip().lower() or "project"
        if scope not in ("user", "project"):
            self.ui.error("use /memory [user|project]")
            return
        try:
            note = memorymod.read(scope, str(self.agent.session.cwd))
        except OSError as exc:
            self.ui.error(f"could not read memory: {exc}")
            return
        self.ui.info(note or f"No {scope} memory saved yet.")

    def _project_command(self, name: str, arguments: str) -> bool:
        if name == "commands":
            directory = pathlib.Path.cwd() / ".forge" / "commands"
            commands = sorted(directory.glob("*.md")) if directory.is_dir() else []
            if commands:
                self.ui.table("project commands", ["command", "file"],
                              [[f"/{p.stem}", str(p)] for p in commands], ["accent", "muted"])
            else:
                self.ui.info("No project commands. Add Markdown files to .forge/commands/.")
            return True
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            return False
        path = pathlib.Path.cwd() / ".forge" / "commands" / f"{name}.md"
        if not path.is_file():
            return False
        try:
            with path.open(encoding="utf-8") as command_file:
                content = command_file.read(12_001)
        except OSError as exc:
            self.ui.error(f"could not read {path}: {exc}")
            return True
        if len(content) > 12_000:
            self.ui.error(f"{path} exceeds the 12,000-character command limit")
            return True
        self.run(content.replace("$ARGUMENTS", arguments), echo=f"command: {name}")
        return True

    def cmd_parallel(self, arg):
        low = arg.lower()
        if low in ("", "on", "off"):
            self.agent.parallel = (not self.agent.parallel) if not low else low == "on"
            if self.agent.parallel:
                self.ui.ok(f"parallel ON — forge may split independent work across up to "
                           f"{self.agent.settings.max_agents} agents at once")
            else:
                self.ui.ok("parallel OFF — one agent at a time")
            return
        tasks = parallel.split_tasks(arg)
        assigned = any(t.startswith("@") for t in tasks)
        if len(tasks) < 2 and not assigned:
            self.ui.error("give at least two tasks separated by ||, e.g. "
                          "/parallel write tests for a.py || @minimax add docstrings to b.py")
            return
        report = parallel.run_tasks(self.agent, tasks)
        # Put the reports into the main conversation so every later reply knows what happened.
        self.run("I ran these tasks in parallel with sub-agents. Their reports:\n\n" + report +
                 "\n\nGive me a short combined summary, and flag any conflicts or follow-ups.")

    def cmd_team(self, arg):
        bits = arg.split(maxsplit=1)
        low = bits[0].lower() if bits else ""
        if low == "lead":
            want = bits[1] if len(bits) > 1 else ""
            w = roster.resolve(roster.build(self.router), want)
            if not w:
                self.ui.error(f"no worker matches {want!r}; see /roster")
                return
            self.agent.settings.coordinator = w.id
            low = "on"
        if low not in ("", "on", "off"):
            self.ui.error("use /team, /team on, /team off, or /team lead <worker>")
            return
        on = (not self.agent.team) if not low else low == "on"
        lead = self.agent.set_team(on)
        if on and not lead:
            self.ui.error("no usable model to coordinate; check /status")
        elif on:
            self.ui.ok(f"team ON — {lead} coordinates and hands work to the models best at it "
                       f"(up to {self.agent.settings.max_agents} at once)")
            self.ui.info("change the lead with /team lead <worker>; see who's on the team with /roster")
        else:
            self.ui.ok("team OFF — one model at a time with normal failover")

    def cmd_city(self, arg):
        """Turn the pixel city on or off, calm its motion, or play a short tour of every state."""
        a = (arg or "").strip().lower()
        if a in ("off", "on"):
            self.ui.scene = a == "on"
            self.ui.ok(f"pixel city {a}" + ("  (FORGE_SCENE=city in .env turns it on at start; the wheelbarrow stays)"
                                            if a == "on" else "  (the small wheelbarrow still shows progress)"))
            return
        if a in ("calm", "still", "reduce"):
            self.ui.city.motion = not self.ui.city.motion
            self.ui.barrow.motion = self.ui.city.motion
            self.ui.ok("reduced motion " + ("off" if self.ui.city.motion else "on"))
            return
        from rich.live import Live
        from .city import City
        tour = [("processing", "wheelbarrow deliveries: commands and tool runs", 7.0),
                ("coding", "typing at the desk: writing and editing files", 3.0),
                ("thinking", "thinking at the desk, pacing now and then: a model is answering", 4.0),
                ("researching", "pulling documents from the archive: reading, searching, fetching", 5.0),
                ("delegating", "handing parcels to helper cubs: parallel agents", 6.0),
                ("waiting", "coffee on the bench: every model is rate-limited", 3.5),
                ("queued", "tapping a foot: your message is queued", 2.0),
                ("completed", "the task finished", 2.5), ("failed", "something broke", 2.5),
                ("cancelled", "you pressed esc / ctrl+c", 2.0)]
        c = City()
        c.motion = self.ui.city.motion
        width = max(20, console.width - 2)
        try:
            with Live(console=console, refresh_per_second=20, transient=True) as live:
                for state, what, secs in tour:
                    if state == "delegating":
                        c.set_helpers(2)
                    elif state == "waiting":
                        c.set_helpers(0)
                    c.set(state, wait=secs)
                    if state == "processing":
                        c.blip()
                    end = time.monotonic() + secs
                    while time.monotonic() < end:
                        live.update(Text.from_ansi(c.frame(width).rstrip("\n") + "\n")
                                    .append(f"  {state:<12}", style="ember").append(what, style="muted"))
                        time.sleep(0.05)
        except KeyboardInterrupt:
            pass
        self.ui.info("the city plays automatically while Forge works · /city off hides it · /city calm stills it")

    def cmd_roster(self, arg):
        low = arg.strip().lower()
        workers = roster.build(self.router)
        if low == "edit":
            roster.write_template(workers)
            self.ui.ok(f"edit {roster.ROSTER_FILE} to describe what each model is good at, then run /roster")
            return
        if low == "bench":
            self.ui.info(f"benching {len(workers)} models (a reasoning check + a tool-call check each)…")

            def done(w, res):
                self.ui.info(f"  {w.id}: {w.measured()}")
            try:
                roster.bench(self.router, workers, on_done=done)
            except KeyboardInterrupt:
                self.ui.warn("bench stopped")
                return
            if self.agent.team:   # results can change who should lead
                self.agent.set_team(True)
            self.ui.ok(f"saved to {roster.STATS_FILE}")
            return
        lead = self.agent.coordinator.id if self.agent.team and self.agent.coordinator else ""
        from . import quality as qualitymod
        rows = []
        workers = sorted(workers, key=lambda w: -qualitymod.score(w.model, w.provider))
        for w in workers:
            ok = roster.usable(w, self.router)
            name = Text(w.id + ("  ★ lead" if w.id == lead else ""), style="accent" if ok else "muted")
            sc = qualitymod.score(w.model, w.provider)
            rows.append([name, Text(f"{sc}/10", style="ok" if sc >= 8 else "warn" if sc <= 4 else ""),
                         qualitymod.explain(w.model, w.provider), ", ".join(w.strengths),
                         Text(w.measured(), style="ok" if ok else "warn"), w.notes])
        self.ui.table("team roster · best first", ["worker", "score", "why", "good at", "measured", "notes"],
                      rows, [None, None, "muted", None, None, "muted"])
        self.ui.info("assign one yourself: /parallel @<worker> <task> || @<worker> <task> · "
                     "describe models: /roster edit · measure: /roster bench")

    def cmd_verbose(self, _):
        self.ui.verbose = not self.ui.verbose
        self.ui.ok(f"failover log {'ON' if self.ui.verbose else 'OFF'}")

    # chat ---------------------------------------------------------------------------
    def run(self, text: str, echo: str = "") -> None:
        if echo:
            self.ui.info(f"  ↳ {echo}")
        text = _clean_text(text)
        self.work(lambda: self.agent.run_turn(text))

    STOPPED = "⏹ stopped — say what to do next, or press ↑ to edit your last message"
    worker: threading.Thread | None = None

    def work(self, fn, stopped: str = STOPPED) -> None:
        """Run a task. In the chat (docked) it runs in the background so the message box stays
        open: you can type more for it, answer its questions, or stop it. Otherwise it blocks."""
        if not self.ui.docked:
            self._guarded(fn, stopped)
            return
        self.worker = threading.Thread(target=self._background, args=(fn, stopped), daemon=True,
                                       name="forge-turn")
        self.worker.start()

    def _guarded(self, fn, stopped: str) -> None:
        self.ui.act("queued")
        before = len(getattr(self.agent, "messages", []) or [])
        try:
            fn()
            # the city acts out how the turn really ended
            ok = getattr(self.agent, "last_turn_ok", True)
            self.ui.act("completed" if ok else "failed")
            if ok:
                self._maybe_ask_feedback(before)
        except KeyboardInterrupt:
            self.ui.act("cancelled")
            self.ui.pending = None
            self.ui.stop_spinner()
            self.ui.flush()
            self.ui.warn(stopped)

    FEEDBACK_EVERY_DAYS = 14

    def _maybe_ask_feedback(self, before: int) -> None:
        """Now and then, after a real piece of work went well, one faint line inviting feedback.
        Never a question, never blocking, never more than once a fortnight, and /feedback off stops it."""
        import random
        import time
        try:
            if os.environ.get("FORGE_FEEDBACK_NUDGE", "1") == "0" or self.prefs.get("feedback_nudge") is False:
                return
            steps = sum(1 for m in self.agent.messages[before:] if m.get("role") == "tool")
            if steps < 4:                                   # a chat reply or a tiny edit: not "a part done"
                return
            done = int(self.prefs.get("tasks_done", 0)) + 1
            self.prefs["tasks_done"] = done
            last = float(self.prefs.get("feedback_asked", 0) or 0)
            due = done >= 5 and time.time() - last > self.FEEDBACK_EVERY_DAYS * 86400 and random.random() < 0.3
            if due:
                self.prefs["feedback_asked"] = time.time()
                self.ui.info("enjoying Forge, or did something annoy you? /feedback tells me in one line "
                             "· /feedback off hides this")
            save_prefs(self.prefs)
        except Exception:
            pass

    def _background(self, fn, stopped: str) -> None:
        try:
            self._guarded(fn, stopped)
            # Messages typed after the model's last step start a follow-up turn.
            while self.agent.inbox and not self._stopping:
                text = "\n\n".join(self.agent.inbox)
                self.agent.inbox.clear()
                self._guarded(lambda: self.agent.run_turn(text), self.STOPPED)
        except KeyboardInterrupt:
            pass   # a stop that landed just as the task was finishing
        except Exception as exc:   # never let a crash kill the chat silently
            self.ui.stop_spinner()
            self.ui.error(f"internal error: {type(exc).__name__}: {exc}")
        finally:
            self.ui.busy = False
            self._stopping = False
            self.worker = None
            if self._redraw:
                self._redraw()

    _stopping = False
    _redraw = None

    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def stop(self) -> None:
        """Stop the running task, as ctrl+c did when the task ran in the foreground."""
        w = self.worker
        if not (w and w.is_alive()):
            return
        self._stopping = True
        self.agent.inbox.clear()
        # Raise KeyboardInterrupt inside the task's thread, so every existing ctrl+c path runs
        # (shell commands are killed, unanswered tool calls get results, the turn is saved).
        import ctypes
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(w.ident), ctypes.py_object(KeyboardInterrupt))
        p = self.ui.pending
        if p:
            p["stopped"] = True
            p["event"].set()

    def goodbye(self) -> int:
        """Close Forge cleanly: the screen and the scrollback are cleared, and all that is left is
        one faint line with the command that resumes this exact conversation.
        FORGE_FRESH_PAGE=off leaves the conversation on screen instead."""
        s = self.agent.session
        # The full id: a short prefix is only the date, which several conversations can share.
        hint = f"forge -r {s.id}" if s.messages else ""
        try:
            if console.is_terminal and os.environ.get("FORGE_FRESH_PAGE", "on").lower() not in ("off", "0", "no"):
                console.file.write("[0m[2J[3J[H[3J")
                console.file.flush()
            if hint:
                console.print(Text(hint, style="#6b6480"))
        except Exception:
            print(hint or "bye")
        return 0

    def loop(self, replay: Session | None = None, pick: bool = False) -> int:
        try:
            from . import ads as adsmod
            self.ads = adsmod.Ads(__version__)     # fetches quietly in the background, if allowed at all
        except Exception:
            self.ads = None
        # The sponsor line sits faintly under the working status (never in the reply or the model).
        self.ui.sponsor = (lambda: self.ads.line(wait=False)) if self.ads and self.ads.ok else None
        try:
            from . import update as upd
            upd.check_in_background()              # once a day, cached for the next start's banner
        except Exception:
            pass
        prompt = make_prompt(self)
        from . import loader
        loader.stop()
        self.fresh_page()
        self.banner()
        note = getattr(self, "start_note", None)
        if note:
            self.ui.warn(note)
        if os.environ.get("FORGE_AUTO_BENCH", "1").strip().lower() not in ("0", "off", "false", "no"):
            # New models get a quick test in the background (two tiny requests each) so their
            # capability scores are measured, not just guessed from the name.
            from . import quality as qualitymod
            qualitymod.auto_bench(self.router)
        if pick:
            self.cmd_resume("")
        if replay:
            self.ui.rule(replay.title or replay.id)
            self.ui.replay(replay.messages)
        docked = getattr(prompt, "docked", False)
        if docked:
            from prompt_toolkit.patch_stdout import patch_stdout
            self.ui.docked = True
            with patch_stdout(raw=True):   # output from the task thread prints above the box
                return self._docked_loop(prompt)
        while True:
            try:
                text = prompt()
            except KeyboardInterrupt:
                self.ui.info("(ctrl+d or /exit to quit)")
                continue
            except EOFError:
                return self.goodbye()
            text = text.replace(BOM, "").strip()  # PowerShell pipes can prepend one
            if not text:
                continue
            if text.startswith("/") and not text.startswith("//"):
                if not self.handle(text):
                    return self.goodbye()
                console.print()
                continue
            self.run(text.removeprefix("/"))

    def _docked_loop(self, prompt) -> int:
        """The chat with the message box always open, even while a task runs."""
        while True:
            try:
                text = prompt()
            except KeyboardInterrupt:
                if self.busy():
                    self.stop()
                else:
                    self.ui.info("(ctrl+d or /exit to quit)")
                continue
            except EOFError:
                if self.busy():
                    self.stop()
                return self.goodbye()
            text = text.replace(BOM, "").strip()  # PowerShell pipes can prepend one
            if self.ui.pending:          # Forge asked something: this is the answer…
                word = text.split()[0].lower() if text.startswith("/") and not text.startswith("//") else ""
                if word in ("/exit", "/quit", "/q"):          # …unless it's a way out
                    self.stop()
                    return self.goodbye()
                if word in ("/stop", "/cancel"):
                    self.stop()
                    continue
                if word:
                    self.ui.info("Forge is waiting for your answer — type it, or /stop to cancel the task")
                    continue
                self.ui.answer(text)
                continue
            if not text:
                continue
            command = text.startswith("/") and not text.startswith("//")
            if self.busy():
                if command and text.split()[0].lower() in ("/stop", "/cancel"):
                    self.stop()
                elif command and text.split()[0].lower() in ("/exit", "/quit"):
                    self.stop()
                    return self.goodbye()
                elif command:
                    self.ui.info("Forge is working — type a message to add it to the task, "
                                 "or stop it first (esc, ctrl+c or /stop)")
                else:
                    self.agent.inbox.append(text.removeprefix("/"))
                    self.ui.queued()
                continue
            if command:
                if not self.handle(text):
                    return self.goodbye()
                console.print()
                continue
            self.run(text.removeprefix("/"))


def _clip(text: str, n: int) -> str:
    """Shorten at a word boundary with an ellipsis instead of cutting mid-word."""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    return text[:n - 1].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"


def _short(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.lower().startswith(home.lower()) else path


class _PromptBuffer(Protocol):
    def insert_text(self, text: str) -> None: ...


class _PromptEvent(Protocol):
    current_buffer: _PromptBuffer


def _windows_clipboard_text() -> str:
    """Read plain text from the Windows clipboard without an optional package."""
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    opened = False
    for _ in range(5):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.02)
    if not opened:
        raise OSError(ctypes.get_last_error(), "could not open the Windows clipboard")
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise OSError(ctypes.get_last_error(), "could not read text from the Windows clipboard")
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _windows_clipboard_files() -> list[str]:
    """Paths of files copied in Explorer (CF_HDROP), or []."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    if not user32.IsClipboardFormatAvailable(15) or not user32.OpenClipboard(None):  # CF_HDROP
        return []
    try:
        handle = user32.GetClipboardData(15)
        if not handle:
            return []
        out = []
        for i in range(shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)):
            buf = ctypes.create_unicode_buffer(shell32.DragQueryFileW(handle, i, None, 0) + 1)
            shell32.DragQueryFileW(handle, i, buf, len(buf))
            out.append(buf.value)
        return out
    finally:
        user32.CloseClipboard()


def _windows_clipboard_image() -> str:
    """Save a clipboard image (screenshot, copied picture) as a PNG in ~/.forge/pastes; return its path."""
    if os.name != "nt":
        return ""
    import ctypes
    user32 = ctypes.WinDLL("user32")
    if not (user32.IsClipboardFormatAvailable(8) or user32.IsClipboardFormatAvailable(17)):  # CF_DIB / CF_DIBV5
        return ""
    folder = HOME_DIR / "pastes"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / time.strftime("paste-%Y%m%d-%H%M%S.png")
    script = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
              "$i=[System.Windows.Forms.Clipboard]::GetImage();"
              f"if($i){{$i.Save('{path}',[System.Drawing.Imaging.ImageFormat]::Png)}}")
    try:
        subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return str(path) if path.exists() else ""


def _quote_path(p: str) -> str:
    return f'"{p}"' if " " in p else p


# --- paste pills -----------------------------------------------------------------
# A paste longer than PASTE_PILL_OVER lines is not dumped into the box. The box shows one
# highlighted pill, "[Pasted text #1 · 42 lines · 1,830 chars]", and the real text is kept on the
# side and put back when the message is sent.

PASTE_PILL_OVER = 10
_PILL = r"\[Pasted text #\d+ · [^\]\n]*\]"
_PILL_RE = re.compile(_PILL)
_PILL_AT_END = re.compile(_PILL + r"\Z")
_PILL_AT_START = re.compile(r"\A" + _PILL)


def _normalize_paste(text: str) -> str:
    return _clean_text(text).replace("\r\n", "\n").replace("\r", "\n")


class PasteStore:
    """Holds the text behind each pill. Keyed by the whole pill text, so a pill recalled from an
    older session's history can never expand into a different paste that reused its number."""

    def __init__(self, over: int = PASTE_PILL_OVER):
        self.over, self.items, self.count = over, {}, 0

    def wants_pill(self, text: str) -> bool:
        return text.rstrip("\n").count("\n") + 1 > self.over

    def add(self, text: str) -> str:
        self.count += 1
        body = text.rstrip("\n")
        lines = body.count("\n") + 1
        token = (f"[Pasted text #{self.count} · {lines} lines · "
                 f"{len(body):,} chars]")
        self.items[token] = text
        return token

    def expand(self, text: str) -> str:
        """The message with every known pill replaced by what was pasted. A pill this store does
        not know (typed by hand, or left over from an old session) stays as the plain text it is."""
        return _PILL_RE.sub(lambda m: self.items.get(m.group(0), m.group(0)), text)


def _pill_fragments(line: str) -> list:
    """One line of the box as (style, text) fragments, with every pill marked class:pill."""
    out, pos = [], 0
    for m in _PILL_RE.finditer(line):
        if m.start() > pos:
            out.append(("", line[pos:m.start()]))
        out.append(("class:pill", m.group(0)))
        pos = m.end()
    out.append(("", line[pos:]))
    return out


def _insert_paste(buf, text: str, store: "PasteStore | None") -> None:
    text = _normalize_paste(text)
    if store is not None and store.wants_pill(text):
        buf.insert_text(store.add(text))
    else:
        buf.insert_text(text)


def _delete_pill(buf, backward: bool) -> bool:
    """Backspace / delete next to a pill removes the whole pill in one keypress."""
    doc = buf.document
    if backward:
        m = _PILL_AT_END.search(doc.text_before_cursor)
        if m:
            buf.delete_before_cursor(len(m.group(0)))
            return True
    else:
        m = _PILL_AT_START.match(doc.text_after_cursor)
        if m:
            buf.delete(len(m.group(0)))
            return True
    return False


def _paste_clipboard(event: _PromptEvent, read_text: Callable[[], str] | None = None,
                     read_files: Callable[[], list[str]] | None = None,
                     read_image: Callable[[], str] | None = None,
                     store: "PasteStore | None" = None) -> str:
    """Insert what is on the clipboard: text as is, files copied in Explorer as their paths,
    an image (screenshot) saved as a PNG and inserted as its path. Returns what was inserted."""
    text = (read_text or _windows_clipboard_text)()
    if not text:
        text = " ".join(_quote_path(f) for f in (read_files or _windows_clipboard_files)())
    if not text:
        image = (read_image or _windows_clipboard_image)()
        text = _quote_path(image) if image else ""
    if text:
        _insert_paste(event.current_buffer, text, store)
    return text


# --- input -----------------------------------------------------------------------

def _fit(text: str, width: int) -> str:
    """text cut to width columns, ending in … when it was longer."""
    return text if len(text) <= width else text[:max(0, width - 1)].rstrip() + "…"


def _on_enter(buf, has_question: bool) -> str:
    """What the enter key does in the message box. Returns what happened (for tests).

    An empty (or whitespace-only) box is not a request, so enter does nothing there: it used to
    submit nothing, which made the loop draw a brand-new box under the old one every time. The
    one exception is when Forge is asking a question: there an empty enter is a real answer
    ("yes", "skip", "cancel"), so it goes through."""
    if buf.document.text_before_cursor.endswith("\\"):
        buf.delete_before_cursor(1)
        buf.insert_text("\n")
        return "newline"
    if buf.complete_state and buf.complete_state.current_completion:
        buf.apply_completion(buf.complete_state.current_completion)
        return "completed"
    if not buf.text.strip() and not has_question:
        if buf.text:
            buf.reset()          # stray spaces or blank lines: clear them, send nothing
        return "ignored"
    buf.validate_and_handle()
    return "submitted"


def _resize_extra_rows(old_cols: int, new_cols: int, rows_above: int) -> int:
    """How many more rows the box's full-width lines occupy after the window got narrower.

    A terminal re-wraps what is already on screen when it is resized: a line drawn 120 columns wide
    becomes two rows in a 70-column window. prompt_toolkit erases the box by moving up the number of
    rows it *drew*, so after a zoom-in it stops short and leaves the old top edge and status rows
    behind (the stack of purple lines). The extra rows are added to its climb. Widening never needs
    any: lines that fit the narrow window still fit the wide one."""
    if new_cols <= 0 or old_cols <= new_cols or rows_above <= 0:
        return 0
    return (-(-old_cols // new_cols) - 1) * rows_above


def _erase_for_resize(renderer, old_cols: int, rows_above: int) -> None:
    """prompt_toolkit's Renderer.erase(), but climbing past the rows the terminal re-wrapped."""
    out = renderer.output
    try:
        new_cols = out.get_size().columns
    except Exception:
        new_cols = old_cols
    out.write_raw("\r")
    out.cursor_up(renderer._cursor_pos.y + _resize_extra_rows(old_cols, new_cols, rows_above))
    out.erase_down()
    out.reset_attributes()
    out.enable_autowrap()
    out.flush()
    renderer.reset(leave_alternate_screen=False)


def _install_resize_fix(pt_app, state: dict) -> None:
    """Replace the application's resize handler with one that survives zooming.

    `state` is kept up to date by the prompt's message(): cols (width it was last drawn at),
    above (full-width rows above the input line) and drawn (a box is on screen right now)."""
    original = pt_app._on_resize

    def on_resize() -> None:
        try:
            if state.get("drawn"):
                _erase_for_resize(pt_app.renderer, state.get("cols") or 0, state.get("above") or 0)
                state["drawn"] = False   # erased: a second resize before the redraw must not climb again
            else:
                pt_app.renderer.reset(leave_alternate_screen=False)
            pt_app._request_absolute_cursor_position()
            pt_app._redraw()
        except Exception:
            original()

    pt_app._on_resize = on_resize


def _term_cols(pt_app=None) -> int:
    """The window's width right now (what prompt_toolkit sees, else rich), read fresh every call."""
    try:
        if pt_app is not None:
            return max(1, pt_app.output.get_size().columns)
    except Exception:
        pass
    return max(1, console.width)


def make_prompt(app: App):
    """Fancy prompt_toolkit input when attached to a terminal, plain input() otherwise."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        def plain():
            lines = []
            while True:
                line = input("› " if not lines else "  ")
                if line.endswith("\\"):
                    lines.append(line[:-1])
                    continue
                lines.append(line)
                return _clean_text("\n".join(lines))
        return plain

    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.lexers import Lexer
    from prompt_toolkit.styles import Style

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/"):
                return
            words = text.split(" ")
            if len(words) == 1:
                cands = [(k, v) for k, v in COMMANDS.items()]
                cands += [("/" + n, "skill · " + s.description) for n, s in app.agent.skills.items()]
                for name, meta in cands:
                    if name.startswith(words[0].lower()):
                        yield Completion(name, -len(words[0]), display_meta=meta[:60])
                return
            cmd, cur = ALIASES.get(words[0].lower(), words[0].lower()), words[-1]
            pos = len(words) - 1
            if cmd in ("/model", "/models", "/provider") and pos == 1:
                opts = [(p.name, p.model) for p in app.router.providers]
                if cmd == "/provider":
                    opts.append(("auto", "automatic failover"))
                if cmd == "/model":
                    lead = app.router.lead()
                    opts += [(m, lead.name) for m in app.model_cache.get(lead.name, [])] if lead else []
                for name, meta in opts:
                    if cur.lower() in name.lower():
                        yield Completion(name, -len(cur), display_meta=meta)
            elif cmd == "/model" and pos == 2 and app.router.get(words[1].lower()):
                for m in app.model_cache.get(words[1].lower(), []):
                    if cur.lower() in m.lower():
                        yield Completion(m, -len(cur))
            elif cmd in ("/resume", "/delete") and pos == 1:
                for i, s in enumerate(sess.list_sessions()[:30], 1):
                    if str(i).startswith(cur) or s["id"].startswith(cur):
                        yield Completion(str(i), -len(cur), display=f"{i}  {s['title'][:40]}",
                                         display_meta=sess.ago(s["updated"]))
            elif cmd == "/approve" and pos == 1:
                for o, m in APPROVAL_TEXT.items():
                    if o.startswith(cur.lower()):
                        yield Completion(o, -len(cur), display_meta=m[:60])
            elif cmd in ("/team", "/roster") and pos == 1:
                opts = ["on", "off", "lead"] if cmd == "/team" else ["bench", "edit"]
                for o in opts:
                    if o.startswith(cur.lower()):
                        yield Completion(o, -len(cur))
            elif (cmd == "/team" and pos == 2 and words[1].lower() == "lead") or \
                    (cmd == "/parallel" and cur.startswith("@")):
                at = "@" if cur.startswith("@") else ""
                for w in roster.build(app.router):
                    if cur.lstrip("@").lower() in w.id.lower():
                        yield Completion(at + w.id, -len(cur), display_meta=", ".join(w.strengths)[:50])
            elif cmd == "/skill" and pos == 1:
                for n, s in [("new", "create a skill")] + [(n, s.description) for n, s in app.agent.skills.items()]:
                    if n.startswith(cur.lower()):
                        yield Completion(n, -len(cur), display_meta=s[:60])

    kb = KeyBindings()
    pastes = PasteStore()

    @kb.add("escape", "enter")
    @kb.add("c-j")
    def _(event):
        event.current_buffer.insert_text("\n")

    @kb.add("enter")
    def _(event):
        _on_enter(event.current_buffer, bool(app.ui.pending))

    @kb.add(Keys.BracketedPaste)
    def _(event):
        # Terminal paste: repair surrogate halves from the Windows console before they reach the
        # box, so they can never poison the history file, the session file or the request.
        _insert_paste(event.current_buffer, event.data, pastes)

    @kb.add("backspace")
    def _(event):
        buf = event.current_buffer
        if not _delete_pill(buf, backward=True):
            buf.delete_before_cursor(event.arg)

    @kb.add("delete")
    def _(event):
        buf = event.current_buffer
        if not _delete_pill(buf, backward=False):
            buf.delete(event.arg)

    @kb.add("c-c")
    def _(event):
        buf = event.current_buffer
        if buf.text:
            buf.reset()          # first ctrl+c clears what you typed
        elif app.busy():
            app.stop()           # stops the running task; the box stays open
        else:
            event.app.exit(exception=KeyboardInterrupt)

    if os.name == "nt":
        # Windows Terminal handles ctrl+v itself and sends text as a bracketed paste (which
        # prompt_toolkit inserts in one go), but it sends nothing for images or copied files.
        # alt+v (and ctrl+v / shift+insert where the terminal passes them through) reads the
        # clipboard directly: text, Explorer files -> paths, screenshots -> saved PNG path.
        @kb.add("c-v")
        @kb.add("s-insert")
        @kb.add("escape", "v")
        def _(event):
            try:
                if not _paste_clipboard(event, store=pastes):
                    app.ui.info("nothing to paste: the clipboard has no text, files or image")
            except OSError as exc:
                app.ui.warn(f"could not paste from the Windows clipboard: {exc}")

    # Not eager: an eager escape fired on the first byte of every escape sequence, so alt+enter
    # (new line) and alt+v (paste) could never reach their bindings.
    @kb.add("escape")
    def _(event):
        buf = event.current_buffer
        if not buf.text and app.busy():
            app.stop()           # esc on an empty box stops the running task
        else:
            buf.reset()

    style = Style.from_dict({
        "prompt": "#ff4fd8 bold",
        "edge": "#5b2a9e",
        "edge.mark": "#d8b4fe bold",
        "ask": "#fbbf24 bold",
        "frame": "#4c3a6b",
        "placeholder": "#6b6480 italic",
        "pill": "bg:#4c1d95 #f5d0fe bold",
        "continuation": "#6b7280",
        "bottom-toolbar": "noreverse bg:#140a26 #c4b5fd",
        "tb": "bg:#140a26 #c4b5fd",
        "tb.sep": "bg:#140a26 #7b2cff",
        "tb.dim": "bg:#140a26 #a78bfa",
        "tb.warn": "bg:#422006 #fbbf24 bold",
        "tb.accent": "bg:#2a1052 #f0abfc bold",
        "completion-menu.completion": "bg:#1f1b2e #e5e7eb",
        "completion-menu.completion.current": "bg:#6d28d9 #ffffff",
        "completion-menu.meta.completion": "bg:#1f1b2e #9ca3af",
        "completion-menu.meta.completion.current": "bg:#6d28d9 #e9d5ff",
    })
    class PillLexer(Lexer):
        """Shows each paste pill as a highlighted chip in the box."""
        def lex_document(self, document):
            lines = document.lines

            return lambda i: _pill_fragments(lines[i])

    class RedactingHistory(FileHistory):
        """Input history never keeps a pasted API key (NEXT 7)."""
        def append_string(self, string: str) -> None:
            # History is a convenience: a line it cannot store must never crash the prompt.
            try:
                super().append_string(_clean_text(keyvault.redact(_clean_text(string))[0]))
            except (UnicodeError, OSError):
                pass

    HOME_DIR.mkdir(parents=True, exist_ok=True)
    session = PromptSession(
        history=RedactingHistory(str(HOME_DIR / "history")),
        completer=SlashCompleter(), complete_while_typing=True,
        key_bindings=kb, multiline=True, style=style, lexer=PillLexer(),
        prompt_continuation=lambda width, line, wrap: FormattedText([("class:continuation", "  ")]),
        bottom_toolbar=lambda: FormattedText(app.toolbar()),
        # The hint is cut to the window so it never wraps into a second row of the box.
        placeholder=lambda: FormattedText([("class:placeholder", _fit(
            "Ask Forge to build, fix or explain something…   / for commands · tab completes",
            _term_cols(session.app) - 4))]),
        reserve_space_for_menu=6,
        refresh_interval=0.1,   # keeps the pixel city and live status above the box moving
    )

    layout: dict = {"cols": 0, "above": 0, "drawn": False}

    def message():
        """What sits above and in front of the box: the live status while Forge works, and the
        question being asked when it waits on you."""
        parts = []
        try:
            # How tall the box is right now (typed lines plus wrapped ones), so the live status
            # above it shrinks instead of pushing the box and the footer off the screen.
            cols = max(20, _term_cols(session.app) - 4)
            doc = session.default_buffer.document
            app.ui.input_rows = sum(max(1, -(-len(ln) // cols)) for ln in doc.lines) + (2 if app.ui.pending else 0)
        except Exception:
            app.ui.input_rows = 1
        status = app.ui.status_ansi()
        if status:
            parts += to_formatted_text(ANSI(status))
        # The box's top edge: a thin neon line with the Forge mark, drawn by prompt_toolkit itself
        # so it always moves with the box (multi-line input, pastes, the menu, resizes).
        # Width is read fresh on every draw (never cached), one column short of the window so the
        # line can never wrap by itself.
        width = _term_cols(session.app)
        edge = max(10, width - 1)
        label = " ◆ forge "
        parts += [("class:edge", "╭─"), ("class:edge.mark", label),
                  ("class:edge", "─" * max(0, edge - len(label) - 2) + "\n")]
        p = app.ui.pending
        if p:
            parts += [("class:ask", f"{p['label']}\n"), ("class:prompt", "› ")]
        else:
            parts.append(("class:prompt", "❯ "))
        # What the resize handler needs to erase this box cleanly after a zoom.
        layout.update(cols=width, drawn=True,
                      above=sum(str(frag[1]).count("\n") for frag in parts))
        return parts

    def ask():
        # Plain prompt drawn entirely by prompt_toolkit. The old box had its top and bottom lines
        # printed by rich outside the prompt, so they went out of step with multi-line input,
        # pastes, the completion menu and window resizes, and littered the scrollback.
        if not app.busy():
            console.print()
        layout["drawn"] = False          # nothing of this prompt is on screen yet
        try:
            return pastes.expand(_clean_text(session.prompt(message)))
        finally:
            layout["drawn"] = False      # submitted: the box is history now, never erase it again

    _install_resize_fix(session.app, layout)
    app._pt_app = session.app
    ask.docked = True
    app._redraw = lambda: session.app.invalidate() if session.app.is_running else None
    try:
        from . import courier_hd
        courier_hd.attach(app.ui, session.app)   # the courier in real pixels where the terminal can
    except Exception:
        pass
    return ask


# --- main ------------------------------------------------------------------------

def setup_without_app(ui: UI) -> None:
    """Minimal key walkthrough for when no provider is usable yet (so no App exists)."""
    from .config import KNOWN_PROVIDERS
    import getpass
    store = keyvault.KeyStore()
    ui.info("No usable provider. Paste an API key to start (" +
            ", ".join(f"{n}: {keyvault.SIGNUP[n][2]}" for n in ("groq", "gemini", "openrouter")) + ")")
    while True:
        try:
            key = getpass.getpass("  key (hidden, enter = done): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not key:
            return
        provider = keyvault.detect_provider(key) or keyvault.identify(
            key, {n: v for n, v in KNOWN_PROVIDERS.items()}, keyvault.candidates(key) or None)
        if not provider:
            ui.error("couldn't tell which provider that key is for — start forge and use /keys add")
            continue
        base, model = KNOWN_PROVIDERS.get(provider, ("", ""))
        ok, why = keyvault.test_key(base, key, model) if base else (True, "")
        if not ok and "rejected" in why:
            ui.error(f"✗ {provider} {keyvault.mask(key)}: {why}")
            continue
        store.add(key, provider)
        ui.ok(f"✓ {provider} key {keyvault.mask(key)} saved · {why}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="forge", description="Terminal coding agent with key rotation and provider failover.")
    ap.add_argument("prompt", nargs="*", help="run one prompt and exit (otherwise starts the chat)")
    ap.add_argument("-y", "--yes", action="store_true", help="auto-approve file writes and shell commands")
    ap.add_argument("-v", "--verbose", action="store_true", help="show key rotation / failover log")
    ap.add_argument("-m", "--model", help="model for this run (for the first provider, or provider:model)")
    ap.add_argument("-p", "--provider", help="use only this provider for this run")
    ap.add_argument("-c", "--continue", dest="cont", action="store_true",
                    help="continue the most recent conversation in this folder")
    ap.add_argument("-r", "--resume", metavar="ID", nargs="?", const="",
                    help="continue a saved conversation (# from --sessions, or id); no ID shows a picker")
    ap.add_argument("--sessions", action="store_true", help="list saved conversations and exit")
    ap.add_argument("--status", action="store_true", help="print key status and exit")
    ap.add_argument("-t", "--team", action="store_true", help="start in team mode (a coordinator delegates by strength)")
    ap.add_argument("--roster", action="store_true", help="show what each model is good at and exit")
    ap.add_argument("--json", action="store_true", help="emit one-shot prompt results as JSON on stdout")
    ap.add_argument("--update", action="store_true", help="install the newest Forge from PyPI and exit")
    ap.add_argument("--version", action="version", version=f"forge {__version__}")
    args = ap.parse_args(argv)
    if args.update:
        from . import update as upd
        latest = upd.fetch()
        if latest and not upd.newer(latest):
            print(f"Forge {__version__} is the newest version.")
            return 0
        kind, cmd = upd.how_installed()
        if kind == "exe":
            print(f"Download the new forge.exe zip from {RELEASES} and replace the old forge.exe.")
            return 0
        if kind == "source" or not cmd:
            print("This Forge runs from a source folder: update it with  git pull  there.")
            return 0
        print(f"Updating Forge {__version__} → {latest or 'newest'}:  {' '.join(cmd)}")
        return subprocess.run(cmd).returncode
    if args.json and not args.prompt:
        ap.error("--json requires a one-shot prompt")
    if args.json:
        global console
        console = Console(file=sys.stderr, theme=uimod.THEME, highlight=False, soft_wrap=False)
        uimod.console = console

    from . import loader
    if not (args.prompt or args.status or args.sessions or args.roster) and args.resume is None             and sys.stdin.isatty():
        # The chat is starting: a loading screen while settings, keys, models and skills load.
        # Stopped (and wiped) just before the start screen, or on any early exit.
        import atexit
        loader.start()
        atexit.register(loader.stop)
    settings = load_settings()
    settings.auto_approve = args.yes
    settings.verbose = args.verbose
    apply_prefs(settings, load_prefs())
    ui = UI(verbose=args.verbose)

    if not settings.providers and sys.stdin.isatty():
        loader.stop()
        # Nothing usable at all (keyless gateways turned off): add a key before the app starts.
        setup_without_app(ui)
        settings = load_settings()
        settings.auto_approve, settings.verbose = args.yes, args.verbose
        apply_prefs(settings, load_prefs())
    if not settings.providers:
        if args.json:
            print(json.dumps({"ok": False, "error": "No providers configured."}))
        else:
            console.print(SETUP)
        return 1

    session = None
    pick = args.resume == ""          # `forge -r` with no ID → choose from a list once the app is up
    if args.cont or args.resume:
        sid = sess.find(args.resume) if args.resume else sess.latest_for(os.getcwd())
        if not sid:
            loader.stop()
            ui.error("no matching saved conversation")
            return 1
        session = Session.load(sid)

    agent = Agent(settings, ui, session=session)
    app = App(agent, ui)

    if args.provider:
        if not agent.router.get(args.provider.lower()):
            loader.stop()
            ui.error(f"no provider {args.provider!r} configured")
            return 1
        agent.router.pinned = args.provider.lower()
    if args.model:
        prov_name, _, model = args.model.partition(":") if ":" in args.model and \
            agent.router.get(args.model.split(":", 1)[0].lower()) else ("", "", args.model)
        prov = agent.router.get(prov_name.lower()) if prov_name else agent.router.lead()
        prov.model = model  # this run only; /model saves a choice

    if args.status:
        app.cmd_status("")
        return 0
    if args.roster:
        app.cmd_roster("")
        return 0
    if args.team and not agent.team:
        if not agent.set_team(True):
            ui.warn("team mode: no usable coordinator, continuing without it")
    if args.sessions:
        app.cmd_sessions("")
        return 0

    if args.prompt:
        try:
            agent.run_turn(" ".join(args.prompt))
        except KeyboardInterrupt:
            ui.warn("interrupted")
            return 130
        if args.json:
            answer = next((str(message.get("content") or "") for message in reversed(agent.messages)
                           if message.get("role") == "assistant" and not message.get("tool_calls")), "")
            print(json.dumps({"ok": agent.last_turn_ok, "answer": answer,
                              "model": agent.last_answer, "session_id": agent.session.id},
                             ensure_ascii=False))
        return 0 if agent.last_turn_ok else 1

    # NEXT 7: first run with no keys of your own → the key walkthrough (enter skips it; asked once).
    if sys.stdin.isatty() and not app.prefs.get("setup_done") and \
            all(p.pool.keys == [keyvault.ANON] for p in agent.router.providers):
        loader.stop()
        app.cmd_setup("")
        app.prefs["setup_done"] = True
        save_prefs(app.prefs)
    failing = app.failing_keys()
    if failing:
        # shown under the start screen (the page is cleared just before it)
        app.start_note = "keys failing: " + ", ".join(failing[:4]) + (" …" if len(failing) > 4 else "") + " · /keys test"
    return app.loop(replay=session, pick=pick)


if __name__ == "__main__":
    sys.exit(main())
