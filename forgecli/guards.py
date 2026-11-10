"""Hallucination guards.

Two kinds of check, both cheap and offline:

* Write guards run inside write_file / edit_file: they refuse lazy content (placeholders such as
  "... rest of the code unchanged ...") and edits that change nothing.
* The claim check runs on a final answer: every claim the answer makes ("tests pass", "fixed",
  "installed", "created foo.py") must be backed by something a tool actually did this turn.
  An unbacked claim is bounced back to the model once; if it is still there, the answer is shown
  marked as unverified.
"""
from __future__ import annotations

import pathlib
import re
import os
from dataclasses import dataclass, field

# --- placeholders ------------------------------------------------------------------------------
# Phrases a model writes instead of code it couldn't be bothered to produce. Matched per line and
# only when the line looks like a comment / ellipsis line, so prose such as a README is left alone.
_PLACEHOLDER = re.compile(
    r"(rest|remainder) of (the )?(code|file|function|class|implementation|component|logic|method)s?"
    r"|(existing|unchanged|previous|original|same|other) (code|content|lines|implementation|methods|logic)"
    r" *(here|goes here|remains?|stays?|as before|unchanged|omitted)"
    r"|(implement(ation)?|add|put|insert) (the |your )?(logic|code|implementation|rest) here"
    r"|your (code|logic|implementation) (goes )?here"
    r"|todo:? *(implement|add (the )?(logic|code|implementation)|fill in)"
    r"|(code|content|lines|methods) (omitted|truncated) for brevity"
    r"|\.\.\. *(existing|unchanged|rest|same|more|other)\b",
    re.I)
_COMMENTISH = re.compile(r"^\s*(#|//|/\*|\*|<!--|--|;|\.\.\.|…|\{/\*|rem\b)", re.I)
_TODO = re.compile(r"\b(?:TODO|FIXME|XXX)\b", re.I)
_FUNCTION_HEADER = re.compile(r"^(\s*)(?:async\s+)?def\s+\w+\s*\(.*\)\s*(?:->.*?)?\s*:\s*(.*?)\s*$")
_STUB_BODY = re.compile(r"(?:pass|\.\.\.|raise\s+(?:NotImplementedError|NotImplemented)(?:\(.*\))?)")


def placeholders(content: str, old: str = "") -> list[str]:
    """Placeholder lines in content that weren't already in old."""
    before = set(old.splitlines())
    before_stripped = {line.strip() for line in before}
    hits = []
    lines = content.splitlines()
    for line in lines:
        s = line.strip()
        if s in before or not s:
            continue
        if _TODO.search(s) and _COMMENTISH.match(s):
            hits.append(s[:120])
            continue
        if _PLACEHOLDER.search(s) and (_COMMENTISH.match(s) or len(s) < 60 and s.startswith(("...", "…"))):
            hits.append(s[:120])
    for i, line in enumerate(lines):
        header = _FUNCTION_HEADER.match(line)
        if not header:
            continue
        base_indent = len(header.group(1).expandtabs(8))
        statements = [header.group(2).strip()] if header.group(2).strip() else []
        for body_line in lines[i + 1:]:
            if not body_line.strip() or _COMMENTISH.match(body_line):
                continue
            indent = len(body_line) - len(body_line.lstrip(" \t"))
            if indent <= base_indent:
                break
            statements.append(body_line.strip())
        existing_stub = (line.strip() in before_stripped if header.group(2).strip() else
                         line.strip() in before_stripped and all(s in before_stripped for s in statements))
        if statements and all(_STUB_BODY.fullmatch(s) for s in statements) and not existing_stub:
            hits.append(line.strip()[:120])
    return hits


# --- the evidence a turn produced ---------------------------------------------------------------
@dataclass
class Step:
    name: str
    args: dict
    ok: bool
    out: str = ""


