"""Wake-phrase matching and transcript clean-up. Pure Python, so it is easy to test."""
from __future__ import annotations

import re
import statistics
from difflib import SequenceMatcher

PREFIXES = {"hey", "hi", "ok", "okay", "okey", "hello", "yo", "hay", "hei", "aye"}
FILLERS = {"uh", "um", "so", "well", "and", "oh", "ah", "erm"}
PHANTOMS = {"thank you", "thanks for watching", "thank you for watching", "you", "bye", "bye bye", "subscribe",
            "please subscribe", "like and subscribe", "thanks", "the end", "okay", "mm", "hmm"}
YES = re.compile(r"^(?:yes|yeah|yep|yup|correct|right|exactly|that'?s (?:right|correct)|go ahead|do it|sure|please do)\b", re.I)
NO = re.compile(r"^(?:no|nope|nah|wrong|incorrect|that'?s (?:wrong|not right)|not (?:quite|really))\b", re.I)


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[A-Za-z0-9']+", text)]


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def match_wake(text: str, name: str, sensitivity: float = 0.7, allow_bare: bool = False,
               extra: tuple | list = ()) -> tuple[bool, str]:
    """Does `text` start with the wake phrase ("hey <name>")? Returns (matched, rest_of_the_text).

    Tolerant of how speech recognition mishears a name ("Noah", "Nover") and of a stray filler word first.
    `rest` keeps the original wording and punctuation of whatever followed the wake phrase.
    """
    toks = _tokens(text)
    name_toks = name.lower().split() or [name.lower()]
    n = len(name_toks)
    target = " ".join(name_toks)

    def name_at(i: int, thr: float) -> bool:
        chunk = toks[i:i + n]
        return len(chunk) == n and _sim(" ".join(t[0] for t in chunk), target) >= thr

    def done(end_index: int) -> tuple[bool, str]:
        rest = text[toks[end_index][2]:] if end_index < len(toks) else ""
        return True, rest.lstrip(" ,.!?:;-\u2013\u2014\"'")

    for skip in (0, 1):  # allow one filler word before the phrase ("um, hey nova")
        if skip and not (toks and toks[0][0].lower() in FILLERS):
            break
        i = skip
        if i < len(toks) and toks[i][0].lower() in PREFIXES and name_at(i + 1, sensitivity):
            return done(i + n)
        if i < len(toks) and _sim(toks[i][0], "hey" + target.replace(" ", "")) >= min(0.95, sensitivity + 0.1):
            return done(i)  # heard as one word: "heynova"
        if allow_bare and name_at(i, max(0.85, sensitivity)):
            return done(i + n - 1)
        for phrase in extra:
            pt = [p.lower() for p in re.findall(r"[A-Za-z0-9']+", phrase)]
            if pt and [t[0].lower() for t in toks[i:i + len(pt)]] == pt:
                return done(i + len(pt) - 1)
    return False, text


def strip_wake(text: str, name: str, sensitivity: float = 0.7, allow_bare: bool = False,
               extra: tuple | list = ()) -> tuple[bool, str]:
    """For the final transcript: (found, command_text_without_the_wake_phrase)."""
    return match_wake(text, name, sensitivity, allow_bare, extra)


def is_phantom(text: str, avg_logprob: float, no_speech_prob: float) -> bool:
    """Whisper sometimes 'hears' phrases like 'Thank you.' in silence or noise. Drop those, keep real ones."""
    t = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if not t:
        return True
    if no_speech_prob > 0.85:
        return True
    return t in PHANTOMS and (avg_logprob < -0.8 or no_speech_prob > 0.5)


def apply_corrections(text: str, mapping: dict) -> str:
    for wrong, right in (mapping or {}).items():
        if wrong:
            text = re.sub(rf"\b{re.escape(str(wrong))}\b", str(right), text, flags=re.I)
    return text


def build_prompt(name: str, user_name: str = "", vocabulary: list | None = None, limit: int = 220) -> str:
    """A short 'previous context' that nudges recognition toward your names and words."""
    bits = [f"Hey {name}."]
    if user_name:
        bits.append(f"{user_name}.")
    vocab = [str(v).strip() for v in (vocabulary or []) if str(v).strip()]
    if vocab:
        bits.append(", ".join(vocab) + ".")
    return " ".join(bits)[:limit]


def mean_logprob(values: list[float]) -> float:
    return statistics.fmean(values) if values else -5.0


def is_yes(text: str) -> bool:
    return bool(YES.match(text.strip()))


def is_no(text: str) -> bool:
    return bool(NO.match(text.strip()))


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate (0 = perfect). Case and punctuation are ignored."""
    norm = lambda s: re.findall(r"[a-z0-9']+", s.lower())
    r, h = norm(reference), norm(hypothesis)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[-1] / len(r)
