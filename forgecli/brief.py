"""The pinned brief: what the user asked for and already decided, kept in front of the model.

Long builds overflow the context, and trimming used to drop the very things the model needed most:
the real request (when the newest message was only "continue") and the answers to its own
questions. With those gone it interviewed the user again, invented details ("product designer"),
and re-read the same reference files. The brief is rebuilt from the full saved conversation before
every request and joined to the system message, so trimming can never remove it.

It also powers two guards on ask_user: a question on a topic the user already answered gets that
answer back instead of reaching the user, and an interview is capped at a few questions."""
from __future__ import annotations

import functools
import json
import re

ATTACH_MARK = "\n\n[Forge: the user attached"
LINK_MARK = "\n\n[Forge: the user gave a link"
ANSWER_PREFIX = "The user answered: "
MAX_QUESTIONS = 2          # ask_user calls per request before Forge says "build with what you have"

_CONTINUE = re.compile(
    r"^\s*(?:ok(?:ay)?|yes|yep|yeah|sure|go|go on|go ahead|continue|keep going|carry on|proceed|resume|"
    r"next|do it|please continue|continue please|keep building|finish(?: it)?|carry on please)"
    r"[\s.!,]*$", re.I)

# Questions are compared by topic, not wording: "Which Hallmark theme resonates most?" and
# "Which visual style appeals to you?" are the same question.
TOPICS = {
    "style": ("theme", "style", "visual", "look", "aesthetic", "vibe", "palette", "colour", "color",
              "mood", "design direction", "feel"),
    "goal": ("goal", "purpose", "aim", "objective", "visitors", "what do you want", "primary"),
    "identity": ("your name", "who are you", "what do you do", "your role", "job title", "profession",
                 "describe yourself", "occupation"),
    "content": ("case stud", "projects", "work should", "show", "screenshots", "materials", "gallery",
                "portfolio include", "content"),
    "pages": ("one page", "multi-page", "multi page", "single page", "how many pages", "separate pages",
              "sections"),
    "audience": ("audience", "who is it for", "target", "who will"),
    "stack": ("framework", "react", "plain html", "tech stack", "which language", "library"),
}


def topic(question: str) -> str | None:
    q = (question or "").lower()
    scores = {name: sum(1 for w in words if w in q) for name, words in TOPICS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else None


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (s or "").lower())}


def similar(a: str, b: str) -> bool:
    ta, tb = topic(a), topic(b)
    if ta and ta == tb:
        return True
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5


@functools.lru_cache(maxsize=512)
def _continuation(text: str) -> bool:
    from . import attach
    if _CONTINUE.match(text):
        return True
    return len(text) < 600 and bool(_CONTINUE.match(attach.strip_paths(text)))


def is_continuation(text) -> bool:
    """'continue', 'yes', 'go on' … — also after attached paths are taken out of the text."""
    return _continuation(own_text(text).strip())


def own_text(content) -> str:
    """The words the user typed, without Forge's attachment note."""
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    text = str(content or "")
    return text.split(ATTACH_MARK, 1)[0].split(LINK_MARK, 1)[0]


def _is_forge(text: str) -> bool:
    return text.lstrip().startswith("[Forge")


def requests(messages: list[dict]) -> list[str]:
    """The user's own messages, oldest first, without Forge's injected notes."""
    out = []
    for m in messages:
        if m.get("role") != "user":
            continue
        text = own_text(m.get("content")).strip()
        if text and not _is_forge(text):
            out.append(text)
    return out


def current_request(messages: list[dict]) -> str:
    """The newest real request: 'continue' and friends point back to the one before."""
    reqs = requests(messages)
    for text in reversed(reqs):
        if not is_continuation(text):
            return text
    return reqs[-1] if reqs else ""