@dataclass
class Ledger:
    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, args: dict, result: str) -> None:
        output = result or ""
        if len(output) > 4000:
            output = output[:2000] + "\n… output truncated …\n" + output[-2000:]
        self.steps.append(Step(name, args if isinstance(args, dict) else {}, succeeded(name, result), output))

    def clear(self) -> None:
        self.steps.clear()

    def last_change(self) -> int:
        """Index of the last step that changed files (-1 if none)."""
        for i in range(len(self.steps) - 1, -1, -1):
            s = self.steps[i]
            if s.ok and s.name in ("write_file", "edit_file", "apply_patch"):
                return i
        return -1

    def after_change(self) -> list[Step]:
        return self.steps[self.last_change() + 1:]

    def touched(self) -> set[str]:
        """Every path-ish string any tool was given or printed this turn (lower-cased)."""
        seen = set()
        for s in self.steps:
            for v in s.args.values():
                if isinstance(v, str):
                    seen.add(v.lower())
            seen.add(s.out.lower())
        return seen


def succeeded(name: str, result: str) -> bool:
    r = (result or "").lstrip()
    if r.startswith(("Error", "The user denied", "Interrupted", "Stopped before", "Command was stopped")):
        return False
    if name == "run_shell":
        m = re.match(r"(?:The command exited right away \()?exit code (-?\d+)", r)
        if m:
            return m.group(1) == "0"
    return True


# --- claims -------------------------------------------------------------------------------------
_TESTISH = re.compile(r"\b(test|tests|pytest|unittest|jest|vitest|mocha|spec|check|verify|ctest|tox|nox)\b"
                      r"|cargo test|go test|npm (run )?test|_test\.|test_\w+", re.I)
_TEST_PASS = re.compile(
    r"\b(?:[1-9]\d*\s+(?:tests?\s+)?passed|[1-9]\d*\s+passing|test result:\s*ok|"
    r"100%\s+tests?\s+passed|all\s+tests?\s+passed|tests run:\s*[1-9]\d*[,;].*?"
    r"failures:\s*0[,;].*?errors:\s*0|[1-9]\d*\s+tests? completed,\s*0 failed|"
    r"(?m:^ok\s+\S+\s+\d+(?:\.\d+)?s?\s*$)|OK\s*\(\s*[1-9]\d*\s+tests?\s*\))", re.I)
_TEST_FAIL = re.compile(
    r"\b[1-9]\d*\s+(?:failed|failures?|errors?)\b|\b(?:tests?|test suite)\s+failed\b|"
    r"\b0\s+(?:tests?|passed|passing)\b|\bno tests? (?:ran|found|collected)\b", re.I)
_TEST_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:(?:uv\s+run|poetry\s+run)\s+)?(?:"
    r"pytest(?:\.exe)?\b|"
    r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?\s+-m\s+(?:pytest|unittest)\b|"
    r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?\s+[\w./\\-]*(?:test_[\w.-]*\.py|[\w.-]*_test\.py)\b|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b|"
    r"(?:npx\s+)?(?:jest|vitest|mocha)\b|cargo\s+test\b|go\s+test\b|"
    r"(?:mvn|gradle|gradlew)\s+[^|;&]*\btest\b|tox(?:\s|$)|nox(?:\s|$)|ctest\b)", re.I)
_BUILD_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:(?:uv\s+run|poetry\s+run)\s+)?(?:"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build\b|"
    r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?\s+-m\s+(?:compileall|py_compile)\b|"
    r"tsc(?:\.cmd)?\b|make(?:\.exe)?\b|cargo\s+build\b|go\s+build\b|"
    r"(?:\.?[\\/])?(?:mvn|gradle|gradlew)\s+[^|;&]*\b(?:build|package|compile)\b|"
    r"dotnet\s+build\b|msbuild(?:\.exe)?\b|cmake\s+--build\b|"
    r"(?:gcc|g\+\+|clang(?:\+\+)?)\b)", re.I)
_INSTALLISH = re.compile(r"\b(install|add|pip|npm|pnpm|yarn|uv|poetry|conda|choco|winget|brew|apt|"
                         r"cargo|go get|gem|composer)\b", re.I)

