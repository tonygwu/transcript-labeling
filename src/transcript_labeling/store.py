"""Where a dataset's answers are read and written.

The answers file keeps the shape it has always had in verbatim-index,
`{"labels", "attribution", "spans", "corrections"}`, so old files and their
hashes keep working. Only `FileStore` exists: the answers file on disk. The
server talks to the `Store` interface, so a remote store could replace the file
without touching the page.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Callable, Protocol

SECTIONS = ("labels", "attribution", "spans", "corrections")


def write_atomic(path: str | Path, text: str, encoding: str = "utf-8") -> Path:
    """Replace `path` with `text` in one step, or leave it exactly as it was.

    Writing to a temporary file in the same directory and renaming over the target
    means a concurrent reader sees either the whole old file or the whole new one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            # The rename is atomic, but only orders against data already handed
            # to the filesystem. Without this a crash can leave a renamed file
            # with no contents.
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


@contextlib.contextmanager
def locked(path: Path):
    """Hold an exclusive `flock` on `path` (created if missing) for the block.

    The lock lives on its own file, never on the file it protects: git and
    `os.replace` swap that file's inode, so a lock on it would protect nothing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_answers(path: Path) -> dict:
    """The answers in `path`, every section present. A malformed file stops the caller."""
    if not path.exists():
        return {s: {} for s in SECTIONS}
    raw = json.loads(path.read_text())
    if (not isinstance(raw, dict) or not {"labels", "attribution"} <= set(raw) or set(raw) - set(SECTIONS)
            or not all(isinstance(v, dict) for v in raw.values())):
        raise SystemExit(f"REFUSING: {path} is not an answers file ({{'labels': ..., 'attribution': ...}})")
    return {s: raw.get(s, {}) for s in SECTIONS}


def dump_answers(answers: dict) -> str:
    """The answers file's bytes. Every writer uses this, so the format cannot drift."""
    return json.dumps(answers, indent=1, ensure_ascii=False) + "\n"


class Store(Protocol):
    where: str

    def load(self) -> dict:
        """A copy of every answer, by section."""

    def update(self, change: Callable[[dict], dict]) -> dict:
        """Apply `change` to the current answers and persist the result atomically.

        `change` receives the current answers and returns the new ones. If it raises,
        nothing is written and the error reaches the caller. Returns what was stored.
        """


class FileStore:
    """The dataset's answers file, written whole and atomically under one lock."""

    def __init__(self, path: Path):
        self.path = path
        self.where = str(path)
        self._lock = threading.Lock()
        self._answers = load_answers(path)

    def load(self) -> dict:
        with self._lock:
            return {s: dict(self._answers[s]) for s in SECTIONS}

    def update(self, change: Callable[[dict], dict]) -> dict:
        with self._lock:
            updated = change({s: dict(self._answers[s]) for s in SECTIONS})
            if set(updated) != set(SECTIONS):
                raise ValueError(f"an update must keep exactly the sections {SECTIONS}")
            write_atomic(self.path, dump_answers(updated))
            self._answers = updated
            return {s: dict(updated[s]) for s in SECTIONS}
