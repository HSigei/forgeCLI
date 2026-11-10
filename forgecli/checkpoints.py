"""In-memory file snapshots used by /undo."""
from __future__ import annotations

import os
import pathlib
import stat
import tempfile
from dataclasses import dataclass


@dataclass
class FileState:
    path: pathlib.Path
    existed: bool
    content: bytes = b""
    mode: int | None = None


@dataclass
class Checkpoint:
    files: list[FileState]
    message_index: int | None


class Checkpoints:
    def __init__(self, limit: int = 50):
        self.limit = limit
        self.entries: list[Checkpoint] = []
        self.message_index: int | None = None

    def capture(self, paths: list[pathlib.Path]) -> Checkpoint:
        files = []
        for path in dict.fromkeys(paths):
            exists = path.exists()
            if exists and not path.is_file():
                raise OSError(f"cannot checkpoint non-file path: {path}")
            content = path.read_bytes() if exists else b""
            mode = stat.S_IMODE(path.stat().st_mode) if exists else None
            files.append(FileState(path, exists, content, mode))
        return Checkpoint(files, self.message_index)

    def add(self, checkpoint: Checkpoint) -> None:
        if (checkpoint.message_index is not None and self.entries and
                self.entries[-1].message_index == checkpoint.message_index):
            previous = self.entries[-1]
            known = {state.path for state in previous.files}
            previous.files.extend(state for state in checkpoint.files if state.path not in known)
        else:
            self.entries.append(checkpoint)
        del self.entries[:-self.limit]
        self.message_index = None

    def clear(self) -> None:
        self.entries.clear()
        self.message_index = None

    def restore(self, checkpoint: Checkpoint) -> None:
        for state in reversed(checkpoint.files):
            if not state.existed:
                state.path.unlink(missing_ok=True)
                continue
            fd, tmp_name = tempfile.mkstemp(prefix=f".{state.path.name}.forge-undo-",
                                            dir=state.path.parent)
            tmp = pathlib.Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(state.content)
                if state.mode is not None:
                    os.chmod(tmp, state.mode)
                os.replace(tmp, state.path)
            finally:
                if tmp.exists():
                    tmp.unlink()

    def undo(self) -> Checkpoint:
        if not self.entries:
            raise LookupError("there are no file changes to undo")
        checkpoint = self.entries[-1]
        self.restore(checkpoint)
        self.entries.pop()
        return checkpoint
