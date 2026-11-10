"""Files the user hands Forge by typing or pasting their path: any kind, not just images.

A path in a message (or in an answer to one of Forge's questions) is found even with spaces, without
quotes, or with words glued to its end ("C:\\clip.mp4continue"). Each file becomes an attachment note
the model can't miss: what it is, how big, and what to do with it. Word documents, PDFs, slides and
text files have their text pulled out right here, so the model never has to guess at a parser (the
old way: `python3 -c "import docx"` failing four times in a row). Images still go to vision models
through images.py."""
from __future__ import annotations

import pathlib
import re
import zipfile

from . import images

TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".toml", ".html", ".htm", ".css", ".js",
             ".ts", ".tsx", ".jsx", ".py", ".rtf", ".xml", ".ini", ".log", ".sql", ".php", ".java", ".c",
             ".cpp", ".h", ".go", ".rs", ".rb", ".sh", ".ps1", ".bat", ".svg", ".tex", ".bib"}
VIDEO = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
AUDIO = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"}
FONTS = {".woff", ".woff2", ".ttf", ".otf"}
ARCHIVES = {".zip", ".tar", ".gz", ".7z", ".rar"}
MAX_TEXT = 8000          # characters of a document put into the conversation
MAX_FILES = 8

# Where a path can start: a drive, a UNC share, home, or ./ ../
_START = re.compile(r"""(?:(?<=^)|(?<=[\s"'(\[<,;]))(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|\.{1,2}[\\/]|/(?=[^\s/]))""")
_QUOTED = re.compile(r""""([^"\n]{2,400})"|'([^'\n]{2,400})'""")


def kind(path: pathlib.Path) -> str:
    if path.is_dir():
        return "folder"
    ext = path.suffix.lower()
    if ext in images.EXTS:
        return "image"
    if ext in VIDEO:
        return "video"
    if ext in AUDIO:
        return "audio"
    if ext in {".docx", ".pdf", ".pptx", ".odt", ".doc"}:
        return "document"
    if ext in {".xlsx", ".xls", ".ods"}:
        return "spreadsheet"
    if ext in FONTS:
        return "font"
    if ext in ARCHIVES:
        return "archive"
    if ext in TEXT_EXTS:
        return "text"
    return "file"


def _exists(raw: str, root: pathlib.Path) -> pathlib.Path | None:
    raw = raw.strip().rstrip(".,;:)]}>!?")
    if not raw:
        return None
    p = pathlib.Path(raw).expanduser()
    if not p.is_absolute():
        p = root / p
    try:
        if p.exists():
            return p.resolve()
    except (OSError, ValueError):
        return None
    return None


def _longest(text: str, start: int, root: pathlib.Path) -> tuple[pathlib.Path, int] | None:
    """The longest existing path that starts at `start`, so spaces and glued words both work:
    "C:\\a b\\clip.mp4continue" → C:\\a b\\clip.mp4. Files beat folders that are only a prefix."""
    end = text.find("\n", start)
    end = len(text) if end < 0 else end
    chunk = text[start:min(end, start + 400)]
    best = None
    # Candidate cut points: every position after an extension-looking end, or a separator.
    for i in range(len(chunk), 1, -1):
        cand = chunk[:i]
        if cand[-1] in " \t":
            continue
        nxt = chunk[i:i + 1]
        is_file_end = bool(re.search(r"\.[A-Za-z0-9]{1,5}$", cand))
        if not (is_file_end or nxt in ("", " ", "\t", '"', "'", ",", ";", ")") or cand[-1] in "\\/"):
            continue
        p = _exists(cand, root)
        if p is not None:
            if p.is_file():
                return p, start + len(cand)
            if best is None and nxt in ("", " ", "\t", '"', "'", ",", ";", ")"):
                best = (p, start + len(cand))
    return best


