"""Where a dataset's answers are read and written.

The answers file keeps the shape it has always had in verbatim-index,
`{"labels", "attribution", "spans", "corrections"}`, so old files and their
hashes keep working. `FileStore` is the version-2 store the Phase 1 server uses:
it keeps a copy in memory and takes no lock, so it must not serve a file anything
else writes. `DatasetStore` is the service's store for version-3 datasets.
"""
from __future__ import annotations

import contextlib
import fcntl
import glob
import hashlib
import json
import math
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

SECTIONS = ("labels", "attribution", "spans", "corrections")


class StaleWrite(Exception):
    """A save that would overwrite a change its page has not seen: the page must reload."""


def fingerprint(path: Path) -> tuple | None:
    """`(inode, size, mtime_ns, sha256)` of `path`, or None when it does not exist."""
    try:
        st = os.stat(path)
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns, hashlib.sha256(data).hexdigest())


class _DuplicateKey(ValueError):
    """A key written twice in one object. A ValueError, so a body parser that already
    turns bad JSON into a refusal does the same here."""

    def __init__(self):
        super().__init__("a key is written twice in one object")


class _NotFinite(ValueError):
    def __init__(self):
        super().__init__("NaN or Infinity is not allowed in JSON")


def _unique_pairs(pairs: list) -> dict:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise _DuplicateKey()
    return out


def _no_constant(name: str):
    raise _NotFinite()


def _finite_float(literal: str) -> float:
    value = float(literal)
    if not math.isfinite(value):  # 1e400 parses as Infinity
        raise _NotFinite()
    return value


def strict_loads(text: str | bytes):
    """`json.loads`, refusing what the standard parser lets through: a key written twice
    in one object (it keeps the last copy without a word), NaN or Infinity, and a number
    too large for a float (it becomes Infinity). Each is a ValueError."""
    return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_no_constant,
                      parse_float=_finite_float)


def read_json_object(path: Path) -> dict:
    """The JSON object stored in `path`, or a refusal that names the file.

    Refuses a file that is not UTF-8, is not JSON (a merge left conflict markers in
    it, say), holds a key twice in one object or NaN or Infinity, holds a number
    Python will not convert, is nested too deeply to parse, or holds something other
    than an object. A missing file still raises FileNotFoundError.
    """
    try:
        data = strict_loads(path.read_text(encoding="utf-8"))
    except _DuplicateKey:
        # The key is not named: refusal text can reach logs, and a key could be anything.
        raise SystemExit(f"REFUSING: {path} holds a key twice in one object, and only the last copy "
                         "would be read") from None
    except _NotFinite:
        raise SystemExit(f"REFUSING: {path} holds NaN or Infinity, which JSON does not allow") from None
    except UnicodeDecodeError:
        raise SystemExit(f"REFUSING: {path} is not UTF-8 text") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"REFUSING: {path} is not valid JSON ({exc.msg}, line {exc.lineno})") from None
    except ValueError as exc:  # after its two subclasses above; an integer of over 4300 digits, say
        raise SystemExit(f"REFUSING: {path} cannot be read as JSON ({exc})") from None
    except RecursionError:
        raise SystemExit(f"REFUSING: {path} is nested too deeply to read") from None
    if not isinstance(data, dict):
        raise SystemExit(f"REFUSING: {path} is not a JSON object")
    return data


