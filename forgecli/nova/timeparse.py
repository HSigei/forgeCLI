"""Understands phrases like "in 10 minutes", "at 5pm", "tomorrow at 9", "every weekday at 8:30".

Plain code, no language model: instant and predictable.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WD = "(?:" + "|".join(WEEKDAYS) + ")"
NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
                "forty-five": 45, "sixty": 60}
NUM = r"(?:\d+(?:\.\d+)?|forty-five|fifteen|twenty|thirty|forty|sixty|one|two|three|four|five|six|seven|eight|nine|ten|half an?|an?)"
UNIT = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)"
DURATION = rf"{NUM}\s*{UNIT}(?:\s*(?:and\s+)?(?:a\s+half|{NUM}\s*{UNIT}))*"
MERIDIEM = r"(?:am|pm|a\.m\.|p\.m\.)"
CLOCK = rf"(?:noon|midnight|\d{{1,2}}:\d{{2}}\s*{MERIDIEM}?|\d{{1,2}}\s*{MERIDIEM}|\d{{1,2}}\s*o'?clock)"
PART = r"(?:morning|afternoon|evening|night)"
DEFAULT_HOUR = {"morning": 9, "afternoon": 14, "evening": 18, "night": 21, "tonight": 20}

REC_RE = re.compile(
    rf"\b(?:every|each)\s+(?:\d+\s*(?:minutes?|mins?|hours?|hrs?)|hour|day|weekdays?|{PART}|{WD})"
    rf"(?:\s+(?:at\s+(?:{CLOCK}|\d{{1,2}}(?::\d{{2}})?)|{CLOCK}))?"
    rf"|\b(?:daily|everyday|weekdays)(?:\s+(?:at\s+(?:{CLOCK}|\d{{1,2}}(?::\d{{2}})?)|{CLOCK}))?", re.I)
REL_RE = re.compile(rf"\bin\s+{DURATION}", re.I)
ABS_RE = re.compile(
    rf"\b(?:(?:today|tonight|tomorrow|day after tomorrow|(?:next\s+|on\s+|this\s+)?{WD})(?:\s+{PART})?\s*)"
    rf"(?:(?:at\s+)?(?:{CLOCK}|\d{{1,2}}(?::\d{{2}})?\b))?"
    rf"|\b(?:at\s+)?(?:{CLOCK})|\bat\s+\d{{1,2}}(?::\d{{2}})?\b"
    rf"|\b(?:this\s+)?(?:morning|afternoon|evening)\b", re.I)


def _number(s: str) -> float:
    s = s.strip().lower()
    if s.startswith("half"):
        return 0.5
    try:
        return float(s)
    except ValueError:
        return float(NUMBER_WORDS.get(s, 1))


def _unit_seconds(u: str) -> int:
    u = u.lower()
    return 86400 if u.startswith("day") else 3600 if u.startswith(("hour", "hr")) else 60 if u.startswith("min") else 1


def duration_seconds(phrase: str) -> float | None:
    total, last = 0.0, 0
    for m in re.finditer(rf"({NUM})\s*({UNIT})", phrase, re.I):
        last = _unit_seconds(m.group(2))
        total += _number(m.group(1)) * last
    if re.search(r"\b(?:and\s+)?a\s+half\b", phrase, re.I) and last:
        total += 0.5 * last
    return total or None


def _clock(text: str):
    """-> (hour, minute, meridiem_known) or None. hour may be ambiguous when meridiem_known is False."""
    t = text.lower()
    if re.search(r"\bnoon\b", t):
        return 12, 0, True
    if re.search(r"\bmidnight\b", t):
        return 0, 0, True
    m = re.search(rf"\b(\d{{1,2}}):(\d{{2}})\s*({MERIDIEM})?", t) or re.search(
        rf"\b(\d{{1,2}})()\s*({MERIDIEM})", t) or re.search(r"\b(\d{1,2})()\s*o'?clock", t) or re.search(
        r"\bat\s+(\d{1,2})()()", t)
    if not m:
        return None
    hour, minute, mer = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if hour > 23 or minute > 59:
        return None
    if mer:
        if not 1 <= hour <= 12:
            return None
        return (hour % 12) + (12 if mer == "pm" else 0), minute, True
    if hour == 0 or hour >= 13 or ":" in m.group(0) and hour >= 13:
        return hour, minute, True
    return hour, minute, False


def _pick_hour(hour: int, known: bool, base: datetime, now: datetime, same_day_ok: bool) -> int:
    """For 'at 5' without am/pm: choose the sensible one."""
    if known:
        return hour
    cands = [hour % 12, hour % 12 + 12]
    if same_day_ok:
        future = [h for h in cands if base.replace(hour=h) > now]
        return future[0] if future else cands[0]
    return hour if 7 <= hour <= 11 else (hour % 12 + 12)  # other days: 7-11 morning, 12 and 1-6 afternoon


def next_occurrence(spec: dict, after: datetime) -> datetime:
    kind = spec["type"]
    if kind == "interval":
        return after + timedelta(seconds=spec["seconds"])
    base = after.replace(hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0)
    for add in range(0, 9):
        cand = base + timedelta(days=add)
        if cand <= after:
            continue
        if kind == "daily":
            return cand
        if kind == "weekdays" and cand.weekday() < 5:
            return cand
        if kind == "weekly" and cand.weekday() == spec["weekday"]:
            return cand
    return base + timedelta(days=1)


def _recurrence(phrase: str, now: datetime) -> dict | None:
    t = phrase.lower()
    m = re.search(r"\b(?:every|each)\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)", t)
    if m:
        return {"type": "interval", "seconds": int(m.group(1)) * _unit_seconds(m.group(2))}
    if re.search(r"\b(?:every|each)\s+hour\b", t):
        return {"type": "interval", "seconds": 3600}
    clock = _clock(t)
    if clock:
        h, mi, known = clock
        h = _pick_hour(h, known, now, now, same_day_ok=False)
    else:
        part = re.search(PART, t)
        h, mi = (DEFAULT_HOUR[part.group(0)] if part else 9), 0
    if re.search(r"\bweekdays?\b", t):
        return {"type": "weekdays", "hour": h, "minute": mi}
    wd = re.search(WD, t)
    if wd:
        return {"type": "weekly", "weekday": WEEKDAYS.index(wd.group(0)), "hour": h, "minute": mi}
    if re.search(r"\b(?:every|each)\s+day\b|\bdaily\b|\beveryday\b|\b(?:every|each)\s+" + PART, t):
        return {"type": "daily", "hour": h, "minute": mi}
    return None


def _absolute(phrase: str, now: datetime) -> datetime | None:
    t = phrase.lower()
    day_offset, explicit_day = 0, False
    if "day after tomorrow" in t:
        day_offset, explicit_day = 2, True
    elif "tomorrow" in t:
        day_offset, explicit_day = 1, True
    elif re.search(r"\b(?:today|tonight)\b", t):
        explicit_day = True
    wd = re.search(WD, t)
    clock = _clock(t)
    part = re.search(PART, t)
    tonight = "tonight" in t
    if wd:
        target = WEEKDAYS.index(wd.group(0))
        ahead = (target - now.weekday()) % 7
        if ahead == 0 and not (clock and now.replace(hour=clock[0] % 24, minute=clock[1]) > now):
            ahead = 7
        day_offset, explicit_day = ahead, True
    if not clock and not part and not tonight and not explicit_day:
        return None
    base = (now + timedelta(days=day_offset)).replace(second=0, microsecond=0)
    if clock:
        h, mi, known = clock
        h = _pick_hour(h, known, base.replace(minute=mi), now, same_day_ok=(day_offset == 0))
    else:
        key = "tonight" if tonight else (part.group(0) if part else "morning")
        h, mi = DEFAULT_HOUR[key], 0
    due = base.replace(hour=h, minute=mi)
    if due <= now and not explicit_day:
        due += timedelta(days=1)
    elif due <= now and day_offset == 0 and explicit_day and not wd:
        return None  # "today at 9am" when 9am has passed
    return due


def parse_when(phrase: str, now: datetime | None = None) -> dict | None:
    """-> {"due": datetime, "recur": dict | None} or None if no time was understood."""
    now = now or datetime.now()
    rec = _recurrence(phrase, now) if re.search(r"\b(?:every|each|daily|everyday|weekdays)\b", phrase, re.I) else None
    if rec:
        return {"due": next_occurrence(rec, now), "recur": rec}
    secs = duration_seconds(phrase) if re.search(rf"\b{UNIT}\b", phrase, re.I) else None
    if secs:
        return {"due": now + timedelta(seconds=max(1, secs)), "recur": None}
    due = _absolute(phrase, now)
    return {"due": due, "recur": None} if due else None


def find_when(text: str) -> tuple[int, int] | None:
    """Where in `text` the time expression sits (start, end)."""
    for rx in (REC_RE, REL_RE, ABS_RE):
        m = rx.search(text)
        if m and m.group(0).strip():
            return m.start(), m.end()
    return None


def split_reminder_ex(text: str) -> tuple[str, str, str] | None:
    """'to call mom in 10 minutes' -> ('call mom', 'in 10 minutes', 'to'). None if no time found.
    The third item is the connecting word ('to', 'that', 'about', 'of' or '') so replies can sound natural."""
    span = find_when(text)
    if not span:
        return None
    when = text[span[0]:span[1]].strip()
    message = (text[:span[0]] + " " + text[span[1]:]).strip()
    message = re.sub(r"\s+", " ", message)
    lead = ""
    m = re.match(r"^(?:me\s+)?(to|that|about|of)\s+", message, flags=re.I)
    if m:
        lead = m.group(1).lower()
        message = message[m.end():]
    message = re.sub(r"\s+and$", "", message, flags=re.I).strip(" ,.")
    return (message or "Reminder"), when, (lead if message else "")


def split_reminder(text: str) -> tuple[str, str] | None:
    r = split_reminder_ex(text)
    return (r[0], r[1]) if r else None


def human_duration(seconds: float) -> str:
    secs = int(round(seconds))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s and not h:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " and ".join(parts) or "a moment"


def describe(due: datetime, recur: dict | None, now: datetime | None = None) -> str:
    now = now or datetime.now()
    clock = due.strftime("%I:%M %p").lstrip("0")
    if recur:
        t = recur["type"]
        if t == "interval":
            s = recur["seconds"]
            return "every hour" if s == 3600 else f"every {s // 3600} hours" if s % 3600 == 0 else f"every {s // 60} minutes"
        clock = f"{recur['hour'] % 12 or 12}:{recur['minute']:02d} {'AM' if recur['hour'] < 12 else 'PM'}"
        who = {"daily": "every day", "weekdays": "every weekday",
               "weekly": f"every {WEEKDAYS[recur.get('weekday', 0)].title()}"}[t]
        return f"{who} at {clock}"
    delta = due - now
    secs = round(delta.total_seconds())
    if secs < 60:
        return "right now" if secs <= 3 else f"in {secs} seconds"
    if delta < timedelta(hours=2):
        mins = round(secs / 60)
        return f"in {mins} minute{'s' if mins != 1 else ''}"
    days = (due.date() - now.date()).days
    day = "today" if days == 0 else "tomorrow" if days == 1 else due.strftime("%A") if days < 7 else due.strftime("%d %b")
    return f"{day} at {clock}"
