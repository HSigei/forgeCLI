"""Text hygiene for anything typed, pasted or produced that is about to be stored or sent.

Windows consoles and clipboards hand Python text as UTF-16 code units, so an emoji or other
character outside the basic plane can arrive as two lone *surrogate* characters. Python happily
holds them in a str but cannot write them as UTF-8, which is the
"'utf-8' codec can't encode characters ... surrogates not allowed" crash.

clean() fixes that at the source: a valid surrogate pair is joined back into the real character
(so a pasted emoji survives), a lone surrogate becomes U+FFFD, and NUL bytes are dropped.
"""
from __future__ import annotations

import re

_SURROGATE = re.compile("[\ud800-\udfff]")


def has_surrogates(text: str) -> bool:
    return bool(_SURROGATE.search(text))


def clean(text):
    """A str that is always safe to encode as UTF-8. Non-str values are returned untouched."""
    if not isinstance(text, str):
        return text
    if "\x00" in text:
        text = text.replace("\x00", "")
    if not has_surrogates(text):
        return text
    try:
        # re-pair UTF-16 halves that were split into two str characters
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    except (UnicodeError, ValueError):
        return text.encode("utf-8", "replace").decode("utf-8", "replace")


def clean_deep(obj):
    """clean() applied through nested dicts, lists and tuples (messages, tool results)."""
    if isinstance(obj, str):
        return clean(obj)
    if isinstance(obj, dict):
        return {clean(k): clean_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_deep(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(clean_deep(v) for v in obj)
    return obj