def write_atomic(path: str | Path, text: str, encoding: str = "utf-8", expect: tuple | None | bool = False,
                 written: list | None = None) -> Path:
    """Replace `path` with `text` in one step, or leave it exactly as it was.

    Writing to a temporary file in the same directory and renaming over the target
    means a concurrent reader sees either the whole old file or the whole new one.
    Each write uses its own temporary name, so two writers never share one.

    With `expect` (a `fingerprint` result, or None for "must not exist"), the file is
    compared again just before the rename, and a change raises `StaleWrite` and
    writes nothing. A writer that skips this store's lock, such as `git pull`, can
    still land in the instant between that check and the rename; the window is
    microseconds, not the length of a save.

    With `written` (a list), the fingerprint of the new file is appended to it. It is
    taken from the temporary file before the rename, which keeps the inode, size and
    modification time, so no outside write after the rename can be mistaken for ours.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            # The rename is atomic, but only orders against data already handed
            # to the filesystem. Without this a crash can leave a renamed file
            # with no contents.
            os.fsync(fh.fileno())
        if written is not None:
            written.append(fingerprint(tmp))
        if expect is not False and fingerprint(path) != expect:
            raise StaleWrite(f"{path} changed on disk while this save was in progress; reload and try again")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


@contextlib.contextmanager
def locked(path: Path, make_parents: bool = True):
    """Hold an exclusive `flock` on `path` (created if missing) for the block.

    The lock lives on its own file, never on the file it protects: git and
    `os.replace` swap that file's inode, so a lock on it would protect nothing.
    If the lock file is deleted or replaced while this call waits (its folder was
    deleted and made again, say), the lock it got guards a file nobody else will
    open, so it takes the lock on the new file instead, up to LOCK_TRIES times.
    Without `make_parents`, a missing folder is refused instead of made.
    """
    for _ in range(LOCK_TRIES):
        if make_parents:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)  # a state folder under $TL_HOME
        elif not path.parent.is_dir():
            raise SystemExit(f"REFUSING: {path.parent} does not exist; run `tl init`")
        fh = open(path, "ab")  # only locked, never written
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                now = os.stat(path)
            except FileNotFoundError:
                now = None
            held = os.fstat(fh.fileno())
            if now is not None and (now.st_ino, now.st_dev) == (held.st_ino, held.st_dev):
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
                return
        finally:
            fh.close()
    raise SystemExit(f"REFUSING: {path} was replaced {LOCK_TRIES} times while waiting for its lock")


#: How many times `locked` takes the lock again after finding its file replaced.
LOCK_TRIES = 20


def load_answers(path: Path) -> dict:
    """The answers in `path`, every section present. A malformed file stops the caller."""
    if not path.exists():
        return {s: {} for s in SECTIONS}
    raw = read_json_object(path)
    if (not isinstance(raw, dict) or not {"labels", "attribution"} <= set(raw) or set(raw) - set(SECTIONS)
            or not all(isinstance(v, dict) for v in raw.values())):
        raise SystemExit(f"REFUSING: {path} is not an answers file ({{'labels': ..., 'attribution': ...}})")
    return {s: raw.get(s, {}) for s in SECTIONS}


def dump_answers(answers: dict) -> str:
    """The answers file's bytes. Every writer uses this, so the format cannot drift."""
    return json.dumps(answers, indent=1, ensure_ascii=False, allow_nan=False) + "\n"


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


def _clear_temps(path: Path) -> None:
    """Remove temporary files a crashed save of `path` left. Call only under the file's lock."""
    for tmp in path.parent.glob(f".{glob.escape(path.name)}.*.tmp"):
        tmp.unlink(missing_ok=True)


class _GuardedJSON:
    """A JSON file the service writes under its dataset lock, refusing saves from a page
    that has not seen the file's latest outside change.

    The store remembers the fingerprint of the file as it last read or wrote it. Each
    read and each save compares the file on disk with that fingerprint first. A
    difference is an outside change, such as a `git pull` or a revert, and the store
    counts it. A page receives a version with what it loads: a token unique to this
    store object and the count of outside changes so far. A save sends the version
    back, and the store refuses it with `StaleWrite` when an outside change was counted
    after that version was issued, or when the version came from another store object:
    a server process that has since restarted, or the other file of the dataset. The
    page then says to reload. Saves from pages that loaded after the last outside
    change merge as before, because each save changes the current file.

    Keep ONE store object per file for the life of the server. A second object on the
    same file would count the first one's saves as outside changes; because versions
    carry the object's token, that mistake refuses pages loudly instead of accepting
    a stale save.

    The store's own writes never count: their fingerprint is taken from the temporary
    file before the rename. The first read of a new store object counts nothing,
    because there is nothing to compare with yet; versions issued before it are
    refused by the token instead. A symlinked file is refused: the rename would
    replace the link, and labels would stop reaching the file it pointed to.
    """

    def __init__(self, path: Path, lock: Path):
        self.path = path
        self.lock = lock
        self.where = str(path)
        self._seen: tuple | None | bool = False  # False: never read by this store
        self._outside = 0
        self._token = secrets.token_hex(6)
        self._mutex = threading.Lock()

    def _read(self):  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    def _dump(self, value) -> str:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    def _check(self, value) -> None:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    def _version(self) -> str:
        """Issued only with a read, so no caller can get a version without the file's check."""
        return f"{self._token}.{self._outside}"

    def _observe(self, fp) -> None:
        """Count an outside change if the file is not the one this store last saw. Call under the mutex."""
        if self._seen is not False and fp != self._seen:
            self._outside += 1
        self._seen = fp

    def _read_stable(self) -> tuple:
        """`(value, fingerprint)`, refusing a file that changed while it was read."""
        if self.path.is_symlink():
            raise SystemExit(f"REFUSING: {self.path} is a symlink; a save would replace the link, so labels "
                             "would stop reaching the file it points to. Replace it with the real file.")
        before = fingerprint(self.path)
        value = self._read()
        if fingerprint(self.path) != before:
            raise StaleWrite(f"{self.path} changed on disk while it was read; reload and try again")
        return value, before

    def load_versioned(self) -> tuple:
        """`(value, version)`: what a page shows, and the version its saves must send back."""
        with self._mutex:
            value, fp = self._read_stable()
            self._observe(fp)
            return value, self._version()

    def load(self):
        return self.load_versioned()[0]

    def update_hashed(self, change: Callable, version: str) -> tuple:
        """`(stored value, sha256 of the bytes written, version)` for a save from a page that loaded `version`."""
        if not isinstance(version, str) or not version:
            raise ValueError("a save must carry the version its page loaded")
        with locked(self.lock), self._mutex:
            _clear_temps(self.path)
            current, before = self._read_stable()
            self._observe(before)
            if version != self._version():
                raise StaleWrite(f"{self.path} changed outside this page since it loaded (another program, such "
                                 "as git, wrote it, or the server restarted); reload the page and try again")
            updated = change(json.loads(json.dumps(current)))
            self._check(updated)
            text = self._dump(updated)
            written: list = []
            write_atomic(self.path, text, expect=before, written=written)
            self._seen = written[0]
            return updated, hashlib.sha256(text.encode()).hexdigest(), self._version()

    def update(self, change: Callable, version: str):
        return self.update_hashed(change, version)[0]


