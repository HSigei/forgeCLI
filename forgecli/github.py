"""GitHub control (NEXT 9 foundation): sort REST calls by risk and check "PR opened"-style claims.

GitHub API credentials can come from GITHUB_TOKEN, the key store, or `gh auth login`; the token is
resolved only for the request and redacted from output. API and git/gh writes use the existing
approval policy, destructive operations always ask, and creation claims require returned URLs.
See HANDOFF.md NEXT 9 for workflow coverage and remaining limits.
"""
from __future__ import annotations

import re

READ, WRITE, DESTRUCTIVE = "read", "write", "destructive"

# Always ask, even in auto mode (HANDOFF NEXT 9, item 3).
_DESTRUCTIVE = [
    ("DELETE", r".*"),                                             # repos, branches, releases, comments…
    ("PUT", r"^/repos/[^/]+/[^/]+/pulls/\d+/merge$"),              # merging a PR
    ("PATCH", r"^/repos/[^/]+/[^/]+$"),                            # repo settings / visibility / rename
    ("PUT", r"^/repos/[^/]+/[^/]+/collaborators/"),
    ("PUT", r"^/repos/[^/]+/[^/]+/actions/secrets/"),
    ("PUT", r"^/repos/[^/]+/[^/]+/branches/[^/]+/protection"),
    ("PATCH", r"^/repos/[^/]+/[^/]+/git/refs/heads/(main|master)$"),  # moving the default branch
    ("POST", r"^/repos/[^/]+/[^/]+/transfer$"),
]


def classify(method: str, path: str) -> str:
    method = method.upper()
    path = "/" + re.sub(r"^https?://[^/]+", "", path).split("?", 1)[0].strip("/")
    if method in ("GET", "HEAD", "OPTIONS"):
        return READ
    for m, rx in _DESTRUCTIVE:
        if method == m and re.match(rx, path):
            return DESTRUCTIVE
    return WRITE


def git_risk(command: str) -> str:
    """Same split for git/gh shell commands."""
    c = command.strip()
    if re.search(r"\bpush\b.*(--force|-f\b|--force-with-lease)|\bpush\b.*\b(main|master)\b"
                 r"|\bgh (repo delete|release delete|pr merge|secret set|repo edit)\b|\bbranch -D\b", c):
        return DESTRUCTIVE
    if re.search(r"\b(push|commit|gh (pr|issue) (create|comment|edit|close)|gh release create)\b", c):
        return WRITE
    return READ


_PR_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/(pull|issues)/\d+")


def pr_url_ok(answer_url: str, tool_outputs: list[str]) -> bool:
    """A claimed PR/issue URL counts only if a tool actually returned it."""
    return bool(_PR_URL.fullmatch(answer_url.strip())) and any(answer_url.strip() in o for o in tool_outputs)