_CLAIMS = [
    ("tests", re.compile(r"\b(all |the )?(\d+ )?tests? (now |all |still )*(pass|passed|passing|succeed|"
                         r"succeeded|are green|run green|go green)\b|\btest suite (passes|passed|is green)", re.I)),
    ("build", re.compile(r"\b(build|compil(ation|es))\b[^.\n]{0,20}\b(succeeds|succeeded|passes|passed|"
                         r"works|is clean|without errors)", re.I)),
    ("installed", re.compile(r"\b(i(?:'ve| have)? |successfully |now )installed\b|\binstalled successfully\b",
                             re.I)),
    ("works", re.compile(r"\b(it|this|that|everything|the (app|code|fix|page|script|server|function|bug))"
                         r"( now| should( now)?)? (works?|is working|runs? (fine|correctly|cleanly)|"
                         r"is (now )?fixed)\b|\bshould (now )?work\b|\b(i(?:'ve| have)? )?fixed (it|the)\b", re.I)),
]
# A sentence that hedges, negates or asks is not a claim.
_HEDGE = re.compile(r"\b(not|n't|never|unverified|unable|couldn't|could not|didn't|haven't|wasn't|"
                    r"if|once|after you|when you|try|whether|may|might|until)\b|\?", re.I)

_PATH_TOKEN = re.compile(r"`([^`]{2,400})`|\]\(([^)]{2,400})\)")
_EXT = re.compile(r"\.(py|js|mjs|cjs|ts|tsx|jsx|json|md|txt|html?|css|scss|toml|ya?ml|ini|cfg|sh|ps1|bat|"
                  r"go|rs|java|kt|c|cc|cpp|h|hpp|cs|rb|php|swift|sql|xml|csv|env|lock|vue|svelte)$", re.I)
_PLAIN_PATH = re.compile(
    r"(?<![\w`])((?:[A-Za-z]:[\\/])?(?:[\w.@+-]+[\\/])*[\w.@+-]+\."
    r"(?:py|js|mjs|cjs|ts|tsx|jsx|json|md|txt|html?|css|scss|toml|ya?ml|ini|cfg|sh|ps1|bat|"
    r"go|rs|java|kt|c|cc|cpp|h|hpp|cs|rb|php|swift|sql|xml|csv|env|lock|vue|svelte))"
    r"(?=$|[\s):,;.!?])", re.I)
_SYMBOL = re.compile(r"`([A-Za-z_]\w*)\s*\([^`\n]*\)`")
_SYMBOL_CONTEXT = re.compile(
    r"\b(function|method|class|symbol|call(?:s|ed)?|defined|declared|implements|uses|imports?)\b", re.I)
_CREATED = re.compile(r"\b(created|wrote|added|generated|saved)\b", re.I)
_CHANGE_TOOLS = {"write_file", "edit_file", "apply_patch"}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".next", "dist", "build"}
_GITHUB_CREATION = re.compile(r"\b(?:opened|created)\b.{0,50}\b(?:pull request|PR|issue)\b", re.I)
_GITHUB_URL = re.compile(r"https://github\.com/[^\s\"'<>]+/(?:pull|issues)/\d+")


def _sentences(text: str) -> list[str]:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)   # code samples aren't claims
    return [s.strip() for s in re.split(r"(?<=[.!])\s+|\n+", text) if s.strip()]


def test_succeeded(step: Step) -> bool:
    """Require a successful command and recognizable positive test output, not just exit code zero."""
    command = step.args.get("command", "")
    return (step.ok and _TEST_COMMAND.search(command) is not None and
            _TEST_PASS.search(step.out) is not None and _TEST_FAIL.search(step.out) is None)


