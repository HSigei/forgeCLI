"""Bounded, dependency-free repository symbol map."""
from __future__ import annotations

import os
import pathlib
import re
import time

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
             ".next", "dist", "build", "target", "bin", "obj"}
EXTENSIONS = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
}
DECLARATION = re.compile(
    r"^\s*(?:(?:export|pub|public|private|protected|static|abstract|async|default|final)\s+)*"
    r"(?:"
    r"(?:async\s+)?(?:def|function|class|interface|enum|struct|trait|type|fn|func|record)\s+"
    r"([A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
    r")"
)


def build(root: pathlib.Path, max_files: int = 1500, max_symbols: int = 250,
          max_dirs: int = 5000, max_seconds: float = 4.0) -> str:
    root = root.resolve()
    if not root.is_dir():
        return f"Error: repository folder not found: {root}"

    started = time.monotonic()
    dirs_seen, files_seen, bytes_seen = 0, 0, 0
    symbols: list[str] = []
    limited = False
    for current, dirs, names in os.walk(root):
        dirs_seen += 1
        if dirs_seen > max_dirs or time.monotonic() - started > max_seconds:
            limited = True
            break
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            file_path = pathlib.Path(current, name)
            if file_path.suffix.lower() not in EXTENSIONS:
                continue
            files_seen += 1
            if files_seen > max_files or time.monotonic() - started > max_seconds:
                limited = True
                break
            try:
                size = file_path.stat().st_size
                if size > 1_000_000 or bytes_seen + size > 12_000_000:
                    continue
                bytes_seen += size
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative = file_path.relative_to(root).as_posix()
            for line_number, line in enumerate(content.splitlines(), 1):
                match = DECLARATION.match(line)
                if match:
                    symbol = match.group(1) or match.group(2)
                    symbols.append(f"{relative}:{line_number} {symbol}")
                    if len(symbols) >= max_symbols:
                        limited = True
                        break
            if limited:
                break
        if limited:
            break

    if not symbols:
        summary = f"No symbols found in {files_seen} source files."
    else:
        summary = f"Repository map ({len(symbols)} symbols from {files_seen} source files):\n"
        summary += "\n".join(symbols)
    if limited:
        summary += "\n… map truncated at a safety limit; narrow the folder or inspect specific files."
    return summary