def answers(messages: list[dict]) -> list[tuple[str, str]]:
    """Every (question, answer) pair from ask_user, oldest first."""
    asked = {}
    out = []
    for m in messages:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                if fn.get("name") == "ask_user":
                    try:
                        q = json.loads(fn.get("arguments") or "{}").get("question", "")
                    except (ValueError, AttributeError):
                        q = ""
                    asked[c.get("id")] = str(q)
        elif m.get("role") == "tool" and m.get("tool_call_id") in asked:
            content = str(m.get("content") or "")
            if content.startswith(ANSWER_PREFIX):
                a = own_text(content[len(ANSWER_PREFIX):]).strip()
                out.append((asked[m["tool_call_id"]], a))
            elif content.startswith("Already answered"):
                pass
    return out


def asked_since_request(messages: list[dict]) -> int:
    """ask_user calls since the user's newest real (non-continuation) request."""
    start = 0
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            text = own_text(m.get("content")).strip()
            if text and not _is_forge(text) and not is_continuation(text):
                start = i
    n = 0
    for m in messages[start:]:
        for c in m.get("tool_calls") or [] if m.get("role") == "assistant" else []:
            if (c.get("function") or {}).get("name") == "ask_user":
                n += 1
    return n


def attachments(messages: list[dict]) -> list[str]:
    """Attached paths, from user messages and from answers to ask_user."""
    seen = []
    for m in messages:
        found = list(m.get("attachments") or [])
        content = m.get("content")
        if m.get("role") == "tool" and isinstance(content, str) and ATTACH_MARK in content:
            note = content.split(ATTACH_MARK, 1)[1]
            found += re.findall(r"^- (.+?)  \(", note, re.M)
        for p in found:
            if p not in seen:
                seen.append(p)
    return seen


def recent_files(messages: list[dict], limit: int = 4) -> list[str]:
    """The files of the latest piece of work: those written or edited after the newest user request
    that changed any, newest first. Follow-ups ("make this the logo", "add a gradient") are about this
    work, not about whatever the conversation began with: a model re-built an earlier calculator into
    the website it had just been told to make instead."""
    out: list[str] = []
    for m in reversed(messages):
        if m.get("role") == "user" and out:
            text = own_text(m.get("content")).strip()
            if text and not _is_forge(text) and not is_continuation(text):
                break           # older work belongs to an older request
        if m.get("role") != "assistant":
            continue
        for c in reversed(m.get("tool_calls") or []):
            fn = c.get("function") or {}
            name = fn.get("name")
            if name not in ("write_file", "edit_file", "apply_patch"):
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                continue
            if not isinstance(args, dict):
                continue
            paths = ([str(args.get("path") or "")] if name != "apply_patch" else
                     re.findall(r"^\*\*\* (?:Add|Update) File: (.+)$", str(args.get("patch") or ""), re.M))
            for path in paths:
                path = path.strip()
                if path and path not in out:
                    out.append(path)
            if len(out) >= limit:
                return out[:limit]
    return out


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def build(messages: list[dict], todo: list[dict] | None = None) -> str:
    """The brief text, or '' when there is nothing worth pinning yet."""
    reqs = requests(messages)
    qa = answers(messages)
    files = attachments(messages)
    if len(reqs) < 2 and not qa and not files and not todo:
        return ""   # a one-message conversation is fully visible anyway
    lines = ["[Pinned brief: the user's own words and decisions, always current even when older "
             "messages were trimmed. Never ask again about anything answered here and never invent "
             "details that contradict it; build from it.]"]
    current = current_request(messages)
    earlier = [r for r in reqs if r != current and not is_continuation(r)][-5:]
    if earlier:
        lines.append("Earlier requests (oldest first) - context only. When the user moved on to "
                     "something else (\"instead\", \"not the X\", a new thing to build), the older work "
                     "is finished: never go back to it, rebuild it or reuse its design unless the "
                     "current request asks; where they differ, the newest request wins:")
        lines += [f"- {_clip(r, 300)}" for r in earlier]
    if current:
        lines.append(f"Current request: {_clip(current, 900)}")
    worked = recent_files(messages)
    if worked and len(reqs) > 1:
        lines.append("Latest work (newest first): " + ", ".join(worked) + ". Unless the user names "
                     "something else, a follow-up (\"this\", \"it\", \"the logo\", \"the site\", \"add "
                     "X\") changes this work in place - keep what it is and add to it; don't turn it "
                     "back into an earlier project.")
    if reqs and is_continuation(reqs[-1]) and current != reqs[-1]:
        lines.append(f"The user's newest message was \"{_clip(own_text(reqs[-1]), 60)}\": keep working "
                     "on the current request from where it stopped. Don't start over, don't re-read "
                     "references you already used, and don't interview again.")
    if qa:
        lines.append("Answers the user already gave (final; do not ask these again):")
        lines += [f"- Q: {_clip(q, 160)} → A: {_clip(a, 300)}" for q, a in qa[-12:]]
    if files:
        lines.append("Files the user attached (use them; exact paths):")
        lines += [f"- {p}" for p in files[-10:]]
    if todo:
        open_items = [i.get("task", "") for i in todo if i.get("status") != "done"]
        done_items = [i.get("task", "") for i in todo if i.get("status") == "done"]
        if open_items:
            lines.append("Plan, still open: " + "; ".join(_clip(t, 80) for t in open_items[:10]))
        if done_items:
            lines.append("Plan, done: " + "; ".join(_clip(t, 60) for t in done_items[:10]))
    return "\n".join(lines)