class DatasetStore(_GuardedJSON):
    """A version-3 dataset's answers file (see `_GuardedJSON`)."""

    def _read(self) -> dict:
        return load_answers(self.path)

    def _dump(self, value: dict) -> str:
        return dump_answers(value)

    def _check(self, value: dict) -> None:
        if set(value) != set(SECTIONS):
            raise ValueError(f"an update must keep exactly the sections {SECTIONS}")


REVIEW_SECTIONS = ("notes", "done", "reveals", "finished")


def empty_review() -> dict:
    return {"schema_version": 1, "notes": {}, "done": {}, "reveals": {}, "finished": None}


def load_review(path: Path) -> dict:
    """review.json: notes, item done marks, reveals and the dataset's Finish. A bad file stops the caller."""
    if not path.exists():
        return empty_review()
    raw = read_json_object(path)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1 or set(raw) != {"schema_version",
                                                                                     *REVIEW_SECTIONS}:
        raise SystemExit(f"REFUSING: {path} is not a version-1 review file")
    return raw


class ReviewStore(_GuardedJSON):
    """review.json, owned by the server (see `_GuardedJSON`).

    It is created on the first note, mark or reveal, so a dataset nobody reviewed has
    no review file.
    """

    def _read(self) -> dict:
        return load_review(self.path)

    def _dump(self, value: dict) -> str:
        return json.dumps(value, indent=1, ensure_ascii=False, allow_nan=False) + "\n"

    def _check(self, value: dict) -> None:
        if set(value) != {"schema_version", *REVIEW_SECTIONS}:
            raise ValueError("an update must keep exactly the review sections")


class EventLog:
    """`events.jsonl` in a dataset's state folder: one line per browser write, never any text.

    Each line holds the time, the kind (answer, note, reveal, done, finish), the id,
    the principal, the kind of login, and the sha256 of the file after the write.
    """

    def __init__(self, path: Path):
        self.path = path

    def append(self, kind: str, ident: str, principal: str, login: str, sha256_after: str | None,
               now: str | None = None) -> dict:
        line = {"time": now or datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind,
                "id": ident, "principal": principal, "login": login, "sha256_after": sha256_after}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n")
        return line

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise SystemExit(f"REFUSING: {self.path} is not UTF-8 text") from None
        lines = []
        # Split on "\n" alone: append writes U+2028, U+2029 and U+0085 unescaped, and
        # splitlines() would cut a line at each of them.
        for n, x in enumerate(text.split("\n"), 1):
            if not x.strip():
                continue
            try:
                line = strict_loads(x)
            except (_DuplicateKey, _NotFinite):
                raise SystemExit(f"REFUSING: {self.path} line {n} holds a key twice or NaN or Infinity") from None
            except ValueError as exc:  # also catches a JSONDecodeError
                raise SystemExit(f"REFUSING: {self.path} line {n} is not valid JSON ({exc})") from None
            except RecursionError:
                raise SystemExit(f"REFUSING: {self.path} line {n} is nested too deeply to read") from None
            if not isinstance(line, dict):
                raise SystemExit(f"REFUSING: {self.path} line {n} is not a JSON object")
            lines.append(line)
        return lines
