"""Images the user gives Forge: a pasted screenshot (alt+v saves it and types its path) or the path
of an image file in a message.

A user message keeps its text as a plain string, so everything that trims or summarises history keeps
working, and lists the images under "images". Only when a request is sent does `parts()` turn the
newest message's images into content parts, and only for a model that can see them; any other model
is told plainly that an image was attached that it can't see, so it asks instead of guessing."""
from __future__ import annotations

import base64
import pathlib
import re

EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp"}
MAX_BYTES = 8 * 1024 * 1024

# A quoted path, or an unquoted one without spaces, ending in an image extension.
_PATH = re.compile(r"""(?:"([^"\n]+?\.(?:png|jpe?g|gif|webp|bmp))"|'([^'\n]+?\.(?:png|jpe?g|gif|webp|bmp))'"""
                   r"""|((?:[A-Za-z]:[\\/]|[\\/]|~[\\/]|\.{1,2}[\\/])?[^\s"'<>|]*?\.(?:png|jpe?g|gif|webp|bmp)))"""
                   r"""(?=$|[\s"',;:)\]])""", re.I)
# Windows paths with spaces, typed or pasted without quotes ("C:\...\Screenshot (2349).png").
_SPACED = re.compile(r"[A-Za-z]:[\\/][^\n\"'<>|*?]*?\.(?:png|jpe?g|gif|webp|bmp)(?=$|[\s\"',;)\]])", re.I)

# Model names that take images (OpenAI-style image_url parts). Anything else gets a text note.
_SEES = re.compile(r"(^|[-/_.:])(vl|vision|omni)([-/_.:]|$)|qwen3\.5|qwen2\.5-vl|gemini|gemma-3|"
                   r"gpt-4o|gpt-4\.1|gpt-5|claude|llama-4|pixtral|mistral-(small|medium)-3|kimi-k2\.5|"
                   r"glm-4\.\dv|grok-4", re.I)


def sees_images(model: str) -> bool:
    return bool(_SEES.search(model or ""))


def find(text: str, root: pathlib.Path | None = None) -> list[str]:
    """Image files named in the text that exist, as absolute paths, in order, without repeats."""
    root = pathlib.Path(root or pathlib.Path.cwd())
    found: list[str] = []
    cands = [m.group(1) or m.group(2) or m.group(3) for m in _PATH.finditer(text or "")]
    cands += [m.group(0) for m in _SPACED.finditer(text or "")]
    for c in cands:
        if not c:
            continue
        p = pathlib.Path(c.strip()).expanduser()
        if not p.is_absolute():
            p = root / p
        try:
            if p.suffix.lower() in EXTS and p.is_file() and 0 < p.stat().st_size <= MAX_BYTES:
                s = str(p.resolve())
                if s not in found:
                    found.append(s)
        except OSError:
            continue
    return found[:4]


def user_message(text: str, root: pathlib.Path | None = None) -> dict:
    msg = {"role": "user", "content": text}
    found = find(text, root)
    if found:
        msg["images"] = found
    return msg


def has_images(messages) -> bool:
    return any(m.get("images") for m in messages if m.get("role") == "user")


def _data_url(path: str) -> str | None:
    p = pathlib.Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > MAX_BYTES:
        return None
    return f"data:{EXTS.get(p.suffix.lower(), 'image/png')};base64,{base64.b64encode(raw).decode()}"


def parts(messages: list[dict], model: str) -> list[dict]:
    """The messages as sent to `model`: the newest user message with images gets them as image
    parts when the model can see them; every other image becomes a one-line note."""
    last = max((i for i, m in enumerate(messages) if m.get("role") == "user" and m.get("images")),
               default=-1)
    if last < 0:
        return messages
    vision = sees_images(model)
    out = []
    for i, m in enumerate(messages):
        imgs = m.get("images") if m.get("role") == "user" else None
        if not imgs:
            out.append(m)
            continue
        text = m.get("content") or ""
        names = ", ".join(pathlib.Path(p).name for p in imgs)
        if i == last and vision:
            content = [{"type": "text", "text": text + f"\n\n[Forge: the image(s) {names} are attached below.]"}]
            for p in imgs:
                url = _data_url(p)
                if url:
                    content.append({"type": "image_url", "image_url": {"url": url}})
            out.append(dict(m, content=content if len(content) > 1 else text))
        elif i == last:
            out.append(dict(m, content=text + (
                f"\n\n[Forge: the user attached the image(s) {names}, but this model can't see images. "
                "Don't open or guess at the file: say you can't see it and ask what it shows (or work "
                "from what they wrote).]")))
        else:
            out.append(dict(m, content=text + f"\n[earlier image(s): {names}]"))
    return out