_JUST_DO_IT = re.compile(r"\b(?:just|simple|simply|quick(?:ly)?|basic|you decide|your call|"
                         r"whatever you think|surprise me|don'?t ask|no questions)\b", re.I)
WORTH_ASKING = {"irreversible", "costly_rework", "personal_fact"}


def request_text(messages: list[dict]) -> str:
    """The user's newest real request (not a continuation or a Forge note)."""
    for m in reversed(messages):
        if m.get("role") == "user":
            text = own_text(m.get("content")).strip()
            if text and not _is_forge(text) and not is_continuation(text):
                return text
    return ""


def gate(messages: list[dict], args: dict) -> str | None:
    """Decide whether an ask_user reaches the user. Returns the tool result to use instead, or None
    to ask. Cheap-to-change questions are answered with the model's own default; the user only sees
    questions where a wrong guess is costly, can't be undone, or needs a fact only they know."""
    question = str(args.get("question", ""))
    stakes = str(args.get("stakes", "")).strip().lower()
    default = str(args.get("default", "")).strip()
    request = request_text(messages)
    from .agent import wants_questions          # late import: agent imports brief
    invited = wants_questions(request)
    use_default = (f" Go with your default ({_clip(default, 100)!r})" if default else
                   " Pick the most sensible default yourself")
    if not invited and stakes != "irreversible" and _JUST_DO_IT.search(request):
        return ("Not asked: the user said to just do it." + use_default +
                ", keep working, and list this assumption at the end of your reply.")
    if not invited and stakes not in WORTH_ASKING:
        return ("Not asked: this is cheap to change later, so it isn't worth interrupting the user." +
                use_default + ", keep working, and list this assumption at the end of your reply.")
    return reuse(messages, question)


def gate_label(result: str) -> str:
    if result.startswith("Already"):
        return "already answered — reusing the user's answer"
    if result.startswith("Not asked"):
        return "decided myself instead of asking — assumptions listed at the end"
    return "enough questions — building now"


def reuse(messages: list[dict], question: str) -> str | None:
    """A tool result for an ask_user that shouldn't reach the user, or None to ask it."""
    for q, a in reversed(answers(messages)):
        if similar(question, q):
            return (f"Already answered — the user said: {a!r} (to: {_clip(q, 120)!r}). Don't ask again; "
                    "use that answer.")
    if asked_since_request(messages) > MAX_QUESTIONS:
        return (f"You've already asked {MAX_QUESTIONS} questions for this request. Stop interviewing: "
                "build now with the answers in the pinned brief, pick sensible defaults for anything "
                "still open, and say which defaults you chose.")
    return None