def check_claims(answer: str, ledger: Ledger, root: pathlib.Path) -> list[str]:
    """Problems with the answer: claims no tool backed up, and files that don't exist."""
    problems: list[str] = []
    after = ledger.after_change()
    all_shells = [s for s in ledger.steps if s.name == "run_shell"]
    test_runs_after = [s for s in after if s.name == "run_shell" and
                       _TESTISH.search(s.args.get("command", ""))]
    latest_test_after = test_runs_after[-1] if test_runs_after else None
    passing_tests_after = latest_test_after is not None and test_succeeded(latest_test_after)
    checked_after = [s for s in after if s.ok and (
        s.name in ("open_url", "process") or
        s.name == "run_shell" and (
            _BUILD_COMMAND.search(s.args.get("command", "")) or test_succeeded(s)))]
    changed = ledger.last_change() >= 0
    github_outputs = [step.out for step in ledger.steps if step.ok and
                      step.name in {"github_request", "http_request"}]
    github_urls = _GITHUB_URL.findall("\n".join(github_outputs))
    from . import github
    kinds = set()
    for sent in _sentences(answer):
        if _HEDGE.search(sent):
            continue
        for kind, rx in _CLAIMS:
            if kind not in kinds and rx.search(sent):
                kinds.add(kind)
    if "tests" in kinds and not passing_tests_after:
        test_runs = [s for s in all_shells if _TESTISH.search(s.args.get("command", ""))]
        latest_test = test_runs[-1] if test_runs else None
        problems.append("you said the tests pass, but " + (
            "the last test run did not show a passing result" if latest_test and not test_succeeded(latest_test) else
            "no test command showed a passing result" + (" after your last change" if changed else "") + " this turn"))
    if "build" in kinds and not any(
            s.ok and _BUILD_COMMAND.search(s.args.get("command", "")) for s in after if s.name == "run_shell"):
        problems.append("you said it builds, but no build ran successfully"
                        + (" after your last change" if changed else "") + " this turn")
    if "installed" in kinds and not any(_INSTALLISH.search(s.args.get("command", "")) for s in all_shells if s.ok):
        problems.append("you said something was installed, but no install command succeeded this turn")
    # A plain question ("skills", "how does X work") has no change to vouch for: words like
    # "works" there describe the code, not a fix, so a turn that used no tools at all is not checked.
    if "works" in kinds and ledger.steps and not checked_after and "tests" not in kinds:
        problems.append("you said it works / is fixed, but nothing was run or re-checked"
                        + (" after your last change" if changed else " this turn"))
    for sent in _sentences(answer):
        if _GITHUB_CREATION.search(sent):
            claimed_urls = _GITHUB_URL.findall(sent)
            if not any(url in github_urls and
                       github.pr_url_ok(url, github_outputs)
                       for url in claimed_urls):
                problems.append("you claimed a GitHub pull request or issue was created without including "
                                "a returned GitHub URL")
        if not _CREATED.search(sent):
            continue
        paths = _path_candidates(sent)
        if paths and not any(_written_this_turn(path, ledger, root) for path in paths):
            problems.append("you said you created a file, but no successful file write for its path "
                            "happened this turn: " + ", ".join(paths[:5]))
    missing = missing_paths(answer, ledger, root)
    if missing:
        problems.append("these files you mention don't exist: " + ", ".join(missing[:5]))
    missing_names = missing_symbols(answer, root, ledger)
    if missing_names:
        problems.append("these function names were not found in the project: " + ", ".join(missing_names[:5]))
    return problems


def _path_candidates(text: str) -> list[str]:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    candidates = []
    for match in _PATH_TOKEN.finditer(text):
        candidates.append((match.group(1) or match.group(2) or "").strip().rstrip(".,:;"))
    candidates.extend(match.group(1) for match in _PLAIN_PATH.finditer(text))
    return list(dict.fromkeys(candidates))


def _resolved(path: str, root: pathlib.Path) -> str:
    p = pathlib.Path(path).expanduser()
    try:
        return os.path.normcase(str((p if p.is_absolute() else root / p).resolve()))
    except OSError:
        return os.path.normcase(str(p if p.is_absolute() else root / p))