def find(text: str, root: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Existing files and folders named in the text, in order, without repeats."""
    root = pathlib.Path(root or pathlib.Path.cwd())
    text = text or ""
    found: list[pathlib.Path] = []

    def add(p):
        if p is not None and p not in found and str(p) != str(root.resolve()):
            found.append(p)

    for m in _QUOTED.finditer(text):
        add(_exists(m.group(1) or m.group(2), root))
    for m in _START.finditer(text):
        hit = _longest(text, m.start(), root)
        if hit:
            add(hit[0])
    # Bare relative names with an extension ("photo.jpg", "docs/cv.pdf").
    for m in re.finditer(r"(?<![\w\\/:.-])([\w.-]+(?:[\\/][\w .-]+)*\.[A-Za-z0-9]{1,5})(?![\w\\/])", text):
        add(_exists(m.group(1), root))
    # Drop a folder when a file inside it was also named (the folder was just a prefix).
    files = [p for p in found if p.is_file()]
    found = [p for p in found if p.is_file() or not any(f.is_relative_to(p) for f in files)]
    return found[:MAX_FILES]


def strip_paths(text: str, root: pathlib.Path | None = None) -> str:
    """The text with every existing path taken out: "C:\\clip.mp4continue" → "continue"."""
    root = pathlib.Path(root or pathlib.Path.cwd())
    text = text or ""
    cuts = []
    for m in _QUOTED.finditer(text):
        if _exists(m.group(1) or m.group(2), root):
            cuts.append((m.start(), m.end()))
    for m in _START.finditer(text):
        hit = _longest(text, m.start(), root)
        if hit:
            cuts.append((m.start(), hit[1]))
    out, pos = [], 0
    for a, b in sorted(cuts):
        if a >= pos:
            out.append(text[pos:a])
            pos = b
    out.append(text[pos:])
    return " ".join("".join(out).split())


def _size(p: pathlib.Path) -> str:
    try:
        n = p.stat().st_size
    except OSError:
        return "?"
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return "?"


def _xml_text(xml: str, para: str, run: str) -> str:
    out = []
    for p in re.findall(rf"<{para}[ >].*?</{para}>", xml, re.S):
        t = "".join(re.findall(rf"<{run}(?: [^>]*)?>([^<]*)</{run}>", p))
        if t.strip():
            out.append(t)
    return "\n".join(out)


def _unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
             .replace("&apos;", "'").replace("&amp;", "&"))


def extract(p: pathlib.Path) -> str | None:
    """Readable text of a document, or None when it can't be read here."""
    ext = p.suffix.lower()
    try:
        if ext == ".docx":
            with zipfile.ZipFile(p) as z:
                xml = z.read("word/document.xml").decode("utf8", "replace")
            return _unescape(_xml_text(xml, "w:p", "w:t"))
        if ext == ".pptx":
            with zipfile.ZipFile(p) as z:
                names = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                               key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
                slides = [_unescape(_xml_text(z.read(n).decode("utf8", "replace"), "a:p", "a:t"))
                          for n in names]
            return "\n\n".join(f"[slide {i}]\n{s}" for i, s in enumerate(slides, 1) if s.strip())
        if ext == ".odt":
            with zipfile.ZipFile(p) as z:
                xml = z.read("content.xml").decode("utf8", "replace")
            return _unescape(re.sub(r"<[^>]+>", " ", re.sub(r"</text:p>", "\n", xml)))
        if ext == ".pdf":
            try:
                from pypdf import PdfReader  # optional
            except ImportError:
                return None
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages[:30])
        if kind(p) == "text" and p.stat().st_size <= 2_000_000:
            return p.read_text(encoding="utf8", errors="replace")
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, AttributeError):
        return None
    except Exception:  # a broken document must never break the turn
        return None
    return None


_USE = {
    "image": "an image the user wants used; to put it on a page copy it into the project (e.g. "
             "assets/) and reference that copy, replacing any placeholder in the slot it fits",
    "video": "a video the user wants used (e.g. a hero/background); copy it into the project and use a "
             "<video autoplay muted loop playsinline> element pointing at the copy",
    "audio": "an audio file; copy it into the project before referencing it",
    "font": "a font; copy it into the project and load it with @font-face",
    "folder": "a folder the user pointed at; list it before using what's inside",
}


def note(paths: list[pathlib.Path], root: pathlib.Path | None = None) -> str:
    """The attachment block appended to the user's message."""
    if not paths:
        return ""
    lines = ["[Forge: the user attached these — they are inputs to use, not questions to ask about. "
             "Paths are exact; on Windows use `python`, not `python3`.]"]
    bodies = []
    for p in paths:
        k = kind(p)
        lines.append(f"- {p}  ({k}, {_size(p)})" + (f" — {_USE[k]}" if k in _USE else ""))
        if k in ("document", "text", "spreadsheet"):
            text = extract(p)
            if text and text.strip():
                text = re.sub(r"\n{3,}", "\n\n", text.strip())
                cut = len(text) > MAX_TEXT
                bodies.append(f"--- text of {p.name}{' (first part)' if cut else ''} ---\n"
                              f"{text[:MAX_TEXT]}\n--- end of {p.name} ---")
                lines[-1] += " — its text is below, use it directly"
            elif k != "text":
                lines[-1] += " — couldn't read it here; read it with a tool if you need its contents"
    return "\n".join(lines + bodies)


def user_message(text: str, root: pathlib.Path | None = None) -> dict:
    """A user message with every attached file noted and images listed for vision models."""
    msg = images.user_message(text, root)
    paths = find(text, root)
    if paths:
        msg["content"] = (text or "") + "\n\n" + note(paths, root)
        msg["attachments"] = [str(p) for p in paths]
        imgs = [str(p) for p in paths if kind(p) == "image" and 0 < p.stat().st_size <= images.MAX_BYTES]
        merged = list(dict.fromkeys((msg.get("images") or []) + imgs))[:4]
        if merged:
            msg["images"] = merged
    return msg