def _written_this_turn(path: str, ledger: Ledger, root: pathlib.Path) -> bool:
    path = re.sub(r"#L?\d+(-L?\d+)?$|:\d+(-\d+)?$", "", path)
    target = _resolved(path, root)
    # "Files created in C:\site" names the folder: a write to any file inside it counts.
    inside = target.rstrip("\\/") + os.sep

    def hit(written: str) -> bool:
        w = _resolved(written, root)
        return w == target or w.startswith(inside)

    for step in ledger.steps:
        if not step.ok or step.name not in _CHANGE_TOOLS:
            continue
        if step.name != "apply_patch":
            if hit(str(step.args.get("path", ""))):
                return True
            continue
        patch_paths = re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$",
                                 str(step.args.get("patch", "")), re.M)
        if any(hit(item.strip()) for item in patch_paths):
            return True
    return False


def missing_paths(answer: str, ledger: Ledger, root: pathlib.Path) -> list[str]:
    touched = ledger.touched()
    out = []
    for tok in _path_candidates(answer):
        tok = re.sub(r"#L?\d+(-L?\d+)?$|:\d+(-\d+)?$", "", tok)   # file.py#L12 / file.py:12
        if not tok or "://" in tok or tok.startswith(("-", "$", "@", "<", "*")) or "*" in tok:
            continue
        if not _EXT.search(tok) or "(" in tok or "=" in tok:
            continue
        p = pathlib.Path(tok).expanduser()
        p = p if p.is_absolute() else root / p
        try:
            if p.exists():
                continue
        except OSError:
            continue
        if tok.lower() in touched or any(tok.lower() in t for t in touched if len(t) < 400):
            continue   # a tool talked about it (deleted it, moved it, it was in an error) — not invented
        if "/" not in tok and "\\" not in tok and _cheap(root) and next(root.rglob(tok), None):
            continue   # bare file name that exists somewhere in the project
        out.append(tok)
    return out


def missing_symbols(answer: str, root: pathlib.Path, ledger: Ledger | None = None) -> list[str]:
    candidates = set()
    for sent in _sentences(answer):
        if _SYMBOL_CONTEXT.search(sent):
            candidates.update(_SYMBOL.findall(sent))
    if not candidates:
        return []

    evidenced = set()
    if ledger:
        for step in ledger.steps:
            if not step.ok:
                continue
            evidence = step.out + "\n" + "\n".join(
                value for value in step.args.values() if isinstance(value, str))
            evidenced.update(name for name in candidates
                             if re.search(r"\b" + re.escape(name) + r"\b", evidence))
    candidates -= evidenced
    if not candidates:
        return []

    definitions = re.compile(
        r"\b(?:async\s+def|def|class|function|fn|func|interface|type|const|let|var)\s+("
        + "|".join(re.escape(name) for name in sorted(candidates, key=len, reverse=True))
        + r")\b")
    found = set()
    scanned = 0
    bytes_scanned = 0
    try:
        for current, dirs, names in os.walk(root):
            dirs[:] = [name for name in dirs if name not in _SKIP_DIRS]
            for name in names:
                if pathlib.Path(name).suffix.lower() not in {
                    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
                    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
                }:
                    continue
                scanned += 1
                if scanned > 20000:
                    return []
                file_path = pathlib.Path(current, name)
                try:
                    size = file_path.stat().st_size
                    if size > 1_000_000:
                        continue
                    bytes_scanned += size
                    if bytes_scanned > 16_000_000:
                        return []
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                found.update(definitions.findall(content))
                if found == candidates:
                    return []
    except OSError:
        return []
    return sorted(candidates - found)


def _cheap(root: pathlib.Path) -> bool:
    """rglob is fine for a project folder, not for a home directory or drive root."""
    try:
        return root != pathlib.Path.home() and len(root.parts) > 1 and sum(1 for _ in root.iterdir()) < 400
    except OSError:
        return False


def bounce_message(problems: list[str]) -> str:
    return ("[Forge check] Before this answer goes to the user: " + "; ".join(problems) + ". "
            "Either do it now (run the tests or command, re-check the file), or rewrite the answer so it "
            "says plainly what was not verified. Don't repeat a claim no tool has backed up.")
